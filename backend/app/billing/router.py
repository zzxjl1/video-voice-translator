"""计费接口（用户看自己的积分）。成本列一律不返回。"""
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
    """账户概览：积分余额、本月已用、按步骤拆开。"""
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
            "unit": "points"}


@router.get("/prices")
def prices(user: dict = Depends(require_user)):  # noqa: ARG001 - 只是要登录
    """
    用户可见的单价（已含倍率，单位积分）。前端把"约 x 积分"写死在代码里就会和
    后台调价脱节，所以单价走接口。
    """
    clone = billing.price_of("clone", pricing.ENROLLMENT_MODEL)
    return {
        "unit": "points",
        # 按次计费的项目；按用量计费的（TTS/翻译/识别）只能给估算，前端不展示单价
        "per_unit": [clone] if clone else [],
        "markups": {k: v for k, v in config.BILLING_MARKUP.items()},
    }


class EstimateBody(BaseModel):
    duration_seconds: float
    dubbing_mode: str = "remote"
    mm_enhance: bool = True
    enable_voice_clone: bool = True


@router.post("/estimate")
def estimate(body: EstimateBody, user: dict = Depends(require_user)):  # noqa: ARG001
    """按时长 + 设置估一次初始化要花多少积分（上界）。前端落地页选设置时实时算。"""
    from app.billing import estimate as _est
    e = _est.estimate_init_points(
        body.duration_seconds, dubbing_mode=body.dubbing_mode, mm_enhance=body.mm_enhance,
        voice_clone=body.enable_voice_clone and body.dubbing_mode == "remote",
    )
    return {**e, "balance_points": pricing.as_points(billing.balance_points_micro(user["id"]))}


class AdjustBody(BaseModel):
    user_id: str
    amount_points: float
    note: str = ""


@router.post("/adjust")
def adjust(body: AdjustBody, user: dict = Depends(require_user)):
    """
    人工充值 / 扣减（管理员）。真实收款是线下的，这里只落账。
    入参是**积分**，内部换 micro 积分存，避免 0.1+0.2 这类浮点误差进账本。
    """
    if not _is_admin(user):
        raise HTTPException(status_code=403, detail="只有管理员能调整积分")
    if not body.amount_points:
        raise HTTPException(status_code=400, detail="数量不能为 0")
    delta = round(body.amount_points * MICRO)
    try:
        result = billing.adjust(body.user_id, delta, note=body.note, actor=user.get("email") or user["id"])
    except billing.BillingError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    logger.info("[billing] admin %s adjusted %s by %s points",
                user.get("email"), body.user_id, body.amount_points)
    return result


@router.get("/projects/{project_id}")
def project(project_id: str, user: dict = Depends(require_user)):
    """一个工程累计用了多少积分。工程页展示用。"""
    _owned(project_id, user)
    data = billing.project_summary(project_id)
    data["balance_points"] = pricing.as_points(billing.balance_points_micro(user["id"]))
    return data
