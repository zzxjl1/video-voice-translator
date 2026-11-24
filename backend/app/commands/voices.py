"""
音色类命令：给说话人换系统音色、克隆音色、生成候选音色、试听。

音色是**说话人**的属性（同一个人不该说到一半换声音），所以设在说话人上；单句要特殊
再用 `set_dub_style voice=`。换音色之后该说话人所有句子的配音过期。

克隆音色有"当前选用"（`Speaker.clone`，导出 / 配音只认它）和"候选"
（`Speaker.clone_candidates`，同一说话人用不同取材预设做的几个音色，让用户听着挑）。
"""
import asyncio
import os
import time
import uuid
from typing import Optional

from app import config, jobs, status, store
from app.commands.guards import _need_balance
from app.commands.registry import (
    CONFIRM_COST, CONFIRM_DESTROY, CommandError, Ctx, command, resolve_lines, resolve_speaker,
)
from app.services import voice_presets as vp


@command(
    "assign_browser_voices",
    "为浏览器配音的每位说话人分配不同音色（只填空着的；已有选择不覆盖）。",
    {"type": "object", "properties": {
        "voices": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    }, "required": ["voices"]},
)
def assign_browser_voices(ctx: Ctx, voices: list[str]):
    state = ctx.state
    if state.dubbing_mode != "browser":
        raise CommandError("只有浏览器配音模式才能分配音色")
    if not voices:
        raise CommandError("至少要有一个本地音色")
    bad = [v for v in voices if not config.browser_voice_fits_language(v, state.target_language)]
    if bad:
        raise CommandError(f"这些音色不是 {state.target_language} 的：{bad}")
    def apply(s):
        for i, sp in enumerate(s.speakers):
            if not sp.browser_voice:
                sp.browser_voice = voices[i % len(voices)]
    ctx.change(apply, "分配浏览器音色")
    return {"assigned": {sp.id: sp.browser_voice for sp in ctx.state.speakers}}


@command(
    "set_line_browser_voice",
    "单句改用某个本地音色；voice 为空 = 跟随说话人。",
    {"type": "object", "properties": {
        "lines": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "voice": {"type": "string"},
    }, "required": ["lines"]},
)
def set_line_browser_voice(ctx: Ctx, lines: list[int], voice: str = ""):
    state = ctx.state
    if state.dubbing_mode != "browser":
        raise CommandError("只有浏览器配音模式才能改音色")
    segs = resolve_lines(state, lines)
    if not segs:
        raise CommandError("没有找到这些行")
    if voice and not config.browser_voice_fits_language(voice, state.target_language):
        raise CommandError(f"音色 {voice!r} 不是 {state.target_language} 的")
    def apply(s):
        for item in segs:
            target = s.segment(item.id)
            if target is not None:
                target.browser_voice = voice or None
    ctx.change(apply, "改浏览器单句音色")
    return {"lines": [s.id for s in segs], "voice": voice or None}


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


def _clone_record(voice_id: str, plan, vc, *, cid: Optional[str] = None) -> dict:
    """一条克隆音色的登记：只存历史事实（怎么做的、用了哪几句），素材量之类现算。"""
    rec = {"voice_id": voice_id, "target_model": vc.clone_target_model(),
           "created_at": int(time.time()), "preset": plan.preset, "lines": plan.line_ids,
           "seconds": round(plan.seconds, 1)}
    if cid:
        rec["id"] = cid
    return rec


def _line_numbers(state, ids: list[str]) -> list[int]:
    return [state.segments.index(s) + 1 for s in (state.segment(i) for i in ids) if s is not None]


def _need_remote(state) -> None:
    """克隆音色、候选、试听都走 DashScope（花钱）。浏览器配音 / 仅字幕模式下不该被触发。"""
    if state.dubbing_mode != "remote":
        raise CommandError("声音克隆和试听只在远端配音（remote）模式下可用；浏览器配音用本地 Kokoro 音色，不克隆")


def _audio_url(video_id: str) -> str:
    """给 omni 取音频用：它没有登录 cookie，必须带签名令牌（见 jobs._public_audio_url）。"""
    return jobs._public_audio_url(config.SERVER_URL_BASE, video_id)


@command(
    "clone_voice", "为说话人克隆音色（花钱，需要几十秒到几分钟）。可以指定用哪几句做参考（不给就自动挑；"
    "参考句要干净、单人、不要音乐，合计 10~20 秒最好）或指定取材预设 preset"
    "（continuous 平稳连读 / long_one 单句长段 / mid_concat 中句拼接 / smart 智能挑选）。"
    "想多试几种再挑，用 generate_voice_candidates。",
    {"type": "object", "properties": {
        "speaker": {"type": "string"},
        "reference_lines": {"type": "array", "items": {"type": "integer", "minimum": 1},
                            "description": "参考句的行号；不给则自动挑"},
        "preset": {"type": "string", "enum": list(vp.ALL_PRESETS),
                   "description": "取材预设；给了 reference_lines 时忽略"},
    }, "required": ["speaker"]},
    confirm=CONFIRM_COST, group="job",
)
async def clone_voice(ctx: Ctx, speaker: str, reference_lines: Optional[list] = None, preset: Optional[str] = None):
    _need_balance(ctx, "克隆音色")
    from app.services import voice_clone_service as vc
    state = ctx.state
    _need_remote(state)
    sp = resolve_speaker(state, speaker)

    async def body(job):
        job.step = f"克隆 {sp.name}"
        current = store.require_state(ctx.video_id)
        if reference_lines:
            segs = resolve_lines(current, reference_lines)
            try:
                chosen = vc.validate_chosen(current.segments, sp.id, [s.id for s in segs])
            except ValueError as e:
                raise CommandError(str(e))
            plan = vp.plan_from_segments("manual", chosen)
        else:
            plan = await vc.pick_plan(
                ctx.video_id, current.segments, sp.id, preset,
                audio_url=_audio_url(ctx.video_id),
                target_language=current.target_language, smart=current.clone_smart_pick,
                emit=lambda e: ctx.emit(e),
            )
        voice_id = await vc.enroll_voice(ctx.video_id, sp.id, plan, emit=lambda e: ctx.emit(e))

        def apply(s):
            s.enable_voice_clone = True
            s.speaker(sp.id).clone = _clone_record(voice_id, plan, vc)
        store.mutate(ctx.video_id, apply, actor=ctx.actor, cmd="clone_voice",
                     summary=f"{sp.name} 的克隆音色就绪（参考 {len(plan.line_ids)} 句）")
        return {"voice_id": voice_id, "preset": plan.preset,
                "reference_lines": _line_numbers(state, plan.line_ids)}

    job = await jobs.run(ctx.video_id, "clone", f"克隆 {sp.name}", ctx.actor, body)
    result = await job.task
    if job.error:
        raise CommandError(f"克隆失败：{job.error}")
    if result is None:
        raise CommandError("克隆没有完成（可能被取消）")
    return {**result, "note": "该说话人的配音已过期；redub 补上"}


def _preview_voices(state, sp) -> list[tuple[str, str]]:
    """[(voice_id, 展示名)]：当前选用的克隆音色在最前，后面是候选。"""
    voices = [(c["voice_id"], c.get("preset") or "") for c in sp.clone_candidates]
    if status.clone_voice_id(state, sp):
        voices.insert(0, (sp.clone["voice_id"], "current"))
    return voices


async def _synth_previews(ctx: Ctx, sp, voices: list[tuple[str, str]]) -> int:
    """给还没有试听的音色合成那句固定的话；返回新合成了几个。已经有的不重做（不花钱）。"""
    from app.services import voice_clone_service as vc
    state = ctx.state
    text = vp.preview_sentence(state.target_language)
    made = 0
    for voice_id, _label in voices:
        name = vp.preview_filename(voice_id, text)
        if os.path.exists(vc.preview_path(ctx.video_id, sp.id, name)):
            continue
        try:
            await asyncio.wait_for(
                vc.preview_voice(ctx.video_id, voice_id, vc.clone_target_model(), state.target_language,
                                 sp.id, text=text, name=name),
                timeout=config.TTS_CALL_TIMEOUT + 10,
            )
        except asyncio.TimeoutError:
            raise RuntimeError("试听合成超时（DashScope 无响应），稍后再试") from None
        made += 1
    return made


@command(
    "generate_voice_candidates",
    "为说话人用几种不同的取材预设各克隆一个候选音色，并用同一句固定的话做好试听，让用户听着挑"
    "（花钱：每个预设新建一个音色）。presets 取值：continuous 平稳连读 / long_one 单句长段 / "
    "mid_concat 中句拼接 / smart 智能挑选。不给 presets = 所有能做的规则型预设（给了 reference_lines "
    "而没给 presets 时只做手动指定那一个）。reference_lines 是用户指定的参考句行号，做成一个“手动指定”候选。"
    "选定后用 use_voice_candidate。",
    {"type": "object", "properties": {
        "speaker": {"type": "string"},
        "presets": {"type": "array", "items": {"type": "string", "enum": list(vp.ALL_PRESETS)}},
        "reference_lines": {"type": "array", "items": {"type": "integer", "minimum": 1},
                            "description": "手动指定的参考句行号（同一说话人、干净单人、合计 10~20 秒最好）"},
    }, "required": ["speaker"]},
    confirm=CONFIRM_COST, group="job",
)
async def generate_voice_candidates(ctx: Ctx, speaker: str, presets: Optional[list] = None,
                                    reference_lines: Optional[list] = None):
    _need_balance(ctx, "生成候选音色")
    from app.services import voice_clone_service as vc
    state = ctx.state
    _need_remote(state)
    sp = resolve_speaker(state, speaker)
    options = {o["id"]: o for o in vp.preset_options(state.segments, sp.id)}
    if presets:
        bad = [p for p in presets if p not in options]
        if bad:
            raise CommandError(f"没有这些预设：{bad}", hint=f"可选：{list(options)}")
        blocked = [f"{options[p]['label']}（{options[p]['reason']}）" for p in presets if not options[p]["available"]]
        if blocked:
            raise CommandError("这些预设在这个说话人身上做不了：" + "；".join(blocked))
        todo = list(dict.fromkeys(presets))
    elif reference_lines:
        todo = []
    else:
        todo = [p for p, o in options.items() if o["available"] and p != "smart"]
    manual_ids: Optional[list[str]] = None
    if reference_lines:
        try:
            manual_ids = [x.id for x in vc.validate_chosen(
                state.segments, sp.id, [s.id for s in resolve_lines(state, reference_lines)])]
        except ValueError as e:
            raise CommandError(str(e))
    if not todo and not manual_ids:
        raise CommandError("这个说话人没有能做的预设（素材不足）")
    specs: list[tuple[str, Optional[list[str]]]] = [(p, None) for p in todo]
    if manual_ids:
        specs.append(("manual", manual_ids))

    async def body(job):
        made: list[dict] = []
        for n, (preset, manual) in enumerate(specs, 1):
            job.check()
            label = vp.PRESET_LABELS.get(preset, "手动指定")
            job.step = f"{sp.name}：{label}（{n}/{len(specs)}）"
            current = store.require_state(ctx.video_id)
            try:
                if manual:
                    plan = vp.plan_from_segments("manual", [current.segment(i) for i in manual if current.segment(i)])
                else:
                    plan = await vc.pick_plan(
                        ctx.video_id, current.segments, sp.id, preset,
                        audio_url=_audio_url(ctx.video_id),
                        target_language=current.target_language, emit=lambda e: ctx.emit(e),
                    )
                cid = "c" + uuid.uuid4().hex[:8]
                voice_id = await vc.enroll_voice(ctx.video_id, sp.id, plan, label=cid, emit=lambda e: ctx.emit(e))
            except Exception as e:  # noqa: BLE001 - 一个预设失败不拖垮其余
                made.append({"preset": preset, "error": str(e)[:160]})
                continue
            rec = _clone_record(voice_id, plan, vc, cid=cid)

            def apply(s, rec=rec):
                s.speaker(sp.id).clone_candidates.append(rec)
            store.mutate(ctx.video_id, apply, actor=ctx.actor, cmd="generate_voice_candidates",
                         summary=f"{sp.name} 新增候选音色：{label}")
            made.append({"preset": preset, "id": cid})
        after = store.require_state(ctx.video_id)
        sp_now = after.speaker(sp.id)
        if sp_now is not None:
            try:
                await _synth_previews(ctx, sp_now, _preview_voices(after, sp_now))
            except Exception as e:  # noqa: BLE001 - 试听失败不影响候选本身，界面上点播放会重试
                made.append({"preview_error": str(e)[:160]})
        return made

    job = await jobs.run(ctx.video_id, "clone", f"{sp.name} 候选音色", ctx.actor, body)
    result = await job.task
    if job.error:
        raise CommandError(f"生成候选失败：{job.error}")
    if result is None:
        raise CommandError("生成候选没有完成（可能被取消）")
    if not any(r.get("id") for r in result):
        raise CommandError("所有候选都失败了：" + "；".join(r.get("error", "") for r in result if r.get("error")))
    return {"speaker": sp.id, "candidates": result, "note": "已用同一句固定的话做好试听；听完用 use_voice_candidate 选用"}


@command(
    "preview_voice_candidates",
    "让某个说话人的当前音色或某个候选音色念同一句固定的试听句。voice_id 不给 = 当前音色 + 所有候选；"
    "给了就只合成那一个（界面点哪一行就只做哪一行，不用等全部）。已有试听的不重做（不重复花钱）。",
    {"type": "object", "properties": {
        "speaker": {"type": "string"},
        "voice_id": {"type": "string", "description": "只合成这个音色；不给=全部"},
    }, "required": ["speaker"]},
    group="job",
)
async def preview_voice_candidates(ctx: Ctx, speaker: str, voice_id: Optional[str] = None):
    from app.services import voice_clone_service as vc
    state = ctx.state
    _need_remote(state)
    sp = resolve_speaker(state, speaker)
    voices = _preview_voices(state, sp)
    if not voices:
        raise CommandError(f"{sp.name} 还没有克隆音色")
    if voice_id:
        match = next((v for v in voices if v[0] == voice_id), None)
        if match is None:
            raise CommandError("这个音色不属于该说话人（可能已经被删除）")
        voices = [match]
    made = await _synth_previews(ctx, sp, voices)
    preview_url = None
    if len(voices) == 1:
        text = vp.preview_sentence(state.target_language)
        name = vp.preview_filename(voices[0][0], text)
        if os.path.exists(vc.preview_path(ctx.video_id, sp.id, name)):
            preview_url = f"/api/projects/{ctx.video_id}/voice-preview/{sp.id}/{name}"
    return {"speaker": sp.id, "text": vp.preview_sentence(state.target_language),
            "synthesized": made, "voice_id": voice_id, "preview_url": preview_url}


@command(
    "use_voice_candidate",
    "把某个候选音色设为说话人当前使用的音色（不花钱；原来的音色退成候选，随时能切回来）。"
    "该说话人所有句子的配音随之过期，需要 redub 补上（花钱）。",
    {"type": "object", "properties": {
        "speaker": {"type": "string"},
        "candidate": {"type": "string", "description": "候选 id（见说话人的 candidates）"},
    }, "required": ["speaker", "candidate"]},
)
def use_voice_candidate(ctx: Ctx, speaker: str, candidate: str):
    state = ctx.state
    _need_remote(state)
    sp = resolve_speaker(state, speaker)
    cand = next((c for c in sp.clone_candidates if c.get("id") == candidate), None)
    if cand is None:
        raise CommandError(f"{sp.name} 没有候选 {candidate!r}")
    if cand.get("target_model") != config.TTS_MODEL:
        raise CommandError("这个候选是按别的 TTS 模型克隆的，不能用了；重新生成")

    def apply(s):
        target = s.speaker(sp.id)
        old = target.clone
        rest = [c for c in target.clone_candidates if c.get("id") != candidate]
        if old and old.get("voice_id") != cand["voice_id"]:
            rest.append({**old, "id": old.get("id") or "c" + uuid.uuid4().hex[:8]})
        target.clone = {k: v for k, v in cand.items() if k != "id"}
        target.clone_candidates = rest
        s.enable_voice_clone = True
    ctx.change(apply, f"{sp.name} 换用候选音色：{vp.PRESET_LABELS.get(cand.get('preset'), cand.get('preset'))}")
    lines = sum(1 for x in state.segments if x.speaker_id == sp.id and status.dubbable(state, x))
    return {"speaker": sp.id, "lines_affected": lines, "note": "该说话人的配音已过期；redub 补上"}


@command(
    "delete_voice_candidates",
    "删掉说话人没选用的候选音色（同时清掉服务端的音色，释放额度）。candidate 不给 = 全部没选用的。",
    {"type": "object", "properties": {
        "speaker": {"type": "string"},
        "candidate": {"type": "string"},
    }, "required": ["speaker"]},
    confirm=CONFIRM_DESTROY,
)
async def delete_voice_candidates(ctx: Ctx, speaker: str, candidate: Optional[str] = None):
    from app.services import voice_clone_service as vc
    state = ctx.state
    _need_remote(state)
    sp = resolve_speaker(state, speaker)
    doomed = [c for c in sp.clone_candidates if candidate is None or c.get("id") == candidate]
    if not doomed:
        raise CommandError("没有可删的候选")
    # 还被某一句单独指定着的候选不能删：删了那一句的配音就再也合成不出来
    in_use = {x.tts_voice: i + 1 for i, x in enumerate(state.segments) if x.tts_voice}
    pinned = [(c, in_use[c["voice_id"]]) for c in doomed if c["voice_id"] in in_use]
    if pinned:
        if candidate is not None:
            raise CommandError(f"第 {pinned[0][1]} 句单独用着这个音色；先给那一句清除单句音色（set_dub_style voice=\"\"）")
        doomed = [c for c in doomed if c["voice_id"] not in in_use]
        if not doomed:
            raise CommandError("剩下的候选都还有句子单独用着，没有可删的")
    gone = [c for c in doomed if await vc.delete_voice(c["voice_id"])]
    ids = {c.get("id") for c in gone}

    def apply(s):
        t = s.speaker(sp.id)
        t.clone_candidates = [c for c in t.clone_candidates if c.get("id") not in ids]
    ctx.change(apply, f"清理 {sp.name} 的 {len(gone)} 个候选音色", undoable=False)
    return {"speaker": sp.id, "deleted": len(gone), "failed": len(doomed) - len(gone)}


@command(
    "preview_voice", "试听某个说话人当前的音色（合成一句固定的话）。结果在界面上可以播放。",
    {"type": "object", "properties": {"speaker": {"type": "string"}}, "required": ["speaker"]},
    group="job",
)
async def preview_voice(ctx: Ctx, speaker: str):
    from app.services import voice_clone_service as vc
    state = ctx.state
    _need_remote(state)
    sp = resolve_speaker(state, speaker)
    voice = status.clone_voice_id(state, sp) or sp.voice
    if not voice:
        raise CommandError(f"{sp.name} 还没有音色")
    model = vc.clone_target_model()
    await vc.preview_voice(ctx.video_id, voice, model, state.target_language, sp.id)
    ctx.ui("play_voice_preview", speaker=sp.id,
           url=f"/api/projects/{ctx.video_id}/voice-preview/{sp.id}?t={int(time.time())}")
    return {"speaker": sp.id, "voice": voice, "played_in_ui": True}
