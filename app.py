"""应用装配：服务容器 → 路由 → 静态托管 → 生命周期钩子。"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import ACCESS_TOKEN, AUTO_IMPORT, CERT_DIR, DATA_DIR, STATIC_DIR, WARM_OBJECT_INFO
from comfy.client import ComfyClient
from comfy.object_info import ObjectInfoCache
from comfy.ws_listener import ComfyWSListener
from core.session import SessionStore
from core.template_store import list_templates, load_template
from core.workflow_converter import dry_run_template
from tasks import TaskManager
from api import AppState, build_routers
from api.imports import auto_import_missing

log = logging.getLogger("comfyui_remote")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

# 下拉选项（底模/LoRA/采样器…）来自 /object_info 缓存，每隔这么久刷一次，
# 让新放进 ComfyUI models/ 目录的文件自动出现，无需重启控制层。
OBJECT_INFO_REFRESH_SECONDS = 300


def create_app() -> FastAPI:
    client = ComfyClient()
    object_info = ObjectInfoCache(client)
    ws_listener = ComfyWSListener()
    session = SessionStore(DATA_DIR)
    tasks = TaskManager(client, object_info, ws_listener, session)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 1. object_info 预热（失败不阻塞：ComfyUI 可能后启动）
        if WARM_OBJECT_INFO:
            ok = await object_info.refresh()
            log.info("object_info 预热: %s", "ok" if ok else "失败（ComfyUI 未启动？）")
        # 2. 自动同步：ComfyUI 工作流目录里未注册的 → 生成 schema + 注册，
        #    让电脑上的工作流开箱即在手机端可选（幂等，离线也能跑）。
        #    可用环境变量 COMFY_AUTO_IMPORT=0 关闭（只用手工导入）。
        oi = object_info.data() if object_info.ready else None
        if AUTO_IMPORT:
            synced = auto_import_missing(oi)
            if synced.get("imported"):
                log.info("自动导入 %d 个工作流: %s", len(synced["imported"]), ", ".join(synced["imported"]))
            for e in synced.get("errors", []):
                log.warning("自动导入跳过 %s", e)
        else:
            log.info("自动导入已关闭（COMFY_AUTO_IMPORT=0），工作流需在设置页手动导入")
        # 3. 模板 schema 校验（配置错误早报）+ 转换 dry-run（widget 对齐告警）
        for t in list_templates():
            try:
                tpl = load_template(t["id"])
                for w in dry_run_template(tpl.workflow, tpl.schema):
                    log.warning("模板 %s dry-run: %s", t["id"], w)
            except ValueError as e:
                log.error("模板 %s 校验失败:\n%s", t["id"], e)
            except Exception as e:
                log.error("模板 %s dry-run 异常: %s", t["id"], e)
        # 4. WS 常驻 + 自愈：ComfyUI 后启动时，WS 连上即刷新 object_info
        ws_listener.on_connected(object_info.refresh)
        ws_listener.start()

        # 5. 周期性刷新 /object_info：新放的底模/LoRA 定时出现在下拉（无需重启）
        async def _periodic_object_info():
            while True:
                await asyncio.sleep(OBJECT_INFO_REFRESH_SECONDS)
                # 走 state 引用：设置页热重配替换 object_info 后仍刷新当前实例
                await state.object_info.refresh()  # 内部吞异常，ComfyUI 不在线则保持旧缓存

        period_task = asyncio.create_task(_periodic_object_info())

        yield
        period_task.cancel()
        await ws_listener.stop()
        await client.close()

    app = FastAPI(title="ComfyUI 遥控控制层", version="0.1.0", lifespan=lifespan)

    # 局域网访问令牌（可选）：/api/* 与 /ws 需带 X-Access-Token 头或 ?token= 参数。
    # 静态文件（PWA 壳）不拦截——壳本身无机密，数据全在 API 层。
    if ACCESS_TOKEN:
        @app.middleware("http")
        async def _access_token_auth(request, call_next):
            path = request.url.path
            if path.startswith("/api/") or path == "/ws":
                token = request.headers.get("X-Access-Token") or request.query_params.get("token")
                if token != ACCESS_TOKEN:
                    return JSONResponse({"detail": "需要访问令牌"}, status_code=401)
            return await call_next(request)

    # 局域网 HTTPS 引导：手机在控制层还是 HTTP 时下载根证书装信任。
    # ca.crt 是公开证书（私钥 ca.key 永不外发），无需鉴权即可取。
    @app.get("/ca.crt")
    async def ca_cert():
        p = CERT_DIR / "ca.crt"
        if not p.exists():
            return JSONResponse({"detail": "尚未生成证书，请先运行 make_cert.bat"}, status_code=404)
        return FileResponse(p, media_type="application/x-x509-ca-cert", filename="ca.crt")

    state = AppState(client=client, object_info=object_info, ws=ws_listener,
                     tasks=tasks, session=session)

    for r in build_routers(state):
        app.include_router(r)

    # 静态挂载放最后：/api/* 路由优先，未匹配的走 PWA
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

    return app


app = create_app()
