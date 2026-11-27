"""观澜 WorkBuddy 云端快照同步。

云端不保存可被浏览器直接读取的明文表：数据库只开放几个带资料库密钥校验的
SECURITY DEFINER RPC。客户端把整库快照切成小片上传，提交时用 revision 做 CAS；
拉取时按同一 revision 读取所有分片并校验 SHA-256。

协议仍是 PeerSync 的 guanlan-library-v1 快照：
    SQLite + PDF/图片/音频附件 + manifest.json
因此云端、电脑和手机看到的是同一份资料库，而不是只同步几行笔记。

配置存在资料目录的 cloud.json：
    {"enabled": false, "explicit_choice": false, "endpoint": "...", "key": "wbpk_...",
     "secret": "...", "interval": 20}
默认关闭；只有用户在设置页显式选择后才写 enabled=true / explicit_choice=true。
版本状态存在 cloud-state.json，与局域网同步状态互不干扰。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from math import ceil
from pathlib import Path

from peer_sync import PeerSync, _json_read, _json_write, _now, _remove_tree, _sha256_file

# Chaquopy 的 Python 不保证自动找到 Android 系统 CA；APK 内附一份 certifi 包。
import app_paths

_LOCAL_CA = app_paths.RES_DIR / "cacert.pem"
if _LOCAL_CA.is_file():
    os.environ.setdefault("SSL_CERT_FILE", str(_LOCAL_CA))

DEFAULT_CLOUD_ENDPOINT = "https://atlas-cognition-13448.app.workbuddy.host"
# publishableKey 只标识应用，不授予底层表权限；真正权限由资料库随机密钥校验。
DEFAULT_CLOUD_KEY = "wbpk_5wJMxwVyfLllRVlfD6rHRD_EhydyhsWQWKq0lA9ZBkLx2gcULYEFZyA"
DEFAULT_INTERVAL = 20
RPC_PREFIX = "/.cloud/database/rest/rpc/"
CHUNK_BYTES = 192 * 1024

_CONFIG_LOCK = threading.Lock()


def cloud_config_path(data_dir: Path) -> Path:
    return Path(data_dir) / "cloud.json"


def cloud_config_load(data_dir: Path, token_hint: str = "") -> dict:
    """读取配置、补齐随机密钥，并把旧版缺省开启一次性迁移为关闭。"""
    path = cloud_config_path(data_dir)
    with _CONFIG_LOCK:
        cfg = _json_read(path, {})
        if not isinstance(cfg, dict):
            cfg = {}
        changed = False

        def put(key: str, value) -> None:
            nonlocal changed
            if cfg.get(key) in (None, ""):
                cfg[key] = value
                changed = True

        put("enabled", False)
        put("endpoint", DEFAULT_CLOUD_ENDPOINT)
        put("key", DEFAULT_CLOUD_KEY)
        put("secret", secrets.token_urlsafe(32))
        if not bool(cfg.get("explicit_choice")) and bool(cfg.get("enabled")):
            cfg["enabled"] = False
            changed = True
        put("explicit_choice", False)
        if not str(cfg.get("token") or ""):
            cfg["token"] = token_hint
            changed = True
        try:
            interval = max(15, int(cfg.get("interval") or DEFAULT_INTERVAL))
        except (TypeError, ValueError):
            interval = DEFAULT_INTERVAL
        if cfg.get("interval") != interval:
            cfg["interval"] = interval
            changed = True
        if changed:
            _json_write(path, cfg)
        return cfg


def cloud_config_save(data_dir: Path, cfg: dict) -> None:
    with _CONFIG_LOCK:
        _json_write(cloud_config_path(data_dir), cfg)


class CloudSync(PeerSync):
    """WorkBuddy 云数据库同步客户端。"""

    pull_timeout = 600
    push_timeout = 600

    def __init__(self, root, db_path, data_dir, media_dirs, lock, config: dict):
        super().__init__(root, db_path, data_dir, media_dirs, lock)
        self.config = dict(config or {})
        if not self.config.get("secret"):
            self.config["secret"] = secrets.token_urlsafe(32)
            cloud_config_save(self.data_dir, self.config)
        # 云同步状态、下载包和冲突包与局域网同步隔离。
        self.sync_dir = self.data_dir / "cloud-sync"
        self.history_dir = self.sync_dir / "history"
        self.conflict_dir = self.sync_dir / "conflicts"
        self.state_file = self.data_dir / "cloud-state.json"
        self.current_zip = self.sync_dir / "current.zip"
        self._host_name = "云端"

    # ── 配置与 RPC ─────────────────────────────────────

    @property
    def base_url(self) -> str:
        return str(self.config.get("endpoint") or DEFAULT_CLOUD_ENDPOINT).rstrip("/")

    @property
    def publishable_key(self) -> str:
        return str(self.config.get("key") or DEFAULT_CLOUD_KEY).strip()

    @property
    def cloud_secret(self) -> str:
        return str(self.config.get("secret") or "").strip()

    # ── 只同步数据库，附件保持各设备本地 ───────────────

    def fingerprint(self) -> str:
        """云端只把结构化内容作为版本信号，附件不参与每次笔记同步。

        FTS 影子表是 SQLite 自行维护的倒排索引，不同系统自带的 SQLite/FTS
        版本会用不同的段号表示同一个索引。把影子表算进版本会造成桌面与手机
        永远“看起来都改过”，所以只比较用户表和虚拟表定义。
        """
        with self.lock:
            if not self.db_path.is_file():
                return ""
            h = hashlib.sha256()
            h.update(self.library_id.encode("ascii", "ignore"))
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                h.update(f"\0user_version={version}\0".encode("ascii"))
                schema = conn.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master "
                    "ORDER BY type,name").fetchall()
                virtuals = {
                    str(row[1]) for row in schema
                    if row[0] == "table" and str(row[3] or "").lstrip().upper()
                    .startswith("CREATE VIRTUAL TABLE")
                }
                shadows = {
                    str(row[1]) for row in schema
                    if any(str(row[1]).startswith(name + "_") for name in virtuals)
                }
                for row in schema:
                    if str(row[1]) in shadows:
                        continue
                    h.update(json.dumps(row, ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8", "replace"))
                    h.update(b"\n")
                derived = shadows | virtuals | {"sqlite_sequence"}
                tables = [
                    r[1] for r in schema
                    if r[0] == "table"
                    and not str(r[1]).startswith("sqlite_")
                    and r[1] not in derived
                ]
                for table in sorted(set(tables)):
                    h.update(("T\0" + table + "\0").encode("utf-8"))
                    escaped = table.replace(chr(34), chr(34) * 2)
                    try:
                        rows = conn.execute(f'SELECT * FROM "{escaped}" ORDER BY rowid')
                    except sqlite3.DatabaseError:
                        rows = conn.execute(f'SELECT * FROM "{escaped}"')
                    for row in rows:
                        normalized = [
                            ("__blob__:" + value.hex()) if isinstance(value, bytes) else value
                            for value in row
                        ]
                        h.update(json.dumps(normalized, ensure_ascii=False,
                                            separators=(",", ":"), default=str)
                                 .encode("utf-8", "replace"))
                        h.update(b"\n")
            finally:
                conn.close()
            return h.hexdigest()

    def build_snapshot(self, dest: Path, parent_id: str = "") -> dict:
        """生成数据库快照；附件不重复上传，避免一条笔记触发几十 MB 传输。"""
        with self.lock:
            dest.parent.mkdir(parents=True, exist_ok=True)
            snapshot_id = uuid.uuid4().hex
            source_fp = self.fingerprint()
            tmp_db = self.sync_dir / "snapshot-db.tmp"
            self.sync_dir.mkdir(parents=True, exist_ok=True)
            src = sqlite3.connect(self.db_path, timeout=30)
            out = sqlite3.connect(tmp_db)
            try:
                src.backup(out)
                out.execute("PRAGMA journal_mode=DELETE")
                out.commit()
            finally:
                src.close()
                out.close()
            manifest = self._snapshot_manifest(snapshot_id, parent_id, source_fp)
            tmp_zip = dest.with_suffix(dest.suffix + ".tmp")
            try:
                with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED,
                                     compresslevel=9) as zf:
                    zf.writestr("manifest.json",
                                json.dumps(manifest, ensure_ascii=False, indent=2))
                    zf.write(tmp_db, "data/guanlan.db")
                os.replace(tmp_zip, dest)
            finally:
                tmp_db.unlink(missing_ok=True)
                tmp_zip.unlink(missing_ok=True)
            manifest["sha256"] = _sha256_file(dest)
            manifest["size"] = dest.stat().st_size
            manifest["fingerprint"] = source_fp
            return manifest

    def apply_snapshot(self, path: Path) -> dict:
        """替换数据库，但保留本机已有附件目录。"""
        self.sync_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="guanlan-cloud-apply-", dir=self.sync_dir))
        try:
            manifest = self._inspect_zip(path, tmp)
            incoming_db = tmp / "data" / "guanlan.db"
            with self.lock:
                staged = self.data_dir / "guanlan.db.cloud-new"
                shutil.copyfile(incoming_db, staged)
                os.replace(staged, self.db_path)
                for suffix in ("-wal", "-shm"):
                    self.db_path.with_name(self.db_path.name + suffix).unlink(missing_ok=True)
            return manifest
        finally:
            _remove_tree(tmp)

    def _rpc(self, name: str, args: dict, timeout: int | None = None):
        if not self.base_url or not self.publishable_key or not self.cloud_secret:
            raise RuntimeError("云同步配置不完整")
        body = json.dumps(args, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + RPC_PREFIX + name,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-wb-webapp-access-key": self.publishable_key,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.push_timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
                message = parsed.get("message") or parsed.get("details") or parsed.get("hint")
            except (ValueError, AttributeError):
                message = raw[:300]
            raise RuntimeError(f"云端 HTTP {e.code}: {message or '请求失败'}") from e
        try:
            return json.loads(raw) if raw else None
        except ValueError as e:
            raise RuntimeError(f"云端返回不是 JSON: {raw[:200]}") from e

    def _head(self) -> dict:
        result = self._rpc(
            "guanlan_sync_head",
            {"p_library_id": self.library_id, "p_secret": self.cloud_secret},
            timeout=45,
        )
        return result if isinstance(result, dict) else {}

    def _chunk_get(self, revision: int, seq: int) -> str:
        result = self._rpc(
            "guanlan_sync_chunk_get",
            {
                "p_library_id": self.library_id,
                "p_secret": self.cloud_secret,
                "p_revision": revision,
                "p_seq": seq,
            },
            timeout=90,
        )
        if not isinstance(result, str):
            raise RuntimeError(f"云端快照分片 {seq} 缺失")
        return result

    def _chunk_put(self, upload_id: str, seq: int, data: str) -> None:
        result = self._rpc(
            "guanlan_sync_chunk_put",
            {
                "p_upload_id": upload_id,
                "p_secret": self.cloud_secret,
                "p_seq": seq,
                "p_data": data,
            },
            timeout=120,
        )
        if result is not True:
            raise RuntimeError(f"云端拒绝快照分片 {seq}")

    # ── 状态 ───────────────────────────────────────────

    def _remote_state(self, remote: dict) -> dict:
        return {
            "library_id": self.library_id,
            "revision": int(remote.get("revision") or 0),
            "fingerprint": str(remote.get("fingerprint") or ""),
            "sha256": str(remote.get("sha256") or ""),
            "chunk_count": int(remote.get("chunk_count") or 0),
            "snapshot_id": str(remote.get("snapshot_id") or ""),
            "synced_at": _now(),
            "host": self._host_name,
            "updated_at": str(remote.get("updated_at") or ""),
        }

    def _save_remote_state(self, remote: dict) -> None:
        self._save_state(self._remote_state(remote))

    def _is_remote_changed(self, remote: dict, state: dict) -> bool:
        return (
            int(remote.get("revision") or 0) != int(state.get("revision") or 0)
            or str(remote.get("fingerprint") or "") != str(state.get("fingerprint") or "")
        )

    # ── 同步决策 ───────────────────────────────────────

    def sync_once(self) -> dict:
        with self._client_lock:
            self._last_sync_at = _now()
            self._last_error = ""
            self._last_action = ""
            try:
                remote = self._head()
                state = self._load_state()
                local_fp = self.fingerprint()
                remote_exists = bool(remote.get("exists"))
                remote_rev = int(remote.get("revision") or 0)

                if not remote_exists or remote_rev <= 0:
                    action = self._push({
                        "revision": 0,
                        "snapshot_id": str((state or {}).get("snapshot_id") or ""),
                    })
                    self._last_action = "seeded"
                    return self.peer_status()

                remote_fp = str(remote.get("fingerprint") or "")
                if not state:
                    if local_fp == remote_fp:
                        self._save_remote_state(remote)
                        self._last_action = "already"
                        return self.peer_status()
                    self._save_conflict()
                    self._pull(remote)
                    self._last_action = "conflict_pulled"
                    return self.peer_status()

                local_changed = local_fp != str(state.get("fingerprint") or "")
                remote_changed = self._is_remote_changed(remote, state)

                if not local_changed and not remote_changed:
                    self._last_action = "already"
                    return self.peer_status()

                if not local_changed and remote_changed:
                    self._pull(remote)
                    self._last_action = "pulled"
                    return self.peer_status()

                if local_changed and not remote_changed:
                    self._last_action = self._push(state)
                    return self.peer_status()

                self._save_conflict()
                self._pull(remote)
                self._last_action = "conflict_pulled"
                return self.peer_status()
            except Exception as e:  # noqa: BLE001
                self._last_error = f"{type(e).__name__}: {e}"
                return self.peer_status()

    # ── 上传 ───────────────────────────────────────────

    def _push(self, state: dict) -> str:
        self.sync_dir.mkdir(parents=True, exist_ok=True)
        target = self.sync_dir / "upload.zip"
        manifest = self.build_snapshot(target, str(state.get("snapshot_id") or ""))
        try:
            raw_size = target.stat().st_size
            chunk_count = max(1, ceil(raw_size / CHUNK_BYTES))
            sha256 = manifest["sha256"]
            begin = self._rpc(
                "guanlan_sync_begin",
                {
                    "p_library_id": self.library_id,
                    "p_secret": self.cloud_secret,
                    "p_base_revision": int(state.get("revision") or 0),
                    "p_fingerprint": manifest["fingerprint"],
                    "p_sha256": sha256,
                    "p_chunk_count": chunk_count,
                    "p_device_name": str(manifest.get("device_name") or "观澜设备"),
                },
                timeout=60,
            )
            if not isinstance(begin, dict):
                raise RuntimeError("云端没有返回上传会话")
            status = str(begin.get("status") or "")
            if status in {"conflict", "same"}:
                remote = self._head()
                if status == "same":
                    self._save_remote_state(remote)
                    return "already"
                self._save_conflict()
                self._pull(remote)
                return "conflict_pulled"

            upload_id = str(begin.get("upload_id") or "")
            if status != "ok" or not upload_id:
                raise RuntimeError(f"云端拒绝上传：{status or '未知状态'}")

            with target.open("rb") as fh:
                for seq in range(chunk_count):
                    raw = fh.read(CHUNK_BYTES)
                    if not raw:
                        raise RuntimeError(f"快照分片 {seq} 为空")
                    self._chunk_put(
                        upload_id,
                        seq,
                        base64.b64encode(raw).decode("ascii"),
                    )

            committed = self._rpc(
                "guanlan_sync_commit",
                {"p_upload_id": upload_id, "p_secret": self.cloud_secret},
                timeout=180,
            )
            if not isinstance(committed, dict):
                raise RuntimeError("云端没有返回提交结果")
            if committed.get("status") == "conflict":
                remote = self._head()
                self._save_conflict()
                self._pull(remote)
                return "conflict_pulled"
            if committed.get("status") != "ok":
                raise RuntimeError(f"云端提交失败：{committed.get('status') or '未知状态'}")
            committed["snapshot_id"] = manifest.get("snapshot_id") or ""
            self._save_remote_state(committed)
            return "pushed"
        finally:
            target.unlink(missing_ok=True)

    # ── 下载 ───────────────────────────────────────────

    def _pull(self, remote: dict) -> None:
        revision = int(remote.get("revision") or 0)
        chunk_count = int(remote.get("chunk_count") or 0)
        expected_sha = str(remote.get("sha256") or "")
        if revision <= 0 or chunk_count <= 0 or not expected_sha:
            raise RuntimeError("云端快照元数据不完整")

        self.sync_dir.mkdir(parents=True, exist_ok=True)
        target = self.sync_dir / "download.zip"
        digest = hashlib.sha256()
        try:
            with target.open("wb") as out:
                for seq in range(chunk_count):
                    encoded = self._chunk_get(revision, seq)
                    try:
                        raw = base64.b64decode(encoded, validate=True)
                    except ValueError as e:
                        raise RuntimeError(f"云端快照分片 {seq} 损坏") from e
                    out.write(raw)
                    digest.update(raw)
            if digest.hexdigest() != expected_sha:
                raise RuntimeError("云端快照 SHA-256 校验失败")
            manifest = self.apply_snapshot(target)
            remote = dict(remote)
            remote["snapshot_id"] = manifest.get("snapshot_id") or remote.get("snapshot_id") or ""
            remote["fingerprint"] = remote.get("fingerprint") or manifest.get("fingerprint") or ""
            self._save_remote_state(remote)
        finally:
            target.unlink(missing_ok=True)

    # ── 状态与控制 ─────────────────────────────────────

    def cloud_status(self) -> dict:
        status = self.peer_status()
        state = self._load_state()
        status.update({
            "endpoint": self.base_url,
            "enabled": bool(self.config.get("enabled", False)),
            "explicit_choice": bool(self.config.get("explicit_choice", False)),
            "configured": bool(self.publishable_key and self.cloud_secret),
            "interval": int(self.config.get("interval") or DEFAULT_INTERVAL),
            "running": self.running(),
            "sha256": str(state.get("sha256") or ""),
            "chunk_count": int(state.get("chunk_count") or 0),
            "updated_at": str(state.get("updated_at") or ""),
        })
        return status

    def running(self) -> bool:
        return self._client_lock.locked()

    def apply_config(self, cfg: dict) -> None:
        self.config = dict(cfg)
        self._host_name = "云端"

    def reset_state(self) -> None:
        with self._client_lock:
            self.state_file.unlink(missing_ok=True)
            self._last_action = ""
            self._last_error = ""

    def start_auto_sync(self, interval: int = 20) -> None:
        """后台兜底循环：只起一条，且关掉开关时不再推云。

        基类 PeerSync.start_auto_sync 每次调用都无条件新起一个线程，
        也完全不看 enabled：设置页把云同步关掉后，旧线程会继续把整库
        快照往云端推。这里覆盖实现，并在设置保存后由 server 调用。
        """
        if getattr(self, "_auto_started", False):
            return
        self._auto_started = True

        def loop():
            while True:
                try:
                    if self.config.get("enabled", False):
                        self.sync_once()
                except Exception:  # noqa: BLE001 - 兜底线程不能因为一次失败就死
                    pass
                time.sleep(max(8, interval))

        threading.Thread(target=loop, name="guanlan-cloud-sync-auto",
                         daemon=True).start()

    def start_auto_sync_if_enabled(self):
        if not self.config.get("enabled", False):
            return None
        interval = max(15, int(self.config.get("interval") or DEFAULT_INTERVAL))
        return self.start_auto_sync(interval)
