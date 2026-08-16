"""图库：历史生成记录。

- GET /api/gallery?limit=50&tpl=<模板id> → [{batch_id, prompt_id, tpl_id, time, params, images}]
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from .state import AppState


def router(state: AppState) -> APIRouter:
    r = APIRouter(prefix="/api", tags=["gallery"])

    @r.get("/gallery")
    def gallery(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                tpl: str | None = Query(None)):
        return state.tasks.gallery(limit, offset, tpl_id=tpl)

    return r
