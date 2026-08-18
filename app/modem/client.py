from __future__ import annotations

import time
from collections.abc import Iterator


class ModemClient:
    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial = None

    def open(self) -> None:
        import serial

        self._serial = serial.Serial(
            self.port,
            self.baudrate,
            timeout=self.timeout,
            write_timeout=self.timeout,
        )

    def close(self) -> None:
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    def __enter__(self) -> "ModemClient":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def send_command(self, command: str) -> None:
        if self._serial is None:
            raise RuntimeError("Modem serial port is not open")
        data = (command.rstrip("\r\n") + "\r").encode()
        self._serial.write(data)
        self._serial.flush()

    def write_bytes(self, data: bytes) -> None:
        """原样写入字节（不追加回车），用于短信报文 + Ctrl+Z 等场景。"""
        if self._serial is None:
            raise RuntimeError("Modem serial port is not open")
        self._serial.write(data)
        self._serial.flush()

    def dial(self, phone: str) -> None:
        self.send_command(f"ATD{phone};")

    def hangup(self) -> None:
        self.send_command("AT+CHUP")

    def read_line(self) -> str:
        if self._serial is None:
            raise RuntimeError("Modem serial port is not open")
        return self._serial.readline().decode(errors="replace").strip()

    def iter_lines(self, seconds: int) -> Iterator[str]:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            line = self.read_line()
            if line:
                yield line
