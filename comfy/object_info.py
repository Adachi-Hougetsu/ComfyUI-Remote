"""object_info 拉取 + 缓存。模型/下拉选项来源。"""
from __future__ import annotations

from .client import ComfyClient


class ObjectInfoCache:
    def __init__(self, client: ComfyClient):
        self._client = client
        self._cache: dict = {}
        self._ready = False

    async def refresh(self) -> bool:
        try:
            self._cache = await self._client.get_object_info()
            self._ready = True
        except Exception:
            # ComfyUI 掉线/刷新失败时保留旧缓存：下拉不因此变空，下轮刷新自愈
            self._ready = False
        return self._ready

    @property
    def ready(self) -> bool:
        return self._ready

    def data(self) -> dict:
        """原始 object_info 全量 dict（导入识别引擎用；未就绪时为空）。"""
        return self._cache

    def get(self, node_class: str) -> dict:
        return self._cache.get(node_class) or {}

    def combo_options(self, node_class: str, widget: str) -> list:
        """input.required[widget][0] = 文件名/选项列表。"""
        info = self.get(node_class)
        required = info.get("input", {}).get("required", {})
        if widget in required:
            val = required[widget]
            if isinstance(val, list) and val and isinstance(val[0], list):
                return val[0]
        return []

    def widget_default(self, node_class: str, widget: str):
        """combo 的默认值（input.required[widget][1] 里的 default）。"""
        info = self.get(node_class)
        required = info.get("input", {}).get("required", {})
        if widget in required:
            val = required[widget]
            if isinstance(val, list) and len(val) > 1 and isinstance(val[1], dict):
                return val[1].get("default")
        return None
