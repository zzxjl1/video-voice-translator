"""测试共用的假模型：按剧本回复，模拟 OpenAI 流式响应的形状。test_agent / test_e2e 共用。"""
import json
import types

from app import agent


class Script:
    def __init__(self, steps):
        self.steps = list(steps)
        self.seen = []

    async def create(self, **kw):
        self.seen.append(json.loads(json.dumps(kw["messages"], default=str)))
        step = self.steps.pop(0)
        return self._stream(step() if callable(step) else step)

    async def _stream(self, step):
        text = step.get("text", "")
        for i in range(0, len(text), 4):
            yield self._chunk(content=text[i:i + 4])
        for idx, (name, args) in enumerate(step.get("calls", [])):
            raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            half = len(raw) // 2
            yield self._chunk(tool=(idx, f"call_{idx}_{name}", name, raw[:half]))     # 参数分片到达
            yield self._chunk(tool=(idx, None, None, raw[half:]))
        yield types.SimpleNamespace(
            choices=[], usage=types.SimpleNamespace(model_dump=lambda: {"prompt_tokens": 100, "completion_tokens": 20}))

    @staticmethod
    def _chunk(content=None, tool=None):
        tcs = None
        if tool:
            idx, cid, name, args = tool
            tcs = [types.SimpleNamespace(index=idx, id=cid, function=types.SimpleNamespace(name=name, arguments=args))]
        delta = types.SimpleNamespace(content=content, tool_calls=tcs)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)], usage=None)


def install(steps):
    script = Script(steps)
    agent._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=script))
    return script
