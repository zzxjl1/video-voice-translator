#!/usr/bin/env python3
"""One-shot migration: enable_dubbing bool -> dubbing_mode enum.

No runtime compatibility is kept in the app. Run this once over backend/data.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"


def main() -> int:
    changed = 0
    for path in sorted(DATA_DIR.glob("*/project.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "dubbing_mode" in data:
            continue
        old = data.pop("enable_dubbing", True)
        data["dubbing_mode"] = "remote" if old else "off"
        data.setdefault("browser_voice", None)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        changed += 1
        print(f"{path.parent.name}: enable_dubbing={old} -> dubbing_mode={data['dubbing_mode']}")
    print(f"migrated {changed} projects")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
