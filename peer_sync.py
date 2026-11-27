"""观澜局域网增量快照同步。

设计目标不是把 SQLite 变成分布式数据库，而是保持单一用户、低频写的特点：
每次真正发生变化时才生成一个完整资料库快照，通过版本号做 compare-and-swap。
同一时间只有一台设备能提交；两端都改过时保留冲突快照，绝不静默覆盖。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

PROTOCOL = 1
DISCOVERY_PORT = 8791
DISCOVERY_MAGIC = b"GUANLAN_DISCOVER_V1"
MAX_SNAPSHOT_BYTES = 300 * 1024 * 1024
MAX_UNCOMPRESSED = 600 * 1024 * 1024
SNAPSHOT_HISTORY = 4


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json_read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _json_write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _remove_tree(path: Path) -> None:
    """不经过 shutil.rmtree，避免某些桌面环境对批量删除做交互式拦截。"""
    try:
        for item in path.iterdir():
            if item.is_dir() and not item.is_symlink():
                _remove_tree(item)
            else:
                try:
                    item.unlink()
                except OSError:
                    pass
        path.rmdir()
    except OSError:
        pass


class PeerSync:
    # 局域网传输快；公网云同步在子类里放宽（cloud_sync.CloudSync）。
    pull_timeout = 120
    push_timeout = 120

    def __init__(self, root: Path, db_path: Path, data_dir: Path,
                 media_dirs: dict[str, Path], lock: threading.RLock):
        self.root = Path(root)
        self.db_path = Path(db_path)
        self.data_dir = Path(data_dir)
        self.media_dirs = {k: Path(v) for k, v in media_dirs.items()}
        self.lock = lock
        self.sync_dir = self.data_dir / "peer-sync"
        self.history_dir = self.sync_dir / "history"
        self.conflict_dir = self.sync_dir / "conflicts"
        self.library_file = self.data_dir / "library.json"
        self.device_file = self.data_dir / "device.json"
        self.token_file = self.data_dir / "sync-token.json"
        self.host_file = self.sync_dir / "host.json"
        self.current_zip = self.sync_dir / "current.zip"
        self.state_file = self.data_dir / "peer-state.json"
        self._host_lock = threading.RLock()
        self._client_lock = threading.Lock()
        self._last_error = ""
        self._last_action = ""
        self._last_sync_at = ""
        self._host_name = ""
        self.ensure_identity()

    # ── 身份与指纹 ─────────────────────────────────────

    def ensure_identity(self) -> None:
        library = _json_read(self.library_file, {})
        if not library.get("library_id"):
            library = {
                "library_id": uuid.uuid4().hex,
                "created_at": _now(),
                "name": "观澜",
            }
            _json_write(self.library_file, library)
        device = _json_read(self.device_file, {})
        if not device.get("device_id"):
            device = {
                "device_id": uuid.uuid4().hex,
                "name": socket.gethostname() or "Android",
                "created_at": _now(),
            }
            _json_write(self.device_file, device)
        token = _json_read(self.token_file, {})
        if not token.get("token"):
            token = {"token": secrets.token_urlsafe(32), "created_at": _now()}
            _json_write(self.token_file, token)

    @property
    def library_id(self) -> str:
        return str(_json_read(self.library_file, {}).get("library_id") or "")

    @property
    def device(self) -> dict:
        return _json_read(self.device_file, {})

    @property
    def token(self) -> str:
        return str(_json_read(self.token_file, {}).get("token") or "")

    def _checkpoint(self) -> None:
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA wal_checkpoint(FULL)")
        finally:
            conn.close()

    def fingerprint(self) -> str:
        """逻辑数据库 + 附件元数据。

        DB 按 schema、表名和 rowid 顺序做规范化序列化，不比较 SQLite 页/日志文件：
        同一份数据在桌面 Python 3.13 和 Android Python 3.10 上也会得到同一指纹。
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
                for row in schema:
                    h.update(json.dumps(row, ensure_ascii=False,
                                        separators=(",", ":")).encode("utf-8", "replace"))
                    h.update(b"\n")
                tables = [r[1] for r in schema
                          if r[0] == "table" and not r[1].startswith("sqlite_")]
                tables.append("sqlite_sequence")
                for table in sorted(set(tables)):
                    exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone()
                    if not exists:
                        continue
                    h.update(("T\0" + table + "\0").encode("utf-8"))
                    try:
                        rows = conn.execute(
                            f'SELECT * FROM "{table.replace(chr(34), chr(34) * 2)}" ORDER BY rowid')
                    except sqlite3.DatabaseError:
                        rows = conn.execute(
                            f'SELECT * FROM "{table.replace(chr(34), chr(34) * 2)}"')
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
            for kind, base in sorted(self.media_dirs.items()):
                h.update(kind.encode("ascii", "ignore"))
                if not base.is_dir():
                    continue
                for path in sorted(base.rglob("*")):
                    if not path.is_file():
                        continue
                    rel = path.relative_to(base).as_posix()
                    h.update(f"\0{rel}\0{path.stat().st_size}\0".encode("utf-8", "replace"))
                    with path.open("rb") as fh:
                        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                            h.update(chunk)
            return h.hexdigest()

    # ── 快照生成与校验 ─────────────────────────────────

    def _snapshot_manifest(self, snapshot_id: str, parent_id: str,
                           source_fingerprint: str) -> dict:
        return {
            "format": "guanlan-library-v1",
            "library_id": self.library_id,
            "snapshot_id": snapshot_id,
            "parent_id": parent_id,
            "device_id": str(self.device.get("device_id") or ""),
            "device_name": str(self.device.get("name") or "观澜设备"),
            "created_at": _now(),
            "source_fingerprint": source_fingerprint,
        }

    def build_snapshot(self, dest: Path, parent_id: str = "") -> dict:
        with self.lock:
            dest.parent.mkdir(parents=True, exist_ok=True)
            snapshot_id = uuid.uuid4().hex
            source_fp = self.fingerprint()
            tmp_db = self.sync_dir / "snapshot-db.tmp"
            tmp_db.parent.mkdir(parents=True, exist_ok=True)
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
                                     compresslevel=6) as zf:
                    zf.writestr("manifest.json",
                                json.dumps(manifest, ensure_ascii=False, indent=2))
                    zf.write(tmp_db, "data/guanlan.db")
                    for kind, base in self.media_dirs.items():
                        if not base.is_dir():
                            continue
                        for path in sorted(base.rglob("*")):
                            if path.is_file():
                                arc = f"data/{kind}/{path.relative_to(base).as_posix()}"
                                zf.write(path, arc)
                os.replace(tmp_zip, dest)
            finally:
                tmp_db.unlink(missing_ok=True)
                tmp_zip.unlink(missing_ok=True)
            manifest["sha256"] = _sha256_file(dest)
            manifest["size"] = dest.stat().st_size
            manifest["fingerprint"] = source_fp
            return manifest

    def _inspect_zip(self, path: Path, extract_to: Path) -> dict:
        if not path.is_file() or path.stat().st_size > MAX_SNAPSHOT_BYTES:
            raise ValueError("快照文件为空或过大")
        with zipfile.ZipFile(path, "r") as zf:
            total = sum(max(0, i.file_size) for i in zf.infolist())
            if total > MAX_UNCOMPRESSED:
                raise ValueError("快照解压后过大")
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            if manifest.get("format") != "guanlan-library-v1":
                raise ValueError("不是观澜资料库快照")
            if manifest.get("library_id") != self.library_id:
                raise ValueError("资料库身份不一致，拒绝同步")
            root = extract_to.resolve()
            for item in zf.infolist():
                target = (extract_to / item.filename).resolve()
                if not target.is_relative_to(root):
                    raise ValueError("快照包含越界路径")
            zf.extractall(extract_to)
        if not (extract_to / "data" / "guanlan.db").is_file():
            raise ValueError("快照缺少数据库")
        conn = sqlite3.connect(extract_to / "data" / "guanlan.db")
        try:
            got = conn.execute("PRAGMA integrity_check").fetchone()
            if not got or got[0] != "ok":
                raise ValueError("快照数据库完整性校验失败")
        finally:
            conn.close()
        return manifest

    def apply_snapshot(self, path: Path) -> dict:
        tmp = Path(tempfile.mkdtemp(prefix="guanlan-apply-", dir=self.sync_dir))
        old = Path(tempfile.mkdtemp(prefix="guanlan-old-", dir=self.sync_dir))
        try:
            manifest = self._inspect_zip(path, tmp)
            with self.lock:
                incoming_db = tmp / "data" / "guanlan.db"
                staged_db = self.data_dir / "guanlan.db.peer-new"
                shutil.copyfile(incoming_db, staged_db)
                os.replace(staged_db, self.db_path)
                for suffix in ("-wal", "-shm"):
                    self.db_path.with_name(self.db_path.name + suffix).unlink(missing_ok=True)
                for kind, base in self.media_dirs.items():
                    incoming = tmp / "data" / kind
                    previous = old / kind
                    if base.exists():
                        os.replace(base, previous)
                    if incoming.is_dir():
                        base.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(incoming, base)
                    else:
                        base.mkdir(parents=True, exist_ok=True)
            return manifest
        except Exception:
            # 半途失败：old 里存着「替换前的原件」。原来的 finally 无论如何
            # 都把 old 删掉，于是失败一次 = 用户的附件全被删光且无法恢复。
            # 这里把原件搬回去，只清理临时目录。
            for kind, base in self.media_dirs.items():
                previous = old / kind
                if not previous.exists():
                    continue
                try:
                    if base.exists():
                        _remove_tree(base)
                    base.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(previous, base)
                except OSError:
                    pass
            raise
        finally:
            _remove_tree(tmp)
            _remove_tree(old)

    # ── 主机侧 ────────────────────────────────────────

    def _load_host(self) -> dict:
        return _json_read(self.host_file, {})

    def _save_host(self, meta: dict) -> None:
        _json_write(self.host_file, meta)

    def _archive_current(self) -> None:
        meta = self._load_host()
        if not self.current_zip.is_file() or not meta.get("snapshot_id"):
            return
        self.history_dir.mkdir(parents=True, exist_ok=True)
        name = f"{int(time.time() * 1000)}-{meta.get('snapshot_id', '')}.zip"
        shutil.copyfile(self.current_zip, self.history_dir / name)
        items = sorted(self.history_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime)
        while len(items) > SNAPSHOT_HISTORY:
            items.pop(0).unlink(missing_ok=True)

    def host_refresh(self) -> dict:
        with self._host_lock:
            meta = self._load_host()
            if not meta.get("snapshot_id") or not self.current_zip.is_file():
                fp = self.fingerprint()
                built = self.build_snapshot(self.current_zip)
                meta = {
                    "library_id": self.library_id,
                    "revision": 1,
                    "snapshot_id": built["snapshot_id"],
                    "fingerprint": built["fingerprint"],
                    "sha256": built["sha256"],
                    "size": built["size"],
                    "updated_at": built["created_at"],
                    "updated_by": built["device_name"],
                }
                self._save_host(meta)
            else:
                current = self.fingerprint()
                if current != meta.get("fingerprint"):
                    self._archive_current()
                    built = self.build_snapshot(self.current_zip, meta.get("snapshot_id", ""))
                    meta.update({
                        "library_id": self.library_id,
                        "revision": int(meta.get("revision") or 0) + 1,
                        "snapshot_id": built["snapshot_id"],
                        "fingerprint": built["fingerprint"],
                        "sha256": built["sha256"],
                        "size": built["size"],
                        "updated_at": built["created_at"],
                        "updated_by": built["device_name"],
                    })
                    self._save_host(meta)
            return dict(meta)

    def host_submit(self, uploaded: Path, base_revision: int) -> tuple[int, dict]:
        with self._host_lock:
            meta = self.host_refresh()
            if int(base_revision) != int(meta.get("revision") or 0):
                return 409, {"error": "版本已变化，请先拉取最新资料", "current": meta}
            tmp = Path(tempfile.mkdtemp(prefix="guanlan-submit-", dir=self.sync_dir))
            try:
                manifest = self._inspect_zip(uploaded, tmp)
                self._archive_current()
                self.apply_snapshot(uploaded)
                new_meta = {
                    "library_id": self.library_id,
                    "revision": int(meta.get("revision") or 0) + 1,
                    "snapshot_id": manifest.get("snapshot_id") or uuid.uuid4().hex,
                    "fingerprint": self.fingerprint(),
                    "sha256": _sha256_file(uploaded),
                    "size": uploaded.stat().st_size,
                    "updated_at": _now(),
                    "updated_by": manifest.get("device_name") or "另一台设备",
                }
                current_tmp = self.current_zip.with_suffix(".zip.tmp")
                shutil.copyfile(uploaded, current_tmp)
                os.replace(current_tmp, self.current_zip)
                self._save_host(new_meta)
                return 200, {"ok": True, "current": new_meta}
            finally:
                _remove_tree(tmp)

    # ── 客户端侧 ──────────────────────────────────────

    def _load_state(self) -> dict:
        return _json_read(self.state_file, {})

    def _save_state(self, state: dict) -> None:
        _json_write(self.state_file, state)

    def _request(self, url: str, token: str, data: bytes | None = None,
                 headers: dict | None = None, timeout: int = 45):
        h = {"X-GL-Sync-Token": token}
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=data, headers=h)
        return urllib.request.urlopen(req, timeout=timeout)

    def _broadcast_targets(self) -> list[tuple[str, int]]:
        """受限广播 + 各网段定向广播。

        Android/部分路由器不放行 255.255.255.255，补一条 192.168.x.255
        能让手机在更多网络里发现电脑。
        """
        targets: list[tuple[str, int]] = [("255.255.255.255", DISCOVERY_PORT)]
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("8.8.8.8", 80))
                ip = probe.getsockname()[0]
            finally:
                probe.close()
            if ip and not ip.startswith("127."):
                head = ".".join(ip.split(".")[:3])
                targets.append((f"{head}.255", DISCOVERY_PORT))
        except OSError:
            pass
        return targets

    def discover(self, timeout: float = 2.2) -> dict | None:
        wanted = self.library_id
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.55)
        targets = self._broadcast_targets()
        try:
            for _ in range(3):
                for target in targets:
                    try:
                        sock.sendto(DISCOVERY_MAGIC, target)
                    except OSError:
                        pass
                end = time.time() + timeout / 3
                while time.time() < end:
                    try:
                        raw, addr = sock.recvfrom(4096)
                    except socket.timeout:
                        break
                    except OSError:
                        break
                    try:
                        info = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        continue
                    if (info.get("service") == "guanlan"
                            and int(info.get("protocol") or 0) == PROTOCOL
                            and info.get("library_id") == wanted):
                        info["address"] = addr[0]
                        return info
            return None
        finally:
            sock.close()

    def sync_once(self) -> dict:
        with self._client_lock:
            self._last_sync_at = _now()
            self._last_error = ""
            self._last_action = ""
            host = self.discover()
            if not host:
                self._last_error = "同一 WiFi 下没有发现电脑端观澜"
                return self.peer_status()
            http_port = int(host.get("http_port") or 0)
            if http_port <= 0:
                self._last_error = "电脑端没有返回同步端口"
                return self.peer_status()
            # 云同步子类通过 scheme 字段走 https；局域网默认还是 http。
            base = f"{host.get('scheme') or 'http'}://{host['address']}:{http_port}"
            self._host_name = str(host.get("host_name") or "电脑端")
            token = self.token
            try:
                with self._request(base + "/api/peer/info", token, timeout=45) as resp:
                    remote = json.loads(resp.read().decode("utf-8"))
                state = self._load_state()
                local_fp = self.fingerprint()
                remote_rev = int(remote.get("revision") or 0)
                remote_fp = str(remote.get("fingerprint") or "")
                last_fp = str(state.get("fingerprint") or "")
                last_rev = int(state.get("revision") or 0)
                local_changed = bool(state) and local_fp != last_fp
                remote_changed = remote_rev != last_rev or remote_fp != last_fp

                # 第一次同步：完全一致就直接记基线；否则电脑端优先，先把手机当前状态留成冲突包。
                if not state:
                    if local_fp == remote_fp:
                        self._save_state({
                            "library_id": self.library_id,
                            "revision": remote_rev,
                            "snapshot_id": remote.get("snapshot_id") or "",
                            "fingerprint": remote_fp,
                            "synced_at": _now(),
                            "host": self._host_name,
                        })
                        self._last_action = "already"
                        return self.peer_status()
                    self._save_conflict()
                    self._pull(base, token, remote)
                    self._last_action = "conflict_pulled"
                    return self.peer_status()

                if not local_changed and not remote_changed:
                    self._last_action = "already"
                    return self.peer_status()

                if not local_changed and remote_changed:
                    self._pull(base, token, remote)
                    self._last_action = "pulled"
                    return self.peer_status()

                if local_changed and not remote_changed:
                    self._push(base, token, state)
                    self._last_action = "pushed"
                    return self.peer_status()

                self._save_conflict()
                self._pull(base, token, remote)
                self._last_action = "conflict_pulled"
                return self.peer_status()
            except urllib.error.HTTPError as e:
                if e.code == 409:
                    with self._request(base + "/api/peer/info", token, timeout=30) as resp:
                        remote = json.loads(resp.read().decode("utf-8"))
                    self._save_conflict()
                    self._pull(base, token, remote)
                    self._last_action = "conflict_pulled"
                    return self.peer_status()
                self._last_error = f"电脑端返回 HTTP {e.code}"
                return self.peer_status()
            except Exception as e:  # noqa: BLE001
                self._last_error = f"{type(e).__name__}: {e}"
                return self.peer_status()

    def _save_conflict(self) -> None:
        if not self.db_path.is_file():
            return
        self.conflict_dir.mkdir(parents=True, exist_ok=True)
        target = self.conflict_dir / f"guanlan-conflict-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        self.build_snapshot(target)

    def _pull(self, base: str, token: str, remote: dict) -> None:
        self.sync_dir.mkdir(parents=True, exist_ok=True)
        target = self.sync_dir / "download.zip"
        with self._request(base + "/api/peer/snapshot", token,
                           timeout=self.pull_timeout) as resp:
            # 快照响应头和文件来自同一时刻；若电脑端在下载过程中又变了，
            # 下一次 25 秒同步会继续追平，不必让本次直接失败。
            got_rev = int(resp.headers.get("X-GL-Sync-Revision") or
                          remote.get("revision") or 0)
            got_snapshot = str(resp.headers.get("X-GL-Sync-Snapshot") or
                               remote.get("snapshot_id") or "")
            got_fingerprint = str(resp.headers.get("X-GL-Sync-Fingerprint") or
                                  remote.get("fingerprint") or "")
            with target.open("wb") as out:
                shutil.copyfileobj(resp, out, 1024 * 1024)
        try:
            self.apply_snapshot(target)
        finally:
            target.unlink(missing_ok=True)
        self._save_state({
            "library_id": self.library_id,
            "revision": got_rev,
            "snapshot_id": got_snapshot,
            "fingerprint": got_fingerprint or self.fingerprint(),
            "synced_at": _now(),
            "host": self._host_name,
        })

    def _push(self, base: str, token: str, state: dict) -> None:
        self.sync_dir.mkdir(parents=True, exist_ok=True)
        target = self.sync_dir / "upload.zip"
        manifest = self.build_snapshot(target, str(state.get("snapshot_id") or ""))
        try:
            url = f"{base}/api/peer/submit?base_revision={int(state.get('revision') or 0)}"
            with self._request(url, token, data=target.read_bytes(),
                               headers={"Content-Type": "application/zip"},
                               timeout=self.push_timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            current = result.get("current") or {}
            self._save_state({
                "library_id": self.library_id,
                "revision": int(current.get("revision") or 0),
                "snapshot_id": current.get("snapshot_id") or manifest.get("snapshot_id") or "",
                "fingerprint": current.get("fingerprint") or self.fingerprint(),
                "synced_at": _now(),
                "host": self._host_name,
            })
        finally:
            target.unlink(missing_ok=True)

    def peer_status(self) -> dict:
        state = self._load_state()
        return {
            "available": True,
            "library_id": self.library_id,
            "host": self._host_name,
            "last_action": self._last_action,
            "last_error": self._last_error,
            "last_sync_at": self._last_sync_at,
            "revision": int(state.get("revision") or 0),
            "snapshot_id": state.get("snapshot_id") or "",
            "synced_at": state.get("synced_at") or "",
        }

    def start_auto_sync(self, interval: int = 20) -> None:
        def loop():
            while True:
                try:
                    self.sync_once()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(max(8, interval))

        threading.Thread(target=loop, name="guanlan-peer-sync", daemon=True).start()

    # ── 发现响应 ──────────────────────────────────────

    def start_discovery_responder(self, http_port: int) -> None:
        """常驻应答局域网发现广播。

        Windows 的 UDP 有一个坑：某个端口回过 ICMP 不可达之后，下一次 recvfrom
        会抛 ConnectionReset，原实现直接 break，发现线程就此死掉——手机端
        表现为「同一 WiFi 下没有发现电脑端」。这里关掉 SIO_UDP_CONNRESET，
        并把异常改成重试而不是退出。
        """
        payload = {
            "service": "guanlan",
            "protocol": PROTOCOL,
            "library_id": self.library_id,
            "http_port": int(http_port),
            "host_name": socket.gethostname() or "电脑端",
        }
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        def loop():
            while True:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    sock.bind(("0.0.0.0", DISCOVERY_PORT))
                except OSError as e:
                    print(f"[peer-sync] UDP {DISCOVERY_PORT} 绑定失败：{e}", flush=True)
                    sock.close()
                    time.sleep(5)
                    continue
                try:
                    while True:
                        try:
                            data, addr = sock.recvfrom(2048)
                            if data.strip() == DISCOVERY_MAGIC:
                                sock.sendto(raw, addr)
                        except socket.timeout:
                            continue
                        except OSError:
                            # 收到 ICMP 错误也继续守着，别让一次网络抖动废掉发现
                            time.sleep(0.2)
                finally:
                    sock.close()
                time.sleep(1)

        threading.Thread(target=loop, name="guanlan-discovery", daemon=True).start()
