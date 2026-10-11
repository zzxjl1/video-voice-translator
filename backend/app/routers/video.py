"""
Video processing API routes.
All endpoints are prefixed with /api/videos.
"""
import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app import config
from app.deps import enforce_separator_token, validate_video_id
from app.http_range import iter_file_range, parse_range
from app.services import (
    run_registry,
    speech_timing,
    token_service,
    voice_clone_service,
)
from app.models import (
    VideoState,
    get_or_create_state,
    get_state,
    drop_enhanced_line,
    get_video_dir,
    patch_asr_text,
    save_state,
    save_translations,
    set_segment_flag,
    update_state_fields,
    video_dir_path,
)
from app.schemas import (
    ExportRequest,
    MuteRequest,
    HideRequest,
    ExportResponse,
    SegmentOut,
    SpeakerOut,
    SegmentTextRequest,
    TranslateRequest,
    TranslateResponse,
    TranslationResultItem,
    TTSRequest,
    TTSResponse,
    UploadResponse,
    VideoStatusResponse,
    ProcessRequest,
    SpeakerVoiceRequest,
)
from app.services import (
    export_service,
    llm_service,
    subtitle_service,
    tts_service,
)
from app.services import pipeline_service
from app.services import mix_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/videos", tags=["videos"], dependencies=[Depends(validate_video_id)])

# Which voices a language has is a property of the TTS MODEL, not of any video,
# and the landing page needs that answer before anything is uploaded. Hence a
# router of its own rather than another `/{video_id}/...` route — same split as
# `subtitles.py`, which carries a `video_router` alongside its own.
tts_router = APIRouter(prefix="/tts", tags=["tts"])

# ASR languages are a model property for the same reason TTS voices are, and the
# landing page shows them next to the upload — they constrain what can be
# uploaded at all, which is a question about the input, not a preference.
asr_router = APIRouter(prefix="/asr", tags=["asr"])


@asr_router.get("/languages")
async def asr_languages():
    """
    The languages and Chinese dialects the transcription model can hear.

    Shown beside the upload, because it answers "can I even use this video?"
    rather than anything about the settings. Deliberately separate from the TTS
    voice list: the two cover different sets (30 languages heard vs 10 spoken by
    a built-in voice) and conflating them is how a dub ends up mispronounced.
    """
    return {
        "languages": list(config.ASR_SOURCE_LANGUAGES),
        "dialects": list(config.ASR_CHINESE_DIALECTS),
    }


@tts_router.get("/voices")
async def voices_for_language(language: Optional[str] = None):
    """
    The built-in voices that can speak `language`, for a caller with no video.

    Same answer as `GET /videos/{id}/voices`, minus the per-speaker assignment.
    The two exist separately because the question is asked at two different
    times: "what will this sound like?" before an upload, and "who is on which
    voice?" after a run.

    `needs_voice_cloning` means no built-in voice speaks this language at all.
    The list is empty in that case rather than filled with something close: the
    fallback pool produces audio, but mispronounced, and the docs describe that
    outcome as a quality problem rather than an error — so nothing would report
    it.
    """
    supported = config.system_voices_support(language)
    return {
        "language": language,
        "voices": (
            [
                {"id": v, "label": config.voice_label(v)}
                for v in config.voices_for_language(language)
            ]
            if supported
            else []
        ),
        "needs_voice_cloning": not supported,
    }


def _reviewed_text(video_id: str) -> dict[str, str]:
    """
    读回 omni 的审听版（纠错 + 语气标签）—— 管线跑多模态增强时落盘的那份。

    为什么这个端点也要读：Reprocess（重译 + 重做全部音频）与菜单里的单行重译都
    直接走 `/translate`，而前端手上只有 ASR 原文。不把审听版读回来的话，每次重译
    都会**丢掉 omni 的纠错与标签**：译文按未纠错的原文重翻，侧栏的标签色块随之
    消失。这份文件以前是"只写不读"的诊断产物（pipeline_service 写、全仓无人读）。

    读不出来（没有/坏掉）就返回空 —— 退回按原文翻译，与旧行为一致。
    """
    path = os.path.join(video_dir_path(video_id), "enhanced_transcript.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(
            f"[{video_id}] enhanced_transcript.json unreadable ({e}); "
            f"re-translating from the raw transcript"
        )
        return {}
    lines = data.get("lines") if isinstance(data, dict) else None
    if not isinstance(lines, dict):
        return {}
    return {str(k): str(v) for k, v in lines.items()}


def _reject_if_running(video_id: str) -> None:
    """
    管线正在跑这个工程时，别的写端点一律 409（P1 #10）。

    为什么必须挡：管线在开跑时读入一份 `state` 副本，整段都由它写回磁盘
    （`save_state` 调了很多次）。此时如果有别的端点（改字幕样式、手动重合成、
    导出计划…）也读改写同一份 state，谁的写入在后谁就赢 —— 管线那份旧副本会把
    中间的修改整体抹掉，典型 lost update。与 `/process` 的重复请求守卫一致。
    """
    run = run_registry.snapshot(video_id)
    if run.get("active"):
        raise HTTPException(
            status_code=409,
            detail="this video is being processed right now; retry when it finishes",
        )


_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _safe_name(value: str, what: str) -> str:
    """
    A path parameter that is about to become a file or directory name.

    `segment_id` / `speaker_id` arrive in the URL and used to be joined straight
    into `data/<id>/tts/<segment_id>.mp3` and `voice_clone/<speaker_id>/…`. A
    framework-decoded `..` or a `%2f` has no business in either.
    """
    if not _SAFE_NAME_RE.match(value or ""):
        raise HTTPException(status_code=400, detail=f"malformed {what}")
    return value


def _require_state(video_id: str):
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")
    return state


def _client_address(request: Request) -> str:
    """
    The caller's address for rate limiting.

    Behind nginx every request's socket peer is 127.0.0.1, which put all users in
    ONE rate-limit bucket. nginx sets `X-Real-IP` (see nginx.conf); it is trusted
    ONLY when the peer is the local proxy, so a direct client cannot pick its own
    bucket by sending the header.
    """
    peer = request.client.host if request.client else "unknown"
    if peer in ("127.0.0.1", "::1"):
        forwarded = (request.headers.get("x-real-ip") or "").strip()
        if forwarded:
            return forwarded
    return peer


def _compute_md5(file_path: str) -> str:
    """Compute MD5 hash of a file."""
    md5 = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            md5.update(chunk)
    return md5.hexdigest()


@router.post("/upload", response_model=UploadResponse)
def upload_video(file: UploadFile = File(...)):
    """
    Upload a video file. Returns video_id (MD5 hash) and whether it already exists.

    A plain `def` on purpose: the body is blocking file I/O (a copy of up to 2 GB
    plus an MD5 pass). As `async def` it ran on the event loop and froze every
    other request — including the SSE progress streams of running pipelines —
    for the length of the upload. FastAPI runs a sync endpoint in its thread pool.
    """
    logger.info(f"Received upload request for file: {file.filename}")
    
    # 类型/大小校验（P2 #17）：以前既不限大小也不查类型 —— 任意客户端可以写满
    # 磁盘，或者塞个非视频文件让后面的 ffmpeg 去啃。扩展名先挡一道；拷贝过程中
    # 累计字节数，超限立刻中止并删掉临时文件（不能等写完再判）。
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in config.UPLOAD_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported file type: {ext or '(none)'}",
        )

    # We need to save to a temp location first to compute MD5
    temp_dir = os.path.join(config.DATA_DIR, "temp")
    os.makedirs(temp_dir, exist_ok=True)
    # 临时文件名不带上传的文件名：对方给的名字可以直接进路径，没必要冒这个险。
    temp_path = os.path.join(temp_dir, f"{uuid.uuid4()}{ext}")

    try:
        with open(temp_path, "wb") as f:
            total = 0
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > config.UPLOAD_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"file too large (>{config.UPLOAD_MAX_BYTES // (1024 * 1024)} MB)"
                        ),
                    )
                f.write(chunk)

        video_id = _compute_md5(temp_path)
        logger.info(f"Computed MD5 for {file.filename}: {video_id}")

        # Final destination
        video_dir = get_video_dir(video_id)
        final_video_name = f"original_video{ext}"
        final_path = os.path.join(video_dir, final_video_name)

        already_exists = os.path.exists(final_path)

        if not already_exists:
            os.rename(temp_path, final_path)
            logger.info(f"Saved new video file: {final_path}")
        else:
            os.remove(temp_path)  # Already uploaded before
            logger.info(f"Video already exists, skipping save: {final_path}")

        # Create/retrieve state
        state = get_or_create_state(video_id, file.filename, final_path)
        save_state(state)

        return UploadResponse(
            video_id=video_id,
            filename=file.filename,
            exists=already_exists,
            status=state.status.value,
        )
    except HTTPException:
        # 校验失败（类型/大小）：按原状态码返回，不要被下面那句吞成 500
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        logger.error(f"Upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{video_id}/status", response_model=VideoStatusResponse)
def get_video_status(video_id: str, mix_version: Optional[str] = None):
    """
    Get the current processing status of a video with detailed progress info.

    Sync (thread pool) because it is polled every couple of seconds and does real
    work: several JSON reads plus the mix plan, which may probe audio durations.
    """
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    # 混音计划：与导出用**同一个生成器**（现场生成，永远新鲜）。带 mix_version
    # 查询参数时做版本协商：命中就只回版本号，不重复下发整个 plan。
    mix_payload: Optional[dict] = None
    try:
        mix_plan = mix_service.build_mix_plan(state)
        mix_payload = (
            {"version": mix_plan["version"], "plan": None}
            if mix_version and mix_version == mix_plan["version"]
            else {"version": mix_plan["version"], "plan": mix_plan}
        )
    except Exception as e:
        # 没有可混的内容（还没合成/垫底判定不了）→ 界面本来就没有配音可播，
        # 不给 plan 即可；导出仍会在自己的入口响亮失败。
        logger.info(f"[{video_id}] no mix plan for status: {e}")

    video_dir = video_dir_path(video_id)
    # Resolved, not assumed: a styled export lands in an MKV, so checking only
    # the MP4 path would report "no export" for a file that exists.
    export_found = export_service.find_export(video_id)
    has_export = export_found is not None

    return VideoStatusResponse(
        video_id=state.video_id,
        filename=state.filename,
        status=state.status.value,
        error=state.error_message,
        segments=[
            SegmentOut(
                id=seg.id,
                speaker_id=seg.speaker_id,
                speaker_label=seg.speaker_label,
                start_time=seg.start_time,
                end_time=seg.end_time,
                text=seg.text,
                translated_text=seg.translated_text,
                muted=seg.muted,
                hidden=seg.hidden,
                # 静音行不给 audio_url：界面上播一条已经静音的音轨会让人
                # 以为静音没生效。
                audio_url=(
                    f"/api/videos/{video_id}/tts/{seg.id}"
                    if seg.audio_path and not seg.muted else None
                ),
            )
            for seg in state.segments
        ],
        speakers=[
            SpeakerOut(id=s.id, name=s.name)
            for s in state.speakers
        ],
        has_vocals=os.path.exists(os.path.join(video_dir, "vocals.wav")),
        has_background=os.path.exists(os.path.join(video_dir, "background.wav")),
        has_asr=os.path.exists(os.path.join(video_dir, "asr_result.json")),
        has_translation=os.path.exists(os.path.join(video_dir, "translation_result.json")),
        has_tts=os.path.exists(os.path.join(video_dir, "tts_results.json")),
        has_export=has_export,
        export_url=f"/api/videos/{video_id}/export/download" if has_export else None,
        export_available=export_service.is_available(),
        # 工作流状态：这个工程此刻有没有在跑、跑到哪一步、能不能停。
        # 页面重载 / 换标签页后靠它判断"是接管还是新起一单"。
        run=run_registry.snapshot(video_id),
        mix=mix_payload,
        cancelled_step=state.cancelled_step,
        # 缺口清单：哪些行没有译文 / 没有配音。界面上要能直接指出来，
        # 否则用户只能从成片里"少了一句声音"倒推。
        failed_translation_ids=getattr(state, "failed_translation_ids", None) or [],
        failed_audio_ids=getattr(state, "failed_audio_ids", None) or [],
        # Which container the finished file is in, so the UI can label the
        # download correctly (and re-initialise its export panel).
        export_container=export_found[1] if export_found else None,
        subtitle_style=subtitle_service.SubtitleStyle.from_dict(
            getattr(state, "subtitle_style", None)
        ).to_dict(),
        subtitle_export=subtitle_service.ExportPlan.from_dict(
            getattr(state, "subtitle_export", None)
        ).to_dict(),
        subtitle_capabilities=subtitle_service.capabilities(),
        # The project's own mode once a pipeline has run — that is the choice the
        # user made and the one a recovery must adopt. Before any run the state
        # field is empty, and the server default is the honest answer, so a
        # project that has not started cannot read as one run with separation
        # deliberately off.
        separation_mode=state.separation_mode or config.SEPARATION_MODE,
        separation_backends=config.separation_capabilities(),
        enable_voice_clone=state.enable_voice_clone,
        target_language=state.target_language,
        accent=state.accent,
        custom_prompt=state.custom_prompt,
        mm_enhance=state.mm_enhance,
        clone_smart_pick=state.clone_smart_pick,
    )


@router.post("/{video_id}/reset")
def reset_video(video_id: str):
    """
    Reset a video's processing data so it can be re-processed from scratch.
    Deletes all intermediate files but keeps the original video.
    """
    # 运行中不能清：管线正往这个目录里写，边写边删会得到半套文件。
    _reject_if_running(video_id)

    # 只读路径，不用 get_video_dir：它会 mkdir，于是对不存在的工程调 reset 也会
    # 凭空建出一个目录。
    video_dir = video_dir_path(video_id)
    if not os.path.isdir(video_dir):
        raise HTTPException(status_code=404, detail="Video not found")

    # Find original video file (keep it)
    original_video = None
    for f in os.listdir(video_dir):
        if f.startswith("original_video"):
            original_video = f
            break

    if not original_video:
        raise HTTPException(status_code=404, detail="Original video file not found")

    # Remember the name the user uploaded: the fresh state below must keep it,
    # or the project list would show the on-disk "original_video.mp4" afterwards.
    previous = get_state(video_id)
    display_name = (previous.filename if previous and previous.filename else original_video)

    # Delete everything except original video
    for item in os.listdir(video_dir):
        if item == original_video:
            continue
        item_path = os.path.join(video_dir, item)
        if os.path.isdir(item_path):
            shutil.rmtree(item_path)
        else:
            os.remove(item_path)

    # The directory is gone, so the in-process caches that mirror it must go too —
    # otherwise they would keep serving the old speakers' voices / cloned voices
    # and write them back to disk on the next assignment.
    tts_service.forget_voice_map(video_id)
    voice_clone_service.forget_cloned_voices(video_id)

    # Re-create clean state
    original_path = os.path.join(video_dir, original_video)
    state = VideoState(
        video_id=video_id,
        filename=display_name,
        file_path=original_path,
    )
    save_state(state)

    logger.info(f"[{video_id}] Reset complete. All intermediate data removed.")
    return {"video_id": video_id, "status": "reset"}


@router.delete("/{video_id}")
async def delete_video(video_id: str):
    """
    永久删除一个工程：原始视频、音频、译文、配音、导出，全部删除，不保留。

    与 `/reset` 的区别：reset 保留原始视频以便重跑；这个是把工程整个去掉。
    所以"从最近列表移除"这种前端本地操作不算删除 —— 服务器上的文件必须一起走。

    运行中拒绝删除（409）：管线正在往这个目录里写，边写边删只会得到半套文件。
    前端会先取消运行再调用，这里做最后一道。
    """
    # 用"有没有 info.json"判断工程是否存在，而不是目录是否存在：
    # `get_video_dir` 会按需 mkdir，于是"已删除的工程"会被它重新建成一个空
    # 目录，删除一个不存在的工程就会静默成功（200）而不是 404。
    if get_state(video_id) is None:
        raise HTTPException(status_code=404, detail="Video not found")

    video_dir = get_video_dir(video_id)
    if run_registry.get(video_id) is not None:
        raise HTTPException(
            status_code=409,
            detail="这个工程正在处理中，请先取消运行再删除。",
        )

    try:
        shutil.rmtree(video_dir)
    except OSError as e:
        logger.error(f"[{video_id}] Failed to delete project dir: {e}")
        raise HTTPException(status_code=500, detail=f"删除失败：{e}")

    # 内存里同样不能留痕迹
    tts_service.forget_voice_map(video_id)
    voice_clone_service.forget_cloned_voices(video_id)
    logger.info(f"[{video_id}] Project deleted (all files removed)")

    return {"deleted": True, "video_id": video_id}


@router.post("/{video_id}/translate", response_model=TranslateResponse)
async def translate_video(video_id: str, req: TranslateRequest):
    """
    Translate the given transcript segments with the configured LLM.

    Used for one-line re-translation from the editor: `req.segments` is usually a
    SINGLE line. Three things follow from that, and each was a bug before:

    * The result is merged into the project's translations and the WHOLE set is
      written back (`save_translations`). Writing only this request's lines
      replaced `translation_result.json` with one entry, so after a refresh every
      other line had lost its translation.
    * The project status is left alone. Flipping it to TRANSLATING/TRANSLATED made
      a finished project read as "unfinished" on the next open.
    * `custom_prompt` here is a ONE-LINE instruction ("只对这句话生效"). It is
      passed to the model for this call only; persisting it as the project's
      prompt made every later Reprocess re-translate EVERYTHING with it.
    """
    _reject_if_running(video_id)
    logger.info(f"Translation requested for video_id: {video_id}, Target: {req.target_language}")
    state = _require_state(video_id)

    try:
        # When the caller does not send end_time we fall back to the persisted
        # segment, so the length budget is still available for one-line retranslation.
        stored = {seg.id: seg for seg in state.segments}
        unknown = [seg.id for seg in req.segments if seg.id not in stored]
        if unknown:
            raise HTTPException(status_code=404, detail=f"Segment not found: {unknown[0]}")

        # 审听版作为翻译基底（omni 的纠错 + 语气标签）。两个前提，缺一个都不该套用：
        #
        # ① **当前**开着多模态增强。关掉之后磁盘上那份 enhanced_transcript.json 是
        #    上一次开启时留下的；照用会让"关掉 omni"之后的行级重译仍带着旧纠错与旧
        #    标签 —— 用户明明在界面上关了它。所以这里看 state.mm_enhance，不看文件在不在。
        # ② 请求里的文本与该段存着的 ASR 原文逐字相同：侧栏的 Original 栏是可编辑的，
        #    用户手改过就以他的文本为准，不能被旧的审听版顶掉。
        reviewed: dict[str, str] = _reviewed_text(video_id) if state.mm_enhance else {}
        text_overrides: dict[str, str] = {}
        for seg in req.segments:
            base = reviewed.get(seg.id)
            if not base:
                continue
            original = stored[seg.id].text or ""
            if seg.text.strip() != original.strip():
                continue          # 用户改过 → 尊重用户的
            if base.strip() and base.strip() != seg.text.strip():
                text_overrides[seg.id] = base
        if text_overrides:
            logger.info(
                f"[{video_id}] /translate: using the reviewed (omni) text as the "
                f"translation base for {len(text_overrides)}/{len(req.segments)} line(s)"
            )

        context = [
            {
                "id": seg.id,
                "text": seg.text,
                "speaker_id": seg.speaker_id,
                "start_time": seg.start_time,
                "end_time": seg.end_time or stored[seg.id].end_time or 0.0,
            }
            for seg in req.segments
        ]

        # Clamp here, once: the prompt injector downstream trusts this to be short.
        custom_prompt = (req.custom_prompt or "").strip()[:500]
        length_hint = req.length_hint if req.length_hint in ("longer", "shorter") else None
        results, _failed = await llm_service.translate_script(
            video_id, context, req.target_language,
            custom_prompt=custom_prompt, length_hint=length_hint,
            text_overrides=text_overrides or None,
        )

        # Merge into the project and persist the WHOLE translation set.
        translations = []
        result_map = {r["id"]: r["translatedText"] for r in results}
        for seg in state.segments:
            new_text = result_map.get(seg.id)
            if not new_text:
                continue
            if new_text != seg.translated_text:
                # The old take reads the OLD text; leaving it would export a dub
                # that no longer matches the subtitle.
                tts_service.discard_segment_audio(video_id, seg.id)
                seg.audio_path = None
                seg.actual_duration = None
            seg.translated_text = new_text
            translations.append(
                TranslationResultItem(id=seg.id, translated_text=new_text)
            )
        save_translations(video_id, state.segments)
        logger.info(f"[{video_id}] Translated {len(translations)} item(s) on request")

        return TranslateResponse(video_id=video_id, translations=translations)
    except HTTPException:
        raise
    except Exception as e:
        # Not recorded on the project: a failed single-line request is not a failed
        # project, and `state.status = ERROR` made the whole thing look broken.
        logger.error(f"[{video_id}] Translation error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/{video_id}/segments/{segment_id}/text")
def save_segment_text(video_id: str, segment_id: str, req: SegmentTextRequest):
    """
    Persist text the user edited in the editor (original and/or translation).

    These edits used to trigger a re-translation or re-synthesis and nothing else:
    nothing wrote the new text to disk. The subtitles (built from the server's
    state) kept the old words while the dub spoke the new ones, and a refresh
    threw the edit away.

    A changed line also loses its synthesized audio: it reads the old text.
    """
    _reject_if_running(video_id)
    _safe_name(segment_id, "segment id")
    state = _require_state(video_id)
    seg = next((s for s in state.segments if s.id == segment_id), None)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")

    changed = False
    if req.text is not None and req.text != seg.text:
        if not patch_asr_text(video_id, segment_id, req.text):
            raise HTTPException(status_code=409, detail="no transcript on disk to update")
        # The cached omni review of this line describes the OLD words.
        drop_enhanced_line(video_id, segment_id)
        seg.text = req.text
        changed = True
    if req.translated_text is not None and req.translated_text != seg.translated_text:
        seg.translated_text = req.translated_text
        save_translations(video_id, state.segments)
        changed = True

    if changed:
        tts_service.discard_segment_audio(video_id, segment_id)
    return {"id": segment_id, "saved": changed}


@router.post("/{video_id}/tts", response_model=TTSResponse)
async def synthesize_speech(video_id: str, req: TTSRequest):
    """Synthesize speech for ONE text segment (editor "regenerate" / "refit")."""
    _reject_if_running(video_id)
    logger.info(f"TTS requested for video_id: {video_id}")
    state = _require_state(video_id)

    seg_obj = next((s for s in state.segments if s.id == req.segment_id), None)
    if seg_obj is None:
        raise HTTPException(status_code=404, detail="Segment not found")
    if not req.text.strip():
        raise HTTPException(status_code=400, detail="text is empty")

    # The request's language/accent win; the project's own are the fallback, so a
    # client that omits them does not silently drop this one line to Mandarin or
    # to the default voice pool.
    language = req.target_language or state.target_language or None
    accent = req.accent if req.accent is not None else state.accent

    try:
        # Same precedence the batch pipeline applies: explicit > cloned > the
        # speaker's assigned system voice. The last step is what keeps a
        # re-generated line in the SAME voice as its neighbours — without it the
        # line fell back to the global default voice instead.
        voice = req.voice
        if not voice and state.enable_voice_clone:
            voice = voice_clone_service.get_cloned_voice(video_id, seg_obj.speaker_id)
            if voice:
                logger.info(
                    f"[{video_id}] Using cloned voice {voice} for segment "
                    f"{req.segment_id} (speaker {seg_obj.speaker_id})"
                )
        if not voice:
            voice = tts_service.assign_voice_for_speaker(video_id, seg_obj.speaker_id, language)

        # When the caller tells us which slot this line has to fill, aim the
        # synthesis at it; the measurement-based correction inside
        # synthesize_speech works without a language, so a missing one degrades.
        planned_rate = (
            speech_timing.plan_speech_rate(req.text, language, req.target_duration)
            if (req.target_duration and req.target_duration > 0 and language)
            else None
        )
        # `synthesize_speech` registers the path and measured duration in
        # tts_results.json (write_registry defaults to True), which is what
        # `load_state` reads back. The state is NOT re-saved here: `save_state`
        # stores no segments, so doing it only risked clobbering fields written
        # elsewhere in the meantime.
        await tts_service.synthesize_speech(
            video_id,
            req.segment_id,
            req.text,
            voice,
            speech_rate=planned_rate,
            target_duration=req.target_duration,
            instruction=config.tts_instruction(language, accent),
        )
        logger.info(f"[{video_id}] TTS synthesis complete (Segment: {req.segment_id})")

        return TTSResponse(
            audio_url=f"/api/videos/{video_id}/tts/{req.segment_id}",
            content_type="audio/mp3",
        )
    except Exception as e:
        logger.error(f"[{video_id}] TTS error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{video_id}/video")
def serve_video(video_id: str, request: Request):
    """Serve the original video file with Range request support for seeking."""
    state = _require_state(video_id)
    if not state.file_path:
        raise HTTPException(status_code=404, detail="Video not found")

    if not os.path.exists(state.file_path):
        raise HTTPException(status_code=404, detail="Video file not found on disk")

    file_path = state.file_path
    file_size = os.path.getsize(file_path)
    content_type = mimetypes.guess_type(file_path)[0] or "video/mp4"

    byte_range = parse_range(request.headers.get("range", ""), file_size)
    if byte_range is None:
        return FileResponse(
            file_path,
            media_type=content_type,
            headers={"Accept-Ranges": "bytes"},
        )

    start, end = byte_range
    content_length = end - start + 1

    return StreamingResponse(
        iter_file_range(file_path, start, content_length),
        status_code=206,
        media_type=content_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(content_length),
        },
    )


@router.get("/{video_id}/audio")
def serve_audio(video_id: str):
    """
    Serve the extracted audio file.
    Used internally for Ali ASR to access the audio file via URL.
    """

    logger.info(f"Audio serve requested for video_id: {video_id}")
    state = get_state(video_id)
    if not state or not state.audio_path:
        logger.warning(f"Audio serve failed: No state or audio path for {video_id}")
        raise HTTPException(status_code=404, detail="Audio not found")

    if not os.path.exists(state.audio_path):
        logger.error(f"Audio serve failed: File not found on disk at {state.audio_path}")
        raise HTTPException(status_code=404, detail="Audio file not found on disk")

    return FileResponse(state.audio_path, media_type="audio/wav")


@router.get(
    "/{video_id}/audio/stereo",
    dependencies=[Depends(enforce_separator_token)],
)
def serve_separation_source(video_id: str):
    """
    STEREO 44.1 kHz audio, for in-browser vocal separation.

    Deliberately NOT the file `/{video_id}/audio` serves. That one is the
    16 kHz MONO track ASR uses, and MDX-Net needs both channels across the full
    band: separating the ASR file would band-limit the output to 8 kHz and throw
    away the stereo image. The result is audibly muffled, and the cause is very
    hard to trace back from the symptom.

    Exists because browser separation cannot always rely on the in-memory File
    (after a page reload there is none, yet the pipeline can still be started
    from the URL). Extracted on demand from the original upload and cached
    beside it; token gated because the extraction costs real CPU.
    """

    state = _require_state(video_id)

    video_dir = video_dir_path(video_id)
    stereo_path = os.path.join(video_dir, "separation_source.wav")

    # Plain `def` (thread pool): the extraction below is a long blocking ffmpeg
    # run, which as `async def` froze the event loop for its whole duration.
    if not os.path.exists(stereo_path):
        if not state.file_path or not os.path.exists(state.file_path):
            raise HTTPException(
                status_code=404,
                detail=(
                    "The original video is no longer on disk, so a separation "
                    "source cannot be produced. Re-upload it to separate "
                    "vocals in the browser."
                ),
            )

        # Written to a temp name and renamed: a half-written WAV left at the final
        # path would be served as-is by every later request.
        partial_path = f"{stereo_path}.part.wav"
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-i", state.file_path,
                "-vn", "-acodec", "pcm_s16le",
                "-ar", "44100", "-ac", "2",
                partial_path,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            os.replace(partial_path, stereo_path)
        elif os.path.exists(partial_path):
            os.remove(partial_path)
        if result.returncode != 0:
            logger.error(
                f"[{video_id}] Stereo extraction for separation failed: "
                f"{result.stderr[-300:]}"
            )
            raise HTTPException(
                status_code=500, detail="Could not extract stereo audio"
            )
        logger.info(f"[{video_id}] Extracted separation source -> {stereo_path}")

    return FileResponse(stereo_path, media_type="audio/wav")


@router.get("/{video_id}/events")
async def stream_events(video_id: str):
    """
    订阅这个工程**正在跑**的那条管线的事件（SSE），并先回放有界历史（P3 #9）。

    语义仍然是"一条运行一个消费者"：接管时替换队列，旧连接的读取端会被唤醒并
    结束（见 `PipelineRun.subscribe`），新订阅者先拿到最近 `HISTORY_LIMIT` 条事件
    再继续接收。
    """
    run = run_registry.get(video_id)
    if run is None:
        # 没有活跃运行：明确说清楚，而不是挂一条永远不发事件的流。
        raise HTTPException(status_code=404, detail="No active run for this video")

    return StreamingResponse(
        _sse_stream(run, run.subscribe()),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


# 反向代理不要缓冲、不要缓存这条流；nginx 看 X-Accel-Buffering，别的代理看 no-cache。
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}

# 管线可能在一个步骤里静默几分钟（等 ASR/omni）。每隔这么久发一行 SSE 注释：
# 既让中间设备不因空闲掐断连接，也让客户端断开时服务端能尽快发现并清理。
_SSE_HEARTBEAT_S = 15.0


async def _sse_stream(run, queue: "asyncio.Queue"):
    """
    One consumer of a run's events, as an SSE byte stream.

    Ends on the `None` sentinel. Always detaches in `finally` — and only if the
    queue is still THIS consumer's: a reconnect replaces it, and the old reader
    must not clear the new one's.
    """
    try:
        while True:
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=_SSE_HEARTBEAT_S)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if msg is None:
                break
            yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
    finally:
        run.unsubscribe(queue)


@router.post("/{video_id}/cancel")
async def cancel_processing(video_id: str):
    """
    Ask the live pipeline for this video to stop.

    Cooperative: the current provider request (ASR / omni / one TTS line) is
    allowed to finish, then the pipeline stops at that step boundary and the
    project is left CANCELLED — not "paused", nothing continues on its own.
    Everything already produced stays on disk, so a later run resumes from the
    last finished phase instead of redoing it.
    """
    run = run_registry.get(video_id)
    if run is None:
        return {"cancelling": False, "reason": "no pipeline is running for this video"}

    run.request_cancel()
    logger.info(f"[{video_id}] Cancellation requested (at step {run.step})")
    return {
        "cancelling": True,
        "step": run.step,
        "note": "the current provider request is allowed to finish",
    }


# 后台任务的强引用。事件循环只对任务保留**弱**引用：没人持有的 `create_task(...)`
# 可能在运行中途被垃圾回收，管线就这样无声无息地停了。
_background_tasks: set["asyncio.Task"] = set()


@router.post("/{video_id}/process")
async def process_video(video_id: str, req: ProcessRequest):
    """
    Run the full processing pipeline (separation → ASR → translation → TTS)
    with SSE progress streaming. Frontend only needs to call this once after upload.

    If a run for this video is already live, the request is refused with 409
    (see the registry note above) — it never joins that run.
    """
    logger.info(f"Full pipeline requested for video_id: {video_id}, lang: {req.target_language}")
    _require_state(video_id)

    if run_registry.get(video_id) is not None:
        live = run_registry.get(video_id)
        # 拒绝，而不是接管：一条管线的设置属于发起它的人（见 run_registry）。
        raise HTTPException(
            status_code=409,
            detail=(
                f"这个工程正在处理中（当前：{live.step or '启动中'}），"
                "请先停止，或等它结束再发起。"
            ),
        )

    run = run_registry.start(video_id)
    # 队列必须先于任务创建：drive 一开跑就会 emit，队列还没建的话那几个事件
    # 就被静默丢掉了（客户端会以为管线卡在开头）。
    queue = run.subscribe()

    async def drive() -> None:
        try:
            await pipeline_service.run_pipeline(
                video_id=video_id,
                target_language=req.target_language,
                server_url_base=config.SERVER_URL_BASE,
                emit=run.emit,
                separation_mode=req.separation_mode,
                enable_voice_clone=req.enable_voice_clone,
                accent=req.accent,
                custom_prompt=(req.custom_prompt or "").strip()[:500],
                mm_enhance=req.mm_enhance,
                clone_smart_pick=req.clone_smart_pick,
                # 协作式取消：管线在每个步骤边界（以及每个 TTS/克隆请求之前）
                # 询问一次，允许当前正在进行的那个请求跑完。
                should_cancel=lambda: run.cancel_event.is_set(),
                # "从翻译重做"（Reprocess）：见 schemas.ProcessRequest.start_from。
                start_phase=req.start_from if req.start_from == "translation" else None,
            )
        except Exception as e:
            logger.error(f"[{video_id}] Pipeline error: {e}", exc_info=True)
            await run.emit({"error": str(e)})
        finally:
            # 先注销，再发结束标记：标记发出时 /status 必须已经是"没在跑"。
            run_registry.finish(video_id)
            run.close()

    task = asyncio.create_task(drive())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return StreamingResponse(
        _sse_stream(run, queue),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/{video_id}/audio/background")
def serve_background_audio(video_id: str):
    """Serve the separated background (instrumental) audio."""

    video_dir = video_dir_path(video_id)
    bg_path = os.path.join(video_dir, "background.wav")

    if not os.path.exists(bg_path):
        raise HTTPException(status_code=404, detail="Background audio not found")

    return FileResponse(bg_path, media_type="audio/wav")


@router.get("/{video_id}/tts/{segment_id}")
def serve_segment_audio(video_id: str, segment_id: str):
    """Serve synthesized MP3 for a specific segment."""
    _safe_name(segment_id, "segment id")
    video_dir = video_dir_path(video_id)
    audio_path = os.path.join(video_dir, "tts", f"{segment_id}.mp3")
    
    if not os.path.exists(audio_path):
        raise HTTPException(status_code=404, detail="Segment audio not found")
        
    return FileResponse(audio_path, media_type="audio/mp3")


@router.get("/{video_id}/voice-sample/{speaker_id}")
def serve_voice_sample(video_id: str, speaker_id: str):
    """Serve extracted voice sample WAV for DashScope voice enrollment."""
    _safe_name(speaker_id, "speaker id")

    video_dir = video_dir_path(video_id)
    sample_path = os.path.join(video_dir, "voice_clone", speaker_id, "sample.wav")

    if not os.path.exists(sample_path):
        raise HTTPException(status_code=404, detail="Voice sample not found")

    return FileResponse(sample_path, media_type="audio/wav")


@router.get("/{video_id}/voice-clone/status")
async def get_voice_clone_status(video_id: str):
    """
    Cloned voices for a video, keyed by speaker: `{speaker_id: voice_id}`.

    Only voices enrolled against the currently configured TTS model are
    reported. Cloned voices cannot be used across models, so returning a stale
    one would make the UI show a speaker as "ready" while synthesis would
    actually fail with `InvalidParameter`.
    """
    video_dir = video_dir_path(video_id)
    current_model = voice_clone_service.clone_target_model()
    clone_map = voice_clone_service.load_clone_map(video_dir)

    usable = {
        speaker_id: entry["voice_id"]
        for speaker_id, entry in clone_map.items()
        if entry.get("voice_id") and entry.get("target_model") == current_model
    }

    return {
        "video_id": video_id,
        "cloned_voices": usable,
        "target_model": current_model,
    }


@router.get("/{video_id}/voices")
async def list_voices(video_id: str, language: Optional[str] = None):
    """
    The voices a user can pick for `language`.

    Driven by the TARGET LANGUAGE and nothing else: which language is being
    dubbed decides which voices exist. Voice ids are model- AND language-specific
    — most Mandarin "精品中文" voices reject an English line outright, answering
    `InvalidParameter` / `Engine error [411]` instead of falling back — so the
    filtering rule lives here rather than being duplicated in the client.

    `voices` is EMPTY when no built-in voice speaks this language, with
    `needs_voice_cloning` set. Returning the fallback pool instead would be a
    lie the user only discovers by listening: the four multilingual voices are
    not documented for Spanish, Russian, Thai and several others, and the docs
    describe the result as 「可能发音错误或语音不自然」 — audio, produced badly.

    `assigned` is EMPTY until something has picked voices; a speaker missing
    from it is "automatic", not broken, since `assign_voice_for_speaker` chooses
    on first use.
    """
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    # The caller may ask about another language; default to what the job is set
    # to, which is what `pin_speaker_voice` will validate against.
    language = language or state.target_language
    supported = config.system_voices_support(language)

    return {
        "video_id": video_id,
        "language": language,
        "voices": (
            [
                {"id": v, "label": config.voice_label(v)}
                for v in config.voices_for_language(language)
            ]
            if supported
            else []
        ),
        "assigned": tts_service.get_speaker_voice_map(video_id),
        "needs_voice_cloning": not supported,
    }


def _set_flag(video_id: str, segment_id: str, flag: str, value: bool) -> dict:
    """
    Shared body of mute / hide: validate, persist.

    Allowed while a pipeline runs: the flags live in their own file
    (`segment_flags.json`, read-modify-written under the models lock) that the
    pipeline never writes, so there is nothing to race with. A line muted mid-run
    may still get synthesized by that run; the flag is honoured by every consumer
    afterwards (mix plan, export, status), and the take is kept for unmuting.
    """
    _safe_name(segment_id, "segment id")
    state = _require_state(video_id)
    seg = next((s for s in state.segments if s.id == segment_id), None)
    if not seg:
        raise HTTPException(status_code=404, detail="Segment not found")
    set_segment_flag(video_id, segment_id, flag, value)
    logger.info(f"[{video_id}] Segment {segment_id} {flag}={value}")
    return {"id": segment_id, flag: value}


@router.post("/{video_id}/segments/{segment_id}/mute")
def mute_segment(video_id: str, segment_id: str, req: MuteRequest):
    """
    Mute or unmute ONE line.

    Muted means: no dubbed audio for this line — the export leaves that slice
    of the timeline silent. The flag is the ONLY change: the previously
    synthesized take stays on disk, and every consumer (pipeline TTS, the
    timeline builder, the status endpoint) honours the flag instead. Keeping
    the take means unmuting brings the line back without paying for a
    re-synthesis.
    """
    return _set_flag(video_id, segment_id, "muted", bool(req.muted))


@router.post("/{video_id}/segments/{segment_id}/hide")
def hide_segment(video_id: str, segment_id: str, req: HideRequest):
    """
    Hide or unhide ONE line's subtitle.

    "Hidden" means: this line produces NO cue — not in the live overlay, not in
    the SRT download, not in the embedded tracks (all three go through
    `subtitle_service.build_cues`). Its dubbed AUDIO is untouched, so the line
    is still spoken; muting is the flag that removes the voice.
    """
    return _set_flag(video_id, segment_id, "hidden", bool(req.hidden))


@router.post("/{video_id}/speaker-voice")
async def pin_speaker_voice(video_id: str, req: SpeakerVoiceRequest):
    """
    Pin one speaker to one voice, for when voice cloning is off.

    Without this the user has no say at all: `assign_voice_for_speaker` picks
    randomly from the language's pool, so which voice a speaker ends up with
    changes between runs.

    The pairing is checked against the pool for the CURRENT target language
    before it is stored. Getting it wrong is not cosmetic — the service rejects
    the pair at synthesis time, halfway through a run — so it is refused here
    and the error names the language.
    """
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    if not any(s.id == req.speaker_id for s in (state.speakers or [])):
        raise HTTPException(status_code=404, detail="Speaker not found")

    # An empty voice is "back to automatic", not an error. It has to be
    # supported: the picker offers it, and without it a pinned speaker could
    # never return to the random assignment. Checked BEFORE the language, so a
    # speaker can always be released even if the pool later became unusable.
    if not req.voice:
        tts_service.clear_speaker_voice(video_id, req.speaker_id)
        return {"video_id": video_id, "speaker_id": req.speaker_id, "voice": None}

    target = state.target_language
    if not config.system_voices_support(target):
        # Same rule the listing applies: there is nothing valid to pin. Refusing
        # here keeps the two endpoints from disagreeing — a picker that offers
        # nothing while the API accepts anything is worse than either alone.
        raise HTTPException(
            status_code=400,
            detail=(
                f"No built-in voice can speak {target or 'the selected language'}. "
                "Turn on voice cloning to dub this language."
            ),
        )

    pool = config.voices_for_language(target)
    if req.voice not in pool:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Voice '{req.voice}' cannot speak "
                f"{state.target_language or 'the selected language'}. "
                f"Pick one of: {', '.join(pool)}"
            ),
        )

    tts_service.set_speaker_voice(video_id, req.speaker_id, req.voice)
    return {"video_id": video_id, "speaker_id": req.speaker_id, "voice": req.voice}


@router.post("/{video_id}/voice-clone/{speaker_id}/preview")
async def preview_cloned_voice(video_id: str, speaker_id: str):
    """Generate and serve a short TTS preview using the cloned voice."""
    _safe_name(speaker_id, "speaker id")
    state = _require_state(video_id)

    # 试听文案要用工程自己的目标语言：硬编码 "Chinese" 时，给英语/日语工程试听念的
    # 是中文，用户听到的和片子里的不是一回事。
    target_language = (state.target_language or "").strip() or "Chinese"

    try:
        preview_path = await voice_clone_service.preview_cloned_voice(
            video_id, speaker_id, target_language
        )
    except Exception as e:
        logger.error(f"[{video_id}] Voice preview failed for {speaker_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

    # Outside the `try`: the 404 below used to be raised inside it, caught by the
    # blanket `except Exception`, and re-reported as a 500.
    if not preview_path or not os.path.exists(preview_path):
        raise HTTPException(
            status_code=404, detail="No cloned voice available for this speaker"
        )
    return FileResponse(preview_path, media_type="audio/mp3")


@router.get("/{video_id}/voice-clone/{speaker_id}/preview")
def serve_voice_preview(video_id: str, speaker_id: str):
    """Serve previously generated voice preview audio."""
    _safe_name(speaker_id, "speaker id")
    # video_dir_path, not get_video_dir: this is a GET and must not create folders.
    preview_path = os.path.join(
        video_dir_path(video_id), "voice_clone", speaker_id, "preview.mp3"
    )

    if not os.path.exists(preview_path):
        raise HTTPException(
            status_code=404,
            detail="Voice preview not found. Generate one first via POST.",
        )

    return FileResponse(preview_path, media_type="audio/mp3")


@router.post("/{video_id}/separator-token")
async def issue_separator_token(video_id: str, request: Request):
    """
    Issue a short-lived token authorising the client-compute endpoints for this
    video (`GET /api/models/separator/onnx`, `POST /api/videos/{id}/stems`).

    Requires a video that already exists, and issuance is rate limited per
    client address, so the heavy endpoints cannot be used as an open CDN.

    Note: this is an abuse speed bump, not authentication — there are no user
    accounts in this app.
    """
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    client_key = _client_address(request)
    if not token_service.check_issue_rate(client_key):
        raise HTTPException(
            status_code=429,
            detail="Too many token requests. Please retry later.",
        )

    token, ttl = token_service.issue_token(video_id)
    logger.info(f"[{video_id}] Issued separator token (ttl={ttl}s, client={client_key})")
    return {
        "video_id": video_id,
        "token": token,
        "expires_in": ttl,
        "model_download_url": "/api/models/separator/onnx",
    }


@router.post("/{video_id}/stems", dependencies=[Depends(enforce_separator_token)])
def upload_stems(
    video_id: str,
    vocals: UploadFile = File(...),
    background: UploadFile = File(...),
):
    """
    Accept vocals/background stems produced by the **browser**.

    Separation normally runs client-side (see `app/routers/models.py`): the
    browser downloads the MDX-Net ONNX model, separates the locally decoded
    audio, and pushes the two WAV stems here. The rest of the pipeline then
    works exactly as if the server had done the separation — `vocals.wav` feeds
    ASR and voice cloning, `background.wav` is mixed back in during export.
    """
    _reject_if_running(video_id)
    state = get_state(video_id)
    if not state:
        raise HTTPException(status_code=404, detail="Video not found")

    video_dir = get_video_dir(video_id)

    written = []
    for filename, upload in (("vocals.wav", vocals), ("background.wav", background)):
        dest = os.path.join(video_dir, filename)
        tmp = f"{dest}.part"
        try:
            with open(tmp, "wb") as out:
                shutil.copyfileobj(upload.file, out)

            size = os.path.getsize(tmp)
            if size < 44:  # smaller than a WAV header
                raise ValueError(f"{filename} is too small to be a WAV file ({size} bytes)")

            # Fail fast on non-WAV payloads instead of breaking ffmpeg later.
            with open(tmp, "rb") as f:
                header = f.read(4)
            if header[:4] not in (b"RIFF", b"RF64"):
                raise ValueError(f"{filename} is not a RIFF/WAV file (header={header!r})")

            os.replace(tmp, dest)
            written.append({"file": filename, "bytes": size})
            logger.info(f"[{video_id}] Received stem {filename} ({size / 1048576:.1f} MB)")
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            logger.error(f"[{video_id}] Failed to store stem {filename}: {e}")
            raise HTTPException(status_code=400, detail=f"Invalid stem {filename}: {e}")

    # Separation is done — make sure the pipeline does not try to redo it. The 16 kHz
    # ASR derivative of an earlier vocals.wav is stale now; the pipeline rebuilds it.
    stale = os.path.join(video_dir, "vocals_asr.wav")
    if os.path.exists(stale):
        os.remove(stale)
    state.audio_path = os.path.join(video_dir, "vocals.wav")
    save_state(state)

    return {
        "video_id": video_id,
        "stems": written,
        "background_url": f"/api/videos/{video_id}/audio/background",
    }


@router.post("/{video_id}/export", response_model=ExportResponse)
async def export_video(video_id: str, req: Optional[ExportRequest] = None):
    """
    Mux the dubbed audio (and optional subtitle tracks) into the video.

    Cheap by design: the video stream is copied (`-c:v copy`) and subtitles are
    EMBEDDED, not burned, so even several styled tracks cost one remux instead
    of a re-encode. Burning happens in the browser.

    The body is optional. When present it is merged over the video's saved plan
    — only the fields actually sent are overridden, so a client that flips one
    switch does not reset the rest. The subtitle STYLE always starts from the
    project's own saved style (what the style panel edits).

    `persist=false` exports with the merged plan WITHOUT saving it. The plan is
    handed to the exporter explicitly: previously it was computed and then
    discarded, and the exporter re-read the SAVED plan, so a "no subtitles, don't
    remember this" request (the browser burn-in) still produced whatever the
    project had saved — embedded tracks the burn then drew a second time, or an
    MKV the in-browser burner cannot read.
    """
    _reject_if_running(video_id)
    logger.info(f"Export requested for video_id: {video_id}")

    state = _require_state(video_id)

    if not export_service.is_available():
        raise HTTPException(
            status_code=503, detail="ffmpeg/ffprobe are not available on the server"
        )

    patch = req.subtitles.model_dump(exclude_none=True) if req and req.subtitles else None
    plan = export_service.resolve_plan(state, patch)
    if patch and req.persist:
        # Only the plan's own fields: the style is the project's, kept in
        # `subtitle_style` and edited from the style panel.
        saved = plan.to_dict()
        saved.pop("style", None)
        update_state_fields(video_id, subtitle_export=saved)

    try:
        result = await export_service.export_video(video_id, plan=plan)
        size_mb = os.path.getsize(result.path) / 1024 / 1024
        return ExportResponse(
            video_id=video_id,
            url=f"/api/videos/{video_id}/export/download",
            size_mb=round(size_mb, 1),
            container=result.container,
            filename=f"translated_{video_id[:8]}.{result.container}",
            subtitle_tracks=result.subtitle_tracks,
        )
    except Exception as e:
        logger.error(f"[{video_id}] Export failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{video_id}/export/download")
def download_export(video_id: str):
    """
    Download the exported (dubbed) video.

    The container depends on the subtitle format that was used — Matroska when
    styled subtitles were requested, MP4 otherwise — so the file is resolved
    from disk rather than assumed.
    """

    found = export_service.find_export(video_id)
    if not found:
        raise HTTPException(
            status_code=404, detail="Export not found. Run POST /export first."
        )

    export_path, container = found
    media_type = (
        "video/x-matroska" if container == "mkv" else "video/mp4"
    )
    return FileResponse(
        export_path,
        media_type=media_type,
        filename=f"translated_{video_id[:8]}.{container}",
    )
