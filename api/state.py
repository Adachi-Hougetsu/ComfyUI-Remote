"""共享服务容器。独立模块避免 api/__init__ 与各路由模块循环导入。"""
from __future__ import annotations

from dataclasses import dataclass

from comfy.client import ComfyClient
from comfy.object_info import ObjectInfoCache
from comfy.ws_listener import ComfyWSListener
from core.session import SessionStore
from tasks import TaskManager


@dataclass
class AppState:
    client: ComfyClient
    object_info: ObjectInfoCache
    ws: ComfyWSListener
    tasks: TaskManager
    session: SessionStore

    async def reconfigure_comfy(self, url: str) -> None:
        """设置页改 ComfyUI 地址：重建 client / object_info / WS，热生效（免重启）。

        运行中的旧批次改用新 client 查询（地址真变了则查询失败，由前端轮询容错）。
        """
        ws_url = (url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
                  + "/ws")
        new_client = ComfyClient(url)
        new_oi = ObjectInfoCache(new_client)
        await new_oi.refresh()  # 失败不阻塞：ComfyUI 可能后启动，WS 连上会自愈刷新
        new_ws = ComfyWSListener(ws_url)
        new_ws.on_connected(new_oi.refresh)
        new_ws.on_queue_status(self.tasks._on_queue_status)
        await self.ws.stop()
        old_client = self.client
        self.client = new_client
        self.object_info = new_oi
        self.ws = new_ws
        self.tasks.client = new_client
        self.tasks.object_info = new_oi
        self.tasks.ws = new_ws
        new_ws.start()
        await old_client.close()
