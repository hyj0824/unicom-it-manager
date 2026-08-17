# AGENTS.md

本文件是 AI 编码代理在本仓库中的工作约定。开始修改前，先阅读本文件、
`README.md`、`TODO.md`，以及与任务相关的代码。产品范围和状态定义以
`docs/callback-demo-plan.md` 为准；若代码与设计文档不一致，应先指出差异，
不要静默扩大范围。

## 项目目标

这是运行在 Rock Pi 3A 上的最小电话回访管理 Demo：FastAPI 后台按计划生成
单通道外呼任务，A7670E 负责拨号，电话接通后由本机通过 `aplay` 播放 WAV，
系统保存任务、通话记录及串口事件。

第一版明确不实现实时 AI 对话、ASR、录音、DTMF、抢断或通话中的动态生成。
不要在无明确需求时引入这些能力。

## 环境与命令

本项目统一使用 `uv` 管理 Python 环境和依赖，不维护 `requirements.txt`，也不
直接使用系统 Python 安装依赖。

```bash
uv sync
cp .env.example .env
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

在运行交互式命令或者长时间运行的监听程序时，优先使用 herdr（其次 tmux）
把程序挂在新 tab 执行，方便查看历史和交互。

提交修改前，至少运行：

```bash
uv run python -m compileall -q app scripts tests
uv run pytest -q
```

修改 SQLAlchemy 模型时，同时用 `uv run alembic revision --autogenerate` 生成
迁移脚本，人工审查后再执行 `uv run alembic upgrade head`。

只修改文档时可以不运行应用测试，但需要检查文档中的命令和路径仍然有效。

## 配置与密钥

- `.env.example` 只放安全的示例值，真实 `.env` 不得提交。
- `ADMIN_PASSWORD` 是内网后台的登录密码，不能保留为 `change-me`。
- `SESSION_SECRET` 用于签名登录会话 Cookie，不是用户输入的密码。生产或真实
  设备上应设置为不可预测的随机字符串，例如用 `openssl rand -hex 32` 生成。
- 不得在代码、测试、日志、文档示例或提交信息中写入真实密码、手机号、API
  Key 或 Session Secret。
- 数据库和运行日志位于 `data/`，属于运行时数据，不应提交。

## 代码地图

- `app/main.py`：FastAPI 生命周期、登录保护、SSR 页面与表单路由。
- `app/config.py`：环境变量读取、运行时配置和存储目录初始化。
- `app/models.py`：SQLAlchemy 数据模型和通话主状态集合。
- `alembic/`：数据库迁移脚本；初始版本含完整新 schema，种子版本预置字典、
  权限和岗位角色。
- `app/services/plans.py`：计划校验、下一次执行时间、任务入队和重启恢复。
- `app/scheduler.py`：APScheduler 定时扫描，只负责把到期计划放入队列。
- `app/services/call_worker.py`：单通道任务消费者；当前只有领取任务和前置校验
  骨架，真实拨号状态机尚未完成。
- `app/modem/client.py`：A7670E 串口命令和读取封装。
- `app/modem/parser.py`：将模块原始行解析成结构化事件。
- `app/audio.py`：本地 WAV 播放封装。
- `app/tts/`：TTS Provider 接口；当前 `none` Provider 不生成音频。
- `scripts/hardware_smoke.py`：人工执行的最小拨号和音频链路验收脚本。
- `app/templates/`、`app/static/`：Jinja2 后台页面及本地静态资源。
- `tests/`：无需真实串口、声卡或外呼即可执行的自动化测试。

## 架构与行为约束

- A7670E 只有一路通话能力。任何队列实现都必须保证同一时刻最多处理一条
  `CallTask`，排序为 `due_at`、`created_at` 升序。
- Scheduler 只生成任务，不直接拨号；Call Worker 才能访问串口和播放音频。
- Web 服务目前不会自动启动 Call Worker。这是防止开发或测试时误拨电话的
  安全边界。接入自动 Worker 前，必须增加显式配置开关并默认关闭。
- 真实硬件操作不得出现在模块导入、数据库迁移、页面请求或普通单元测试中。
- 每条串口收发、URC、状态变化、播放开始/结束和错误都应保存为对应
  `CallRecord` 的 `CallEvent`，不能只打印到控制台。
- 通话主状态使用 `app/models.py` 中的 `CALL_STATUSES`。音频播放等细节是事件，
  不新增为主状态。
- 重启后不补打历史任务：过期的 `once` 计划记为 `missed`；`cron` 只计算下一次
  未来执行时间。
- 自动重试上限和延迟来自配置。重试必须创建可追踪的尝试记录，不能原地无限
  循环或阻塞 Scheduler。
- 数据库结构由 Alembic 管理：修改模型后生成迁移脚本，部署执行
  `uv run alembic upgrade head`；应用启动只校验 schema 版本与代码一致，
  不再 `create_all` 自动变更结构（见 docs/migration-plan.md）。

## 实现风格

- 优先沿用现有 FastAPI、SQLAlchemy、Jinja2 和服务模块边界。
- 页面保持内网管理工具风格；使用本地 CSS 和少量原生 JavaScript，不引入
  React、Vue 或 CDN 依赖。
- 保持改动小而完整。不要顺手重构无关模块，也不要提前搭建第一版范围外的
  抽象层。
- 数据校验和时间计算应放在服务层并可单元测试，路由负责 HTTP 与表单转换。
- 时间在数据库中按 UTC 处理，在展示和 cron 计算时使用计划时区。
- 注释只解释不直观的硬件约束、状态转换和恢复策略。

## 测试策略

- Parser、计划计算、状态转换、重试和重启恢复应使用纯单元测试覆盖。
- 数据库相关测试使用临时 SQLite 数据库，不读写 `data/app.db`。
- 自动化测试必须 mock 串口和 `aplay`；不得拨打真实号码。
- 涉及 Web 路由时，覆盖未登录重定向、合法提交和非法输入。
- 涉及硬件的修改除自动化测试外，还要给出明确的人工验收命令和预期事件。

## 硬件安全

- `scripts/hardware_smoke.py` 会真实拨号，只能由操作者提供明确号码后人工运行。
- 代理不得自行运行硬件 smoke test、发送 `ATD` 或向真实号码拨号。
- 修改串口逻辑时保留 `AT+CHUP` 的清理路径，并处理超时、异常和进程退出。
- 不假设 `/dev/ttyUSB1` 或 `plughw:1,0` 永远正确；它们必须继续由环境变量或
  命令行参数配置。

## 完成标准

一次代码任务完成时应满足：行为符合设计范围，失败路径有处理，相关测试已
新增或更新，基础验证命令通过，且没有意外触发真实硬件。若因缺少设备无法
验证硬件，需在交付说明中明确列出未验证项和人工验证步骤。
