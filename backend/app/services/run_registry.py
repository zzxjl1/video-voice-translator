"""
"同一工程同一时刻只允许一条运行"的注册表。

从 `routers/video.py` 搬出来的：这套东西与 HTTP 无关（不碰 Request/Response，
只有一个队列和一个取消事件），留在路由文件里让那个文件又多背了 100 多行。
路由只做三件事：取一条运行、判断能不能开始、把事件流出去。

设计要点（搬迁时一条没改）：

* 一条运行只有一个消费者。客户端断开时清掉队列，管线继续跑（磁盘上逐阶段
  落盘），但不再往没人消费的队列里塞东西。
* 重复请求是 **拒绝**（409），不是接管别人的运行：一条管线的设置（语言、口音、
  增强开关）属于发起它的那个人，后来者拿到的应该是"现在不能开始"。
* 结束即注销。工程跑完后的状态在它自己的文件里（status / cancelled_step）。
"""

import asyncio
from datetime import datetime, timedelta, timezone

# 时间戳用带时区的北京时间，与 usage 台账一致：前端显示"已跑 1m20s"不会因
# 服务器时区而偏移。
RUN_TZ = timezone(timedelta(hours=8))


class PipelineRun:
    """
    One live pipeline, plus the state needed to watch or stop it.

    Fields are derived from the events the pipeline already emits, so there is
    no second progress bookkeeping to keep in sync.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue | None = None
        # ── 步骤状态 ──
        self.step: str | None = None          # separation / asr / mm_enhance / …
        self.step_status: str | None = None   # started / listening / done / failed
        self.done_count: int | None = None    # 批次进度（TTS 分段数）
        self.total_count: int | None = None
        self.cancelled = False
        self.error: str | None = None
        self.started_at = datetime.now(RUN_TZ)
        self.cancel_event = asyncio.Event()

    async def emit(self, event: dict) -> None:
        phase = event.get("phase")
        if phase:
            self.step = phase
            self.step_status = event.get("status")
        if event.get("progress") is not None:
            self.done_count = event.get("progress")
            self.total_count = event.get("total")
        if event.get("cancelled"):
            self.cancelled = True
        if event.get("error") and not phase:
            self.error = str(event["error"])
        if self.queue is not None:
            self.queue.put_nowait(event)

    def request_cancel(self) -> None:
        """请求停止。真正的停止发生在下一个步骤边界（见 run_pipeline）。"""
        self.cancel_event.set()

    def snapshot(self) -> dict:
        return {
            "active": True,
            "step": self.step,
            "step_status": self.step_status,
            "progress": self.done_count,
            "total": self.total_count,
            "cancelling": self.cancel_event.is_set(),
            "cancelled": self.cancelled,
            "error": self.error,
            "started_at": self.started_at.isoformat(),
            "elapsed_s": round((datetime.now(RUN_TZ) - self.started_at).total_seconds(), 1),
        }


_running: dict[str, PipelineRun] = {}


def get(video_id: str) -> PipelineRun | None:
    return _running.get(video_id)


def start(video_id: str) -> PipelineRun:
    """登记一条新运行（调用方必须先确认没有在跑的）。"""
    run = PipelineRun()
    _running[video_id] = run
    return run


def finish(video_id: str) -> None:
    """注销。生产端必须在发出结束标记【之前】调用。"""
    _running.pop(video_id, None)


def snapshot(video_id: str) -> dict:
    """给 /status 用的运行状态；没有在跑就报告 active=False。"""
    run = _running.get(video_id)
    return run.snapshot() if run is not None else {"active": False}
