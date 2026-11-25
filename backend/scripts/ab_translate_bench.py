"""

题库按"视频翻译里真正会出问题的类别"出，每题标考点；评分是人工的，脚本只负责
把三方的译文并排打出来、算标签保留率和成本。

跑：cd backend && PYTHONPATH=. ./.venv/bin/python scripts/ab_translate.py
"""
import asyncio
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.WARNING)

from openai import AsyncOpenAI
from app import config
from app.services import llm_service

# (考点, 原文)。时长随便给，够放下译文就行；长度预算会按它算。
CASES = [
    # —— 反讽 / 反话：视频里最常见的"意思反了" ——
    ("反讽-自爆", "[excited] If this isn't the most Asian thing ever, I don't know what is."),
    ("反讽-条件句", "[sarcastic] Oh sure, because that's totally how you parallel park."),
    ("反讽-反问", "[amazed] You waited until the day before the deadline? Genius move."),
    ("反讽-双否定", "[bored] It's not like I wanted to come anyway."),
    # —— 习语 / 短语动词：直译就错 ——
    ("习语-spill the beans", "[curious] Come on, spill the beans, who broke the vase?"),
    ("习语-pull an all-nighter", "[tired] I pulled an all-nighter and now I can't think straight."),
    ("习语-hit the sack", "[tired] I'm gonna hit the sack, this is taking forever."),
    ("短语动词-chicken out", "[laughing] Don't tell me you chickened out again."),
    ("习语-under the weather", "[tired] I'm feeling a bit under the weather today."),
    # —— 口语填充 / 重复：ASR 原样输出，译文要像人话 ——
    ("口语-填充词", "So like, I was kinda thinking, maybe we should, you know, just order in?"),
    ("口语-重复结巴", "Okay, here's—here's why this is such a big deal, okay?"),
    ("口语-缩读", "Y'all aren't gonna believe what happened at the gym."),
    # —— 文化专有项：音译 vs 意译 ——
    ("文化-节日", "[excited] We're doing Thanksgiving at my aunt's place this year."),
    ("文化-体育", "[shouting] That buzzer beater was insane!"),
    ("文化-品牌/店", "Let's just grab In-N-Out and call it a day."),
    ("文化-俚语-地域", "That's hella far from here, dude."),
    # —— 人称 / 性别 / 数量：英文不显，中文必须显 ——
    ("人称-性别中立", "My partner is picking up the kids from school."),
    ("数量-单复数歧义", "[curious] Do you have any siblings?"),
    ("敬语-对长辈", "Would you mind repeating that, sir?"),
    # —— 幽默 / 双关：译不出就意译，不能死译 ——
    ("双关-time flies", "[laughing] Time flies like an arrow; fruit flies like a banana."),
    ("幽默-夸张", "[excited] I'm so hungry I could eat a whole cow."),
    ("幽默-自嘲", "[sarcastic] Yeah, I'm basically a professional couch potato."),
    # —— 技术术语：该保留原文 ——
    ("术语-API", "Just hit the API endpoint and the JSON payload comes back."),
    ("术语-前端", "The component re-renders every time the prop changes."),
    ("术语-金融", "[serious] The Fed just cut rates by twenty-five basis points."),
    # —— 否定 / 转折：最容易意思反的地方 ——
    ("否定-双重否定", "I can't say I disagree with that."),
    ("否定-hardly", "That's hardly the point here."),
    ("转折-but", "I love the idea, but there's no way we can afford it."),
    # —— 长难句 / 从句嵌套 ——
    ("长句-嵌套", "[serious] The report, which was finalized after months of debate that nobody expected to drag on this long, lands on the CEO's desk tomorrow."),
    ("长句-被动", "[serious] It has been decided that the event will be postponed until further notice."),
    # —— 情绪标签：保留 + 位置 ——
    ("标签-多标签", "[angry] I told you [sighing] a hundred times not to touch my stuff."),
    ("标签-句中", "And then [laughing] she just walked out of the room."),
    # —— 泛化验证（v2 few-shot 没覆盖的习语 / 书面词），防止"背例子" ——
    ("泛化-习语-cut corners", "[angry] They cut corners on the inspection and now the whole bridge is unsafe."),
    ("泛化-习语-piece of cake", "[excited] Don't worry, the exam was a piece of cake."),
    ("泛化-书面词-utilize", "You can utilize this button to restart the machine."),
    ("泛化-书面词-purchase", "[bored] I'm gonna purchase a new phone tomorrow, whatever."),
    ("泛化-习语-let the cat out", "[laughing] He let the cat out of the bag about the surprise party."),
    ("泛化-文化-potluck", "We're having a potluck on Saturday, bring a dish."),
    ("泛化-正式-学术", "[serious] The findings suggest a statistically significant correlation between sleep deprivation and cognitive decline."),
    ("泛化-正式-政治", "[serious] The two sides agreed to resume negotiations pending further consultation."),
    # —— 语域：正式 vs 口语，不能全翻成一个味 ——
    ("语域-新闻", "[serious] The company announced a recall affecting roughly two million vehicles."),
    ("语域-法庭", "[serious] Objection, Your Honor, the witness is speculating."),
    ("语域-游戏", "[excited] Dude, that boss fight was absolutely broken."),
]


MODELS = [
    ("ds-none", config.LLM_BASE_URL, config.LLM_API_KEY, "deepseek-flash", "none"),
]


async def run(label, base_url, key, model, effort):
    config.LLM_MODEL = model
    config.LLM_REASONING_EFFORT = effort
    llm_service._client = AsyncOpenAI(api_key=key, base_url=base_url)
    segs = [{"id": f"c{i:02d}", "text": text, "speaker_id": "0", "start_time": i * 6.0,
             "end_time": i * 6.0 + 5.0, "kind": "speech"} for i, (_, text) in enumerate(CASES)]
    t0 = time.time()
    out, failed = await llm_service.translate_script("ab-bench", segs, target_language="Chinese")
    return {o["id"]: o.get("translatedText", "") for o in out}, failed, time.time() - t0


results = {}
for label, url, key, model, effort in MODELS:
    tr, failed, dt = asyncio.run(run(label, url, key, model, effort))
    results[label] = tr
    print(f"# {label}: {len(tr)} 句, 失败 {len(failed)}, {dt:.1f}s", file=sys.stderr)

# 账本：按模型分别汇总成本
import glob
cost = {}
for label, _, _, model, _ in MODELS:
    p = "data/ab-bench/usage.json"
    if os.path.exists(p):
        d = json.load(open(p))
        cost[label] = sum(e.get("cost_yuan") or 0 for e in d["entries"])

print(f"\n{'考点':14}{'原文':60}")
for i, (tag, text) in enumerate(CASES):
    cid = f"c{i:02d}"
    print(f"\n## {tag}\n   原文: {text}")
    for label in results:
        print(f"   {label:8}: {results[label].get(cid, '<缺失>')}")

tagged = [(i, t) for i, (_, t) in enumerate(CASES) if llm_service._tags_in(t)]
print("\n标签保留率:")
for label in results:
    kept = sum(1 for i, t in tagged if llm_service._tags_in(t) <= llm_service._tags_in(results[label].get(f"c{i:02d}", "")))
    print(f"  {label:8}: {kept}/{len(tagged)}")
print("\n成本:", {k: f"¥{v:.4f} ({v*200:.1f} 积分)" for k, v in cost.items()})
json.dump(results, open("/tmp/ab-bench.json", "w"), ensure_ascii=False, indent=1)
