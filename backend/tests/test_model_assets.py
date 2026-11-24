"""
随代码库提交的模型资源：目录规范、完整性、真实路由、int8 转换器的数值正确性。

不联网、不下载：资源都在 backend/models/ 里。装了 onnx 才跑转换器相关的几条，没装就 SKIP
（`pip install onnx numpy`，维护者才需要）。
"""
import os, subprocess, sys
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND); sys.path.insert(0, os.path.join(BACKEND, "scripts")); os.chdir(BACKEND)
os.environ.setdefault("DASHSCOPE_API_KEY", "test"); os.environ.setdefault("LLM_API_KEY", "test")

from app import config
config.AUTH_DB_PATH = os.path.join(__import__("tempfile").mkdtemp(), "auth.sqlite3")
from fastapi.testclient import TestClient
from app.main import app
from _auth import login

fails = []
def check(name, cond, detail=None):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail is not None and not cond else ""))
    if not cond: fails.append(name)

MODELS = config.MODELS_DIR

# ====================================================================== ① 目录规范：models/ 下一个模型一个文件夹
entries = sorted(os.listdir(MODELS))
check("① models/ 下只有文件夹（没有散落在外面的模型文件）", all(os.path.isdir(os.path.join(MODELS, e)) for e in entries), entries)
check("① 两个模型各一个文件夹", entries == ["kokoro-82m", "uvr-mdx-net-inst-hq-3"], entries)
uvr = os.path.join(MODELS, "uvr-mdx-net-inst-hq-3")
check("① UVR 文件夹里有模型，和它的参数表一起", os.path.isfile(os.path.join(uvr, "UVR-MDX-NET-Inst_HQ_3.onnx"))
      and os.path.isfile(os.path.join(uvr, "mdx_model_data.json")), os.listdir(uvr))
check("① 分离模型的默认配置指向这个文件夹", config.SEPARATOR_MODEL_FILE == "uvr-mdx-net-inst-hq-3/UVR-MDX-NET-Inst_HQ_3.onnx", config.SEPARATOR_MODEL_FILE)
check("① 两个模型的文件都能从配置里找到", os.path.isfile(os.path.join(MODELS, config.SEPARATOR_MODEL_FILE))
      and os.path.isdir(config.KOKORO_MODELS_DIR) and os.path.basename(config.KOKORO_MODELS_DIR) == "kokoro-82m")

# ====================================================================== ② 真正提交进库：没有被 .gitignore 挡住
def ignored(path):
    r = subprocess.run(["git", "check-ignore", "-q", path], cwd=BACKEND, capture_output=True)
    return r.returncode == 0
try:
    big = [os.path.join(d, f) for d, _, fs in os.walk(MODELS) for f in fs if os.path.getsize(os.path.join(d, f)) > 1_000_000]
    check("② 大模型文件没有被 .gitignore 忽略（要随代码库提交，不是现场下载）", not any(ignored(p) for p in big), [p for p in big if ignored(p)])
    check("② 整个 kokoro-82m 目录没有被忽略", not ignored(os.path.join(config.KOKORO_MODELS_DIR, "manifest.json"))
          and not ignored(os.path.join(config.KOKORO_MODELS_DIR, "voices", "zf_xiaobei.bin")))
    check("② 最大文件 < 100MB（GitHub 单文件上限）", max(os.path.getsize(p) for p in big) < 100 * 1024 * 1024, max(os.path.getsize(p) for p in big))
except FileNotFoundError:
    print("SKIP ② git 不可用")

# ====================================================================== ③ prepare 脚本的 --verify（sha256）
p = subprocess.run([sys.executable, "scripts/prepare_kokoro_models.py", "--verify"], capture_output=True, text=True, cwd=BACKEND)
check("③ prepare_kokoro_models.py --verify：manifest 与磁盘上的文件完全一致（大小 + sha256）", p.returncode == 0 and p.stdout.strip() == "ok", (p.returncode, p.stderr[:200]))

# ====================================================================== ④ 真实路由提供提交进库的文件
c = TestClient(app); login(c)
r = c.get("/api/models/kokoro").json()
check("④ GET /api/models/kokoro：用真实目录，资源齐全", r["available"] is True and r["missing"] == [] and r["size_mb"] > 100, r)
r = c.head("/api/models/kokoro/onnx/model_int8.onnx")
check("④ 模型文件可取，长度与磁盘一致", r.status_code == 200 and int(r.headers["content-length"]) == os.path.getsize(os.path.join(config.KOKORO_MODELS_DIR, "onnx", "model_int8.onnx")), r.status_code)
r = c.get("/api/models/kokoro/onnx/model_int8.onnx", headers={"Range": "bytes=0-15"})
check("④ 模型文件支持 Range（下载中断可续）", r.status_code == 206 and len(r.content) == 16, r.status_code)
check("④ 音色 / tokenizer / espeak 都能取", all(c.head(f"/api/models/kokoro/{x}").status_code == 200 for x in
      ("voices/zf_xiaobei.bin", "voices/af_heart.bin", "tokenizer.json", "config.json", "espeak/espeak-ng.js", "espeak/espeak-ng.data")))
check("④ 没有随库提供的音色（es / hi / ja）→ 404", all(c.head(f"/api/models/kokoro/voices/{v}.bin").status_code == 404 for v in ("ef_dora", "hf_alpha", "jf_alpha")))
info = c.get("/api/models/separator")
check("④ GET /api/models/separator：用真实默认路径找到分离模型", info.status_code == 200 and info.json()["filename"] == "UVR-MDX-NET-Inst_HQ_3.onnx"
      and info.json()["size_mb"] > 60, (info.status_code, info.text[:120]))
check("④ separation_capabilities：浏览器后端可用", config.separation_capabilities()["client"]["available"] is True)

# ====================================================================== ⑤ int8 转换器（需要 onnx）
try:
    import numpy as np, onnx
    from onnx import TensorProto, helper, numpy_helper as nh
    from onnx.reference import ReferenceEvaluator
except Exception:
    print("SKIP ⑤ 转换器测试（没装 onnx / numpy：pip install onnx numpy）")
else:
    import kokoro_int8, tempfile
    rng = np.random.default_rng(7)

    def build_matmul():
        K, N = 16, 8
        Wq = rng.integers(-127, 128, (K, N)).astype("int8"); ws = (rng.random(N).astype("float32") * 0.02 + 0.005)
        wzp = np.zeros(N, "int8"); bias = rng.normal(0, 0.1, N).astype("float32")
        init = [nh.from_array(Wq, "Wq"), nh.from_array(ws, "ws"), nh.from_array(wzp, "wzp"), nh.from_array(bias, "bias")]
        nodes = [
            helper.make_node("DynamicQuantizeLinear", ["x"], ["xq", "xs", "xzp"], name="dql"),
            helper.make_node("MatMulInteger", ["xq", "Wq", "xzp", "wzp"], ["acc"], name="mmi"),
            helper.make_node("Cast", ["acc"], ["accf"], to=TensorProto.FLOAT, name="c"),
            helper.make_node("Mul", ["xs", "ws"], ["sc"], name="scm"),
            helper.make_node("Mul", ["accf", "sc"], ["y0"], name="m"),
            helper.make_node("Add", ["y0", "bias"], ["y"], name="a"),
        ]
        g = helper.make_graph(nodes, "g", [helper.make_tensor_value_info("x", TensorProto.FLOAT, [4, K])],
                              [helper.make_tensor_value_info("y", TensorProto.FLOAT, [4, N])], init)
        ref = lambda x: x @ ((Wq.astype("float32") - 0) * ws) + bias
        return helper.make_model(g, opset_imports=[helper.make_opsetid("", 20)]), ref, (4, K)

    def build_conv():
        Cin, Cout, T = 4, 6, 10
        Wq = rng.integers(-127, 128, (Cout, Cin, 3)).astype("int8"); ws = np.float32(0.01); wzp = np.int8(0)
        bias = rng.normal(0, 0.1, Cout).astype("float32")
        init = [nh.from_array(Wq, "Wq"), nh.from_array(np.array(ws), "ws"), nh.from_array(np.array(wzp), "wzp"),
                nh.from_array(bias, "bias"), nh.from_array(np.array([1, Cout, 1], "int64"), "shp")]
        nodes = [
            helper.make_node("DynamicQuantizeLinear", ["x"], ["xq", "xs", "xzp"], name="dql"),
            helper.make_node("ConvInteger", ["xq", "Wq", "xzp", "wzp"], ["acc"], name="ci", kernel_shape=[3], pads=[1, 1], strides=[1], dilations=[1], group=1),
            helper.make_node("Mul", ["xs", "ws"], ["bsc"], name="bsm"),
            helper.make_node("Div", ["bias", "bsc"], ["bdiv"], name="bd"),
            helper.make_node("Floor", ["bdiv"], ["bfl"], name="bf"),
            helper.make_node("Cast", ["bfl"], ["bq"], to=TensorProto.INT32, name="bc"),
            helper.make_node("Reshape", ["bq", "shp"], ["bqr"], name="br"),
            helper.make_node("Add", ["acc", "bqr"], ["accb"], name="ab"),
            helper.make_node("Cast", ["accb"], ["accf"], to=TensorProto.FLOAT, name="c"),
            helper.make_node("Mul", ["accf", "bsc"], ["y"], name="m"),
        ]
        g = helper.make_graph(nodes, "g", [helper.make_tensor_value_info("x", TensorProto.FLOAT, [1, Cin, T])],
                              [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, Cout, T])], init)
        def ref(x):
            Wf = Wq.astype("float32") * ws
            xp = np.pad(x, ((0, 0), (0, 0), (1, 1)))
            return np.stack([np.stack([sum((xp[0, :, t:t + 3] * Wf[o]).sum() for _ in [0]) for t in range(T)]) for o in range(Cout)])[None] + bias[None, :, None]
        return helper.make_model(g, opset_imports=[helper.make_opsetid("", 20)]), ref, (1, Cin, T)

    for label, build, tol_orig in (("MatMul", build_matmul, 0.05), ("Conv", build_conv, 0.08)):
        model, ref, shape = build()
        d = tempfile.mkdtemp(); a, b = os.path.join(d, "a.onnx"), os.path.join(d, "b.onnx")
        onnx.save(model, a)
        info = kokoro_int8.convert(a, b)
        x = rng.normal(0, 1, shape).astype("float32")
        y_ref = ref(x)
        y_new = ReferenceEvaluator(onnx.load(b)).run(None, {"x": x})[0]
        check(f"⑤ 转换器 {label}：转换后和浮点参考结果一致（只有权重量化误差，没有激活量化误差）",
              np.allclose(y_new, y_ref, atol=1e-4), float(np.abs(y_new - y_ref).max()))
        try:
            y_old = ReferenceEvaluator(onnx.load(a)).run(None, {"x": x})[0]
            err = float(np.abs(y_old - y_ref).max() / (np.abs(y_ref).max() + 1e-9))
            check(f"⑤ 转换器 {label}：原动态 int8 图与浮点参考的差在激活量化误差范围内（对照：转换版更准）", err < tol_orig, err)
        except Exception as e:
            print(f"SKIP ⑤ {label} 原图对照（reference evaluator 不支持其中的整数算子：{str(e)[:60]}）")
        check(f"⑤ 转换器 {label}：不再有 WebGPU 不支持的算子", kokoro_int8.check(b) == [], kokoro_int8.check(b))
        check(f"⑤ 转换器 {label}：权重仍是 int8（模型没有变大）", any(i.data_type == TensorProto.INT8 for i in onnx.load(b).graph.initializer))
        check(f"⑤ 转换器 {label}：不用 DequantizeLinear（ORT WebGPU 上它输出全零）", all(n.op_type != "DequantizeLinear" for n in onnx.load(b).graph.node))

    shipped = os.path.join(config.KOKORO_MODELS_DIR, "onnx", "model_int8.onnx")
    check("⑤ 随库提供的 model_int8.onnx：没有 WebGPU 不支持的算子", kokoro_int8.check(shipped) == [], kokoro_int8.check(shipped))
    left = {n.op_type for n in onnx.load(shipped, load_external_data=False).graph.node} & {"DynamicQuantizeLinear", "MatMulInteger", "ConvInteger", "DequantizeLinear"}
    check("⑤ 随库提供的 model_int8.onnx：整数算子和 DequantizeLinear 全部换掉", not left, left)

print(f"\n合计 {'全部通过' if not fails else str(len(fails)) + ' 项失败: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
