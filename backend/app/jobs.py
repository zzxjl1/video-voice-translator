"""
任务：初始化、批量翻译、批量配音、克隆、导出 —— 所有"长而贵"的事。

和旧版 `run_pipeline` 的根本区别：

  · **每个任务只处理"缺或过期"的行**（判断来自 `app/status.py` 的指纹），所以天然
    幂等、天然可续：中途取消、崩溃、改了设置再跑，都是"把还缺的补上"，不需要
    `_detect_resume_phase`，也不需要在每处改动后手动"作废"下游产物。
  · **任务里没有设置副本。** 每个步骤开始时现读最新文档；写回只改它自己负责的那几个
    字段（`store.mutate`）。用户在任务运行期间改的字幕样式、译文、静音，不会被任务
    手里的旧副本抹掉 —— 这一类 lost update 在结构上就不存在了。
  · **按行加锁，而不是整个工程 409。** 任务正在处理的行记在 `Job.lines` 里；命令想改
    这些行时被拒绝并说明原因，其它行照常可改。
  · 任务的开始/结束写进事件流，AI 的"自上次以来发生了什么"能看到。

任务本身只负责"编排与调用服务"；翻译/合成/克隆这些具体能力在 `app/services/`。
"""
import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from app import config, status, store
from app.bus import bus
from app.models import (
    STAGE_FAILED, STAGE_INITIALIZING, STAGE_READY, Segment, Speaker, get_video_dir,
)
from app.services import asr_service, llm_service, speech_timing, tts_service

logger = logging.getLogger(__name__)


class JobCancelled(Exception):
    pass


@dataclass
class Job:
    id: str
    video_id: str
    kind: str                       # init | translate | dub | clone | export
    label: str
    actor: str
    lines: set = field(default_factory=set)     # 正在处理的行 id（命令改这些行会被拒）
    started_at: float = field(default_factory=time.time)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    step: Optional[str] = None
    done: int = 0
    total: int = 0
    task: Optional[asyncio.Task] = None
    # body 抛出的异常会记在这里（wrapper 自己不抛）。调用方 `await job.task` 拿到的
    # 返回值里看不出失败 —— 不看这个字段，就会把"全军覆没"当成"0 句失败"报成功。
    error: Optional[str] = None

    def snapshot(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "actor": self.actor,
            "step": self.step, "done": self.done, "total": self.total,
            "cancelling": self.cancel.is_set(),
            "elapsedS": round(time.time() - self.started_at, 1),
        }

    def check(self) -> None:
        """协作式取消的检查点：在每个"一次外部请求"之前调用。"""
        if self.cancel.is_set():
            raise JobCancelled()


_jobs: dict[str, dict[str, Job]] = {}     # video_id -> {job_id: Job}


def active(video_id: str) -> list[Job]:
    return list(_jobs.get(video_id, {}).values())


def active_snapshot(video_id: str) -> list[dict]:
    return [j.snapshot() for j in active(video_id)]


def locked_lines(video_id: str) -> dict[str, Job]:
    out: dict[str, Job] = {}
    for job in active(video_id):
        for line in job.lines:
            out[line] = job
    return out


def find(video_id: str, job_id: Optional[str] = None, kind: Optional[str] = None) -> Optional[Job]:
    for job in active(video_id):
        if (job_id is None or job.id == job_id) and (kind is None or job.kind == kind):
            return job
    return None


def cancel(video_id: str, job_id: Optional[str] = None) -> int:
    n = 0
    for job in active(video_id):
        if job_id is None or job.id == job_id:
            job.cancel.set()
            n += 1
    return n


def _emit(video_id: str, job: Job, event: dict) -> None:
    bus.publish(video_id, {"type": "progress", "job": job.id, "kind": job.kind, **event})


async def run(
    video_id: str,
    kind: str,
    label: str,
    actor: str,
    body: Callable[[Job], Awaitable[Any]],
    *,
    exclusive: bool = False,
) -> Job:
    """
    启动一个任务（后台跑），立刻返回 Job。

    `exclusive`：同类任务只允许一个（初始化、导出）。其它任务可以并行，互相靠行锁隔离。
    """
    if exclusive and find(video_id, kind=kind):
        raise RuntimeError(f"已经有一个{label}在进行了")
    job = Job(id=uuid.uuid4().hex[:8], video_id=video_id, kind=kind, label=label, actor=actor)
    _jobs.setdefault(video_id, {})[job.id] = job
    store.record_job_event(video_id, {"phase": "started", "job": job.id, "kind": kind, "label": label, "actor": actor})

    async def wrapper():
        outcome, error = "done", None
        try:
            result = await body(job)
            return result
        except JobCancelled:
            outcome = "cancelled"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as e:
            outcome, error = "failed", str(e)
            job.error = str(e)
            logger.error(f"[{video_id}] job {kind} failed: {e}", exc_info=True)
        finally:
            _jobs.get(video_id, {}).pop(job.id, None)
            store.record_job_event(video_id, {
                "phase": outcome, "job": job.id, "kind": kind, "label": label,
                "actor": actor, **({"error": error} if error else {}),
                "elapsedS": round(time.time() - job.started_at, 1),
            })

    job.task = asyncio.create_task(wrapper())
    return job


# ============================================================================
# 初始化 —— 上传之后的第一遍：转写 →（审听）→ 翻译 →（克隆）→ 配音
# ============================================================================

AUDIO_URL = "{base}/api/projects/{vid}/audio"


def _line_dict(seg: Segment) -> dict:
    return {"id": seg.id, "speaker_id": seg.speaker_id, "start_time": seg.start_time,
            "end_time": seg.end_time, "text": seg.text}


async def _run_in_thread(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


async def step_prepare_audio(video_id: str, job: Job) -> str:
    """抽 16 kHz 单声道音轨（ASR 和 omni 都听它）；有人声分离结果时用人声。"""
    state = store.require_state(video_id)
    base = get_video_dir(video_id)
    extracted = os.path.join(base, "extracted_audio.wav")
    if not os.path.exists(extracted):
        await _run_in_thread(asr_service.extract_audio, state.file_path, extracted)

    vocals = os.path.join(base, "vocals.wav")
    if os.path.exists(vocals):
        derived = os.path.join(base, "vocals_asr.wav")
        # 浏览器上传的 vocals 是 44.1 kHz；交给 ASR/omni 的是 16 kHz 单声道的派生版
        # （上传量小几倍，识别没有损失）。克隆读的仍是全频带的 vocals.wav。
        if not os.path.exists(derived) or os.path.getmtime(derived) < os.path.getmtime(vocals):
            await _run_in_thread(asr_service.extract_audio, vocals, derived)
        return derived
    return extracted


async def step_transcribe(video_id: str, job: Job, server_url: str) -> None:
    """ASR。已经有转写（重试/续跑）就跳过 —— 重新识别是显式命令，不是顺带发生的。"""
    state = store.require_state(video_id)
    if state.segments:
        _emit(video_id, job, {"phase": "asr", "status": "skipped", "lines": len(state.segments)})
        return
    job.check()
    _emit(video_id, job, {"phase": "asr", "status": "started"})
    audio_path = await step_prepare_audio(video_id, job)
    lines = await asr_service.transcribe_video(
        video_id, state.file_path, audio_path, AUDIO_URL.format(base=server_url, vid=video_id)
    )

    def apply(s):
        s.segments = [Segment(id=l["id"], speaker_id=l["speaker_id"], start_time=l["start_time"],
                              end_time=l["end_time"], text=l["text"]) for l in lines]
        ids = sorted({seg.speaker_id for seg in s.segments})
        old = {sp.id: sp for sp in s.speakers}
        s.speakers = [old.get(i) or Speaker(id=i, name=f"Speaker {i}") for i in ids]

    store.mutate(video_id, apply, actor="system", cmd="transcribe", summary=f"转写完成：{len(lines)} 句")
    _emit(video_id, job, {"phase": "asr", "status": "done", "lines": len(lines)})


async def step_review(video_id: str, job: Job, server_url: str) -> None:
    """omni 审听：纠错 + 语气标签。每一行的审听版存在行上；失败只降级，不阻断。"""
    state = store.require_state(video_id)
    if not state.mm_enhance:
        return
    todo = [s for s in state.segments if s.kind == "speech" and (s.text or "").strip() and not s.reviewed_text]
    if not todo:
        _emit(video_id, job, {"phase": "mm_enhance", "status": "skipped"})
        return
    job.check()
    _emit(video_id, job, {"phase": "mm_enhance", "status": "started", "count": len(todo)})
    try:
        from app.services import omni_service
        _emit(video_id, job, {"phase": "mm_enhance", "status": "listening"})
        enhanced = await omni_service.enhance_transcript(
            video_id, [{"id": s.id, "text": s.text} for s in todo],
            AUDIO_URL.format(base=server_url, vid=video_id),
        )

        def apply(s):
            for seg in todo:
                cur = s.segment(seg.id)
                # 只在这一行的原文没被改过时落审听版（用户期间改了就以用户的为准）
                if cur is not None and cur.text == seg.text and seg.id in enhanced:
                    cur.reviewed_text = enhanced[seg.id] if enhanced[seg.id] != seg.text else ""

        store.mutate(video_id, apply, actor="system", cmd="review", summary="审听完成")
        adjusted = sum(1 for s in todo if enhanced.get(s.id, s.text) != s.text)
        _emit(video_id, job, {"phase": "mm_enhance", "status": "done", "adjusted": adjusted})
    except Exception as e:  # 增强不是依赖
        logger.warning(f"[{video_id}] review failed (continuing with raw ASR text): {e}")
        _emit(video_id, job, {"phase": "mm_enhance", "status": "failed", "error": str(e)[:120]})


async def translate_lines(
    video_id: str, job: Job, ids: Optional[list[str]] = None,
    length_hint: Optional[str] = None, extra_prompt: str = "",
) -> list[str]:
    """
    翻译缺失/过期的行（或指定的行）。返回失败的行 id。

    译文和它的指纹在**同一次** mutate 里写入，所以任何时刻读到的译文都带着正确的指纹。
    写回前再核对一次基底：翻译期间用户改了这一行原文，这份译文就不落（指纹会对不上，
    下次再翻）。
    """
    state = store.require_state(video_id)
    want = set(ids) if ids else None
    todo = [
        s for s in state.segments
        if (want is None or s.id in want)
        and status.tr_status(state, s) in ((status.MISSING, status.STALE) if want is None
                                           else (status.MISSING, status.STALE, status.FRESH))
    ]
    if not todo:
        return []
    job.lines |= {s.id for s in todo}
    job.check()
    _emit(video_id, job, {"phase": "translation", "status": "started", "count": len(todo)})

    sources = {s.id: status.translation_source(state, s) for s in todo}
    context = [
        {"id": s.id, "text": sources[s.id], "speaker_id": s.speaker_id,
         "start_time": s.start_time, "end_time": s.end_time}
        for s in todo
    ]
    prompt = "\n".join(p for p in (state.custom_prompt, extra_prompt) if p)[:500]

    async def relay(event):
        _emit(video_id, job, event)

    results, failed = await llm_service.translate_script(
        video_id, context, state.target_language, emit=relay,
        text_overrides={s.id: sources[s.id] for s in todo if sources[s.id] != s.text} or None,
        custom_prompt=prompt, length_hint=length_hint,
    )
    texts = {r["id"]: r["translatedText"] for r in results}

    def apply(s):
        for seg in todo:
            cur = s.segment(seg.id)
            new = texts.get(seg.id)
            if cur is None or not new:
                continue
            if status.translation_source(s, cur) != sources[seg.id]:
                continue                       # 翻译期间原文被改了：这份译文已经过期，不落
            cur.translated_text = new
            cur.tr_fp = status.expected_tr_fp(s, cur)

    store.mutate(video_id, apply, actor="system" if job.actor == "system" else job.actor,
                 cmd="translate", summary=f"翻译了 {len(texts)} 句", undoable=job.actor != "system")
    _emit(video_id, job, {"phase": "translation", "status": "done", "count": len(texts), "failed": len(failed)})
    job.lines -= {s.id for s in todo}
    return list(failed)


async def clone_speakers(video_id: str, job: Job, server_url: str, only: Optional[list[str]] = None) -> None:
    """给说话人克隆音色。已有且服务端还活着的复用（免费）；只对还没有的发起注册。"""
    from app.services import voice_clone_service as vc
    state = store.require_state(video_id)
    if not (state.enable_dubbing and state.enable_voice_clone):
        return
    speakers = [sp for sp in state.speakers if only is None or sp.id in only]
    _emit(video_id, job, {"phase": "voice_clone", "status": "started", "total": len(speakers)})

    for i, sp in enumerate(speakers, 1):
        job.check()
        job.step = f"克隆 {sp.name}"
        base = {"phase": "voice_clone", "progress": i, "total": len(speakers), "speaker_id": sp.id}
        try:
            current = store.require_state(video_id)
            speaker = current.speaker(sp.id)
            if speaker and status.clone_voice_id(current, speaker) and await vc.verify_voice(speaker.clone["voice_id"]):
                _emit(video_id, job, {**base, "status": "done", "reused": True})
                continue
            selected = await vc.pick_reference_lines(
                video_id, current.segments, sp.id,
                AUDIO_URL.format(base=server_url, vid=video_id), current.clone_smart_pick,
                emit=lambda e: _async_emit(video_id, job, e),
            )
            voice_id = await vc.enroll_voice(video_id, sp.id, selected, server_url,
                                             emit=lambda e: _async_emit(video_id, job, e))

            def apply(s, voice_id=voice_id, sid=sp.id):
                target = s.speaker(sid)
                if target is not None:
                    target.clone = {"voice_id": voice_id, "target_model": vc.clone_target_model(),
                                    "created_at": int(time.time())}

            store.mutate(video_id, apply, actor="system", cmd="clone", summary=f"{sp.name} 的克隆音色就绪")
            _emit(video_id, job, {**base, "status": "done", "voice_id": voice_id, "reused": False})
        except JobCancelled:
            raise
        except Exception as e:
            logger.error(f"[{video_id}] clone failed for {sp.id}: {e}")
            _emit(video_id, job, {**base, "status": "failed", "error": str(e)})
    _emit(video_id, job, {"phase": "voice_clone", "status": "complete"})


async def _async_emit(video_id: str, job: Job, event: dict) -> None:
    _emit(video_id, job, event)


def ensure_speaker_voices(video_id: str) -> None:
    """给还没有音色的说话人分配系统音色（尽量不重复）；已有的（含手动指定的）不动。"""
    def apply(s):
        taken = {sp.voice for sp in s.speakers if sp.voice}
        for sp in s.speakers:
            if sp.voice or status.clone_voice_id(s, sp):
                continue
            sp.voice = tts_service.pick_voice(s.target_language, taken)
            taken.add(sp.voice)

    store.mutate(video_id, apply, actor="system", cmd="assign_voices", summary="分配音色", undoable=False)


async def dub_lines(video_id: str, job: Job, ids: Optional[list[str]] = None, force: bool = False) -> list[str]:
    """
    给缺/过期的行（或指定的行，`force` 时即使新鲜也重做）合成配音。返回失败的行 id。

    并发合成，每合成完一行立刻把 mp3 的指纹和时长写进文档 —— 中途取消或崩溃，已经
    合成好的行不会丢，下次只补剩下的。
    """
    state = store.require_state(video_id)
    if not state.enable_dubbing:
        return []
    ensure_speaker_voices(video_id)
    state = store.require_state(video_id)

    want = set(ids) if ids else None
    todo = [
        s for s in state.segments
        if status.dubbable(state, s)
        and (want is None or s.id in want)
        and (force or status.tts_status(state, s) in (status.MISSING, status.STALE))
    ]
    if not todo:
        return []
    job.lines |= {s.id for s in todo}
    job.total, job.done = len(todo), 0
    _emit(video_id, job, {"phase": "tts", "status": "started", "total": len(todo),
                          "concurrency": config.TTS_CONCURRENCY})
    sem = asyncio.Semaphore(max(1, config.TTS_CONCURRENCY))
    failed: list[str] = []
    errors: list[str] = []

    async def one(seg: Segment) -> None:
        async with sem:
            if job.cancel.is_set():
                return
            try:
                cur = store.require_state(video_id)
                line = cur.segment(seg.id)
                if line is None or not status.dubbable(cur, line):
                    return
                voice = status.resolve_voice(cur, line)
                text = line.translated_text
                slot = max(0.5, (line.end_time or 0.0) - (line.start_time or 0.0))
                rate = line.tts_rate if line.tts_rate is not None else speech_timing.plan_speech_rate(
                    text, cur.target_language, slot)
                instruction = status.dub_instruction(cur, line)
                _, duration = await tts_service.synthesize_speech(
                    video_id, seg.id, text, voice, speech_rate=rate,
                    target_duration=slot if line.tts_rate is None else None,
                    instruction=instruction,
                )

                def apply(s):
                    target = s.segment(seg.id)
                    if target is None:
                        return
                    target.actual_duration = duration
                    # 指纹按"合成时用的那份设置"记，而不是按写回时的最新设置：
                    # 合成期间用户改了语速/译文，写回后指纹自然对不上 → 判过期，下次重做。
                    target.tts_fp = _fp_from(cur, line)

                store.mutate(video_id, apply, actor="system", cmd="dub", summary=f"配音：{seg.id}", undoable=False)
                job.done += 1
                _emit(video_id, job, {"phase": "tts", "segment_id": seg.id, "progress": job.done, "total": job.total})
            except Exception as e:
                logger.error(f"[{video_id}] TTS failed for {seg.id}: {e}")
                failed.append(seg.id); errors.append(str(e))
                job.done += 1
                _emit(video_id, job, {"phase": "tts", "segment_id": seg.id, "tts_error": str(e),
                                      "progress": job.done, "total": job.total})

    await asyncio.gather(*(one(s) for s in todo))
    job.lines -= {s.id for s in todo}
    job.check()
    _emit(video_id, job, {"phase": "tts", "status": "done", "failed": len(failed),
                          **({"lastError": errors[-1][:200]} if errors else {})})
    return failed


def _fp_from(state, seg) -> str:
    """用 `state` 里那份设置算 `seg` 的配音指纹（合成当时的快照）。"""
    return status.expected_tts_fp(state, seg) or ""


async def initialize(video_id: str, job: Job, server_url: str) -> dict:
    """
    初始化：转写 → 审听 → 翻译 → 克隆 → 配音。每一步只做"还缺的"，所以失败后重试、
    取消后继续、改了设置再跑，都是同一个函数。结束时写一份报告，聊天的第一条消息据此生成。
    """
    def stage(value, error=None):
        def f(s):
            s.stage = value
            s.init_error = error
        store.mutate(video_id, f, actor="system", cmd="stage", summary=f"阶段：{value}", undoable=False)

    stage(STAGE_INITIALIZING)
    report: dict = {"steps": [], "startedAt": time.time()}
    try:
        job.step = "转写"
        await step_transcribe(video_id, job, server_url)
        job.check()

        job.step = "审听"
        await step_review(video_id, job, server_url)
        job.check()

        job.step = "翻译"
        failed_tr = await translate_lines(video_id, job)
        job.check()

        job.step = "克隆"
        await clone_speakers(video_id, job, server_url)
        job.check()

        job.step = "配音"
        failed_dub = await dub_lines(video_id, job)

        state = store.require_state(video_id)
        report.update({
            "finishedAt": time.time(),
            "lines": len(state.segments),
            "speakers": len(state.speakers),
            "reviewAdjusted": sum(1 for s in state.segments if s.reviewed_text),
            "failedTranslation": failed_tr,
            "failedDub": failed_dub,
            "settings": view_settings(state),
        })

        def finish(s):
            s.init_report = report
        store.mutate(video_id, finish, actor="system", cmd="init_report", summary="初始化完成", undoable=False)
        stage(STAGE_READY)
        _emit(video_id, job, {"phase": "init", "status": "done"})
        return report
    except JobCancelled:
        stage(STAGE_READY if store.require_state(video_id).segments else STAGE_FAILED,
              None if store.require_state(video_id).segments else "已取消")
        raise
    except Exception as e:
        has_lines = bool(store.require_state(video_id).segments)
        # 有转写结果就进编辑器（剩下的缺口在聊天里补），否则才算失败
        stage(STAGE_READY if has_lines else STAGE_FAILED, None if has_lines else str(e))
        report.update({"error": str(e)[:300]})
        if has_lines:
            # 失败也把报告写回：欢迎消息和界面要从 init_report.error 读原因，
            # 只改局部变量的话，用户看到的是"没有解释的半成品"。
            def failed(s):
                s.init_report = report
            store.mutate(video_id, failed, actor="system", cmd="init_report", summary="初始化未全部完成", undoable=False)
        raise


def view_settings(state) -> dict:
    from app import view
    return view.settings_view(state)
