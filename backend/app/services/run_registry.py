"""
"同一工程同一时刻只允许一条运行"的注册表。

从 `routers/video.py` 搬出来的：这套东西与 HTTP 无关（不碰 Request/Response，
只有一个队列和一个取消事件），留在路由文件里让那个文件又多背了 100 多行。
路由只做三件事：取一条运行、判断能不能开始、把事件流出去。

设计要点（搬迁时一条没改）：

* 一条运行只有一个消费者。客户端断开时清掉队列，管线继续跑（磁盘上逐阶段
  落盘），但不再往没人消费的队列里塞东西。
* **断开后可以重连**：`GET /{video_id}/events` 会接管消费，并先回放有界历史
  （`HISTORY_LIMIT` 条），所以刷新页面不会失去进度视图。注意接管是"换队列"，
  同一时刻仍然只有一个活跃消费者。
* 重复请求是 **拒绝**（409），不是接管别人的运行：一条管线的设置（语言、口音、
  增强开关）属于发起它的那个人，后来者拿到的应该是"现在不能开始"。
* 结束即注销。工程跑完后的状态在它自己的文件里（status / cancelled_step）。
"""

import asyncio
from datetime import datetime, timedelta, timezone

# 时间戳用带时区的北京时间，与 usage 台账一致：前端显示"已跑 1m20s"不会因
# 服务器时区而偏移。
RUN_TZ = timezone(timedelta(hours=8))

# 历史回放的上限。够覆盖一个阶段的状态行，又不会随长视频无限增长。
HISTORY_LIMIT = 300


class PipelineRun:
    """
    One live pipeline, plus the state needed to watch or stop it.

    Fields are derived from the events the pipeline already emits, so there is
    no second progress bookkeeping to keep in sync.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue | None = None
        # 有界历史（P3 #9）：新订阅者先拿到已经发生的事件，否则"刷新页面重连"
        # 之后界面上是一片空白，用户会以为管线没在跑。只留最近 N 条：状态行是
        # 增量的，几十条足够还原现场，无限留会随长视频吃内存。
        self.history: list[dict] = []
        # ── 步骤状态 ──
        self.step: str | None = None          # separation / asr / mm_enhance / …
        self.step_status: str | None = None   # started / listening / done / failed
        self.done_count: int | None = None    # 批次进度（TTS 分段数）
        self.total_count: int | None = None
        self.cancelled = False
        self.error: str | None = None
        self.started_at = datetime.now(RUN_TZ)
        self.cancel_event = asyncio.Event()
        # 管线已结束（`close()` 之后）。`/events` 用它判断要不要等后续事件。
        self.closed = False

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
        self.history.append(event)
        if len(self.history) > HISTORY_LIMIT:
            del self.history[:-HISTORY_LIMIT]
        queue = self.queue
        if queue is not None:
            queue.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        """
        接管事件消费：队列先灌入历史，之后接着收新事件。

        仍然是"一条运行一个消费者"。被替换下来的旧队列会收到一个结束标记：它的
        读取端（一条已经断开、但服务端还没发现的连接）就此退出，而不是永远挂在
        `queue.get()` 上泄漏一个协程。
        """
        previous = self.queue
        queue: asyncio.Queue = asyncio.Queue()
        for event in self.history:
            queue.put_nowait(event)
        if self.closed:
            # 运行已经结束：历史回放完就收尾，别让新订阅者等一个不会来的事件。
            queue.put_nowait(None)
        self.queue = queue
        if previous is not None:
            previous.put_nowait(None)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        """断线清理；只在"这个队列仍是我在消费"时清空引用。"""
        if self.queue is queue:
            self.queue = None

    def close(self) -> None:
        """
        运行结束：给当前消费者发结束标记。

        生产端以前直接读 `run.queue` 再 `put_nowait(None)`：读与用之间消费者可能刚
        好断开并把它置空，于是抛 AttributeError。这里只读一次、判一次。
        """
        self.closed = True
        queue = self.queue
        if queue is not None:
            queue.put_nowait(None)

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
