"""
用户计价：成本（元）→ 积分。这一层是唯一知道"倍率"的地方。

口径（产品定的，别自己改）：

    积分 = 成本（元） × 倍率 × 100

也就是**积分的单位是"分"**：1 积分 = 0.01 元（在用户侧），倍率已经折进积分数量里。
成本 1 元、倍率 2 → 用户看到 200 积分（= 付 2 元）。

两条分工（刻意分开，别合并）：

  · **成本**（我们付给供应商的钱）由 `services/usage_service.py` 记在工程的
    `usage.json`，只给运营/排障看，**任何面向用户的接口都不返回它**。
  · **积分**由这里算，进 `billing.sqlite3` 的账本，从用户余额里扣。

倍率只影响之后的价格：每笔账都存了当时的 `markup`、成本快照和积分，历史不会被改写。

币种只有 CNY（北京站）。成本用 **micro 元整数**（1e-6 元）；积分用
**micro 积分整数**（1e-6 积分）——单句常常不到 1 积分（¥0.0006 → 0.12 积分），
用整数存才不会在累加时丢掉零头。
"""
from __future__ import annotations

from typing import Optional

MICRO = 1_000_000            # 1 元 = 1_000_000 micro 元；1 积分 = 1_000_000 micro 积分
POINTS_PER_YUAN = 100        # 积分 = 元 × 100（再乘倍率，见 points_micro）

# 声音复刻走的是固定的 enrollment 模型（和合成模型无关），单价按次。
ENROLLMENT_MODEL = "qwen-voice-enrollment"

# 成本价：元 / 百万 token（或每次）。取证来源与日期见 usage_service 的同名表；
# 这里只是它的一份只读镜像——两处不一致会让"内部成本"和"对外定价"对不上，
# 所以由 test_billing.py 断言两边完全相等。
#
# DeepSeek 的峰谷价差**不考虑**（产品决定）：统一按峰时价一条线。
COST_TABLE: dict[tuple[str, str], dict] = {
    ("tts", "qwen-audio-3.1-tts-flash"): {"input_per_mtoken": 1.5, "output_per_mtoken": 12.0},
    ("asr", "qwen-audio-3.1-asr-flash-filetrans"): {"input_per_mtoken": 0.8, "output_per_mtoken": 2.7},
    ("omni", "qwen3.8-omni-flash"): {"input_per_mtoken": 0.8, "output_per_mtoken": 2.7},
    ("clone", "qwen-voice-enrollment"): {"per_unit": 0.01},
    ("llm", "deepseek-flash"): {
        "cache_hit_per_mtoken": 0.04,
        "cache_miss_per_mtoken": 2.0,
        "output_per_mtoken": 8.0,
    },
}

# 倍率：积分 = 成本 × 倍率 × 100。默认值放这里，由 config.BILLING_MARKUP 覆盖
# （可只覆盖某几步）。
DEFAULT_MARKUP: dict[str, float] = {
    "tts": 2.0,
    "asr": 2.0,
    "omni": 2.0,
    "llm": 2.0,
    "clone": 2.0,
}

# 不走供应商、本来就不花钱的步骤：记 0 积分，并标 free（否则用户会以为漏记）。
FREE_STEPS = frozenset({"browser_tts", "separation", "export"})


class NoCost(Exception):
    """这一步的价格未知（缺价格表条目，或没拿到用量）。绝不猜一个数出来。"""


def cost_micro(step: str, model: str, usage: dict, *, api_cost: Optional[dict] = None) -> int:
    """
    按成本价算一次调用的成本（micro 元）。算不出来抛 NoCost。

    `api_cost` 是供应商自己报的金额（{"amount": 0.0012, "currency": "CNY"}），
    有就以它为准——我们算的是清单价，供应商报的是实收。
    """
    if step in FREE_STEPS:
        return 0
    if api_cost and api_cost.get("amount") is not None:
        if (api_cost.get("currency") or "CNY").upper() != "CNY":
            raise NoCost(f"供应商报的币种不是 CNY：{api_cost.get('currency')}")
        return round(float(api_cost["amount"]) * MICRO)

    rule = COST_TABLE.get((step, model))
    if not rule:
        raise NoCost(f"没有 {step}/{model} 的价格")

    if "per_unit" in rule:
        return round(rule["per_unit"] * MICRO)

    input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
    if not input_tokens and not output_tokens:
        # 按 token 计价却没拿到 token 用量 = "数量未知"，不是 0 元。
        raise NoCost(f"{step}/{model} 没拿到 token 用量")
    cache_hit = int(usage.get("prompt_cache_hit_tokens") or usage.get("cache_hit_tokens") or 0)

    if "cache_miss_per_mtoken" in rule:
        miss = max(0, input_tokens - cache_hit)
        yuan = (
            cache_hit * rule["cache_hit_per_mtoken"]
            + miss * rule["cache_miss_per_mtoken"]
            + output_tokens * rule["output_per_mtoken"]
        ) / 1_000_000
    else:
        yuan = (input_tokens * rule.get("input_per_mtoken", 0.0)
                + output_tokens * rule.get("output_per_mtoken", 0.0)) / 1_000_000
    return round(yuan * MICRO)


def markup_for(step: str, markups: dict[str, float]) -> float:
    return float(markups.get(step) or markups.get("*") or DEFAULT_MARKUP.get(step) or 1.0)


def points_micro(cost: int, markup: float) -> int:
    """
    micro 元成本 → micro 积分。积分 = 元 × 倍率 × 100，所以

        micro 积分 = micro 元 × 倍率 × 100

    整数运算（倍率先乘成整数化的比例）避免浮点在账本里累积误差。
    """
    return round(cost * markup * POINTS_PER_YUAN)


def price_micro(step: str, model: str, usage: dict, *, markups: dict[str, float],
                api_cost: Optional[dict] = None) -> tuple[int, int, float]:
    """返回 (成本 micro 元, 用户积分 micro, 倍率)。成本未知时抛 NoCost。"""
    cost = cost_micro(step, model, usage, api_cost=api_cost)
    ratio = markup_for(step, markups)
    return cost, points_micro(cost, ratio), ratio


def as_points(pm: int) -> float:
    """micro 积分 → 积分（显示用）。"""
    return round(pm / MICRO, 4)


def fmt_points(pm: int) -> str:
    """给字符串场景用的紧凑格式：大额两位、小额三位，去掉多余的 0。"""
    pts = pm / MICRO
    if pts == 0:
        return "0"
    text = f"{pts:.3f}" if abs(pts) < 1 else f"{pts:.2f}"
    return text.rstrip("0").rstrip(".")
