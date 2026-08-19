# ComfyUI Remote（局域网手机遥控 ComfyUI）

在局域网内用手机 / 平板遥控电脑上的 [ComfyUI](https://github.com/comfyanonymous/ComfyUI)：选模板、填参数、一键生成，出图后直接在手机上浏览。

- 控制层：FastAPI（电脑端，默认 `http://<你的IP>:8000`）
- 前端：原生 JS PWA，无框架，可「添加到主屏幕」当 App 用
- 依赖：电脑已装 ComfyUI（API 开启 `--listen 0.0.0.0`）

## 功能

- **工作流自动导入**：扫描 ComfyUI 用户工作流目录，自动生成手机端表单（识别引擎认 widget 名 + 图拓扑，不认节点类名）
- **群组框开关组**：把 ComfyUI 群组框当组件边界，自动推导「功能开关」（ControlNet 摘除/补入、IPAdapter 旁路、放大链、模块开关等）
- **模块开关**：源工作流中整体透传(bypass)的模块框（如 ADetailer、风格组合）自动升级为手机端开关——默认关保持作者设定，打开即激活整个模块
- **气泡式生成页**：每个节点一个气泡，点击弹出小窗调参；LoRA 每个独立，正负提示词两个独立气泡
- **弹窗选择器**：平板/桌面上下拉（模板/底模/LoRA/采样器）以底部弹窗呈现，手机保持系统原生选择器
- **抽卡生成**：一次提交 N 个随机种子，WebSocket 实时推送进度，取消按批次定向中断
- **图库**：记录每次生成的真实种子与参数，可按模板筛选、查看单图生成参数；超限自动清理
- **结果区参数**：生成结果每张图可查看该次实际注入参数（含真实种子）
- **断线自愈**：控制层与 ComfyUI 的 WS 断开自动重连，任务进度轮询对账；切页后进度自动恢复
- **外观切换**：设置页可选跟随系统 / 浅色 / 深色
- **ComfyUI 地址可配置**：设置页直接修改（热重连，免改代码重启）
- **可选鉴权**：设置页一键生成访问令牌（后端持久化、热更新），`/api/*` 与 `/ws` 需带 `X-Access-Token` 头（或 `?token=` 参数）
- **安卓 HTTPS 安装态**：一键自签证书，浏览器「添加到主屏幕」走 HTTPS（见 `docs/安卓安装态.md`）

## 安全（共用网络 / 宿舍 / 公共 WiFi）

控制层默认监听 `0.0.0.0:8000`——同一局域网内任何设备都能访问。共用网络建议：

1. **防火墙来源限制（推荐方案）**：只放行 Tailscale 网段访问 8000 端口，局域网内
   其他设备直接连接失败（连端口扫描都探测不到服务）：
   ```bat
   netsh advfirewall firewall delete rule name="ComfyUI Remote (comfyui-ds venv)"
   netsh advfirewall firewall add rule name="ComfyUI Remote (Tailscale-only)" dir=in action=allow program="<你的路径>\comfyui-ds\.venv\Scripts\python.exe" protocol=TCP localport=8000 remoteip=100.64.0.0/10 profile=any
   ```
   配置后所有设备（手机/平板）通过 Tailscale 虚拟 IP（`100.x.x.x`）访问，共用网络
   中的其他设备无法连接。
2. **可选：访问令牌**：设置环境变量 `ACCESS_TOKEN`（或写入 `data/settings.json` 的
   `access_token` 字段）后，`/api/*` 与 `/ws` 需带 `X-Access-Token` 头（或 `?token=`
   参数）；忘记令牌时删除 `data/settings.json` 里的 `access_token` 字段并重启。
3. **HTTPS**（可选，防局域网嗅探）：`make_cert.bat` 生成证书后 `run.bat https` 启动。

## 如何连接（使用者必读）

```
手机 / 平板 ──> 控制层（你的电脑 :8000）──> ComfyUI（同一台电脑 :8188）
```

- **控制层与 ComfyUI 装在同一台电脑**（本项目的标准用法）：ComfyUI 地址默认
  `http://127.0.0.1:8188`，**零配置直接可用**。
- **手机端访问控制层**：`http://<电脑的局域网IP>:8000`（电脑 IP 用 `ipconfig` 查看，
  或看控制层启动日志提示）。
- **ComfyUI 地址不对时**（改了端口、或 ComfyUI 在别的机器）：在手机端
  **设置页 → ComfyUI 地址**填写 `http://IP:端口`，保存即热重连（免重启）。
  ComfyUI 启动时终端会打印它自己的地址（`To see the GUI go to: ...`）。
- **在外网访问**：电脑与手机各装 [Tailscale](https://tailscale.com) 并登录同一账号，
  用虚拟 IP（`100.x.x.x`）替代局域网 IP 即可（免费、加密、无需公网服务器）。

## 快速开始

> 假设：电脑 IP 为 `<你的IP>`（请换成你自己的），控制层与 ComfyUI 同机。

### 1. 安装依赖

```bat
pip install -r requirements.txt
```

### 2. 配置（可选）

默认 `ComfyUI\input`、`ComfyUI\user\default\workflows` 取**控制层所在目录的相对路径**。如果你的 ComfyUI 在别处，设置环境变量：

```bat
set COMFY_INPUT_DIR=D:\你的ComfyUI\input
set COMFY_WORKFLOWS_DIR=D:\你的ComfyUI\user\default\workflows
set COMFY_URL=http://127.0.0.1:8188
set COMFY_AUTO_IMPORT=0   rem 可选：设为 0 关闭启动时的自动导入（改用设置页手动导入）
```

> ComfyUI 地址也可以不改环境变量：控制层启动后，在手机端**设置页 → ComfyUI 地址**直接修改并保存（热重连，免重启）。

### 3. 启动

```bat
run.bat
```

默认 HTTP 模式（日常调试用），控制层启动后会自动扫描并导入工作流、校验模板、预热模型下拉。

手机浏览器打开 `http://<你的IP>:8000` 即可使用。

### 4.（可选）装成 PWA / HTTPS

```bat
make_cert.bat        # 生成局域网自签证书（certs/）
run.bat https        # 以 HTTPS 启动
```

手机访问 `http://<你的IP>:8000/ca.crt` 下载根证书并信任，再在 Chrome 里「添加到主屏幕」。详见 `docs/安卓安装态.md`。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/templates` | 模板列表 |
| GET | `/api/templates/{tpl}` | 模板面板定义 |
| GET | `/api/templates/{tpl}/model-lists` | 底模/LoRA 等下拉（`?refresh=1` 强制重读） |
| GET/PUT | `/api/templates/{tpl}/params` | 读写参数 |
| POST | `/api/generate` | 提交生成批次 |
| GET/DELETE | `/api/generate/{batch_id}` | 批次状态 / 取消 |
| GET | `/api/gallery` | 图库 |
| GET | `/api/images/{filename}` | 图片（代理 ComfyUI） |
| GET | `/api/workflows` | 扫描 ComfyUI 工作流 |
| POST | `/api/templates/import` | 导入工作流生成模板 |
| DELETE | `/api/templates/{tpl_id}` | 删除模板 |
| POST | `/api/upload` | 上传参考图 |
| GET/PUT | `/api/settings` | 读取 / 修改 ComfyUI 地址（保存即热重连） |
| WS | `/ws` | 任务进度推送 |
| GET | `/ca.crt` | 下载局域网根证书（装 PWA 用） |

## 项目结构

```
app.py / server.py     FastAPI 装配与 uvicorn 入口
config.py              全局配置（端口、目录、令牌）
tasks.py               生成批次管理（队列/进度/取消）
api/                   REST 路由 + WebSocket
core/                  schema 识别引擎、模板存储、工作流转换、会话
comfy/                 ComfyUI 客户端（API / object_info / WS）
static/                PWA 前端（生成/图库/设置三页）
schemas/               模板面板定义（_rules.json 规则注册表 + 每模板 overrides）
templates/             模板 workflow（UI 快照）
tests/                 单元测试（stdlib unittest）
docs/                  文档（自动导入原理、安卓 HTTPS 安装态）
```

## 测试

```bat
python -m unittest discover -s tests
```

> 全新副本首次运行前，请先启动一次控制层：开启自动导入（默认）时会从
> `COMFY_WORKFLOWS_DIR` 自动生成 `data/templates.json`；关闭自动导入时需在设置页
> 手动导入。**个人工作流与面板定义（templates/、schemas/）不入库**——仓库自带
> 一个示例模板 `amiya.json`（含 IPAdapter / OpenPose 开关组）。示例模板引用的
> 底模/LoRA/参考图是你本机的文件，导入后模型下拉为空或显示"（已失效）"属正常，
> 选你自己的模型即可；参考图槽位请自行上传。

## 技术栈

FastAPI · uvicorn · httpx · websockets · python-multipart · cryptography（自签证书）· 原生 JS PWA（service worker，网络优先 + 离线缓存兜底）

## 文档

- [docs/IMPORT.md](docs/IMPORT.md) —— 工作流自动导入与 schema 识别原理
- [docs/安卓安装态.md](docs/安卓安装态.md) —— 安卓 HTTPS / PWA 安装步骤

## 后续开发

本项目后续开发将与作者的 ComfyUI 学习进度同步推进：随着对 ComfyUI 生态
（自定义节点、复杂工作流、ControlNet / IPAdapter / 高清修复等玩法）的理解
不断加深，持续扩展识别引擎、面板能力与模板库，让手机端面板逐步覆盖
更多真实使用场景。

## License

MIT License（见 [LICENSE](LICENSE)）。
