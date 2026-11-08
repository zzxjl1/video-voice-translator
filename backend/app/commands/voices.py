"""
音色类命令：给说话人换系统音色、克隆音色、试听。

音色是**说话人**的属性（同一个人不该说到一半换声音），所以设在说话人上；单句要特殊
再用 `set_dub_style voice=`。换音色之后该说话人所有句子的配音过期。
"""
from typing import Optional

from app import config, jobs, status, store
from app.commands.registry import CONFIRM_COST, CommandError, Ctx, command, resolve_lines, resolve_speaker


@command(
    "set_speaker_voice", "给说话人指定系统音色（见 list_voices）；voice 为空 = 交回自动分配。"
    "该说话人所有句子的配音随之过期。",
    {"type": "object", "properties": {
        "speaker": {"type": "string"}, "voice": {"type": "string"},
    }, "required": ["speaker"]},
)
def set_speaker_voice(ctx: Ctx, speaker: str, voice: str = ""):
    state = ctx.state
    sp = resolve_speaker(state, speaker)
    if voice:
        pool = config.voices_for_language(state.target_language)
        if voice not in pool:
            raise CommandError(f"音色 {voice!r} 不能用于{state.target_language}", hint="用 list_voices 看可选的")

    def apply(s):
        target = s.speaker(sp.id)
        if voice:
            target.voice = voice
        else:
            target.voice = None
            from app.services import tts_service
            taken = {x.voice for x in s.speakers if x.voice}
            target.voice = tts_service.pick_voice(s.target_language, taken)
    ctx.change(apply, f"给 {sp.name} {'换音色 ' + config.voice_label(voice) if voice else '重新自动分配音色'}")
    return {"speaker": sp.id, "voice": voice or "(自动)",
            "lines_affected": sum(1 for x in state.segments if x.speaker_id == sp.id),
            "note": "该说话人的配音已过期；redub 补上"}


@command(
    "clone_voice", "为说话人克隆音色（花钱，需要几十秒到几分钟）。可以指定用哪几句做参考（不给就自动挑；"
    "参考句要干净、单人、不要音乐，合计 10~20 秒最好）。",
    {"type": "object", "properties": {
        "speaker": {"type": "string"},
        "reference_lines": {"type": "array", "items": {"type": "integer", "minimum": 1},
                            "description": "参考句的行号；不给则自动挑"},
    }, "required": ["speaker"]},
    confirm=CONFIRM_COST, group="job",
)
async def clone_voice(ctx: Ctx, speaker: str, reference_lines: Optional[list] = None):
    from app.services import voice_clone_service as vc
    state = ctx.state
    sp = resolve_speaker(state, speaker)
    if not state.enable_dubbing:
        raise CommandError("仅字幕模式不需要音色")
    server_url = config.SERVER_URL_BASE

    async def body(job):
        job.step = f"克隆 {sp.name}"
        current = store.require_state(ctx.video_id)
        if reference_lines:
            segs = resolve_lines(current, reference_lines)
            try:
                chosen = vc.validate_chosen(current.segments, sp.id, [s.id for s in segs])
            except ValueError as e:
                raise CommandError(str(e))
        else:
            chosen = await vc.pick_reference_lines(
                ctx.video_id, current.segments, sp.id,
                f"{server_url}/api/projects/{ctx.video_id}/audio", current.clone_smart_pick,
                emit=lambda e: ctx.emit(e),
            )
        voice_id = await vc.enroll_voice(ctx.video_id, sp.id, chosen, server_url, emit=lambda e: ctx.emit(e))

        def apply(s):
            s.enable_voice_clone = True
            s.speaker(sp.id).clone = {"voice_id": voice_id, "target_model": vc.clone_target_model(),
                                      "created_at": __import__("time").time().__int__()}
        store.mutate(ctx.video_id, apply, actor=ctx.actor, cmd="clone_voice",
                     summary=f"{sp.name} 的克隆音色就绪（参考 {len(chosen)} 句）")
        return {"voice_id": voice_id, "reference_lines": [state.segments.index(state.segment(s.id)) + 1 for s in chosen
                                                         if state.segment(s.id)]}

    job = await jobs.run(ctx.video_id, "clone", f"克隆 {sp.name}", ctx.actor, body)
    result = await job.task
    if result is None:
        raise CommandError("克隆没有完成（失败或被取消），详情见任务事件")
    return {**result, "note": "该说话人的配音已过期；redub 补上"}


@command(
    "preview_voice", "试听某个说话人当前的音色（合成一句固定的话）。结果在界面上可以播放。",
    {"type": "object", "properties": {"speaker": {"type": "string"}}, "required": ["speaker"]},
    group="job",
)
async def preview_voice(ctx: Ctx, speaker: str):
    from app.services import voice_clone_service as vc
    state = ctx.state
    sp = resolve_speaker(state, speaker)
    voice = status.clone_voice_id(state, sp) or sp.voice
    if not voice:
        raise CommandError(f"{sp.name} 还没有音色")
    model = vc.clone_target_model()
    await vc.preview_voice(ctx.video_id, voice, model, state.target_language, sp.id)
    ctx.ui("play_voice_preview", speaker=sp.id,
           url=f"/api/projects/{ctx.video_id}/voice-preview/{sp.id}?t={__import__('time').time().__int__()}")
    return {"speaker": sp.id, "voice": voice, "played_in_ui": True}
