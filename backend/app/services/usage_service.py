"""
Per-video cost ledger — the one place every billable pipeline step records spend.

Priority, stated once here because every step will eventually route through it:

  1. If the inference API itself returned a cost, use it verbatim.
  2. Else, if the local price table has an entry for (step, model), compute the
     cost from the usage the API returned.
  3. Else, record the call with cost=None and `cost_source: "no_price"` — a
     missing price is REPORTED as missing, never guessed.

The table only contains prices verified against the official billing page
(help.aliyun.com/zh/model-studio/billing/, fetched 2026-10-07, Beijing region,
list prices — promotions are console-side and invisible here). Models without a
verified entry are deliberately absent: adding one is a single table line, and
a wrong price misleads a human, never the pipeline.

Ledger shape, per video (`data/<video_id>/usage.json`):

    { "entries": [ {at, step, model, detail, usage{...raw}, input_tokens,
                    output_tokens, cost_yuan, cost_source, price_note?} ],
      "totals": { "calls": n, "cost_yuan": x, "cost_missing_calls": m,
                  "by_step": { step: {calls, cost_yuan, cost_missing} } } }

Wired today: tts. Planned next: asr, llm, clone (enrollment), separation (302.AI).
"""

import json
import logging
import os
import threading
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

from app.models import get_video_dir

logger = logging.getLogger(__name__)

# Ledger timestamps are recorded in Beijing time WITH offset, so the peak /
# off-peak determination below is correct no matter which timezone the server
# runs in, and a human reading the file can compare it against DeepSeek's own
# billing page.
_BEIJING = ZoneInfo("Asia/Shanghai")

_LEDGER_FILENAME = "usage.json"
_lock = threading.Lock()

# ("step", "model") -> pricing rule. Supported rules:
#   {"input_per_mtoken": x, "output_per_mtoken": y}   token-based (ASR/TTS/LLM)
#   {"per_unit": x}                                   one-off per call (clone)
_PRICE_TABLE: dict[tuple[str, str], dict] = {
    ("tts", "qwen-audio-3.1-tts-flash"): {
        "input_per_mtoken": 1.5, "output_per_mtoken": 12.0,
    },
    ("asr", "qwen-audio-3.1-asr-flash-filetrans"): {
        "input_per_mtoken": 0.8, "output_per_mtoken": 2.7,
    },
    # Enrollment (voice cloning) bills per NEW voice; the account also has a
    # 1000-voice free quota that this ledger cannot observe, so the computed
    # figure is the gross price, not necessarily what is actually invoiced.
    ("clone", "qwen-voice-enrollment"): {"per_unit": 0.01},
    # deepseek-flash — DeepSeek OFFICIAL API (api-docs.deepseek.com, fetched
    # 2026-10-07, CNY per Mtok). Billing splits three ways: cached vs uncached
    # input, and peak vs off-peak (off-peak = half price; Beijing time Mon-Fri
    # 9:00-12:00 and 14:00-18:00 outside CN legal holidays). The usage object
    # carries the cache split (prompt_cache_hit_tokens), so the computation is
    # exact except for the holiday calendar, which is treated as peak — the
    # conservative reading.
    ("llm", "deepseek-flash"): {
        "cache_hit_per_mtoken": {"peak": 0.04, "off_peak": 0.02},
        "cache_miss_per_mtoken": {"peak": 2.0, "off_peak": 1.0},
        "output_per_mtoken": {"peak": 8.0, "off_peak": 4.0},
        "peak_windows_beijing": ((9, 0), (12, 0), (14, 0), (18, 0)),
    },
}

# Peak windows for rules that have them, as Beijing-time (start, end) pairs.
_PEAK_WINDOWS_BEIJING = {
    ("llm", "deepseek-flash"): ((dt_time(9, 0), dt_time(12, 0)), (dt_time(14, 0), dt_time(18, 0))),
}

_PRICE_NOTES: dict[tuple[str, str], str] = {
    ("tts", "qwen-audio-3.1-tts-flash"): "免费额度 100 万 token（北京）",
    ("asr", "qwen-audio-3.1-asr-flash-filetrans"): "免费额度 100 万 token（北京）",
    ("clone", "qwen-voice-enrollment"): "免费额度 1000 个音色/账号（北京）；删除音色不返还",
    ("llm", "deepseek-flash"): "峰谷计价：峰=北京工作日9-12/14-18，其余半价；节假日按峰时保守计",
}


def _is_peak(at: datetime, key: tuple[str, str]) -> bool:
    """Peak = Beijing-time Mon-Fri (holidays not visible here) inside the windows."""
    windows = _PEAK_WINDOWS_BEIJING.get(key)
    if not windows:
        return True
    local = at.astimezone(_BEIJING)
    if local.weekday() >= 5:
        return False
    clock = local.time()
    return any(start <= clock < end for start, end in windows)


def _compute_cost(step: str, model: str, entry: dict) -> float | None:
    rule = _PRICE_TABLE.get((step, model))
    if not rule:
        return None
    if "per_unit" in rule:
        return rule["per_unit"]
    if "cache_miss_per_mtoken" in rule:
        # DeepSeek-style three-way split, priced at the tier of THIS call's
        # timestamp. Cache-hit tokens come from the API's own usage split.
        peak = _is_peak(datetime.fromisoformat(entry["at"]), (step, model))
        tier = "peak" if peak else "off_peak"
        hit = entry.get("cache_hit_tokens", 0)
        miss = max(0, entry.get("input_tokens", 0) - hit)
        cost = (
            hit * rule["cache_hit_per_mtoken"][tier]
            + miss * rule["cache_miss_per_mtoken"][tier]
            + entry.get("output_tokens", 0) * rule["output_per_mtoken"][tier]
        ) / 1_000_000
        entry["price_tier"] = tier
        return cost
    cost = 0.0
    cost += entry.get("input_tokens", 0) * rule.get("input_per_mtoken", 0.0) / 1_000_000
    cost += entry.get("output_tokens", 0) * rule.get("output_per_mtoken", 0.0) / 1_000_000
    return cost


def record(
    video_id: str | None,
    step: str,
    model: str,
    detail: str = "",
    usage: dict | None = None,
    api_cost: dict | None = None,
) -> None:
    """
    Add one billable call to the video's ledger.

    `usage` is the RAW usage object the API returned (its shape is preserved
    verbatim inside the entry — normalization happens on the extracted fields
    only, so a shape change never silently reads as zeros).

    `api_cost`, when the API reports money itself, wins over any computation:
    {"amount": float, "currency": "CNY"}.
    """
    if not video_id:
        return

    usage = usage or {}
    entry: dict = {
        "at": datetime.now(_BEIJING).isoformat(timespec="seconds"),
        "step": step,
        "model": model,
        "detail": detail,
        "usage": usage,
    }
    # Known fields, extracted leniently: DashScope chat completions call them
    # prompt_tokens/completion_tokens, the TTS task-finished payload uses
    # input_tokens/output_tokens. Both are accepted. DeepSeek additionally
    # splits the prompt into cache-hit vs cache-miss, which the price table
    # needs — recorded verbatim, the miss side is derived at compute time.
    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    if input_tokens:
        entry["input_tokens"] = input_tokens
    if output_tokens:
        entry["output_tokens"] = output_tokens
    cache_hit = int(usage.get("prompt_cache_hit_tokens") or usage.get("cache_hit_tokens") or 0)
    if cache_hit:
        entry["cache_hit_tokens"] = cache_hit
    characters = int(usage.get("characters") or 0)
    if characters:
        entry["characters"] = characters

    api_amount = (api_cost or {}).get("amount")
    if api_amount is not None:
        entry["cost_yuan"] = float(api_amount)
        entry["cost_source"] = "api"
    else:
        computed = _compute_cost(step, model, entry)
        if computed is None:
            entry["cost_source"] = "no_price"
        else:
            entry["cost_yuan"] = round(computed, 6)
            entry["cost_source"] = "computed"
    note = _PRICE_NOTES.get((step, model))
    if note:
        entry["price_note"] = note

    path = os.path.join(get_video_dir(video_id), _LEDGER_FILENAME)
    try:
        with _lock:
            try:
                with open(path, encoding="utf-8") as f:
                    ledger = json.load(f)
            except (OSError, json.JSONDecodeError):
                ledger = {}

            entries = ledger.get("entries", []) + [entry]
            by_step: dict[str, dict] = {}
            for e in entries:
                bucket = by_step.setdefault(e["step"], {"calls": 0, "cost_yuan": 0.0, "cost_missing": 0})
                bucket["calls"] += 1
                if e.get("cost_yuan") is None:
                    bucket["cost_missing"] += 1
                else:
                    bucket["cost_yuan"] = round(bucket["cost_yuan"] + e["cost_yuan"], 6)

            total_cost = sum(b["cost_yuan"] for b in by_step.values())
            missing = sum(b["cost_missing"] for b in by_step.values())
            ledger["entries"] = entries
            ledger["totals"] = {
                "calls": len(entries),
                "cost_yuan": round(total_cost, 6),
                "cost_missing_calls": missing,
                "by_step": by_step,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(ledger, f, ensure_ascii=False, indent=1)
    except Exception as e:
        # The ledger is auxiliary. A full disk or an unwritable directory must
        # never kill the work it is trying to account for: the API call has
        # already happened and its cost is sunk either way. Log and move on.
        logger.warning(
            f"Usage ledger write failed for {video_id} ({step}/{model}): {e} "
            "— call NOT recorded"
        )
        return

    cost_str = (
        f"¥{entry['cost_yuan']:.6f} [{entry['cost_source']}]"
        if entry.get("cost_yuan") is not None
        else "cost unknown [no_price]"
    )
    logger.info(f"[usage] {video_id} {step}({model}): {cost_str}, video total ¥{total_cost:.6f}")


def summary(video_id: str) -> dict:
    """Read-only view of a video's ledger totals; {} when nothing recorded."""
    path = os.path.join(get_video_dir(video_id), _LEDGER_FILENAME)
    try:
        with open(path, encoding="utf-8") as f:
            ledger = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return ledger.get("totals", {})
