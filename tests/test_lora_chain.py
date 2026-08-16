"""LoRA 链插入测试：多挂串联 / 清空透传 / 权重覆盖。"""
import copy
import unittest

from core.workflow_converter import ConvertOptions, convert_ui_to_api
from fixtures import make_params, convert


class TestLoraChain(unittest.TestCase):
    def test_multi_lora_serial(self):
        loras = [
            {"name": "L1.safetensors", "strength_model": 0.8, "strength_clip": 1.0},
            {"name": "L2.safetensors", "strength_model": 0.6, "strength_clip": 0.9},
            {"name": "L3.safetensors", "strength_model": 0.5, "strength_clip": 1.0},
        ]
        ui, schema, opts = convert(loras=loras)
        api, diag = convert_ui_to_api(ui, schema, opts)
        # 链 4→10(L1)→48(L2)→49(L3)→43；clip 4→10→48→49→47
        self.assertEqual(api["10"]["inputs"]["model"], ["4", 0])
        self.assertEqual(api["10"]["inputs"]["lora_name"], "L1.safetensors")
        self.assertEqual(api["48"]["inputs"]["model"], ["10", 0])
        self.assertEqual(api["48"]["inputs"]["lora_name"], "L2.safetensors")
        self.assertEqual(api["49"]["inputs"]["model"], ["48", 0])
        self.assertEqual(api["49"]["inputs"]["lora_name"], "L3.safetensors")
        self.assertEqual(api["43"]["inputs"]["model"], ["49", 0])
        self.assertEqual(api["47"]["inputs"]["clip"], ["49", 1])
        # 权重覆盖
        self.assertEqual(api["48"]["inputs"]["strength_model"], 0.6)
        self.assertEqual(api["48"]["inputs"]["strength_clip"], 0.9)
        self.assertEqual(diag["loras"], [48, 49])

    def test_single_lora_stays_on_head(self):
        loras = [{"name": "L1.safetensors", "strength_model": 0.8, "strength_clip": 1.0}]
        ui, schema, opts = convert(loras=loras)
        api, diag = convert_ui_to_api(ui, schema, opts)
        self.assertNotIn("48", api)
        self.assertEqual(diag["loras"], [])
        self.assertEqual(api["10"]["inputs"]["lora_name"], "L1.safetensors")
        self.assertEqual(api["43"]["inputs"]["model"], ["10", 0])

    def test_empty_loras_bypasses_head(self):
        ui, schema, opts = convert(loras=[])
        api, diag = convert_ui_to_api(ui, schema, opts)
        self.assertNotIn("10", api)
        self.assertEqual(api["43"]["inputs"]["model"], ["4", 0])
        self.assertEqual(api["47"]["inputs"]["clip"], ["4", 1])

    def test_multi_lora_ipa_off(self):
        """IPA 关时链尾消费点应变为 3（从有效图读，而非原始 [43,0]）。"""
        loras = [
            {"name": "L1.safetensors", "strength_model": 0.8, "strength_clip": 1.0},
            {"name": "L2.safetensors", "strength_model": 0.6, "strength_clip": 0.9},
        ]
        ui, schema, opts = convert(groups={"openpose": False, "ipa": False}, loras=loras)
        api, _ = convert_ui_to_api(ui, schema, opts)
        self.assertNotIn("43", api)
        self.assertEqual(api["3"]["inputs"]["model"], ["48", 0])  # 链尾接回 KSampler
        self.assertEqual(api["48"]["inputs"]["model"], ["10", 0])
        self.assertEqual(api["10"]["inputs"]["model"], ["4", 0])
        self.assertEqual(api["47"]["inputs"]["clip"], ["48", 1])


class TestMultiTemplateLoraChain(unittest.TestCase):
    """双模板 LoRA 链（4→10→50→43/47，template_lora_chain=[10,50]）位置映射测试。

    用户列表按应用顺序对上模板链：模板多余的透传，用户多余的新建节点插链尾。
    """

    def _fixture(self):
        """把 amiya 模板扩成双模板 LoRA 链。"""
        from fixtures import TPL
        ui = copy.deepcopy(TPL.workflow)
        nodes = ui["nodes"]
        mod_out = [l[0] for l in ui["links"] if l[1] == 10 and l[2] == 0]
        clip_out = [l[0] for l in ui["links"] if l[1] == 10 and l[2] == 1]
        for l in ui["links"]:
            if l[0] in mod_out or l[0] in clip_out:
                l[1] = 50  # 原 10→消费点 的链改从 50 出发
        lid = ui["last_link_id"] + 1
        ui["links"].append([lid, 10, 0, 50, 0, "MODEL"])
        ui["links"].append([lid + 1, 10, 1, 50, 1, "CLIP"])
        ui["last_link_id"] = lid + 1
        n10 = next(n for n in nodes if n["id"] == 10)
        n50 = copy.deepcopy(n10)
        n50["id"] = 50
        n50["order"] = n10["order"] + 1
        n50["inputs"][0]["link"] = lid
        n50["inputs"][1]["link"] = lid + 1
        n50["outputs"][0]["links"] = list(mod_out)
        n50["outputs"][1]["links"] = list(clip_out)
        n10["outputs"][0]["links"] = [lid]
        n10["outputs"][1]["links"] = [lid + 1]
        nodes.append(n50)
        ui["last_node_id"] = 50

        schema = copy.deepcopy(TPL.schema)
        schema.lora.template_lora_chain = [10, 50]
        schema.lora.default = [
            {"name": "T1.safetensors", "strength_model": 0.8, "strength_clip": 1.0},
            {"name": "T2.safetensors", "strength_model": 0.6, "strength_clip": 0.9},
        ]
        return ui, schema

    def _conv(self, loras):
        ui, schema = self._fixture()
        opts = ConvertOptions(params=make_params(loras=loras), enabled_groups={}, image_slots={})
        return convert_ui_to_api(ui, schema, opts)

    def test_equal_length_updates_in_place(self):
        """默认两 LoRA → 原地更新 10/50，不动链。"""
        api, diag = self._conv([
            {"name": "T1.safetensors", "strength_model": 0.8, "strength_clip": 1.0},
            {"name": "T2.safetensors", "strength_model": 0.6, "strength_clip": 0.9},
        ])
        self.assertEqual(diag["loras"], [])
        self.assertEqual(api["10"]["inputs"]["lora_name"], "T1.safetensors")
        self.assertEqual(api["50"]["inputs"]["lora_name"], "T2.safetensors")
        self.assertEqual(api["50"]["inputs"]["model"], ["10", 0])
        self.assertEqual(api["50"]["inputs"]["clip"], ["10", 1])
        self.assertEqual(api["43"]["inputs"]["model"], ["50", 0])
        self.assertEqual(api["47"]["inputs"]["clip"], ["50", 1])

    def test_fewer_user_loras_bypasses_tail(self):
        """用户只留 1 个 → 链头承载，链尾 50 透传（透明）。"""
        api, diag = self._conv([
            {"name": "T1.safetensors", "strength_model": 0.8, "strength_clip": 1.0},
        ])
        self.assertEqual(diag["loras"], [])
        self.assertNotIn("50", api)
        self.assertEqual(api["10"]["inputs"]["lora_name"], "T1.safetensors")
        self.assertEqual(api["10"]["inputs"]["model"], ["4", 0])
        self.assertEqual(api["43"]["inputs"]["model"], ["10", 0])  # 消费点直连链头
        self.assertEqual(api["47"]["inputs"]["clip"], ["10", 1])

    def test_more_user_loras_inserts_after_tail(self):
        """用户 3 个 → 10/50 各承 1 个，第 3 个新建 51 插到链尾（last_node_id=50 → 从 51 起）。"""
        api, diag = self._conv([
            {"name": "T1.safetensors", "strength_model": 0.8, "strength_clip": 1.0},
            {"name": "T2.safetensors", "strength_model": 0.6, "strength_clip": 0.9},
            {"name": "T3.safetensors", "strength_model": 0.5, "strength_clip": 1.0},
        ])
        self.assertEqual(diag["loras"], [51])
        self.assertEqual(api["51"]["inputs"]["lora_name"], "T3.safetensors")
        self.assertEqual(api["51"]["inputs"]["model"], ["50", 0])
        self.assertEqual(api["43"]["inputs"]["model"], ["51", 0])
        self.assertEqual(api["47"]["inputs"]["clip"], ["51", 1])
        self.assertEqual(api["50"]["inputs"]["model"], ["10", 0])

    def test_empty_bypasses_both(self):
        """清空 → 两条模板 LoRA 全透传，checkpoint 直连消费点。"""
        api, diag = self._conv([])
        self.assertNotIn("10", api)
        self.assertNotIn("50", api)
        self.assertEqual(api["43"]["inputs"]["model"], ["4", 0])
        self.assertEqual(api["47"]["inputs"]["clip"], ["4", 1])


if __name__ == "__main__":
    unittest.main()
