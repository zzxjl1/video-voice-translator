#!/usr/bin/env python3
"""
用项目真实的 ASR 路径（asr_service：提交 → 轮询 → 解析）跑一遍样本，保存原始结果当基线。

和流水线唯一的区别：音频不是放在公网服务器上，而是传到百炼的临时存储（oss:// URL，
48 小时有效）。文档说明 REST 接口支持这种地址；asr_service 走的正是 REST，所以这里只给
请求头加一个 X-DashScope-OssResourceResolve，其余原样复用，不改项目代码。

跑两遍（各自一次真实调用，音频约 35 秒）：
  mix     没做人声分离时流水线送去的音频（视频抽出的 16k 单声道）
  vocals  人声分离之后送去的音频（这里用理想的人声轨代替）

需要 backend/.env 里有 DASHSCOPE_API_KEY。用法：
    python3 run_asr.py
"""
import asyncio
import json
import os
import sys
import tempfile

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.abspath(os.path.join(HERE, "..", "..", "backend"))
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
from dotenv import dotenv_values  # noqa: E402

for k, v in dotenv_values(os.path.join(BACKEND, ".env")).items():
    os.environ.setdefault(k, v)

from app import config  # noqa: E402
from app.services import asr_service  # noqa: E402

OUT = os.path.join(HERE, "asr_baseline")
UPLOAD_API = "https://dashscope.aliyuncs.com/api/v1/uploads"


async def upload_temp(path: str, model: str) -> str:
    """百炼临时存储：先取上传凭证，再把文件 POST 到 OSS，返回 oss:// 地址。"""
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.get(UPLOAD_API, params={"action": "getPolicy", "model": model},
                        headers={"Authorization": f"Bearer {config.DASHSCOPE_API_KEY}"})
        r.raise_for_status()
        p = r.json()["data"]
        key = f"{p['upload_dir']}/{os.path.basename(path)}"
        with open(path, "rb") as f:
            resp = await c.post(p["upload_host"], data={
                "OSSAccessKeyId": p["oss_access_key_id"], "Signature": p["signature"], "policy": p["policy"],
                "x-oss-object-acl": p["x_oss_object_acl"], "x-oss-forbid-overwrite": p["x_oss_forbid_overwrite"],
                "key": key, "success_action_status": "200",
            }, files={"file": (os.path.basename(path), f)})
        resp.raise_for_status()
    return f"oss://{key}"


def with_oss_header():
    original = asr_service._asr_headers
    asr_service._asr_headers = lambda: {**original(), "X-DashScope-OssResourceResolve": "enable"}


async def run_one(name: str, wav: str) -> dict:
    model = config.ASR_MODEL
    url = await upload_temp(wav, model)
    print(f"[{name}] 已上传 → {url[:60]}…  提交 {model}（diarization=True）")
    results, usage = await asr_service._run_transcription(url, model, True)
    lines = await asr_service.parse_asr_results(results)
    # 同时存下服务端返回的完整转写 JSON（含 asr_service 解析时丢掉的字段，比如词级时间戳）
    raw = []
    async with httpx.AsyncClient(timeout=60) as c:
        for res in results:
            if res.get("transcription_url"):
                t = (await c.get(res["transcription_url"])).json()
                t["file_url"] = "<redacted temporary signed url>"      # 带签名的临时地址，不落盘
                raw.append(t)
    doc = {"model": model, "diarization": True, "usage": usage, "parsed_lines": lines, "raw_transcripts": raw}
    with open(os.path.join(OUT, f"asr_{name}.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
    return doc


async def main():
    if not config.DASHSCOPE_API_KEY:
        raise SystemExit("backend/.env 里没有 DASHSCOPE_API_KEY")
    os.makedirs(OUT, exist_ok=True)
    with_oss_header()
    with tempfile.TemporaryDirectory() as d:
        mix = asr_service.extract_audio(os.path.join(HERE, "problem_sample.mp4"), os.path.join(d, "mix_16k.wav"))
        voc = asr_service.extract_audio(os.path.join(HERE, "speech_stem.flac"), os.path.join(d, "vocals_16k.wav"))
        for name, wav in (("mix", mix), ("vocals", voc)):
            doc = await run_one(name, wav)
            print(f"[{name}] {len(doc['parsed_lines'])} 句，usage={doc['usage']}")


if __name__ == "__main__":
    asyncio.run(main())
