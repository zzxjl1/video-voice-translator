"""
项目事件总线：一个工程一条事件流，UI 和 AI 看同一条。

事件类型（`type` 字段）：
  change    状态变了（落盘到 events.jsonl）。带 `patch`（客户端视图的增量）、`rev`、
            `actor`（user / ai / system）、`summary`（给 AI 和聊天里看的一句人话）。
  job       任务开始 / 结束 / 失败 / 取消（落盘）。
  progress  任务里的细粒度进度（只在内存里，初始化弹窗的日志就是它）。
  chat      AI 对话的实时事件：delta / tool_call / tool_result / message / error。
  ui        界面意图：AI 让界面滚动到某行、打开某个面板。不改状态，客户端自己决定怎么做。

`seq` 是整个工程里单调递增的序号（所有类型共用），断线重连时 `?since=<seq>` 回放
内存里最近的 `RING` 条。回放不到（进程重启过）也没关系：客户端以 `rev` 为准，
发现落后就重新取一次完整状态。

线程模型：命令和任务跑在线程池里，SSE 在事件循环里。`publish` 两边都能调：
在事件循环线程里直接投递，否则 `call_soon_threadsafe`。
"""
import asyncio
import threading
import time
from collections import deque
from typing import Optional

RING = 600


class _Channel:
    __slots__ = ("seq", "ring", "subs")

    def __init__(self, seq: int = 0):
        self.seq = seq
        self.ring: deque = deque(maxlen=RING)
        self.subs: set = set()


class EventBus:
    def __init__(self):
        self._channels: dict[str, _Channel] = {}
        self._lock = threading.Lock()
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """应用启动时记下事件循环 —— 线程池里的 publish 要靠它回到循环线程。"""
        self.loop = loop

    def _channel(self, video_id: str) -> _Channel:
        ch = self._channels.get(video_id)
        if ch is None:
            ch = self._channels[video_id] = _Channel()
        return ch

    def seed(self, video_id: str, seq: int) -> None:
        """进程重启后接着落盘事件里最大的 seq 往下数，序号不回退。"""
        with self._lock:
            ch = self._channel(video_id)
            ch.seq = max(ch.seq, seq)

    def current_seq(self, video_id: str) -> int:
        with self._lock:
            return self._channel(video_id).seq

    def publish(self, video_id: str, event: dict) -> dict:
        with self._lock:
            ch = self._channel(video_id)
            ch.seq += 1
            event["seq"] = ch.seq
            event.setdefault("ts", time.time())
            ch.ring.append(event)
            subs = list(ch.subs)
        if subs:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            # 在事件循环线程里、或者根本没绑定循环（启动前 / 同步测试）：直接投递。
            # 只有"循环存在、而我们在别的线程里"才需要 call_soon_threadsafe。
            direct = self.loop is None or running is self.loop
            for queue in subs:
                if direct:
                    queue.put_nowait(event)
                else:
                    self.loop.call_soon_threadsafe(queue.put_nowait, event)
        return event

    def subscribe(self, video_id: str, since: int = 0) -> asyncio.Queue:
        """新订阅者先拿到 ring 里 seq > since 的事件，之后接着收新事件。"""
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            ch = self._channel(video_id)
            for event in ch.ring:
                if event["seq"] > since:
                    queue.put_nowait(event)
            ch.subs.add(queue)
        return queue

    def unsubscribe(self, video_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            ch = self._channels.get(video_id)
            if ch is not None:
                ch.subs.discard(queue)

    def recent(self, video_id: str, types: Optional[tuple] = None, limit: int = 300) -> list[dict]:
        with self._lock:
            events = list(self._channel(video_id).ring)
        if types:
            events = [e for e in events if e.get("type") in types]
        return events[-limit:]

    def forget(self, video_id: str) -> None:
        """工程被删除：断开所有订阅者，清掉历史。"""
        with self._lock:
            ch = self._channels.pop(video_id, None)
        if ch is not None and self.loop is not None:
            for queue in list(ch.subs):
                self.loop.call_soon_threadsafe(queue.put_nowait, None)


bus = EventBus()
