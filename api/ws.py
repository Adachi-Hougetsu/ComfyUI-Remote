"""WebSocket 后端→手机推送：/ws 只读事件流。

客户端连上后发 {type:"subscribe", batch_id} 订阅某批次，服务端按订阅扇出：
hello / batch_start / progress / queue / run_done / run_error / batch_done / batch_error。
每条消息 {type, data}。TaskManager 的事件通过 WSHub.broadcast 送达。
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .state import AppState

log = logging.getLogger(__name__)


class WSHub:
    def __init__(self):
        self._clients: dict[WebSocket, set[str]] = {}

    def register(self, ws: WebSocket) -> None:
        self._clients.setdefault(ws, set())

    def unregister(self, ws: WebSocket) -> None:
        self._clients.pop(ws, None)

    async def handle(self, ws: WebSocket, msg: dict) -> None:
        mtype = msg.get("type")
        batch_id = msg.get("batch_id")
        if not batch_id:
            return
        subs = self._clients.setdefault(ws, set())
        if mtype == "subscribe":
            subs.add(batch_id)
        elif mtype == "unsubscribe":
            subs.discard(batch_id)

    async def broadcast(self, batch_id: str, event: str, payload: dict) -> None:
        """TaskManager 事件 handler：发给订阅了该批次的客户端。"""
        if not self._clients:
            return
        dead = []
        for ws, subs in list(self._clients.items()):
            if batch_id not in subs:
                continue
            try:
                await ws.send_json({"type": event, "data": payload})
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.pop(ws, None)

    async def _broadcast_hello(self, ws: WebSocket) -> None:
        try:
            await ws.send_json({
                "type": "hello",
                "data": {"status": "ok", "server": "comfyui-remote"},
            })
        except Exception:
            log.exception("发送 hello 失败")


def router(state: AppState) -> APIRouter:
    hub = WSHub()
    state.tasks.on_event(hub.broadcast)
    r = APIRouter(tags=["ws"])

    @r.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        hub.register(websocket)
        await hub._broadcast_hello(websocket)
        try:
            while True:
                raw = await websocket.receive_json()
                await hub.handle(websocket, raw)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("WS 会话异常")
        finally:
            hub.unregister(websocket)

    return r
