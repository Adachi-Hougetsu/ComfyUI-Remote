# 工作流自动导入 —— 节点多样性怎么保证

设置页 →「工作流导入」把 ComfyUI `user/default/workflows/` 里的任意 workflow 一键变成
手机面板模板（识别引擎生成 schema → 交叉校验 → 转换 dry-run → 注册）。

**核心原则：识别引擎不认节点类名，只认三样东西**——widget 名、输入/输出类型 token、
图拓扑。所以新节点只要遵循 ComfyUI 命名惯例（`seed/steps/cfg`、`ckpt_name/lora_name`、
`MODEL/CLIP/CONDITIONING/LATENT`、`model/positive/latent_image`…）就自动被识别，
**不需要改任何代码**。加了新节点类型也不用等更新。

## 识别能力一览

| 角色 | 靠什么认 |
|---|---|
| 采样器 | 消费 CONDITIONING + 产 LATENT（KSampler / KSamplerAdvanced / 未来任何采样器）|
| 底模 | MODEL+CLIP+VAE 三输出 |
| LoRA | `lora_name` widget + MODEL/CLIP 输出 |
| 提示词 | `text` widget + CONDITIONING 输出 |
| 尺寸 | `width`/`height` widget + LATENT 输出（空 latent）|
| 参考图 | `image` widget + IMAGE 输出 + upload 输入 |
| 保存/预览 | `filename_prefix`+`images` / 消费 IMAGE 输入 |
| ControlNet | `control_net`+`image` 输入 / `control_net_name` 加载器 |
| IPAdapter | `ipadapter` 输入 / `ipadapter_file` 加载器 |
| 姿态预处理 | POSE_KEYPOINT 输出 / `detect_*`、`resolution` |
| VAE | `vae`+`samples`→IMAGE / `vae`+`pixels`→LATENT |
| 多段生成 | 反向可达判定终采样器（refer1/2 自动选中 refine 段）|

未识别节点：**原样透传**（链接完整保留），其数值控件自动浮成"高级参数"，
导入永不因陌生节点失败。

## 三层保障（节点多样性 = 数据驱动，不是堆类名）

1. **约定驱动识别**（上表）——新节点遵守命名惯例即自动可用，零配置。
2. **规则注册表 `schemas/_rules.json`（数据，非代码）**——新出现的陌生 widget，
   在文件里加一行即可升级为带标签/区间的参数：
   ```json
   { "widgets": { "guidance": { "type": "float", "min": 0, "max": 35,
       "step": 0.5, "default": 3.5, "label": "CFG Guidance" } } }
   ```
   不加也行——它已经作为通用高级参数可用，加一行只是让它更好看。规则 > 内置惯例。
3. **每模板覆盖 `schemas/<id>.overrides.json`**——自动识别个别不理想（如多采样器
   特殊接线、奇怪的死分支），按 key 非破坏性合并，可手工精修该模板的
   `params / prompts / node_groups.extra_params` 等字段，不动共享代码。

优先级：**每模板 overrides > 规则注册表 > 内置惯例**。

## 安全保证

导入前自动做两类硬校验，任一不过即 422 拒绝导入：
- schema 与 workflow 交叉校验（声明的 node/widget 必须真实存在）；
- 转换 dry-run：**每组开关两态 + 无/有 LoRA** 各转换一次，检查无未接线的必填输入、
  链接源必须是字符串且指向现存节点、patch 补线数 = 声明的 LinkOps 数。

覆盖导入已有模板前，先把旧 schema 备份为 `schemas/<id>.json.bak`。

## API

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/workflows` | 扫描来源目录，标注已导入/可导入 |
| POST | `/api/templates/import` | `{filename, id?, overwrite?}` 导入并注册 |
| DELETE | `/api/templates/{tpl}` | 注销并删 schema/workflow/session |

## 其它

- 没有采样器的工作流（纯预处理预览图）导入时 422，提示不构成可生成模板。
- 识别/导入是**确定性**的：同一文件同一配置重复导入结果一致。
