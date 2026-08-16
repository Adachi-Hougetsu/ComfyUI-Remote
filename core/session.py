"""每模板当前参数（内存缓存 + data/session_<tpl>.json 持久化，刷新后恢复）。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

from config import DATA_DIR


class SessionStore:
    def __init__(self, data_dir: Path = DATA_DIR):
        self._data_dir = data_dir
        self._cache: dict[str, dict] = {}
        self._lock = threading.RLock()

    def _path(self, tpl_id: str) -> Path:
        return self._data_dir / f"session_{tpl_id}.json"

    def get(self, tpl_id: str, defaults: dict | None = None) -> dict:
        with self._lock:
            if tpl_id in self._cache:
                return self._cache[tpl_id]
            p = self._path(tpl_id)
            data = None
            if p.exists():
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    # 损坏容错：备份损坏文件，按默认值重建，不让启动/读页崩溃
                    try:
                        os.replace(p, p.with_suffix(".corrupt"))
                    except OSError:
                        pass
                    data = None
            if data is None:
                data = dict(defaults or {})
            self._cache[tpl_id] = data
            return data

    def save(self, tpl_id: str, data: dict) -> None:
        with self._lock:
            self._cache[tpl_id] = dict(data)
            self._data_dir.mkdir(parents=True, exist_ok=True)
            p = self._path(tpl_id)
            # 原子写盘：先写同目录临时文件再 os.replace，避免崩溃留半截文件
            fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=str(self._data_dir))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, p)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    def delete(self, tpl_id: str) -> None:
        with self._lock:
            self._cache.pop(tpl_id, None)
            p = self._path(tpl_id)
            if p.exists():
                p.unlink()
