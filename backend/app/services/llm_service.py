"""
LLM translation service via SiliconFlow (using OpenAI SDK).
Translates transcript segments with full context awareness.
"""
import json
import logging

from openai import AsyncOpenAI

from app import config

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(
    api_key=config.SILICONFLOW_API_KEY,
    base_url=config.SILICONFLOW_BASE_URL,
    timeout=config.LLM_TIMEOUT,
)


async def translate_single(text: str, target_language: str) -> str:
    """Translate a single piece of text."""
    response = await _client.chat.completions.create(
        model=config.SILICONFLOW_MODEL,
        messages=[
            {
                "role": "system",
                "content": f"You are a professional translator. Translate the given text to {target_language}. Provide ONLY the translation, no explanations.",
            },
            {
                "role": "user",
                "content": text,
            },
        ],
        temperature=0.3,
    )
    return response.choices[0].message.content.strip()


async def translate_script(
    segments: list[dict],
    target_language: str = "English",
) -> list[dict]:
    """
    Translate an entire script with full context.
    
    Args:
        segments: List of dicts with keys: id, text, speaker_id, start_time
        target_language: Target language for translation
    
    Returns:
        List of dicts with keys: id, translated_text
    """
    script_context = json.dumps(segments, ensure_ascii=False, indent=2)

    prompt = f"""You are a professional video translator.
Translate the following script to {target_language}.
Use the provided time and speaker context to ensure the translation flows naturally and maintains the correct tone.

IMPORTANT: Return the output as a STRICT JSON ARRAY of objects.
Each object must have exactly two properties: "id" (matching the input) and "translatedText".
Do not wrap the JSON in markdown code blocks. Just return the raw JSON string.

Input Script:
{script_context}"""

    logger.info(f"Submitting translation task to LLM ({config.SILICONFLOW_MODEL})")
    
    response = await _client.chat.completions.create(
        model=config.SILICONFLOW_MODEL,
        messages=[
            {
                "role": "system",
                "content": "You are a professional video translator. Always respond with valid JSON only.",
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        temperature=0.3,
    )

    result_text = response.choices[0].message.content.strip()

    # Clean potential markdown wrapping
    clean = result_text.replace("```json", "").replace("```", "").strip()
    start = clean.find("[")
    end = clean.rfind("]")
    if start == -1 or end == -1:
        logger.error(f"Invalid JSON response from LLM: {result_text}")
        raise ValueError(f"Invalid JSON response from LLM: {result_text[:200]}")

    return json.loads(clean[start : end + 1])
