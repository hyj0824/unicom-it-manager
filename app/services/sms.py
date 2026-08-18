from __future__ import annotations

"""A7670E 短信发送封装（真机验证流程）。

已人工验证的发送流程（文本模式 + IRA 字符集，ASCII 内容）：

    AT+CMGF=1                → OK（文本模式）
    AT+CSCS="IRA"            → OK（ASCII 字符集）
    AT+CMGS="<phone>"        → "> " 输入提示符
    写入 content + Ctrl+Z(0x1A) → "+CMGS: <mr>"（成功）/ "+CMS ERROR: <code>"（失败）

非 ASCII 内容自动切换 UCS2：``AT+CSCS="UCS2"``，号码与内容均按 UCS2
十六进制编码（27.005 文本模式约定）。发送前执行 ``AT+CPIN?``（非 READY
拒绝发送）与 ``AT+CSQ``（RSSI < 10 记录弱信号警告但继续尝试）。

单通道约束：短信与语音共用同一个串口，由调用方（CallWorker）保证串行，
本模块不做并发控制。
"""

import logging
import re
import time

from ..modem.client import ModemClient

logger = logging.getLogger(__name__)

# 等待 AT+CMGS 输入提示符（>）的时长（秒）。
PROMPT_TIMEOUT_S = 5.0
# 弱信号警告阈值（+CSQ 的 RSSI 分量，0-31；真机验证值为 24）。
WEAK_SIGNAL_RSSI = 10

# +CMS ERROR 常见错误码的可读文案（27.007 定义）。
_CMS_ERROR_TEXT = {
    300: "ME 内部故障",
    301: "短信服务被占用",
    302: "当前操作不允许",
    304: "无效的 PDU 模式参数",
    305: "无效的文本模式参数",
    310: "未检测到 SIM 卡",
    311: "需要 SIM PIN 码",
    313: "SIM 卡故障",
    316: "SIM 卡被 PUK 锁定",
    320: "内存故障",
    321: "无效的内存索引",
    322: "短信存储已满",
    330: "短信中心地址未知",
    331: "无网络服务",
    332: "网络超时",
    340: "缺少 +CNMA 确认",
    500: "未知错误",
}


class SmsError(Exception):
    """短信发送失败；message 面向运营人员，可直接落库展示。"""


def is_ascii(text: str) -> bool:
    """所有字符都是 ASCII（ord < 128）时可走 IRA 文本模式。"""

    return all(ord(char) < 128 for char in text)


def encode_ucs2_hex(text: str) -> str:
    """UTF-16BE 编码后转大写十六进制字符串（文本模式 UCS2 的报文格式）。"""

    return text.encode("utf-16-be").hex().upper()


def _cms_error_message(line: str) -> str:
    match = re.search(r"\+CMS ERROR:\s*(\d+)", line, re.IGNORECASE)
    if match:
        code = int(match.group(1))
        detail = _CMS_ERROR_TEXT.get(code, "")
        if detail:
            return f"短信中心拒绝发送（+CMS ERROR: {code} {detail}）"
        return f"短信中心拒绝发送（+CMS ERROR: {code}）"
    return f"短信中心拒绝发送：{line.strip()}"


def _expect(
    modem: ModemClient,
    command: str,
    ok_texts: tuple[str, ...],
    error_texts: tuple[str, ...] = ("ERROR",),
    timeout_s: float = 5.0,
    timeout_message: str | None = None,
) -> str:
    """发送 AT 命令并等待期望行之一；返回命中的行，超时/错误抛 SmsError。"""

    modem.send_command(command)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        line = modem.read_line()
        if not line:
            continue
        upper = line.upper()
        for text in error_texts:
            if text in upper:
                raise SmsError(f"AT 命令 {command} 失败：{line}")
        for text in ok_texts:
            if text in upper:
                return line
    raise SmsError(timeout_message or f"AT 命令 {command} 超时（{timeout_s:.0f}s）未收到响应")


def _check_sim_ready(modem: ModemClient) -> None:
    """AT+CPIN?：非 READY 拒绝发送（避免无 SIM/PIN 锁定下白等超时）。"""

    response = _expect(modem, "AT+CPIN?", ok_texts=("+CPIN:",))
    if "READY" not in response.upper():
        raise SmsError(f"SIM 卡未就绪：{response}（无法发送短信）")


def _check_signal(modem: ModemClient) -> None:
    """AT+CSQ：RSSI < 10 记录弱信号警告，但不阻断发送（真机验证值为 24）。"""

    response = _expect(modem, "AT+CSQ", ok_texts=("+CSQ:",))
    match = re.search(r"\+CSQ:\s*(\d+)", response, re.IGNORECASE)
    if not match:
        logger.warning("无法解析信号强度响应：%s", response)
        return
    rssi = int(match.group(1))
    if rssi < WEAK_SIGNAL_RSSI:
        logger.warning("信号较弱（+CSQ: %d），仍继续尝试发送", rssi)


def send_sms_text(
    serial: ModemClient, phone: str, content: str, timeout_s: float = 40.0
) -> None:
    """按真机验证流程发送一条短信；成功返回，任何失败抛 SmsError。

    ``serial`` 为已打开的 ModemClient（复用其 send_command/read_line，另需
    ``write_bytes`` 原样写入报文）。``timeout_s`` 只约束等待最终
    ``+CMGS``/``+CMS ERROR`` 响应；输入提示符等待固定 5 秒。
    """

    phone = (phone or "").strip()
    if not phone:
        raise SmsError("收件人号码为空，无法发送短信。")
    if not content:
        raise SmsError("短信内容为空，无法发送短信。")

    use_ucs2 = not is_ascii(content)
    if use_ucs2:
        # UCS2 文本模式下号码与内容都按 UCS2 十六进制编码。
        charset, da, payload = "UCS2", encode_ucs2_hex(phone), encode_ucs2_hex(content)
        logger.info("短信内容含非 ASCII 字符，切换 UCS2 编码发送（%d 字符）", len(content))
    else:
        charset, da, payload = "IRA", phone, content

    # 先开启详细错误码（+CMS ERROR 需要 AT+CMEE=1），再检查 SIM 与信号。
    _expect(serial, "AT+CMEE=1", ("OK",))
    _check_sim_ready(serial)
    _check_signal(serial)
    _expect(serial, "AT+CMGF=1", ("OK",))
    _expect(serial, f'AT+CSCS="{charset}"', ("OK",))
    _expect(
        serial,
        f'AT+CMGS="{da}"',
        (">",),
        timeout_s=PROMPT_TIMEOUT_S,
        timeout_message=f"AT+CMGS 等待输入提示符 > 超时（{PROMPT_TIMEOUT_S:.0f}s）",
    )
    # 文本内容 + Ctrl+Z(0x1A) 结束；不带回车，避免把 CR 写进报文。
    serial.write_bytes(payload.encode("ascii") + b"\x1a")

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        line = serial.read_line()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("+CMGS:"):
            logger.info("短信发送成功：%s（号码 %s***）", line.strip(), phone[:3])
            return
        if "+CMS ERROR" in upper:
            raise SmsError(_cms_error_message(line))
        if upper == "ERROR":
            raise SmsError(f"发送失败，模块返回：{line.strip()}")
    raise SmsError(f"发送超时（{timeout_s:.0f}s）未收到 +CMGS 响应")
