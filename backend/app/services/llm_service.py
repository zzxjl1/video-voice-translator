"""
LLM translation service (OpenAI-compatible endpoint, e.g. SiliconFlow).

Design notes
------------
A whole script can easily exceed what a single request/response can carry
safely: the model either runs out of context or gets cut off mid-JSON. So the
script is translated **in chunks** with a small overlap that is supplied as
read-only context, and each chunk's response is parsed and validated on its own.

Two dubbing-specific pieces are layered on top:

* **Glossary** — names / jargon / product terms are extracted once from the
  full source script and injected into every chunk, so terminology stays
  identical across chunk boundaries.
* **Length budget** — every segment knows how long its time slot is, so the
  model is asked to produce a line that fits the speaking rate of the target
  language. This is what keeps the dubbed audio roughly aligned with the video
  instead of being time-stretched on playback.
"""
import datetime
import json
import logging
import os
import re
from typing import Any, Optional

from openai import AsyncOpenAI

from app import config
from app.models import get_video_dir
from app.services import usage_service

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(
    api_key=config.LLM_API_KEY,
    base_url=config.LLM_BASE_URL,
    timeout=config.LLM_TIMEOUT,
)

_SYSTEM_PROMPT = (
    "You are a professional dubbing translator. "
    "Always respond with valid JSON only — no markdown fences, no commentary."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text_length(text: str, language: str) -> int:
    """
    'Speech length' in the unit the LLM reasons in: characters for CJK, words
    for latin scripts. This is what `maxLength` in the prompt is expressed in.

    For estimating how long a line will TAKE, use `speech_units` instead — a
    character count is a poor proxy for spoken duration.
    """
    if language in ("Chinese", "Japanese", "Korean"):
        # Count non-whitespace characters.
        return len(re.sub(r"\s+", "", text))
    return len(text.split())


_SYLLABLE_RE = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)
_LATIN_WORD_RE = re.compile(r"[A-Za-z]+")
_DIGIT_RUN_RE = re.compile(r"\d+")


def speech_units(text: str) -> float:
    """
    Estimate how many spoken units (roughly syllables) a text contains.

    Duration scales with this, not with the character count, and the two can
    differ by several times:

        Python   6 characters -> ~2 units  (read "pai-sen")
        API      3 characters ->  3 units  (spelled out letter by letter)
        73%      3 characters -> ~8 units  ("qi-shi-san" + "percent")
        ……       2 characters ->  3 units  (a pause; it makes no sound)

    Measured against real synthesis (leave-one-out, 6 samples): predicting
    duration from a character count was 21% off on average; this estimate gets
    that down to 11%. See `config.SPEECH_UNITS_PER_SECOND` for the rate and
    `config.TTS_FIXED_OVERHEAD` for the per-line fixed cost.
    """
    units = float(len(_SYLLABLE_RE.findall(text)))

    for word in _LATIN_WORD_RE.findall(text):
        # Short ALL-CAPS tokens are read letter by letter; ordinary words are
        # read as one syllable, or a couple for longer ones.
        units += len(word) if (word.isupper() and len(word) <= 5) else 1.5

    # Digits are read one at a time.
    units += sum(len(run) for run in _DIGIT_RUN_RE.findall(text))

    # An ellipsis or dash is a pause: no sound, but it costs time.
    units += (text.count("…") + text.count("——")) * 1.5

    return units


def predict_duration(text: str, language: str) -> float:
    """
    Best-effort spoken duration for `text`, in seconds.

    Linear rather than proportional for a reason: a line costs a fixed amount of
    time regardless of length (measured: a 3-character Chinese line still takes
    0.78s), so a pure ratio badly under-predicts short lines.
    """
    return (
        speech_units(text) / config.speech_units_per_second(language)
        + config.TTS_FIXED_OVERHEAD
    )


def plan_speech_rate(text: str, language: str, target_duration: float) -> float:
    """
    The `speech_rate` that should make `text` fill `target_duration`.

    This is a prediction and it is not precise (measured ~11% average error),
    but it is free — it does not add a TTS call — and it decouples the residual
    from how badly the translation overshot its slot.
    """
    if target_duration <= 0:
        return 1.0
    rate = predict_duration(text, language) / target_duration
    return max(config.TTS_RATE_MIN, min(config.TTS_RATE_MAX, rate))


def _parse_json_array(raw: str) -> list[dict]:
    """Extract a JSON array from a model response, tolerating fences/preamble."""
    clean = raw.replace("```json", "").replace("```", "").strip()
    start = clean.find("[")
    end = clean.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"No JSON array found in response: {raw[:200]}")
    return json.loads(clean[start : end + 1])


def _dump(path: str, payload: Any, label: str) -> None:
    try:
        if isinstance(payload, (dict, list)):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(str(payload))
    except Exception as e:
        logger.warning(f"Failed to save {label} to {path}: {e}")


def _request_kwargs(*, json_mode: bool) -> dict:
    """
    Chat-completion arguments shared by every call, kept in one place so the two
    call sites cannot drift apart.

    Two of these matter more than they look:

    * `max_tokens` — the configured model is a reasoning model, which spends
      part of the completion budget on hidden thinking before writing the
      answer. Without an explicit cap, a long chunk gets truncated mid-JSON.
    * `response_format` — everything downstream parses JSON, so having the
      provider guarantee it removes a whole class of parse failures.

    `reasoning_effort` is passed through `extra_body` rather than as a named
    argument so it works regardless of the installed OpenAI SDK's version.
    """
    extra_body: dict = {"enable_thinking": config.LLM_THINKING_ENABLED}
    if config.LLM_REASONING_EFFORT:
        extra_body["reasoning_effort"] = config.LLM_REASONING_EFFORT

    kwargs: dict = {
        "temperature": config.LLM_TEMPERATURE,
        "max_tokens": config.LLM_MAX_TOKENS,
        "extra_body": extra_body,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return kwargs


async def _chat(
    prompt: str,
    system: str = _SYSTEM_PROMPT,
    video_id: str | None = None,
    detail: str = "",
) -> tuple[str, dict]:
    """
    One chat completion. Returns (content, raw_usage) — and hands the usage to
    the cost ledger, which for DeepSeek needs the cache-hit/miss split the API
    includes in `usage`.
    """
    response = await _client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        **_request_kwargs(json_mode=True),
    )
    try:
        usage = response.usage.model_dump() if response.usage else {}
    except Exception:  # defensive: a shape change must not break translation
        usage = {}
    usage_service.record(
        video_id=video_id,
        step="llm",
        # Keyed by what WE asked for, not response.model: the price table must
        # match the config the request was built from.
        model=config.LLM_MODEL,
        detail=detail,
        usage=usage,
    )
    return (response.choices[0].message.content or "").strip(), usage


# ---------------------------------------------------------------------------
# Glossary
# ---------------------------------------------------------------------------

def _load_local_glossary(video_dir: str) -> dict[str, str]:
    """Optional user/operator supplied glossary shipped with the video."""
    path = os.path.join(video_dir, config.GLOSSARY_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            logger.info(f"Loaded glossary with {len(data)} entries from {path}")
            return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        logger.warning(f"Failed to load glossary {path}: {e}")
    return {}


async def _extract_glossary(
    video_dir: str, segments: list[dict], llm_dir: str, video_id: str | None = None
) -> dict[str, str]:
    """
    Ask the model to list proper nouns / jargon that must stay consistent.
    Output is deliberately small, so this is safe even for long scripts.
    """
    source = "\n".join(seg.get("text", "") for seg in segments)
    # Cap the input; the head of a script already carries most of the names.
    if len(source) > 12000:
        source = source[:12000]

    prompt = f"""Read the script below and extract a terminology glossary that a translator must keep consistent.

Include ONLY:
- people / character names
- place, brand and product names
- domain jargon and recurring technical terms

Exclude ordinary words and anything already unambiguous.

Return a STRICT JSON OBJECT mapping the source term to its best translation
(use "?" if it should stay untranslated). At most {config.GLOSSARY_MAX_TERMS} entries.
Return {{}} if there is nothing worth listing. No markdown.

Script:
{source}
"""

    try:
        raw, _ = await _chat(prompt, video_id=video_id, detail="glossary")
        clean = raw.replace("```json", "").replace("```", "").strip()
        start, end = clean.find("{"), clean.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object found")
        data = json.loads(clean[start : end + 1])
        glossary = {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        glossary = dict(list(glossary.items())[: config.GLOSSARY_MAX_TERMS])
        logger.info(f"Extracted glossary with {len(glossary)} terms")
        _dump(os.path.join(llm_dir, "glossary_extracted.json"), glossary, "glossary")
        return glossary
    except Exception as e:
        logger.warning(f"Glossary extraction failed, continuing without it: {e}")
        return {}


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _chunk(items: list[dict]) -> list[list[dict]]:
    """Split segments into chunks bounded by segment count and character count."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0

    for item in items:
        text_len = len(item.get("text") or "")
        too_many = len(current) >= config.LLM_CHUNK_MAX_SEGMENTS
        too_long = current and (current_chars + text_len) > config.LLM_CHUNK_MAX_CHARS
        if too_many or too_long:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += text_len

    if current:
        chunks.append(current)
    return chunks


def _build_chunk_prompt(
    chunk: list[dict],
    context: list[dict],
    glossary: dict[str, str],
    target_language: str,
) -> str:
    rate = config.speech_rate_value(target_language)
    unit = (
        "characters"
        if target_language in ("Chinese", "Japanese", "Korean")
        else "words"
    )

    parts: list[str] = [
        f"You are dubbing a video into {target_language}.",
        "",
        "Translate every entry in 'Segments to translate' into "
        f"{target_language}, following these rules:",
        "1. Read the whole batch first so the dialogue flows naturally.",
        "2. Match the register of the original (casual stays casual, formal stays formal).",
        "3. Keep each speaker's personality consistent across the script.",
        "4. Write idiomatic lines a native speaker would actually say — avoid translationese.",
        "5. Adapt cultural references so they land for the target audience.",
        "",
        "## Length budget (important — this audio is lip/duration synced)",
        f"Spoken {target_language} runs at roughly {rate} {unit} per second here.",
        "Each segment has `maxLength`, the number of "
        f"{unit} that comfortably fit its time slot. Stay at or below it whenever",
        "the meaning allows. Prefer a shorter, punchier line over a literal long one.",
        "Never pad a line just to reach the limit.",
    ]

    if glossary:
        parts += [
            "",
            "## Glossary (use these exact translations)",
            "\n".join(f"- {k} => {v}" for k, v in glossary.items()),
        ]

    if context:
        parts += [
            "",
            "## Previous lines (ALREADY translated — read for continuity, DO NOT include in the output)",
            json.dumps(context, ensure_ascii=False, indent=2),
        ]

    parts += [
        "",
        "## Segments to translate",
        json.dumps(chunk, ensure_ascii=False, indent=2),
        "",
        "## Output format",
        "Return a STRICT JSON ARRAY. One object per segment, exactly two keys:",
        '[{"id": "<same id as input>", "translatedText": "<translation>"}]',
        "Include ONLY ids listed under 'Segments to translate'. "
        "No markdown fences, no explanation.",
    ]
    return "\n".join(parts)


async def _translate_chunk(
    chunk: list[dict],
    context: list[dict],
    glossary: dict[str, str],
    target_language: str,
    llm_dir: str,
    chunk_index: int,
    video_id: str | None = None,
) -> tuple[dict[str, str], set[str]]:
    """
    Translate one chunk. Returns (id -> translation, failed ids).

    Retries the whole chunk on parse/transport errors; any segment still
    missing after the retries is reported as failed instead of aborting.
    """
    prompt = _build_chunk_prompt(chunk, context, glossary, target_language)
    _dump(os.path.join(llm_dir, f"prompt_chunk{chunk_index:03d}.txt"), prompt, "prompt")

    chunk_ids = {item["id"] for item in chunk}
    last_error: Optional[Exception] = None

    for attempt in range(1, config.LLM_CHUNK_MAX_RETRIES + 2):
        try:
            raw, _ = await _chat(prompt, video_id=video_id, detail=f"chunk{chunk_index:03d}")
            _dump(
                os.path.join(llm_dir, f"response_chunk{chunk_index:03d}_try{attempt}.json"),
                raw,
                "response",
            )
            parsed = _parse_json_array(raw)
            result = {
                str(entry["id"]): str(entry.get("translatedText", "")).strip()
                for entry in parsed
                if isinstance(entry, dict) and entry.get("id") is not None
            }
            # Only accept ids that belong to this chunk.
            result = {k: v for k, v in result.items() if k in chunk_ids}
            missing = chunk_ids - set(result.keys())
            if not missing:
                return result, set()

            last_error = ValueError(f"missing ids: {sorted(missing)[:5]}")
            logger.warning(
                f"Chunk {chunk_index} attempt {attempt} incomplete "
                f"({len(missing)} ids missing), retrying"
            )
            # Give the model another go, this time demanding the missing ids.
            prompt = (
                _build_chunk_prompt(chunk, context, glossary, target_language)
                + "\n\nIMPORTANT: your previous answer was missing these ids, "
                "you MUST include all of them: "
                + ", ".join(sorted(missing))
            )
        except Exception as e:
            last_error = e
            logger.warning(f"Chunk {chunk_index} attempt {attempt} failed: {e}")

    logger.error(f"Chunk {chunk_index} failed after retries: {last_error}")
    return {}, chunk_ids


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def translate_single(text: str, target_language: str) -> str:
    """Translate a single piece of text."""
    response = await _client.chat.completions.create(
        model=config.LLM_MODEL,
        messages=[
            {
                "role": "system",
                "content": (
                    f"You are a professional translator. Translate the given text to "
                    f"{target_language}. Provide ONLY the translation, no explanations."
                ),
            },
            {"role": "user", "content": text},
        ],
        # Plain text, not JSON — do not constrain the output format.
        **_request_kwargs(json_mode=False),
    )
    return (response.choices[0].message.content or "").strip()


async def translate_script(
    video_id: str,
    segments: list[dict],
    target_language: str = "English",
) -> list[dict]:
    """
    Translate an entire script in chunks, with glossary consistency and a
    per-segment length budget.

    Args:
        video_id: Unique ID for the video (used for saved diagnostics).
        segments: dicts with keys id, text, speaker_id, start_time and
            optionally end_time (used to compute the length budget).
        target_language: Target language for translation.

    Returns:
        List of dicts with keys: id, translatedText (the format persisted to
        translation_result.json and consumed by app.models.load_state).
    """
    if not segments:
        return []

    video_dir = get_video_dir(video_id)
    llm_dir = os.path.join(video_dir, "llm")
    os.makedirs(llm_dir, exist_ok=True)

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    rate = config.speech_rate_value(target_language)
    unit = (
        "chars"
        if target_language in ("Chinese", "Japanese", "Korean")
        else "words"
    )

    # ---- Normalise input + compute the per-segment length budget ----
    items: list[dict] = []
    durations: dict[str, float] = {}
    for seg in segments:
        duration = float(seg.get("end_time") or 0) - float(seg.get("start_time") or 0)
        if duration <= 0:
            # Fall back to a very rough estimate from the source text length.
            duration = max(1.0, len(seg.get("text") or "") / 4.0)
        durations[seg["id"]] = duration

        item: dict[str, Any] = {
            "id": seg["id"],
            "speaker": seg.get("speaker_id") or "unknown",
            "text": seg.get("text") or "",
        }
        # Subtract the per-line fixed cost first: a 1s slot does not really fit
        # `rate` characters, because part of that second goes on the lead-in and
        # trailing pause.
        speakable = max(0.3, duration - config.TTS_FIXED_OVERHEAD)
        max_length = int(round(speakable * rate))
        if max_length >= 2:
            item["maxLength"] = max_length
        items.append(item)

    logger.info(
        f"[{video_id}] Translating {len(items)} segments to {target_language} "
        f"({config.LLM_MODEL}, temp={config.LLM_TEMPERATURE})"
    )

    # ---- Glossary: local file wins, then auto-extraction ----
    glossary = _load_local_glossary(video_dir)
    if config.GLOSSARY_AUTO_EXTRACT:
        extracted = await _extract_glossary(video_dir, items, llm_dir, video_id=video_id)
        # Explicit local entries always take precedence over auto-extracted ones.
        merged = dict(extracted)
        merged.update(glossary)
        glossary = merged
    if glossary:
        _dump(os.path.join(llm_dir, f"glossary_{timestamp}.json"), glossary, "glossary")

    # ---- Chunked translation ----
    chunks = _chunk(items)
    logger.info(f"[{video_id}] Split script into {len(chunks)} chunk(s)")

    translations: dict[str, str] = {}
    failed_ids: set[str] = set()

    for index, chunk in enumerate(chunks):
        context: list[dict] = []
        if index > 0 and config.LLM_CHUNK_CONTEXT_SEGMENTS > 0:
            # Tail of the previous chunk, already translated -> continuity.
            tail = chunks[index - 1][-config.LLM_CHUNK_CONTEXT_SEGMENTS :]
            for item in tail:
                translated = translations.get(item["id"])
                if translated:
                    context.append(
                        {
                            "speaker": item["speaker"],
                            "source": item["text"],
                            "translated": translated,
                        }
                    )

        chunk_result, chunk_failed = await _translate_chunk(
            chunk=chunk,
            context=context,
            glossary=glossary,
            target_language=target_language,
            llm_dir=llm_dir,
            chunk_index=index,
            video_id=video_id,
        )
        translations.update(chunk_result)
        failed_ids |= chunk_failed
        logger.info(
            f"[{video_id}] Chunk {index + 1}/{len(chunks)} done "
            f"({len(chunk_result)} translated, {len(chunk_failed)} failed)"
        )

    if not translations:
        raise RuntimeError(
            "Translation produced no results at all — check the LLM credentials "
            f"and the saved prompts in {llm_dir}"
        )

    # ---- Length report: where did the model overshoot its slot? ----
    overshoot: list[dict] = []
    for item in items:
        translated = translations.get(item["id"])
        if not translated:
            continue
        budget = item.get("maxLength")
        if not budget:
            continue
        actual = _text_length(translated, target_language)
        if actual > budget * 1.3:
            target_seconds = durations.get(item["id"], 0.0)
            overshoot.append(
                {
                    "id": item["id"],
                    "budget": budget,
                    "actual": actual,
                    "ratio": round(actual / budget, 2),
                    # The numbers that actually matter downstream: what the TTS
                    # is likely to produce versus the slot it has to fill.
                    "target_seconds": round(target_seconds, 2),
                    "predicted_seconds": round(
                        predict_duration(translated, target_language), 2
                    ),
                    "needed_rate": round(
                        plan_speech_rate(translated, target_language, target_seconds), 2
                    ),
                    "source": item["text"][:80],
                    "translated": translated[:80],
                }
            )

    report = {
        "target_language": target_language,
        "speed_unit": unit,
        "segments_total": len(items),
        "translated": len(translations),
        "failed": len(failed_ids),
        "overshoot_count": len(overshoot),
        "overshoot": overshoot[:50],
        "glossary_terms": len(glossary),
    }
    _dump(
        os.path.join(llm_dir, f"length_report_{timestamp}.json"),
        report,
        "length report",
    )
    if overshoot:
        logger.warning(
            f"[{video_id}] {len(overshoot)} segment(s) exceed their length budget "
            f"by >30% (see length_report_{timestamp}.json)"
        )
    if failed_ids:
        logger.warning(f"[{video_id}] {len(failed_ids)} segment(s) were not translated")

    # ---- Persist in the format expected by models.load_state ----
    ordered = [
        {"id": item["id"], "translatedText": translations[item["id"]]}
        for item in items
        if item["id"] in translations
    ]

    latest_path = os.path.join(video_dir, "translation_result.json")
    _dump(latest_path, ordered, "translation result")
    logger.info(f"[{video_id}] Saved {len(ordered)} translations to {latest_path}")

    return ordered
