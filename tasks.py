"""任务批次管理：转换 → 逐个提交 → WS 进度聚合 → 结果收集 → 取消。

抽卡 = 生成次数 N：一次提交 N 个 prompt（每个随机种子），ComfyUI 自己排队，
进度靠 WS + queue_remaining 聚合，结果写入 data/gallery.jsonl 供图库页使用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass, field

from config import DATA_DIR, MAX_RUNS
from comfy.client import ComfyError
from core.template_store import load_template
from core.workflow_converter import ConvertOptions, convert_ui_to_api

log = logging.getLogger(__name__)

# 图库不记录的超大字段（提示词太长）
_GALLERY_STRIP = ("positive", "negative")

# 批次记录上限 / 完成批次 TTL（内存中，防无限增长）
_MAX_BATCHES = 50
_BATCH_TTL = 6 * 3600

# 图库文件超限清理：超过 2MB 时裁剪保留尾部最新 _GALLERY_KEEP 条
_GALLERY_MAX_BYTES = 2 * 1024 * 1024
_GALLERY_KEEP = 1000


def _image_url(img: dict) -> str:
    """ImageRef → 前端可访问的代理 URL。"""
    q = [("filename", img.get("filename", ""))]
    if img.get("subfolder"):
        q.append(("subfolder", img["subfolder"]))
    q.append(("type", img.get("type", "output")))
    return "/api/images/" + img.get("filename", "") + "?" + "&".join(
        f"{k}={v}" for k, v in q
    )


def _pct(progress: list) -> int:
    """[value, max] → 0-100 百分比（前端 run_done 期望数字）。"""
    v, m = progress if len(progress) >= 2 else (0, 0)
    return round(v / m * 100) if m else 0


def _describe_submit_error(e: Exception) -> str:
    """把 post_prompt 异常格式化为可读信息；ComfyUI 400 的 node_errors 一并透出。"""
    if isinstance(e, ComfyError) and e.node_errors:
        parts = []
        for nid, err in e.node_errors.items():
            if isinstance(err, dict):
                errs = err.get("errors") or [{"message": str(err)}]
                parts.append(f"节点 {nid}: {errs[0].get('message', str(errs[0])) if isinstance(errs[0], dict) else errs[0]}")
            else:
                parts.append(f"节点 {nid}: {err}")
        return f"{e}\n—— 节点错误 ——\n" + "\n".join(parts)
    return str(e)


def _tail_lines(path, n: int | None) -> list:
    """从文件末尾向上读最多 n 行（按文件顺序返回）。n=None 读全量。避免大文件全量读。"""
    if n is not None and n <= 0:
        return []
    out = []
    with open(path, "rb") as f:
        f.seek(0, 2)
        pos = f.tell()
        carry = b""
        while pos > 0 and (n is None or len(out) < n):
            step = min(pos, 8192)
            pos -= step
            f.seek(pos)
            blk = f.read(step)
            parts = (blk + carry).split(b"\n")
            carry = parts[0]  # 块首可能是不完整行，交给更早的块拼
            for p in reversed(parts[1:]):
                if n is not None and len(out) >= n:
                    break
                out.append(p.decode("utf-8", "replace"))
    if carry:
        out.append(carry.decode("utf-8", "replace"))
    return list(reversed(out))


@dataclass
class BatchRun:
    batch_id: str
    tpl_id: str
    n: int
    prompt_ids: list = field(default_factory=list)
    status: dict = field(default_factory=dict)     # prompt_id -> queued|running|done|error
    progress: dict = field(default_factory=dict)   # prompt_id -> [value, max]
    images: dict = field(default_factory=dict)     # prompt_id -> [ImageRef]
    queue_remaining: int = 0
    error: dict = field(default_factory=dict)      # prompt_id -> message
    created_at: float = 0.0
    done: bool = False
    params_snapshot: dict = field(default_factory=dict)
    run_params: dict = field(default_factory=dict)  # prompt_id -> 该次实际注入后的参数（含种子）
    saved: set = field(default_factory=set)        # 已写入 gallery 的 prompt_id（防重复）
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TaskManager:
    def __init__(self, client, object_info, ws_listener, session_store):
        self.client = client
        self.object_info = object_info
        self.ws = ws_listener
        self.session = session_store
        self.batches: dict[str, BatchRun] = {}
        self._done_handlers: list = []
        self._event_handlers: list = []
        self.ws.on_queue_status(self._on_queue_status)

    # ---------- 事件 ----------

    def on_event(self, handler) -> None:
        """注册事件推送 handler(batch_id, event, payload)。WS 层用。"""
        self._event_handlers.append(handler)

    async def _emit(self, batch: BatchRun, event: str, payload: dict) -> None:
        for h in self._event_handlers:
            try:
                await h(batch.batch_id, event, payload)
            except Exception:
                log.exception("event handler 异常")

    # ---------- 提交 ----------

    async def submit(self, tpl_id: str, params: dict, runs: int,
                     enabled_groups: dict, image_slots: dict) -> BatchRun:
        self._prune()
        runs = max(1, min(int(runs or 1), MAX_RUNS))
        tpl = load_template(tpl_id)
        batch = BatchRun(batch_id=uuid.uuid4().hex[:8], tpl_id=tpl_id, n=runs,
                         created_at=time.time(), params_snapshot=dict(params))
        self.batches[batch.batch_id] = batch

        seed_mode = params.get("seed_mode") or tpl.schema.seed_mode_default
        base_seed = params.get("seed")

        for i in range(runs):
            params_run = dict(params)
            if seed_mode == "random":
                # 抽卡语义：每次随机种子（不管 runs 多少）
                params_run["seed"] = random.randrange(0, 2 ** 63)
            elif base_seed is not None:
                # fixed 语义：尊重用户固定种子——runs>1 时各次用同一种子
                params_run["seed"] = int(base_seed)
            else:
                params_run.pop("seed", None)

            opts = ConvertOptions(params=params_run, enabled_groups=enabled_groups or {},
                                  image_slots=image_slots or {})
            api, diag = convert_ui_to_api(tpl.workflow, tpl.schema, opts)
            try:
                resp = await self.client.post_prompt(api, self.ws.client_id)
            except Exception as e:
                detail = _describe_submit_error(e)
                batch.status["submit_error"] = f"提交失败: {detail}"
                batch.error.setdefault("submit", detail)
                batch.done = True
                await self._emit(batch, "batch_error", {
                    "batch_id": batch.batch_id, "message": detail, "error": detail})
                await self._notify_done(batch)
                return batch
            pid = resp.get("prompt_id")
            batch.prompt_ids.append(pid)
            batch.status[pid] = "queued"
            batch.progress[pid] = [0, 0]
            batch.run_params[pid] = dict(params_run)
            self.ws.subscribe(pid, lambda msg, b=batch: self._on_comfy_msg(b, msg))

        await self._emit(batch, "batch_start", {"batch_id": batch.batch_id, "n": batch.n,
                                                "prompt_ids": list(batch.prompt_ids)})
        return batch

    # ---------- 消息处理 ----------

    async def _on_comfy_msg(self, batch: BatchRun, msg: dict) -> None:
        mtype = msg.get("type")
        data = msg.get("data") or {}
        pid = data.get("prompt_id")
        if not pid or pid not in batch.status:
            return
        if mtype == "executing":
            if data.get("node"):
                batch.status[pid] = "running"
        elif mtype == "progress":
            batch.progress[pid] = [data.get("value", 0), data.get("max", 0)]
            await self._emit(batch, "progress", {
                "batch_id": batch.batch_id, "prompt_id": pid,
                "value": data.get("value", 0), "max": data.get("max", 0),
                "queue_remaining": batch.queue_remaining})
        elif mtype == "progress_state":
            nodes = data.get("nodes") or {}
            for _nid, nd in nodes.items():
                if nd.get("prompt_id") == pid:
                    batch.progress[pid] = [nd.get("value", 0), nd.get("max", 0)]
                    await self._emit(batch, "progress", {
                        "batch_id": batch.batch_id, "prompt_id": pid,
                        "value": nd.get("value", 0), "max": nd.get("max", 0),
                        "queue_remaining": batch.queue_remaining})
                    break
        elif mtype == "executed":
            output = data.get("output") or {}
            images = output.get("images")
            if images:
                batch.images.setdefault(pid, []).extend(images)
        elif mtype == "execution_success":
            batch.status[pid] = "done"
            batch.images.setdefault(pid, [])
            # WS 可能漏掉 executed 图，兜底从 history 补
            if not batch.images[pid]:
                try:
                    hist = await self.client.history(pid)
                    entry = hist.get(pid)
                    if entry:
                        for _nid, out in (entry.get("outputs") or {}).items():
                            for img in out.get("images", []):
                                batch.images[pid].append(img)
                except Exception:
                    pass
            await self._emit(batch, "run_done", {
                "batch_id": batch.batch_id, "prompt_id": pid,
                "images": [_image_url(i) for i in batch.images.get(pid, [])],
                "progress": _pct(batch.progress.get(pid, [0, 0]))})
            self._save_gallery(batch, pid)
            await self._check_done(batch)
        elif mtype == "execution_error":
            batch.status[pid] = "error"
            batch.error[pid] = data.get("exception_message", "生成出错")
            await self._emit(batch, "run_error", {
                "batch_id": batch.batch_id, "prompt_id": pid,
                "message": batch.error[pid], "error": batch.error[pid]})
            await self._check_done(batch)
        elif mtype == "execution_interrupted":
            batch.status[pid] = "error"
            batch.error[pid] = "已中断"
            await self._emit(batch, "run_error", {
                "batch_id": batch.batch_id, "prompt_id": pid,
                "message": "已中断", "error": "已中断"})
            await self._check_done(batch)

    async def _on_queue_status(self, data: dict) -> None:
        status = data.get("status") or {}
        qr = (status.get("exec_info") or {}).get("queue_remaining", 0)
        for b in self.batches.values():
            b.queue_remaining = qr
            if b.prompt_ids and not b.done:
                await self._emit(b, "queue", {
                    "batch_id": b.batch_id, "queue_remaining": qr})

    async def _check_done(self, batch: BatchRun) -> None:
        if batch.done or not batch.prompt_ids:
            return
        if all(batch.status.get(pid) in ("done", "error") for pid in batch.prompt_ids):
            batch.done = True
            await self._notify_done(batch)

    async def _notify_done(self, batch: BatchRun) -> None:
        await self._emit(batch, "batch_done", {
            "batch_id": batch.batch_id, "n": batch.n,
            "status": dict(batch.status), "error": dict(batch.error)})
        for h in self._done_handlers:
            try:
                await h(batch)
            except Exception:
                log.exception("batch done handler 异常")

    def on_batch_done(self, handler) -> None:
        self._done_handlers.append(handler)

    # ---------- 取消 / 查询 ----------

    async def cancel(self, batch_id: str) -> bool:
        batch = self.batches.get(batch_id)
        if not batch:
            return False
        running = [pid for pid, s in batch.status.items() if s == "running"]
        queued = [pid for pid, s in batch.status.items() if s == "queued"]
        for pid in running:
            try:
                await self.client.interrupt(pid)  # aki 定向中断：只停本批次运行中的 prompt
            except Exception:
                log.warning("定向 interrupt 失败 %s，降级全局中断", pid)
                try:
                    await self.client.interrupt()
                except Exception:
                    pass
        if queued:
            try:
                await self.client.delete_queue(queued)
            except Exception:
                log.warning("删除排队 prompt 失败")
        for pid in running + queued:
            batch.status[pid] = "error"
            batch.error[pid] = "已取消"
        batch.done = True
        await self._emit(batch, "batch_done", {
            "batch_id": batch.batch_id, "n": batch.n,
            "status": dict(batch.status), "error": dict(batch.error)})
        return True

    def _prune(self) -> None:
        """清理内存批次：过 TTL 的已完成批次删除，总量超上限按最旧优先删。"""
        now = time.time()
        for bid in list(self.batches):
            b = self.batches[bid]
            if b.done and now - b.created_at > _BATCH_TTL:
                del self.batches[bid]
        while len(self.batches) > _MAX_BATCHES:
            oldest = min(self.batches, key=lambda k: self.batches[k].created_at)
            del self.batches[oldest]

    # ---------- 惰性对账（WS 断线/漏事件兜底） ----------

    async def reconcile(self, batch_id: str) -> dict | None:
        """GET /api/generate/{id} 轮询时调用：拿 ComfyUI /history + /queue 与本地批次对账，
        补齐漏掉的终态/图片，并维护 queue_remaining。返回最新 snapshot。"""
        batch = self.batches.get(batch_id)
        if not batch:
            return None
        async with batch.lock:
            try:
                await self._reconcile(batch)
            except Exception:
                log.exception("批次对账失败 %s", batch_id)
        return self.snapshot(batch_id)

    async def _reconcile(self, batch: BatchRun) -> None:
        if batch.done or not batch.prompt_ids:
            return
        q = None
        try:
            q = await self.client.queue()
            qr = len(q.get("queue_running") or []) + len(q.get("queue_pending") or [])
        except Exception:
            qr = None
        if qr is not None and qr != batch.queue_remaining:
            batch.queue_remaining = qr
            await self._emit(batch, "queue", {"batch_id": batch.batch_id, "queue_remaining": qr})

        live = [pid for pid, s in batch.status.items() if s in ("queued", "running")]
        if not live:
            return

        active = set()
        if q:
            for item in (q.get("queue_running") or []):
                if len(item) > 1:
                    active.add(item[1])
            for item in (q.get("queue_pending") or []):
                if len(item) > 1:
                    active.add(item[1])

        for pid in list(live):
            try:
                hist = await self.client.history(pid)
            except Exception:
                continue
            entry = hist.get(pid)
            if not entry:
                # 既不在 history 也不在 queue → 已丢失（被外部取消 / ComfyUI 重启清队）
                if q is not None and pid not in active:
                    batch.status[pid] = "error"
                    msg = "已不在 ComfyUI 队列或历史中（可能被外部取消或 ComfyUI 重启）"
                    batch.error[pid] = msg
                    await self._emit(batch, "run_error", {
                        "batch_id": batch.batch_id, "prompt_id": pid, "message": msg, "error": msg})
                    await self._check_done(batch)
                continue
            st = entry.get("status") or {}
            imgs = batch.images.setdefault(pid, [])
            for _nid, out in (entry.get("outputs") or {}).items():
                for img in out.get("images", []):
                    if img not in imgs:
                        imgs.append(img)
            if st.get("status_str") == "success":
                batch.status[pid] = "done"
                await self._emit(batch, "run_done", {
                    "batch_id": batch.batch_id, "prompt_id": pid,
                    "images": [_image_url(i) for i in imgs],
                    "progress": _pct(batch.progress.get(pid, [0, 0]))})
                self._save_gallery(batch, pid)
            else:
                batch.status[pid] = "error"
                msgs = st.get("messages") or []
                emsg = "生成出错"
                if msgs:
                    last = msgs[-1]
                    if len(last) > 1:
                        emsg = str(last[1]) or emsg
                batch.error[pid] = emsg
                await self._emit(batch, "run_error", {
                    "batch_id": batch.batch_id, "prompt_id": pid,
                    "message": emsg, "error": emsg})
            await self._check_done(batch)

    def snapshot(self, batch_id: str) -> dict | None:
        batch = self.batches.get(batch_id)
        if not batch:
            return None
        return {
            "batch_id": batch.batch_id,
            "tpl_id": batch.tpl_id,
            "n": batch.n,
            "status": dict(batch.status),
            "progress": {pid: list(v) for pid, v in batch.progress.items()},
            "images": {
                pid: [{"filename": i.get("filename"), "subfolder": i.get("subfolder", ""),
                       "type": i.get("type", "output")} for i in imgs]
                for pid, imgs in batch.images.items()
            },
            "queue_remaining": batch.queue_remaining,
            "error": dict(batch.error),
            "done": batch.done,
            # 每个 prompt 实际注入后的参数（含真实种子），供结果区查看该次生成参数
            "run_params": {pid: dict(p) for pid, p in batch.run_params.items()},
        }

    # ---------- 图库 ----------

    def _save_gallery(self, batch: BatchRun, pid: str) -> None:
        if pid in batch.saved:
            return
        imgs = [{"filename": i.get("filename"), "subfolder": i.get("subfolder", ""),
                 "type": i.get("type", "output")} for i in batch.images.get(pid, [])]
        if not imgs:
            return
        batch.saved.add(pid)
        # 用该 run 实际注入后的参数（含真实种子），fallback 到提交快照
        params = {k: v for k, v in batch.run_params.get(pid, batch.params_snapshot).items()
                  if k not in _GALLERY_STRIP}
        entry = {"batch_id": batch.batch_id, "prompt_id": pid, "time": time.time(),
                 "tpl_id": batch.tpl_id, "params": params, "images": imgs}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "gallery.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._prune_gallery()

    def _prune_gallery(self) -> None:
        """图库文件超过上限时裁剪：保留尾部最新条目，防无限增长。"""
        path = DATA_DIR / "gallery.jsonl"
        try:
            if path.stat().st_size <= _GALLERY_MAX_BYTES:
                return
            lines = _tail_lines(path, None)
            keep = lines[-_GALLERY_KEEP:]
            path.write_text("\n".join(keep) + "\n" if keep else "", encoding="utf-8")
            log.info("图库超限清理: %d 行 → %d 行", len(lines), len(keep))
        except Exception:
            log.exception("图库清理失败（忽略，下次写入再试）")

    def gallery(self, limit: int = 50, offset: int = 0, tpl_id: str | None = None) -> list:
        """图库记录（倒序）。offset 用于前端加载更多游标。只读文件尾部，不做全量读。

        tpl_id 非空时按模板过滤（旧记录无 tpl_id 字段，一律不匹配）。
        """
        path = DATA_DIR / "gallery.jsonl"
        if not path.exists():
            return []
        tail = _tail_lines(path, offset + limit if tpl_id is None else None)
        out = []
        for line in reversed(tail):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if tpl_id and entry.get("tpl_id") != tpl_id:
                continue
            out.append(entry)
        return out[offset:offset + limit]
