"""模板面板 schema —— 数据驱动控制面板与接线规则。

一个 schema 文件（schemas/<id>.json）声明：面板长什么样（模型/提示词/参数/开关/参考图槽位）、
每个可编辑项落在哪个 node.widget、节点群开关用什么机制（patch 补线 / bypass 透传）。
转换器据此把参数写进 workflow。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class ModelField:
    node: int
    widget: str
    label: str = "底模"


@dataclass
class LoraChainSpec:
    anchor: dict = field(default_factory=dict)       # {node, model_slot, clip_slot} 链首上游（模型链起点）
    template_lora_chain: list = field(default_factory=list)   # 模板里预置的 LoraLoader 节点 id，作链头
    max: int = 8
    connection: str = "serial"                        # 连接方式（策略注册表键）
    default: list = field(default_factory=list)       # [{name, strength_model, strength_clip}]


@dataclass
class TextField:
    node: int
    widget: str = "text"
    default: str = ""


@dataclass
class ParamField:
    key: str
    node: int
    widget: str
    label: str
    type: str = "float"          # int / float / combo
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    default: Any = None
    options: list = field(default_factory=list)      # combo 显式选项（否则从 object_info 拉）
    seed_mode: Optional[dict] = None                 # {type:"toggle", options:["fixed","random"]}
    extra: dict = field(default_factory=dict)


@dataclass
class LinkOp:
    """一条补线操作：add 或 remove 一条链接。"""
    action: str                  # "add" | "remove"
    origin: list                 # [node_id, slot]
    target: list                 # [node_id, slot]
    link_type: str = "ANY"


@dataclass
class NodeGroup:
    key: str
    label: str
    nodes: list = field(default_factory=list)        # 组内节点（禁用时剔除）
    mode: str = "bypass"          # "patch"（补线）| "bypass"（透传）
    default_enabled: bool = True
    bypass_node: Optional[int] = None                # bypass 模式：被透传的节点
    patches_enable: list = field(default_factory=list)   # list[LinkOp] 启用时执行的补线
    patches_disable: list = field(default_factory=list)  # list[LinkOp] 禁用时执行的补线
    extra_params: list = field(default_factory=list)     # list[ParamField]


@dataclass
class ImageSlot:
    key: str
    label: str
    node: int
    group: Optional[str] = None   # 属于哪个节点群（群关时该槽不展示）


@dataclass
class PanelSchema:
    id: str
    title: str
    workflow: str
    save_output_node: int
    model: Optional[ModelField] = None
    lora: LoraChainSpec = field(default_factory=LoraChainSpec)
    prompts: dict = field(default_factory=dict)      # key -> TextField
    params: list = field(default_factory=list)       # list[ParamField]
    node_groups: list = field(default_factory=list)  # list[NodeGroup]
    image_slots: list = field(default_factory=list)  # list[ImageSlot]
    drop: list = field(default_factory=list)         # 永远从有效图剔除的节点 id（如死节点）
    misc_nodes: list = field(default_factory=list)   # 其它节点展示分组 [{node, title, keys}]（参数在 params 里）
    seed_mode_default: str = "fixed"

    def group(self, key: str) -> Optional[NodeGroup]:
        for g in self.node_groups:
            if g.key == key:
                return g
        return None


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _parse_param(p: dict) -> ParamField:
    return ParamField(
        key=p["key"],
        node=p["node"],
        widget=p["widget"],
        label=p.get("label", p["key"]),
        type=p.get("type", "float"),
        min=p.get("min"),
        max=p.get("max"),
        step=p.get("step"),
        default=p.get("default"),
        options=p.get("options", []),
        seed_mode=p.get("seed_mode"),
        extra={k: v for k, v in p.items() if k not in
               ("key", "node", "widget", "label", "type", "min", "max", "step", "default", "options", "seed_mode")},
    )


def _parse_patch_ops(ops) -> list:
    """补线操作列表（add/remove 同一形状）→ LinkOp。"""
    patches = []
    for op in ops or []:
        if "add" in op:
            patches.append(LinkOp("add", op["add"][0], op["add"][1], op.get("add_type", "ANY")))
        if "remove" in op:
            patches.append(LinkOp("remove", op["remove"][0], op["remove"][1], op.get("remove_type", "ANY")))
    return patches


def _parse_group(g: dict) -> NodeGroup:
    return NodeGroup(
        key=g["key"],
        label=g.get("label", g["key"]),
        nodes=g.get("nodes", []),
        mode=g.get("mode", "bypass"),
        default_enabled=g.get("default_enabled", True),
        bypass_node=g.get("bypass_node"),
        patches_enable=_parse_patch_ops(g.get("patches_enable", [])),
        patches_disable=_parse_patch_ops(g.get("patches_disable", [])),
        extra_params=[_parse_param(p) for p in g.get("extra_params", [])],
    )


def parse_schema(raw: dict) -> PanelSchema:
    """把 schema 字典（生成器输出与手工 JSON 同一形状）解析成 PanelSchema。"""
    return PanelSchema(
        id=raw["id"],
        title=raw.get("title", raw["id"]),
        workflow=raw.get("workflow", ""),
        save_output_node=raw.get("save_output_node", 0),
        model=ModelField(**raw["model"]) if raw.get("model") else None,
        lora=LoraChainSpec(**raw.get("lora", {})),
        prompts={k: TextField(**v) for k, v in raw.get("prompts", {}).items()},
        params=[_parse_param(p) for p in raw.get("params", [])],
        node_groups=[_parse_group(g) for g in raw.get("node_groups", [])],
        image_slots=[ImageSlot(**s) for s in raw.get("image_slots", [])],
        drop=raw.get("drop", []),
        misc_nodes=raw.get("misc_nodes", []),
        seed_mode_default=raw.get("seed_mode_default", "fixed"),
    )


def load_schema(path: Path, apply_overrides: bool = True) -> PanelSchema:
    """加载 schema；若存在同名的 `<stem>.overrides.json` 则先合入（非破坏性）。"""
    raw = _load_json(path)
    if apply_overrides:
        ov_path = path.with_name(path.stem + ".overrides.json")
        if ov_path.exists():
            ov = _load_json(ov_path)
            ov.pop("id", None)   # id 由文件名决定，禁止覆盖
            raw = merge_schema(raw, ov)
    return parse_schema(raw)


# ---------------------------------------------------------------------------
# override 合并（非破坏性：手写 override 补丁合到生成的 schema 上）
# ---------------------------------------------------------------------------

def merge_schema(base: dict, ov: dict) -> dict:
    """override 字典合到 schema 字典上，返回新 dict（不改入参）。

    规则：
    - 顶层整表字段（title/model/prompts/image_slots/drop/seed_mode_default 等）：ov 出现即整表替换
    - lora：按 key 浅合（只盖 ov 里出现的字段）
    - params：按 param key 合（同名覆盖其余字段、新 key 追加）
    - node_groups：按 group key 合；组内 extra_params 再按 param key 合（保留生成组其它字段）
    """
    merged = dict(base)
    for k, v in ov.items():
        if k == "lora" and isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        elif k == "node_groups" and isinstance(v, list):
            merged[k] = _merge_groups(merged.get("node_groups", []), v)
        elif k == "params" and isinstance(v, list):
            merged[k] = _merge_by_key(merged.get("params", []), v, "key")
        else:
            merged[k] = v
    return merged


def _merge_groups(base: list, ov: list) -> list:
    out = [dict(g) for g in base]
    idx = {g["key"]: i for i, g in enumerate(out)}
    for g_ov in ov:
        key = g_ov["key"]
        if key in idx:
            merged = dict(out[idx[key]])
            for k, v in g_ov.items():
                if k == "extra_params" and isinstance(v, list) and isinstance(merged.get("extra_params"), list):
                    merged["extra_params"] = _merge_by_key(merged["extra_params"], v, "key")
                else:
                    merged[k] = v
            out[idx[key]] = merged
        else:
            out.append(dict(g_ov))
            idx[key] = len(out) - 1
    return out


def _merge_by_key(base: list, ov: list, key: str) -> list:
    out = list(base)
    idx = {item[key]: i for i, item in enumerate(out)}
    for item in ov:
        k = item[key]
        if k in idx:
            out[idx[k]] = {**out[idx[k]], **item}
        else:
            out.append(item)
            idx[k] = len(out) - 1
    return out


# ---------------------------------------------------------------------------
# 位置 widget 名注册表（共享数据）
# ---------------------------------------------------------------------------
# 某些自定义节点（efficiency 系）把 widget 值存进 widgets_values 却不把这些 widget
# 声明进 inputs[]（节点包未安装时前端尤甚，见 stress_test2）。按注册表顺序补全后，
# 生成器/校验器/align_widget_values/serialize 都能按名识别。顺序即 widgets_values
# 的位置，绝不能重排既有 inputs。生成（schema_gen）与加载校验（validate_against_workflow）
# 同读这一份，保证 schema 里引用的 widget 在磁盘 workflow 上"逻辑存在"。
_WIDGET_ORDER = {
    "KSampler (Efficient)": [
        "seed", "control_after_generate", "steps", "cfg",
        "sampler_name", "scheduler", "denoise", "preview_method", "vae_decode",
    ],
    "KSampler Adv. (Efficient)": [
        "seed", "control_after_generate", "steps", "cfg",
        "sampler_name", "scheduler", "start_at_step", "end_at_step",
    ],
}


def ensure_widget_entries(node: dict) -> None:
    """把 _WIDGET_ORDER 里缺的 widget 输入就地补进 node['inputs']（按位置顺序追加）。

    就地修改：图索引持有的是 ui 的节点引用，转换器/校验/序列化读同一份 ui，
    补全后全链路可见。已在 inputs[] 声明过的名字跳过（避免重复消费 widgets_values）。
    """
    order = _WIDGET_ORDER.get(node.get("type"))
    if not order:
        return
    declared = {i.get("name") for i in node.get("inputs", [])}
    for name in order:
        if name in declared:
            continue
        node["inputs"].append({"name": name, "type": "INT", "widget": {"name": name}, "link": None})


# ---------------------------------------------------------------------------
# 与 workflow 交叉校验（启动时执行，防静默错值）
# ---------------------------------------------------------------------------

def _node_input_names(node: dict) -> set:
    return {i.get("name") for i in node.get("inputs", []) if i.get("name")}


def _node_linked_inputs(node: dict) -> set:
    """模板里已接线的输入名：写入 widget 值会被 serialize 的 link 分支覆盖，等于静默无效。"""
    return {i.get("name") for i in node.get("inputs", []) if i.get("name") and i.get("link")}


def validate_against_workflow(schema: PanelSchema, ui: dict) -> list:
    """校验 schema 声明的 node/widget 在 workflow 里真实存在且可写。返回问题列表（空=通过）。"""
    problems = []
    nodes = {n["id"]: n for n in ui.get("nodes", [])}
    # efficiency 系节点把 widget 值存 widgets_values 而不声明进 inputs[]，先按注册表
    # 补全，校验才能识别这些 widget（与生成器同源，见 ensure_widget_entries）。
    for n in nodes.values():
        ensure_widget_entries(n)

    def check(node_id, widget, where):
        node = nodes.get(node_id)
        if node is None:
            problems.append(f"[{where}] 节点 {node_id} 不存在于 workflow")
            return
        if not widget:
            return
        if widget not in _node_input_names(node):
            problems.append(f"[{where}] 节点 {node_id}({node.get('type')}) 无 widget 输入 '{widget}'")
        elif widget in _node_linked_inputs(node):
            problems.append(f"[{where}] 节点 {node_id}({node.get('type')}) 的 '{widget}' 输入已由连线接管，写入会被忽略")

    if schema.model:
        check(schema.model.node, schema.model.widget, "model")
    # LoRA 链：链首锚点节点 + 模板预置 LoraLoader 链头必须真实存在
    if schema.lora:
        anchor_node = (schema.lora.anchor or {}).get("node")
        if anchor_node is not None and anchor_node not in nodes:
            problems.append(f"[lora.anchor] 节点 {anchor_node} 不存在")
        for nid in schema.lora.template_lora_chain or []:
            if nid not in nodes:
                problems.append(f"[lora.template_lora_chain] 节点 {nid} 不存在")
    for key, tf in schema.prompts.items():
        check(tf.node, tf.widget, f"prompts.{key}")
    for p in schema.params:
        check(p.node, p.widget, f"params.{p.key}")
    for g in schema.node_groups:
        for nid in g.nodes:
            if nid not in nodes:
                problems.append(f"[node_groups.{g.key}] 节点 {nid} 不存在")
        if g.mode == "bypass" and g.bypass_node and g.bypass_node not in nodes:
            problems.append(f"[node_groups.{g.key}] bypass_node {g.bypass_node} 不存在")
        for ep in g.extra_params:
            check(ep.node, ep.widget, f"node_groups.{g.key}.extra_params.{ep.key}")
        for op in g.patches_enable + g.patches_disable:
            for nid, _slot in (op.origin, op.target):
                if nid not in nodes:
                    problems.append(f"[node_groups.{g.key}.patches] 节点 {nid} 不存在")
    for s in schema.image_slots:
        check(s.node, "image", f"image_slots.{s.key}")
    for nid in schema.drop:
        if nid not in nodes:
            problems.append(f"[drop] 节点 {nid} 不存在")
    # misc_nodes：纯展示分组，引用真实节点 + 已声明的参数 key
    param_keys = {p.key for p in schema.params}
    for m in schema.misc_nodes:
        if m.get("node") not in nodes:
            problems.append(f"[misc_nodes] 节点 {m.get('node')} 不存在")
        for k in m.get("keys", []):
            if k not in param_keys:
                problems.append(f"[misc_nodes] key {k} 未在 params 中声明")
    return problems
