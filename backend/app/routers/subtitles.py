"""
字幕：与具体工程无关的两个查询。

每个工程的样式、预览、SRT 下载现在分别在 `commands/style.py`（改样式）和
`routers/projects.py`（预览 / 下载）里 —— 样式是项目文档的一部分，和 AI 共用一份。
"""
from fastapi import APIRouter

from app.services import subtitle_service

router = APIRouter(prefix="/subtitles", tags=["subtitles"])


@router.get("/capabilities")
def get_capabilities():
    """
    这台服务器实际能做哪些字幕交付方式，以及做不了的原因。
    让界面把做不到的选项置灰并写明理由，而不是让用户点了之后在导出半途失败。
    """
    return {
        "capabilities": subtitle_service.capabilities(),
        "default_style": subtitle_service.default_style().to_dict(),
    }


@router.get("/defaults")
def get_defaults():
    return {"style": subtitle_service.default_style().to_dict()}
