"""命令层：UI 和 AI 共用的动作。导入各个模块即完成注册。"""
from app.commands import registry  # noqa: F401
from app.commands import read, transcript, translate, dub, voices, style, project  # noqa: F401
