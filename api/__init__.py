"""API 层装配：把共享服务注入各路由模块。

AppState 定义在 api/state.py（避免循环导入），app.py 负责组装。
"""
from __future__ import annotations

from .state import AppState
from .panel import router as panel_router
from .generate import router as generate_router
from .upload import router as upload_router
from .gallery import router as gallery_router
from .images import router as images_router
from .ws import router as ws_router
from .imports import router as imports_router
from .settings import router as settings_router


def build_routers(state: AppState) -> list:
    """返回全部 APIRouter（按顺序 include，/api/* 在静态挂载之前匹配）。"""
    return [
        panel_router(state),
        generate_router(state),
        upload_router(state),
        gallery_router(state),
        images_router(state),
        ws_router(state),
        imports_router(state),
        settings_router(state),
    ]
