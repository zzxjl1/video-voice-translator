"""
项目 API：`/api/projects/...`。

路由层只做三件事：把请求变成命令、把事件流出去、把文件送出去。业务全在
`app/commands/`（动作）、`app/jobs.py`（长任务）、`app/agent.py`（对话）里。

  POST   /projects                         上传（返回 id，不自动开始）
  GET    /projects/{id}                    完整视图（含 rev）
  DELETE /projects/{id}                    删除
  POST   /projects/{id}/commands/{name}    执行一个命令（UI 的所有按钮都走这里）
  GET    /projects/{id}/commands           命令清单（含参数 schema，调试/文档用）
  GET    /projects/{id}/events?since=N     SSE：变更 / 任务 / 进度 / 聊天 / 界面意图
  GET    /projects/{id}/chat               聊天记录
  POST   /projects/{id}/chat               发一条消息（立刻返回，回复走事件流）
  POST   /projects/{id}/chat/confirm       确认 / 拒绝一张确认卡片
  POST   /projects/{id}/chat/stop          停止当前这轮对话
  文件：video / audio / audio/background / audio/stereo / tts/{seg} / voice-sample / voice-preview / export/download / subtitles.srt
  浏览器分离：separator-token / stems
"""
import asyncio
import hashlib
import json
import logging
import mimetypes
import os
import shutil
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from app import agent, config, jobs, status, store, view
from app.bus import bus
from app.commands import registry as R
from app.deps import enforce_separator_token, validate_video_id
from app.http_range import iter_file_range, parse_range
from app.models import STAGE_INITIALIZING, asr_audio_path, get_video_dir, tts_audio_path, video_dir_path
from app.services import export_service, subtitle_service, token_service, tts_service, voice_clone_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["projects"], dependencies=[Depends(validate_video_id)])
HEARTBEAT_S = 15


def _need(video_id: str):
    state = store.get_state(video_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return state


# ----------------------------------------------------------------------------
# 上传 / 视图 / 删除
# ----------------------------------------------------------------------------

def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# 上传不带 {video_id}，不能挂在上面那个带校验依赖的 router 上 → 单独的 router
upload_router = APIRouter(prefix="/projects", tags=["projects"])


@upload_router.post("")
def upload(file: UploadFile = File(...)):
    """
    上传视频。返回工程 id（文件 MD5）以及是否已存在。**不会自动开始处理** —— 开始是一个
    命令（`run_initialize`），要带上用户选的设置，见 `POST /projects/{id}/setup`。

    普通 `def`：函数体是阻塞的文件拷贝 + MD5（最大 2 GB）。写成 `async def` 会卡住事件
    循环，连带所有正在推送的事件流。
    """
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in config.UPLOAD_ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"unsupported file type: {ext or '(none)'}")

    temp_dir = os.path.join(config.DATA_DIR, "temp")
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, f"{uuid.uuid4()}{ext}")      # 不带对方给的文件名
    try:
        total = 0
        with open(temp_path, "wb") as out:
            while True:
                chunk = file.file.read(1 << 20)
                if not chunk:
                    break
                total += len(chunk)
                if total > config.UPLOAD_MAX_BYTES:       # 超限立刻中止，不等写完
                    raise HTTPException(status_code=413, detail=f"file too large (>{config.UPLOAD_MAX_BYTES >> 20} MB)")
                out.write(chunk)

        video_id = _md5(temp_path)
        base = get_video_dir(video_id)
        final_path = os.path.join(base, f"original_video{ext}")
        existed = os.path.exists(final_path)
        if existed:
            os.remove(temp_path)
        else:
            os.rename(temp_path, final_path)
        state = store.create(video_id, file.filename or f"video{ext}", final_path)
        return {"id": video_id, "filename": state.filename, "exists": existed, "stage": state.stage,
                "hasLines": bool(state.segments)}
    except HTTPException:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        logger.error(f"Upload failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{video_id}")
def get_project(video_id: str):
    state = _need(video_id)
    out = view.state_view(state)
    out["jobs"] = jobs.active_snapshot(video_id)
    out["seq"] = store.current_seq(video_id)
    out["undo"] = store.can_undo(video_id)
    out["export"] = _export_info(video_id)
    out["capabilities"] = {"subtitle": subtitle_service.capabilities(), "export": export_service.is_available()}
    return out


def _export_info(video_id: str) -> Optional[dict]:
    found = export_service.find_export(video_id)
    if not found:
        return None
    path, container = found
    return {"container": container, "sizeMb": round(os.path.getsize(path) / 1048576, 1),
            "url": f"/api/projects/{video_id}/export/download"}


@router.delete("/{video_id}")
def delete_project(video_id: str):
    if jobs.active(video_id):
        raise HTTPException(status_code=409, detail="有任务正在运行；先取消再删除")
    _need(video_id)
    agent.forget(video_id)
    store.delete(video_id)
    return {"deleted": True, "id": video_id}


class SetupBody(BaseModel):
    target_language: str
    accent: Optional[str] = None
    separation_mode: str = "off"
    enable_dubbing: bool = True
    enable_voice_clone: bool = False
    clone_smart_pick: bool = False
    mm_enhance: bool = False
    custom_prompt: str = ""


@router.post("/{video_id}/setup")
async def setup_and_start(video_id: str, body: SetupBody):
    """
    落地页的「开始」：写入设置并启动初始化。这是**唯一**一个不走 `commands/` 的写入口，
    因为此时工程还没有任何内容，没有“改动”可言，也不该进撤销栈。
    """
    _need(video_id)
    if jobs.find(video_id, kind="init"):
        raise HTTPException(status_code=409, detail="初始化已经在进行")
    if body.separation_mode not in config.VALID_SEPARATION_MODES:
        raise HTTPException(status_code=400, detail=f"separation_mode 必须是 {sorted(config.VALID_SEPARATION_MODES)}")
    if body.accent and body.accent not in config.CHINESE_ACCENTS:
        raise HTTPException(status_code=400, detail="不支持的方言")

    def apply(s):
        s.target_language = body.target_language
        s.accent = (body.accent or None) if body.target_language == "Chinese" else None
        s.enable_dubbing = body.enable_dubbing
        s.separation_mode = body.separation_mode if body.enable_dubbing else "off"
        s.enable_voice_clone = body.enable_voice_clone and body.enable_dubbing
        s.clone_smart_pick = body.clone_smart_pick and s.enable_voice_clone
        s.mm_enhance = body.mm_enhance
        s.custom_prompt = (body.custom_prompt or "").strip()[:500]
        s.stage = STAGE_INITIALIZING
        s.init_error = None
    store.mutate(video_id, apply, actor="system", cmd="setup", summary="设置并开始初始化", undoable=False)

    from app.commands.project import start_initialize
    ctx = R.Ctx(video_id=video_id, actor="system", cmd="setup")
    job = await start_initialize(ctx)
    return {"job": job.id}



# ----------------------------------------------------------------------------
# 命令
# ----------------------------------------------------------------------------

class CommandBody(BaseModel):
    args: dict = {}


@router.get("/{video_id}/commands")
def list_commands(video_id: str):
    _need(video_id)
    return {"commands": [
        {"name": c.name, "description": c.description, "params": c.params, "confirm": c.confirm,
         "readonly": c.readonly, "group": c.group}
        for c in R.all_commands()
    ]}


@router.post("/{video_id}/commands/{name}")
async def run_command(video_id: str, name: str, body: CommandBody):
    """
    UI 的每个按钮都走这里。返回 `{ok, result|error, rev}`：`rev` 让客户端知道执行后的
    版本，不用等事件流就能判断自己是不是已经是最新。
    """
    _need(video_id)
    outcome = await R.execute(video_id, name, body.args, actor="user")
    state = store.get_state(video_id)
    outcome["rev"] = state.rev if state else None
    if not outcome.get("ok"):
        # 业务上的"做不了"是 200 + ok:false（和 AI 拿到的形状一致）；只有找不到才 404
        if outcome.get("error", "").startswith("没有这个命令"):
            raise HTTPException(status_code=404, detail=outcome["error"])
    result = outcome.get("result")
    if isinstance(result, dict):
        result.pop("_audio", None)            # 给 AI 听的音频，不该回给 HTTP 客户端
    return outcome


@router.post("/{video_id}/undo")
async def undo(video_id: str):
    _need(video_id)
    return await R.execute(video_id, "undo", {}, actor="user")


# ----------------------------------------------------------------------------
# 事件流
# ----------------------------------------------------------------------------

@router.get("/{video_id}/events")
async def events(video_id: str, request: Request, since: int = 0):
    """
    SSE。`since` 之后的事件先回放（内存环形缓冲），再接着推新的。

    事件的 `seq` 在整个工程内单调；客户端记着最后收到的 seq，断线重连时带上它。
    回放不到（进程重启过）没关系：客户端以 `rev` 为准，发现落后就重新 GET 一次视图。
    """
    _need(video_id)
    queue = bus.subscribe(video_id, since=since)

    async def stream():
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        return
                    yield ": ping\n\n"           # 心跳：写不出去时生成器被关闭，订阅随之清理
                    continue
                if event is None:                 # 工程被删除
                    return
                yield f"id: {event.get('seq', 0)}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            bus.unsubscribe(video_id, queue)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ----------------------------------------------------------------------------
# 聊天
# ----------------------------------------------------------------------------

class ChatBody(BaseModel):
    text: str


class ConfirmBody(BaseModel):
    id: str
    approve: bool


@router.get("/{video_id}/chat")
def get_chat(video_id: str, conv: Optional[str] = None):
    """当前会话（或 `?conv=` 指定的那个）的消息。纯读：不分配会话、不写盘。"""
    _need(video_id)
    wanted = conv if conv is not None else store.current_conv(video_id, create=False)
    if wanted and not any(c["id"] == wanted for c in store.list_convs(video_id)):
        raise HTTPException(status_code=404, detail="没有这个会话")
    chat = agent.chat_for(video_id)
    return {
        "messages": store.read_chat(video_id, 300, conv=wanted),
        # running 是"这一轮"的状态，不是工程级的：显示别的会话时不该显示停止按钮。
        "running": chat.running and chat.conv == wanted,
        "conv": wanted,
    }


# ---- 会话（历史 / 新对话） ---------------------------------------------------

@router.get("/{video_id}/chats")
def list_chats(video_id: str):
    _need(video_id)
    return {"current": store.current_conv(video_id, create=False), "conversations": store.list_convs(video_id)}


@router.post("/{video_id}/chats")
def create_chat(video_id: str):
    """开一个新对话（旧的保留在历史里），并补上初始化报告那条默认消息。"""
    _need(video_id)
    conv = store.new_conv(video_id)
    agent.post_welcome(video_id)       # 每个新会话都从同一句"初始化完成…"开始
    return {"conv": conv}


class SelectConvBody(BaseModel):
    conv: str


@router.delete("/{video_id}/chats/{conv}")
def delete_chat(video_id: str, conv: str):
    """删掉一个会话。切到别的会话时由 store 顺带完成，返回新的当前会话。"""
    _need(video_id)
    if not store.delete_conv(video_id, conv):
        raise HTTPException(status_code=404, detail="没有这个会话")
    # post_welcome 自己判重：当前会话已有消息就不写；一个会话都不剩时 store
    # 会新开一个，正好由它补上默认消息。
    agent.post_welcome(video_id)
    return {"conv": store.current_conv(video_id, create=False)}


@router.post("/{video_id}/chats/select")
def select_chat(video_id: str, body: SelectConvBody):
    """切到某个会话。id 走 body 而不是路径，避免空值/特殊字符拼进 URL。"""
    _need(video_id)
    if not store.select_conv(video_id, body.conv):
        raise HTTPException(status_code=404, detail="没有这个会话")
    return {"conv": body.conv, "messages": store.read_chat(video_id, 300, conv=body.conv)}


@router.post("/{video_id}/chat")
async def post_chat(video_id: str, body: ChatBody):
    _need(video_id)
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="消息不能为空")
    if len(text) > 4000:
        raise HTTPException(status_code=400, detail="消息太长（最多 4000 字）")
    # 请求进入时就锁定会话：用户可能在排队期间切了会话，这一轮仍属于原来的那个。
    asyncio.create_task(agent.handle_user_message(video_id, text, store.current_conv(video_id, create=False)))
    return {"accepted": True}


@router.post("/{video_id}/chat/confirm")
def chat_confirm(video_id: str, body: ConfirmBody):
    _need(video_id)
    if not agent.resolve_confirmation(video_id, body.id, body.approve):
        raise HTTPException(status_code=404, detail="这张确认卡片已经处理过了，或已过期")
    return {"ok": True}


@router.post("/{video_id}/chat/stop")
def chat_stop(video_id: str):
    _need(video_id)
    agent.cancel_chat(video_id)
    return {"stopping": True}


# ----------------------------------------------------------------------------
# 文件
# ----------------------------------------------------------------------------

def _ranged(path: str, request: Request, media_type: str):
    size = os.path.getsize(path)
    rng = parse_range(request.headers.get("range", ""), size)
    if rng is None:
        return FileResponse(path, media_type=media_type, headers={"Accept-Ranges": "bytes"})
    start, end = rng
    length = end - start + 1
    return StreamingResponse(
        iter_file_range(path, start, length), status_code=206, media_type=media_type,
        headers={"Content-Range": f"bytes {start}-{end}/{size}", "Accept-Ranges": "bytes", "Content-Length": str(length)},
    )


@router.get("/{video_id}/video")
def serve_video(video_id: str, request: Request):
    state = _need(video_id)
    if not state.file_path or not os.path.exists(state.file_path):
        raise HTTPException(status_code=404, detail="Video file not found on disk")
    return _ranged(state.file_path, request, mimetypes.guess_type(state.file_path)[0] or "video/mp4")


@router.get("/{video_id}/audio")
def serve_audio(video_id: str):
    """16 kHz 单声道音轨 —— ASR 和 omni 通过公网 URL 取它。"""
    path = asr_audio_path(video_id)
    if not path:
        raise HTTPException(status_code=404, detail="Audio not found")
    return FileResponse(path, media_type="audio/wav")


@router.get("/{video_id}/audio/background")
def serve_background(video_id: str, request: Request):
    path = os.path.join(video_dir_path(video_id), "background.wav")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="No separated background")
    return _ranged(path, request, "audio/wav")


@router.get("/{video_id}/audio/stereo", dependencies=[Depends(enforce_separator_token)])
def serve_separation_source(video_id: str):
    """44.1 kHz 立体声，给浏览器端人声分离读（不是 16 kHz 单声道那份：会削掉高频）。"""
    import subprocess
    state = _need(video_id)
    stereo = os.path.join(video_dir_path(video_id), "separation_source.wav")
    if not os.path.exists(stereo):
        if not state.file_path or not os.path.exists(state.file_path):
            raise HTTPException(status_code=404, detail="原视频已不在磁盘上，无法提供分离源；请重新上传")
        partial = f"{stereo}.part.wav"
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", state.file_path, "-vn", "-acodec", "pcm_s16le",
                            "-ar", "44100", "-ac", "2", partial], capture_output=True, text=True)
        if r.returncode != 0:
            if os.path.exists(partial):
                os.remove(partial)
            logger.error(f"[{video_id}] stereo extraction failed: {r.stderr[-300:]}")
            raise HTTPException(status_code=500, detail="Could not extract stereo audio")
        os.replace(partial, stereo)
    return FileResponse(stereo, media_type="audio/wav")


@router.get("/{video_id}/tts/{segment_id}")
def serve_dub(video_id: str, segment_id: str):
    if not tts_service.is_safe_segment_id(segment_id):
        raise HTTPException(status_code=400, detail="malformed segment id")
    state = _need(video_id)
    seg = state.segment(segment_id)
    # 只送"新鲜"的配音：过期的 mp3 还在磁盘上，但念的是旧文本，不该被播放
    if seg is None or status.tts_status(state, seg) != status.FRESH:
        raise HTTPException(status_code=404, detail="这一句的配音不存在或已过期")
    return FileResponse(tts_audio_path(video_id, segment_id), media_type="audio/mpeg",
                        headers={"Cache-Control": "private, max-age=31536000, immutable"})


@router.get("/{video_id}/voice-sample/{speaker_id}")
def voice_sample(video_id: str, speaker_id: str):
    """克隆参考样本 —— DashScope 通过公网 URL 取它来注册音色。"""
    if not tts_service.is_safe_segment_id(speaker_id):
        raise HTTPException(status_code=400, detail="malformed speaker id")
    path = os.path.join(video_dir_path(video_id), "voice_clone", speaker_id, "sample.wav")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Sample not found")
    return FileResponse(path, media_type="audio/wav")


@router.get("/{video_id}/voice-preview/{speaker_id}")
def voice_preview(video_id: str, speaker_id: str):
    if not tts_service.is_safe_segment_id(speaker_id):
        raise HTTPException(status_code=400, detail="malformed speaker id")
    path = voice_clone_service.preview_path(video_id, speaker_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Preview not found")
    return FileResponse(path, media_type="audio/mpeg")


@router.get("/{video_id}/export/download")
def download_export(video_id: str):
    found = export_service.find_export(video_id)
    if not found:
        raise HTTPException(status_code=404, detail="还没有导出；先导出")
    path, container = found
    return FileResponse(path, media_type="video/x-matroska" if container == "mkv" else "video/mp4",
                        filename=f"translated_{video_id[:8]}.{container}")


@router.get("/{video_id}/subtitles.srt")
def download_srt(video_id: str, track: str = ""):
    from app.services.subtitle_service import SubtitleStyle
    state = _need(video_id)
    style = SubtitleStyle.from_dict({**SubtitleStyle.from_dict(state.subtitle_style or None).to_dict(),
                                     **({"track": track} if track in SubtitleStyle.VALID_TRACKS else {})})
    cues = subtitle_service.build_cues(state.segments, style)
    if not cues:
        raise HTTPException(status_code=409, detail="还没有字幕：先完成转写和翻译")
    return Response(content=subtitle_service.to_srt(cues, style), media_type="application/x-subrip; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{style.track}_{video_id[:8]}.srt"'})


@router.post("/{video_id}/subtitles/preview")
def preview_cues(video_id: str, body: dict):
    """用调用方给的样式预览字幕（不保存）—— 样式面板拖滑块时实时看效果。"""
    from app.services.subtitle_service import SubtitleStyle
    state = _need(video_id)
    base = SubtitleStyle.from_dict(state.subtitle_style or None).to_dict()
    patch = body.get("style") if isinstance(body, dict) else None
    if isinstance(patch, dict):
        base = {**base, **patch}
    if isinstance(body, dict) and body.get("track"):
        base["track"] = body["track"]
    style = SubtitleStyle.from_dict(base)
    cues = subtitle_service.build_cues(state.segments, style)
    return {"track": style.track, "count": len(cues), "style": style.to_dict(),
            "cues": [{"start": round(c.start, 3), "end": round(c.end, 3), "text": c.text,
                      "secondary": c.secondary, "lines": c.lines(style)} for c in cues]}


# ----------------------------------------------------------------------------
# 浏览器端人声分离：令牌 + 接收分离结果
# ----------------------------------------------------------------------------

@router.post("/{video_id}/separator-token")
def separator_token(video_id: str, request: Request):
    _need(video_id)
    peer = request.client.host if request.client else "unknown"
    if peer in ("127.0.0.1", "::1"):
        peer = (request.headers.get("x-real-ip") or "").strip() or peer
    if not token_service.check_issue_rate(peer):
        raise HTTPException(status_code=429, detail="令牌签发过于频繁，稍后再试")
    token, ttl = token_service.issue_token(video_id)       # (token, ttl 秒)：必须拆开，直接返回会变成 JSON 数组
    return {"video_id": video_id, "token": token, "expires_in": ttl,
            "model_download_url": "/api/models/separator/onnx"}


@router.post("/{video_id}/stems", dependencies=[Depends(enforce_separator_token)])
def upload_stems(video_id: str, vocals: UploadFile = File(...), background: UploadFile = File(...)):
    """接收浏览器分离出的两条 WAV。写盘后删掉过期的 16 kHz 派生音轨（下次初始化重新生成）。"""
    if jobs.find(video_id, kind="init"):
        raise HTTPException(status_code=409, detail="初始化正在进行，分离结果请在它开始之前上传")
    _need(video_id)
    base = get_video_dir(video_id)
    written = []
    for filename, up in (("vocals.wav", vocals), ("background.wav", background)):
        dest = os.path.join(base, filename)
        tmp = f"{dest}.part"
        try:
            with open(tmp, "wb") as out:
                shutil.copyfileobj(up.file, out)
            size = os.path.getsize(tmp)
            with open(tmp, "rb") as f:
                header = f.read(4)
            if size < 44 or header not in (b"RIFF", b"RF64"):
                raise ValueError("不是 WAV 文件")
            os.replace(tmp, dest)
            written.append({"file": filename, "bytes": size})
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise HTTPException(status_code=400, detail=f"Invalid stem {filename}: {e}")
    stale = os.path.join(base, "vocals_asr.wav")
    if os.path.exists(stale):
        os.remove(stale)
    store.mutate(video_id, lambda s: None, actor="system", cmd="stems", summary="收到人声分离结果", undoable=False)
    bus.publish(video_id, {"type": "change", "actor": "system", "cmd": "stems", "summary": "收到人声分离结果",
                           "rev": store.get_state(video_id).rev, "ids": [], "patch": {"hasStems": True}})
    return {"stems": written, "backgroundUrl": f"/api/projects/{video_id}/audio/background"}
