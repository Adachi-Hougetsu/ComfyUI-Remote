"""工作流自动导入：扫描来源目录 → 生成 schema → 校验 → 注册。

- GET    /api/workflows             扫描 ComfyUI 工作流目录，标注已导入/可导入
- POST   /api/templates/import      {filename, id?, overwrite?} 导入并注册模板
- DELETE /api/templates/{id}        注销模板（删注册 + schema + workflow + session）

导入流程（幂等、确定性）：读源 workflow → generate_schema（识别引擎）→ 交叉校验 +
转换 dry-run（每组开关两态）全绿才落盘；覆盖已有 schema 前先备份 .bak。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

from config import COMFY_INPUT_DIR, DATA_DIR, SCHEMAS_DIR, TEMPLATES_DIR
from core import schema_gen as sg
from core.template_store import is_registered, list_templates, register_template, unregister_template
from .state import AppState


def _all_uploaded_files(state) -> set:
    """所有已注册模板会话里用户上传过的文件集合（用于删模板时不误删共享文件）。"""
    files: set[str] = set()
    for t in list_templates():
        sess = state.session.get(t["id"]) or {}
        files.update(f for f in (sess.get("uploads") or {}).values() if f)
    return files


def auto_import_missing(oi: dict | None = None) -> dict:
    """启动时把 ComfyUI 工作流目录里尚未注册的工作流自动导入（幂等、离线安全）。

    - 已注册的绝不动（保护手写 schema，如 amiya）；
    - 无采样器（纯预览草稿）、生成/校验/dry-run 失败的跳过并记录；
    - 目录里已存在 schema 文件但未注册的（孤儿），校验通过就直接用它注册。
    返回 {"imported": [id...], "skipped": [name...], "errors": [...]}。
    """
    src = sg.source_workflows_dir()
    result = {"imported": [], "skipped": [], "errors": []}
    if not src.exists():
        return result
    for f in sorted(src.glob("*.json")):
        sid = sg.sanitize_id(f.name)
        if not sid:
            result["skipped"].append(f"{f.name} (id 为空，跳过)")
            continue
        if is_registered(sid):
            continue
        try:
            ui = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"{f.name}: 读取失败 {e}")
            continue
        try:
            if not sg.count_samplers(ui):
                result["skipped"].append(f"{f.name} (无采样器)")
                continue
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"{f.name}: {type(e).__name__}: {e}")
            continue

        schema_path = SCHEMAS_DIR / f"{sid}.json"
        if schema_path.exists():
            # 孤儿 schema：校验通过直接用，不重新生成（保护手写内容）
            from core.schema import load_schema, validate_against_workflow
            try:
                schema = load_schema(schema_path)
                problems = validate_against_workflow(schema, ui)
            except Exception as e:  # noqa: BLE001
                problems = [str(e)]
            if problems:
                result["errors"].append(f"{f.name}: 既有 schema 校验失败，未注册")
                continue
            _register_from(sid, f.stem, sg.describe(ui))
            result["imported"].append(sid)
            continue

        try:
            raw = sg.generate_schema(ui, oi, schema_id=sid, title=f.stem)
            problems = sg.validate_generated(raw, ui)
            if problems:
                raise ValueError("校验失败: " + "; ".join(problems[:8]))
            dr = sg.dry_run(ui, raw)
            if not dr["ok"]:
                raise ValueError("dry-run 失败: " + "; ".join(dr["problems"][:8]))
        except sg.NoSamplerError:
            result["skipped"].append(f"{f.name} (无采样器)")
            continue
        except Exception as e:  # noqa: BLE001
            result["errors"].append(f"{f.name}: {type(e).__name__}: {e}")
            continue

        SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
        schema_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / f.name, TEMPLATES_DIR / f"{sid}.json")
        _register_from(sid, f.stem, sg.describe(ui))
        result["imported"].append(sid)
    return result


def _register_from(sid: str, title: str, description: str) -> None:
    entry = {
        "id": sid,
        "title": title,
        "description": description,
        "workflow": f"{sid}.json",
        "schema": f"{sid}.json",
    }
    register_template(entry, overwrite=False)


def router(state: AppState) -> APIRouter:
    r = APIRouter(tags=["imports"])

    @r.get("/api/workflows")
    def list_source_workflows():
        src = sg.source_workflows_dir()
        out = []
        for f in sorted(src.glob("*.json")):
            sid = sg.sanitize_id(f.name)
            registered = is_registered(sid)
            has_schema = (SCHEMAS_DIR / f"{sid}.json").exists()
            ui = json.loads(f.read_text(encoding="utf-8"))
            try:
                samplers = sg.count_samplers(ui)
                desc = sg.describe(ui)
            except Exception as e:  # noqa: BLE001
                samplers, desc = 0, f"解析失败: {e}"
            out.append({
                "filename": f.name,
                "id": sid,
                "title": f.stem,
                "samplers": samplers,
                "description": desc,
                "registered": registered,
                "has_schema": has_schema,
            })
        return {"directory": str(src), "workflows": out}

    @r.post("/api/templates/import")
    def import_template(body: dict = Body(...)):
        filename = body.get("filename")
        if not filename:
            raise HTTPException(422, "缺少 filename")
        src_dir = sg.source_workflows_dir()
        src = src_dir / filename
        if not src.exists():
            raise HTTPException(404, f"来源工作流不存在: {filename}")

        sid = body.get("id") or sg.sanitize_id(filename)
        if not sid:
            raise HTTPException(422, "无法从文件名生成模板 id（含非 ASCII？）请手动指定 id")
        overwrite = bool(body.get("overwrite", False))

        # 权威存在性检查在 register_template 的锁内；这里只做快速 409 提示
        if is_registered(sid) and not overwrite:
            raise HTTPException(409, f"模板 {sid} 已导入（用 overwrite=true 覆盖，注意会重生成面板）")

        ui = json.loads(src.read_text(encoding="utf-8"))
        oi = state.object_info.data() if state.object_info.ready else None
        try:
            raw = sg.generate_schema(ui, oi,
                                     schema_id=sid, title=body.get("title") or src.stem)
        except sg.NoSamplerError as e:
            raise HTTPException(422, f"{filename} 无法导入：{e}")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, f"{filename} 生成 schema 失败: {type(e).__name__}: {e}")

        problems = sg.validate_generated(raw, ui)
        if problems:
            raise HTTPException(422, "生成的 schema 与 workflow 不匹配:\n" + "\n".join(problems[:12]))
        dr = sg.dry_run(ui, raw)
        if not dr["ok"]:
            raise HTTPException(422, "导入前 dry-run 校验失败:\n" + "\n".join(dr["problems"][:12]))

        # 落盘：备份既有 schema → 写 schema + workflow → 原子注册
        schema_path = SCHEMAS_DIR / f"{sid}.json"
        backup = None
        if schema_path.exists() and overwrite:
            backup = schema_path.with_suffix(".json.bak")
            shutil.copy2(schema_path, backup)
        SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
        schema_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, TEMPLATES_DIR / f"{sid}.json")

        entry = {
            "id": sid,
            "title": body.get("title") or raw["title"],
            "description": sg.describe(ui),
            "workflow": f"{sid}.json",
            "schema": f"{sid}.json",
        }
        ok = register_template(entry, overwrite=overwrite)
        if not ok:
            # 竞态：另一请求抢先注册（文件已写，内容确定性一致，可接受）
            raise HTTPException(409, f"模板 {sid} 刚被并发注册，请刷新")

        return {
            "ok": True,
            "id": sid,
            "title": entry["title"],
            "description": entry["description"],
            "overwrote": overwrite,
            "backup": str(backup) if backup else None,
            "groups": [{"key": g["key"], "mode": g["mode"], "label": g["label"]}
                       for g in raw.get("node_groups", [])],
            "group_notes": raw.get("group_notes", []),
            "diagnostics": dr["diagnostics"],
        }

    @r.delete("/api/templates/{tpl_id}")
    def delete_template(tpl_id: str):
        if not is_registered(tpl_id):
            raise HTTPException(404, f"模板 {tpl_id} 未导入")
        # 清理该模板用户上传的参考图（先读 session 再删文件/缓存）
        sess = state.session.get(tpl_id) or {}
        uploaded = {f for f in (sess.get("uploads") or {}).values() if f}
        unregister_template(tpl_id)
        # 内存缓存也要清，否则删后 get(tpl) 会返回陈旧会话
        state.session.delete(tpl_id)
        removed = []
        for p in (SCHEMAS_DIR / f"{tpl_id}.json",
                  TEMPLATES_DIR / f"{tpl_id}.json",
                  DATA_DIR / f"session_{tpl_id}.json"):
            if p.exists():
                p.unlink()
                removed.append(str(p))
        # 删除仍被其它模板 uploads 引用的文件是危险的，这里只删本模板独占的上传文件
        others = _all_uploaded_files(state)
        for f in sorted(uploaded - others):
            target = COMFY_INPUT_DIR / f
            try:
                if target.exists():
                    target.unlink()
                    removed.append(str(target))
            except OSError:
                pass
        return {"ok": True, "removed": removed}

    return r
