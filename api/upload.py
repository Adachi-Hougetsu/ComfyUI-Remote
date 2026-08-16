"""参考图上传：multipart → 转发 ComfyUI input → 记入会话 image_slots。

- POST /api/upload  字段: file(二进制) + key(槽位 key) [+ tpl 可选]
  → {key, filename}；失败 400/502 {detail}
"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Form

from comfy.client import ComfyError
from .state import AppState

_ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

_CT_BY_EXT = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}


def router(state: AppState) -> APIRouter:
    r = APIRouter(prefix="/api", tags=["upload"])

    @r.post("/upload")
    async def upload(
        file: UploadFile = File(...),
        key: str = Form(...),
        tpl: str | None = Form(None),
    ):
        # 防目录穿越 + 只收图片
        ext = Path(file.filename or "").suffix.lower()
        if ext not in _ALLOWED_EXT:
            raise HTTPException(400, f"不支持的图片类型: {ext or '(无后缀)'}")

        data = await file.read()
        if not data:
            raise HTTPException(400, "空文件")
        if len(data) > 50 * 1024 * 1024:
            raise HTTPException(400, "文件过大（>50MB）")

        sess_uploads = None
        if tpl:
            sess = state.session.get(tpl) or {}
            # 同槽覆盖复用：该槽之前上传过（记录在 uploads 里）就原地覆盖同一文件，
            # 不产生重复文件；工作流默认参考图（未经 uploads 记录）绝不动。
            sess_uploads = dict(sess.get("uploads") or {})
            safe_name = sess_uploads.get(key)
        if not safe_name:
            # 文件名加短随机前缀，避免与 ComfyUI 已有文件重名冲突
            safe_name = uuid.uuid4().hex[:8] + ext
        try:
            resp = await state.client.upload_image(
                data, safe_name, content_type=_CT_BY_EXT.get(ext, "image/png"),
                type_="input", overwrite=True)
        except ComfyError as e:
            raise HTTPException(502, f"转发 ComfyUI 失败: {e}")
        filename = resp.get("name", safe_name)

        # 记入模板会话的 image_slots + uploads（uploads 用于删除模板时清理我们上传的文件）
        if tpl:
            sess = state.session.get(tpl) or {}
            slots = dict(sess.get("image_slots") or {})
            slots[key] = filename
            sess_uploads = dict(sess.get("uploads") or {}) if sess_uploads is None else sess_uploads
            sess_uploads[key] = filename
            state.session.save(tpl, {**sess, "image_slots": slots, "uploads": sess_uploads})

        return {"key": key, "filename": filename}

    return r
