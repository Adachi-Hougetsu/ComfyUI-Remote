"""到 ComfyUI 的 WebSocket 监听（常驻、自动重连、按 prompt_id 分发）。

/Prompt 提交时带同一 client_id，执行消息只推给本 sid。
断线自动重连；断线期间的进度由 tasks 用 /history 兜底。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid

import websockets

from config import COMFY_WS

log = logging.getLogger(__name__)


class ComfyWSListener:
    def __init__(self, ws_url: str = COMFY_WS):
        self.ws_url = ws_url
        self.client_id = uuid.uuid4().hex
        self._subs: dict[str, set] = {}
        self._queue_handlers: list = []
        self._connected_handlers: list = []
        self._stop = False
        self._task: asyncio.Task | None = None

    def subscribe(self, prompt_id: str, handler) -> None:
        self._subs.setdefault(prompt_id, set()).add(handler)

    def on_queue_status(self, handler) -> None:
        self._queue_handlers.append(handler)

    def on_connected(self, handler) -> None:
        """连上 ComfyUI 时回调（async）。用于自愈：ComfyUI 后启动时刷新 object_info。"""
        self._connected_handlers.append(handler)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while not self._stop:
            try:
                async with websockets.connect(f"{self.ws_url}?clientId={self.client_id}",
                                              max_size=None, ping_interval=20, ping_timeout=20) as ws:
                    await self._hello(ws)
                    for h in list(self._connected_handlers):
                        try:
                            await h()
                        except Exception:
                            log.exception("connected handler 异常")
                    async for raw in ws:
                        await self._handle(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning(f"ComfyUI WS 断开，2s 后重连: {e}")
                await asyncio.sleep(2)

    async def _hello(self, ws) -> None:
        # v0.30 协议：首条消息做 feature_flags 协商
        await ws.send(json.dumps({"type": "feature_flags", "data": {"supports_preview_metadata": True}}))

    async def _handle(self, raw) -> None:
        try:
            msg = json.loads(raw)
        except Exception:
            return
        mtype = msg.get("type")
        data = msg.get("data") or {}
        if mtype == "status":
            for h in list(self._queue_handlers):
                try:
                    await h(data)
                except Exception:
                    log.exception("queue status handler 异常")
            return
        pid = data.get("prompt_id")
        if pid and pid in self._subs:
            for h in list(self._subs.get(pid, ())):
                try:
                    await h(msg)
                except Exception:
                    log.exception("WS handler 异常")
