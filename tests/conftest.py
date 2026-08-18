"""pytest 全局环境：在任何 app 模块被导入前提供安全的测试配置。

测试环境使用专用密钥和内存数据库，避免读取开发机真实 `.env` 或触碰
`data/app.db`。使用 `setdefault`，不覆盖外部显式设置的变量。
"""

from __future__ import annotations

import os

os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-for-pytest")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TTS_PROVIDER", "none")
os.environ.setdefault("DEFAULT_TIMEZONE", "Asia/Shanghai")
