#!/usr/bin/env python3
"""Standalone Whisper ASR spike on existing project audio.

This is intentionally NOT integrated into the app. It reads a project's current
speech lines from project.json, transcribes the project's ASR audio with a local
faster-whisper model, and writes a comparison report.

Install the experimental dependency first:

    .venv/Scripts/python -m pip install faster-whisper

Examples:

    python scripts/asr_whisper_test.py --project-id a00820...
    python scripts/asr_whisper_test.py --all --limit 2
    python scripts/asr_whisper_test.py --all --model small --device cpu --compute-type int8

The report is written to backend/data/whisper-test/<project_id>.json.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import wave
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BACKEND_DIR / "data"
REPORT_DIR = DATA_DIR / "whisper-test"
sys.path.insert(0, str(BACKEND_DIR))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def normalize(text: str) -> str:
    """Loose text normalization for a rough CER comparison."""
    text = re.sub(r"\[[^\]]*\]|【[^】]*】", " ", text or "")
    text = text.lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    return text


def levenshtein(a: str, b: str) -> int:
    """Two-row edit distance; enough for a short test report."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(
                current[j - 1] + 1,
                previous[j] + 1,
                previous[j - 1] + (ca != cb),
            ))
        previous = current
    return previous[-1]


def cer(reference: str, hypothesis: str) -> float | None:
    ref = normalize(reference)
    hyp = normalize(hypothesis)
    if not ref:
        return None
    return round(levenshtein(ref, hyp) / len(ref), 4)


def wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as f:
            return f.getnframes() / float(f.getframerate())
    except (OSError, wave.Error, ZeroDivisionError):
        return None


def project_dirs(ids: list[str], limit: int | None) -> list[Path]:
    if ids:
        dirs = [DATA_DIR / i for i in ids]
    else:
        dirs = sorted(
            p for p in DATA_DIR.iterdir()
            if p.is_dir() and (p / "project.json").is_file()
        )
    if limit is not None:
        dirs = dirs[:limit]
    return dirs


def load_project(project_dir: Path) -> tuple[dict, list[dict]]:
    state = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
    refs = [
        {
            "speaker": s.get("speaker_id"),
            "start": s.get("start_time"),
            "end": s.get("end_time"),
            "text": s.get("text", ""),
            "reviewed": s.get("reviewed_text", ""),
        }
        for s in state.get("segments", [])
        if s.get("kind") == "speech" and (s.get("text") or "").strip()
    ]
    return state, refs


def choose_audio(project_dir: Path) -> Path | None:
    # vowels_asr is what the remote ASR consumed; fall back for older projects.
    for name in ("vocals_asr.wav", "extracted_audio.wav", "vocals.wav", "original_video.mp4"):
        path = project_dir / name
        if path.is_file():
            return path
    return None


def run_one(model, project_dir: Path, language: str | None) -> dict:
    state, refs = load_project(project_dir)
    audio = choose_audio(project_dir)
    if audio is None:
        raise SystemExit(f"no audio found for {project_dir.name}")

    duration = wav_duration(audio) if audio.suffix.lower() == ".wav" else None
    started = time.perf_counter()
    segments, info = model.transcribe(
        str(audio),
        language=language,
        beam_size=5,
        vad_filter=True,
        word_timestamps=False,
        condition_on_previous_text=False,
        temperature=0.0,
    )
    hypotheses = [
        {
            "start": round(float(s.start), 2),
            "end": round(float(s.end), 2),
            "text": (s.text or "").strip(),
        }
        for s in segments
        if (s.text or "").strip()
    ]
    elapsed = time.perf_counter() - started

    reference_text = " ".join(r["text"] for r in refs)
    hypothesis_text = " ".join(h["text"] for h in hypotheses)
    report = {
        "project_id": project_dir.name,
        "filename": state.get("filename"),
        "audio": audio.name,
        "audio_seconds": round(duration, 2) if duration else None,
        "model": getattr(model, "model_size_or_path", "unknown"),
        "detected_language": getattr(info, "language", None),
        "language_probability": round(float(getattr(info, "language_probability", 0.0)), 4),
        "elapsed_seconds": round(elapsed, 2),
        "real_time_factor": round(elapsed / duration, 3) if duration else None,
        "reference_lines": refs,
        "hypothesis_lines": hypotheses,
        "reference_chars": len(normalize(reference_text)),
        "hypothesis_chars": len(normalize(hypothesis_text)),
        "cer": cer(reference_text, hypothesis_text),
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"{project_dir.name}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def print_report(report: dict) -> None:
    print(f"\n=== {report['project_id']} / {report['filename']} ===")
    print(f"audio       {report['audio']}  {report['audio_seconds']}s")
    print(f"language    {report['detected_language']} ({report['language_probability']})")
    print(f"elapsed     {report['elapsed_seconds']}s  RTF={report['real_time_factor']}")
    print(f"lines       reference={len(report['reference_lines'])}  whisper={len(report['hypothesis_lines'])}")
    print(f"CER(rough)  {report['cer']}")
    print("reference:")
    for r in report["reference_lines"][:8]:
        print(f"  [{r['start']}-{r['end']}] {r['text'][:120]}")
    print("whisper:")
    for h in report["hypothesis_lines"][:8]:
        print(f"  [{h['start']}-{h['end']}] {h['text'][:120]}")


def write_summary(reports: list[dict]) -> None:
    lines = [
        "# Whisper ASR spike summary",
        "",
        "| project | language | audio s | elapsed s | RTF | ref lines | whisper lines | CER |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in reports:
        lines.append(
            f"| {r['project_id']} | {r['detected_language']} | {r['audio_seconds']} | "
            f"{r['elapsed_seconds']} | {r['real_time_factor']} | "
            f"{len(r['reference_lines'])} | {len(r['hypothesis_lines'])} | {r['cer']} |"
        )
    lines += [
        "",
        "CER is a rough normalized character error rate against the existing reviewed/source text.",
        "It does not measure speaker diarization, and song/vocal segments can make the number look worse.",
        "",
    ]
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", action="append", default=[], help="project id (repeatable)")
    parser.add_argument("--all", action="store_true", help="all projects under backend/data")
    parser.add_argument("--limit", type=int, default=None, help="max projects")
    parser.add_argument("--model", default="base", help="faster-whisper model: tiny/base/small/medium/large-v3")
    parser.add_argument("--device", default="cpu", choices=["cpu", "cuda", "auto"])
    parser.add_argument("--compute-type", default="int8", help="int8, int8_float16, float16, float32")
    parser.add_argument("--language", default=None, help="force language, e.g. en / zh; default auto")
    args = parser.parse_args()

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("Missing faster-whisper. Install with:\n"
              "  .venv/Scripts/python -m pip install faster-whisper", file=sys.stderr)
        return 2

    if not args.project_id and not args.all:
        parser.error("pass --project-id or --all")

    print(f"loading whisper {args.model} ({args.device}, {args.compute_type}) ...")
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)

    failures = 0
    reports: list[dict] = []
    for project_dir in project_dirs(args.project_id, args.limit):
        try:
            report = run_one(model, project_dir, args.language)
            reports.append(report)
            print_report(report)
        except Exception as e:
            failures += 1
            print(f"FAILED {project_dir.name}: {e}", file=sys.stderr)
    if reports:
        write_summary(reports)
    print(f"\nreports: {REPORT_DIR}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
