"""
命令注册表 —— UI 和 AI 共用的唯一动作入口。

    @command("edit_translation", "改某一句的译文", params=..., confirm="never")
    async def edit_translation(ctx, line: int, text: str): ...

一个命令 = 名字 + 说明 + 参数 schema + 执行函数 + 确认级别。HTTP 层
（`POST /api/projects/{id}/commands/{name}`）和 AI 的 tool calling 调用的是**同一个**
`execute()`；AI 的 tool 列表是 `tool_specs()` 从这张表直接生成的。所以：

  · UI 能做的 AI 一定能做（覆盖是结构性的，不是靠人记着给每个按钮补一个 tool）；
  · 新增一个命令，两边同时拥有它；
  · 参数校验、"第 N 句"到 id 的解析、过期检查、确认流程，只有一份。

执行函数拿到 `Ctx`：当前工程、谁在调用（user / ai）、一个 `change()` 方法（落到
`store.mutate`，自动带上 actor 和 cmd 名）、一个发进度的 `emit()`。

返回值：命令的执行函数返回一个 dict（给 AI 看的结果，也会原样回给 HTTP 调用方）。
抛 `CommandError(message)` 表示"这件事做不了，原因是……"——AI 会看到原因并自己改正；
UI 会把它显示出来。
"""
import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app import store
from app.bus import bus
from app.models import VideoState

logger = logging.getLogger(__name__)

# 确认级别：
#   never    直接执行（读取、改文本、单句操作 —— 都能撤销）
#   cost     会花钱/花时间（批量配音、克隆、重跑识别）—— AI 调用时需要用户在聊天里点确认
#   destroy  不可撤销或会推翻大量已有工作（改目标语言、重置、删除）—— 同上
CONFIRM_NEVER, CONFIRM_COST, CONFIRM_DESTROY = "never", "cost", "destroy"


class CommandError(Exception):
    """命令做不了。消息写给人和 AI 看：说清楚为什么、怎么改。"""

    def __init__(self, message: str, *, hint: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.hint = hint


@dataclass
class Command:
    name: str
    description: str
    params: dict                 # JSON Schema（object）
    fn: Callable
    confirm: str = CONFIRM_NEVER
    readonly: bool = False       # 只读命令不产生变更，也不需要确认
    ai: bool = True              # False = 只给 UI（比如需要浏览器能力的动作）
    group: str = "edit"


_REGISTRY: dict[str, Command] = {}


def command(
    name: str,
    description: str,
    params: Optional[dict] = None,
    *,
    confirm: str = CONFIRM_NEVER,
    readonly: bool = False,
    ai: bool = True,
    group: str = "edit",
):
    def deco(fn):
        if name in _REGISTRY:
            raise RuntimeError(f"duplicate command: {name}")
        _REGISTRY[name] = Command(
            name=name, description=description,
            params=params or {"type": "object", "properties": {}},
            fn=fn, confirm=confirm, readonly=readonly, ai=ai, group=group,
        )
        return fn
    return deco


def get(name: str) -> Optional[Command]:
    return _REGISTRY.get(name)


def all_commands() -> list[Command]:
    return list(_REGISTRY.values())


def tool_specs() -> list[dict]:
    """OpenAI 风格的 tool 列表，从注册表直接生成。"""
    out = []
    for c in _REGISTRY.values():
        if not c.ai:
            continue
        desc = c.description
        if c.confirm != CONFIRM_NEVER:
            desc += "（会先请用户确认）"
        out.append({
            "type": "function",
            "function": {"name": c.name, "description": desc, "parameters": c.params},
        })
    return out


# ----------------------------------------------------------------------------
# 执行上下文
# ----------------------------------------------------------------------------

@dataclass
class Ctx:
    video_id: str
    actor: str                       # user | ai | system
    cmd: str = ""
    # 由任务层注入：长任务命令用它登记/取消。
    jobs: Any = None
    # AI 对话里的当前 tool call id（让事件能对上）
    call_id: Optional[str] = None
    notes: list = field(default_factory=list)

    @property
    def state(self) -> VideoState:
        """最新的只读快照（每次访问都重读，命令执行期间别的变更也看得到）。"""
        return store.require_state(self.video_id)

    def change(self, fn: Callable[[VideoState], Any], summary: str, *, undoable: Optional[bool] = None,
               extra: Optional[dict] = None):
        """所有写入都走这里：自动带上 actor 和命令名。返回 fn 的返回值。"""
        m = store.mutate(
            self.video_id, fn, actor=self.actor, cmd=self.cmd,
            summary=summary, undoable=undoable, extra=extra,
        )
        return m.value

    async def emit(self, event: dict) -> None:
        """长任务的细粒度进度（只进内存环形缓冲，不落盘）。"""
        bus.publish(self.video_id, {"type": "progress", **event})

    def ui(self, action: str, **payload) -> None:
        """界面意图：滚动到某行、打开某个面板……不改状态，客户端自己决定怎么做。"""
        bus.publish(self.video_id, {"type": "ui", "action": action, "actor": self.actor, **payload})


# ----------------------------------------------------------------------------
# 参数辅助：把"第 N 句"解析成行
# ----------------------------------------------------------------------------

def resolve_line(state: VideoState, ref) -> "tuple[int, Any]":
    """
    `ref` 可以是 1 起的行号（人和 AI 说的"第 N 句"）或分段 id。返回 (下标, 行)。
    找不到就抛 CommandError，并告诉调用方有效范围。
    """
    segs = state.segments
    if isinstance(ref, bool):
        raise CommandError("行号参数无效")
    if isinstance(ref, (int, float)) or (isinstance(ref, str) and ref.strip().isdigit()):
        n = int(ref)
        if not 1 <= n <= len(segs):
            raise CommandError(f"没有第 {n} 句：这个工程共 {len(segs)} 句（1~{len(segs)}）")
        return n - 1, segs[n - 1]
    for i, seg in enumerate(segs):
        if seg.id == ref:
            return i, seg
    raise CommandError(f"找不到这一句：{ref!r}")


def resolve_lines(state: VideoState, refs) -> list:
    """多个行引用 → 行列表（去重、保持顺序）。"""
    out, seen = [], set()
    for ref in refs or []:
        _, seg = resolve_line(state, ref)
        if seg.id not in seen:
            seen.add(seg.id)
            out.append(seg)
    return out


def resolve_speaker(state: VideoState, ref):
    ref = str(ref)
    for sp in state.speakers:
        if sp.id == ref or sp.name == ref:
            return sp
    names = ", ".join(f"{sp.id}（{sp.name}）" for sp in state.speakers) or "（还没有说话人）"
    raise CommandError(f"找不到说话人 {ref!r}。现有：{names}")


# ----------------------------------------------------------------------------
# 执行
# ----------------------------------------------------------------------------

async def execute(video_id: str, name: str, args: dict, *, actor: str = "user",
                  jobs: Any = None, call_id: Optional[str] = None) -> dict:
    """
    唯一的执行入口。HTTP 路由和 AI 工具调用都走这里。

    返回 `{"ok": True, "result": ...}` 或 `{"ok": False, "error": "...", "hint": ...}`；
    **不抛**（除了编程错误会被转成 error 返回并记日志），这样 AI 循环和 HTTP 层处理
    失败的方式一致：把原因交还给调用方。
    """
    cmd = get(name)
    if cmd is None:
        return {"ok": False, "error": f"没有这个命令：{name}"}
    if not store.exists(video_id):
        return {"ok": False, "error": "工程不存在"}

    ctx = Ctx(video_id=video_id, actor=actor, cmd=name, jobs=jobs, call_id=call_id)
    try:
        err = _validate(cmd.params, args or {})
        if err:
            return {"ok": False, "error": f"参数不对：{err}"}
        result = cmd.fn(ctx, **(args or {}))
        if inspect.isawaitable(result):
            result = await result
        return {"ok": True, "result": result if result is not None else {}}
    except CommandError as e:
        return {"ok": False, "error": e.message, **({"hint": e.hint} if e.hint else {})}
    except store.ProjectNotFound:
        return {"ok": False, "error": "工程不存在"}
    except asyncio.CancelledError:
        raise
    except TypeError as e:
        # 参数名对不上函数签名：schema 与实现不一致，是编程错误，但对调用方说人话。
        logger.error(f"[{video_id}] command {name} signature mismatch: {e}", exc_info=True)
        return {"ok": False, "error": f"命令 {name} 的参数无法识别：{e}"}
    except Exception as e:
        logger.error(f"[{video_id}] command {name} failed: {e}", exc_info=True)
        return {"ok": False, "error": f"{name} 执行失败：{e}"}


def _validate(schema: dict, args: dict) -> Optional[str]:
    """够用的 JSON Schema 校验：必填、类型、枚举、数值范围。返回问题描述或 None。"""
    props = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in args or args[key] is None:
            return f"缺少必填参数 {key}"
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            return f"未知参数 {key}（有效参数：{', '.join(props) or '无'}）"
        if value is None:
            continue
        problem = _check_type(key, value, spec)
        if problem:
            return problem
    return None


def _check_type(key: str, value, spec: dict) -> Optional[str]:
    t = spec.get("type")
    if "enum" in spec and value not in spec["enum"]:
        return f"{key} 必须是 {spec['enum']} 之一，收到 {value!r}"
    if t == "integer":
        if isinstance(value, bool) or not (isinstance(value, int) or (isinstance(value, float) and value.is_integer())):
            return f"{key} 必须是整数，收到 {value!r}"
    elif t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{key} 必须是数字，收到 {value!r}"
    elif t == "string":
        if not isinstance(value, str):
            return f"{key} 必须是字符串，收到 {value!r}"
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            return f"{key} 太长（最多 {spec['maxLength']} 个字符，收到 {len(value)}）"
    elif t == "boolean":
        if not isinstance(value, bool):
            return f"{key} 必须是 true/false，收到 {value!r}"
    elif t == "array":
        if not isinstance(value, list):
            return f"{key} 必须是数组，收到 {value!r}"
        item = spec.get("items")
        if item:
            for i, v in enumerate(value):
                p = _check_type(f"{key}[{i}]", v, item)
                if p:
                    return p
    if t in ("integer", "number") and not isinstance(value, bool) and isinstance(value, (int, float)):
        if "minimum" in spec and value < spec["minimum"]:
            return f"{key} 不能小于 {spec['minimum']}，收到 {value}"
        if "maximum" in spec and value > spec["maximum"]:
            return f"{key} 不能大于 {spec['maximum']}，收到 {value}"
    return None


def needs_confirmation(name: str, actor: str) -> Optional[str]:
    """AI 调用需要确认时返回确认级别；用户自己点的按钮本身就是确认。"""
    cmd = get(name)
    if cmd is None or actor != "ai" or cmd.readonly:
        return None
    return cmd.confirm if cmd.confirm != CONFIRM_NEVER else None


# 供命令实现复用：行的一句话描述（给 AI 看的结果、事件摘要都用它）
def brief(state: VideoState, seg, index: Optional[int] = None) -> str:
    if index is None:
        index = next((i for i, s in enumerate(state.segments) if s.id == seg.id), -1)
    return f"第 {index + 1} 句"
