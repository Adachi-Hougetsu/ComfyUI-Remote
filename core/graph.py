"""有效图：UI 格式 workflow 的节点/链接可变模型。

从原始模板深拷贝而来，每次转换重建。提供链接记账（add/remove/connect）、
节点移除、bypass 透传、补线（patch）等图操作。workflow_converter 与 lora_chain 共享。
"""
from __future__ import annotations

import copy
from typing import Optional

from .ids import IdAllocator


class Graph:
    def __init__(self, ui: dict, ids: IdAllocator):
        self.ids = ids
        self.nodes = {n["id"]: copy.deepcopy(n) for n in ui.get("nodes", [])}
        self.links = {l[0]: list(l) for l in ui.get("links", [])}
        self.last_node_id = ui.get("last_node_id", max(self.nodes, default=0))
        self.last_link_id = ui.get("last_link_id", max(self.links, default=0))

    # ---------- 链接记账 ----------

    def find_link(self, origin_id: int, origin_slot: int, target_id: int, target_slot: int) -> Optional[int]:
        for lid, l in self.links.items():
            if (l[1] == origin_id and l[2] == origin_slot
                    and l[3] == target_id and l[4] == target_slot):
                return lid
        return None

    def add_link(self, origin_id: int, origin_slot: int, target_id: int, target_slot: int,
                 link_type: str = "*") -> Optional[int]:
        """新建链接并记账。target_slot 为目标节点的 inputs 下标。"""
        node = self.nodes.get(target_id)
        if node is None:
            return None
        inp = self._input_at_slot(node, target_slot)
        if inp is None:
            return None
        lid = self.ids.next_link()
        self.links[lid] = [lid, origin_id, origin_slot, target_id, target_slot, link_type]
        inp["link"] = lid
        onode = self.nodes.get(origin_id)
        if onode is not None:
            out = self._output_at_slot(onode, origin_slot)
            if out is not None and "links" in out:
                if lid not in out["links"]:
                    out["links"].append(lid)
        return lid

    def remove_link(self, link_id: int) -> None:
        l = self.links.pop(link_id, None)
        if l is None:
            return
        _, oid, oslot, tid, tslot, _ = l
        node = self.nodes.get(tid)
        if node is not None:
            inp = self._input_at_slot(node, tslot)
            if inp is not None and inp.get("link") == link_id:
                inp["link"] = None
        onode = self.nodes.get(oid)
        if onode is not None:
            out = self._output_at_slot(onode, oslot)
            if out is not None and link_id in out.get("links", []):
                out["links"].remove(link_id)

    def connect(self, origin_id: int, origin_slot: int, target_node: dict,
                target_input_name: str, link_type: str = "*") -> bool:
        """把 target_node 的指定输入接到 origin 槽位（先拆旧链）。"""
        for idx, inp in enumerate(target_node.get("inputs", [])):
            if inp.get("name") == target_input_name:
                if inp.get("link"):
                    self.remove_link(inp["link"])
                self.add_link(origin_id, origin_slot, target_node["id"], idx, link_type)
                return True
        return False

    # ---------- 节点操作 ----------

    def remove_node(self, node_id: int) -> None:
        node = self.nodes.pop(node_id, None)
        if node is None:
            return
        for lid in list(self.links):
            l = self.links[lid]
            if l[1] == node_id or l[3] == node_id:
                self.remove_link(lid)

    # ---------- 输入查询 ----------

    def input_source(self, node: dict, input_name: str) -> Optional[tuple]:
        """返回输入当前来源：链接则 (origin_id, origin_slot)，否则 None。"""
        for inp in node.get("inputs", []):
            if inp.get("name") == input_name:
                lid = inp.get("link")
                if lid:
                    l = self.links.get(lid)
                    if l:
                        return (l[1], l[2])
                return None
        return None

    def set_widget(self, node_id: int, widget_name: str, value) -> None:
        node = self.nodes.get(node_id)
        if node is None:
            return
        node.setdefault("_override", {})[widget_name] = value

    @staticmethod
    def _input_at_slot(node: dict, slot: int) -> Optional[dict]:
        ins = node.get("inputs", [])
        return ins[slot] if 0 <= slot < len(ins) else None

    @staticmethod
    def _output_at_slot(node: dict, slot: int) -> Optional[dict]:
        outs = node.get("outputs", [])
        return outs[slot] if 0 <= slot < len(outs) else None


def _bypass_downstream_first(graph: Graph, bypass_ids) -> list:
    """把透传节点按「下游优先」排序，供 bypass_rewire 处理。

    链式透传（U→B→T，U/B 都透传）时，B 的输入源 U 必须仍在图里才能重连；
    若按集合任意序先把 U 摘掉，B 就找不到源、目标悬空。下游优先 = 逆拓扑序：
    每个节点在它的所有透传下游被处理之后才处理，故处理它时其透传上游仍在。
    """
    bset = set(bypass_ids) & set(graph.nodes)
    if len(bset) < 2:
        return list(bset)
    downstream = {bid: set() for bid in bset}   # bid → 它直接喂的透传下游
    indeg = {bid: 0 for bid in bset}            # 入度 = 上游透传节点数
    for bid in bset:
        for out in graph.nodes[bid].get("outputs", []):
            for lid in list(out.get("links") or []):
                l = graph.links.get(lid)
                if l and l[3] in bset and l[3] != bid:
                    downstream[bid].add(l[3])
                    indeg[l[3]] += 1
    # Kahn 拓扑序（上游优先），再反转 = 下游优先；环残余按 id 保底
    topo, stack = [], sorted(b for b, d in indeg.items() if d == 0)
    while stack:
        cur = stack.pop(0)
        topo.append(cur)
        for d in sorted(downstream[cur]):
            indeg[d] -= 1
            if indeg[d] == 0:
                stack.append(d)
    topo.extend(sorted(bset - set(topo)))
    return list(reversed(topo))


def bypass_rewire(graph: Graph, bypass_ids) -> None:
    """对每个透传节点 B：把 B 的输入来源（按类型匹配）直连到 B 的输出目标，再移除 B。

    IPA 关闭场景：43 的 MODEL 输出目标 [3,0] 接到 43 的 MODEL 输入来源 [10,0]，
    得到 10→3 直连。被剔除卫星（44/45/46）不影响 43 的 model 链路。
    处理顺序为下游优先（见 _bypass_downstream_first），链式透传不会被中途摘断。
    """
    for bid in _bypass_downstream_first(graph, bypass_ids):
        node = graph.nodes.get(bid)
        if node is None:
            continue
        edges = []  # (link_id, out_type, target_id, target_slot, link_type)
        for out in node.get("outputs", []):
            for lid in list(out.get("links") or []):  # 某些自定义节点输出 links 为 null
                l = graph.links.get(lid)
                if l:
                    edges.append((lid, out.get("type", "*"), l[3], l[4], l[5]))
        for (lid, otype, tid, tslot, ltype) in edges:
            src = _find_bypass_src(graph, node, otype)
            if src is None:
                continue
            soid, soslot = src
            graph.remove_link(lid)                       # B → T
            graph.add_link(soid, soslot, tid, tslot, ltype)   # S → T（清空 S→B 由 remove_node 处理）
        graph.remove_node(bid)                           # 清 S→B 及 B 本身


def _find_bypass_src(graph: Graph, node: dict, otype: str) -> Optional[tuple]:
    for inp in node.get("inputs", []):
        if inp.get("type") == otype:
            src = graph.input_source(node, inp["name"])
            if src:
                return src
    for inp in node.get("inputs", []):
        src = graph.input_source(node, inp["name"])
        if src:
            return src
    return None


def apply_patches(graph: Graph, schema, opts) -> list:
    """执行 patch 模式节点群的补线（add/remove 链接）。

    启用 → patches_enable（死链补入场景）；禁用 → patches_disable（活链摘除恢复直连场景）。
    返回实际执行的补线描述列表（用于诊断/测试）。
    """
    applied = []
    for g in schema.node_groups:
        if g.mode != "patch":
            continue
        enabled = opts.enabled_groups.get(g.key, g.default_enabled)
        patches = g.patches_enable if enabled else g.patches_disable
        for op in patches:
            if op.action == "remove":
                lid = graph.find_link(op.origin[0], op.origin[1], op.target[0], op.target[1])
                if lid is not None:
                    graph.remove_link(lid)
                    applied.append(f"remove {op.origin[0]}.{op.origin[1]}→{op.target[0]}.{op.target[1]}")
            elif op.action == "add":
                if graph.add_link(op.origin[0], op.origin[1], op.target[0], op.target[1], op.link_type):
                    applied.append(f"add {op.origin[0]}.{op.origin[1]}→{op.target[0]}.{op.target[1]}")
    return applied
