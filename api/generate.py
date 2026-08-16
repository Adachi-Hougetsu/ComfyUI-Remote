"""生成：健康检查 / 提交批次 / 批次状态 / 取消。

- GET    /api/health               存活 + ComfyUI 连通性
- POST   /api/generate             提交一批（runs=N 随机种子）
- GET    /api/generate/{batch_id}  批次状态
- DELETE /api/generate/{batch_id}  取消
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Body

from config import COMFY_INPUT_DIR, COMFY_URL
from core.template_store import list_templates, load_template
from .state import AppState


def router(state: AppState) -> APIRouter:
    r = APIRouter(tags=["generate"])

    @r.get("/api/health")
    async def health():
        stats = await state.client.system_stats()
        queue_remaining = 0
        try:
            q = await state.client.queue()
            queue_remaining = (q.get("queue_running") and len(q["queue_running"]) or 0) \
                + (q.get("queue_pending") and len(q["queue_pending"]) or 0)
        except Exception:
            pass
        return {
            "ok": True,
            "comfy": {
                "ok": bool(stats),
                "url": COMFY_URL,
                "queue_remaining": queue_remaining,
                "system_stats": stats,
                "object_info_ready": state.object_info.ready,
            },
            "missing_images": _missing_input_files(state),
        }

    @r.post("/api/generate")
    async def generate(body: dict = Body(...)):
        tpl_id = body.get("tpl")
        if not tpl_id:
            raise HTTPException(400, "缺少 tpl")
        try:
            load_template(tpl_id)
        except KeyError:
            raise HTTPException(404, f"模板 {tpl_id} 不存在")
        except ValueError as e:
            raise HTTPException(500, str(e))

        params = body.get("params") or {}
        runs = body.get("runs") or 1
        enabled_groups = body.get("enabled_groups") or {}
        image_slots = body.get("image_slots") or {}

        # 校验非字符串字段类型（防脏数据进转换器）
        try:
            runs = int(runs)
        except (TypeError, ValueError):
            raise HTTPException(400, "runs 必须是整数")

        batch = await state.tasks.submit(
            tpl_id, dict(params), runs,
            enabled_groups=dict(enabled_groups),
            image_slots=dict(image_slots),
        )
        if not batch.prompt_ids:
            raise HTTPException(502, batch.error.get("submit", "提交失败"))
        return {"batch_id": batch.batch_id, "prompt_ids": list(batch.prompt_ids)}

    @r.get("/api/generate/{batch_id}")
    async def batch_status(batch_id: str):
        snap = await state.tasks.reconcile(batch_id)
        if snap is None:
            raise HTTPException(404, f"批次 {batch_id} 不存在")
        return snap

    @r.delete("/api/generate/{batch_id}")
    async def batch_cancel(batch_id: str):
        ok = await state.tasks.cancel(batch_id)
        if not ok:
            raise HTTPException(404, f"批次 {batch_id} 不存在")
        return {"ok": True}

    return r


def _missing_input_files(state) -> list:
    """模板 workflow 与已存会话引用的 input 文件清单里，物理缺失的部分。"""
    if COMFY_INPUT_DIR is None:
        return []
    if not COMFY_INPUT_DIR.exists():
        return ["ComfyUI input 目录不存在，请在 config.py 修正 COMFY_INPUT_DIR"]
    referenced: set[str] = set()
    for t in list_templates():
        try:
            tpl = load_template(t["id"])
        except Exception:
            continue
        for n in tpl.workflow.get("nodes", []):
            if n.get("type") == "LoadImage":
                wv = n.get("widgets_values") or []
                if wv and isinstance(wv[0], str) and wv[0]:
                    referenced.add(wv[0])
        sess = state.session.get(t["id"]) or {}
        for f in (sess.get("image_slots") or {}).values():
            if f:
                referenced.add(f)
    missing = [f for f in sorted(referenced) if not (COMFY_INPUT_DIR / f).exists()]
    return missing
