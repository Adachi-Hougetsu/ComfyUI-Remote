"""面板相关：模板列表 / 面板定义 / 模型下拉 / 参数读写。

- GET  /api/templates                    模板列表
- GET  /api/templates/{tpl}              面板定义 + 当前已存值
- GET  /api/templates/{tpl}/model-lists  一次取齐所有 combo 选项
- GET/PUT /api/templates/{tpl}/params    读/存当前参数
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Body

from config import MAX_RUNS
from core.template_store import list_templates, load_template, schema_dict
from .state import AppState


def router(state: AppState) -> APIRouter:
    r = APIRouter(prefix="/api/templates", tags=["panel"])

    @r.get("")
    def templates():
        return list_templates()

    @r.get("/{tpl}")
    def panel_detail(tpl: str):
        try:
            template = load_template(tpl)
        except KeyError:
            raise HTTPException(404, f"模板 {tpl} 不存在")
        except ValueError as e:
            raise HTTPException(500, str(e))
        return {"schema": schema_dict(template), "values": values(tpl, state.session)}

    @r.get("/{tpl}/model-lists")
    async def model_lists(tpl: str, refresh: bool = False):
        try:
            template = load_template(tpl)
        except KeyError:
            raise HTTPException(404, f"模板 {tpl} 不存在")
        if refresh or not state.object_info.ready:
            # ?refresh=1 手动强制重读：ComfyUI 里新放的底模/LoRA 立即出现在下拉，
            # 不用等 300s 周期刷新；刷新失败时 object_info 保留旧缓存（掉线不空下拉）
            await state.object_info.refresh()
        return _build_model_lists(template, state)

    @r.get("/{tpl}/params")
    def get_params(tpl: str):
        try:
            load_template(tpl)
        except KeyError:
            raise HTTPException(404, f"模板 {tpl} 不存在")
        return values(tpl, state.session)

    @r.put("/{tpl}/params")
    def put_params(tpl: str, body: dict = Body(...)):
        try:
            load_template(tpl)
        except KeyError:
            raise HTTPException(404, f"模板 {tpl} 不存在")
        clean = _sanitize_values(body)
        # uploads 由上传流程维护，PUT 面板参数不能覆盖它（save 是整体替换）
        existing = state.session.get(tpl) or {}
        clean["uploads"] = existing.get("uploads") or {}
        state.session.save(tpl, clean)
        return {"ok": True}

    return r


# ---------------------------------------------------------------------------
# 参数结构（前后端契约）
# ---------------------------------------------------------------------------
# values: {model, loras, prompts, params, groups, image_slots}
#   - model: 底模文件名
#   - loras: [{name, strength_model, strength_clip}]
#   - prompts: {positive, negative}
#   - params: {steps, cfg, sampler_name, scheduler, denoise, width, height, seed, seed_mode}
#   - groups: {openpose: bool, ipa: bool}
#   - image_slots: {key: 文件名}


def _slot_default(tpl, slot) -> Optional[str]:
    """槽位当前引用图：工作流 LoadImage 节点里已填的文件名（参考图随工作流同步）。

    识别引擎生成的 image_slots 只声明 node，不存当前图；这里从原始工作流读
    widgets_values[0]，让手机端直接显示工作流已有的参考图，而不是要求重新上传。
    """
    for n in tpl.workflow.get("nodes", []):
        if n.get("id") == slot.node:
            wv = n.get("widgets_values") or []
            if wv and isinstance(wv[0], str) and wv[0]:
                return wv[0]
    return None


def _default_values(tpl) -> dict:
    s = tpl.schema
    slot_defaults = {sl.key: _slot_default(tpl, sl) for sl in s.image_slots}
    values = {
        "model": s.model.widget if s.model else None,
        "loras": list(s.lora.default),
        "prompts": {k: tf.default for k, tf in s.prompts.items()},
        "params": {},
        "groups": {g.key: g.default_enabled for g in s.node_groups},
        "image_slots": {k: v for k, v in slot_defaults.items() if v},
        "runs": 1,
    }
    for p in s.params:
        if p.seed_mode:
            values["params"][p.key] = p.default if p.default is not None else 0
            # 默认 seed_mode 与 schema.seed_mode_default 对齐（抽卡默认 random）
            dflt = getattr(s, "seed_mode_default", None)
            values["params"]["seed_mode"] = dflt if dflt in p.seed_mode["options"] else p.seed_mode["options"][0]
        elif p.type == "combo":
            values["params"][p.key] = p.options[0] if p.options else p.default
        else:
            values["params"][p.key] = p.default
    for g in s.node_groups:
        for ep in g.extra_params:
            if ep.type == "combo":
                values["params"][ep.key] = ep.options[0] if ep.options else ep.default
            else:
                values["params"][ep.key] = ep.default
    return values


def values(tpl_id: str, session_store) -> dict:
    """当前已存值（默认值 + 会话覆盖）。

    image_slots 按 key 深合并：用户只传过 ipa_ref，openpose/compose 仍回落到
    工作流默认参考图，而不是整个槽位字典被会话替换成空。
    """
    tpl = load_template(tpl_id)
    defaults = _default_values(tpl)
    saved = session_store.get(tpl_id) or {}
    merged = {k: saved.get(k, v) for k, v in defaults.items()}
    merged["params"] = {**defaults["params"], **(saved.get("params") or {})}
    merged["image_slots"] = {**defaults.get("image_slots", {}), **(saved.get("image_slots") or {})}
    return merged


def _sanitize_values(body: dict) -> dict:
    """只保留已知字段，防脏数据写盘。"""
    keys = ("model", "loras", "prompts", "params", "groups", "image_slots", "runs")
    out = {k: body.get(k) for k in keys if k in body}
    if "loras" in out and not isinstance(out["loras"], list):
        out["loras"] = []
    if "runs" in out:
        try:
            out["runs"] = max(1, min(int(out["runs"]), MAX_RUNS))
        except (TypeError, ValueError):
            out.pop("runs", None)
    return out


# ---------------------------------------------------------------------------
# 模型下拉
# ---------------------------------------------------------------------------

def _build_model_lists(template, state) -> dict:
    s = template.schema
    oi = state.object_info
    # 需要拉取的 combo widget 名
    wanted = {"ckpt_name", "lora_name", "sampler_name", "scheduler",
              "ipadapter_file", "clip_name"}
    if s.model:
        wanted.add(s.model.widget)
    for p in s.params:
        if p.type == "combo":
            wanted.add(p.widget)
    for g in s.node_groups:
        for ep in g.extra_params:
            if ep.type == "combo":
                wanted.add(ep.widget)

    # widget 名 → 第一个带该输入名的节点类型
    node_type_by_widget: dict[str, str] = {}
    for n in template.workflow.get("nodes", []):
        for inp in n.get("inputs", []):
            name = inp.get("name")
            if name in wanted and name not in node_type_by_widget:
                node_type_by_widget[name] = n.get("type", "")

    out = {}
    for wname in wanted:
        ctype = node_type_by_widget.get(wname)
        out[wname] = oi.combo_options(ctype, wname) if ctype else []
    return out
