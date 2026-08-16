"""UI 格式 workflow → API prompt 格式转换器（核心）。

无状态、幂等：每次从原始模板重建"有效图"，依次处理
mute/bypass/节点群开关（patch 补线）/LoRA 链/参数覆盖，再序列化为 POST /prompt 的 API 格式。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .graph import Graph, apply_patches, bypass_rewire
from .ids import IdAllocator
from .lora_chain import apply_lora_chain


# ---------------------------------------------------------------------------
# 输入选项
# ---------------------------------------------------------------------------

@dataclass
class ConvertOptions:
    params: dict = field(default_factory=dict)          # schema key → value（含 loras 列表）
    enabled_groups: dict = field(default_factory=dict)  # group key → bool
    image_slots: dict = field(default_factory=dict)     # slot key → 已上传文件名
    seed: Optional[int] = None                          # 本次运行固定种子（抽卡由调用方填进 params）
    seed_rng: Optional[Callable[[], int]] = None        # 预留：随机种子生成器


# ---------------------------------------------------------------------------
# 序列化辅助
# ---------------------------------------------------------------------------

def align_widget_values(node: dict) -> dict:
    """把 widgets_values 对齐到各 widget 输入名。

    规则（已对照 amiya.json 全部节点验证）：
    - 按 inputs[] 顺序消费带 widget 的输入
    - seed 的 control_after_generate 占位：显式条目（前端声明或 _ensure_widget_entries 补全）
      由它自己消费；否则按"下一值是字符串"启发式跳过
    - upload / IMAGEUPLOAD 占位跳过、不上送
    - 尾部多余值（如 CLIPTextEncode 的 [false,true]）截断
    """
    wv = node.get("widgets_values") or []
    vals = {}
    idx = 0
    # 显式 control_after_generate 条目存在时（前端声明 / 注册表补全），由它消费占位；
    # 此时 seed 的启发式跳位会叠加错位，须禁用。
    has_explicit_cag = any(i.get("name") == "control_after_generate" and i.get("widget")
                          for i in node.get("inputs", []))
    for inp in node.get("inputs", []):
        if not inp.get("widget"):
            continue
        name = inp.get("name")
        typ = inp.get("type", "")
        if name == "upload" or typ == "IMAGEUPLOAD":
            idx += 1
            continue
        if idx >= len(wv):
            break
        vals[name] = wv[idx]
        idx += 1
        if name == "seed" and not has_explicit_cag:
            # control_after_generate 占位：仅当下一个值是字符串（"fixed"/"randomize"…）才跳位。
            # 普通 INT 种子（无该占位，如部分自定义节点）若无条件跳位会错位。
            if idx < len(wv) and isinstance(wv[idx], str):
                idx += 1
    # control_after_generate 是前端占位，绝不上送 API；从对齐结果里剔除。
    vals.pop("control_after_generate", None)
    return vals


def _wrap_value(v):
    """数组类 widget 值按 ComfyUI 前端约定包一层 __value__。"""
    if isinstance(v, (list, tuple)):
        return {"__value__": list(v)}
    return v


def serialize(graph: Graph, schema) -> dict:
    """有效图 → API prompt dict。"""
    api = {}
    node_ids = sorted(graph.nodes.keys(), key=lambda nid: graph.nodes[nid].get("order", 0))
    for nid in node_ids:
        node = graph.nodes[nid]
        class_type = node.get("type")
        if not class_type:
            continue
        aligned = align_widget_values(node)
        overrides = node.get("_override", {})
        inputs = {}
        for inp in node.get("inputs", []):
            name = inp.get("name")
            if inp.get("link"):
                l = graph.links.get(inp["link"])
                if l:
                    # ComfyUI 校验用 prompt[o_id] 查来源节点，o_id 必须是 string 键（见 execution.py validate_inputs）
                    inputs[name] = [str(l[1]), l[2]]
                    continue
            if name in overrides:
                inputs[name] = _wrap_value(overrides[name])
            elif name in aligned:
                inputs[name] = _wrap_value(aligned[name])
        title = (node.get("title")
                 or node.get("properties", {}).get("Node name for S&R")
                 or class_type)
        api[str(nid)] = {"class_type": class_type, "inputs": inputs, "_meta": {"title": title}}
    return api


# ---------------------------------------------------------------------------
# 参数覆盖
# ---------------------------------------------------------------------------

def apply_param_overrides(graph: Graph, schema, opts: ConvertOptions) -> None:
    params = opts.params
    if schema.model and "model" in params:
        graph.set_widget(schema.model.node, schema.model.widget, params["model"])
    for key, tf in schema.prompts.items():
        if key in params:
            graph.set_widget(tf.node, tf.widget, params[key])
    for p in schema.params:
        if p.key in params:
            graph.set_widget(p.node, p.widget, params[p.key])
    # 启用节点群的 extra_params
    for g in schema.node_groups:
        enabled = opts.enabled_groups.get(g.key, g.default_enabled)
        if enabled:
            for ep in g.extra_params:
                if ep.key in params:
                    graph.set_widget(ep.node, ep.widget, params[ep.key])
    # 参考图槽 → LoadImage.image
    for slot in schema.image_slots:
        if slot.key in opts.image_slots:
            graph.set_widget(slot.node, "image", opts.image_slots[slot.key])


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def convert_ui_to_api(ui: dict, schema, opts: ConvertOptions, ids: Optional[IdAllocator] = None):
    """返回 (api_prompt, diagnostics)。api_prompt: {str(node_id): {class_type, inputs, _meta}}。"""
    opts.params = opts.params or {}
    opts.enabled_groups = opts.enabled_groups or {}
    opts.image_slots = opts.image_slots or {}
    ids = ids or IdAllocator(ui.get("last_node_id", 0), ui.get("last_link_id", 0))
    graph = Graph(ui, ids)
    diagnostics = {"removed": [], "bypassed": [], "patches": [], "loras": [], "notes": []}

    # 1) 收集剔除/透传集合
    removed = set(schema.drop)
    bypass = set()
    for n in ui.get("nodes", []):
        mode = n.get("mode")
        if mode == 1:
            removed.add(n["id"])
        elif mode == 4:
            bypass.add(n["id"])
    for g in schema.node_groups:
        enabled = opts.enabled_groups.get(g.key, g.default_enabled)
        if g.mode == "module":
            # 模块开关：开 = 激活整框（覆盖源工作流的 bypass 状态，链接原样生效）；
            # 关 = 整框透传（与源状态一致，透传对多入多出按类型配对，安全）。
            for nid in g.nodes:
                removed.discard(nid)
                if enabled:
                    bypass.discard(nid)
                else:
                    bypass.add(nid)
            continue
        if not enabled:
            if g.mode == "bypass" and g.bypass_node:
                # 卫星剔除，bypass_node 走透传
                bypass.add(g.bypass_node)
                for nid in g.nodes:
                    if nid != g.bypass_node:
                        removed.add(nid)
            else:
                for nid in g.nodes:
                    removed.add(nid)

    # 2) 剔除 + bypass 透传（透传作用于未被剔除的原始链路上，见 graph.bypass_rewire）
    for nid in removed:
        graph.remove_node(nid)
    diagnostics["removed"] = sorted(removed)
    bypass -= removed
    bypass_rewire(graph, bypass)
    diagnostics["bypassed"] = sorted(bypass)

    # 3) 补线（OpenPose 启用时把 17 插进 35→3 条件链）
    diagnostics["patches"] = apply_patches(graph, schema, opts)

    # 4) LoRA 链（消费点必须从 group/bypass 处理后的有效图读取）
    loras = opts.params.get("loras") or []
    diagnostics["loras"] = apply_lora_chain(graph, schema, opts, loras)

    # 5) 参数覆盖
    apply_param_overrides(graph, schema, opts)

    # 6) 序列化
    api = serialize(graph, schema)
    return api, diagnostics


def dry_run_template(ui: dict, schema, enabled_groups: dict | None = None) -> list:
    """启动/导入时 dry-run：全部组启用跑一次默认转换，返回一致性告警（空=干净）。

    重点：每个声明参数（model/prompts/params/节点群 extra/image_slots）的 widget 是否
    真实落入序列化输出——widget 对齐错位（如 seed 占位误判、输入被连线接管）会让参数
    静默失效，这里提前暴露而非出图后才察觉。
    """
    warnings = []
    groups_all = {g.key: True for g in schema.node_groups}
    opts = ConvertOptions(
        params={},
        enabled_groups={**groups_all, **(enabled_groups or {})},
        image_slots={},
    )
    try:
        api, _diag = convert_ui_to_api(ui, schema, opts)
    except Exception as e:
        return [f"dry-run 转换抛异常: {e!r}"]

    checks = []
    if schema.model:
        checks.append(("model", schema.model.node, schema.model.widget))
    for key, tf in schema.prompts.items():
        checks.append((f"prompts.{key}", tf.node, tf.widget))
    for p in schema.params:
        checks.append((f"params.{p.key}", p.node, p.widget))
    for g in schema.node_groups:
        for ep in g.extra_params:
            checks.append((f"groups.{g.key}.{ep.key}", ep.node, ep.widget))
    for s in schema.image_slots:
        checks.append((f"image_slots.{s.key}", s.node, "image"))

    for where, node_id, widget in checks:
        nkey = str(node_id)
        if nkey not in api:
            warnings.append(f"[{where}] 节点 {node_id} 不在有效图（组开关/剔除？）")
        elif widget not in api[nkey]["inputs"]:
            warnings.append(f"[{where}] widget '{widget}' 未落入序列化输出（对齐错位或已被连线接管）")
    return warnings
