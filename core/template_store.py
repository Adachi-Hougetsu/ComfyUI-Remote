"""模板注册表：data/templates.json → (workflow UI json, PanelSchema)。

加第二个模板 = templates.json 加一行 + schemas/ 放一个 schema + templates/ 拷一份 workflow，
零代码改动。
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from config import DATA_DIR, SCHEMAS_DIR, TEMPLATES_DIR
from .schema import PanelSchema, load_schema, validate_against_workflow


@dataclass
class Template:
    id: str
    title: str
    description: str
    workflow_path: Path
    schema: PanelSchema
    workflow: dict


def _load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# 注册表读写（原子，check-then-act 在同一把锁内，杜绝并发重复注册）
_registry_lock = threading.Lock()


def _registry_path() -> Path:
    return DATA_DIR / "templates.json"


def _load_registry() -> list:
    reg_path = _registry_path()
    if not reg_path.exists():
        return []
    return _load_json(reg_path)


def _write_registry(reg: list) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    reg_path = _registry_path()
    tmp = reg_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, reg_path)


def is_registered(tpl_id: str) -> bool:
    return any(t["id"] == tpl_id for t in _load_registry())


def register_template(entry: dict, overwrite: bool = False) -> bool:
    """原子注册：检查-写入在锁内。返回 True=成功；False=id 已存在且未允许覆盖。"""
    with _registry_lock:
        reg = _load_registry()
        exists = any(t["id"] == entry["id"] for t in reg)
        if exists and not overwrite:
            return False
        reg = [t for t in reg if t["id"] != entry["id"]]
        reg.append(entry)
        _write_registry(reg)
        return True


def unregister_template(tpl_id: str) -> bool:
    with _registry_lock:
        reg = _load_registry()
        new = [t for t in reg if t["id"] != tpl_id]
        if len(new) == len(reg):
            return False
        _write_registry(new)
        return True


def list_templates() -> list:
    reg_path = DATA_DIR / "templates.json"
    if not reg_path.exists():
        return []
    reg = _load_json(reg_path)
    return [{"id": t["id"], "title": t["title"], "description": t.get("description", "")} for t in reg]


def load_template(tpl_id: str) -> Template:
    reg_path = DATA_DIR / "templates.json"
    reg = _load_json(reg_path)
    entry = next((t for t in reg if t["id"] == tpl_id), None)
    if entry is None:
        raise KeyError(f"模板 {tpl_id} 不存在")

    workflow_path = Path(entry["workflow"])
    if not workflow_path.is_absolute():
        workflow_path = TEMPLATES_DIR / workflow_path
    schema_path = Path(entry["schema"])
    if not schema_path.is_absolute():
        schema_path = SCHEMAS_DIR / schema_path

    ui = _load_json(workflow_path)
    schema = load_schema(schema_path)
    problems = validate_against_workflow(schema, ui)
    if problems:
        # 启动时发现问题直接抛错，杜绝静默错值
        raise ValueError(f"模板 {tpl_id} schema 校验失败:\n" + "\n".join(problems))

    return Template(
        id=entry["id"],
        title=entry.get("title", schema.title),
        description=entry.get("description", ""),
        workflow_path=workflow_path,
        schema=schema,
        workflow=ui,
    )


def schema_dict(tpl: Template) -> dict:
    """面板定义（前端渲染用）：把 PanelSchema 序列化成 JSON 可表达结构。"""
    s = tpl.schema
    return {
        "id": s.id,
        "title": tpl.title,
        "model": {"node": s.model.node, "widget": s.model.widget, "label": s.model.label} if s.model else None,
        "lora": {
            "max": s.lora.max,
            "connection": s.lora.connection,
            "default": s.lora.default,
        },
        "prompts": {k: {"node": tf.node, "widget": tf.widget} for k, tf in s.prompts.items()},
        "params": [
            {
                "key": p.key, "widget": p.widget, "label": p.label, "type": p.type,
                "min": p.min, "max": p.max, "step": p.step, "default": p.default,
                "options": p.options,
                "seed_mode": p.seed_mode,
            }
            for p in s.params
        ],
        "node_groups": [
            {
                "key": g.key, "label": g.label, "mode": g.mode, "default_enabled": g.default_enabled,
                "extra_params": [
                    {"key": ep.key, "widget": ep.widget, "label": ep.label, "type": ep.type,
                     "min": ep.min, "max": ep.max, "step": ep.step, "default": ep.default,
                     "options": ep.options}
                    for ep in g.extra_params
                ],
            }
            for g in s.node_groups
        ],
        "image_slots": [
            {"key": sl.key, "label": sl.label, "group": sl.group}
            for sl in s.image_slots
        ],
        "misc_nodes": [
            {"node": m["node"], "title": m["title"], "keys": m["keys"]}
            for m in s.misc_nodes
        ],
    }
