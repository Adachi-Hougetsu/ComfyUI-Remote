"""运行时设置：ComfyUI 地址热配置（设置页可改，免改代码重启）。

- GET /api/settings  → {comfy_url}
- PUT /api/settings  {comfy_url} → 校验 → 落盘 data/settings.json → 热重配连接
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Body, HTTPException

from config import DATA_DIR
from .state import AppState


def router(state: AppState) -> APIRouter:
    r = APIRouter(prefix="/api", tags=["settings"])

    @r.get("/settings")
    def get_settings():
        return {"comfy_url": state.client.base_url}

    @r.put("/settings")
    async def put_settings(body: dict = Body(...)):
        url = str(body.get("comfy_url") or "").strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise HTTPException(422, "地址必须以 http:// 或 https:// 开头")
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            (DATA_DIR / "settings.json").write_text(
                json.dumps({"comfy_url": url}, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError as e:
            raise HTTPException(500, f"保存设置失败: {e}")
        await state.reconfigure_comfy(url)
        return {"ok": True, "comfy_url": url}

    return r
