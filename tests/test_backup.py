from __future__ import annotations

import http.server
import os
import shutil
import socket
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

import pytest

from app.config import Settings
from app.services.backups import BackupService


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = dict(
        admin_password="test",
        session_secret="test-secret",
        database_url=f"sqlite:///{tmp_path / 'data' / 'app.db'}",
        modem_port="/dev/null",
        modem_baud=115200,
        audio_device="plughw:0,0",
        call_connect_timeout_seconds=90,
        rejected_end_seconds=20,
        min_connected_seconds=8,
        retry_delay_seconds=300,
        max_call_attempts=2,
        tts_provider="none",
        tts_api_key="",
        tts_voice="",
        default_timezone="Asia/Shanghai",
        call_worker_enabled=False,
        worker_poll_seconds=5,
        backup_enabled=True,
        backup_interval_hours=24,
        backup_retention_days=30,
        backup_dir=str(tmp_path / "backups"),
        backup_max_retries=0,
        backup_webdav_url="",
        backup_webdav_username="",
        backup_webdav_password="",
    )
    values.update(overrides)
    return Settings(**values)


def seed_source(settings: Settings) -> Path:
    db_path = Path(settings.database_url.removeprefix("sqlite:///"))
    db_path.parent.mkdir(parents=True)
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        for table in ("customers", "business_services", "callback_plans", "call_records"):
            connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, value TEXT)")
            connection.execute(f"INSERT INTO {table} (value) VALUES (?)", (table,))
    (db_path.parent / "imports").mkdir()
    (db_path.parent / "imports" / "original.xlsx").write_bytes(b"source-ledger")
    (db_path.parent / "audio").mkdir()
    (db_path.parent / "audio" / "script.wav").write_bytes(b"RIFF-test-wave")
    return db_path


def test_full_backup_restore_drill(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed_source(settings)
    service = BackupService(settings)

    info = service.create_backup(upload=False)
    manifest = service.validate_backup(info.path)
    names = {item["path"] for item in manifest["files"]}
    assert names == {"database/app.db", "imports/original.xlsx", "audio/script.wav"}

    restored = service.restore_backup(info.path, tmp_path / "restored")
    with sqlite3.connect(restored / "app.db") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        for table in ("customers", "business_services", "callback_plans", "call_records"):
            assert connection.execute(f"SELECT value FROM {table}").fetchone() == (table,)
    assert (restored / "imports" / "original.xlsx").read_bytes() == b"source-ledger"
    assert (restored / "audio" / "script.wav").read_bytes() == b"RIFF-test-wave"


def test_backup_is_consistent_during_database_writes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    db_path = seed_source(settings)
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE writes (id INTEGER PRIMARY KEY, value TEXT)")

    stop = threading.Event()

    def writer() -> None:
        with sqlite3.connect(db_path, timeout=10) as connection:
            while not stop.is_set():
                connection.execute("INSERT INTO writes (value) VALUES ('active')")
                connection.commit()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        time.sleep(0.02)
        info = BackupService(settings).create_backup(upload=False)
    finally:
        stop.set()
        thread.join(timeout=5)

    restored = BackupService(settings).restore_backup(info.path, tmp_path / "concurrent-restore")
    with sqlite3.connect(restored / "app.db") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT count(*) FROM writes").fetchone()[0] >= 0


class PutHandler(http.server.BaseHTTPRequestHandler):
    uploaded: dict[str, bytes] = {}

    def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers["Content-Length"])
        self.uploaded[self.path] = self.rfile.read(length)
        self.send_response(201)
        self.end_headers()

    def log_message(self, _format: str, *args) -> None:
        return


def test_webdav_put_uploads_integrity_checked_zip(tmp_path: Path) -> None:
    PutHandler.uploaded = {}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PutHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = make_settings(
            tmp_path,
            backup_webdav_url=f"http://127.0.0.1:{server.server_port}/remote/backups",
            backup_webdav_username="backup-user",
            backup_webdav_password="app-password",
        )
        seed_source(settings)
        info = BackupService(settings).create_backup()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert info.remote_uploaded is True
    assert list(PutHandler.uploaded) == [f"/remote/backups/{info.filename}"]
    uploaded = tmp_path / "uploaded.zip"
    uploaded.write_bytes(next(iter(PutHandler.uploaded.values())))
    BackupService(settings).validate_backup(uploaded)


def test_webdav_unavailable_keeps_local_backup(tmp_path: Path) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    settings = make_settings(
        tmp_path,
        backup_webdav_url=f"http://127.0.0.1:{port}/unavailable",
        backup_webdav_username="user",
        backup_webdav_password="password",
    )
    seed_source(settings)
    service = BackupService(settings)
    info = service.create_backup()
    assert info.path.exists()
    assert info.remote_uploaded is False
    assert info.remote_error
    assert service.status()["remote_last_error"]


def test_webdav_retry_count_is_honored(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(
        tmp_path,
        backup_max_retries=2,
        backup_webdav_url="https://backup.invalid/target",
        backup_webdav_username="user",
        backup_webdav_password="password",
    )
    seed_source(settings)
    attempts = 0

    def upload(_self: BackupService, _path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("temporary network failure")

    monkeypatch.setattr(BackupService, "_upload_webdav", upload)
    monkeypatch.setattr(BackupService, "_prune_webdav", lambda _self: None)
    info = BackupService(settings).create_backup()
    assert info.remote_uploaded is True
    assert attempts == 3


def test_disk_full_failure_removes_partial_archive(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    seed_source(settings)

    def no_space(*_args, **_kwargs):
        raise OSError(28, os.strerror(28))

    monkeypatch.setattr(zipfile.ZipFile, "write", no_space)
    with pytest.raises(OSError, match="No space left"):
        BackupService(settings).create_backup(upload=False)
    assert not list((tmp_path / "backups").glob("*.zip"))
    assert not list((tmp_path / "backups").glob("*.tmp"))


def test_corrupted_backup_is_rejected(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    seed_source(settings)
    service = BackupService(settings)
    info = service.create_backup(upload=False)
    data = bytearray(info.path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    info.path.write_bytes(data)
    with pytest.raises((ValueError, zipfile.BadZipFile)):
        service.validate_backup(info.path)


def test_interrupted_restore_leaves_no_partial_target(tmp_path: Path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    seed_source(settings)
    service = BackupService(settings)
    info = service.create_backup(upload=False)

    def interrupted(*_args, **_kwargs):
        raise OSError("restore interrupted")

    monkeypatch.setattr(shutil, "copyfileobj", interrupted)
    target = tmp_path / "interrupted"
    with pytest.raises(OSError, match="restore interrupted"):
        service.restore_backup(info.path, target)
    assert not (target / "data").exists()
