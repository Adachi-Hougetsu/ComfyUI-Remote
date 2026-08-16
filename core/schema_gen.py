"""工作流自动导入 —— 从任意 ComfyUI UI 格式 workflow 生成面板 schema。

识别引擎不认节点"类名"，只认三样东西（数据全部来自 workflow 自身 + object_info）：
  1. widget 名（steps/cfg/ckpt_name/lora_name/text/image/strength…——ComfyUI 全生态的强惯例）
  2. 输入/输出类型 token（MODEL/CLIP/VAE/CONDITIONING/IMAGE/LATENT/CONTROL_NET/IPADAPTER…）
  3. 图拓扑（采样器 → 条件链/模型链/尺寸/保存节点的数据流）

因此新节点只要遵循 ComfyUI 命名惯例就自动被识别；不认识的节点原样透传、
数值控件浮成"高级参数"，导入永不失败。规则表见 generate_schema 内的识别谓词。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

from config import COMFY_INPUT_DIR, COMFY_WORKFLOWS_DIR
from .schema import ensure_widget_entries as _ensure_widget_entries
from .workflow_converter import align_widget_values

log = logging.getLogger(__name__)

# ComfyUI 用户工作流目录（自动导入来源）。未配置或不存在时回退到 input 目录同级默认位置。
WORKFLOWS_DIR = COMFY_WORKFLOWS_DIR


class NoSamplerError(Exception):
    """工作流里没有可生成图像的采样器（如纯预处理预览图）。"""


def source_workflows_dir() -> Path:
    """导入来源目录：优先显式配置，否则回退到 ComfyUI input 目录同级。"""
    if WORKFLOWS_DIR.exists():
        return WORKFLOWS_DIR
    if COMFY_INPUT_DIR is not None:
        fb = COMFY_INPUT_DIR.parent / "user" / "default" / "workflows"
        if fb.exists():
            return fb
    return WORKFLOWS_DIR


def sanitize_id(filename: str) -> str:
    """文件名 → 模板 id：去扩展名，非字母数字串换成单 '-'，小写。

    '2LoRa-2-try -single.json' -> '2lora-2-try-single'
    """
    stem = Path(filename).stem
    sid = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    return sid


# efficiency 系采样器（KSampler (Efficient) / easy fullkSampler 等）把 CONDITIONING 透传成
# 输出，旧采样器判定的"不产 CONDITIONING"会误杀。开放规则（消费 MODEL+CONDITIONING+LATENT、
# 产出 LATENT、无 IMAGE 输入）在已装节点上零误报；视频/refiner/容器家族是唯一例外，黑名单兜底。
_SAMPLER_PASSTHROUGH_EXCLUDED = {
    # 容器/透传盒（rgthree）：有 IMAGE 输入所以开放规则已排除，双保险
    "Context (rgthree)", "Context Big (rgthree)",
    # 视频/refiner/引导类：同样吃 MODEL+CONDITIONING+LATENT、出 LATENT，但不是"主采样器"
    "HunyuanRefinerLatent", "HunyuanVideo15SuperResolution",
    "WanImageToVideoSVIPro", "WanSoundImageToVideoExtend",
    "ICLightConditioning",
    "LTXVAddGuide", "LTXVAddGuideMulti", "LTXVAddGuidesFromBatch", "LTXVCropGuides",
    "ResolutionBucket", "easy pipeEdit",
}

# 位置 widget 名注册表在 core/schema.py（_WIDGET_ORDER / ensure_widget_entries）。
# 某些自定义节点（efficiency 系）把 widget 值存进 widgets_values 却不声明进 inputs[]；
# 生成器与加载校验同读这一份，保证生成的 schema 在磁盘 workflow 上"逻辑存在"。


# ---------------------------------------------------------------------------
# 图索引
# ---------------------------------------------------------------------------

class _GraphIndex:
    """从 UI workflow 建拓扑索引：入边（按输入名）、出边、widget 对齐、角色判定。"""

    ROLES = (
        "sampler", "model", "lora", "prompt", "dims", "loadimage", "save",
        "preview", "controlnet_apply", "controlnet_loader", "ipa_modifier",
        "ipa_loader", "clip_vision", "preprocessor", "clip_layer",
        "latent_upscaler", "model_upscaler", "vae_decode", "vae_encode",
    )

    def __init__(self, ui: dict):
        self.ui = ui
        self.nodes = {n["id"]: n for n in ui.get("nodes", [])}
        for n in self.nodes.values():
            _ensure_widget_entries(n)
        self.incoming: dict[int, dict[str, tuple]] = {nid: {} for nid in self.nodes}
        self.outgoing: dict[int, list] = {nid: [] for nid in self.nodes}
        for l in ui.get("links", []):
            if len(l) < 6:
                continue
            _, oid, oslot, tid, tslot, ltype = l[:6]
            if oid not in self.nodes or tid not in self.nodes:
                continue
            tnode = self.nodes[tid]
            ins = tnode.get("inputs", [])
            name = ins[tslot]["name"] if 0 <= tslot < len(ins) else None
            self.outgoing[oid].append((oslot, tid, tslot, ltype))
            if name:
                self.incoming[tid][name] = (oid, oslot)
        self._role_cache: dict[int, Optional[str]] = {}

    # ---- 节点元数据 ----

    @staticmethod
    def inames(node: dict) -> set:
        return {i.get("name") for i in node.get("inputs", []) if i.get("name")}

    @staticmethod
    def in_types(node: dict) -> dict:
        return {i.get("name"): i.get("type") for i in node.get("inputs", [])}

    @staticmethod
    def otypes(node: dict) -> list:
        return [o.get("type") for o in node.get("outputs", [])]

    @staticmethod
    def in_index(node: dict, name: str) -> Optional[int]:
        for idx, inp in enumerate(node.get("inputs", [])):
            if inp.get("name") == name:
                return idx
        return None

    @staticmethod
    def has_widget(node: dict, name: str) -> bool:
        return any(i.get("name") == name and i.get("widget") for i in node.get("inputs", []))

    def widgets(self, node: dict) -> dict:
        return align_widget_values(node)

    def title(self, nid: int) -> str:
        n = self.nodes.get(nid, {})
        return (n.get("title")
                or n.get("properties", {}).get("Node name for S&R")
                or n.get("type") or str(nid))

    # ---- 角色识别（只认 widget 名 / I/O 类型 / 图位置，不认类名）----

    def role(self, nid: int) -> Optional[str]:
        if nid in self._role_cache:
            return self._role_cache[nid]
        node = self.nodes[nid]
        itypes = self.in_types(node)
        otypes = self.otypes(node)
        w = self.widgets(node)
        r = None

        # 采样器：消费 CONDITIONING、不产 CONDITIONING、消费+产出 LATENT
        has_cond_in = "CONDITIONING" in itypes.values()
        has_lat_in = "LATENT" in itypes.values()
        has_lat_out = "LATENT" in otypes
        if has_cond_in and "CONDITIONING" not in otypes and has_lat_in and has_lat_out:
            r = "sampler"
        elif (has_cond_in and has_lat_in and has_lat_out
              and "MODEL" in itypes.values()
              and "IMAGE" not in itypes.values()
              and node.get("type") not in _SAMPLER_PASSTHROUGH_EXCLUDED):
            # efficiency 系采样器（KSampler (Efficient) / easy fullkSampler 等）：
            # 把 CONDITIONING 透传成 CONDITIONING+/− 输出，旧判定误杀。要求吃 MODEL、
            # 且无 IMAGE 输入，排除 Detailer/容器盒（rgthree Context 有 IMAGE in）。
            r = "sampler"
        elif {"MODEL", "CLIP", "VAE"} <= set(otypes):
            r = "model"  # 底模加载器（checkpoint）
        elif self.has_widget(node, "lora_name") and ("MODEL" in otypes or "CLIP" in otypes):
            r = "lora"
        elif self.has_widget(node, "text") and "CONDITIONING" in otypes:
            r = "prompt"
        elif {"width", "height"} <= set(w) and "LATENT" in otypes and "IMAGE" not in itypes.values():
            r = "dims"
        elif self.has_widget(node, "image") and "IMAGE" in otypes and "upload" in self.inames(node):
            r = "loadimage"
        elif self.has_widget(node, "filename_prefix") and (
                "images" in self.inames(node) or "image" in self.inames(node)
                or "samples" in self.inames(node)):
            r = "save"  # SaveImage 家族 + SaveLatent（samples 输入）
        elif ("CONTROL_NET" in itypes.values() and "IMAGE" in itypes.values()
              and "CONDITIONING" in otypes):
            r = "controlnet_apply"
        elif self.has_widget(node, "control_net_name") and "CONTROL_NET" in otypes:
            r = "controlnet_loader"
        elif "IPADAPTER" in itypes.values() and "MODEL" in otypes:
            r = "ipa_modifier"
        elif self.has_widget(node, "ipadapter_file") and "IPADAPTER" in otypes:
            r = "ipa_loader"
        elif self.has_widget(node, "clip_name") and "CLIP_VISION" in otypes:
            r = "clip_vision"
        elif ("POSE_KEYPOINT" in otypes
              or (any(self.has_widget(node, x) for x in ("detect_hand", "detect_body", "detect_face", "resolution"))
                  and "IMAGE" in itypes.values())):
            r = "preprocessor"
        elif (("images" in self.inames(node) or "image" in self.inames(node))
              and "IMAGE" in otypes
              and not any(self.has_widget(node, x) for x in ("detect_hand", "detect_body",
                                                             "detect_face", "resolution"))):
            # 预览类：输入名可能是 image（FastPreview/ImageAndMaskPreview）不是 images。
            # 放在 preprocessor 之后：预处理节点也吃 image+出 IMAGE，先归类预处理。
            r = "preview"
        elif self.has_widget(node, "stop_at_clip_layer") and "CLIP" in otypes:
            r = "clip_layer"
        elif ("samples" in itypes and itypes["samples"] == "LATENT"
              and "LATENT" in otypes):
            r = "latent_upscaler"
        elif ("upscale_model" in itypes or self.has_widget(node, "model_name")):
            r = "model_upscaler"
        elif ("vae" in itypes and itypes.get("samples") == "LATENT" and "IMAGE" in otypes):
            r = "vae_decode"
        elif ("vae" in itypes and itypes.get("pixels") == "IMAGE" and "LATENT" in otypes):
            r = "vae_encode"

        self._role_cache[nid] = r
        return r

    # ---- 拓扑查询 ----

    def samplers(self) -> list:
        return sorted(nid for nid in self.nodes if self.role(nid) == "sampler")

    def upstream(self, nid: int) -> set:
        """nid 的所有祖先（含自身），任意边类型。"""
        seen, stack = set(), [nid]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for name, (oid, _) in self.incoming.get(cur, {}).items():
                if oid in self.nodes:
                    stack.append(oid)
        return seen

    def downstream(self, nid: int) -> set:
        """nid 的所有后代（含自身），任意边类型。"""
        seen, stack = set(), [nid]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for (_oslot, tid, _tslot, _t) in self.outgoing.get(cur, []):
                if tid in self.nodes:
                    stack.append(tid)
        return seen

    def sampler_feeds(self, a: int, b: int) -> bool:
        """采样器 a 是否给采样器 b 供 latent（a 在 b 的 latent 链反向可达内，任意边类型）。"""
        bsrc = self.incoming.get(b, {}).get("latent_image")
        if bsrc is None:
            for name, src in self.incoming.get(b, {}).items():
                if src and bsrc is None:
                    if self.in_types(self.nodes[b]).get(name) == "LATENT":
                        bsrc = src
        if bsrc is None:
            return False
        return a in self.upstream(bsrc[0])

    def base_sampler(self, samplers: list) -> int:
        """链首采样器：不被任何其它采样器供 latent 的那个（多段生成的主生成段）。

        refer1/refer2 的 base(5)→refine(14) 链：5 不被任何人供 latent → base=5。
        抽卡（seed 变化）落在它身上，主图才会变；refine 只是精修段。
        """
        bases = [s for s in samplers if not any(self.sampler_feeds(a, s) for a in samplers if a != s)]
        if not bases:
            bases = samplers
        return bases[0]

    def terminal_sampler(self, samplers: list) -> int:
        """终采样器：不向其它采样器供 latent 的那个（2 段放大取最后一段）。

        refer1/refer2 的 base(5)→refine(14) 链：5 给 14 供 latent，故终采样器=14。
        多个满足时取 latent 链能到保存/预览节点的，否则取 order 最大者。
        """
        terminals = [s for s in samplers if not any(self.sampler_feeds(s, a) for a in samplers if a != s)]
        if not terminals:
            terminals = samplers
        if len(terminals) == 1:
            return terminals[0]
        saves = self.saves()
        for s in terminals:
            if saves and any(s in self.upstream(sv) for sv in saves):
                return s
        return max(terminals, key=lambda nid: self.nodes[nid].get("order", 0))

    def saves(self) -> list:
        return sorted(nid for nid in self.nodes if self.role(nid) == "save")

    def previews(self) -> list:
        return sorted(nid for nid in self.nodes if self.role(nid) == "preview")


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------

def _oi_meta(object_info, node_type: str, widget: str) -> dict:
    """从 object_info 取某 widget 的数值元数据 {min,max,step,default}（没有则空）。"""
    try:
        req = (object_info or {}).get(node_type, {}).get("input", {}).get("required", {})
        spec = req.get(widget)
        if isinstance(spec, list) and len(spec) > 1 and isinstance(spec[1], dict):
            return {k: spec[1][k] for k in ("min", "max", "step", "default", "round") if k in spec[1]}
    except Exception:
        pass
    return {}


def _num(v) -> float:
    """安全取数值：自定义节点的 INT/FLOAT 控件可能存 'auto' 等哨兵字符串，
    按 0 兜底（既不让 int()/float() 崩，也不让字符串混进数值滑杆）。"""
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return 0.0


# object_info 的数值元数据是"技术硬界"（KSampler.steps 最大 10000、EmptyLatentImage
# 尺寸最大 16384）——直接暴露到手机滑杆既不美观也容易误触。这里按 widget 名把常用控件
# 收窄到人类实用的范围；未知控件不封顶（保持 object_info 原值）。规则的优先级更高：
# _rules.json 里显式预注册的控件天然带实用范围，不经过这里。
_PRACTICAL_CAPS = {
    # 生成参数
    "steps": (1, 150),
    "cfg": (0, 20),
    "denoise": (0, 1),
    "width": (256, 2048),
    "height": (256, 2048),
    # 强度 / 进度类（LoRA、ControlNet、IPAdapter、参考强度…）
    "strength": (0, 2),
    "strength_model": (0, 2),
    "strength_clip": (0, 2),
    "weight": (0, 2),
    "effect": (0, 2),
    "reference_strength": (0, 2),
    "base_multiplier": (0, 2),
    "start_at": (0, 1),
    "end_at": (0, 1),
    "start_percent": (0, 1),
    "end_percent": (0, 1),
    "guidance_start": (0, 1),
    "guidance_end": (0, 1),
    "softness": (0, 1),
    # 分块解码/放大家族（VAEDecodeTiled 等：object_info 把 tile 上限给到 8192）
    "tile_size": (64, 2048),
    "overlap": (0, 512),
    "temporal_size": (8, 512),
    "temporal_overlap": (4, 128),
}


def _clamped(meta: dict, widget: str, default=None) -> dict:
    """object_info 元数据 → 实用范围（widget 命中 _PRACTICAL_CAPS 时收窄）。

    默认值不能跑出滑块边界（否则工作流里 steps=200 会显示越界）：
    默认值超界时把边界放宽到默认值，保工作流真值、滑杆可拖。
    未命中 _PRACTICAL_CAPS 的未知控件：过滤 INT64 技术极值
    （Impact 系 value 等 min/max 是 ±9.2e18），否则滑杆直接废掉；
    过滤后调用方回退到"从当前值猜区间"。
    """
    cap = _PRACTICAL_CAPS.get(widget)
    if cap is None:
        out = dict(meta)
        for k in ("min", "max"):
            v = out.get(k)
            if not isinstance(v, (int, float)) or abs(v) > 1e9:
                out.pop(k, None)
        lo, hi = out.get("min"), out.get("max")
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and lo >= hi:
            out.pop("min", None)
            out.pop("max", None)
        return out
    lo, hi = cap
    out = dict(meta)
    out["min"] = max(out.get("min", lo), lo)
    out["max"] = min(out.get("max", hi), hi)
    if default is not None:
        try:
            f = float(default)
            if f < out["min"]:
                out["min"] = f
            if f > out["max"]:
                out["max"] = f
        except (TypeError, ValueError):
            pass
    return out


def _param(node_id: int, node_type: str, widget: str, default, ptype: str, label: str,
           key: str, meta: dict, fallback: dict, seed_mode: Optional[dict] = None) -> dict:
    meta = _clamped(meta, widget, default)
    return {
        "key": key, "node": node_id, "widget": widget, "label": label, "type": ptype,
        "min": meta.get("min", fallback.get("min")),
        "max": meta.get("max", fallback.get("max")),
        "step": meta.get("step", fallback.get("step")),
        "default": default,
        "options": [],
        "seed_mode": seed_mode,
    }


def generate_schema(ui: dict, object_info: Optional[dict] = None, *,
                    schema_id: str, title: Optional[str] = None) -> dict:
    """任意 UI workflow → 面板 schema dict（不含 id/workflow，由调用方补）。

    规则全部基于 widget 名 / I/O 类型 / 图拓扑，不认节点类名，保证新节点类型可识别。
    永不抛错（除 NoSamplerError）：不认识的节点透传，数值控件浮成高级参数。
    """
    g = _GraphIndex(ui)
    samplers = g.samplers()
    if not samplers:
        raise NoSamplerError("工作流里没有可生成的采样器（KSampler/KSamplerAdvanced）")

    terminal = g.terminal_sampler(samplers)
    base = g.base_sampler(samplers)
    t_node = g.nodes[terminal]
    t_itypes = g.in_types(t_node)

    # ---- 保存节点 ----
    save_node = _find_main_save(g, samplers)

    # ---- 模型链：底模 + LoRA + 链上修饰器（IPA）----
    chain = _model_chain(g, terminal)
    checkpoint = None
    for nid in reversed(chain):
        if g.role(nid) == "model":
            checkpoint = nid
            break
    chain_set = set(chain)
    loras = [nid for nid in chain if g.role(nid) == "lora"]  # 采样器侧在前（见下方反转）
    modifiers = [nid for nid in chain if g.role(nid) == "ipa_modifier"]

    # ---- 提示词 ----
    # 多采样器时从 base 追（与 seed/steps 同一语义单元：用户编辑的主提示词）；
    # base 追不到（条件链只接 refine 段）再回退 terminal。
    prompt_sampler = base if base != terminal else terminal
    pos_node = _trace_text(g, prompt_sampler, "positive")
    neg_node = _trace_text(g, prompt_sampler, "negative")
    if pos_node is None and neg_node is None and prompt_sampler != terminal:
        pos_node = _trace_text(g, terminal, "positive")
        neg_node = _trace_text(g, terminal, "negative")

    # ---- 尺寸节点 ----
    dims_node = _find_dims(g, samplers)

    # ---- 组（先于 drop 判定）。群组框优先 = 组件边界，框内节点不再被自动识别重复处理 ----
    groups, group_members, group_notes = [], set(), []
    boxes = _box_members(ui)
    dead_used_terms: set = set()
    if boxes:
        groups, group_notes, dead_used_terms = _derive_boxed_groups(
            g, boxes, samplers, terminal, checkpoint, object_info, modifiers, chain_set)
    boxed_covered = set().union(*(grp["nodes"] for grp in groups)) if groups else set()
    groups += _derive_ipa_group(g, modifiers, chain_set, terminal, object_info, skip=boxed_covered)
    groups += _derive_cn_patch_group(g, samplers, terminal, checkpoint, object_info,
                                     claimed=boxed_covered, used_terms=dead_used_terms)
    # 框组优先拿 key；自动组与框组 key 撞车时后者改名（extra_params key 前缀跟随）
    seen_keys = set()
    for grp in groups:
        new_key = _unique_group_key(grp["key"], seen_keys)
        if new_key != grp["key"]:
            old = grp["key"]
            for ep in grp.get("extra_params", []):
                if ep.get("key", "").startswith(f"{old}_"):
                    ep["key"] = new_key + ep["key"][len(old):]
            grp["key"] = new_key
        seen_keys.add(new_key)
    for grp in groups:
        group_members.update(grp["nodes"])

    # ---- 参考图槽位 ----
    image_slots = _derive_image_slots(g, samplers, groups, group_members, len(samplers))

    # ---- 参数 ----
    # 多采样器时采样参数落到 base（主生成段，seed 变化才有效）；单采样器 base==terminal
    params = _derive_params(g, base if base != terminal else terminal, dims_node, object_info)

    # ---- 高级参数（未知节点的数值控件，仅在采样器祖先上）----
    params += _derive_advanced_params(g, samplers, params)

    # ---- 活跃（已接线）ControlNet 的强度参数：没有开关（常驻），浮成高级参数，
    #      死分支的强度走节点群 extra_params。键按节点区分，避免多个 CN 冲突。
    sampler_anc = set().union(*(g.upstream(s) for s in samplers))
    for nid in sorted(g.nodes):
        if g.role(nid) != "controlnet_apply" or nid not in sampler_anc or nid in group_members:
            continue
        node = g.nodes[nid]
        wv = g.widgets(node)
        for wid, wid_label, fall in (("strength", "控制强度", {"min": 0, "max": 2, "step": 0.05}),
                                     ("start_percent", "起始位置", {"min": 0, "max": 1, "step": 0.05}),
                                     ("end_percent", "结束位置", {"min": 0, "max": 1, "step": 0.05})):
            if g.has_widget(node, wid):
                meta = _oi_meta(object_info, node.get("type"), wid)
                params.append(_param(nid, node.get("type"), wid, wv.get(wid, 0.0), "float",
                                     wid_label, f"cn{nid}_{wid}", meta, fall))

    # ---- drop：死节点 = 全图 − 存活 − 组成员 ----
    drop = _derive_drop(g, samplers, group_members, save_node)

    # ---- 其它节点：所有存活、非组、未声明的可编辑控件 → 参数（m{nid}_{name}）----
    misc_params, misc_nodes = _derive_misc_nodes(g, samplers, params, groups,
                                                 group_members, save_node, object_info)
    params += misc_params

    prompts = {}
    if pos_node is not None:
        prompts["positive"] = {"node": pos_node, "widget": "text",
                               "default": g.widgets(g.nodes[pos_node]).get("text", "")}
    if neg_node is not None:
        prompts["negative"] = {"node": neg_node, "widget": "text",
                               "default": g.widgets(g.nodes[neg_node]).get("text", "")}

    # ---- 动态控制兜底：被连线接管的控件（rgthree Seed / ImpactSwitch / easy int 驱动）----
    # 写 widget 会被 link 覆盖，ComfyUI 静默忽略 → 不生成参数、记 note（安全跳过而非硬失败）。
    def _linked(nid: int, name: str) -> Optional[int]:
        """widget 输入已被连线接管时返回驱动源节点 id，否则 None。"""
        src = g.incoming.get(nid, {}).get(name)
        return src[0] if src else None

    linked_notes: list = []
    kept = []
    for p in params:
        src = _linked(p["node"], p["widget"])
        if src is not None:
            linked_notes.append(
                f"参数 {p.get('label') or p['widget']} 已被连线接管（由 {g.title(src)} 驱动），"
                f"跳过手机端控制")
            continue
        kept.append(p)
    params = kept
    for side in list(prompts):
        p = prompts[side]
        src = _linked(p["node"], p["widget"])
        if src is not None:
            linked_notes.append(
                f"提示词({side}) 已被连线接管（由 {g.title(src)} 驱动），跳过手机端编辑")
            del prompts[side]

    # 底模控件名不总是 ckpt_name（efficiency 系 Loader 用 ckpt1 或未声明）→ 动态识别，
    # 找不到就跳过 + note（宁缺勿假，validate 不会拿错 widget 拦下）。
    model_spec = None
    if checkpoint is not None:
        mw = _model_widget(g, checkpoint)
        if mw is None:
            linked_notes.append(
                f"底模加载器（{g.title(checkpoint)}）的模型选择控件无法识别，跳过手机端切换")
        else:
            model_spec = {"node": checkpoint, "widget": mw, "label": "底模"}
    if linked_notes:
        group_notes += linked_notes

    lora_spec = {"max": 8, "connection": "serial", "default": []}
    if checkpoint is not None:
        lora_spec["anchor"] = {"node": checkpoint, "model_slot": 0, "clip_slot": 1}
    if loras:
        # loras 是采样器侧在前（_model_chain 反向走），应用顺序 = 反转：
        # checkpoint → L2 → L16 → IPA → 采样器。default 与 template_lora_chain
        # 必须按应用顺序给全链，否则 2LoRA 系列只露出链尾一个、链头的 LoRA
        # 会留在工作流里悄悄生效、用户看不到也改不了。
        loras_in_order = list(reversed(loras))
        lora_spec["template_lora_chain"] = loras_in_order
        lora_spec["default"] = []
        for nid in loras_in_order:
            wv = g.widgets(g.nodes[nid])
            lora_spec["default"].append({
                "name": wv.get("lora_name", ""),
                "strength_model": round(float(wv.get("strength_model", 0.8)), 4),
                "strength_clip": round(float(wv.get("strength_clip", 1.0)), 4),
            })

    raw = {
        "id": schema_id,
        "title": title or Path(str(schema_id)).stem,
        "workflow": f"{schema_id}.json",
        "save_output_node": save_node or 0,
        "drop": sorted(drop),
        "model": model_spec,
        "lora": lora_spec,
        "prompts": prompts,
        "params": params,
        "node_groups": groups,
        "group_notes": group_notes,
        "image_slots": image_slots,
        "misc_nodes": misc_nodes,
        "seed_mode_default": "random",
    }
    return raw


# ---------------------------------------------------------------------------
# 各环节
# ---------------------------------------------------------------------------

def _model_widget(g: _GraphIndex, checkpoint: int) -> Optional[str]:
    """底模加载器的模型选择控件名：不硬编码 ckpt_name（自定义 Loader 用 ckpt1 等）。"""
    node = g.nodes[checkpoint]
    for w in ("ckpt_name", "ckpt1", "model_name"):
        if g.has_widget(node, w):
            return w
    for i in node.get("inputs", []):
        if i.get("widget") and "ckpt" in i.get("name", "").lower():
            return i["name"]
    return None


def _find_main_save(g: _GraphIndex, samplers: list) -> Optional[int]:
    saves = g.saves()
    for sid in saves:
        if _save_traces_to_sampler(g, sid, samplers):
            return sid
    if saves:
        return saves[0]
    previews = g.previews()
    return previews[0] if previews else None


def _save_traces_to_sampler(g: _GraphIndex, save_id: int, samplers: list) -> bool:
    """保存节点的 images 链上存在 VAEDecode，其 samples 链能追溯到采样器。"""
    node = g.nodes[save_id]
    src = None
    for name in ("images", "image", "samples"):  # SaveLatent 的输入名是 samples
        if name in g.incoming.get(save_id, {}):
            src = g.incoming[save_id][name]
            break
    if src is None:
        return False
    seen, stack = set(), [src[0]]
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        node = g.nodes[nid]
        if g.role(nid) == "vae_decode":
            cur = g.incoming.get(nid, {}).get("samples")
            if cur is None:
                for name, s in g.incoming.get(nid, {}).items():
                    if s and g.in_types(node).get(name) == "LATENT":
                        cur = s
                        break
            while cur:
                oid, _ = cur
                if oid in samplers:
                    return True
                onode = g.nodes[oid]
                lat = next((i.get("name") for i in onode.get("inputs", [])
                            if i.get("type") == "LATENT" and i.get("link")), None)
                if lat is None:
                    break
                cur = g.incoming.get(oid, {}).get(lat)
        for _n, (oid, _s) in g.incoming.get(nid, {}).items():
            if oid in g.nodes:
                stack.append(oid)
    return False


def _model_chain(g: _GraphIndex, sampler: int) -> list:
    """从采样器 model 输入反向走 MODEL 链，到 MODEL 源（checkpoint）为止。"""
    chain = []
    cur = g.incoming.get(sampler, {}).get("model")
    if cur is None:
        for name, src in g.incoming.get(sampler, {}).items():
            if src and g.in_types(g.nodes[sampler]).get(name) == "MODEL":
                cur = src
                break
    while cur:
        oid, _ = cur
        if oid in chain:
            break
        node = g.nodes[oid]
        chain.append(oid)
        nxt = None
        for inp in node.get("inputs", []):
            if inp.get("type") == "MODEL" and inp.get("link"):
                nxt = g.incoming.get(oid, {}).get(inp["name"])
                break
        if nxt is None:
            break
        cur = nxt
    return chain


def _trace_text(g: _GraphIndex, sampler: int, side: str) -> Optional[int]:
    """沿条件链回溯找提示词文本节点。

    穿过两类节点：ControlNetApply 类（输出槽 0/1 对应 positive/negative 输入）与
    一般条件变换节点（ConditioningConcat/SetMask/SetArea/Combine 等：沿第一个
    已接线的 CONDITIONING 输入继续追）。找不回 None → 前端显示禁用提示。
    """
    cur = g.incoming.get(sampler, {}).get(side)
    if cur is None:
        return None
    seen = set()
    while cur and cur[0] not in seen:
        oid, oslot = cur
        seen.add(oid)
        node = g.nodes[oid]
        if g.has_widget(node, "text") and "CONDITIONING" in g.otypes(node):
            return oid
        # ControlNetApply 类：输出槽 0 → positive 输入，槽 1 → negative 输入
        if g.role(oid) in ("controlnet_apply", "sampler"):
            iname = "positive" if oslot == 0 else "negative"
            cur = g.incoming.get(oid, {}).get(iname)
            continue
        # 一般条件变换：有 CONDITIONING 输出才继续，沿第一个已接线的 CONDITIONING 输入
        if "CONDITIONING" in g.otypes(node):
            nxt = _first_conditioning_input(g, oid)
            if nxt is None:
                break
            cur = nxt
            continue
        break
    return None


def _first_conditioning_input(g: _GraphIndex, nid: int) -> Optional[tuple]:
    """节点第一个已接线的 CONDITIONING 输入源（[节点, 槽]）。"""
    for inp in g.nodes[nid].get("inputs", []):
        if inp.get("type") == "CONDITIONING" and inp.get("link"):
            src = g.incoming.get(nid, {}).get(inp["name"])
            if src:
                return src
    return None


def _find_dims(g: _GraphIndex, samplers: list) -> Optional[int]:
    """找"输出分辨率"尺寸节点。

    1) 优先终采样器 latent 链（输出由最后一段决定），必要时穿过 IMAGE 空间节点
       （VAEEncodeTiled → ImageScale：放大工作流的输出尺寸可调，不再只暴露第一段
       的 EmptyLatentImage，改尺寸会静默裁剪出图）；
    2) 没有则回退任意采样器链（原行为，兼容 img2img 等无 EmptyLatentImage 场景）。
    """
    terminal = g.terminal_sampler(samplers)
    nid = _dims_on_chain(g, terminal)
    if nid is not None:
        return nid
    for sid in samplers:
        if sid == terminal:
            continue
        nid = _dims_on_chain(g, sid)
        if nid is not None:
            return nid
    return None


def _dims_on_chain(g: _GraphIndex, sampler: int) -> Optional[int]:
    """从采样器 latent_image 反向 BFS（LATENT 链优先，IMAGE 链兜底），
    返回第一个带 width/height 控件的节点（EmptyLatentImage / LatentUpscale /
    ImageScale 等）。"""
    cur = None
    for name, src in g.incoming.get(sampler, {}).items():
        if src and g.in_types(g.nodes[sampler]).get(name) == "LATENT":
            cur = src
            break
    if cur is None:
        return None
    seen, stack = set(), [cur[0]]
    while stack:
        nid = stack.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        node = g.nodes[nid]
        if {"width", "height"} <= set(g.widgets(node)):
            return nid
        for typ in ("LATENT", "IMAGE"):
            for inp in node.get("inputs", []):
                if inp.get("type") == typ and inp.get("link"):
                    src2 = g.incoming.get(nid, {}).get(inp["name"])
                    if src2 and src2[0] not in seen:
                        stack.append(src2[0])
    return None


# ---------------------------------------------------------------------------
# 群组框识别（workflow.groups = 组件边界）
# ---------------------------------------------------------------------------

def _box_members(ui: dict) -> list:
    """workflow.groups 几何映射：节点中心点在框内 → 框成员。返回 [(title, [nid,...])]。"""
    nodes = {n["id"]: n for n in ui.get("nodes", [])}
    out = []
    for grp in ui.get("groups", []):
        b = grp.get("bounding") or []
        if len(b) != 4:
            continue
        bx, by, bw, bh = b
        members = []
        for nid, n in nodes.items():
            pos = n.get("pos") or [0, 0]
            size = n.get("size") or [0, 0]
            cx, cy = pos[0] + size[0] / 2, pos[1] + size[1] / 2
            if bx <= cx <= bx + bw and by <= cy <= by + bh:
                members.append(nid)
        out.append((grp.get("title") or "", sorted(members)))
    return out


def _unique_group_key(base: str, used: set) -> str:
    key = base
    n = 1
    while key in used:
        n += 1
        key = f"{base}_{n}"
    return key


def _derive_boxed_groups(g: _GraphIndex, boxes: list, samplers: list, terminal: int,
                         checkpoint: Optional[int], object_info,
                         modifiers: list, chain_set: set) -> tuple:
    """每个非空群组框 → 节点群开关。返回 (groups, notes)。

    无法干净成组（死链/活链/IPA/链段都不匹配，或框内会摘断别的必填输入）→
    跳过并记 note，由导入诊断透传给用户。框内已成组的节点不再被自动识别重复处理。
    同一条件链只生成一个死 CN 开关（静态补线无法串两个可独立开关的死分支）——
    第二个框跳过并提示。
    """
    groups, used_keys = [], set()
    used_terms: set[int] = set()  # 已插入条件链的 terminal（自动识别同链死 CN 时不再重复成组）
    derived: list = []  # (name, grp_or_None, note_line)
    for title, members in boxes:
        name = title or "未命名"
        if not members:
            derived.append((name, None, f"群组框「{name}」为空，跳过"))
            continue
        grp, reason = _derive_boxed_one(g, members, samplers, terminal, checkpoint,
                                        object_info, modifiers, chain_set, used_terms,
                                        title=name)
        if grp is None:
            derived.append((name, None, f"群组框「{name}」跳过：{reason}"))
            continue
        grp["key"] = _unique_group_key(grp["key"], used_keys)
        used_keys.add(grp["key"])
        derived.append((name, grp, f"群组框「{name}」→ {grp['label']}（{len(grp['nodes'])} 节点）"))
    # 跨组引用后验：patch 组的恢复直连引用了其它开关组的成员时，组同时关闭会指向
    # 幽灵节点（来源/目标已被其它组剔除）→ 剔除该组并记 note。
    derived_members = set()
    for _n, grp, _t in derived:
        if grp is not None:
            derived_members.update(grp["nodes"])
    for i, (name, grp, note) in enumerate(derived):
        if grp is None or grp.get("mode") != "patch":
            continue
        others = derived_members - set(grp["nodes"])
        for op in grp.get("patches_disable", []):
            src_id, dst_id = op["add"][0][0], op["add"][1][0]
            if src_id in others or dst_id in others:
                derived[i] = (name, None,
                              f"群组框「{name}」跳过：恢复直连关联到其它开关组的节点，"
                              f"跨组同时关闭会指向幽灵节点")
                break
    groups = [g for _n, g, _t in derived if g is not None]
    notes = [t for _n, _g, t in derived if t]
    return groups, notes, used_terms


def _derive_boxed_one(g: _GraphIndex, members: list, samplers: list, terminal: int,
                      checkpoint: Optional[int], object_info,
                      modifiers: list, chain_set: set, used_terms: set,
                      title: str = "") -> tuple:
    """单个群组框 → (节点群 dict, 备注)。无法干净成组 → (None, 原因)。

    优先级：模块框（源工作流中整框透传）→ ControlNet（活链=摘除恢复 / 死链=补入）
    → IPAdapter（bypass）→ 单入单出链段。
    """
    members = set(members)
    # 模块框：框内真实节点全部处于源 bypass(mode=4)（如 Fast Muter 管理的模块）。
    # 做成「模块开关」：默认关 = 保持作者设定的透传状态；开 = 整框激活参与执行。
    # 不要求框内成链段——模块可以是多路独立分支，一起开关正是用户意图。
    real = [nid for nid in members
            if g.nodes.get(nid, {}).get("type") not in ("Note", "StickyNote")]
    if real and all(g.nodes.get(nid, {}).get("mode") == 4 for nid in real):
        return {
            "key": "module",
            "label": title or "模块",
            "mode": "module",
            "default_enabled": False,
            "bypass_node": None,
            "nodes": sorted(members),
            "patches_enable": [], "patches_disable": [],
            "extra_params": [],
        }, ""
    applies = [nid for nid in members if g.role(nid) == "controlnet_apply"]
    if len(applies) == 1:
        d = applies[0]
        sampler_anc = set().union(*(g.upstream(s) for s in samplers))
        if d in sampler_anc:
            grp, reason = _derive_boxed_chain_patch(g, members)
            if grp is None:
                return None, f"活链 ControlNet 框无法干净摘除恢复：{reason}"
            key, label = _cn_group_label_key(g, members)
            grp["key"], grp["label"] = key, label
            grp["extra_params"] = _cn_extra_params(g, d, key, object_info)
            return grp, ""
        return _derive_boxed_dead_cn(g, members, d, terminal, checkpoint, object_info, used_terms)
    if len(applies) > 1:
        return None, "一个框里有多个 ControlNet 节点，开关语义不明确"
    mods = [nid for nid in members if g.role(nid) == "ipa_modifier"]
    if len(mods) == 1:
        grp = _derive_boxed_ipa(g, members, mods[0], object_info, chain_set)
        if grp is None:
            return None, "IPAdapter 框会摘断别的必填输入（含共享节点？）"
        return grp, ""
    if len(mods) > 1:
        return None, "一个框里有多个 IPAdapter，开关语义不明确"
    grp, reason = _derive_boxed_chain_patch(g, members)
    if grp is None:
        return None, f"框内链段无法干净摘除恢复（{reason}）"
    return grp, ""


def _derive_boxed_dead_cn(g: _GraphIndex, members: set, d: int, terminal: int,
                          checkpoint: Optional[int], object_info,
                          used_terms: set) -> tuple:
    """死 ControlNet 框 → patch 组（启用时把 D 插进 terminal 条件链）。"""
    if terminal in used_terms:
        return None, "同一条件链已有死 ControlNet 开关（静态补线无法串两个可独立开关的死分支），跳过"
    if not _cn_insertable(g, d):
        return None, "死 ControlNet 缺 control_net/image 接线，无法插入"
    patches = _cn_insert_patches(g, d, terminal, checkpoint)
    if patches is None:
        return None, "terminal 条件链缺 positive/negative 输入，无法插入"
    if _removal_broken(g, members, exempt=()):
        return None, "框内含共享节点，关组会摘断别人的必填输入"
    used_terms.add(terminal)
    key, label = _cn_group_label_key(g, members)
    return {
        "key": key, "label": label, "mode": "patch", "default_enabled": False,
        "bypass_node": None, "nodes": sorted(members),
        "patches_enable": patches, "patches_disable": [],
        "extra_params": _cn_extra_params(g, d, key, object_info),
    }, ""


def _derive_boxed_ipa(g: _GraphIndex, members: set, mod: int,
                      object_info, chain_set: set) -> Optional[dict]:
    """IPAdapter 框 → bypass 组（关 = 剔卫星 + bypass_node 透传）。"""
    members = set(members)
    exempt = {(nid, tid, tslot) for nid in members
              for (_oslot, tid, tslot, _t) in g.outgoing.get(nid, [])
              if tid not in members and nid == mod}
    if _removal_broken(g, members, exempt):
        return None
    extra = []
    node = g.nodes[mod]
    wv = g.widgets(node)
    for wid, ekey in (("weight", "ipa_weight"), ("start_at", "ipa_start"), ("end_at", "ipa_end")):
        if g.has_widget(node, wid):
            meta = _oi_meta(object_info, node.get("type"), wid)
            fall = {"min": 0, "max": 2, "step": 0.05} if wid == "weight" else {"min": 0, "max": 1, "step": 0.05}
            extra.append(_param(mod, node.get("type"), wid, wv.get(wid, 0.0), "float",
                                _GROUP_PARAM_LABELS.get(ekey, ekey), ekey, meta, fall))
    return {
        "key": "ipa", "label": "IPAdapter 风格参考", "mode": "bypass",
        "default_enabled": mod in chain_set, "bypass_node": mod,
        "nodes": sorted(members), "patches_enable": [], "extra_params": extra,
    }


def _derive_boxed_chain_patch(g: _GraphIndex, members: set) -> tuple:
    """单入单出主链段（活链 CN / 放大 / 二次采样）→ patch 组。

    禁用 = 摘除组内节点 + 把每个出口信号接回对应入口来源（恢复直连）。
    返回 (节点群 dict, 原因)；无法干净成组 → (None, 原因)。
    额外跳过两类：
    - 框内节点全在源工作流中透传(bypass/mode=4)：开关无实际效果（节点总是从有效图消失）；
    - 恢复直连引用源工作流已透传(bypass/mode=4)的节点：补线要么落空、要么指向幽灵
      节点 → 宁缺勿假，跳过并说明。跨组引用由 _derive_boxed_groups 后验剔除。
    """
    members = set(members)
    if not _intra_connected(g, members):
        return None, "框内节点不成链段"
    if all(g.nodes.get(nid, {}).get("mode") == 4 for nid in members):
        return None, "框内节点均已在工作流中透传(bypass)，开关无实际效果"
    entries = []  # (member_id, input_name, src_id, src_slot, input_type)
    for nid in members:
        for nm, (oid, oslot) in g.incoming.get(nid, {}).items():
            if oid in members or oid not in g.nodes:
                continue
            entries.append((nid, nm, oid, oslot, g.in_types(g.nodes[nid]).get(nm, "ANY")))
    exits = []  # (member_id, oslot, dst_id, dst_slot, ltype, dst_name)
    for nid in members:
        for (oslot, tid, tslot, ltype) in g.outgoing.get(nid, []):
            if tid in members or tid not in g.nodes:
                continue
            ins = g.nodes[tid].get("inputs", [])
            dst_name = ins[tslot]["name"] if 0 <= tslot < len(ins) else None
            exits.append((nid, oslot, tid, tslot, ltype, dst_name))
    if not entries or not exits:
        return None, "框内无进/出口（自包含死分支 / 纯源 / 纯汇）"
    restores = _pair_restores(entries, exits)
    if restores is None:
        return None, "入口/出口无法干净配对（多同型歧义）"
    for op in restores:
        src_id, dst_id = op["add"][0][0], op["add"][1][0]
        if g.nodes.get(src_id, {}).get("mode") == 4 or g.nodes.get(dst_id, {}).get("mode") == 4:
            return None, "恢复直连关联到已透传(bypass)的节点，无法干净摘除恢复"
    key = ("upscale" if any(g.role(n) == "latent_upscaler" for n in members)
           else "upscale_model" if any(g.role(n) == "model_upscaler" for n in members)
           else "component")
    label = {"upscale": "放大", "upscale_model": "模型放大"}.get(key, "组件")
    return {
        "key": key, "label": label, "mode": "patch", "default_enabled": True,
        "bypass_node": None, "nodes": sorted(members),
        "patches_enable": [], "patches_disable": restores, "extra_params": [],
    }, ""


def _pair_restores(entries: list, exits: list) -> Optional[list]:
    """出口链接与入口链接按类型配对，生成恢复直连 ops。任一出口配不上 → None。

    多同型入口时按目标输入名配对（ControlNet 的 positive/negative 对称），
    仍歧义则放弃（框内拓扑不够干净）。
    entries: (member_id, input_name, src_id, src_slot, input_type)
    exits:   (member_id, oslot, dst_id, dst_slot, ltype, dst_name)
    """
    restores = []
    used = set()
    for (_enid, _oslot, dst_id, dst_slot, ltype, dst_name) in exits:
        cands = [i for i, e in enumerate(entries) if i not in used and e[4] == ltype]
        if not cands:
            return None
        if len(cands) == 1:
            pick = cands[0]
        else:
            name_cands = [i for i in cands if entries[i][1] == dst_name]
            if len(name_cands) == 1:
                pick = name_cands[0]
            else:
                return None
        e = entries[pick]
        used.add(pick)
        restores.append({"add": [[e[2], e[3]], [dst_id, dst_slot]], "add_type": ltype})
    return restores


def _intra_connected(g: _GraphIndex, members: set) -> bool:
    """框内节点经组内链接（无向）是否连成一片。"""
    members = set(members)
    if not members:
        return False
    adj = {nid: set() for nid in members}
    for nid in members:
        for (_oslot, tid, _tslot, _t) in g.outgoing.get(nid, []):
            if tid in members:
                adj[nid].add(tid)
                adj[tid].add(nid)
    seen, stack = set(), [next(iter(members))]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(adj[cur] - seen)
    return seen == members


def _removal_broken(g: _GraphIndex, members: set, exempt=()) -> list:
    """去掉 members 后会把非成员节点链接输入弄断的链接列表（非空 = 不能干净摘除）。

    exempt：允许成员→非成员、由补线/透传恢复的链接 (origin_id, target_id, target_slot)。
    """
    members = set(members)
    broken = []
    for nid in members:
        for (oslot, tid, tslot, _t) in g.outgoing.get(nid, []):
            if tid in members or tid not in g.nodes:
                continue
            if (nid, tid, tslot) in exempt:
                continue
            ins = g.nodes[tid].get("inputs", [])
            if 0 <= tslot < len(ins) and ins[tslot].get("link"):
                broken.append((tid, tslot, ins[tslot].get("name")))
    return broken


def _derive_ipa_group(g: _GraphIndex, modifiers: list, chain_set: set,
                      terminal: int, object_info, skip: Optional[set] = None) -> list:
    """链上 IPAdapter 类修饰器 → bypass 节点群（如 amiya 的 ipa 群）。

    skip：已被群组框覆盖的节点 id（框内 IPA 由 _derive_boxed_ipa 处理，这里不重复成组）。
    """
    if not modifiers:
        return []
    skip = skip or set()
    if any(mod in skip for mod in modifiers):
        return []
    mod = modifiers[0]  # 取采样器侧最外层一个，更深的保持静态
    node = g.nodes[mod]
    itypes = g.in_types(node)
    # 卫星：反向可达 mod、非模型链、且不走 model/clip 输入到达 mod 的节点
    model_feed = set()
    for name, (oid, _s) in g.incoming.get(mod, {}).items():
        if name in ("model", "clip") and oid in g.nodes:
            model_feed.add(oid)
    model_feed = set().union(*(g.upstream(x) for x in model_feed)) if model_feed else set()
    back = g.upstream(mod) - {mod}
    satellites = {x for x in back if x not in chain_set and x not in model_feed}
    members = sorted({mod} | satellites)

    key = "ipa"
    extra = []
    wv = g.widgets(node)
    for wid, ekey in (("weight", f"{key}_weight"), ("start_at", f"{key}_start"),
                      ("end_at", f"{key}_end")):
        if g.has_widget(node, wid):
            meta = _oi_meta(object_info, node.get("type"), wid)
            fall = {"min": 0, "max": 2, "step": 0.05} if wid == "weight" else {"min": 0, "max": 1, "step": 0.05}
            extra.append(_param(mod, node.get("type"), wid, wv.get(wid, 0.0), "float",
                                _GROUP_PARAM_LABELS.get(ekey, ekey), ekey, meta, fall))
    return [{
        "key": key, "label": "IPAdapter 风格参考", "mode": "bypass",
        "default_enabled": mod in chain_set, "bypass_node": mod,
        "nodes": members, "patches_enable": [], "extra_params": extra,
    }]


_GROUP_PARAM_LABELS = {
    "ipa_weight": "IPA 权重", "ipa_start": "IPA 起始", "ipa_end": "IPA 结束",
    "cn_strength": "控制强度", "cn_start": "起始位置", "cn_end": "结束位置",
}


def _cn_insertable(g: _GraphIndex, d: int) -> bool:
    """死 ControlNet 分支是否可插入：control_net/image 已接线（分支数据就位）。

    positive/negative 是否已接不重要——插入时会把 terminal 条件链的 src 直接接到 D 上
    （add_link 覆盖旧接线），已接但输出没人消费的"半死" CN（如 4th 的 17）一样可插入。
    活链 CN（输出进入条件链）由 _derive_boxed_chain_patch 摘除机制处理，不走这里。
    """
    node = g.nodes[d]
    itypes = g.in_types(node)
    return ("control_net" in itypes and g.incoming.get(d, {}).get("control_net")
            and "image" in itypes and g.incoming.get(d, {}).get("image"))


def _cn_insert_patches(g: _GraphIndex, d: int, terminal: int,
                       checkpoint: Optional[int]) -> Optional[list]:
    """把死 CN apply d 插进 terminal 条件链的补线 ops（src → d → terminal）。

    terminal 或 d 缺 positive/negative 输入、或 terminal 条件链未接时返回 None。
    """
    t_node = g.nodes[terminal]
    pos_idx = g.in_index(t_node, "positive")
    neg_idx = g.in_index(t_node, "negative")
    if pos_idx is None or neg_idx is None:
        return None
    src_pos = g.incoming.get(terminal, {}).get("positive")
    src_neg = g.incoming.get(terminal, {}).get("negative")
    d_node = g.nodes[d]
    d_pos_idx = g.in_index(d_node, "positive")
    d_neg_idx = g.in_index(d_node, "negative")
    if d_pos_idx is None or d_neg_idx is None or not src_pos or not src_neg:
        return None
    # 来源槽位用实际链接的 origin slot（CLIPTextEncode 的 CONDITIONING 在 0，
    # ControlNetApply 的 negative 在 1 —— 不能硬编码）
    patches = [
        {"remove": [[src_pos[0], src_pos[1]], [terminal, pos_idx]], "remove_type": "CONDITIONING"},
        {"remove": [[src_neg[0], src_neg[1]], [terminal, neg_idx]], "remove_type": "CONDITIONING"},
        {"add": [[src_pos[0], src_pos[1]], [d, d_pos_idx]], "add_type": "CONDITIONING"},
        {"add": [[src_neg[0], src_neg[1]], [d, d_neg_idx]], "add_type": "CONDITIONING"},
        {"add": [[d, 0], [terminal, pos_idx]], "add_type": "CONDITIONING"},
        {"add": [[d, 1], [terminal, neg_idx]], "add_type": "CONDITIONING"},
    ]
    if "vae" in g.in_types(d_node) and checkpoint is not None:
        d_vae_idx = g.in_index(d_node, "vae")
        if d_vae_idx is not None:
            patches.append({"add": [[checkpoint, 2], [d, d_vae_idx]], "add_type": "VAE"})
    return patches


def _cn_group_label_key(g: _GraphIndex, members: set) -> tuple:
    """ControlNet 组：含姿态预处理 → openpose，否则 controlnet。返回 (key, label)。"""
    has_pose = any(g.role(x) == "preprocessor" and "POSE_KEYPOINT" in g.otypes(g.nodes[x])
                   for x in members)
    key = "openpose" if has_pose else "controlnet"
    label = "OpenPose 姿态控制" if key == "openpose" else "ControlNet 控制"
    return key, label


def _cn_extra_params(g: _GraphIndex, d: int, key: str, object_info) -> list:
    """CN apply 的强度/起止参数 → 组 extra_params。"""
    d_node = g.nodes[d]
    extra = []
    wv = g.widgets(d_node)
    for wid, ekey in (("strength", f"{key}_strength"),
                      ("start_percent", f"{key}_start"),
                      ("end_percent", f"{key}_end")):
        if g.has_widget(d_node, wid):
            meta = _oi_meta(object_info, d_node.get("type"), wid)
            fall = {"min": 0, "max": 2, "step": 0.05} if wid == "strength" else {"min": 0, "max": 1, "step": 0.05}
            extra.append(_param(d, d_node.get("type"), wid, wv.get(wid, 0.0), "float",
                                _GROUP_PARAM_LABELS.get(ekey, ekey), ekey, meta, fall))
    return extra


def _derive_cn_patch_group(g: _GraphIndex, samplers: list, terminal: int,
                           checkpoint: Optional[int], object_info,
                           claimed: Optional[set] = None,
                           used_terms: Optional[set] = None) -> list:
    """死 ControlNet 分支 → patch 节点群（如 amiya 的 openpose 群）。

    只在"可插入"模式下生成：分支的 control_net/image 已接线。
    有歧义（≥2 个死分支）时放弃成组、交给 drop（干净剔除）。
    claimed：已被群组框覆盖的节点 id（框内死分支由 _derive_boxed_dead_cn 处理）。
    used_terms：群组框已插入死 CN 的 terminal（静态补线无法串两个可独立开关的死分支，
    同链死分支不再重复成组、交给 drop）。
    """
    claimed = claimed or set()
    used_terms = used_terms or set()
    if terminal in used_terms:
        return []  # 群组框已占用该条件链的死 CN 插口
    applies = [nid for nid in g.nodes if g.role(nid) == "controlnet_apply"]
    if not applies:
        return []
    sampler_anc = set().union(*(g.upstream(s) for s in samplers))
    live = [nid for nid in applies if nid in sampler_anc]
    dead = [nid for nid in applies if nid not in live and nid not in claimed]

    insertable = [d for d in dead if _cn_insertable(g, d)]
    if len(insertable) != 1:
        return []  # 0 或 ≥2 个可插入死分支：放弃（≥2 归入 drop + 备注）
    d = insertable[0]

    # 集群：D + control_net 加载器 + image 源子图（预处理 + LoadImage，传递）+ 下游孤儿
    cluster = {d}
    cn_src = g.incoming[d].get("control_net")
    if cn_src and g.role(cn_src[0]) == "controlnet_loader":
        cluster.add(cn_src[0])
    img_src = g.incoming[d].get("image")
    if img_src:
        _collect_img_subgraph(g, img_src[0], cluster)
    # 下游孤儿：只能经集群到达采样器/保存的节点（如预处理的 PreviewImage）
    cluster |= _downstream_orphans(g, cluster, samplers)
    # 共享节点排除：死分支与活跃分支共用加载器/预处理时，不能把共享节点收进组，
    # 否则关组把共享节点一起剔除、活跃 ControlNet 的必填输入悬空。
    # 必须放在孤儿收集之后：死 CN 自身的输出（→ 孤儿预览）也算集群内。
    _exclude_shared(g, cluster)

    patches = _cn_insert_patches(g, d, terminal, checkpoint)
    if patches is None:
        return []
    key, label = _cn_group_label_key(g, cluster)
    extra = _cn_extra_params(g, d, key, object_info)
    return [{
        "key": key, "label": label, "mode": "patch", "default_enabled": False,
        "bypass_node": None, "nodes": sorted(cluster), "patches_enable": patches,
        "extra_params": extra,
    }]


def _exclude_shared(g: _GraphIndex, cluster: set) -> None:
    """从集群剔除"输出到集群外"的共享节点，迭代到不动点。

    例：ControlNetLoader 同时喂死分支 CN 与活跃 CN → 它不在集群里被剔除，
    关组只摘死分支，活跃分支保持完整。被剔除节点的上游（若只在死分支内）
    也随之在下轮被剔除（整条共享支路都不随组删，只删专属部分）。
    """
    changed = True
    while changed:
        changed = False
        for nid in list(cluster):
            if any(tid not in cluster for (_o, tid, _t, _x) in g.outgoing.get(nid, [])):
                cluster.discard(nid)
                changed = True


def _collect_img_subgraph(g: _GraphIndex, start: int, cluster: set) -> None:
    """把 image 源子图收集进集群：预处理节点 → 它们的 LoadImage，传递。"""
    seen, stack = set(), [start]
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in g.nodes:
            continue
        seen.add(nid)
        role = g.role(nid)
        if role in ("preprocessor", "loadimage"):
            cluster.add(nid)
        if role == "preprocessor":
            for name, (oid, _s) in g.incoming.get(nid, {}).items():
                if g.in_types(g.nodes[nid]).get(name) == "IMAGE":
                    stack.append(oid)
        elif role == "loadimage":
            pass
        else:
            cluster.add(nid)  # 未知但被引用：保留在集群里，避免悬空


def _downstream_orphans(g: _GraphIndex, cluster: set, samplers: list) -> set:
    """集群下游、去掉集群后到不了采样器/保存节点的节点（收进组，随组剔除）。"""
    sroot = set(samplers) | set(g.saves())
    orphan = set()
    reach = set()
    for nid in list(cluster):
        reach |= g.downstream(nid)
    reach -= cluster
    for x in reach:
        if x not in g.nodes:
            continue
        # x 能否不经过集群到达采样器/保存？
        if not _reachable_avoiding(x, cluster, sroot, g):
            orphan.add(x)
    return orphan


def _reachable_avoiding(start: int, avoid: set, targets: set, g: _GraphIndex) -> bool:
    seen, stack = set(), [start]
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        if nid in targets and nid not in avoid:
            return True
        for (_oslot, tid, _tslot, _t) in g.outgoing.get(nid, []):
            if tid in avoid or tid in seen:
                continue
            stack.append(tid)
    return False


def _derive_image_slots(g: _GraphIndex, samplers: list, groups: list,
                        group_members: set, sampler_count: int) -> list:
    slots, seen = [], set()
    if not samplers:
        # 无采样器（纯预览图）：全部 LoadImage 都做槽
        for nid in sorted(g.nodes):
            if g.role(nid) == "loadimage":
                slots.append({"key": f"ref_{nid}", "label": f"参考图 {nid}",
                              "node": nid, "group": None})
        return slots
    grp_by_key = {grp["key"]: grp for grp in groups}
    group_of_node = {nid: grp["key"] for grp in groups for nid in grp["nodes"]}
    # 存活集：采样器祖先 ∪ 保存/预览祖先 ∪ 组成员。死分支的 LoadImage 不做槽位
    # （上传会被 drop 静默丢弃——ipa_try 的 compose_ref 实测）。
    alive = set().union(*(g.upstream(s) for s in samplers)) if samplers else set()
    for sv in set(g.saves()) | set(g.previews()):
        alive |= g.upstream(sv)
    alive |= group_members
    used_keys = set()
    for nid in sorted(g.nodes):
        if g.role(nid) != "loadimage" or nid in seen:
            continue
        seen.add(nid)
        if nid not in alive:
            continue  # 死分支：运行时不在有效图里，槽位无意义
        if g.nodes[nid].get("mode") in (1, 4):
            continue  # 源里 mute/bypass：任何组状态下都不在有效图，上传会被静默丢弃
        cons = [(tslot, tid) for (oslot, tid, tslot, _t) in g.outgoing.get(nid, [])
                if oslot == 0 and tid in g.nodes]
        role_cons = [(tslot, tid) for tslot, tid in cons if g.role(tid) is not None]
        if not role_cons:
            key, label, grp_key = f"ref_{nid}", f"参考图 {nid}", None
        else:
            _tslot, tid = role_cons[0]
            role = g.role(tid)
            if role == "ipa_modifier":
                grp = grp_by_key.get("ipa")
                key, label = "ipa_ref", "IPA 参考图"
                grp_key = "ipa" if grp else None
            elif tid in group_members:
                grp_key = group_of_node.get(tid)
                key = f"{grp_key}_pose" if grp_key == "openpose" else f"{grp_key}_ref"
                label = "姿态图" if grp_key == "openpose" else "参考图"
            elif role == "controlnet_apply":
                key, label, grp_key = "compose_ref", "构图参考图", None
            else:
                key, label, grp_key = f"ref_{nid}", f"参考图 {nid}", None
        # 同组多张参考图：键去重（重复的加序号后缀），否则前端上传互相覆盖
        base_key = key
        n = 1
        while key in used_keys:
            n += 1
            key = f"{base_key}_{n}"
        used_keys.add(key)
        slots.append({"key": key, "label": label, "node": nid, "group": grp_key})
    return slots


def _derive_params(g: _GraphIndex, terminal: int, dims_node: Optional[int],
                   object_info) -> list:
    t_node = g.nodes[terminal]
    t_type = t_node.get("type")
    wv = g.widgets(t_node)
    params = []

    def push(widget, ptype, label, key, fallback, seed_mode=None):
        if not g.has_widget(t_node, widget):
            return  # 该采样器没有此控件（如 KSamplerAdvanced 无 denoise），跳过防校验失败
        meta = _oi_meta(object_info, t_type, widget)
        params.append(_param(terminal, t_type, widget, wv.get(widget), ptype, label, key,
                             meta, fallback, seed_mode))

    push("steps", "int", "步数", "steps", {"min": 1, "max": 150, "step": 1})
    push("cfg", "float", "CFG", "cfg", {"min": 0, "max": 20, "step": 0.5})
    push("sampler_name", "combo", "采样器", "sampler_name", {})
    push("scheduler", "combo", "调度器", "scheduler", {})
    push("denoise", "float", "重绘幅度", "denoise", {"min": 0, "max": 1, "step": 0.05})
    push("seed", "int", "种子", "seed",
         {"min": 0, "max": 2 ** 63 - 1, "step": 1},
         {"type": "toggle", "options": ["fixed", "random"]})

    if dims_node is not None:
        d_node = g.nodes[dims_node]
        d_wv = g.widgets(d_node)
        for widget, label, key in (("width", "宽", "width"), ("height", "高", "height")):
            meta = _oi_meta(object_info, d_node.get("type"), widget)
            fallback = {"min": 256, "max": 2048, "step": 64}
            params.append(_param(dims_node, d_node.get("type"), widget, d_wv.get(widget),
                                 "int", label, key, meta, fallback))
    return params


# ---------------------------------------------------------------------------
# 可扩展规则注册表（数据，非代码）：schemas/_rules.json
# 认不出标准惯例的"新 widget"：加一条规则即可升级为带标签/区间的参数，
# 不需要改代码。规则优先于内置惯例。
# ---------------------------------------------------------------------------

_RULES: dict = {}


def _load_rules() -> dict:
    global _RULES
    if _RULES:
        return _RULES
    try:
        from config import BASE_DIR
        p = BASE_DIR / "schemas" / "_rules.json"
        if p.exists():
            _RULES = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        _RULES = {}
    return _RULES


def _rule_for(widget: str) -> Optional[dict]:
    return _load_rules().get("widgets", {}).get(widget)


_EXCLUDED_ADV = {
    "steps", "cfg", "denoise", "seed", "noise_seed", "sampler_name", "scheduler",
    "width", "height", "batch_size", "upscale_method", "crop", "scale_by",
    "lora_name", "strength_model", "strength_clip", "text", "ckpt_name",
    "control_net_name", "clip_name", "ipadapter_file", "model_name", "image",
    "filename_prefix", "stop_at_clip_layer", "strength", "start_percent",
    "end_percent", "weight", "start_at", "end_at", "resolution",
    "detect_hand", "detect_body", "detect_face", "vae",
    # seed 的 fixed/random 前端占位符：align_widget_values 显式弹出、永不上送 API，
    # 做成面板参数只会是点了没反应的死控件（efficiency 采样器经注册表补全后会出现）。
    "control_after_generate",
    # Weilin 系节点的 UI 辅助控件：实际生效的是 wildcard_text / populated_text，
    # 这些"Select to add ..."下拉只是前端编辑辅助，手机面板暴露只会是无效选项。
    "Select to add Wildcard", "Select to add LoRA",
}


def _derive_advanced_params(g: _GraphIndex, samplers: list, params: list) -> list:
    """未知节点的数值/下拉控件，浮成"高级参数"（仅采样器祖先，上限 6 个）。"""
    known_widgets = {p["widget"] for p in params}
    known_widgets |= _EXCLUDED_ADV
    used_nodes = {p["node"] for p in params}
    out = []
    ancestors = set().union(*(g.upstream(s) for s in samplers))
    ancestors -= set(samplers) | used_nodes
    for nid in sorted(ancestors):
        if len(out) >= 6:
            break
        node = g.nodes[nid]
        if g.role(nid) is not None:
            continue
        wv = g.widgets(node)
        for inp in node.get("inputs", []):
            name, typ = inp.get("name"), inp.get("type")
            if not (inp.get("widget") and typ in ("INT", "FLOAT", "COMBO")):
                continue
            if name in known_widgets:
                continue
            rule = _rule_for(name)
            ptype = "int" if typ == "INT" else ("float" if typ == "FLOAT" else "combo")
            default = wv.get(name)
            if rule:  # 规则优先：升级为带标签/区间的参数（数据文件一行）
                out.append({
                    "key": f"u{nid}_{name}", "node": nid, "widget": name,
                    "label": rule.get("label", f"{g.title(nid)} · {name}"),
                    "type": rule.get("type", ptype),
                    "min": rule.get("min"), "max": rule.get("max"),
                    "step": rule.get("step"),
                    "default": rule.get("default", default),
                    "options": rule.get("options", []), "seed_mode": None,
                })
            elif ptype == "combo":  # 未知下拉：至少保留当前值可选，绝不对字符串做 float()
                out.append({
                    "key": f"u{nid}_{name}", "node": nid, "widget": name,
                    "label": f"{g.title(nid)} · {name}", "type": "combo",
                    "min": None, "max": None, "step": None,
                    "default": default if isinstance(default, str) else "",
                    "options": [default] if isinstance(default, str) and default else [],
                    "seed_mode": None,
                })
            else:  # 无规则：通用高级参数（区间从当前值猜测）
                if ptype == "float":
                    fall = {"min": 0, "max": max(_num(default) * 2 + 1e-6, 1),
                            "step": 0.01}
                else:
                    fall = {"min": 0, "max": max(int(_num(default)) * 4, 1), "step": 1}
                out.append({
                    "key": f"u{nid}_{name}", "node": nid, "widget": name,
                    "label": f"{g.title(nid)} · {name}", "type": ptype,
                    "min": fall["min"], "max": fall["max"], "step": fall["step"],
                    "default": default if isinstance(default, (int, float)) else _num(default),
                    "options": [], "seed_mode": None,
                })
            known_widgets.add(name)
    return out


def _derive_drop(g: _GraphIndex, samplers: list, group_members: set,
                 save_node: Optional[int]) -> set:
    """死节点 = 全图 − 存活 − 组成员。存活 = 采样器祖先 ∪ 保存/预览祖先。"""
    keep = set().union(*(g.upstream(s) for s in samplers)) if samplers else set()
    save_anc = set()
    for sv in set(g.saves()) | set(g.previews()):
        save_anc |= g.upstream(sv)
    keep |= save_anc
    dead = set(g.nodes) - keep - group_members
    dead -= {save_node} if save_node else set()
    return dead


# 其它节点暴露的控件类型：数值、下拉、文本、布尔。
# 布尔在 ComfyUI API 里就是字符串 'true'/'false'，当 combo 暴露最稳妥。
_MISC_TYPES = {"INT", "FLOAT", "COMBO", "STRING", "BOOLEAN"}
_MISC_PER_NODE_MAX = 12   # 单个节点暴露上限（LoraLoaderBlockWeight 类节点控件多，防刷屏）
_MISC_TOTAL_MAX = 40      # 全局上限


def _derive_misc_nodes(g: _GraphIndex, samplers: list, params: list, groups: list,
                       group_members: set, save_node: Optional[int],
                       object_info) -> tuple:
    """其它节点：把所有存活、非组、未声明的可编辑控件暴露成参数（键 m{nid}_{name}）。

    与高级参数（_derive_advanced_params，仅采样器祖先 + 未识别角色 + 数值、上限 6）互补：
    遍历 ALL 存活节点（含已识别角色节点的隐藏控件——LoRA 分层权重、预处理阈值、
    ControlNet 变体额外旋钮都在这），控件类型含 STRING/BOOLEAN。这样工作流里任何
    节点在手机端都"有地方显示"，不再静默不可见。

    不变量：
    - 死节点（不在存活集）、源里 mute(mode=1)/bypass(mode=4) 的节点、节点组成员（随组
      开关剔除，编辑会被丢弃）、保存/预览节点一律不暴露——这些在任何组状态下都不在
      有效图，暴露只会是手机面板上点了没反应的死控件；
    - claimed 按节点（node-scoped）：标准/高级/组参数只占用它们声明所在节点的同名控件，
      两个相同节点（如两个 LoraLoaderBlockWeight）各自的控件都能暴露，不再只露第一个；
    - _EXCLUDED_ADV 仍是全局语义（核心控件名永不暴露）；
    - 键 m{nid}_{name}，不重命名任何既有 key（会话保存兼容）。
    """
    claimed_by_node: dict[int, set] = {}
    for p in params:
        claimed_by_node.setdefault(p["node"], set()).add(p["widget"])
    for grp in groups:
        for ep in grp.get("extra_params", []):
            claimed_by_node.setdefault(ep["node"], set()).add(ep["widget"])

    keep = set().union(*(g.upstream(s) for s in samplers)) if samplers else set()
    for sv in set(g.saves()) | set(g.previews()):
        keep |= g.upstream(sv)

    out_params, keys_by_node = [], {}
    total = 0
    truncated_nodes, truncated_total = [], []
    for nid in sorted(keep):
        if nid == save_node or nid in group_members:
            continue
        node = g.nodes[nid]
        if node.get("mode") in (1, 4):
            continue  # 源里 mute/bypass：任何组状态下都不在有效图，暴露只产生死控件
        if g.role(nid) in ("save", "preview"):
            continue  # 保存/预览只关乎展示，不暴露可调控件
        wv = g.widgets(node)
        node_claimed = claimed_by_node.setdefault(nid, set())
        node_keys, per_node = [], 0
        available = 0  # 本节点可暴露但被上限截断的控件数（诊断用）
        for inp in node.get("inputs", []):
            name, typ = inp.get("name"), inp.get("type")
            if not (inp.get("widget") and not inp.get("link")):
                continue
            if name == "upload" or typ == "IMAGEUPLOAD" or typ not in _MISC_TYPES:
                continue
            if name in _EXCLUDED_ADV:
                continue
            if name in node_claimed:
                continue
            if total >= _MISC_TOTAL_MAX or per_node >= _MISC_PER_NODE_MAX:
                available += 1
                continue
            default = wv.get(name)
            ptype = ("int" if typ == "INT" else "float" if typ == "FLOAT"
                     else "combo" if typ in ("COMBO", "BOOLEAN") else "string")
            key = f"m{nid}_{name}"
            rule = _rule_for(name)
            if rule:  # 规则优先：预注册的控件升级为带标签/区间的参数（数据文件一行）
                out_params.append({
                    "key": key, "node": nid, "widget": name,
                    "label": rule.get("label", f"{g.title(nid)} · {name}"),
                    "type": rule.get("type", ptype),
                    "min": rule.get("min"), "max": rule.get("max"),
                    "step": rule.get("step"),
                    "default": rule.get("default", default),
                    "options": rule.get("options", []), "seed_mode": None,
                })
            elif ptype == "combo":
                dv = default if isinstance(default, str) else ("true" if default else "false")
                options = (["true", "false"] if typ == "BOOLEAN"
                           else ([dv] if dv else []))
                out_params.append({
                    "key": key, "node": nid, "widget": name,
                    "label": f"{g.title(nid)} · {name}", "type": "combo",
                    "min": None, "max": None, "step": None,
                    "default": dv, "options": options, "seed_mode": None,
                })
            elif ptype == "string":
                out_params.append({
                    "key": key, "node": nid, "widget": name,
                    "label": f"{g.title(nid)} · {name}", "type": "string",
                    "min": None, "max": None, "step": None,
                    "default": (default if isinstance(default, str)
                                else (str(default) if default is not None else "")),
                    "options": [], "seed_mode": None,
                })
            else:  # 数值：区间从 object_info 元数据或当前值猜测
                if ptype == "float":
                    fall = {"min": 0, "max": max(_num(default) * 2 + 1e-6, 1),
                            "step": 0.01}
                else:
                    fall = {"min": 0, "max": max(int(_num(default)) * 4, 1), "step": 1}
                meta = _clamped(_oi_meta(object_info, node.get("type"), name), name, default)
                out_params.append({
                    "key": key, "node": nid, "widget": name,
                    "label": f"{g.title(nid)} · {name}", "type": ptype,
                    "min": meta.get("min", fall["min"]),
                    "max": meta.get("max", fall["max"]),
                    "step": meta.get("step", fall["step"]),
                    "default": default if isinstance(default, (int, float)) else _num(default),
                    "options": [], "seed_mode": None,
                })
            node_claimed.add(name)
            node_keys.append(key)
            per_node += 1
            total += 1
        if available:
            truncated_nodes.append((nid, available))
        if node_keys:
            keys_by_node[nid] = node_keys

    # 截断诊断：上限是防刷屏的软限制，静默丢控件会让用户以为"没有其它控件"
    if truncated_nodes:
        log.warning("misc 控件被上限截断: %s", ", ".join(
            f"节点 {nid}({g.title(nid)}) 剩 {n} 个" for nid, n in truncated_nodes))
    if len(out_params) >= _MISC_TOTAL_MAX:
        log.warning("misc 控件总数达到上限 %d，其余节点控件未暴露", _MISC_TOTAL_MAX)

    entries = [{"node": nid, "title": g.title(nid), "keys": keys_by_node[nid]}
               for nid in sorted(keys_by_node)]
    return out_params, entries


# ---------------------------------------------------------------------------
# 生成后自校验 + 转换 dry-run（导入 API 与测试共用）
# ---------------------------------------------------------------------------

def validate_generated(raw: dict, ui: dict) -> list:
    """raw schema 对 workflow 的校验（复用 validate_against_workflow）。"""
    from .schema import parse_schema, validate_against_workflow
    schema = parse_schema(raw)
    return validate_against_workflow(schema, ui)


def dry_run(ui: dict, raw: dict) -> dict:
    """转换 dry-run：每种组开关两态 + 无 LoRA/带首个 LoRA，查链接完整性。

    返回 {ok, problems, diagnostics}。链接完整性的关键不变量：
    模板里带链接且非 widget 的输入，序列化后必须仍有指向现存节点的链接；
    所有链接 o_id 必须是字符串且存在于 api（ComfyUI 校验契约）。
    """
    from .schema import parse_schema
    from .workflow_converter import ConvertOptions, convert_ui_to_api
    schema = parse_schema(raw)
    problems, states = [], []

    group_keys = [grp["key"] for grp in raw.get("node_groups", [])]
    # 全量默认 LoRA（不是第一个）——多 LoRA 模板必须整条链都能 dry-run 通过
    lora_default = list(raw.get("lora", {}).get("default") or [])

    # 每组单独 off/on 两态 + 全默认/全开/全关（patch 组默认关也必须测开启态）
    for grp in group_keys:
        states.append({"desc": f"group:{grp}=off", "enabled_groups": {grp: False}})
        states.append({"desc": f"group:{grp}=on", "enabled_groups": {grp: True}})
    states.append({"desc": "all groups default", "enabled_groups": {}})
    states.append({"desc": "all groups on", "enabled_groups": {k: True for k in group_keys}})
    states.append({"desc": "all groups off", "enabled_groups": {k: False for k in group_keys}})

    for loras in ([], lora_default):
        for st in states:
            try:
                api, diag = convert_ui_to_api(
                    ui, schema, ConvertOptions(
                        params={}, enabled_groups=st["enabled_groups"], image_slots={}))
            except Exception as e:  # noqa: BLE001
                problems.append(f"[{st['desc']}] loras={len(loras)} 转换抛错: {e}")
                continue
            for p in _link_problems(ui, api):
                problems.append(f"[{st['desc']}] loras={len(loras)} {p}")
            # 开启的 patch 组：实际应用的补线数必须等于声明的 LinkOps 数
            expected = 0
            for grp in raw.get("node_groups", []):
                if grp.get("mode") == "patch" and st["enabled_groups"].get(grp["key"]):
                    expected += len(grp.get("patches_enable", []))
            applied = len(diag.get("patches", [])) if diag else 0
            if expected and applied != expected:
                problems.append(
                    f"[{st['desc']}] patch 补线数不符: 声明 {expected} 实际 {applied}")

    diag = {"api_node_count": 0, "problems": problems}
    if not problems:
        api, _diag = convert_ui_to_api(ui, schema, ConvertOptions(
            params={}, enabled_groups={}, image_slots={}))
        diag["api_node_count"] = len(api)
        diag["removed"] = _diag.get("removed", [])
        diag["bypassed"] = _diag.get("bypassed", [])
        diag["patches"] = _diag.get("patches", [])
        diag["loras"] = _diag.get("loras", [])
    return {"ok": not problems, "problems": problems, "diagnostics": diag}


def _expected_bypass_dangle(ui: dict, orig: dict, name: str) -> bool:
    """该输入悬空是否"预期"：沿透传源链追到底，某个透传节点没有同类型的连线输入。

    bypass_rewire 对"对应输入是 widget/空（无连线）"的透传节点无法重连 → 删除节点时
    下游链一并移除，目标输入悬空。链式透传（A→B→C 全 bypass）时只查一层会误判：
    B 有连线输入（来自 A）看似可重连，但 A 的输入是空的，追到底仍然悬空。
    这里沿输入链一路追，只要追到"无同类型连线输入"的透传节点 → 悬空属预期
    （源工作流 ComfyUI bypass 同样丢链，不算转换错误）；追到非透传节点 → 重连
    应当成功，悬空是真错误。
    """
    link_id = orig.get("link")
    if not link_id:
        return False
    link_map = {x[0]: x for x in ui.get("links", []) if len(x) >= 6}
    node_map = {n["id"]: n for n in ui.get("nodes", [])}
    l = link_map.get(link_id)
    if not l:
        return False
    oid, oslot = l[1], l[2]
    src = node_map.get(oid)
    if not src or src.get("mode") != 4:
        return False  # 源不是透传节点：重连本该成功，悬空是真错误
    otype = (src.get("outputs", [])[oslot].get("type")
             if 0 <= oslot < len(src.get("outputs", [])) else None)
    seen = set()
    while src is not None and src.get("mode") == 4 and src.get("id") not in seen:
        seen.add(src.get("id"))
        matched = next((i for i in src.get("inputs", [])
                        if i.get("type") == otype and i.get("link")), None)
        if matched is None:
            return True  # 无同类型连线输入 → 无法重连 → 悬空属预期
        l2 = link_map.get(matched.get("link"))
        if not l2:
            return False
        src = node_map.get(l2[1])
        if src is None:
            return False
    return False


def _link_problems(ui: dict, api: dict) -> list:
    problems = []
    for nid_str, node in api.items():
        try:
            nid = int(nid_str)
        except ValueError:
            continue
        orig = next((n for n in ui.get("nodes", []) if n["id"] == nid), None)
        if orig is None:
            continue
        for inp in orig.get("inputs", []):
            name = inp.get("name")
            if inp.get("link") and not inp.get("widget"):
                val = node["inputs"].get(name)
                if not (isinstance(val, list) and len(val) == 2):
                    if _expected_bypass_dangle(ui, inp, name):
                        continue  # 源 bypass 节点无法重连，源工作流本就这么悬空
                    problems.append(f"节点 {nid}({orig.get('type')}) 必填链接输入 '{name}' 缺失")
                elif str(val[0]) not in api:
                    problems.append(f"节点 {nid} 输入 '{name}' 指向缺失节点 {val[0]}")
    for nid, node in api.items():
        for name, val in node["inputs"].items():
            if isinstance(val, list) and len(val) == 2 and isinstance(val[0], str):
                if val[0] not in api:
                    problems.append(f"{nid}.{name} 链接指向缺失节点 {val[0]}")
    return problems


def count_samplers(ui: dict) -> int:
    return len(_GraphIndex(ui).samplers())


def describe(ui: dict) -> str:
    """导入用的一句话描述：识别出的特性摘要。"""
    g = _GraphIndex(ui)
    parts = []
    if g.samplers():
        parts.append("KSampler")
    if any(g.role(n) == "lora" for n in g.nodes):
        parts.append("LoRA")
    if any(g.role(n) == "ipa_modifier" for n in g.nodes):
        parts.append("IPAdapter")
    if any(g.role(n) == "controlnet_apply" for n in g.nodes):
        parts.append("ControlNet")
    if len(g.samplers()) >= 2:
        parts.append("多段生成")
    return " + ".join(parts) + f" · {len(g.nodes)} 节点"
