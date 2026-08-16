"""ComfyUI HTTP 客户端封装。"""
from __future__ import annotations

import httpx

from config import COMFY_URL


class ComfyError(Exception):
    def __init__(self, message: str, node_errors: dict | None = None):
        super().__init__(message)
        self.node_errors = node_errors or {}


class ComfyClient:
    def __init__(self, base_url: str = COMFY_URL):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=180.0))

    async def close(self) -> None:
        await self._client.aclose()

    # ---- 生成/队列 ----

    async def post_prompt(self, prompt: dict, client_id: str) -> dict:
        body = {"prompt": prompt, "client_id": client_id}
        try:
            r = await self._client.post(f"{self.base_url}/prompt", json=body)
        except httpx.ConnectError as e:
            raise ComfyError(f"无法连接 ComfyUI（{self.base_url}）: {e}")
        if r.status_code == 200:
            return r.json()
        try:
            data = r.json()
        except Exception:
            data = {}
        err = data.get("error", {})
        raise ComfyError(
            f"提交失败 (HTTP {r.status_code}): {err.get('message', r.text[:300])}",
            node_errors=data.get("node_errors"),
        )

    async def interrupt(self, prompt_id: str | None = None) -> None:
        """全局中断，或 aki fork 的定向中断（带 {prompt_id} body，只停目标执行）。"""
        body = {"prompt_id": prompt_id} if prompt_id else None
        r = await self._client.post(f"{self.base_url}/interrupt", json=body)
        r.raise_for_status()

    async def delete_queue(self, prompt_ids: list) -> None:
        r = await self._client.post(f"{self.base_url}/queue", json={"delete": list(prompt_ids)})
        r.raise_for_status()

    async def queue(self) -> dict:
        r = await self._client.get(f"{self.base_url}/queue")
        r.raise_for_status()
        return r.json()

    async def history(self, prompt_id: str | None = None) -> dict:
        url = f"{self.base_url}/history"
        if prompt_id:
            url += f"/{prompt_id}"
        r = await self._client.get(url)
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()

    # ---- 模型/文件 ----

    async def get_object_info(self, node_class: str | None = None) -> dict:
        url = f"{self.base_url}/object_info"
        if node_class:
            url += f"/{node_class}"
        r = await self._client.get(url)
        r.raise_for_status()
        return r.json()

    async def system_stats(self) -> dict:
        try:
            r = await self._client.get(f"{self.base_url}/system_stats")
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return {}

    # ---- 图片 ----

    async def upload_image(self, data: bytes, filename: str, content_type: str = "image/png",
                           subfolder: str = "", type_: str = "input", overwrite: bool = True) -> dict:
        files = {"image": (filename, data, content_type)}
        params = {"type": type_, "subfolder": subfolder,
                  "overwrite": "true" if overwrite else "false"}
        r = await self._client.post(f"{self.base_url}/upload/image", files=files, params=params)
        r.raise_for_status()
        return r.json()  # {name, subfolder, type}

    async def view_image(self, filename: str, subfolder: str = "", type_: str = "output",
                         preview: str = "webp;80") -> tuple[bytes, str]:
        params = {"filename": filename, "subfolder": subfolder, "type": type_}
        if preview:
            params["preview"] = preview
        r = await self._client.get(f"{self.base_url}/view", params=params)
        if r.status_code == 404:
            raise ComfyError(f"图片不存在: {filename}")
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "image/webp")
