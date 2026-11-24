"""
账本服务：记账、扣余额、查余额、汇总、人工调整。

对外只有这几个动作；所有金额都是 micro 元整数。
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from app import config
from app.billing import db, pricing
from app.billing.pricing import MICRO

logger = logging.getLogger(__name__)


class BillingError(RuntimeError):
    """余额不够 / 账本出问题。消息是给用户看的话，要能指导他下一步做什么。"""


# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------

def _ensure_account(conn, user_id: str, at: int) -> int:
    conn.execute(
        "INSERT OR IGNORE INTO account(user_id, balance_micro, currency, updated_at) VALUES (?, 0, 'CNY', ?)",
        (user_id, at),
    )
    row = conn.execute("SELECT balance_micro FROM account WHERE user_id = ?", (user_id,)).fetchone()
    return int(row["balance_micro"])


def balance_micro(user_id: str) -> int:
    with db.conn() as conn:
        row = conn.execute("SELECT balance_micro FROM account WHERE user_id = ?", (user_id,)).fetchone()
    return int(row["balance_micro"]) if row else 0


def adjust(user_id: str, delta_micro: int, *, note: str, actor: str) -> dict:
    """人工充值 / 扣减（管理员用）。返回调整后的余额。"""
    if delta_micro == 0:
        raise BillingError("调整金额不能为 0")
    at = db.now()
    conn = db.conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _ensure_account(conn, user_id, at)
        conn.execute(
            "INSERT INTO adjustment(id, user_id, delta_micro, note, actor, created_at) VALUES (?,?,?,?,?,?)",
            (uuid.uuid4().hex, user_id, delta_micro, note, actor, at),
        )
        conn.execute(
            "UPDATE account SET balance_micro = balance_micro + ?, updated_at = ? WHERE user_id = ?",
            (delta_micro, at, user_id),
        )
        row = conn.execute("SELECT balance_micro FROM account WHERE user_id = ?", (user_id,)).fetchone()
        conn.execute("COMMIT")
        return {"user_id": user_id, "balance_micro": int(row["balance_micro"]),
                "balance_yuan": round(int(row["balance_micro"]) / MICRO, 2), "delta_micro": delta_micro}
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# 工程 → 账号
# ---------------------------------------------------------------------------

_owner_cache: dict[str, str | None] = {}


def user_for_project(project_id: Optional[str]) -> Optional[str]:
    """工程归谁（记账要用）。后台任务只拿得到 video_id，所以在这里反查并有缓存。"""
    if not project_id:
        return None
    if project_id in _owner_cache:
        return _owner_cache[project_id]
    try:
        from app.auth import users as auth_users
        owner = auth_users.owner_of(project_id)
    except Exception:  # noqa: BLE001 - 没初始化 auth 库时不该让记账炸掉
        owner = None
    _owner_cache[project_id] = owner
    return owner


def forget_project(project_id: str) -> None:
    _owner_cache.pop(project_id, None)


# ---------------------------------------------------------------------------
# 记账（唯一入口）
# ---------------------------------------------------------------------------

def charge(
    *,
    user_id: Optional[str],
    project_id: Optional[str],
    step: str,
    model: str,
    usage: Optional[dict] = None,
    detail: str = "",
    api_cost: Optional[dict] = None,
    idempotency_key: Optional[str] = None,
    markups: Optional[dict] = None,
) -> Optional[dict]:
    """
    把一次调用记进账本，并从余额里扣掉用户价。返回分录摘要；重复投递返回 None。

    成本算不出来时**照样记账**（price 0 + cost_source='no_price'），因为"没算出来"
    不等于"免费"：这笔要能被运营看见，而余额一分不扣、也不阻断用户。
    """
    if not user_id:
        # 没有归属账号（历史工程 / 未登录的后台任务）：不记用户账，只留日志。
        logger.info("[billing] skip charge without user: %s/%s", step, model)
        return None

    usage = usage or {}
    markups = markups if markups is not None else config.BILLING_MARKUP
    cost_source = "computed"
    try:
        cost, price, ratio = pricing.price_micro(step, model, usage, markups=markups, api_cost=api_cost)
        if step in pricing.FREE_STEPS:
            cost_source = "free"
        elif api_cost:
            cost_source = "api"
    except pricing.NoCost as e:
        cost, price = 0, 0
        ratio = pricing.markup_for(step, markups)
        cost_source = "no_price"
        logger.warning("[billing] no price for %s/%s (%s); recorded as unbilled", step, model, e)

    key = idempotency_key or uuid.uuid4().hex
    at = db.now()
    conn = db.conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT OR IGNORE INTO ledger(id, user_id, project_id, step, model, detail,"
            " input_tokens, output_tokens, cache_hit_tokens, calls, cost_micro, price_micro,"
            " markup, currency, cost_source, status, idempotency_key, meta, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'CNY',?,'settled',?,?,?)",
            (
                uuid.uuid4().hex, user_id, project_id, step, model, detail,
                int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
                int(usage.get("output_tokens") or usage.get("completion_tokens") or 0),
                int(usage.get("prompt_cache_hit_tokens") or usage.get("cache_hit_tokens") or 0),
                1, cost, price, ratio, cost_source, key, "", at,
            ),
        )
        if cur.rowcount == 0:
            conn.execute("COMMIT")
            return None                     # 同一个 key 已经记过：幂等
        _ensure_account(conn, user_id, at)
        if price:
            conn.execute(
                "UPDATE account SET balance_micro = balance_micro - ?, updated_at = ? WHERE user_id = ?",
                (price, at, user_id),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    logger.info("[billing] %s/%s user=%s price=%.4f yuan (%s)",
                step, model, user_id, price / MICRO, cost_source)
    return {"step": step, "model": model, "price_micro": price,
            "price_yuan": round(price / MICRO, 4), "cost_source": cost_source}


# ---------------------------------------------------------------------------
# 查账
# ---------------------------------------------------------------------------

def _month_start() -> int:
    """本月 1 日 00:00（进程时区，部署在服务器本地时区）。"""
    import datetime as _dt
    t = _dt.datetime.now()
    return int(t.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())


def summary(user_id: str) -> dict:
    """账户视角：余额、本月已用、按步骤的明细。"""
    at = db.now()
    since = _month_start()
    conn = db.conn()
    _ensure_account(conn, user_id, at)
    acc = conn.execute("SELECT balance_micro FROM account WHERE user_id = ?", (user_id,)).fetchone()
    rows = conn.execute(
        "SELECT step, COUNT(*) AS calls, SUM(price_micro) AS spent"
        " FROM ledger WHERE user_id = ? AND status = 'settled' AND created_at >= ?"
        " GROUP BY step ORDER BY spent DESC",
        (user_id, since),
    ).fetchall()
    month_total = sum(int(r["spent"] or 0) for r in rows)
    return {
        "balance_micro": int(acc["balance_micro"]),
        "balance_yuan": round(int(acc["balance_micro"]) / MICRO, 2),
        "month_spent_micro": month_total,
        "month_spent_yuan": round(month_total / MICRO, 2),
        "month_calls": sum(int(r["calls"] or 0) for r in rows),
        "by_step": [
            {"step": r["step"], "calls": int(r["calls"]),
             "spent_micro": int(r["spent"] or 0), "spent_yuan": round(int(r["spent"] or 0) / MICRO, 4)}
            for r in rows
        ],
        "currency": "CNY",
    }


def project_summary(project_id: str) -> dict:
    """一个工程累计花了多少（用户价），按步骤拆开。"""
    conn = db.conn()
    rows = conn.execute(
        "SELECT step, COUNT(*) AS calls, SUM(price_micro) AS spent"
        " FROM ledger WHERE project_id = ? AND status = 'settled'"
        " GROUP BY step ORDER BY spent DESC",
        (project_id,),
    ).fetchall()
    total = sum(int(r["spent"] or 0) for r in rows)
    # 未定价的调用要显式报出来，否则"账单比预期少"会被当成系统漏记
    unbilled = conn.execute(
        "SELECT COUNT(*) AS n FROM ledger WHERE project_id = ? AND cost_source = 'no_price'",
        (project_id,),
    ).fetchone()
    return {
        "project_id": project_id,
        "total_micro": total,
        "total_yuan": round(total / MICRO, 2),
        "calls": sum(int(r["calls"] or 0) for r in rows),
        "unpriced_calls": int(unbilled["n"] or 0),
        "by_step": [
            {"step": r["step"], "calls": int(r["calls"]),
             "spent_micro": int(r["spent"] or 0), "spent_yuan": round(int(r["spent"] or 0) / MICRO, 4)}
            for r in rows
        ],
        "currency": "CNY",
    }


def recent_entries(user_id: str, limit: int = 50, project_id: Optional[str] = None) -> list[dict]:
    """明细（不含成本列——接口层也不该把它带出去）。"""
    conn = db.conn()
    sql = ("SELECT id, project_id, step, model, detail, input_tokens, output_tokens,"
           " price_micro, cost_source, created_at FROM ledger WHERE user_id = ? AND status = 'settled'")
    args: list = [user_id]
    if project_id:
        sql += " AND project_id = ?"
        args.append(project_id)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(limit, 200)))
    return [
        {"id": r["id"], "project_id": r["project_id"], "step": r["step"], "model": r["model"],
         "detail": r["detail"], "input_tokens": int(r["input_tokens"]),
         "output_tokens": int(r["output_tokens"]), "price_micro": int(r["price_micro"]),
         "price_yuan": round(int(r["price_micro"]) / MICRO, 4),
         "cost_source": r["cost_source"], "at": int(r["created_at"])}
        for r in conn.execute(sql, args).fetchall()
    ]


# ---------------------------------------------------------------------------
# 拦截
# ---------------------------------------------------------------------------

def require_balance(user_id: Optional[str], need_micro: int, what: str) -> None:
    """
    开始一件要花钱的事之前调用。余额不够就抛 BillingError。

    `need_micro` 是保守估计；调用方拿不到可靠估计时用 config.BILLING_MIN_START_MICRO
    （一次任务的起步门槛），因为"精确预估"在 ASR/TTS 这种按用量计费的服务上做不到。
    """
    if not user_id or need_micro <= 0:
        return
    have = balance_micro(user_id)
    if have >= need_micro:
        return
    raise BillingError(
        f"余额不足：{what}约需 ¥{need_micro / MICRO:.2f}，当前余额 ¥{have / MICRO:.2f}。"
        "充值后再试。"
    )


def price_of(step: str, model: str, markups: Optional[dict] = None) -> Optional[dict]:
    """按次计费步骤（clone）的单价，前端要显示"约 x 元"时用。"""
    rule = pricing.COST_TABLE.get((step, model))
    if not rule or "per_unit" not in rule:
        return None
    marks = markups if markups is not None else config.BILLING_MARKUP
    ratio = pricing.markup_for(step, marks)
    per_unit = round(rule["per_unit"] * MICRO * ratio)
    return {"step": step, "model": model, "price_micro": per_unit,
            "price_yuan": round(per_unit / MICRO, 2)}
