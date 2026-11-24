#!/usr/bin/env python3
"""
（维护者用）生成浏览器本地配音（Kokoro）的全部资源到 backend/models/kokoro-82m/。

这些文件**已经提交进代码库**，部署和运行都不需要联网、不需要跑这个脚本。只有在要升级模型、
增减音色、或怀疑文件损坏时才用：

    pip install onnx numpy
    python scripts/prepare_kokoro_models.py            # 下载缺的、转换模型、写 manifest
    python scripts/prepare_kokoro_models.py --verify   # 只校验现有文件（sha256），不联网
                                                       # 退出码 1 = 缺文件 / 内容和 manifest 不一致

产物（目录布局 = transformers.js 的 `localModelPath/<model id>/` 约定，model id 在前端是 "kokoro"）：

    kokoro-82m/
      config.json, tokenizer.json, tokenizer_config.json      来自 onnx-community/Kokoro-82M-v1.0-ONNX
      onnx/model_int8.onnx        官方 model_quantized.onnx 经 kokoro_int8.py 改图后的 WebGPU 可用版（~92MB）
      voices/<id>.bin             应用里能选到的全部音色（来自 kokoro-js 的 npm 包，每个 0.5MB）
      espeak/espeak-ng.{js,data}  fr/it/pt 的音素化（GPL-3.0），COPYING 随文件放着，别删
      manifest.json               每个文件的大小和 sha256

**改了模型内容，必须同时换文件名**（例如 model_int8 -> model_int8_2）并改前端
`features/kokoro/assets.ts` 的 KOKORO_MODEL_FILE：浏览器按 URL 缓存模型，不会重新校验，
同名换文件会让老用户一直用旧模型。`MODEL_FILE` 与前端常量是否一致由 test-kokoro.mjs 检查。

许可：Kokoro 模型 Apache-2.0；espeak-ng GPL-3.0。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import urllib.request

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND_DIR)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEST = os.path.join(BACKEND_DIR, "models", "kokoro-82m")
MODEL_FILE = "model_int8"                    # 必须与 frontend/features/kokoro/assets.ts 的 KOKORO_MODEL_FILE 一致

HF_REPO = "onnx-community/Kokoro-82M-v1.0-ONNX"
HF_HOSTS = ("https://huggingface.co", "https://hf-mirror.com")   # 前者不通就用镜像
SOURCE_MODEL = "onnx/model_quantized.onnx"   # 官方动态 int8，转换的来源
SMALL_FILES = ["config.json", "tokenizer.json", "tokenizer_config.json"]
KOKORO_JS = "1.2.1"                          # 音色文件随 kokoro-js 的 npm 包发布
ESPEAK = "@echogarden/espeak-ng-emscripten@0.3.5"
ESPEAK_FILES = ["espeak-ng.js", "espeak-ng.data", "COPYING"]
JSDELIVR = "https://cdn.jsdelivr.net/npm"


def selectable_voices() -> list[str]:
    """应用里真的能选到的音色：kokoro-js 包里所有 id 以后端语言矩阵里某个前缀开头的。"""
    from app import config
    prefixes = tuple(p for info in config.BROWSER_LANGUAGES.values() for p in info["prefixes"])
    url = f"https://data.jsdelivr.com/v1/packages/npm/kokoro-js@{KOKORO_JS}?structure=flat"
    with urllib.request.urlopen(url, timeout=30) as r:
        files = json.load(r)["files"]
    ids = sorted(f["name"][len("/voices/"):-len(".bin")] for f in files if f["name"].startswith("/voices/"))
    return [v for v in ids if v.startswith(prefixes)]


def download(urls: list[str], dest: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    last: Exception | None = None
    for url in urls:
        try:
            tmp = dest + ".part"
            with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as out:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while chunk := r.read(1 << 20):
                    out.write(chunk)
                    got += len(chunk)
            if total and got != total:
                raise IOError(f"truncated: {got} of {total} bytes")
            os.replace(tmp, dest)
            return
        except Exception as e:  # noqa: BLE001 - 换下一个源
            last = e
            print(f"  x {url}: {e}", file=sys.stderr)
    raise SystemExit(f"download failed for {dest}: {last}")


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def read_manifest() -> dict:
    with open(os.path.join(DEST, "manifest.json"), encoding="utf-8") as f:
        return json.load(f)["files"]


def verify() -> int:
    try:
        files = read_manifest()
    except (OSError, ValueError, KeyError):
        print("manifest.json 缺失或损坏", file=sys.stderr)
        return 1
    bad = 0
    for rel, meta in sorted(files.items()):
        path = os.path.join(DEST, rel)
        if not os.path.isfile(path):
            print(f"缺失: {rel}", file=sys.stderr); bad += 1
        elif os.path.getsize(path) != meta["size"] or sha256(path) != meta["sha256"]:
            print(f"内容与 manifest 不一致: {rel}", file=sys.stderr); bad += 1
    on_disk = {os.path.relpath(os.path.join(d, f), DEST) for d, _, fs in os.walk(DEST) for f in fs} - {"manifest.json"}
    for extra in sorted(on_disk - set(files)):
        print(f"manifest 里没有的文件: {extra}", file=sys.stderr); bad += 1
    print("ok" if not bad else f"{bad} 个问题")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify", action="store_true", help="只校验现有文件，不联网")
    ap.add_argument("--source", help="已下载好的官方 model_quantized.onnx（省一次 92MB 下载）")
    args = ap.parse_args()
    if args.verify:
        return verify()

    from kokoro_int8 import check, convert

    plan: list[tuple[str, list[str]]] = []
    for f in SMALL_FILES:
        plan.append((f, [f"{h}/{HF_REPO}/resolve/main/{f}" for h in HF_HOSTS]))
    for v in selectable_voices():
        plan.append((f"voices/{v}.bin", [f"{JSDELIVR}/kokoro-js@{KOKORO_JS}/voices/{v}.bin"]))
    for f in ESPEAK_FILES:
        plan.append((f"espeak/{f}", [f"{JSDELIVR}/{ESPEAK}/{f}"]))
    for rel, urls in plan:
        path = os.path.join(DEST, rel)
        if not os.path.isfile(path):
            print(f"fetch {rel}")
            download(urls, path)

    model_path = os.path.join(DEST, "onnx", f"{MODEL_FILE}.onnx")
    if not os.path.isfile(model_path):
        with tempfile.TemporaryDirectory() as tmp:
            source = args.source
            if not source:
                source = os.path.join(tmp, "model_quantized.onnx")
                print(f"fetch {SOURCE_MODEL} (官方 int8，转换用)")
                download([f"{h}/{HF_REPO}/resolve/main/{SOURCE_MODEL}" for h in HF_HOSTS], source)
            os.makedirs(os.path.dirname(model_path), exist_ok=True)
            info = convert(source, model_path)
            print(f"convert -> {MODEL_FILE}.onnx  {info['replaced']}")
    left = check(model_path)
    if left:
        raise SystemExit(f"{MODEL_FILE}.onnx 里还有 WebGPU 不支持的算子: {left}")

    files = {}
    for d, _, fs in os.walk(DEST):
        for f in fs:
            if f == "manifest.json" or f.endswith(".part"):
                continue
            path = os.path.join(d, f)
            files[os.path.relpath(path, DEST)] = {"size": os.path.getsize(path), "sha256": sha256(path)}
    with open(os.path.join(DEST, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"files": files}, f, indent=1, sort_keys=True)
    print(f"done: {len(files)} files, {sum(v['size'] for v in files.values()) / 1e6:.0f} MB -> {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
