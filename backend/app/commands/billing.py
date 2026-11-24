"""
计费相关的命令。UI 和 AI 共用。

口径（很重要，AI 会照着说）：用户侧的单位是**积分**，积分 = 成本 × 倍率 × 100，
所以 1 积分相当于 0.01 元；成本本身不对外。明细给到小数点后 2~3 位，因为单句
常常不到 1 积分。
"""
from __future__ import annotations

from app.billing import pricing, service as billing
from app.billing.pricing import MICRO
from app.commands.registry import Ctx, command


@command(
    "get_billing",
    "查账户的积分：余额、本月已用、按步骤的明细。不给 project 时是账户视角；"
    "给了就是那个工程累计用了多少。金额单位都是积分（1 积分 ≈ 0.01 元）。",
    {"type": "object", "properties": {
        "project_id": {"type": "string", "description": "工程 id；不给就是当前工程"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "附带最近多少条明细，默认 0（不列）"},
    }},
    readonly=True, group="read",
)
def get_billing(ctx: Ctx, project_id: str = "", limit: int = 0):
    user_id = billing.user_for_project(project_id or ctx.video_id)
    if not user_id:
        return {"note": "这个工程没有归属账号，没有计费记录"}
    out = billing.summary(user_id)
    out["project"] = billing.project_summary(project_id or ctx.video_id)
    if limit:
        out["recent"] = billing.recent_entries(user_id, limit=limit, project_id=project_id or ctx.video_id)
    return out


@command(
    "get_price",
    "查某项服务现在的单价（单位积分，已含服务费）。目前只有按次计费的（克隆音色）"
    "有固定单价；TTS / 翻译 / 识别按用量计费，只能给预估。",
    {"type": "object", "properties": {
        "step": {"type": "string", "enum": ["clone"], "description": "要查的服务"},
    }, "required": ["step"]},
    readonly=True, group="read",
)
def get_price(ctx: Ctx, step: str):  # noqa: ARG001 - 只用到 step
    if step == "clone":
        got = billing.price_of("clone", pricing.ENROLLMENT_MODEL)
        if got:
            return {**got, "note": "每次新建一个音色按这个数扣积分；删除不返还"}
    return {"note": f"{step} 按用量计费，没有固定单价"}
