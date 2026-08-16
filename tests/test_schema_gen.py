"""自动导入识别引擎测试：全部来源工作流 → schema → 校验 → dry-run。

离线运行（object_info=None，走内置 fallback）。只读来源目录，不写任何文件。
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import schema_gen as sg  # noqa: E402
from core.schema import merge_schema, parse_schema  # noqa: E402

# 来源工作流目录（自动导入引擎的测试输入）：用环境变量 COMFY_WORKFLOWS_DIR 指向你的
# ComfyUI 用户工作流目录；目录不存在时相关测试自动跳过（skipUnless）。
SRC = Path(os.environ.get("COMFY_WORKFLOWS_DIR", r"ComfyUI\user\default\workflows"))


def _load_ui(name: str) -> dict:
    return json.loads((SRC / name).read_text(encoding="utf-8"))


def _no_sampler_ui() -> dict:
    """纯预览草稿：LoadImage → SaveImage，无采样器 → generate_schema 必须拒绝。"""
    return {
        "last_node_id": 3,
        "last_link_id": 2,
        "nodes": [
            {"id": 1, "type": "LoadImage", "mode": 0, "order": 0,
             "inputs": [{"name": "image", "type": "IMAGEUPLOAD",
                         "widget": {"name": "image"}, "link": None}],
             "widgets_values": ["x.png"],
             "outputs": [{"name": "IMAGE", "type": "IMAGE", "links": [1]}]},
            {"id": 2, "type": "SaveImage", "mode": 0, "order": 1,
             "inputs": [
                 {"name": "images", "type": "IMAGE", "link": 1},
                 {"name": "filename_prefix", "type": "STRING",
                  "widget": {"name": "filename_prefix"}, "link": None},
             ],
             "widgets_values": ["x"],
             "outputs": []},
        ],
        "links": [[1, 1, 0, 2, 0, "IMAGE"]],
    }


class TestSanitizeId(unittest.TestCase):
    def test_ascii(self):
        self.assertEqual(sg.sanitize_id("2LoRa-2-try -single.json"), "2lora-2-try-single")
        self.assertEqual(sg.sanitize_id("amiya.json"), "amiya")

    def test_empty_for_pure_non_ascii(self):
        self.assertEqual(sg.sanitize_id("角色.json"), "")


@unittest.skipUnless(SRC.exists(), "来源工作流目录不存在")
class TestAllSourceWorkflows(unittest.TestCase):
    """全部来源工作流生成 schema、通过校验与 dry-run（每组开关两态）。"""

    @classmethod
    def setUpClass(cls):
        cls.files = sorted(SRC.glob("*.json"))
        assert cls.files, "来源工作流目录为空"

    def test_no_sampler_workflow_rejected(self):
        with self.assertRaises(sg.NoSamplerError):
            sg.generate_schema(_no_sampler_ui(), None, schema_id="no_sampler", title="no_sampler")

    def test_all_source_workflows_generate_validate_dryrun(self):
        for f in self.files:
            ui = _load_ui(f.name)
            sid = sg.sanitize_id(f.name)
            if not sg.count_samplers(ui):
                # 无采样器草稿：必须拒绝（不能生成半残面板）
                with self.assertRaises(sg.NoSamplerError, msg=f.name):
                    sg.generate_schema(ui, None, schema_id=sid, title=f.stem)
                continue
            raw = sg.generate_schema(ui, None, schema_id=sid, title=f.stem)
            self.assertGreaterEqual(sg.count_samplers(ui), 1, f.name)

            problems = sg.validate_generated(raw, ui)
            self.assertEqual(problems, [], f"{f.name} 校验失败: {problems[:6]}")

            dr = sg.dry_run(ui, raw)
            self.assertTrue(dr["ok"], f"{f.name} dry-run 失败: {dr['problems'][:6]}")

    def test_params_node_ids_resolve(self):
        """每个 param 的 node/widget 必须真实存在于 workflow。"""
        for f in self.files:
            if f.name == "2nd_expirience.json":
                continue
            ui = _load_ui(f.name)
            nodes = {n["id"] for n in ui["nodes"]}
            raw = sg.generate_schema(ui, None, schema_id=f.stem, title=f.stem)
            for p in raw["params"]:
                self.assertIn(p["node"], nodes, f"{f.name} param {p['key']} 节点缺失")
                self.assertTrue(any(i.get("name") == p["widget"]
                                    for n in ui["nodes"] if n["id"] == p["node"]
                                    for i in n.get("inputs", [])),
                                f"{f.name} param {p['key']} widget 缺失")


class TestAmiyaRecognition(unittest.TestCase):
    """对照手写 amiya schema：模型链 / LoRA 链 / openpose 群含下游预览 / VAE 补线。

    amiya 的 schema 手写受保护、绝不重新生成；回归基准是保护副本 templates/amiya.json
    （ComfyUI 源目录的同名文件会被用户随时编辑，不能当回归基准）。
    """

    @classmethod
    def setUpClass(cls):
        canon = Path(__file__).resolve().parents[1] / "templates" / "amiya.json"
        cls.ui = json.loads(canon.read_text(encoding="utf-8"))
        cls.raw = sg.generate_schema(cls.ui, None, schema_id="amiya", title="amiya")
        cls.g = sg._GraphIndex(cls.ui)

    def test_model_and_lora_chain(self):
        self.assertEqual(self.raw["model"], {"node": 4, "widget": "ckpt_name", "label": "底模"})
        self.assertEqual(self.raw["lora"]["anchor"], {"node": 4, "model_slot": 0, "clip_slot": 1})
        self.assertEqual(self.raw["lora"]["template_lora_chain"], [10])
        self.assertEqual(self.raw["lora"]["default"][0]["strength_model"], 0.85)

    def test_openpose_patch_group_contains_downstream_preview(self):
        grp = next(g for g in self.raw["node_groups"] if g["mode"] == "patch")
        self.assertIn(29, grp["nodes"], "下游 PreviewImage 必须收进组，否则关组会悬空")
        self.assertIn(17, grp["nodes"])

    def test_patch_has_vae_add(self):
        grp = next(g for g in self.raw["node_groups"] if g["mode"] == "patch")
        vae_adds = [p for p in grp["patches_enable"] if p.get("add_type") == "VAE"]
        self.assertEqual(len(vae_adds), 1)
        self.assertEqual(vae_adds[0]["add"], [[4, 2], [17, 3]])

    def test_drop_equals_dead_preprocessor(self):
        # 只有未接线的死节点被 drop（规范 amiya 里是孤立的 DWPreprocessor #19）；
        # 存活的 CN apply(17) 与 text encoder(6/7) 必须保留在组/有效图里
        self.assertEqual(self.raw["drop"], [19])


class TestMultiSampler(unittest.TestCase):
    """refer1/refer2 双采样器：终采样器是 refine 段，但抽卡参数（seed/steps）落在 base 段。"""

    def test_terminal_is_refine_seed_is_base(self):
        # refer1/refer2 已被用户从来源目录改名 → 读冻结快照（同 1st_boxed 等惯例）
        fixtures = Path(__file__).resolve().parent / "fixtures"
        for name in ("refer1.json", "refer2.json"):
            ui = json.loads((fixtures / name).read_text(encoding="utf-8"))
            g = sg._GraphIndex(ui)
            samplers = g.samplers()
            self.assertEqual(len(samplers), 2, name)
            terminal = g.terminal_sampler(samplers)
            base = g.base_sampler(samplers)
            self.assertNotEqual(terminal, base, name)

            # 终采样器 = refine（denoise<1）
            t_wv = g.widgets(g.nodes[terminal])
            self.assertLess(float(t_wv["denoise"]), 1.0, name)
            # 抽卡 seed 落在 base（denoise==1.0 的主生成段）——否则重抽只变精修段
            seed_p = next(p for p in raw_params(ui, name) if p["key"] == "seed")
            self.assertEqual(seed_p["node"], base, name)
            self.assertEqual(float(g.widgets(g.nodes[seed_p["node"]])["denoise"]), 1.0, name)
            self.assertEqual(raw_params(ui, name)[0]["node"], seed_p["node"])


def raw_params(ui, name):
    raw = sg.generate_schema(ui, None, schema_id=name.split(".")[0], title=name)
    return raw["params"]


class TestAdvancedParams(unittest.TestCase):
    """未知节点数值控件浮成高级参数（防御路径）。"""

    def _min_ui(self):
        nodes = [
            {"id": 1, "type": "KSampler", "mode": 0, "order": 5, "pos": [0, 0], "size": [0, 0],
             "inputs": [
                 {"name": "model", "type": "MODEL", "link": 1},
                 {"name": "positive", "type": "CONDITIONING", "link": 2},
                 {"name": "negative", "type": "CONDITIONING", "link": 3},
                 {"name": "latent_image", "type": "LATENT", "link": 4},
                 {"name": "seed", "type": "INT", "widget": {"name": "seed"}, "link": None},
                 {"name": "steps", "type": "INT", "widget": {"name": "steps"}, "link": None},
                 {"name": "cfg", "type": "FLOAT", "widget": {"name": "cfg"}, "link": None},
                 {"name": "sampler_name", "type": "COMBO", "widget": {"name": "sampler_name"}, "link": None},
                 {"name": "scheduler", "type": "COMBO", "widget": {"name": "scheduler"}, "link": None},
                 {"name": "denoise", "type": "FLOAT", "widget": {"name": "denoise"}, "link": None},
             ],
             "widgets_values": [42, 28, 4.0, "dpmpp_2m", "karras", 0.8],
             "outputs": [{"name": "LATENT", "type": "LATENT", "links": []}]},
            {"id": 2, "type": "MysteryAdapter", "mode": 0, "order": 3, "pos": [0, 0], "size": [0, 0],
             "inputs": [
                 {"name": "model", "type": "MODEL", "link": 6},
                 {"name": "fancy_ratio", "type": "FLOAT", "widget": {"name": "fancy_ratio"}, "link": None},
                 {"name": "tiles", "type": "INT", "widget": {"name": "tiles"}, "link": None},
             ],
             "widgets_values": [0.5, 2],
             "outputs": [{"name": "MODEL", "type": "MODEL", "links": [1]}]},
            {"id": 3, "type": "EmptyLatentImage", "mode": 0, "order": 1, "pos": [0, 0], "size": [0, 0],
             "inputs": [
                 {"name": "width", "type": "INT", "widget": {"name": "width"}, "link": None},
                 {"name": "height", "type": "INT", "widget": {"name": "height"}, "link": None},
                 {"name": "batch_size", "type": "INT", "widget": {"name": "batch_size"}, "link": None},
             ],
             "widgets_values": [1024, 1024, 1],
             "outputs": [{"name": "LATENT", "type": "LATENT", "links": [4]}]},
            {"id": 4, "type": "CLIPTextEncode", "mode": 0, "order": 2, "pos": [0, 0], "size": [0, 0],
             "inputs": [
                 {"name": "clip", "type": "CLIP", "link": None},
                 {"name": "text", "type": "STRING", "widget": {"name": "text"}, "link": None},
             ],
             "widgets_values": ["hello"],
             "outputs": [{"name": "CONDITIONING", "type": "CONDITIONING", "links": [2]}]},
            {"id": 5, "type": "CheckpointLoaderSimple", "mode": 0, "order": 0, "pos": [0, 0], "size": [0, 0],
             "inputs": [
                 {"name": "ckpt_name", "type": "COMBO", "widget": {"name": "ckpt_name"}, "link": None},
             ],
             "widgets_values": ["model.safetensors"],
             "outputs": [
                 {"name": "MODEL", "type": "MODEL", "links": [6]},
                 {"name": "CLIP", "type": "CLIP", "links": [7]},
                 {"name": "VAE", "type": "VAE", "links": []},
             ]},
        ]
        links = [
            [1, 2, 0, 1, 0, "MODEL"],        # 2 -> 1.model
            [2, 4, 0, 1, 1, "CONDITIONING"],  # 4 -> 1.positive
            [3, 4, 0, 1, 2, "CONDITIONING"],  # 4 -> 1.negative
            [4, 3, 0, 1, 3, "LATENT"],        # 3 -> 1.latent_image
            [5, 5, 0, 2, 0, "MODEL"],        # 5 -> 2.model
            [6, 5, 1, 4, 0, "CLIP"],          # 5 -> 4.clip
        ]
        return {"last_node_id": 5, "last_link_id": 6, "nodes": nodes, "links": links}

    def test_unknown_widgets_surface_as_advanced(self):
        ui = self._min_ui()
        raw = sg.generate_schema(ui, None, schema_id="min", title="min")
        adv = [p for p in raw["params"] if p["key"].startswith("u")]
        self.assertEqual(len(adv), 2)
        keys = {p["key"] for p in adv}
        self.assertIn("u2_fancy_ratio", keys)
        self.assertIn("u2_tiles", keys)
        self.assertEqual(next(p for p in adv if p["key"] == "u2_fancy_ratio")["type"], "float")
        self.assertEqual(next(p for p in adv if p["key"] == "u2_tiles")["type"], "int")
        dr = sg.dry_run(ui, raw)
        self.assertTrue(dr["ok"], dr["problems"][:6])

    def test_rules_registry_promotes_widget(self):
        """新 widget 在 schemas/_rules.json 加一行 → 直接升级为带标签/区间的参数。"""
        ui = self._min_ui()
        raw = sg.generate_schema(ui, None, schema_id="min", title="min")
        fr = next(p for p in raw["params"] if p["key"] == "u2_fancy_ratio")
        self.assertEqual(fr["label"], "花哨比例", "规则 label 必须生效（数据文件驱动）")
        self.assertEqual(fr["min"], 0)
        self.assertEqual(fr["max"], 2)
        self.assertEqual(fr["default"], 0.5)
        # 无规则的 tiles 走通用高级参数（区间从当前值猜测）
        t = next(p for p in raw["params"] if p["key"] == "u2_tiles")
        self.assertEqual(t["max"], 8)  # default=2 → max=2*4


class TestMergeAndParse(unittest.TestCase):
    def test_parse_schema_roundtrip(self):
        raw = {
            "id": "x", "title": "X", "workflow": "x.json", "save_output_node": 0,
            "model": {"node": 1, "widget": "ckpt_name"},
            "params": [{"key": "steps", "node": 2, "widget": "steps", "label": "步数",
                        "type": "int", "default": 28}],
            "node_groups": [{"key": "g", "label": "G", "nodes": [3], "mode": "patch",
                             "patches_enable": [{"add": [[1, 0], [3, 0]], "add_type": "CONDITIONING"}]}],
            "image_slots": [{"key": "s", "label": "S", "node": 4, "group": "g"}],
            "drop": [5],
        }
        s = parse_schema(raw)
        self.assertEqual(s.id, "x")
        self.assertEqual(s.params[0].default, 28)
        self.assertEqual(s.node_groups[0].patches_enable[0].action, "add")
        self.assertEqual(s.image_slots[0].group, "g")

    def test_merge_schema_group_extra_params_by_key(self):
        base = {
            "id": "x", "title": "X", "workflow": "x.json", "save_output_node": 0,
            "node_groups": [
                {"key": "ipa", "label": "IPA", "nodes": [1], "mode": "bypass",
                 "extra_params": [
                     {"key": "ipa_weight", "node": 1, "widget": "weight", "label": "IPA 权重",
                      "type": "float", "min": 0, "max": 2},
                     {"key": "ipa_start", "node": 1, "widget": "start_at", "label": "IPA 起始",
                      "type": "float", "min": 0, "max": 1},
                 ]},
            ],
        }
        ov = {"node_groups": [
            {"key": "ipa", "extra_params": [
                {"key": "ipa_weight", "default": 1.2, "label": "自定义权重"}]},
        ]}
        merged = merge_schema(base, ov)
        grp = merged["node_groups"][0]
        eps = {p["key"]: p for p in grp["extra_params"]}
        self.assertEqual(len(grp["extra_params"]), 2, "只覆盖 ipa_weight，不能丢掉 ipa_start")
        self.assertEqual(eps["ipa_weight"]["default"], 1.2)
        self.assertEqual(eps["ipa_weight"]["label"], "自定义权重")
        self.assertEqual(eps["ipa_weight"]["min"], 0, "未覆盖字段保留")
        self.assertIn("ipa_start", eps)

    def test_merge_schema_params_by_key(self):
        base = {"params": [{"key": "steps", "node": 2, "widget": "steps", "label": "步数",
                            "type": "int", "min": 1, "max": 150}]}
        merged = merge_schema(base, {"params": [{"key": "steps", "max": 500}]})
        self.assertEqual(merged["params"][0]["max"], 500)
        self.assertEqual(merged["params"][0]["min"], 1)

    def test_merge_schema_scalar_replace(self):
        base = {"title": "A", "drop": [1], "lora": {"max": 8, "connection": "serial"}}
        merged = merge_schema(base, {"title": "B", "drop": [2], "lora": {"max": 4}})
        self.assertEqual(merged["title"], "B")
        self.assertEqual(merged["drop"], [2])
        self.assertEqual(merged["lora"], {"max": 4, "connection": "serial"})

    def test_merge_schema_adds_new_group(self):
        base = {"node_groups": [{"key": "a", "label": "A", "nodes": [1]}]}
        merged = merge_schema(base, {"node_groups": [{"key": "b", "label": "B", "nodes": [2]}]})
        self.assertEqual([g["key"] for g in merged["node_groups"]], ["a", "b"])


class TestRegistration(unittest.TestCase):
    """注册表原子性（临时 DATA_DIR，不污染真实数据）。"""

    def test_register_check_then_act(self):
        from unittest.mock import patch
        import tempfile

        with tempfile.TemporaryDirectory() as td, patch("core.template_store.DATA_DIR", Path(td)):
            from core import template_store as ts
            self.assertFalse(ts.is_registered("x"))
            entry = {"id": "x", "title": "X", "workflow": "x.json", "schema": "x.json"}
            self.assertTrue(ts.register_template(entry))
            self.assertTrue(ts.is_registered("x"))
            # 重复注册且不允许覆盖 -> False
            self.assertFalse(ts.register_template(entry))
            # 允许覆盖 -> True，且只剩一条
            self.assertTrue(ts.register_template(entry, overwrite=True))
            reg = json.loads((Path(td) / "templates.json").read_text(encoding="utf-8"))
            self.assertEqual([t["id"] for t in reg], ["x"])
            self.assertTrue(ts.unregister_template("x"))
            self.assertFalse(ts.is_registered("x"))


class TestMultiLoRA(unittest.TestCase):
    """2LoRa 系列：default 与 template_lora_chain 必须按应用顺序覆盖整条链，
    否则链头 LoRA 会留在工作流里悄悄生效、用户在面板上看不到也改不了。"""

    MULTI_LORA = ("2LoRa-1-try.json", "2LoRa-2-try.json",
                  "2LoRa-2-try -single.json", "2LoRa-3-try.json")

    @unittest.skipUnless(SRC.exists(), "来源工作流目录不存在")
    def test_default_and_chain_cover_all_loras(self):
        for name in self.MULTI_LORA:
            ui = _load_ui(name)
            raw = sg.generate_schema(ui, None, schema_id=name.split(".")[0], title=name)
            g = sg._GraphIndex(ui)
            terminal = g.terminal_sampler(g.samplers())
            chain_loras = [nid for nid in sg._model_chain(g, terminal) if g.role(nid) == "lora"]
            app_order = list(reversed(chain_loras))  # 应用顺序：checkpoint 侧在前
            self.assertGreater(len(app_order), 1, name)
            self.assertEqual(raw["lora"]["template_lora_chain"], app_order,
                             f"{name}: 模板链要按应用顺序覆盖全部 LoRA")
            self.assertEqual(len(raw["lora"]["default"]), len(app_order), name)
            for nid, slot in zip(app_order, raw["lora"]["default"]):
                wv = g.widgets(g.nodes[nid])
                self.assertEqual(slot["name"], wv.get("lora_name"), name)

    @unittest.skipUnless(SRC.exists(), "来源工作流目录不存在")
    def test_convert_applies_all_default_loras(self):
        """用默认 LoRA 列表跑一次转换：两条 LoRA 都要在 API 里、且串联顺序正确。"""
        from core.schema import parse_schema
        from core.workflow_converter import ConvertOptions, convert_ui_to_api
        ui = _load_ui("2LoRa-2-try.json")
        raw = sg.generate_schema(ui, None, schema_id="2lora-2-try", title="2lora-2-try")
        schema = parse_schema(raw)
        loras = raw["lora"]["default"]
        api, _diag = convert_ui_to_api(ui, schema, ConvertOptions(
            params={"loras": loras}, enabled_groups={}, image_slots={}))
        tpl = raw["lora"]["template_lora_chain"]  # [2, 16] 应用顺序
        self.assertEqual(api[str(tpl[0])]["inputs"]["lora_name"], loras[0]["name"])
        self.assertEqual(api[str(tpl[1])]["inputs"]["lora_name"], loras[1]["name"])
        self.assertEqual(api[str(tpl[1])]["inputs"]["model"], [str(tpl[0]), 0])


class TestImageSlotSync(unittest.TestCase):
    """参考图随工作流同步：values.image_slots 默认取工作流 LoadImage 已填的图，
    手机端不该要求重新上传工作流里已有的参考图。"""

    def test_slot_default_from_workflow(self):
        from api.panel import _slot_default
        wf = {"nodes": [
            {"id": 5, "widgets_values": ["ref.png"]},
            {"id": 6, "widgets_values": []},
            {"id": 7},
        ]}
        tpl = type("T", (), {"workflow": wf})()
        slot = type("S", (), {})()
        slot.node = 5
        self.assertEqual(_slot_default(tpl, slot), "ref.png")
        slot.node = 6
        self.assertIsNone(_slot_default(tpl, slot))
        slot.node = 7
        self.assertIsNone(_slot_default(tpl, slot))

    @unittest.skipUnless(
        (Path(__file__).resolve().parents[1] / "templates" / "amiya.json").exists(),
        "amiya 模板不存在")
    def test_default_values_populate_slots(self):
        from core.template_store import load_template
        from api.panel import _default_values
        tpl = load_template("amiya")
        vals = _default_values(tpl)
        self.assertIn("ipa_ref", vals["image_slots"])
        self.assertTrue(vals["image_slots"]["ipa_ref"].endswith(".png"))

    @unittest.skipUnless(
        (Path(__file__).resolve().parents[1] / "templates" / "amiya.json").exists(),
        "amiya 模板不存在")
    def test_values_deep_merges_image_slots(self):
        """会话只覆盖了 ipa_ref，其余槽位必须回落工作流默认参考图。"""
        from core.template_store import load_template
        from api.panel import values

        class FakeSession:
            def __init__(self, d):
                self.d = d

            def get(self, _tpl_id):
                return self.d

        load_template("amiya")  # 确保模板可加载
        saved = {"image_slots": {"ipa_ref": "custom.png"}}
        v = values("amiya", FakeSession(saved))
        slots = v["image_slots"]
        self.assertEqual(slots["ipa_ref"], "custom.png", "会话覆盖生效")
        self.assertIn("openpose_pose", slots, "未覆盖的槽位回落工作流默认图")
        self.assertIn("compose_ref", slots)


class TestAutoImport(unittest.TestCase):
    """启动自动同步：来源目录里未注册的工作流批量导入（幂等、离线安全）。"""

    @unittest.skipUnless(SRC.exists(), "来源工作流目录不存在")
    def test_auto_import_missing_idempotent(self):
        import shutil
        import tempfile
        from unittest.mock import patch

        from api import imports as apiimp
        from core import template_store as ts

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "src"
            schemas = Path(td) / "schemas"
            templates = Path(td) / "templates"
            data = Path(td) / "data"
            for d in (src, schemas, templates, data):
                d.mkdir()
            # 真实来源副本 + 一个无采样器的合成草稿（必须跳过）
            src_files = list(SRC.glob("*.json"))
            n_sampler_files = 0
            for f in src_files:
                shutil.copy2(f, src / f.name)
                if sg.count_samplers(json.loads(f.read_text(encoding="utf-8"))):
                    n_sampler_files += 1
            (src / "draft.json").write_text(
                json.dumps(_no_sampler_ui()), encoding="utf-8")

            with patch("core.schema_gen.source_workflows_dir", return_value=src), \
                 patch("api.imports.SCHEMAS_DIR", schemas), \
                 patch("api.imports.TEMPLATES_DIR", templates), \
                 patch("core.template_store.DATA_DIR", data):
                # 冷启动：可导入的全部导入，无采样器草稿跳过
                r1 = apiimp.auto_import_missing(None)
                self.assertEqual(len(r1["imported"]), n_sampler_files, r1)
                self.assertEqual(len(r1["skipped"]), 1, r1)
                self.assertIn("draft", " ".join(r1["skipped"]))
                self.assertEqual(r1["errors"], [], r1)
                self.assertTrue(ts.is_registered("amiya"))
                self.assertTrue(ts.is_registered("2lora-2-try-single"))

                # 幂等：再跑一遍，已注册的静默跳过，只剩无采样器草稿
                r2 = apiimp.auto_import_missing(None)
                self.assertEqual(r2["imported"], [], r2)
                self.assertEqual(len(r2["skipped"]), 1)
                self.assertEqual(r2["errors"], [])

                # 落盘物：schema + workflow 副本一一对应
                schema_names = sorted(p.name for p in schemas.glob("*.json"))
                tpl_names = sorted(p.name for p in templates.glob("*.json"))
                self.assertEqual(schema_names, tpl_names)
                self.assertEqual(len(schema_names), n_sampler_files)

                # 删除注册后，目录里已有孤儿 schema → 直接复用注册，不重新生成
                self.assertTrue(ts.unregister_template("amiya"))
                r3 = apiimp.auto_import_missing(None)
                self.assertIn("amiya", r3["imported"], "孤儿 schema 应被复用")


# ---------------------------------------------------------------------------
# 审计修复测试：合成工作流验证各项"默认到第一个/单个"缺陷的修复
# ---------------------------------------------------------------------------

def _mk_ui(nodes, links):
    return {"last_node_id": max(n["id"] for n in nodes),
            "last_link_id": max(l[0] for l in links),
            "nodes": nodes, "links": links}


def _n(nid, ntype, order, inputs, outputs, widgets):
    return {"id": nid, "type": ntype, "mode": 0, "order": order,
            "pos": [0, 0], "size": [0, 0], "inputs": inputs, "outputs": outputs,
            "widgets_values": widgets}


def _text(nid, order, text="hello"):
    return _n(nid, "CLIPTextEncode", order, [
        {"name": "clip", "type": "CLIP", "link": None},
        {"name": "text", "type": "STRING", "widget": {"name": "text"}, "link": None},
    ], [{"name": "CONDITIONING", "type": "CONDITIONING", "links": []}], [text])


def _sampler(nid, order, pos_l, neg_l, lat_l, model_l):
    return _n(nid, "KSampler", order, [
        {"name": "model", "type": "MODEL", "link": model_l},
        {"name": "positive", "type": "CONDITIONING", "link": pos_l},
        {"name": "negative", "type": "CONDITIONING", "link": neg_l},
        {"name": "latent_image", "type": "LATENT", "link": lat_l},
        {"name": "seed", "type": "INT", "widget": {"name": "seed"}, "link": None},
        {"name": "steps", "type": "INT", "widget": {"name": "steps"}, "link": None},
        {"name": "cfg", "type": "FLOAT", "widget": {"name": "cfg"}, "link": None},
        {"name": "sampler_name", "type": "COMBO", "widget": {"name": "sampler_name"}, "link": None},
        {"name": "scheduler", "type": "COMBO", "widget": {"name": "scheduler"}, "link": None},
        {"name": "denoise", "type": "FLOAT", "widget": {"name": "denoise"}, "link": None},
    ], [{"name": "LATENT", "type": "LATENT", "links": []}],
        [42, 28, 4.0, "dpmpp_2m", "normal", 1.0])


def _ckpt(nid, order, model_l=None, vae_ls=None):
    return _n(nid, "CheckpointLoaderSimple", order, [
        {"name": "ckpt_name", "type": "COMBO", "widget": {"name": "ckpt_name"}, "link": None},
    ], [{"name": "MODEL", "type": "MODEL", "links": [model_l] if model_l else []},
        {"name": "CLIP", "type": "CLIP", "links": []},
        {"name": "VAE", "type": "VAE", "links": vae_ls or []}], ["model.safetensors"])


def _empty_latent(nid, order, out_l=None):
    return _n(nid, "EmptyLatentImage", order, [
        {"name": "width", "type": "INT", "widget": {"name": "width"}, "link": None},
        {"name": "height", "type": "INT", "widget": {"name": "height"}, "link": None},
        {"name": "batch_size", "type": "INT", "widget": {"name": "batch_size"}, "link": None},
    ], [{"name": "LATENT", "type": "LATENT", "links": [out_l] if out_l else []}], [1024, 1024, 1])


def _loadimage(nid, order, out_l=None):
    return _n(nid, "LoadImage", order, [
        {"name": "image", "type": "COMBO", "widget": {"name": "image"}, "link": None},
        {"name": "upload", "type": "IMAGEUPLOAD", "widget": {"name": "upload"}, "link": None},
    ], [{"name": "IMAGE", "type": "IMAGE", "links": [out_l] if out_l else []}], ["ref.png", "image"])


def _cn_apply(nid, order, pos_l, neg_l, vae_l, cn_l, img_l, out_ls):
    return _n(nid, "ControlNetApply", order, [
        {"name": "positive", "type": "CONDITIONING", "link": pos_l},
        {"name": "negative", "type": "CONDITIONING", "link": neg_l},
        {"name": "vae", "type": "VAE", "link": vae_l},
        {"name": "control_net", "type": "CONTROL_NET", "link": cn_l},
        {"name": "image", "type": "IMAGE", "link": img_l},
        {"name": "strength", "type": "FLOAT", "widget": {"name": "strength"}, "link": None},
        {"name": "start_percent", "type": "FLOAT", "widget": {"name": "start_percent"}, "link": None},
        {"name": "end_percent", "type": "FLOAT", "widget": {"name": "end_percent"}, "link": None},
    ], [{"name": "CONDITIONING", "type": "CONDITIONING", "links": out_ls}], [1.0, 0.0, 1.0])


def _cn_loader(nid, order, out_l=None):
    return _n(nid, "ControlNetLoader", order, [
        {"name": "control_net_name", "type": "COMBO", "widget": {"name": "control_net_name"}, "link": None},
    ], [{"name": "CONTROL_NET", "type": "CONTROL_NET", "links": [out_l] if out_l else []}],
        ["cn.safetensors"])


def _vae_decode(nid, order, samples_l, vae_l, out_ls):
    return _n(nid, "VAEDecode", order, [
        {"name": "samples", "type": "LATENT", "link": samples_l},
        {"name": "vae", "type": "VAE", "link": vae_l},
    ], [{"name": "IMAGE", "type": "IMAGE", "links": out_ls}], [])


def _save_image(nid, order, img_l):
    return _n(nid, "SaveImage", order, [
        {"name": "images", "type": "IMAGE", "link": img_l},
        {"name": "filename_prefix", "type": "STRING", "widget": {"name": "filename_prefix"}, "link": None},
    ], [], ["ComfyUI"])


class TestTraceTextPassthrough(unittest.TestCase):
    """ConditioningConcat 类变换节点不再打断提示词回溯；多采样器从 base 追 + 回退。"""

    def _two_sampler_ui(self, base_pos_l, base_neg_l):
        nodes = [
            _sampler(1, 5, base_pos_l, base_neg_l, 4, 1),
            _sampler(2, 6, 6, 7, 9, 8),
            _text(5, 1), _text(6, 2),
            _n(8, "ConditioningConcat", 3, [
                {"name": "conditioning_1", "type": "CONDITIONING", "link": 4},
                {"name": "conditioning_2", "type": "CONDITIONING", "link": 5},
            ], [{"name": "CONDITIONING", "type": "CONDITIONING", "links": [6, 7]}], []),
            _empty_latent(7, 4, 9),
            _ckpt(4, 0, model_l=1),
        ]
        links = [
            [1, 4, 0, 1, 0, "MODEL"],        # ckpt → base.model
            [2, 5, 0, 1, 1, "CONDITIONING"],  # text A → base.positive
            [3, 6, 0, 1, 2, "CONDITIONING"],  # text B → base.negative
            [4, 5, 0, 8, 0, "CONDITIONING"],  # text A → concat.conditioning_1
            [5, 6, 0, 8, 1, "CONDITIONING"],  # text B → concat.conditioning_2
            [6, 8, 0, 2, 1, "CONDITIONING"],  # concat → terminal.positive
            [7, 8, 0, 2, 2, "CONDITIONING"],  # concat → terminal.negative
            [9, 7, 0, 2, 3, "LATENT"],        # empty → terminal.latent_image
            [8, 4, 0, 2, 0, "MODEL"],         # ckpt → terminal.model
        ]
        return _mk_ui(nodes, links)

    def test_base_prompts_traced_through_concat(self):
        """base 有提示词：从 base 追到 5/6（旧代码追 terminal 会在 Concat 处断掉）。"""
        ui = self._two_sampler_ui(2, 3)
        raw = sg.generate_schema(ui, None, schema_id="x", title="x")
        self.assertEqual(raw["prompts"]["positive"]["node"], 5)
        self.assertEqual(raw["prompts"]["negative"]["node"], 6)

    def test_terminal_fallback_passes_concat(self):
        """base 无条件链：回退 terminal，ConditioningConcat 透传追到文本节点。"""
        ui = self._two_sampler_ui(None, None)
        raw = sg.generate_schema(ui, None, schema_id="x", title="x")
        self.assertEqual(raw["prompts"]["positive"]["node"], 5)
        self.assertIsNotNone(raw["prompts"]["negative"]["node"])

    def test_setmask_passthrough(self):
        """SetMask（条件掩码）节点同样透传，不再丢失提示词。"""
        nodes = [
            _sampler(1, 4, 2, 3, 4, 1),
            _text(5, 1),
            _n(6, "ConditioningSetMask", 2, [
                {"name": "conditioning", "type": "CONDITIONING", "link": 2},
                {"name": "mask", "type": "MASK", "link": None},
                {"name": "strength", "type": "FLOAT", "widget": {"name": "strength"}, "link": None},
                {"name": "set_cond_area", "type": "COMBO", "widget": {"name": "set_cond_area"}, "link": None},
            ], [{"name": "CONDITIONING", "type": "CONDITIONING", "links": [2, 3, 5]}], [1.0, "default"]),
            _empty_latent(7, 3, 4),
            _ckpt(4, 0, model_l=1),
        ]
        links = [
            [1, 4, 0, 1, 0, "MODEL"],
            [2, 5, 0, 6, 0, "CONDITIONING"],  # text → SetMask.conditioning
            [3, 6, 0, 1, 1, "CONDITIONING"],  # SetMask → sampler.positive
            [5, 6, 0, 1, 2, "CONDITIONING"],  # SetMask → sampler.negative
            [4, 7, 0, 1, 3, "LATENT"],
        ]
        raw = sg.generate_schema(_mk_ui(nodes, links), None, schema_id="x", title="x")
        self.assertEqual(raw["prompts"]["positive"]["node"], 5)
        self.assertEqual(raw["prompts"]["negative"]["node"], 5)


class TestDimsBindsOutputChain(unittest.TestCase):
    """输出分辨率 = 终采样器 latent 链上第一个尺寸节点（放大工作流绑 ImageScale）。"""

    def test_upscale_workflow_binds_imagestale(self):
        nodes = [
            _sampler(1, 5, 2, 3, 4, 1),
            _sampler(5, 6, 8, 9, 7, 5),
            _text(2, 1),
            _empty_latent(3, 2, 4),
            _ckpt(4, 0, model_l=1, vae_ls=[11, 13]),
            _n(6, "VAEEncodeTiled", 3, [
                {"name": "pixels", "type": "IMAGE", "link": 6},
                {"name": "vae", "type": "VAE", "link": 11},
                {"name": "tile_size", "type": "INT", "widget": {"name": "tile_size"}, "link": None},
            ], [{"name": "LATENT", "type": "LATENT", "links": [7]}], [512]),
            _n(10, "ImageScale", 4, [
                {"name": "pixels", "type": "IMAGE", "link": 12},
                {"name": "upscale_method", "type": "COMBO", "widget": {"name": "upscale_method"}, "link": None},
                {"name": "crop", "type": "COMBO", "widget": {"name": "crop"}, "link": None},
                {"name": "width", "type": "INT", "widget": {"name": "width"}, "link": None},
                {"name": "height", "type": "INT", "widget": {"name": "height"}, "link": None},
            ], [{"name": "IMAGE", "type": "IMAGE", "links": [6]}], ["lanczos", "disabled", 1280, 1856]),
            _loadimage(11, 5, 12),
            _vae_decode(13, 7, 15, 13, [16]),
            _save_image(14, 8, 16),
        ]
        next(n for n in nodes if n["id"] == 5)["outputs"][0]["links"] = [15]
        links = [
            [1, 4, 0, 1, 0, "MODEL"],        # ckpt → base.model
            [2, 2, 0, 1, 1, "CONDITIONING"],
            [3, 2, 0, 1, 2, "CONDITIONING"],
            [4, 3, 0, 1, 3, "LATENT"],
            [5, 4, 0, 5, 0, "MODEL"],        # ckpt → terminal.model
            [6, 10, 0, 6, 0, "IMAGE"],       # ImageScale → VAEEncodeTiled.pixels
            [7, 6, 0, 5, 3, "LATENT"],       # encode → terminal.latent_image
            [8, 2, 0, 5, 1, "CONDITIONING"],
            [9, 2, 0, 5, 2, "CONDITIONING"],
            [11, 4, 2, 6, 1, "VAE"],         # ckpt.vae → encode.vae
            [12, 11, 0, 10, 0, "IMAGE"],     # load → ImageScale.pixels
            [13, 4, 2, 13, 1, "VAE"],
            [15, 5, 0, 13, 0, "LATENT"],     # terminal.latent → VAEDecode.samples
            [16, 13, 0, 14, 0, "IMAGE"],     # decode → SaveImage
        ]
        raw = sg.generate_schema(_mk_ui(nodes, links), None, schema_id="x", title="x")
        w = {p["key"]: p for p in raw["params"]}
        self.assertEqual(w["width"]["node"], 10, "尺寸必须绑到 ImageScale（输出分辨率），不是第一段的 EmptyLatentImage")
        self.assertEqual(w["width"]["default"], 1280)
        self.assertEqual(w["height"]["default"], 1856)

    def test_img2img_no_dims_stays_absent(self):
        """img2img（latent 来自 VAEEncode）：没有可调尺寸，不硬造。"""
        nodes = [
            _sampler(1, 5, 2, 3, 4, 1),
            _text(2, 1),
            _ckpt(4, 0, model_l=1, vae_ls=[5]),
            _n(6, "VAEEncode", 3, [
                {"name": "pixels", "type": "IMAGE", "link": 3},
                {"name": "vae", "type": "VAE", "link": 5},
            ], [{"name": "LATENT", "type": "LATENT", "links": [4]}], []),
            _loadimage(7, 2, 3),
        ]
        links = [
            [1, 4, 0, 1, 0, "MODEL"],
            [2, 2, 0, 1, 1, "CONDITIONING"],
            [3, 2, 0, 1, 2, "CONDITIONING"],
            [4, 6, 0, 1, 3, "LATENT"],
            [5, 4, 2, 6, 1, "VAE"],
            [3, 7, 0, 6, 0, "IMAGE"],
        ]
        raw = sg.generate_schema(_mk_ui(nodes, links), None, schema_id="x", title="x")
        keys = {p["key"] for p in raw["params"]}
        self.assertNotIn("width", keys)
        self.assertNotIn("height", keys)


class TestComboAdvancedParam(unittest.TestCase):
    """未知节点的 COMBO 控件：当字符串处理，绝不 float() 崩溃（导入不再失败）。"""

    def test_combo_widget_not_crashing(self):
        nodes = [
            _sampler(1, 5, 2, 3, 4, 5),
            _text(2, 1),
            _empty_latent(3, 2, 4),
            _ckpt(4, 0, model_l=1),
            _n(5, "MysteryBlender", 3, [
                {"name": "model", "type": "MODEL", "link": 1},
                {"name": "blend_mode", "type": "COMBO", "widget": {"name": "blend_mode"}, "link": None},
                {"name": "tiles", "type": "INT", "widget": {"name": "tiles"}, "link": None},
            ], [{"name": "MODEL", "type": "MODEL", "links": [5]}], ["screen", 2]),
        ]
        links = [
            [1, 4, 0, 5, 0, "MODEL"],
            [2, 2, 0, 1, 1, "CONDITIONING"],
            [3, 2, 0, 1, 2, "CONDITIONING"],
            [4, 3, 0, 1, 3, "LATENT"],
            [5, 5, 0, 1, 0, "MODEL"],
        ]
        raw = sg.generate_schema(_mk_ui(nodes, links), None, schema_id="x", title="x")
        p = next(p for p in raw["params"] if p["key"] == "u5_blend_mode")
        self.assertEqual(p["type"], "combo")
        self.assertEqual(p["default"], "screen")
        self.assertEqual(p["options"], ["screen"])
        self.assertIsNone(p["min"])
        dr = sg.dry_run(_mk_ui(nodes, links), raw)
        self.assertTrue(dr["ok"], dr["problems"][:6])


class TestImageSlotsLiveness(unittest.TestCase):
    """死分支的 LoadImage 不做槽位；组内图保留；活跃 CN 浮出强度参数。"""

    def _ui(self):
        nodes = [
            _sampler(1, 8, 2, 9, 4, 5),
            _text(2, 1),
            _empty_latent(3, 2, 4),
            _ckpt(4, 0, model_l=5, vae_ls=[6, 13]),
            _loadimage(5, 3, 7),      # 活：→ 6.image
            _cn_apply(6, 4, 1, 3, 6, 8, 7, [2, 9]),   # 活 CN（已接线）
            _cn_loader(7, 5, 8),
            _loadimage(8, 6, 10),     # 组：→ 9.image（死 CN 成为节点群）
            _cn_apply(9, 7, None, None, None, 12, 10, []),   # 死 CN（可插入 → 成组）
            _cn_loader(10, 8, 12),
            _loadimage(11, 9, 14),    # 死：→ 12.image（不可插入的死 CN）
            _cn_apply(12, 10, None, None, None, None, 14, []),  # 死 CN（control_net 未接）
            _vae_decode(13, 11, 15, 13, [16]),
            _save_image(14, 12, 16),
        ]
        links = [
            [1, 2, 0, 6, 0, "CONDITIONING"],  # text → 活CN.positive
            [2, 6, 0, 1, 1, "CONDITIONING"],  # 活CN → sampler.positive
            [3, 2, 0, 6, 1, "CONDITIONING"],
            [4, 3, 0, 1, 3, "LATENT"],
            [5, 4, 0, 1, 0, "MODEL"],
            [6, 4, 2, 6, 2, "VAE"],
            [7, 5, 0, 6, 4, "IMAGE"],
            [8, 7, 0, 6, 3, "CONTROL_NET"],
            [9, 6, 1, 1, 2, "CONDITIONING"],
            [10, 8, 0, 9, 4, "IMAGE"],
            [12, 10, 0, 9, 3, "CONTROL_NET"],
            [13, 4, 2, 13, 1, "VAE"],
            [14, 11, 0, 12, 4, "IMAGE"],
            [15, 1, 0, 13, 0, "LATENT"],
            [16, 13, 0, 14, 0, "IMAGE"],
        ]
        return _mk_ui(nodes, links)

    def test_dead_branch_loadimage_not_a_slot(self):
        raw = sg.generate_schema(self._ui(), None, schema_id="x", title="x")
        keys = {sl["key"] for sl in raw["image_slots"]}
        self.assertIn("compose_ref", keys)      # 活 CN 的参考图
        self.assertIn("controlnet_ref", keys)   # 组内死 CN 的参考图
        self.assertNotIn("ref_11", keys, "死分支 LoadImage 不能做成槽位（上传被静默丢弃）")

    def test_group_excludes_shared_nodes(self):
        """死分支与活跃分支共用加载器/预处理时，共享节点不能收进组。"""
        ui = self._ui()
        # 8 → 预处理 → {活 CN, 死 CN}：预处理与 LoadImage 都成共享节点（重建链接）
        ui["nodes"].append(_n(15, "CannyPreprocessor", 13, [
            {"name": "image", "type": "IMAGE", "link": 11},
            {"name": "resolution", "type": "INT", "widget": {"name": "resolution"}, "link": None},
        ], [{"name": "IMAGE", "type": "IMAGE", "links": [7, 10]}], [512]))
        ui["last_node_id"] = 15
        next(n for n in ui["nodes"] if n["id"] == 8)["outputs"][0]["links"] = [11]
        ui["links"] = [
            [1, 2, 0, 6, 0, "CONDITIONING"], [2, 6, 0, 1, 1, "CONDITIONING"],
            [3, 2, 0, 6, 1, "CONDITIONING"], [4, 3, 0, 1, 3, "LATENT"],
            [5, 4, 0, 1, 0, "MODEL"],       [6, 4, 2, 6, 2, "VAE"],
            [7, 15, 0, 6, 4, "IMAGE"],       [8, 7, 0, 6, 3, "CONTROL_NET"],
            [9, 6, 1, 1, 2, "CONDITIONING"],
            [10, 15, 0, 9, 4, "IMAGE"],      # 预处理 → 死 CN.image
            [11, 8, 0, 15, 0, "IMAGE"],      # LoadImage 8 → 预处理.image
            [12, 10, 0, 9, 3, "CONTROL_NET"],
            [13, 4, 2, 13, 1, "VAE"],        [14, 11, 0, 12, 4, "IMAGE"],
            [15, 1, 0, 13, 0, "LATENT"],     [16, 13, 0, 14, 0, "IMAGE"],
        ]
        raw = sg.generate_schema(ui, None, schema_id="x", title="x")
        grp = next(g for g in raw["node_groups"] if g["mode"] == "patch")
        self.assertNotIn(15, grp["nodes"], "共享预处理不能随组剔除（活跃 CN 会悬空）")
        self.assertNotIn(8, grp["nodes"], "只喂共享预处理的 LoadImage 也不能剔除")
        self.assertIn(9, grp["nodes"])
        self.assertIn(10, grp["nodes"])
        dr = sg.dry_run(ui, raw)
        self.assertTrue(dr["ok"], dr["problems"][:6])

    def test_live_cn_strength_params_surfaced(self):
        raw = sg.generate_schema(self._ui(), None, schema_id="x", title="x")
        keys = {p["key"] for p in raw["params"]}
        self.assertIn("cn6_strength", keys)
        self.assertIn("cn6_start_percent", keys)
        self.assertIn("cn6_end_percent", keys)


class TestPreviewAndSaveRoles(unittest.TestCase):
    """预览/保存节点输入名可能是 image/samples 而不是 images：不再误删 VAEDecode。"""

    def test_fastpreview_role_is_preview(self):
        g = sg._GraphIndex({"last_node_id": 1, "last_link_id": 1, "nodes": [
            _n(1, "FastPreview", 0, [
                {"name": "image", "type": "IMAGE", "link": 1},
            ], [{"name": "IMAGE", "type": "IMAGE", "links": []}], []),
        ], "links": []})
        self.assertEqual(g.role(1), "preview")

    def test_savelatent_role_is_save(self):
        g = sg._GraphIndex({"last_node_id": 1, "last_link_id": 1, "nodes": [
            _n(1, "SaveLatent", 0, [
                {"name": "samples", "type": "LATENT", "link": 1},
                {"name": "filename_prefix", "type": "STRING", "widget": {"name": "filename_prefix"}, "link": None},
            ], [], ["latent"]),
        ], "links": []})
        self.assertEqual(g.role(1), "save")

    def test_vae_decode_still_vae_decode(self):
        g = sg._GraphIndex({"last_node_id": 1, "last_link_id": 1, "nodes": [
            _n(1, "VAEDecode", 0, [
                {"name": "samples", "type": "LATENT", "link": 1},
                {"name": "vae", "type": "VAE", "link": None},
            ], [{"name": "IMAGE", "type": "IMAGE", "links": []}], []),
        ], "links": []})
        self.assertEqual(g.role(1), "vae_decode")


@unittest.skipUnless(SRC.exists(), "来源工作流目录不存在")
class TestSlotsNeverDead(unittest.TestCase):
    """全部来源工作流：image_slots 的节点绝不能落进 drop（上传被静默丢弃）。"""

    def test_all_source_workflows(self):
        for f in sorted(SRC.glob("*.json")):
            ui = _load_ui(f.name)
            if not sg.count_samplers(ui):
                continue
            raw = sg.generate_schema(ui, None, schema_id=f.stem, title=f.stem)
            dropped = set(raw["drop"])
            for sl in raw["image_slots"]:
                self.assertNotIn(sl["node"], dropped,
                                 f"{f.name} 槽位 {sl['key']} 指向会被 drop 的节点 {sl['node']}")


class TestMiscNodes(unittest.TestCase):
    """其它节点：未识别/已识别角色的隐藏控件都暴露成 m 参数，组/死节点不暴露。

    链路：未知节点（STRING/BOOLEAN 控件）+ LoraLoaderBlockWeight（lora 角色的隐藏
    分层权重控件）→ misc_nodes + m 参数 → 校验通过 → 转换时值写回 API。
    """

    def _ui(self):
        # 拓扑：底模(5) → 分层权重 LoRA(7) → 未知节点(6) → 采样器(1)；文本 2/3、空潜 4
        nodes = [
            _sampler(1, 8, 1, 2, 3, 4),
            _text(2, 1), _text(3, 2),
            _empty_latent(4, 3, 3),
            _ckpt(5, 0, model_l=6),
            _n(6, "MysteryBox", 4, [
                {"name": "model", "type": "MODEL", "link": 5},
                {"name": "prompt_text", "type": "STRING", "widget": {"name": "prompt_text"}, "link": None},
                {"name": "enable", "type": "BOOLEAN", "widget": {"name": "enable"}, "link": None},
                {"name": "tiles", "type": "INT", "widget": {"name": "tiles"}, "link": None},
            ], [{"name": "MODEL", "type": "MODEL", "links": [4]}], ["draft", "true", 2]),
            _n(7, "LoraLoaderBlockWeight", 5, [
                {"name": "model", "type": "MODEL", "link": 6},
                {"name": "clip", "type": "CLIP", "link": None},
                {"name": "lora_name", "type": "COMBO", "widget": {"name": "lora_name"}, "link": None},
                {"name": "strength_model", "type": "FLOAT", "widget": {"name": "strength_model"}, "link": None},
                {"name": "strength_clip", "type": "FLOAT", "widget": {"name": "strength_clip"}, "link": None},
                {"name": "block_weights", "type": "STRING", "widget": {"name": "block_weights"}, "link": None},
                {"name": "base_multiplier", "type": "FLOAT", "widget": {"name": "base_multiplier"}, "link": None},
                {"name": "block_multiplier", "type": "FLOAT", "widget": {"name": "block_multiplier"}, "link": None},
            ], [{"name": "MODEL", "type": "MODEL", "links": [5]},
                {"name": "CLIP", "type": "CLIP", "links": []}],
                ["lora.safetensors", 0.7, 1.0, "IN:0.2,OUT:0.8", 1.0, 1.25]),
        ]
        links = [
            [1, 2, 0, 1, 1, "CONDITIONING"],
            [2, 3, 0, 1, 2, "CONDITIONING"],
            [3, 4, 0, 1, 3, "LATENT"],
            [4, 6, 0, 1, 0, "MODEL"],
            [5, 7, 0, 6, 0, "MODEL"],
            [6, 5, 0, 7, 0, "MODEL"],
        ]
        return _mk_ui(nodes, links)

    def test_unknown_node_string_bool_surfaced_as_misc(self):
        raw = sg.generate_schema(self._ui(), None, schema_id="x", title="x")
        keys = {p["key"] for p in raw["params"]}
        self.assertIn("u6_tiles", keys)        # INT 控件 → 高级参数
        self.assertIn("m6_prompt_text", keys)  # STRING → 其它节点
        self.assertIn("m6_enable", keys)       # BOOLEAN → 其它节点
        # 不重复暴露：tiles 只有 u6_tiles 一份
        self.assertNotIn("m6_tiles", keys)
        s = next(p for p in raw["params"] if p["key"] == "m6_prompt_text")
        self.assertEqual(s["type"], "string")
        self.assertEqual(s["default"], "draft")
        b = next(p for p in raw["params"] if p["key"] == "m6_enable")
        self.assertEqual(b["type"], "combo")
        self.assertEqual(b["options"], ["true", "false"])
        self.assertEqual(b["default"], "true")
        self.assertIsNone(b["min"])

    def test_role_node_hidden_widgets_surfaced(self):
        """LoraLoaderBlockWeight 是 lora 角色，分层权重控件不能藏起来。"""
        raw = sg.generate_schema(self._ui(), None, schema_id="x", title="x")
        keys = {p["key"] for p in raw["params"]}
        self.assertIn("m7_block_weights", keys)
        self.assertIn("m7_base_multiplier", keys)
        self.assertNotIn("m7_lora_name", keys)   # 标准 lora 控件不重复暴露
        bw = next(p for p in raw["params"] if p["key"] == "m7_block_weights")
        self.assertEqual(bw["type"], "string")
        self.assertEqual(bw["label"], "分块权重（IN/OUT/MID）")  # _rules.json 预注册
        bm = next(p for p in raw["params"] if p["key"] == "m7_base_multiplier")
        self.assertEqual((bm["min"], bm["max"], bm["step"]), (0, 2, 0.05))
        self.assertEqual(bm["default"], 1.0)

    def test_misc_nodes_grouping(self):
        raw = sg.generate_schema(self._ui(), None, schema_id="x", title="x")
        groups = {m["node"]: m for m in raw["misc_nodes"]}
        self.assertEqual(set(groups), {6, 7})
        self.assertEqual(groups[6]["title"], "MysteryBox")
        self.assertEqual(set(groups[6]["keys"]), {"m6_prompt_text", "m6_enable"})
        self.assertEqual(groups[7]["title"], "LoraLoaderBlockWeight")
        self.assertEqual(set(groups[7]["keys"]),
                         {"m7_block_weights", "m7_base_multiplier", "m7_block_multiplier"})
        # misc keys 必须都指向真实参数
        keys = {p["key"] for p in raw["params"]}
        for m in raw["misc_nodes"]:
            for k in m["keys"]:
                self.assertIn(k, keys)

    def test_full_pipeline_roundtrip(self):
        """generate → parse → 校验 → 转换，m 参数值写进 API 节点输入。"""
        ui = self._ui()
        raw = sg.generate_schema(ui, None, schema_id="x", title="x")
        self.assertEqual(sg.validate_generated(raw, ui), [])
        schema = parse_schema(raw)
        from core.workflow_converter import ConvertOptions, convert_ui_to_api
        # 带 1 个 LoRA：node7（链头）才保留在有效图里，m 参数才有落点
        opts = ConvertOptions(params={
            "loras": [{"name": "lora.safetensors", "strength_model": 0.7, "strength_clip": 1.0}],
            "m6_prompt_text": "改过", "m6_enable": "false",
            "m7_base_multiplier": 1.5})
        api, _diag = convert_ui_to_api(ui, schema, opts)
        self.assertEqual(api["6"]["inputs"]["prompt_text"], "改过")
        self.assertEqual(api["6"]["inputs"]["enable"], "false")
        self.assertEqual(api["7"]["inputs"]["base_multiplier"], 1.5)
        # 未传的 m 参数保持工作流默认
        self.assertEqual(api["7"]["inputs"]["block_weights"], "IN:0.2,OUT:0.8")

    def test_dead_and_group_nodes_not_exposed(self):
        ui = self._ui()
        ui["nodes"].append(_loadimage(8, 9))   # 死 LoadImage：不喂给任何人
        ui["last_node_id"] = 8
        raw = sg.generate_schema(ui, None, schema_id="x", title="x")
        self.assertNotIn(8, {p["node"] for p in raw["params"]}, "死节点不暴露参数")
        self.assertNotIn(8, {m["node"] for m in raw["misc_nodes"]})
        # 组成员不暴露（组关掉时节点被剔除，编辑会被静默丢弃）
        g = sg._GraphIndex(ui)
        out, _entries = sg._derive_misc_nodes(
            g, g.samplers(), params=[], groups=[], group_members={6},
            save_node=None, object_info=None)
        keys = {p["key"] for p in out}
        self.assertNotIn("m6_prompt_text", keys)

    def test_same_name_widget_two_nodes_both_exposed(self):
        """节点作用域去重：两个同名节点各有同名控件，各自都暴露。

        回归：claimed 曾用全局 set，节点 A 的标准/组参数声明同名控件（如 enable）后，
        节点 B 的同名控件会被误吞成"已声明"。修复为按节点（claimed_by_node）后，
        B 的控件仍暴露为 m{nid}_{name}。"""
        nodes = [
            _sampler(1, 7, 1, 2, 3, 4),
            _text(2, 1), _text(3, 2), _empty_latent(4, 3, 3),
            _ckpt(5, 0, model_l=6),
            _n(6, "MysteryBox", 4, [
                {"name": "model", "type": "MODEL", "link": 5},
                {"name": "enable", "type": "BOOLEAN", "widget": {"name": "enable"}, "link": None},
                {"name": "prompt_text", "type": "STRING", "widget": {"name": "prompt_text"}, "link": None},
            ], [{"name": "MODEL", "type": "MODEL", "links": [4]}], ["true", "draft"]),
            _n(7, "MysteryBox", 5, [
                {"name": "model", "type": "MODEL", "link": 6},
                {"name": "enable", "type": "BOOLEAN", "widget": {"name": "enable"}, "link": None},
                {"name": "prompt_text", "type": "STRING", "widget": {"name": "prompt_text"}, "link": None},
            ], [{"name": "MODEL", "type": "MODEL", "links": [5]}], ["false", "second"]),
        ]
        links = [
            [1, 2, 0, 1, 1, "CONDITIONING"],
            [2, 3, 0, 1, 2, "CONDITIONING"],
            [3, 4, 0, 1, 3, "LATENT"],
            [4, 6, 0, 1, 0, "MODEL"],
            [5, 7, 0, 6, 0, "MODEL"],
            [6, 5, 0, 7, 0, "MODEL"],
        ]
        ui = _mk_ui(nodes, links)
        g = sg._GraphIndex(ui)
        out, _entries = sg._derive_misc_nodes(
            g, g.samplers(), params=[{"key": "enable", "node": 6, "widget": "enable"}],
            groups=[], group_members=set(), save_node=None, object_info=None)
        keys = {p["key"] for p in out}
        self.assertNotIn("m6_enable", keys, "node6 的 enable 已被参数声明，不再重复暴露")
        self.assertIn("m7_enable", keys, "node7 的同名 enable 不能因 node6 的声明被吞")
        self.assertEqual(out[next(i for i, p in enumerate(out)
                                   if p["key"] == "m7_enable")]["default"], "false")

    def test_object_info_meta_feeds_misc_numeric(self):
        ui = self._ui()
        oi = {"LoraLoaderBlockWeight": {"input": {"required": {
            "block_multiplier": ["FLOAT", {"min": 0, "max": 3, "step": 0.01, "default": 1.25}],
        }}}}
        raw = sg.generate_schema(ui, oi, schema_id="x", title="x")
        p = next(p for p in raw["params"] if p["key"] == "m7_block_multiplier")
        self.assertEqual((p["min"], p["max"], p["step"]), (0, 3, 0.01))
        # 节点不在 object_info：回退到当前值猜区间，绝不崩溃
        raw2 = sg.generate_schema(ui, None, schema_id="x", title="x")
        q = next(p for p in raw2["params"] if p["key"] == "m7_block_multiplier")
        self.assertEqual(q["default"], 1.25)
        self.assertIsNotNone(q["min"])
        self.assertIsNotNone(q["max"])

    @unittest.skipUnless(SRC.exists(), "来源工作流目录不存在")
    def test_standard_workflow_no_misc_overflow(self):
        """amiya 简单工作流：采样器/底模/LoRA 的标准控件不刷进其它节点区。"""
        ui = _load_ui("amiya.json")
        raw = sg.generate_schema(ui, None, schema_id="amiya", title="amiya")
        misc_keys = {k for m in raw["misc_nodes"] for k in m["keys"]}
        for k in misc_keys:
            p = next(p for p in raw["params"] if p["key"] == k)
            self.assertNotEqual(p["widget"], "strength_model")  # 标准控件不重复暴露


class TestPracticalCaps(TestMiscNodes):
    """object_info 数值元数据是技术硬界（steps max=10000、尺寸 max=16384），
    不能直接进手机面板——按 widget 名收窄到实用范围。"""

    def _oi(self):
        return {
            "KSampler": {"input": {"required": {
                "steps": ["INT", {"default": 20, "min": 1, "max": 10000, "step": 1}],
                "cfg": ["FLOAT", {"default": 7, "min": 0, "max": 100, "step": 0.5}],
                "denoise": ["FLOAT", {"default": 1.0, "min": 0, "max": 1, "step": 0.05}],
            }}},
            "EmptyLatentImage": {"input": {"required": {
                "width": ["INT", {"default": 1024, "min": 16, "max": 16384, "step": 8}],
                "height": ["INT", {"default": 1024, "min": 16, "max": 16384, "step": 8}],
            }}},
        }

    def test_standard_ranges_capped(self):
        """steps/cfg/尺寸 用 object_info 硬界 → 收窄到实用范围。"""
        raw = sg.generate_schema(self._ui(), self._oi(), schema_id="x", title="x")
        pm = {p["key"]: p for p in raw["params"]}
        self.assertEqual((pm["steps"]["min"], pm["steps"]["max"]), (1, 150))
        self.assertEqual((pm["cfg"]["min"], pm["cfg"]["max"]), (0, 20))
        self.assertEqual((pm["width"]["min"], pm["width"]["max"]), (256, 2048))
        self.assertEqual((pm["height"]["min"], pm["height"]["max"]), (256, 2048))

    def test_clamped_default_out_of_range_broadens_bound(self):
        """工作流真值 steps=200 时边界放宽到 200，保滑块不越界、生成真值不变。"""
        from core.schema_gen import _clamped
        m = _clamped({"min": 1, "max": 10000, "step": 1}, "steps", default=200)
        self.assertEqual(m["max"], 200)
        m = _clamped({"min": 0, "max": 100}, "cfg", default=12)
        self.assertEqual((m["min"], m["max"]), (0, 20))

    def test_clamped_untracked_widget_passthrough(self):
        """未在封顶表里的 widget（如 block_multiplier）保持 object_info 原值。"""
        from core.schema_gen import _clamped
        m = _clamped({"min": 0, "max": 3, "step": 0.01}, "block_multiplier")
        self.assertEqual((m["min"], m["max"], m["step"]), (0, 3, 0.01))

    def test_misc_numeric_capped(self):
        """其它节点区的数值控件同样封顶：保存链上 VAEDecodeTiled 的 tile_size
        object_info max=8192 → 收窄到 2048。"""
        ui = self._ui()
        # 保存链：采样器(1) → VAEDecodeTiled(8) → SaveImage(9)。VAEDecodeTiled
        # 是 vae_decode 角色、非采样器祖先，其 tile 控件只能落进其它节点区。
        ui["nodes"].append(_n(8, "VAEDecodeTiled", 6, [
            {"name": "samples", "type": "LATENT", "link": 7},
            {"name": "vae", "type": "VAE", "link": None},
            {"name": "tile_size", "type": "INT", "widget": {"name": "tile_size"}, "link": None},
            {"name": "overlap", "type": "INT", "widget": {"name": "overlap"}, "link": None},
            {"name": "temporal_size", "type": "INT", "widget": {"name": "temporal_size"}, "link": None},
            {"name": "temporal_overlap", "type": "INT", "widget": {"name": "temporal_overlap"}, "link": None},
        ], [{"name": "IMAGE", "type": "IMAGE", "links": [8]}], [512, 64, 64, 8]))
        ui["nodes"].append(_n(9, "SaveImage", 7, [
            {"name": "images", "type": "IMAGE", "link": 8},
            {"name": "filename_prefix", "type": "STRING", "widget": {"name": "filename_prefix"}, "link": None},
        ], [], ["comfy"]))
        ui["links"].extend([
            [7, 1, 0, 8, 0, "LATENT"],
            [8, 8, 0, 9, 0, "IMAGE"],
        ])
        oi = {"VAEDecodeTiled": {"input": {"required": {
            "tile_size": ["INT", {"default": 512, "min": 32, "max": 8192, "step": 64}],
        }}}}
        raw = sg.generate_schema(ui, oi, schema_id="x", title="x")
        p = next(p for p in raw["params"] if p["widget"] == "tile_size")
        self.assertEqual((p["min"], p["max"]), (64, 2048))
        self.assertEqual(p["type"], "int")


class TestBoxedGroups(unittest.TestCase):
    """群组框识别引擎：ComfyUI 的 group 几何框 → 组件开关。

    回归基准 = tests/fixtures/ 里的冻结快照（从 ComfyUI 源目录拷出后固化，不随用户
    重新导入/编辑变化）。templates/ 是产品导入输出目录、每次导入会覆盖，绝不能当
    测试基准；amiya_boxed 是带框 amiya 源副本（templates/amiya.json 是手写 schema
    用的无框保护副本）。
    """

    LIVE_BOX = ("1st_boxed.json", "upscale_boxed.json")   # 框内 CN 在条件链上 → 摘除恢复（默认开）
    DEAD_BOX = ("4th_boxed.json",)                        # 框内死 CN → 补入条件链（默认关）+ ipa 框
    FIX = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    AMIYA_FIX = FIX / "amiya_boxed.json"

    def _ui(self, name):
        return json.loads((self.FIX / name).read_text(encoding="utf-8"))

    def _boxed(self, name, sid=None):
        return sg.generate_schema(self._ui(name), None,
                                  schema_id=sid or name.split(".")[0], title=name.split(".")[0])

    def test_box_members_geometry(self):
        """几何映射：节点中心点在框内 → 成员；空框 → []。"""
        for name in self.LIVE_BOX:
            self.assertEqual(sg._box_members(self._ui(name)),
                             [("Group", [11, 14, 17, 20, 29])], name)
        # 4th 两组：openpose + IPAdapter（用户起名后框标题随框名）
        self.assertEqual(sg._box_members(self._ui("4th_boxed.json")),
                         [("openpose", [11, 14, 17, 20, 29]),
                          ("IPAdapter", [43, 44, 45, 46])])
        boxes = sg._box_members(self._ui("ipa_boxed.json"))
        self.assertEqual(boxes[0], ("Group", [11, 14, 17, 20, 29]))
        self.assertEqual(boxes[1], ("Group", [30, 31, 35]))
        self.assertEqual(boxes[2], ("Group2", []))

    def test_live_cn_box_becomes_chain_patch(self):
        """活链 CN 框：默认开，禁用时摘除组节点并恢复直连（patches_disable）。"""
        expected_restores = {
            "1st_boxed.json":     ({"add": [[6, 0], [3, 1]], "add_type": "CONDITIONING"},
                                   {"add": [[7, 0], [3, 2]], "add_type": "CONDITIONING"}),
            "upscale_boxed.json": ({"add": [[35, 0], [3, 1]], "add_type": "CONDITIONING"},
                                   {"add": [[35, 1], [3, 2]], "add_type": "CONDITIONING"}),
        }
        for name in self.LIVE_BOX:
            raw = self._boxed(name)
            grp = next(g for g in raw["node_groups"] if g["key"] == "openpose")
            self.assertEqual(grp["mode"], "patch", name)
            self.assertTrue(grp["default_enabled"], name)
            self.assertEqual(grp["nodes"], [11, 14, 17, 20, 29], name)
            self.assertEqual(grp["patches_enable"], [], name)
            restores = grp["patches_disable"]
            self.assertEqual(len(restores), 2, name)
            # 摘除后恢复直连：terminal 条件链原来源 → terminal.positive/negative
            for op in expected_restores[name]:
                self.assertIn(op, restores, name)
            self.assertEqual({e["key"] for e in grp["extra_params"]},
                             {"openpose_strength", "openpose_start", "openpose_end"}, name)
            # 框内活链 CN 的强度不进普通参数区（避免与组 extra_params 重复）
            self.assertNotIn("u17_strength", {p["key"] for p in raw["params"]}, name)

    def test_dead_cn_box_becomes_insert_patch(self):
        """死链 CN 框：默认关，启用时补入条件链（patches_enable + VAE 补线）。"""
        for name in self.DEAD_BOX:
            raw = self._boxed(name)
            grp = next(g for g in raw["node_groups"] if g["key"] == "openpose")
            self.assertEqual(grp["mode"], "patch", name)
            self.assertFalse(grp["default_enabled"], name)
            self.assertEqual(grp["nodes"], [11, 14, 17, 20, 29], name)
            ops = grp["patches_enable"]
            self.assertEqual(len(ops), 7, name)
            self.assertIn({"add": [[4, 2], [17, 3]], "add_type": "VAE"}, ops,
                          "死 CN 的 vae 是 required 且无默认，必须补线")
            # 插链 source 用当前条件链来源（新版 4th 已把 35→3 改为 6→3）
            self.assertIn({"remove": [[6, 0], [3, 1]], "remove_type": "CONDITIONING"}, ops, name)
            self.assertIn({"add": [[17, 0], [3, 1]], "add_type": "CONDITIONING"}, ops, name)
            # 同源 ipa 框也识别成组
            self.assertEqual([g["key"] for g in raw["node_groups"]], ["openpose", "ipa"], name)

    def test_two_dead_cn_same_chain_skip_second(self):
        """同条件链第二个死 CN 框：跳过并记 note；空框也记 note。"""
        raw = self._boxed("ipa_boxed.json")
        notes = raw["group_notes"]
        self.assertTrue(any("OpenPose 姿态控制" in n for n in notes), notes)
        self.assertTrue(any("已有死 ControlNet 开关" in n for n in notes), notes)
        self.assertTrue(any("为空，跳过" in n for n in notes), notes)
        self.assertEqual([g["key"] for g in raw["node_groups"]], ["openpose", "ipa"])

    def test_boxed_amiya_matches_handwritten(self):
        """带框 amiya 源副本：openpose + ipa 两组，组结构/成员与手写 schema 一致。

        注意：源副本是用户当前的工作流（条件链已是 6→3，手写 schema 冻结时是 35→3），
        补线 ops 反映当前链、但组骨架（key/成员/mode/默认态）必须与手写权威一致。
        """
        ui = json.loads(self.AMIYA_FIX.read_text(encoding="utf-8"))
        raw = sg.generate_schema(ui, None, schema_id="amiya_boxed", title="amiya_boxed")
        grps = {g["key"]: g for g in raw["node_groups"]}
        self.assertEqual(grps["openpose"]["nodes"], [11, 14, 17, 20, 29])
        self.assertFalse(grps["openpose"]["default_enabled"])
        self.assertEqual(grps["ipa"]["nodes"], [43, 44, 45, 46])
        self.assertTrue(grps["ipa"]["default_enabled"])
        self.assertEqual(grps["ipa"]["mode"], "bypass")
        self.assertEqual(grps["ipa"]["bypass_node"], 43)
        # 与手写 amiya schema（权威）组骨架一致：key/成员/mode/默认态
        canon = json.loads((Path(__file__).resolve().parents[1] / "schemas" / "amiya.json")
                           .read_text(encoding="utf-8"))
        canon_grps = {g["key"]: g for g in canon["node_groups"]}
        self.assertEqual(set(grps), set(canon_grps))
        for key in canon_grps:
            self.assertEqual(grps[key]["mode"], canon_grps[key]["mode"], key)
            self.assertEqual(grps[key]["default_enabled"], canon_grps[key]["default_enabled"], key)
            self.assertEqual(grps[key]["nodes"], canon_grps[key]["nodes"], key)
        # 死 CN 插入模式复现：7 条 ops、VAE 补线、remove 指向当前条件链来源（6→3）
        ops = grps["openpose"]["patches_enable"]
        self.assertEqual(len(ops), 7)
        self.assertIn({"add": [[4, 2], [17, 3]], "add_type": "VAE"}, ops)
        self.assertIn({"remove": [[6, 0], [3, 1]], "remove_type": "CONDITIONING"}, ops)
        self.assertIn({"remove": [[7, 0], [3, 2]], "remove_type": "CONDITIONING"}, ops)
        self.assertIn({"add": [[17, 0], [3, 1]], "add_type": "CONDITIONING"}, ops)

    def test_two_state_conversion_terminal_chain(self):
        """开关两态转换：terminal 条件链按态指向预期来源、不悬空。"""
        from core.schema import parse_schema
        from core.workflow_converter import ConvertOptions, convert_ui_to_api

        expected = {
            # off → (pos, neg) ; on → (pos, neg)；api 节点 id 是字符串
            "1st_boxed.json":     ((["6", 0], ["7", 0]), (["17", 0], ["17", 1])),
            "upscale_boxed.json": ((["35", 0], ["35", 1]), (["17", 0], ["17", 1])),
            "4th_boxed.json":     ((["6", 0], ["7", 0]), (["17", 0], ["17", 1])),
            "ipa_boxed.json":     ((["6", 0], ["7", 0]), (["17", 0], ["17", 1])),
        }
        for name, (exp_off, exp_on) in expected.items():
            ui = self._ui(name)
            raw = self._boxed(name)
            schema = parse_schema(raw)
            g = sg._GraphIndex(ui)
            terminal = g.terminal_sampler(g.samplers())
            keys = [gr["key"] for gr in raw["node_groups"]]

            for state, exp in (("off", exp_off), ("on", exp_on)):
                api, _diag = convert_ui_to_api(ui, schema, ConvertOptions(
                    params={}, enabled_groups={k: (state == "on") for k in keys}, image_slots={}))
                nid = str(terminal)
                pos = api[nid]["inputs"]["positive"]
                neg = api[nid]["inputs"]["negative"]
                self.assertEqual(pos, exp[0], f"{name} {state}")
                self.assertEqual(neg, exp[1], f"{name} {state}")
                # 不悬空：来源节点必须存在于转换后的有效图
                self.assertIn(str(pos[0]), api, (name, state))
                self.assertIn(str(neg[0]), api, (name, state))

    def test_all_boxed_dryrun_green(self):
        """带框模板全量 dry-run：每组两态 + 全默认/全开/全关都通过。"""
        for name in self.LIVE_BOX + self.DEAD_BOX + ("ipa_boxed.json",):
            raw = self._boxed(name)
            dr = sg.dry_run(self._ui(name), raw)
            self.assertTrue(dr["ok"], f"{name}: {dr['problems'][:6]}")

    def test_group_notes_present(self):
        """框识别诊断透传：raw.group_notes 记录每个框的成组/跳过结果。"""
        raw = self._boxed("ipa_boxed.json")
        notes = raw.get("group_notes")
        self.assertIsInstance(notes, list)
        self.assertGreaterEqual(len(notes), 3)
        # 每个框都有一条诊断（成组或跳过），导入接口原样返回 raw.get("group_notes")
        self.assertEqual(raw["group_notes"], notes)


class TestStressWorkflows(unittest.TestCase):
    """压力测试工作流（stress_test1/2）：导入管线必须干净通过。

    用户加了两个复杂工作流测导入器：stress_test1 = 62 节点、detailer 链整体在源里
    bypass(mode=4) 且被框成 9 组；stress_test2 = 134 节点、4 个采样器全是
    KSampler (Efficient)。冻结快照在 tests/fixtures/（同 1st_boxed 惯例）。
    回归点：
    - Gap B：efficiency 采样器识别（st2 全是 KSampler (Efficient)，漏识别 → NoSamplerError）；
    - Gap A：link-driven 参数（rgthree Seed/Steps/CFG 等连线接管）→ 跳过 + 记 note；
    - 框组安全：经 bypass 节点的链段 / 含采样器的框 → 跳过并说明，绝不生成指向幽灵
      节点的开关；dry-run 全态（含全关）保存链不悬空。
    """

    FIX = Path(__file__).resolve().parent / "fixtures"

    def _workflow(self, name):
        return json.loads((self.FIX / name).read_text(encoding="utf-8"))

    def _gen(self, ui, name):
        """generate_schema 原地变异 ui（_ensure_widget_entries 追加效率节点 widget 条目），
        必须与 validate/dry_run 复用同一个 ui 对象，否则校验读新鲜文件会漏判。"""
        return sg.generate_schema(ui, None,
                                  schema_id=name.split(".")[0], title=name.split(".")[0])

    def test_import_pipeline_green(self):
        """generate_schema → validate_generated → dry_run 全绿（不 422）。"""
        for name in ("stress_test1.json", "stress_test2.json"):
            ui = self._workflow(name)
            raw = self._gen(ui, name)
            self.assertEqual(sg.validate_generated(raw, ui), [], name)
            dr = sg.dry_run(ui, raw)
            self.assertTrue(dr["ok"], f"{name}: {dr['problems'][:6]}")

    def test_efficiency_samplers_recognized(self):
        """st2 的 4 个采样器全是 KSampler (Efficient)：Gap B 必须识别，否则 NoSamplerError。"""
        ui = self._workflow("stress_test2.json")
        self.assertEqual(sg.count_samplers(ui), 4)
        raw = self._gen(ui, "stress_test2.json")
        # 效率采样器被识别后，其采样参数才落进面板
        keys = [p["key"] for p in raw["params"]]
        self.assertIn("steps", keys)
        self.assertIn("cfg", keys)

    def test_link_driven_params_skipped_with_note(self):
        """Gap A：被连线接管的参数跳过 + 记 note；不落成假面板参数。"""
        for name in ("stress_test1.json", "stress_test2.json"):
            ui = self._workflow(name)
            raw = self._gen(ui, name)
            notes = raw.get("group_notes", [])
            self.assertTrue(any("已被连线接管" in n for n in notes), name)
        # st1 的 seed/steps/cfg/宽/高全部被 rgthree 原语接管 → 都不进面板
        ui = self._workflow("stress_test1.json")
        keys = [p["key"] for p in self._gen(ui, "stress_test1.json")["params"]]
        for k in ("seed", "steps", "cfg", "width", "height"):
            self.assertNotIn(k, keys, k)

    def test_bypassed_boxes_become_module_switches(self):
        """框组安全升级：源工作流中整框 bypass 的模块框 → 模块开关（默认关 = 保持
        作者设定的透传状态，开 = 激活整框），不再跳过；无法成组的框仍跳过并说明。
        """
        for name in ("stress_test1.json", "stress_test2.json"):
            ui = self._workflow(name)
            raw = self._gen(ui, name)
            groups = raw["node_groups"]
            self.assertTrue(groups, name)
            self.assertTrue(all(g["mode"] == "module" and g["default_enabled"] is False
                                for g in groups), name)
            notes = raw.get("group_notes", [])
            self.assertTrue(any("跳过" in n for n in notes), name)

    def test_save_chain_never_dangles(self):
        """st1 保存链（SaveImage/VAEDecode）全态不悬空：dry_run 干净。"""
        ui = self._workflow("stress_test1.json")
        dr = sg.dry_run(ui, self._gen(ui, "stress_test1.json"))
        self.assertEqual(dr["problems"], [])

    def test_no_dead_misc_params_for_bypassed_nodes(self):
        """源里 mode=4（bypass）的节点不生成 misc 控件：面板不出现点不动的死滑杆。

        回归：st1 的 detailer 链整体在源里 bypass，但其节点（7/13-16/19/20/33 等）
        曾各自暴露 m{nid}_* 参数——节点任何组状态下都不在有效图，启动 dry-run 报
        "节点不在有效图"，面板上是死控件。
        """
        ui = self._workflow("stress_test1.json")
        mode4 = {n["id"] for n in ui["nodes"] if n.get("mode") == 4}
        self.assertTrue(len(mode4) >= 8, "fixture 应含大段 bypass 链段")
        params = self._gen(ui, "stress_test1.json")["params"]
        bad = [p for p in params if p["key"].startswith("m") and p["node"] in mode4]
        self.assertEqual(bad, [])
        # 生成后的面板不引用任何 mode=4 节点
        self.assertTrue(all(p["node"] not in mode4 for p in params), params)

    def test_no_placeholder_or_dead_slots(self):
        """不生成死控件：seed 的 control_after_generate 占位符不当参数；图槽不引用 bypass 节点。"""
        for name in ("stress_test1.json", "stress_test2.json"):
            ui = self._workflow(name)
            raw = self._gen(ui, name)
            # control_after_generate 永不上送 API，做成参数是死控件（st2 效率采样器触发）
            self.assertNotIn("control_after_generate",
                             [p["widget"] for p in raw["params"]], name)
            # 图槽不指向源里 mute/bypass 的节点
            mode_bad = {n["id"] for n in ui["nodes"] if n.get("mode") in (1, 4)}
            slot_nodes = [s["node"] for s in raw.get("image_slots", [])]
            self.assertFalse(set(slot_nodes) & mode_bad, f"{name}: {slot_nodes}")


if __name__ == "__main__":
    unittest.main()
