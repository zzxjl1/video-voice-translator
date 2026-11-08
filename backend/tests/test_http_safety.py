"""
边界与安全：Range 解析、字幕格式的健壮性、路径参数、"对不存在的工程发请求不留痕迹"。

这些是与具体功能无关、但一旦回退就会出事的底线。
"""
import os, sys, tempfile
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")

from app import config
TMP = tempfile.mkdtemp(); config.DATA_DIR = TMP
from fastapi.testclient import TestClient
from app import store
from app.http_range import parse_range
from app.main import app
from app.models import Segment, Speaker
from app.services import subtitle_service as SS

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail is not None and not cond else ""))
    if not cond: fails.append(name)

# ------------------------------------------------------------------ ① Range 解析
check("① a-b", parse_range("bytes=0-9", 100) == (0, 9))
check("① a-（到末尾）", parse_range("bytes=90-", 100) == (90, 99))
check("① -n 取尾部 n 字节（曾被读成从 0 开始）", parse_range("bytes=-10", 100) == (90, 99))
check("① -n 大于文件 → 整个文件", parse_range("bytes=-500", 100) == (0, 99))
check("① 越界 end 被夹住", parse_range("bytes=50-999", 100) == (50, 99))
check("① 乱写的头 / 多段范围 → 忽略", parse_range("garbage", 100) is None and parse_range("bytes=1-2,5-6", 100) is None)
try:
    parse_range("bytes=500-600", 100); ok = False
except Exception as e:
    ok = getattr(e, "status_code", None) == 416
check("① 起点越界 → 416", ok)

# ------------------------------------------------------------------ ② 字幕格式
check("② ASS 时间 59.999s 不写成 60.00", SS._ass_time(59.999) == "0:01:00.00", SS._ass_time(59.999))
check("② ASS 时间 3661.5s", SS._ass_time(3661.5) == "1:01:01.50")
junk = SS.SubtitleStyle.from_dict({"font_size_percent": "abc", "bold": "false", "max_lines": None, "shadow": float("nan")})
check("② 脏数字回落默认值，不 500", junk.font_size_percent == 4.5 and junk.max_lines == 2 and junk.shadow == 1.0)
check("② 字符串 'false' 不被读成 True", junk.bold is False)
ass = SS.to_ass([SS.Cue(0, 2, "你好", "")], SS.SubtitleStyle().normalised(), 1920, 1080)
check("② 半透明底与描边并存（两层样式）", "Style: Box," in ass and "Dialogue: 0," in ass and "Dialogue: 1," in ass)
check("② 描边按画面高度换算（1080p ≈ 2.16px），不是写死 2", "2.16" in ass)

# ------------------------------------------------------------------ ③ 路径与"不留痕迹"
c = TestClient(app)
vid = "safety00" + "0" * 24
src = os.path.join(TMP, "v.mp4"); open(src, "wb").write(b"0123456789" * 6 + b"abcd")      # 64 字节
os.makedirs(os.path.join(TMP, vid), exist_ok=True)
store.create(vid, "My Movie.mp4", src)
def seed(s):
    s.target_language = "English"; s.speakers = [Speaker("0", "S", voice="E_v3.1")]
    s.segments = [Segment("seg-1", "0", 0, 2, "x")]
store.mutate(vid, seed, actor="system", cmd="seed", summary="seed")

check("③ 路径穿越（%2f）被拒", c.get(f"/api/projects/{vid}/tts/..%2Fsecret").status_code in (400, 404))
check("③ 路径穿越（speaker）被拒", c.get(f"/api/projects/{vid}/voice-sample/..%2Fx").status_code in (400, 404))
check("③ 畸形工程 id → 400", c.get("/api/projects/bad%20id%21").status_code in (400, 404))
ghost = "ghostxx0" + "0" * 24
for path in (f"/api/projects/{ghost}", f"/api/projects/{ghost}/video", f"/api/projects/{ghost}/audio",
             f"/api/projects/{ghost}/export/download", f"/api/projects/{ghost}/voice-preview/0",
             f"/api/projects/{ghost}/tts/seg-1", f"/api/projects/{ghost}/subtitles.srt"):
    c.get(path)
check("③ 对不存在的工程发 GET 不会凭空建目录", not os.path.exists(os.path.join(TMP, ghost)))
check("③ 对不存在的工程发命令 → 404，且不建目录",
      c.post(f"/api/projects/{ghost}/commands/get_overview", json={"args": {}}).status_code == 404 and not os.path.exists(os.path.join(TMP, ghost)))
check("③ 删除不存在的工程 → 404，且不建目录",
      c.delete(f"/api/projects/{ghost}").status_code == 404 and not os.path.exists(os.path.join(TMP, ghost)))

# ------------------------------------------------------------------ ④ Range 端到端
r = c.get(f"/api/projects/{vid}/video", headers={"Range": "bytes=-16"})
check("④ /video 后缀 Range → 206 且是尾部 16 字节",
      r.status_code == 206 and r.headers["content-range"] == "bytes 48-63/64" and len(r.content) == 16, (r.status_code, r.headers.get("content-range")))
check("④ 起点越界 → 416", c.get(f"/api/projects/{vid}/video", headers={"Range": "bytes=999-"}).status_code == 416)

# ------------------------------------------------------------------ ⑤ 命令入参
r = c.post(f"/api/projects/{vid}/commands/edit_source", json={"args": {"line": 1, "text": "x" * 5000}})
check("⑤ 超长文本被拒，并说清上限", r.json()["ok"] is False and "2000" in r.json()["error"], r.text[:200])
r = c.post(f"/api/projects/{vid}/chat", json={"text": "x" * 5000})
check("⑤ 聊天消息超长 → 400", r.status_code == 400)
check("⑤ 命令体不是对象 → 422", c.post(f"/api/projects/{vid}/commands/get_overview", json={"args": "oops"}).status_code == 422)

# ------------------------------------------------------------------ ⑥ 令牌门（浏览器分离的下载与上传）
check("⑥ 没有令牌不能下载分离源", c.get(f"/api/projects/{vid}/audio/stereo").status_code == 403)
check("⑥ 没有令牌不能上传分离结果",
      c.post(f"/api/projects/{vid}/stems", files={"vocals": ("v.wav", b"RIFF" + b"0" * 60), "background": ("b.wav", b"RIFF" + b"0" * 60)}).status_code == 403)
tr = c.post(f"/api/projects/{vid}/separator-token").json()
check("⑥ 令牌响应是对象，带 token / expires_in / model_download_url（前端按这个字段读）",
      isinstance(tr, dict) and tr.get("token") and tr.get("expires_in", 0) > 0 and tr.get("model_download_url"), tr)
tok = tr["token"]
bad = c.post(f"/api/projects/{vid}/stems", headers={"X-Separator-Token": tok},
             files={"vocals": ("v.wav", b"not a wav at all" * 5), "background": ("b.wav", b"RIFF" + b"0" * 60)})
check("⑥ 有令牌但内容不是 WAV → 400", bad.status_code == 400, bad.status_code)
good = c.post(f"/api/projects/{vid}/stems", headers={"X-Separator-Token": tok},
              files={"vocals": ("v.wav", b"RIFF" + b"0" * 60), "background": ("b.wav", b"RIFF" + b"0" * 60)})
check("⑥ 有令牌且是 WAV → 收下，视图里 hasStems", good.status_code == 200 and c.get(f"/api/projects/{vid}").json()["hasStems"])
other = "safety01" + "0" * 24
store.create(other, "o.mp4", src)
check("⑥ 拿工程 A 的令牌操作工程 B → 403（令牌绑定工程）",
      c.post(f"/api/projects/{other}/stems", headers={"X-Separator-Token": tok},
             files={"vocals": ("v.wav", b"RIFF" + b"0" * 60), "background": ("b.wav", b"RIFF" + b"0" * 60)}).status_code == 403)

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)


def test_http_safety():
    assert not fails, fails
