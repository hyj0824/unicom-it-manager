"""结构化运行日志。

统一格式：`时间 级别 logger 消息`。应用内各模块用标准
`logging.getLogger(__name__)` 记录，`configure_logging()` 在 `app.main`
导入时调用一次即可（uvicorn 0.49 的默认日志配置不含 root，不会覆盖）。

根 logger 挂载 `RedactFilter`，在消息落盘前：
- 把已知密钥（SESSION_SECRET / ADMIN_PASSWORD / TTS_API_KEY 等）替换为
  `***`，避免密钥进入日志；
- 把中国大陆手机号改写为 `138****5678`，避免完整手机号进入日志。
"""

from __future__ import annotations

import logging
import re

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 中国大陆手机号：11 位、1[3-9] 开头，可选 +86 / 0086 / 空格 / 连字符前缀。
# 台账中的业务编号等纯数字串不在此模式内，避免误伤。
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[\s-]?)?1[3-9]\d{9}(?!\d)")


def mask_phone(text: str) -> str:
    """把文本中的手机号替换为 `138****5678` 形式；没有手机号时原样返回。"""

    def _replace(match: re.Match[str]) -> str:
        value = match.group(0)
        prefix = value[:-11]  # 手机号前的 +86 等前缀原样保留
        return f"{prefix}{value[-11:-8]}****{value[-4:]}"

    return _PHONE_RE.sub(_replace, text)


class RedactFilter(logging.Filter):
    """把日志消息中的已知密钥替换为 `***`，并把完整手机号打码。"""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__("redact")
        self.secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            # 参数不匹配等异常情况下退化为只处理原始 msg。
            message = record.msg if isinstance(record.msg, str) else ""
        if not isinstance(message, str):
            return True
        for secret in self.secrets:
            if secret and secret in message:
                message = message.replace(secret, "***")
        message = mask_phone(message)
        # 把最终消息回写为 record 本身，后续 handler 不再做 % 拼接。
        record.msg = message
        record.args = ()
        return True


_redact_filter: RedactFilter | None = None
_filtered_handler_ids: set[int] = set()


def configure_logging(
    level: int = logging.INFO,
    redact_secrets: tuple[str, ...] = (),
) -> None:
    """配置根 logger：时间/级别/logger 格式 + 密钥与手机号打码。

    幂等：重复调用只更新打码密钥列表，不重复添加 handler / filter。
    说明：logger 级 filter 只对直接记录到该 logger 的记录生效，子 logger
    传播到根 handler 的记录只经过 handler 级 filter，因此把 filter 同时挂到
    根 logger 及其现有 handler 上（含测试框架挂在根的 caplog handler）。
    """

    global _redact_filter
    logging.basicConfig(
        level=level,
        format=LOG_FORMAT,
        datefmt=LOG_DATE_FORMAT,
    )
    # basicConfig 在根 logger 已有 handler（例如 pytest caplog）时不生效，
    # 因此显式设置根级别，确保 INFO 及以上记录能到达 handler。
    logging.getLogger().setLevel(level)
    if _redact_filter is None:
        _redact_filter = RedactFilter()
        logging.getLogger().addFilter(_redact_filter)
    _redact_filter.secrets = tuple(redact_secrets)
    for handler in logging.getLogger().handlers:
        if id(handler) not in _filtered_handler_ids:
            handler.addFilter(_redact_filter)
            _filtered_handler_ids.add(id(handler))
