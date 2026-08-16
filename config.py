"""ComfyUI 手机控制层 —— 全局配置"""
from __future__ import annotations

import json
import os
from pathlib import Path

# 目录
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"   # 模板 workflow（UI 格式快照，不改动）
SCHEMAS_DIR = BASE_DIR / "schemas"       # 面板定义
DATA_DIR = BASE_DIR / "data"             # 运行时数据
STATIC_DIR = BASE_DIR / "static"         # PWA 前端
CERT_DIR = BASE_DIR / "certs"            # 局域网 HTTPS 证书（make_cert.py 生成）


def _runtime_settings() -> dict:
    """data/settings.json 运行时覆盖（设置页可改 ComfyUI 地址，免改代码重启）。"""
    try:
        p = DATA_DIR / "settings.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


_RUNTIME = _runtime_settings()

# ComfyUI 地址（控制层与 ComfyUI 同一台电脑，默认 127.0.0.1 不走代理、不依赖局域网 IP）
# 优先级：data/settings.json（设置页可改）> 环境变量 COMFY_URL > 默认
COMFY_URL = (_RUNTIME.get("comfy_url") or os.environ.get("COMFY_URL")
             or "http://127.0.0.1:8188").rstrip("/")
COMFY_WS = (_RUNTIME.get("comfy_ws")
            or COMFY_URL.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
            + "/ws")

# ComfyUI 的 input 目录（健康检查时探测参考图是否存在；不存在则跳过该检查）
# 本机路径用环境变量 COMFY_INPUT_DIR / COMFY_WORKFLOWS_DIR 指定（默认相对当前目录）。
COMFY_INPUT_DIR = Path(os.environ.get("COMFY_INPUT_DIR", r"ComfyUI\input"))

# ComfyUI 用户工作流目录（自动导入来源；不存在时回退 input 同级 user/default/workflows）
COMFY_WORKFLOWS_DIR = Path(
    os.environ.get("COMFY_WORKFLOWS_DIR", r"ComfyUI\user\default\workflows"))

# 控制层监听
LISTEN = "0.0.0.0"
PORT = 8000

# 启动时预热 object_info（拉取失败不阻塞启动）
WARM_OBJECT_INFO = True

# 启动时自动导入 ComfyUI 工作流目录里未注册的工作流（设置 COMFY_AUTO_IMPORT=0 关闭）
AUTO_IMPORT = os.environ.get("COMFY_AUTO_IMPORT", "1") != "0"

# 限制
MAX_RUNS = 20        # 生成次数（抽卡）上限
MAX_LORAS = 8        # LoRA 数量上限

# 参考图上传到 ComfyUI 的类型
UPLOAD_TYPE = "input"

# 访问令牌（局域网鉴权）：留空 = 不鉴权；设置后 /api/* 与 /ws 需带 X-Access-Token 头
# 或 ?token= 查询参数（图片 <img> 标签用）。可用环境变量 ACCESS_TOKEN 覆盖。
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "")
