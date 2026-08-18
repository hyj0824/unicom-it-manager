"""测试会话级环境：必须在任何 app 模块导入前设置。

`app.config.get_settings()` 是 lru_cache 的，进程内第一次调用即固定配置；
本 conftest 在 pytest 收集测试模块之前执行，保证 `app.main` 及其依赖
（database engine、auth）读到的都是测试配置，而不是默认的空口令配置。

DATABASE_URL 指向临时目录，避免测试读写 `data/app.db`。
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="webui-tests-")

os.environ.setdefault("ADMIN_PASSWORD", "webui-test-password")
os.environ.setdefault("SESSION_SECRET", "webui-test-session-secret")
os.environ["DATABASE_URL"] = os.path.join(_TMP, "webui.db").replace("\\", "/")
os.environ["DATABASE_URL"] = f"sqlite:///{os.environ['DATABASE_URL']}"
os.environ["CALL_WORKER_ENABLED"] = "0"
