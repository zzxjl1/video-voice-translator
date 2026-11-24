"""计费接口（用户看自己的钱）。成本列一律不返回。"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app import config
from app.auth import users as auth_users
from app.auth.dependencies import require_user
from app.billing import pricing, service as billing
from app.billing.pricing import MICRO

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


def _owned(project_id: str, user: dict) -> None:
    """别人的工程一律 404（和工程接口一致，避免 id 可枚举）。"""
    if auth_users.owner_of(project_id) != user["id"]:
        raise HTTPException(status_code=404, detail="工程不存在")


def _is_admin(user: dict) -> bool:
    email = (user.get("email") or "").lower()
    return bool(email and email in config.BILLING_ADMIN_EMAILS)


@router.get("/summary")
def summary(user: dict = Depends(require_user)):
    """账户概览：余额、本月已用、按步骤拆开的花费。"""
    return billing.summary(user["id"])


@router.get("/entries")
def entries(
    limit: int = Query(50, ge=1, le=200),
    project: str | None = Query(None),
    user: dict = Depends(require_user),
):
    """明细（不含成本）。给了 project 就只列那个工程的。"""
    if project:
        _owned(project, user)
    return {"entries": billing.recent_entries(user["id"], limit=limit, project_id=project),
            "currency": "CNY"}


@router.get("/prices")
def prices(user: dict = Depends(require_user)):  # noqa: ARG001 - 只是要登录
    """
    用户可见的单价（已含倍率）。前端把"约 x 元"写死在代码里就会和后台调价脱节，
    所以单价走接口。
    """
    clone = billing.price_of("clone", pricing.ENROLLMENT_MODEL)
    return {
        "currency": "CNY",
        # 按次计费的项目；按用量计费的（TTS/翻译/识别）只能给估算区间，前端不展示单价
        "per_unit": [clone] if clone else [],
        "markups": {k: v for k, v in config.BILLING_MARKUP.items()},
    }


class AdjustBody(BaseModel):
    user_id: str
    amount_yuan: float
    note: str = ""


@router.post("/adjust")
def adjust(body: AdjustBody, user: dict = Depends(require_user)):
    """
    人工充值 / 扣减（管理员）。真实收款是线下的，这里只落账；
    金额用微元换算，避免 0.1+0.2 这类浮点误差进账本。
    """
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="只有管理员能调整余额")
    if not body.amount_yuan:
        raise HTTPException(status_code=400, detail="金额不能为 0")
    delta = round(body.amount_yuan * MICRO)
    try:
        result = billing.adjust(body.user_id, delta, note=body.note, actor=user.get("email") or user["id"])
    except billing.BillingError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    logger.info("[billing] admin %s adjusted %s by %.2f yuan", user.get("email"), body.user_id, body.amount_yuan)
    return result


@router.get("/projects/{project_id}")
def project(project_id: str, user: dict = Depends(require_user)):
    """一个工程累计花了多少（用户价）。工程页展示用。"""
    _owned(project_id, user)
    data = billing.project_summary(project_id)
    data["balance_yuan"] = round(billing.balance_micro(user["id"]) / MICRO, 2)
    return data
