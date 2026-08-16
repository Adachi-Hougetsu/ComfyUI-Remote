"""转换器黄金测试：默认链路 / 参数覆盖 / 各开关组合。"""
import unittest

from core.graph import Graph, _bypass_downstream_first, bypass_rewire
from core.ids import IdAllocator
from core.workflow_converter import convert_ui_to_api
from fixtures import make_params, one_lora, convert


class TestConverterBaseline(unittest.TestCase):
    def test_default_ipa_on(self):
        ui, schema, opts = convert(loras=one_lora())
        api, diag = convert_ui_to_api(ui, schema, opts)
        n3 = api["3"]["inputs"]
        # 模型链 4→10→43→3
        self.assertEqual(n3["model"], ["43", 0])
        self.assertEqual(api["43"]["inputs"]["model"], ["10", 0])
        # 条件链 6/7→35→3
        self.assertEqual(n3["positive"], ["35", 0])
        self.assertEqual(n3["negative"], ["35", 1])
        # 参数覆盖落点
        self.assertEqual(n3["steps"], 28)
        self.assertEqual(n3["cfg"], 4)
        self.assertEqual(n3["sampler_name"], "dpmpp_2m_sde_gpu")
        self.assertEqual(n3["scheduler"], "karras")
        self.assertEqual(n3["denoise"], 0.8)
        self.assertEqual(n3["seed"], 123)
        self.assertEqual(api["5"]["inputs"]["width"], 1024)
        self.assertEqual(api["5"]["inputs"]["height"], 1232)
        # OpenPose 默认关：17 不在有效图
        self.assertNotIn("17", api)
        # 死节点 19 永远剔除
        self.assertNotIn("19", api)
        # LoRA 落在链头
        self.assertEqual(api["10"]["inputs"]["lora_name"], "L1.safetensors")

    def test_param_override_and_image_slot(self):
        params = make_params(steps=40, cfg=7, width=768, seed=999)
        ui, schema, opts = convert(
            params=params, loras=one_lora(),
            slots={"ipa_ref": "ref.png", "compose_ref": "comp.png"},
        )
        api, _ = convert_ui_to_api(ui, schema, opts)
        n3 = api["3"]["inputs"]
        self.assertEqual(n3["steps"], 40)
        self.assertEqual(n3["cfg"], 7)
        self.assertEqual(n3["seed"], 999)
        self.assertEqual(api["5"]["inputs"]["width"], 768)
        # 参考图写入 LoadImage 节点
        self.assertEqual(api["44"]["inputs"]["image"], "ref.png")
        self.assertEqual(api["31"]["inputs"]["image"], "comp.png")

    def test_prompt_override(self):
        params = make_params(positive="custom pos", negative="custom neg")
        ui, schema, opts = convert(params=params, loras=one_lora())
        api, _ = convert_ui_to_api(ui, schema, opts)
        self.assertEqual(api["6"]["inputs"]["text"], "custom pos")
        self.assertEqual(api["7"]["inputs"]["text"], "custom neg")

    def test_idempotent(self):
        """同一模板转换两次结果一致（不污染模板）。"""
        ui, schema, opts = convert(loras=one_lora())
        a1, _ = convert_ui_to_api(ui, schema, opts)
        a2, _ = convert_ui_to_api(ui, schema, opts)
        self.assertEqual(a1, a2)

    def test_link_origins_are_string_keys(self):
        """回归：链接 [o_id, slot] 的 o_id 必须是字符串，且指向存在的 prompt 键。

        ComfyUI 校验用 prompt[o_id] 查来源节点（execution.py validate_inputs），
        int 键会 KeyError → 400 "Prompt outputs failed validation"。
        """
        for groups in (
            {},
            {"openpose": True, "ipa": True},
            {"openpose": False, "ipa": False},
        ):
            ui, schema, opts = convert(groups=groups, loras=one_lora())
            api, _ = convert_ui_to_api(ui, schema, opts)
            for nid, node in api.items():
                for name, val in node["inputs"].items():
                    if isinstance(val, list) and len(val) == 2:
                        self.assertIsInstance(
                            val[0], str,
                            f"groups={groups} {nid}.{name} 链接 o_id 应为字符串，得到 {val[0]!r}")
                        self.assertIn(
                            val[0], api,
                            f"groups={groups} {nid}.{name} 引用不存在的节点 {val[0]}")


class TestConverterGroups(unittest.TestCase):
    def test_ipa_off_rewire(self):
        """IPA 关：43 透传，10→3 直连，卫星剔除。"""
        ui, schema, opts = convert(groups={"openpose": False, "ipa": False}, loras=one_lora())
        api, diag = convert_ui_to_api(ui, schema, opts)
        self.assertNotIn("43", api)
        self.assertNotIn("44", api)
        self.assertNotIn("45", api)
        self.assertNotIn("46", api)
        self.assertEqual(api["3"]["inputs"]["model"], ["10", 0])
        self.assertEqual(set(diag["bypassed"]), {43})

    def test_ipa_off_no_lora(self):
        """IPA 关 + 无 LoRA：4→3 直连。"""
        ui, schema, opts = convert(groups={"openpose": False, "ipa": False})
        api, _ = convert_ui_to_api(ui, schema, opts)
        self.assertNotIn("10", api)
        self.assertEqual(api["3"]["inputs"]["model"], ["4", 0])
        self.assertEqual(api["47"]["inputs"]["clip"], ["4", 1])

    def test_openpose_on_rewire(self):
        """OpenPose 开：17 插进 35→3 条件链，且补上 VAE 线。"""
        ui, schema, opts = convert(groups={"openpose": True, "ipa": True}, loras=one_lora())
        api, diag = convert_ui_to_api(ui, schema, opts)
        n3 = api["3"]["inputs"]
        n17 = api["17"]["inputs"]
        self.assertIn("17", api)
        self.assertEqual(n3["positive"], ["17", 0])
        self.assertEqual(n3["negative"], ["17", 1])
        self.assertEqual(n17["positive"], ["35", 0])
        self.assertEqual(n17["negative"], ["35", 1])
        # VAE 必连线（node17.vae 是 required）
        self.assertEqual(n17["vae"], ["4", 2])
        # 构图 ControlNet 35 仍在
        self.assertEqual(api["35"]["inputs"]["control_net"], ["30", 0])
        self.assertEqual(len(diag["patches"]) >= 7, True)

    def test_openpose_off_preserves_35_to_3(self):
        ui, schema, opts = convert(groups={"openpose": False, "ipa": True}, loras=one_lora())
        api, _ = convert_ui_to_api(ui, schema, opts)
        self.assertNotIn("17", api)
        self.assertNotIn("11", api)
        self.assertEqual(api["3"]["inputs"]["positive"], ["35", 0])

    def test_openpose_extra_params_applied_when_enabled(self):
        params = make_params(op_strength=1.2, op_start=0.1, op_end=0.8)
        ui, schema, opts = convert(
            params=params, groups={"openpose": True, "ipa": True}, loras=one_lora())
        api, _ = convert_ui_to_api(ui, schema, opts)
        n17 = api["17"]["inputs"]
        self.assertEqual(n17["strength"], 1.2)
        self.assertEqual(n17["start_percent"], 0.1)
        self.assertEqual(n17["end_percent"], 0.8)

    def test_ipa_extra_params_ignored_when_disabled(self):
        params = make_params(ipa_weight=1.5)
        ui, schema, opts = convert(
            params=params, groups={"openpose": False, "ipa": False}, loras=one_lora())
        api, _ = convert_ui_to_api(ui, schema, opts)
        self.assertNotIn("43", api)


class TestChainedBypass(unittest.TestCase):
    """链式透传（A→B→C 全 bypass）：下游优先拓扑序，中间先重连、源头后摘。

    回归自 stress_test2 的 LoRA Stacker 链（498←346←458，346/458 全 mode=4）：
    若按集合任意序先摘 458，346 就找不到源、498 悬空。
    """

    def _chain_ui(self, tail_unlinked: bool):
        """1(m0) → 2(m4) → 3(m4) → 4(m0)。tail_unlinked=True 时 3 的输入空（源透传断头）。"""
        n2_link = 10
        n3_link = None if tail_unlinked else 11
        nodes = [
            {"id": 1, "type": "A", "mode": 0, "order": 0,
             "inputs": [{"name": "x", "type": "X", "link": None}],
             "outputs": [{"name": "y", "type": "X", "links": [10]}]},
            {"id": 2, "type": "B", "mode": 4, "order": 1,
             "inputs": [{"name": "x", "type": "X", "link": n2_link}],
             "outputs": [{"name": "y", "type": "X", "links": [11]}]},
            {"id": 3, "type": "C", "mode": 4, "order": 2,
             "inputs": [{"name": "x", "type": "X", "link": n3_link}],
             "outputs": [{"name": "y", "type": "X", "links": [12]}]},
            {"id": 4, "type": "D", "mode": 0, "order": 3,
             "inputs": [{"name": "x", "type": "X", "link": 12}],
             "outputs": [{"name": "y", "type": "X", "links": []}]},
        ]
        links = [
            [10, 1, 0, 2, 0, "X"],
            [11, 2, 0, 3, 0, "X"],
            [12, 3, 0, 4, 0, "X"],
        ]
        return {"nodes": nodes, "links": links, "last_node_id": 4, "last_link_id": 12}

    def test_downstream_first_order(self):
        ui = self._chain_ui(tail_unlinked=False)
        g = Graph(ui, IdAllocator(4, 12))
        order = _bypass_downstream_first(g, {2, 3})
        # 3 是下游（2 喂 3），必须先于 2 处理，否则 3 重连时 2 已被摘掉
        self.assertEqual(order, [3, 2])

    def test_chain_collapses_to_source(self):
        """1→2→3→4 全透传 → 4.x 接回源头 1。"""
        ui = self._chain_ui(tail_unlinked=False)
        g = Graph(ui, IdAllocator(4, 12))
        bypass_rewire(g, {2, 3})
        self.assertNotIn(2, g.nodes)
        self.assertNotIn(3, g.nodes)
        self.assertEqual(g.input_source(g.nodes[4], "x"), (1, 0))

    def test_tail_dangle_is_expected(self):
        """断头透传（3 的输入空）：4 悬空但属预期（源工作流 ComfyUI bypass 同样丢链）。"""
        ui = self._chain_ui(tail_unlinked=True)
        g = Graph(ui, IdAllocator(4, 12))
        bypass_rewire(g, {2, 3})
        self.assertNotIn(2, g.nodes)
        self.assertNotIn(3, g.nodes)
        self.assertIsNone(g.input_source(g.nodes[4], "x"))
        # schema_gen._expected_bypass_dangle 沿链追到底（3 输入空）→ 判预期
        from core import schema_gen as sg
        orig_input = {"link": 12}  # 4.x 原始输入
        self.assertTrue(sg._expected_bypass_dangle(ui, orig_input, "x"))


if __name__ == "__main__":
    unittest.main()
