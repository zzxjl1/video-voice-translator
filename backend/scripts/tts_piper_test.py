#!/usr/bin/env python3
"""Standalone TTS comparison: existing remote TTS vs local Piper.

Reads one project's translated speech lines and its existing per-line TTS
files, synthesizes the same lines with a local Piper voice, and writes a small
HTML page with both players side by side.

This does not touch the app or the project data. It only writes under
backend/data/tts-test/.

Example:

    .venv/Scripts/python -m pip install piper-tts
    .venv/Scripts/python -m piper.download_voices zh_CN-huayan-medium \
        --download-dir backend/data/tts-test/voices
    .venv/Scripts/python backend/scripts/tts_piper_test.py \
        --project-id d24990f5f9d1da0df883566547e41a6f
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
import wave
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BACKEND_DIR / "data"
OUT_DIR = DATA_DIR / "tts-test"
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def clean_tts_text(text: str) -> str:
    return re.sub(r"\[[^\]]*\]|【[^】]*】", " ", text or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--voice", default=str(OUT_DIR / "voices" / "zh_CN-huayan-medium.onnx"))
    parser.add_argument("--config", default=None)
    parser.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()

    try:
        from piper.voice import PiperVoice
    except ImportError:
        print("Missing piper-tts. Install with:\n"
              "  .venv/Scripts/python -m pip install piper-tts", file=sys.stderr)
        return 2

    project_dir = DATA_DIR / args.project_id
    project = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    voice_path = Path(args.voice)
    config_path = Path(args.config) if args.config else voice_path.with_suffix(voice_path.suffix + ".json")
    if not voice_path.is_file() or not config_path.is_file():
        print(f"missing voice files: {voice_path} / {config_path}", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    remote_dir = OUT_DIR / "remote"
    local_dir = OUT_DIR / "piper"
    remote_dir.mkdir(exist_ok=True)
    local_dir.mkdir(exist_ok=True)

    voice = PiperVoice.load(str(voice_path), config_path=str(config_path))
    rows = []
    segments = [
        s for s in project.get("segments", [])
        if s.get("kind") == "speech" and clean_tts_text(s.get("translated_text", ""))
    ][: args.limit]

    for index, segment in enumerate(segments, start=1):
        text = clean_tts_text(segment.get("translated_text", ""))
        source = segment.get("text", "")
        segment_id = segment.get("id", f"line-{index}")
        remote_src = project_dir / "tts" / f"{segment_id}.mp3"
        if not remote_src.is_file():
            print(f"skip {segment_id}: no remote tts/{segment_id}.mp3")
            continue
        remote_name = f"{index:02d}-{segment_id}.mp3"
        shutil.copyfile(remote_src, remote_dir / remote_name)
        local_name = f"{index:02d}-{segment_id}.wav"
        with wave.open(str(local_dir / local_name), "wb") as wav:
            voice.synthesize_wav(text, wav)
        rows.append({
            "index": index,
            "source": source,
            "text": text,
            "remote": f"remote/{remote_name}",
            "local": f"piper/{local_name}",
        })
        print(f"{index:02d} {segment_id}: {text[:60]}")

    cards = []
    for row in rows:
        cards.append(f"""
        <article class="card">
          <div class="meta">#{row['index']:02d} · {html.escape(row['text'])}</div>
          <div class="source">{html.escape(row['source'])}</div>
          <div class="pair">
            <div><div class="label">Remote Qwen TTS</div>
              <audio controls preload="metadata" src="{html.escape(row['remote'])}"></audio></div>
            <div><div class="label">Piper local · zh_CN-huayan-medium</div>
              <audio controls preload="metadata" src="{html.escape(row['local'])}"></audio></div>
          </div>
        </article>""")

    page = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TTS comparison · Qwen vs Piper</title>
<style>
  :root {{ color-scheme: light; --ink:#1f2937; --muted:#8a8f98; --line:#e7e4dc; --bg:#f7f6f2; --accent:#c96442; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--ink); font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  main {{ max-width:980px; margin:0 auto; padding:38px 20px 70px; }}
  h1 {{ font-family:Georgia,serif; font-size:28px; margin:0 0 6px; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:26px; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:18px; padding:18px 20px; margin:14px 0; box-shadow:0 7px 24px rgba(0,0,0,.04); }}
  .meta {{ font-weight:700; font-size:14px; }}
  .source {{ color:var(--muted); font-size:12px; margin:4px 0 12px; }}
  .pair {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}
  .label {{ font-size:10px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; color:var(--muted); margin-bottom:7px; }}
  audio {{ width:100%; height:38px; }}
  .note {{ color:var(--muted); font-size:12px; border-top:1px solid var(--line); padding-top:14px; margin-top:24px; }}
  @media (max-width:700px) {{ .pair {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body><main>
  <h1>TTS comparison</h1>
  <div class="sub">Same translated line · Remote Qwen TTS vs local Piper · {html.escape(project.get('filename',''))}</div>
  {''.join(cards) if cards else '<p>No matching lines found.</p>'}
  <p class="note">Piper 是本地 ONNX 模型，当前测试 voice 为 zh_CN-huayan-medium；它不支持现有 Qwen 的音色克隆/情绪标签。这个页面只是听感对比，不会改动工程数据。</p>
</main></body></html>"""
    (OUT_DIR / "index.html").write_text(page, encoding="utf-8")
    print(f"\nwrote {OUT_DIR / 'index.html'}")
    print("serve with:")
    print(f"  .venv/Scripts/python -m http.server 9200 --bind 127.0.0.1 --directory {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
