#!/usr/bin/env python3
"""
下载浏览器本地配音（Kokoro）需要的全部文件到 backend/models/kokoro/，由后端同源提供。

为什么要这一步：浏览器里的配音引擎原本在运行时从 HuggingFace / jsDelivr 取模型、音色和
espeak 数据，用户的网络到不了这些站点就整个失败，而且没有任何完整性校验。现在改成部署前
下载一次，运行时只访问本服务器（见 app/routers/models.py 的 /api/models/kokoro/*）。

    python scripts/fetch_kokoro_models.py            # 下载缺的，已有且大小一致的跳过
    python scripts/fetch_kokoro_models.py --verify   # 只校验，不下载（退出码 1 = 有缺失/损坏）

目录布局（transformers.js 的 localModelPath 约定 + 我们自己的两个子目录）：

    kokoro/
      onnx-community/Kokoro-82M-v1.0-ONNX/{config,tokenizer,tokenizer_config}.json
      onnx-community/Kokoro-82M-v1.0-ONNX/onnx/model_fp16.onnx    WebGPU 用（163 MB）
      onnx-community/Kokoro-82M-v1.0-ONNX/onnx/model_quantized.onnx  WASM 用（92 MB）
      voices/<id>.bin                                               54 个音色，每个 0.5 MB
      espeak/espeak-ng.{js,data}, COPYING                           fr/it/pt 的音素化（GPL-3.0）
      manifest.json                                                 文件清单 + sha256

许可：Kokoro 模型 Apache-2.0；espeak-ng GPL-3.0（COPYING 随文件一起放，不要删）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(BACKEND_DIR, "models", "kokoro")

MODEL_REPO = "onnx-community/Kokoro-82M-v1.0-ONNX"
HF_HOSTS = ("https://huggingface.co", "https://hf-mirror.com")   # 前者不通就用镜像
KOKORO_JS = "1.2.1"          # 音色文件随 kokoro-js 的 npm 包发布，每个语言的都有
ESPEAK = "@echogarden/espeak-ng-emscripten@0.3.5"
JSDELIVR = "https://cdn.jsdelivr.net/npm"

MODEL_FILES = [
    "config.json", "tokenizer.json", "tokenizer_config.json",
    "onnx/model_fp16.onnx", "onnx/model_quantized.onnx",
]
ESPEAK_FILES = ["espeak-ng.js", "espeak-ng.data", "COPYING"]


def _voice_ids() -> list[str]:
    url = f"https://data.jsdelivr.com/v1/packages/npm/kokoro-js@{KOKORO_JS}?structure=flat"
    with urllib.request.urlopen(url, timeout=30) as r:
        files = json.load(r)["files"]
    return sorted(f["name"][len("/voices/"):-len(".bin")] for f in files if f["name"].startswith("/voices/"))


def _plan() -> list[tuple[str, list[str]]]:
    """[(相对路径, 候选下载地址…)]"""
    plan: list[tuple[str, list[str]]] = []
    for f in MODEL_FILES:
        plan.append((f"{MODEL_REPO}/{f}", [f"{h}/{MODEL_REPO}/resolve/main/{f}" for h in HF_HOSTS]))
    for v in _voice_ids():
        plan.append((f"voices/{v}.bin", [f"{JSDELIVR}/kokoro-js@{KOKORO_JS}/voices/{v}.bin"]))
    for f in ESPEAK_FILES:
        plan.append((f"espeak/{f}", [f"{JSDELIVR}/{ESPEAK}/{f}"]))
    return plan


def _download(urls: list[str], dest: str) -> None:
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
                    if total:
                        print(f"\r  {os.path.basename(dest)}  {got / 1e6:.0f}/{total / 1e6:.0f} MB", end="", flush=True)
            if total and got != total:
                raise IOError(f"truncated: {got} of {total} bytes")
            os.replace(tmp, dest)
            print()
            return
        except Exception as e:  # noqa: BLE001 - 换下一个源
            last = e
            print(f"\n  ✗ {url}: {e}", file=sys.stderr)
    raise SystemExit(f"download failed for {dest}: {last}")


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verify", action="store_true", help="只校验现有文件，不下载")
    args = ap.parse_args()

    manifest_path = os.path.join(DEST, "manifest.json")
    old: dict = {}
    if os.path.exists(manifest_path):
        old = json.load(open(manifest_path))["files"]

    files: dict[str, dict] = {}
    bad = 0
    for rel, urls in _plan():
        path = os.path.join(DEST, rel)
        known = old.get(rel)
        ok = os.path.exists(path) and known and os.path.getsize(path) == known["size"]
        if not ok:
            if args.verify:
                print(f"missing/changed: {rel}", file=sys.stderr)
                bad += 1
                continue
            print(f"fetch {rel}")
            _download(urls, path)
        files[rel] = {"size": os.path.getsize(path), "sha256": known["sha256"] if ok else _sha256(path)}

    if args.verify:
        print("ok" if not bad else f"{bad} file(s) missing")
        return 1 if bad else 0
    with open(manifest_path, "w") as f:
        json.dump({"files": files}, f, indent=1, sort_keys=True)
    print(f"done: {len(files)} files, {sum(v['size'] for v in files.values()) / 1e6:.0f} MB -> {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
