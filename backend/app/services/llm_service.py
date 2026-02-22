"""
LLM translation service via SiliconFlow (using OpenAI SDK).
Translates transcript segments with full context awareness.
"""
import datetime
import json
import logging
import os

from openai import AsyncOpenAI

from app import config

logger = logging.getLogger(__name__)

_client = AsyncOpenAI(
    api_key=config.SILICONFLOW_API_KEY,
    base_url=config.SILICONFLOW_BASE_URL,
    timeout=config.LLM_TIMEOUT,
)


from app.models import get_video_dir

async def translate_single(text: str, target_language: str) -> str:
    """Translate a single piece of text."""
    # (Optional: we could also save single translations, but mostly batch is used)
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
        temperature=0.9,
        extra_body={"enable_thinking": config.LLM_THINKING_ENABLED}
    )
    return response.choices[0].message.content.strip()


async def translate_script(
    video_id: str,
    segments: list[dict],
    target_language: str = "English",
) -> list[dict]:
    """
    Translate an entire script with full context.
    
    Args:
        video_id: Unique ID for the video to save context files
        segments: List of dicts with keys: id, text, speaker_id, start_time
        target_language: Target language for translation
    
    Returns:
        List of dicts with keys: id, translated_text
    """
    script_context = json.dumps(segments, ensure_ascii=False, indent=2)

    prompt = f"""
You are a professional video translator with expertise in cross-cultural localization.

Translate the following script to {target_language}. 
Use the provided time and speaker context to ensure the translation flows naturally and maintains the correct tone.
Follow these strict guidelines:

## Core Principles
1. **Context-Aware Translation**: Read the entire script first to understand the flow, relationships between speakers, and conversational dynamics. Do not translate sentences in isolation.
2. **Fluency Over Literalism**: Prioritize natural, idiomatic expression. A slightly looser translation that sounds authentic is preferred over a word-for-word translation that feels stiff or foreign.
3. **Register Adaptation**: Automatically detect and match the tone:
   - Casual conversations → relaxed, colloquial language (e.g., "bro," "dude," slang, contractions)
   - Formal/Professional settings → polished, appropriate formality
   - Comedy/Humor → preserve comedic timing and punchlines, adapt cultural references
   - Arguments/Emotional moments → convey the emotional weight and urgency

## Specific Requirements
- **Conversational Flow**: Ensure responses logically connect to what came before (e.g., "That's why..." should clearly reference the prior context)
- **Speaker Voice**: Maintain consistent personality per speaker across the script
- **No Translationese**: Avoid awkward word order, literal calques, or vocabulary choices that reveal the text was translated
- **Cultural Localization**: Adapt references so they land naturally for {target_language} audiences

## Quality Check
Before outputting, mentally read each line aloud—if it sounds like something a native speaker would actually say in this situation, you've succeeded.

IMPORTANT: 
Return the output as a STRICT JSON ARRAY of objects.
Each object must have exactly two properties: "id" (matching the input) and "translatedText".
Do not wrap the JSON in markdown code blocks. Just return the raw JSON string.

Input Script:
{script_context}
"""

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Save prompt to disk (in llm/ subfolder)
    video_dir = get_video_dir(video_id)
    llm_dir = os.path.join(video_dir, "llm")
    os.makedirs(llm_dir, exist_ok=True)
    
    prompt_path = os.path.join(llm_dir, f"prompt_{timestamp}.txt")
    try:
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write(prompt)
        logger.info(f"Saved LLM prompt to {prompt_path}")
    except Exception as e:
        logger.warning(f"Failed to save LLM prompt: {e}")

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
        temperature=0.9,
        extra_body={"enable_thinking": config.LLM_THINKING_ENABLED}
    )

    result_text = response.choices[0].message.content.strip()

    # Save raw result to disk
    raw_result_path = os.path.join(llm_dir, f"response_{timestamp}.json")
    try:
        with open(raw_result_path, "w", encoding="utf-8") as f:
            f.write(result_text)
        logger.info(f"Saved raw LLM response to {raw_result_path}")
    except Exception as e:
        logger.warning(f"Failed to save raw LLM response: {e}")

    # Clean potential markdown wrapping
    clean = result_text.replace("```json", "").replace("```", "").strip()
    start = clean.find("[")
    end = clean.rfind("]")
    if start == -1 or end == -1:
        logger.error(f"Invalid JSON response from LLM: {result_text}")
        raise ValueError(f"Invalid JSON response from LLM: {result_text[:200]}")
        
    parsed_results = json.loads(clean[start : end + 1])

    # Save latest translation result to video root
    latest_path = os.path.join(video_dir, "translation_result.json")
    try:
        with open(latest_path, "w", encoding="utf-8") as f:
            json.dump(parsed_results, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved latest translation result to {latest_path}")
    except Exception as e:
        logger.warning(f"Failed to save latest translation result: {e}")

    return parsed_results
