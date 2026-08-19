"""运行时设置：ComfyUI 地址 / 访问令牌 热配置（设置页可改，免改代码重启）。

- GET /api/settings  → {comfy_url, access_token_set}
- PUT /api/settings  {comfy_url?} {access_token?} → 校验 → 落盘 data/settings.json → 热应用
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Body, HTTPException

import config
from config import DATA_DIR
from .state import AppState


def _load_settings() -> dict:
    try:
        p = DATA_DIR / "settings.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def router(state: AppState) -> APIRouter:
    r = APIRouter(prefix="/api", tags=["settings"])

    @r.get("/settings")
    def get_settings():
        return {
            "comfy_url": state.client.base_url,
            "access_token_set": bool(config.ACCESS_TOKEN),
        }

    @r.put("/settings")
    async def put_settings(body: dict = Body(...)):
        data = _load_settings()
        changed = False

        if "comfy_url" in body:
            url = str(body.get("comfy_url") or "").strip().rstrip("/")
            if not url.startswith(("http://", "https://")):
                raise HTTPException(422, "地址必须以 http:// 或 https:// 开头")
            data["comfy_url"] = url
            changed = True

        if "access_token" in body:
            token = str(body.get("access_token") or "").strip()
            if token and len(token) < 8:
                raise HTTPException(422, "令牌至少 8 位")
            data["access_token"] = token
            changed = True

        if not changed:
            raise HTTPException(422, "没有可保存的设置")

        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            (DATA_DIR / "settings.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:
            raise HTTPException(500, f"保存设置失败: {e}")

        # 热应用：地址变化重建连接；令牌立即生效（旧令牌即刻失效）
        if "comfy_url" in data and data.get("comfy_url") != state.client.base_url:
            await state.reconfigure_comfy(data["comfy_url"])
        if "access_token" in body:
            config.set_access_token(data.get("access_token", ""))

        return {"ok": True, "comfy_url": data.get("comfy_url", state.client.base_url)}

    return r
