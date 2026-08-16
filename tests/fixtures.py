"""测试夹具：加载 amiya 模板 + 构造常用 opts。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.template_store import load_template  # noqa: E402
from core.workflow_converter import ConvertOptions  # noqa: E402

TPL = load_template("amiya")


def make_params(**extra) -> dict:
    base = {
        "model": "Illustrious-XL-v2.0.safetensors",
        "positive": "masterpiece, 1girl",
        "negative": "worst quality, low quality",
        "steps": 28, "cfg": 4,
        "sampler_name": "dpmpp_2m_sde_gpu", "scheduler": "karras",
        "denoise": 0.8, "width": 1024, "height": 1232,
        "seed": 123, "seed_mode": "fixed",
    }
    base.update(extra)
    return base


def one_lora() -> list:
    return [{"name": "L1.safetensors", "strength_model": 0.8, "strength_clip": 1.0}]


def convert(params=None, groups=None, slots=None, loras=None):
    opts = ConvertOptions(
        params=dict(params or make_params(), **(dict(loras=loras) if loras is not None else {})),
        enabled_groups=groups or {},
        image_slots=slots or {},
    )
    return TPL.workflow, TPL.schema, opts
