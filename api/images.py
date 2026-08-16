"""图片代理：手机不直连 8188，统一走控制层。

- GET /api/images/{filename}?subfolder=&type=output → WebP 缩略图字节
- GET /api/images/{filename}?download=1 → 原图附件下载（Content-Disposition）
"""
from __future__ import annotations

from urllib.parse import quote, unquote

from fastapi import APIRouter, HTTPException, Response

from comfy.client import ComfyError
from .state import AppState


def router(state: AppState) -> APIRouter:
    r = APIRouter(prefix="/api/images", tags=["images"])

    @r.get("/{filename}")
    async def image(filename: str, subfolder: str = "", type: str = "output", download: int = 0):
        # 防目录穿越：去掉路径分隔符，只留文件名
        filename = unquote(filename).replace("\\", "/").rsplit("/", 1)[-1]
        if not filename:
            raise HTTPException(400, "非法文件名")
        try:
            if download:
                # 原图：不走 webp 预览，附件下载（文件名带 UTF-8 百分号编码兼容中文）
                data, content_type = await state.client.view_image(
                    filename, subfolder=subfolder, type_=type, preview="")
            else:
                data, content_type = await state.client.view_image(
                    filename, subfolder=subfolder, type_=type, preview="webp;80")
        except ComfyError as e:
            raise HTTPException(404, str(e))
        headers = {}
        if download:
            ascii_name = filename.encode("ascii", "ignore").decode() or "download"
            headers["Content-Disposition"] = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quote(filename)}'
        return Response(content=data, media_type=content_type, headers=headers)

    return r
