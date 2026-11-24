"""
Kokoro `model_quantized.onnx`（动态 int8）-> 能在 WebGPU 上整图运行的 int8 模型。

为什么要改图
------------
官方的 int8 模型里，每个线性层 / 卷积是

    x -> DynamicQuantizeLinear -> MatMulInteger(xq, Wq) -> Cast -> Mul(x_scale * W_scale) [-> Add(bias)]
    x -> DynamicQuantizeLinear -> ConvInteger(xq, Wq)    -> Add(int32 bias) -> Cast -> Mul(...)

ORT 的 WebGPU 后端没有 MatMulInteger / ConvInteger / DynamicQuantizeLinear 的 GPU 实现，整批回落 CPU，
速度等同纯 WASM（~0.7x 实时）。

做法（只改图，不改权重）
------------------------
把每一组换成

    x -> MatMul(x, W_float)        x -> Conv(x, W_float, bias_float)
    W_float = (Cast(Wq) - zero_point) * W_scale          # 全是常量，ORT 建会话时折叠

权重仍以 int8 存放（文件和官方 int8 一样大，~92MB）；激活不再量化，所以数值比原 int8 更接近 fp32。
`DynamicQuantizeLSTM` 等其它算子原样保留（它在 WebGPU 上走 CPU，不影响正确性）。

为什么反量化不用 `DequantizeLinear`
----------------------------------
这是一个实测出来的坑：ORT 的 WebGPU 后端会把 `DequantizeLinear`（int8 -> float）算成**全零**
（onnxruntime-web 1.31.1 和 1.33 dev 都一样，同一个文件在 WASM 上正常、在 WebGPU 上全静音）。
`Cast -> Sub -> Mul` 三个都是普通算子，WebGPU 正常。不要"简化"回 DequantizeLinear。

用法（维护者，需要 `pip install onnx numpy`）：
    python kokoro_int8.py model_quantized.onnx model_int8.onnx
"""
from __future__ import annotations

import collections
import sys


def convert(src: str, dst: str) -> dict:
    import numpy as np
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    model = onnx.load(src)
    g = model.graph
    init = {i.name: i for i in g.initializer}
    producer = {o: n for n in g.node for o in n.output}
    consumers: dict[str, list] = collections.defaultdict(list)
    for n in g.node:
        for i in n.input:
            consumers[i].append(n)

    replacement: dict[int, object] = {}        # id(缩放 Mul) -> 取代整条链的新节点
    dead: set[int] = set()
    dequant_nodes: list = []                   # Cast / Sub / Mul，只依赖常量，统一放在图最前面
    zero_points: list = []
    dequant_of: dict[tuple, str] = {}          # 同一个量化权重被多处共用时只反量化一次
    stats: collections.Counter = collections.Counter()

    for n in g.node:
        if n.op_type not in ("MatMulInteger", "ConvInteger"):
            continue
        quantize = producer[n.input[0]]
        assert quantize.op_type == "DynamicQuantizeLinear", f"{n.name}: 输入不是动态量化"
        x, wq = quantize.input[0], n.input[1]
        wzp = n.input[3] if len(n.input) > 3 else ""
        assert wq in init, f"{n.name}: 权重不是常量"

        # 沿唯一消费者走到缩放用的 Mul，沿途只允许 Add / Cast
        chain, cur = [], n
        while True:
            nxt = consumers[cur.output[0]]
            assert len(nxt) == 1, f"{n.name}: 输出有多个消费者"
            cur = nxt[0]
            chain.append(cur)
            if cur.op_type == "Mul":
                break
            assert cur.op_type in ("Add", "Cast"), f"{n.name}: 意外的算子 {cur.op_type}"
        scale_mul = chain[-1]
        cast_out = chain[-2].output[0]
        scale_name = next(i for i in scale_mul.input if i != cast_out)
        scale_node = producer[scale_name]
        assert scale_node.op_type == "Mul" and quantize.output[1] in scale_node.input, f"{n.name}: 缩放结构不符"
        ws = next(i for i in scale_node.input if i != quantize.output[1])
        assert ws in init, f"{n.name}: 权重 scale 不是常量"

        key = (wq, ws, wzp)
        if key not in dequant_of:
            k = len(dequant_of)
            w_f32, w_sub, w_f = f"dq{k}_f32", f"dq{k}_sub", f"dq{k}_float"
            zp_name = f"dq{k}_zp"
            zp = numpy_helper.to_array(init[wzp]).astype("float32") if wzp else np.zeros((), "float32")
            zero_points.append(numpy_helper.from_array(zp, zp_name))
            dequant_nodes.append(helper.make_node("Cast", [wq], [w_f32], name=f"dq{k}_cast", to=TensorProto.FLOAT))
            dequant_nodes.append(helper.make_node("Sub", [w_f32, zp_name], [w_sub], name=f"dq{k}_sub"))
            dequant_nodes.append(helper.make_node("Mul", [w_sub, ws], [w_f], name=f"dq{k}_mul"))
            dequant_of[key] = w_f
        w_float = dequant_of[key]

        if n.op_type == "ConvInteger":
            inputs = [x, w_float]
            add = next((c for c in chain if c.op_type == "Add"), None)
            if add is not None:                    # 偏置：回溯到被量化之前的浮点 bias
                name = add.input[1]
                while name not in init:
                    name = producer[name].input[0]
                assert init[name].data_type == TensorProto.FLOAT, f"{n.name}: 偏置不是 float"
                inputs.append(name)
            attrs = {a.name: helper.get_attribute_value(a) for a in n.attribute}
            new = helper.make_node("Conv", inputs, [scale_mul.output[0]], name=f"{n.name}__conv", **attrs)
            stats["Conv"] += 1
        else:
            new = helper.make_node("MatMul", [x, w_float], [scale_mul.output[0]], name=f"{n.name}__matmul")
            stats["MatMul"] += 1

        replacement[id(scale_mul)] = new
        dead.add(id(n))
        dead.update(id(c) for c in chain)

    nodes = list(dequant_nodes)
    for n in g.node:
        if id(n) in replacement:
            nodes.append(replacement[id(n)])
        elif id(n) in dead:
            continue
        elif n.op_type == "DequantizeLinear":
            # 词嵌入查表后的反量化（Gather 输出是运行时才有的值，不是常量）：同样 WebGPU 上不可靠，换成 Cast/Sub/Mul
            assert not n.attribute, f"{n.name}: 逐轴 DequantizeLinear，没有处理"
            x, scale = n.input[0], n.input[1]
            zp = n.input[2] if len(n.input) > 2 else ""
            k = n.name or n.output[0]
            nodes.append(helper.make_node("Cast", [x], [f"{k}__f32"], name=f"{k}__cast", to=TensorProto.FLOAT))
            cur = f"{k}__f32"
            if zp:
                nodes.append(helper.make_node("Cast", [zp], [f"{k}__zpf"], name=f"{k}__zpcast", to=TensorProto.FLOAT))
                nodes.append(helper.make_node("Sub", [cur, f"{k}__zpf"], [f"{k}__sub"], name=f"{k}__sub"))
                cur = f"{k}__sub"
            nodes.append(helper.make_node("Mul", [cur, scale], [n.output[0]], name=f"{k}__mul"))
            stats["Embedding dequant"] += 1
        else:
            nodes.append(n)

    # 死代码清理：整数算子拿掉后，DynamicQuantizeLinear、缩放 Mul、偏置量化链都没人用了
    outputs = {o.name for o in g.output}
    while True:
        used = {i for n in nodes for i in n.input} | outputs
        keep = [n for n in nodes if any(o in used for o in n.output)]
        if len(keep) == len(nodes):
            break
        nodes = keep

    del g.node[:]
    g.node.extend(nodes)
    g.initializer.extend(zero_points)
    used = {i for n in nodes for i in n.input} | outputs
    kept = [i for i in g.initializer if i.name in used]
    del g.initializer[:]
    g.initializer.extend(kept)

    onnx.checker.check_model(model)
    onnx.save(model, dst)
    ops = collections.Counter(n.op_type for n in nodes)
    return {"replaced": dict(stats), "nodes": len(nodes), "ops": dict(ops)}


# WebGPU 跑不了、而且我们已经全部换掉的算子。留下任何一个都说明转换不完整（速度会掉回 CPU）。
FORBIDDEN = ("MatMulInteger", "ConvInteger", "DynamicQuantizeLinear", "DequantizeLinear", "QLinearMatMul", "QLinearConv")


def check(path: str) -> list[str]:
    """返回问题清单；空 = 这个文件符合"WebGPU 整图 int8"的要求。"""
    import onnx

    ops = collections.Counter(n.op_type for n in onnx.load(path, load_external_data=False).graph.node)
    return [f"{op} x{ops[op]}" for op in FORBIDDEN if ops.get(op)]


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    info = convert(sys.argv[1], sys.argv[2])
    print("replaced:", info["replaced"], "| nodes:", info["nodes"])
    left = check(sys.argv[2])
    print("留下的 WebGPU 不支持算子:", left or "无")
