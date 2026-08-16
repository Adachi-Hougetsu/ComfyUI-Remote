"""LoRA 链式插入 + 连接策略接口。

模板预置一个 LoraLoader（链头，如 amiya.json 的 node10）。用户挂多个 LoRA 时，
列表第一个落在链头（更新其 widget），其余逐个新建 LoraLoader 节点串联插入链尾，
链尾输出接回有效图里的消费点（IPA 开→[43,0]/[47,0]，IPA 关→[3,0]/[47,0]，
消费点必须从 group/bypass 处理后的有效图读取）。

连接方式声明化：schema.lora.connection 是策略注册表键。MVP 实现 serial；
将来加 model_parallel 等只加类不动主流程。
"""
from __future__ import annotations

from .graph import Graph, bypass_rewire


class LoraConnectionStrategy:
    """连接策略接口。ctx 提供图与 schema。"""
    def apply(self, graph: Graph, schema, loras: list) -> list:
        raise NotImplementedError


class SerialLoraStrategy(LoraConnectionStrategy):
    def apply(self, graph: Graph, schema, loras: list) -> list:
        return _insert_serial(graph, schema, loras)


STRATEGIES = {
    "serial": SerialLoraStrategy,
}


def apply_lora_chain(graph: Graph, schema, opts, loras: list) -> list:
    """把用户 LoRA 列表应用到有效图。返回新增节点 id 列表。"""
    lora_spec = schema.lora
    if not lora_spec or not lora_spec.template_lora_chain:
        return []
    cls = STRATEGIES.get(lora_spec.connection, SerialLoraStrategy)
    return cls().apply(graph, schema, loras)


def _insert_serial(graph: Graph, schema, loras: list) -> list:
    lora_spec = schema.lora
    template_ids = [nid for nid in lora_spec.template_lora_chain if nid in graph.nodes]
    added = []

    # 情况 1：用户清空 LoRA → 全部模板 LoRA 透传（锚点直连消费点）
    if not loras:
        if template_ids:
            bypass_rewire(graph, template_ids)
        return added

    # 情况 2：非空。位置映射：用户列表 [A,B,C] 按应用顺序对上模板链 [T1,T2]，
    #   T1←A、T2←B；模板多余的透传（透明），用户多余的新建节点插到链尾。
    n_tpl, n_usr = len(template_ids), len(loras)

    # 2a. 模板链承载用户前 n_tpl 个（原地更新 widget）
    for i in range(min(n_tpl, n_usr)):
        tid = template_ids[i]
        slot = loras[i]
        graph.set_widget(tid, "lora_name", slot["name"])
        graph.set_widget(tid, "strength_model", slot.get("strength_model", 0.8))
        graph.set_widget(tid, "strength_clip", slot.get("strength_clip", 1.0))

    # 2b. 模板链比用户列表长 → 多出的模板 LoRA 透传移除
    leftover = template_ids[n_usr:] if n_usr < n_tpl else []
    if leftover:
        bypass_rewire(graph, leftover)

    # 2c. 用户列表比模板链长 → 多出的新建节点插到链尾
    extras = loras[n_tpl:] if n_usr > n_tpl else []
    if not extras:
        return added  # 模板链已能承载全部用户 LoRA，无需动链

    # 链尾 = 最后一个保留的模板节点（n_usr>=1 时它一定在：更新循环保住它）；
    # 模板链为空（异常模板）→ 以锚点（checkpoint）当上游。
    tail_id = template_ids[min(n_tpl, n_usr) - 1] if template_ids else None
    consumers_model, consumers_clip = [], []
    if tail_id is not None and tail_id in graph.nodes:
        tail_node = graph.nodes[tail_id]
        for out in tail_node.get("outputs", []):
            for lid in list(out.get("links", [])):
                l = graph.links.get(lid)
                if l is None:
                    continue
                (_, _oid, oslot, tid, tslot, ltype) = l
                graph.remove_link(lid)
                bucket = consumers_model if oslot == 0 else consumers_clip
                bucket.append((tid, tslot, ltype))
        prev_model = (tail_id, 0)
        prev_clip = (tail_id, 1)
    else:
        anchor = lora_spec.anchor or {"node": None, "model_slot": 0, "clip_slot": 1}
        prev_model = (anchor["node"], anchor.get("model_slot", 0))
        prev_clip = (anchor["node"], anchor.get("clip_slot", 1))

    # 逐个插入新 LoraLoader
    for i, slot in enumerate(extras, start=1):
        nid = graph.ids.next_node()
        _make_lora_node(graph, nid, slot, i, prev_model, prev_clip)
        added.append(nid)
        prev_model = (nid, 0)
        prev_clip = (nid, 1)

    # 链尾接回消费点
    for (tid, tslot, ltype) in consumers_model:
        graph.add_link(prev_model[0], prev_model[1], tid, tslot, ltype)
    for (tid, tslot, ltype) in consumers_clip:
        graph.add_link(prev_clip[0], prev_clip[1], tid, tslot, ltype)

    return added


def _make_lora_node(graph: Graph, nid: int, slot: dict, index: int,
                    prev_model: tuple, prev_clip: tuple) -> None:
    """新建一个 LoraLoader 节点（完整结构），并接好 model/clip 入链。"""
    node = {
        "id": nid,
        "type": "LoraLoader",
        "order": 999,  # 序列化按 order 排序，新节点放最后无妨
        "mode": 0,
        "inputs": [
            {"name": "model", "type": "MODEL", "link": None},
            {"name": "clip", "type": "CLIP", "link": None},
            {"name": "lora_name", "type": "COMBO", "widget": {"name": "lora_name"}, "link": None},
            {"name": "strength_model", "type": "FLOAT", "widget": {"name": "strength_model"}, "link": None},
            {"name": "strength_clip", "type": "FLOAT", "widget": {"name": "strength_clip"}, "link": None},
        ],
        "outputs": [
            {"name": "MODEL", "type": "MODEL", "slot_index": 0, "links": []},
            {"name": "CLIP", "type": "CLIP", "slot_index": 1, "links": []},
        ],
        "properties": {"cnr_id": "comfy-core", "Node name for S&R": "LoraLoader"},
        "widgets_values": [slot["name"], slot.get("strength_model", 0.8), slot.get("strength_clip", 1.0)],
    }
    graph.nodes[nid] = node
    graph.add_link(prev_model[0], prev_model[1], nid, 0, "MODEL")
    graph.add_link(prev_clip[0], prev_clip[1], nid, 1, "CLIP")
    node["_override"] = {
        "lora_name": slot["name"],
        "strength_model": slot.get("strength_model", 0.8),
        "strength_clip": slot.get("strength_clip", 1.0),
    }
