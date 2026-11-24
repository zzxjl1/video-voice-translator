"""
计费相关的命令。UI 和 AI 共用（注册表那一套）。

口径（很重要，AI 会照着说）：用户看到的金额是**含倍率的价格**，成本不对外；
所有金额都是人民币，明细保留 4 位小数（单句常常不到一分钱），汇总两位。
"""
from __future__ import annotations

from app.billing import pricing, service as billing
from app.billing.pricing import MICRO
from app.commands.registry import Ctx, command


@command(
    "get_billing",
    "查账户的钱：余额、本月已花、按步骤的明细。不给 project 时是账户视角；"
    "给了就是那个工程累计花了多少。金额都是人民币（含服务费）。",
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
    "查某项服务现在的单价（含服务费）。目前只有按次计费的（克隆音色）有固定单价；"
    "TTS / 翻译 / 识别是按用量计费，只能给预估。",
    {"type": "object", "properties": {
        "step": {"type": "string", "enum": ["clone"], "description": "要查的服务"},
    }, "required": ["step"]},
    readonly=True, group="read",
)
def get_price(ctx: Ctx, step: str):  # noqa: ARG001 - 只用到 step
    from app import config
    if step == "clone":
        got = billing.price_of("clone", pricing.ENROLLMENT_MODEL)
        if got:
            return {**got, "note": "每次新建一个音色按这个价扣；删除不返还"}
    return {"note": f"{step} 按用量计费，没有固定单价"}
