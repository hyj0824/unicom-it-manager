from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import BASE_DIR, Settings, get_settings


BACKUP_FORMAT_VERSION = 1
_ALLOWED_RESTORE_ROOTS = {"database", "imports", "audio"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _database_path(settings: Settings) -> Path:
    if not settings.database_url.startswith("sqlite:///"):
        raise RuntimeError("Backups currently support SQLite DATABASE_URL only.")
    path = Path(settings.database_url.removeprefix("sqlite:///"))
    return path if path.is_absolute() else BASE_DIR / path


def _backup_dir(settings: Settings) -> Path:
    path = Path(settings.backup_dir)
    return path if path.is_absolute() else BASE_DIR / path


@dataclass(frozen=True)
class BackupInfo:
    filename: str
    path: Path
    created_at: str
    size: int
    sha256: str
    valid: bool = True
    remote_uploaded: bool = False
    remote_error: str = ""


class BackupService:
    """Create and restore portable, integrity-checked application backups.

    The database is copied with sqlite3.Connection.backup while the source is
    open, so a concurrently written WAL database is never copied as raw bytes.
    The worker owns no SQLAlchemy session and therefore cannot interfere with
    request transactions.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status: dict[str, Any] = {
            "running": False,
            "last_success_at": "",
            "last_failure": "",
            "last_backup": "",
            "remote_last_success_at": "",
            "remote_last_error": "",
        }

    @property
    def backup_directory(self) -> Path:
        return _backup_dir(self.settings)

    def start(self) -> None:
        if not self.settings.backup_enabled:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self.backup_directory.mkdir(parents=True, exist_ok=True)
            self._stop.clear()
            self._status["running"] = True
            self._thread = threading.Thread(
                target=self._run, name="backup-worker", daemon=True
            )
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=5)
        self._status["running"] = False

    def _run(self) -> None:
        interval = max(1, self.settings.backup_interval_hours) * 3600
        # Do not run an expensive snapshot during every development startup.
        while not self._stop.wait(interval):
            try:
                self.create_backup()
            except Exception as exc:  # pragma: no cover - defensive thread guard
                self._status["last_failure"] = str(exc)[:500]

    def status(self) -> dict[str, Any]:
        result = dict(self._status)
        result["enabled"] = self.settings.backup_enabled
        result["interval_hours"] = self.settings.backup_interval_hours
        result["retention_days"] = self.settings.backup_retention_days
        result["directory"] = str(self.backup_directory)
        result["remote_configured"] = bool(
            self.settings.backup_webdav_url
            and self.settings.backup_webdav_username
            and self.settings.backup_webdav_password
        )
        result["remote_state"] = (
            "失败" if result["remote_last_error"] else
            ("已上传" if result["remote_last_success_at"] else "未配置/未上传")
        )
        return result

    def create_backup(self, upload: bool = True) -> BackupInfo:
        backup_dir = self.backup_directory
        backup_dir.mkdir(parents=True, exist_ok=True)
        source_db = _database_path(self.settings)
        if not source_db.exists():
            raise FileNotFoundError(f"Database does not exist: {source_db}")

        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        filename = f"callback-backup-v{BACKUP_FORMAT_VERSION}-{stamp}-{uuid.uuid4().hex[:10]}.zip"
        temporary_zip = backup_dir / f".{filename}.tmp"
        try:
            with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=backup_dir) as temp_name:
                snapshot = Path(temp_name) / "app.db"
                for attempt in range(max(0, self.settings.backup_max_retries) + 1):
                    try:
                        self._sqlite_snapshot(source_db, snapshot)
                        files: list[tuple[Path, str]] = [(snapshot, "database/app.db")]
                        for source_name, archive_root in (("imports", "imports"), ("audio", "audio")):
                            root = source_db.parent / source_name
                            if root.exists():
                                for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
                                    files.append((path, f"{archive_root}/{path.relative_to(root).as_posix()}"))
                        manifest_files = [
                            {"path": arcname, "size": path.stat().st_size, "sha256": _sha256(path)}
                            for path, arcname in files
                        ]
                        manifest = {
                            "format_version": BACKUP_FORMAT_VERSION,
                            "created_at": now.isoformat(),
                            "files": manifest_files,
                        }
                        with zipfile.ZipFile(temporary_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                            for path, arcname in files:
                                archive.write(path, arcname)
                            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                        break
                    except Exception:
                        if attempt >= self.settings.backup_max_retries:
                            raise
                        time.sleep(min(2**attempt, 10))
            self.validate_backup(temporary_zip)
            final_path = backup_dir / filename
            os.replace(temporary_zip, final_path)
            info = BackupInfo(filename, final_path, now.isoformat(), final_path.stat().st_size, _sha256(final_path))
            remote_error = ""
            remote_uploaded = False
            if upload and self._remote_configured:
                for attempt in range(max(0, self.settings.backup_max_retries) + 1):
                    try:
                        self._upload_webdav(final_path)
                        remote_uploaded = True
                        self._status["remote_last_success_at"] = datetime.now(timezone.utc).isoformat()
                        self._status["remote_last_error"] = ""
                        try:
                            self._prune_webdav()
                        except Exception as exc:  # noqa: BLE001 - upload remains successful
                            self._status["remote_last_error"] = f"Remote retention cleanup: {str(exc)[:450]}"
                        break
                    except Exception as exc:  # noqa: BLE001 - retry remote transport only
                        remote_error = str(exc)[:500]
                        self._status["remote_last_error"] = remote_error
                        if attempt < self.settings.backup_max_retries:
                            time.sleep(min(2**attempt, 10))
                if not remote_uploaded:
                    self._status["last_failure"] = f"WebDAV: {remote_error}"
            self._prune_local()
            self._status["last_success_at"] = now.isoformat()
            self._status["last_backup"] = filename
            if not remote_error:
                self._status["last_failure"] = ""
            return BackupInfo(
                info.filename, info.path, info.created_at, info.size, info.sha256,
                remote_uploaded=remote_uploaded, remote_error=remote_error,
            )
        except Exception as exc:
            self._status["last_failure"] = str(exc)[:500]
            temporary_zip.unlink(missing_ok=True)
            raise

    @property
    def _remote_configured(self) -> bool:
        return bool(
            self.settings.backup_webdav_url
            and self.settings.backup_webdav_username
            and self.settings.backup_webdav_password
        )

    def _upload_webdav(self, path: Path) -> None:
        base = self.settings.backup_webdav_url.rstrip("/")
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("BACKUP_WEBDAV_URL must use HTTP or HTTPS")
        url = f"{base}/{urllib.parse.quote(path.name)}"
        credentials = f"{self.settings.backup_webdav_username}:{self.settings.backup_webdav_password}".encode()
        request = urllib.request.Request(
            url,
            data=path.read_bytes(),
            method="PUT",
            headers={
                "Content-Type": "application/zip",
                "Content-Length": str(path.stat().st_size),
                "Authorization": "Basic " + base64.b64encode(credentials).decode("ascii"),
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status not in {200, 201, 204}:
                raise RuntimeError(f"WebDAV PUT returned HTTP {response.status}")

    def _webdav_headers(self) -> dict[str, str]:
        credentials = f"{self.settings.backup_webdav_username}:{self.settings.backup_webdav_password}".encode()
        return {"Authorization": "Basic " + base64.b64encode(credentials).decode("ascii")}

    def _prune_webdav(self) -> None:
        """Apply the local retention window to timestamped remote archives.

        WebDAV collection listing and deletes are best effort after a successful
        PUT. A server without PROPFIND support keeps the uploaded file and the
        cleanup error remains visible on the system monitor page.
        """

        base = self.settings.backup_webdav_url.rstrip("/") + "/"
        body = b'<?xml version="1.0"?><propfind xmlns="DAV:"><prop><getlastmodified/></prop></propfind>'
        request = urllib.request.Request(
            base,
            data=body,
            method="PROPFIND",
            headers=self._webdav_headers() | {"Depth": "1", "Content-Type": "application/xml"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
            if response.status not in {200, 207}:
                raise RuntimeError(f"WebDAV PROPFIND returned HTTP {response.status}")
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, self.settings.backup_retention_days))
        for element in ET.fromstring(payload).iter():
            if element.tag.rsplit("}", 1)[-1] != "href" or not element.text:
                continue
            name = Path(urllib.parse.unquote(urllib.parse.urlparse(element.text).path)).name
            match = re.fullmatch(r"callback-backup-v\d+-(\d{8}T\d{6}Z)-[0-9a-f]+\.zip", name)
            if not match:
                continue
            created = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            if created >= cutoff:
                continue
            delete = urllib.request.Request(
                base + urllib.parse.quote(name), method="DELETE", headers=self._webdav_headers()
            )
            with urllib.request.urlopen(delete, timeout=30) as response:
                if response.status not in {200, 202, 204, 404}:
                    raise RuntimeError(f"WebDAV DELETE returned HTTP {response.status}")

    def _sqlite_snapshot(self, source: Path, destination: Path) -> None:
        source_conn = sqlite3.connect(str(source), timeout=30)
        destination_conn = sqlite3.connect(str(destination))
        try:
            source_conn.backup(destination_conn, pages=100, sleep=0.05)
        finally:
            destination_conn.close()
            source_conn.close()

    def validate_backup(self, path: Path) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(path) as archive:
                if archive.testzip() is not None:
                    raise ValueError("Backup ZIP contains a damaged file")
                try:
                    manifest = json.loads(archive.read("manifest.json"))
                except (KeyError, json.JSONDecodeError) as exc:
                    raise ValueError("Backup manifest is missing or invalid") from exc
                if not isinstance(manifest, dict) or manifest.get("format_version") != BACKUP_FORMAT_VERSION:
                    raise ValueError("Unsupported backup format version")
                items = manifest.get("files")
                if not isinstance(items, list):
                    raise ValueError("Backup manifest file list is invalid")
                names = set(archive.namelist())
                for item in items:
                    if not isinstance(item, dict):
                        raise ValueError("Backup manifest entry is invalid")
                    name = str(item.get("path", ""))
                    if name not in names:
                        raise ValueError(f"Backup file missing: {name}")
                    data = archive.read(name)
                    if len(data) != int(item.get("size", -1)) or hashlib.sha256(data).hexdigest() != item.get("sha256"):
                        raise ValueError(f"Backup file checksum mismatch: {name}")
                return manifest
        except (zipfile.BadZipFile, zlib.error) as exc:
            raise ValueError("Backup ZIP is damaged") from exc

    def list_backups(self) -> list[BackupInfo]:
        result: list[BackupInfo] = []
        self.backup_directory.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.backup_directory.glob("callback-backup-v*.zip"), reverse=True):
            try:
                manifest = self.validate_backup(path)
                created = str(manifest.get("created_at", ""))
                valid = True
            except Exception:
                created, valid = "", False
            result.append(BackupInfo(path.name, path, created, path.stat().st_size, _sha256(path), valid=valid))
        return result

    def restore_backup(self, backup: Path, target: Path) -> Path:
        self.validate_backup(backup)
        target = target.resolve()
        target.mkdir(parents=True, exist_ok=True)
        final_data = target / "data"
        if final_data.exists():
            raise FileExistsError(f"Restore target already exists: {final_data}")
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=target) as temp_name:
            staged_data = Path(temp_name) / "data"
            with zipfile.ZipFile(backup) as archive:
                for member in archive.infolist():
                    if member.filename == "manifest.json":
                        continue
                    relative = PurePosixPath(member.filename)
                    if not relative.parts or relative.parts[0] not in _ALLOWED_RESTORE_ROOTS or relative.is_absolute() or ".." in relative.parts:
                        raise ValueError(f"Unsafe backup path: {member.filename}")
                    if member.is_dir():
                        continue
                    if relative.parts[0] == "database":
                        output = staged_data / Path(*relative.parts[1:])
                    else:
                        output = staged_data / relative.parts[0] / Path(*relative.parts[1:])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, output.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
            restored_db = staged_data / "app.db"
            with sqlite3.connect(restored_db) as connection:
                if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError("Restored database failed SQLite integrity_check")
            os.replace(staged_data, final_data)
        return target / "data"

    def _prune_local(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(0, self.settings.backup_retention_days))
        for info in self.list_backups():
            try:
                created = datetime.fromisoformat(info.created_at)
            except ValueError:
                created = datetime.fromtimestamp(info.path.stat().st_mtime, timezone.utc)
            if created < cutoff:
                info.path.unlink(missing_ok=True)


backup_service = BackupService()


__all__ = ["BackupInfo", "BackupService", "backup_service"]
