"""
混音策略的唯一实现 —— 数值与包络只存在于这个文件。

设计（为什么长这样）
--------------------
之前"混音"散在三处且互不一致：`config.py` 的三个 GAIN、`export_service._mux`
里写死的 amix/alimiter、播放时前端自己算的 0.1/1.0。预览和成片是两个东西。

现在：这个模块从工程状态算出**一份 mix plan**（纯数据），导出执行和播放求值
都是它的消费者 —— 没有任何一方持有第二份策略。导出时现场重新生成 plan，
所以执行端永远新鲜；播放端拿到的 plan 带版本号，变了才重新下发。

策略本身（一段话）：配音满电平摆进自己的时间槽，超槽先变速（与播放同一组
夹取界）再裁；垫底那条在有人说话时按固定 dB 下压、带 attack/release；
整条混完后按 master 目标做响度归一与限峰。分离出的背景（不含人声）不下压，
未分离的原声（含原语言人声）下压到台词之下。
"""

import hashlib
import json
import logging
import os

from app.models import VideoState, get_video_dir

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 策略数值 —— 全仓唯一。改这里的值，导出与预览同时变。
# ---------------------------------------------------------------------------

# 复用 config 的读取助手 —— 连"怎么读环境变量"也不留第二份实现
from app.config import env_float as _env_float


# 整条混音的整合响度目标（LUFS）。-16 是通用/网页内容的常用目标。
TARGET_LUFS = _env_float("MIX_TARGET_LUFS", -16.0)
# 峰值上限（dBFS）。loudnorm 的 TP 参数按真峰算，这里给 -1.5 留出余量。
PEAK_LIMIT_DBFS = _env_float("MIX_PEAK_LIMIT_DBFS", -1.5)
# 响度范围目标（loudnorm 的 LRA）。
LOUDNESS_RANGE = _env_float("MIX_LOUDNESS_RANGE", 11.0)

# 每段配音首尾的淡入淡出（秒），消掉 TTS 边缘的咔哒声。
VOICE_FADE_S = 0.02

# 垫底电平与下压量（dB）。分离出的背景不含人声 → 不下压；
# 未分离的原声就是原语言人声 → 必须压到台词之下。
BGM_GAIN_DB = _env_float("MIX_BGM_GAIN_DB", -1.0)
BGM_DUCK_DB = _env_float("MIX_BGM_DUCK_DB", 0.0)
ORIGINAL_GAIN_DB = _env_float("MIX_ORIGINAL_GAIN_DB", 0.0)
ORIGINAL_DUCK_DB = _env_float("MIX_ORIGINAL_DUCK_DB", -24.0)

# 下压/抬起的快慢。太快会"抽气"，太慢会漏出原声。
DUCK_ATTACK_S = _env_float("MIX_DUCK_ATTACK_S", 0.12)
DUCK_RELEASE_S = _env_float("MIX_DUCK_RELEASE_S", 0.35)

# 每段变速的夹取界。播放与导出用同一组 —— 这是"预览=成片"的另一半。
RATE_MIN = _env_float("MIX_RATE_MIN", 0.8)
RATE_MAX = _env_float("MIX_RATE_MAX", 1.25)

# 策略语义变了就 +1：让所有已下发的 plan 失效，前端会重新取。
POLICY_VERSION = 1


def db_to_gain(db: float) -> float:
    return 10.0 ** (db / 20.0)


# ---------------------------------------------------------------------------
# plan 生成
# ---------------------------------------------------------------------------

def _merge_spans(spans: list[tuple[float, float]], gap: float) -> list[tuple[float, float]]:
    """合并相交/相距小于 gap 的区间。不合并的话，两段之间的包络会抬起又立刻
    压下（几十毫秒的原语言人声漏出来），听感是跳音。"""
    merged: list[list[float]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]


def _duck_breakpoints(
    spans: list[tuple[float, float]], duck_gain: float
) -> list[list[float]]:
    """
    垫底包络的分段线性断点：`[t, gain]`。

    生成规则（与播放侧的求值方式严格对应 —— 播放侧只做断点间线性插值）：
    每个语音区间在开始前 attack 处从 1 开始下压、区间内保持 duck、
    结束后 release 处抬回 1。
    """
    points: list[list[float]] = []
    for start, end in spans:
        points.append([max(0.0, start - DUCK_ATTACK_S), 1.0])
        points.append([start, duck_gain])
        points.append([end, duck_gain])
        points.append([end + DUCK_RELEASE_S, 1.0])
    # 去掉相邻重复/回退的时间点（np.interp 与前端插值都要求时间严格递增）
    cleaned: list[list[float]] = []
    for t, g in points:
        if cleaned and t <= cleaned[-1][0]:
            cleaned[-1][1] = g      # 同一时刻取后者（更接近说话的开始/结束）
        else:
            cleaned.append([t, g])
    return cleaned


def _segment_entries(state: VideoState) -> tuple[list[dict], bool]:
    """
    每段配音的摆位条目。返回 (entries, state 是否被补写过)。

    旧工程没有 actual_duration（以前只写日志不落盘）：这里用 ffprobe 现量一次
    并写回 —— 是数据补齐，只发生一次，之后随 state 持久化。
    """
    from app.services.subtitle_service import probe_duration

    entries: list[dict] = []
    dirty = False
    for seg in state.segments:
        if not seg.audio_path or not os.path.exists(seg.audio_path):
            continue
        if getattr(seg, "muted", False):
            continue
        slot = max(0.0, (seg.end_time or 0.0) - (seg.start_time or 0.0))
        if slot <= 0:
            continue

        actual = seg.actual_duration
        if not actual:
            actual = probe_duration(seg.audio_path)
            if actual:
                seg.actual_duration = actual
                dirty = True
            else:
                # 量不出来（文件损坏）。不是"猜一个 1.0 继续混"：这条按自然速度
                # 摆入并裁到槽长，导出日志会点名 —— 与"跳过坏文件"同一待遇。
                logger.warning(
                    "[%s] segment %s: 无法测量配音时长，按自然速度摆入并裁剪",
                    state.video_id, seg.id,
                )

        if actual:
            ideal = actual / slot
            atempo = min(max(ideal, RATE_MIN), RATE_MAX)
            effective = actual / atempo
            trim = slot if effective > slot + 1e-6 else None
            played = min(effective, slot)
        else:
            # 时长量不出来：按自然速度摆入并裁到槽长 —— 保证不与下一句叠加，
            # 与上面的告警语义一致（否则"裁剪"只是说说）。
            ideal = 1.0
            atempo = 1.0
            trim = slot
            played = slot

        entries.append({
            "id": seg.id,
            "start": round(max(0.0, seg.start_time or 0.0), 4),
            "end": round(max(0.0, seg.end_time or 0.0), 4),
            "atempo": round(atempo, 4),
            "videoRate": round(min(max(atempo / ideal, RATE_MIN), RATE_MAX), 4) if actual else 1.0,
            "trim": round(trim, 4) if trim is not None else None,
            # 变速后这句实际占用的秒数：播放器拿它做进度对位（以前前端用
            # "自然时长 × 进度"，速率≠1 时对位就是错的）。
            "played": round(played, 4),
        })
    return entries, dirty


def build_mix_plan(state: VideoState) -> dict:
    """
    从工程状态算出混音计划。抛错而不是猜：
    - 没有任何可混的配音 → RuntimeError（调用方把"导出"拒掉）；
    - 没有垫底来源（既无分离产物、原片又没音轨）→ RuntimeError —— 这条链路
      本来就走不到（没音轨过不了 ASR），真出现说明有别的 bug，响亮失败。
    """
    entries, dirty = _segment_entries(state)
    if not entries:
        raise RuntimeError("没有任何可混音的配音 —— 先完成合成")

    video_dir = get_video_dir(state.video_id)
    background_path = os.path.join(video_dir, "background.wav")
    if os.path.exists(background_path):
        bed = {
            "kind": "background",
            "gain": round(db_to_gain(BGM_GAIN_DB), 5),
            # 背景不含人声，没有要避让的语音 → 不下压，包络为空
            "duck": [],
        }
    elif state.file_path and os.path.exists(state.file_path):
        bed = {
            "kind": "original",
            "gain": round(db_to_gain(ORIGINAL_GAIN_DB), 5),
            "duck": _duck_breakpoints(
                _merge_spans(
                    [(e["start"], e["end"]) for e in entries],
                    DUCK_ATTACK_S + DUCK_RELEASE_S,
                ),
                db_to_gain(ORIGINAL_DUCK_DB),
            ),
        }
    else:
        raise RuntimeError(
            "没有垫底音源：既没有分离产物 background.wav，源视频也不存在 —— "
            "这种状态不应该出现（没有音轨的工程走不到合成），请检查工程数据"
        )

    plan = {
        "policyVersion": POLICY_VERSION,
        "bed": bed,
        "segments": entries,
        "voiceFadeS": VOICE_FADE_S,
        "master": {
            "targetLufs": TARGET_LUFS,
            "peakLimitDbfs": PEAK_LIMIT_DBFS,
            "loudnessRange": LOUDNESS_RANGE,
        },
    }
    plan["version"] = plan_version(plan)

    if dirty:
        # 补齐过的时长写回 state（只发生一次；info.json 是原子写）
        from app.models import save_state
        save_state(state)

    return plan


def plan_version(plan: dict) -> str:
    """plan 的内容哈希。播放端据此判断要不要重新取（状态轮询里顺带下发）。"""
    payload = {k: v for k, v in plan.items() if k != "version"}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
