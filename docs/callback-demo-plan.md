# Rock Pi 3A 定期电话回访管理 Demo 设计记录

> 范围说明：本文最初针对“定期电话回访 Demo”编写。完整运营商运维台账系统的
> 数据、权限和页面基线以以下文档为准：
> [数据模型基线](data-model-baseline.md)、[权限与审核流程基线](permission-workflow-baseline.md)、
> [Web 交互基线](ui-baseline.md) 和 [数据库迁移方案](migration-plan.md)。
> 本文仍作为本地 A7670E 单通道外呼、通话状态和事件记录的技术参考；系统不接入云网关。

## 目标

在 Rock Pi 3A 上实现一个内网使用的定期电话回访管理 demo 系统。系统通过 A7670E 4G 电话模块拨打电话，通过 Rock Pi 本地音频输出播放预生成话术音频，并通过 Web 后台管理通讯录、话术、回访计划和通话记录。回访任务归属客户主体，但电话实际拨打给通讯录中的负责人，用于督促维系，不直接拨打客户主体。

第一版目标是跑通稳定的外呼回访管理闭环：

1. 在 Web 后台录入客户、话术和回访计划。
2. 定时调度器按计划生成外呼任务。
3. A7670E 通过 AT 指令拨打电话。
4. 电话接通后，Rock Pi 播放本地 WAV 音频。
5. 系统记录通话状态、通话时长、串口事件和错误信息。
6. Web 后台展示任务、日志和统计。

第一版不做实时 AI 对话、不做抢断、不做 ASR、不做录音、不做 DTMF 按键反馈。

## 硬件与运行环境

- 主控：Rock Pi 3A。
- 电话模块：SIMCom A7670E 4G 模块，Raspberry Pi 40PIN GPIO 接口板。
- 串口控制：第一版使用 USB 串口 `/dev/ttyUSB1`。
- 后续串口切换：串口路径必须配置化，方便后期改为 GPIO UART。
- 音频输出：Rock Pi 3.5mm 音频输出。
- 电话上行音频：计划先尝试使用 3.5mm 公对公耳麦线直连到 A7670E 音频接口。
- 音频硬件风险：如果直连不能让对端听到声音，后续再增加 TRRS 转接、混音、衰减、隔直等硬件处理。

当前机器已观察到：

- 系统：Linux rock-3a，aarch64。
- Python：3.11.2。
- A7670E USB 串口：`/dev/ttyUSB0` 到 `/dev/ttyUSB3`。
- 播放设备包括 `rockchip-rk809`，可作为 3.5mm 音频输出候选。

## 第一验收点

在开发完整 Web 和数据库前，必须先验证最小硬件链路：

1. 使用 `/dev/ttyUSB1` 发送 AT 指令拨打测试手机号。
2. 对端接通电话。
3. Rock Pi 使用系统 ffmpeg 播放本地音频文件（WAV/MP3 等）。
4. 对端手机能稳定听到音频。

如果该验收点不通过，优先排查音频接线、TRRS 引脚定义、声卡输出设备、音量和 A7670E 麦克风输入链路。

## 技术栈

- 后端语言：Python。
- Web 框架：FastAPI。
- 页面渲染：Jinja2 服务端渲染。
- 前端资源：本地 CSS + 少量原生 JavaScript，不依赖 CDN，不引入 React/Vue。
- 数据库：SQLite。
- ORM：SQLAlchemy。
- 迁移工具：使用 Alembic，生产启动不再依赖 `create_all` 自动变更结构；
  每次结构变化都有可审查、可回滚的版本。
- 调度器：APScheduler。
- 串口控制：pyserial。
- 音频播放：使用系统 ffmpeg（`-f alsa` 输出）播放本地音频。
- 配置：`.env` + `.env.example`。

## Web 范围

第一版 Web 后台包含 5 个主要区域：

1. 仪表盘
   - 今日待拨。
   - 当前是否正在拨打。
   - 成功、失败、短通话等基础统计。
   - 全局调度启用/暂停状态。

2. 通讯录
   - 姓名。
   - 负责人姓名、电话和职责。
   - 职责可选“客户”，允许直接维护客户电话。
   - 备注。

3. 话术管理
   - 话术标题。
   - 话术文本。
   - 音频生成状态。
   - 本地 WAV 文件路径。

4. 回访计划
   - 客户主体。
   - 明确选择拨打负责人。
   - 话术。
   - 触发规则。
   - 启用状态。
   - 下次执行时间。
   - 立即拨打一次。

5. 通话记录
   - 客户主体。
   - 实际拨打负责人和电话。
   - 计划。
   - 话术。
   - 主状态。
   - 拨号开始时间。
   - 接通时间。
   - 结束时间。
   - 通话时长。
   - 错误信息。
   - 事件日志详情。

界面要求：一切从简，但不能简陋。采用内网后台管理风格：侧边栏、顶部状态、表格、筛选、状态标签、详情页或详情区域。

## 登录与访问

- 服务监听 `0.0.0.0`，用于内网访问。
- 第一版做简单登录保护。
- 登录方式：单密码，不做用户名、多管理员、角色权限。
- 配置项：`ADMIN_PASSWORD`。
- 没有配置 `ADMIN_PASSWORD` 时，服务应拒绝启动或进入明确错误状态，避免误以为已有保护。
- 登录成功后使用会话 Cookie 或签名 Cookie。

## 调度设计

调度存储层只保留两类触发规则：

- `once`：一次性任务。
- `cron`：cron 表达式任务。

UI 层可以提供更友好的选项，例如每天、每周、每月、自定义 cron，但最终都转换为 `once` 或 `cron`。

第一版计划表保留最小字段：

- `trigger_type`：`once` 或 `cron`。
- `run_at`：单次任务执行时间。
- `cron_expr`：cron 表达式。
- `timezone`：默认 `Asia/Shanghai`。
- `enabled`：是否启用。
- `next_run_at`：下次执行时间，便于列表展示和调试。

暂不实现：

- 跳过周末。
- 跳过法定节假日。
- 顺延到下一个工作日。
- 工作日历。
- 复杂补偿策略。

后续如果需要节假日规则，可以使用 `holidays` 库作为触发前过滤策略，而不是自己实现日历。

## 重启恢复规则

系统重启后不补历史，只跑未来：

- 单次任务如果已经过期但未执行，标记为 `missed`，不自动补打。
- cron 任务只计算下一次未来执行时间，不补打停机期间错过的次数。

## 队列规则

- A7670E 只有一路电话能力，第一版必须单通道排队执行。
- 即使多个计划同时到期，也只能一通电话一通电话打。
- 队列排序：按 `due_at` 或 `next_run_at` 升序。
- 同一时间到期时，按创建时间排序。
- 第一版不做优先级、VIP 插队、批次暂停。
- Web 后台需要有全局调度启用/暂停开关。

## 手动拨打

需要提供“立即拨打一次”功能：

- 可从客户详情或计划详情触发。
- 生成一条一次性通话任务并进入队列。
- 不改变原 cron 计划。
- 用于测试电话、音频、话术效果。

## 重试策略

- 每个计划触发后生成通话任务。
- 如果拨号失败、忙线、无人接听等，最多自动重试 1 次。
- 重试延迟：5 分钟。
- 第二次仍失败则标记为失败，不再继续自动重试。
- Web 页面允许后续人工重新发起。

## 电话状态与事件

主状态应保持有限、清晰：

- `queued`：待拨打。
- `dialing`：拨号中。
- `connected`：已接通。
- `no_answer`：无人接听。
- `rejected`：疑似拒接或秒挂。
- `cancelled_or_failed`：未接通但中途释放或网络侧失败。
- `busy`：忙线。
- `short_call`：已接通但有效时长不足。
- `failed`：失败。
- `completed`：完成。
- `missed`：系统重启后发现已错过且不补打。

“音频开始播放”“音频播放结束”不作为主状态，而是记录为事件。

每条事件都应记录时间戳，便于分析：

- 发送 AT 指令。
- AT 响应。
- URC 主动上报。
- 拨号开始。
- 接通。
- 开始播放音频。
- 播放结束。
- 通话结束。
- 挂断。
- 错误信息。

串口原始日志按通话记录维度保存，不只打印到控制台。

## A7670E 通话状态识别

第一版采用“主动上报为主，应用超时兜底”的方式。

已观察到的接通日志示例：

```text
ATD<TEST_NUMBER>;
OK

+CGEV: NW ACT 8,10

+CLCC: 1,0,2,0,0,"<TEST_NUMBER>",129,""

+CLCC: 1,0,3,0,0,"<TEST_NUMBER>",129,""

VOICE CALL: BEGIN

+CLCC: 1,0,0,0,0,"<TEST_NUMBER>",129,""

+COLP: "<TEST_NUMBER>",129

+CGEV: NW DEACT 8,10

+CLCC: 1,0,6,0,0,"<TEST_NUMBER>",129,""

VOICE CALL: END: 000008

NO CARRIER
```

已观察到的未接通日志示例：

```text
ATD<TEST_NUMBER>;
OK

+CGEV: NW ACT 8,10

+CLCC: 1,0,2,0,0,"<TEST_NUMBER>",129,""

+CLCC: 1,0,3,0,0,"<TEST_NUMBER>",129,""

+CGEV: NW DEACT 8,10

+CLCC: 1,0,6,0,0,"<TEST_NUMBER>",129,""

VOICE CALL: END

NO CARRIER
```

解析规则：

- `VOICE CALL: BEGIN`：作为接通点。
- `VOICE CALL: END: xxxxxx`：作为结束点，并提取模块报告的通话时长。
- `VOICE CALL: END`：未接通或无时长结束。
- `NO CARRIER`：通话释放。
- `+CLCC`：记录事件日志，必要时辅助判断。

挂断指令：

- 使用 `AT+CHUP`。
- 后续以同目录下的 A7670E AT 指令 PDF 为准。

应用层接通超时：

- 默认 90 秒。
- 模块通常会自动上报超时或未接通。
- 应用层 90 秒只作为兜底，防止状态机卡死。

未接通粗分类（阈值可由配置调整）：

- 拨号后小于 `REJECTED_END_SECONDS`（默认 20 秒）结束，且没有 `VOICE CALL:
  BEGIN`：`rejected`，视为主动拒接/秒挂，不自动重试。
- `REJECTED_END_SECONDS` 到 80 秒结束，且没有 `VOICE CALL: BEGIN`：
  `cancelled_or_failed`，可自动重试。
- 大于等于 80 秒结束，且没有 `VOICE CALL: BEGIN`：`no_answer`，可自动重试。

> 说明：模块对“主动拒接”和“无人接听”都上报为响铃后释放，系统无法区分；
> 拒接阈值是折中方案（操作者授权实测后定为 20 秒）。

接通后有效性：

- 配置项 `MIN_CONNECTED_SECONDS`，默认 8 秒。
- 如果出现 `VOICE CALL: BEGIN`，但最终接通时长小于阈值，标记为 `short_call`。
- 否则音频播放完成且通话正常结束时标记为 `completed`。

## 音频与 TTS

第一版通话执行阶段只播放本地 WAV 文件：

- 不在拨号时实时 TTS。
- 不在通话过程中生成语音。
- 不做 MP3 运行时转码。
- 播放设备通过配置指定，例如 `AUDIO_DEVICE=plughw:1,0`（ffmpeg 以
  `-f alsa <设备>` 输出，各版本标配）。

音频生成策略：

- Web 后台保存话术文本。
- 创建或编辑话术时，可以通过 TTS provider 生成本地 WAV。
- 如果没有 API key、provider 未配置、接口失败或话术没有可播放 WAV，对应任务直接失败并记录错误。
- 不做手动上传音频兜底。

TTS 抽象：

- 配置 `TTS_PROVIDER`。
- 第一版先实现 `none` provider 和统一接口，不强制接入云 TTS。
- 如果后续要试用云 TTS，也应作为独立 provider 接入，不进入通话执行链路。
- 后期本地部署 TTS 模型时，新增 `local` provider。
- 无论云 TTS 还是本地 TTS，都遵守同一接口：输入话术文本，输出本地 WAV。
- 通话流程只认 WAV 文件，不关心音频来源。

后期方向：

- 内网环境最终需要考虑本地化 TTS。
- 本地 TTS 仍用于离线生成音频文件，不做通话中的实时语音合成。

## ASR、LLM 与客户反馈

第一版不做：

- 录音。
- ASR。
- 实时 AI 对话。
- 打断检测。
- LLM 总结。
- 对方语音内容识别。

第一版客户反馈结果由操作员在 Web 上人工补录，例如：

- 客户满意。
- 需要人工跟进。
- 号码无效。
- 已解决。

后续可扩展：

- 下行音频采集。
- 录音保存。
- ASR 转写。
- LLM 总结。
- 情绪或意图分析。
- 是否需要人工跟进。

建议在数据结构中预留字段或扩展表：

- `recording_path`
- `transcript_text`
- `summary_text`
- `sentiment`
- `follow_up_required`
- `ai_result_json`

## 手机号处理

- 手机号使用字符串保存。
- 不存为整数。
- 不做复杂归一化。
- 用宽松正则进行基础校验。
- 建议正则：`^\+?[0-9]{5,20}$`。
- 拨号时直接拼接：`ATD{phone};`。

## 配置项

`.env.example` 建议包含：

```env
ADMIN_PASSWORD=change-me

DATABASE_URL=sqlite:///./data/app.db

MODEM_PORT=/dev/ttyUSB1
MODEM_BAUD=115200

AUDIO_DEVICE=plughw:1,0

CALL_CONNECT_TIMEOUT_SECONDS=90
MIN_CONNECTED_SECONDS=8
RETRY_DELAY_SECONDS=300
MAX_CALL_ATTEMPTS=2

TTS_PROVIDER=none
TTS_API_KEY=
TTS_VOICE=

DEFAULT_TIMEZONE=Asia/Shanghai
```

## 最小 Demo 骨架 Plan

### 阶段 1：项目骨架

1. 创建 Python 项目结构。
2. 添加依赖声明。
3. 添加 `.env.example`。
4. 实现配置加载。
5. 创建 `data/`、`static/`、`templates/` 等目录。

建议目录：

```text
.
├── app/
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── models.py
│   ├── auth.py
│   ├── scheduler.py
│   ├── modem/
│   │   ├── client.py
│   │   └── parser.py
│   ├── audio.py
│   ├── tts/
│   │   ├── base.py
│   │   └── none.py
│   ├── services/
│   │   ├── call_worker.py
│   │   ├── plans.py
│   │   └── scripts.py
│   ├── templates/
│   └── static/
├── data/
├── docs/
├── tests/
├── .env.example
└── requirements.txt
```

### 阶段 2：数据模型

实现 SQLAlchemy 模型：

1. `Customer`
2. `Script`
3. `CallbackPlan`
4. `CallTask`
5. `CallRecord`
6. `CallEvent`
7. `AppSetting` 或简单配置表，用于保存全局调度启用/暂停状态。

### 阶段 3：登录与基础页面

1. 实现单密码登录。
2. 实现统一后台布局。
3. 实现仪表盘。
4. 实现客户 CRUD。
5. 实现话术 CRUD。
6. 实现计划 CRUD。
7. 实现通话记录列表和详情。

### 阶段 4：调度与队列

1. 集成 APScheduler。
2. 启动时加载启用中的计划。
3. 实现 `once` 和 `cron` 的下一次运行时间计算。
4. 到期时生成 `CallTask`。
5. 实现单通道 worker。
6. 实现全局调度启用/暂停开关。
7. 实现“立即拨打一次”。
8. 实现不补历史，只跑未来。

### 阶段 5：串口模块

1. 实现 pyserial 客户端。
2. 支持发送 `AT`、`ATD{phone};`、`AT+CHUP`。
3. 实现串口读循环。
4. 实现 URC 解析：
   - `VOICE CALL: BEGIN`
   - `VOICE CALL: END`
   - `VOICE CALL: END: xxxxxx`
   - `NO CARRIER`
   - `+CLCC`
5. 所有发送命令和收到的串口行都写入 `CallEvent`。

### 阶段 6：通话状态机

1. 拨号任务进入 `dialing`。
2. 收到 `VOICE CALL: BEGIN` 后进入 `connected`。
3. 接通后调用音频播放。
4. 收到结束事件后计算最终状态：
   - `completed`
   - `short_call`
   - `rejected`
   - `cancelled_or_failed`
   - `no_answer`
   - `busy`
   - `failed`
5. 实现 90 秒接通超时兜底。
6. 实现最多 1 次、延迟 5 分钟的失败重试。

### 阶段 7：音频播放

1. 用系统 ffmpeg 播放音频文件。
2. 播放设备配置化。
3. 记录音频播放开始和结束事件。
4. 如果音频文件不存在，任务直接失败。

### 阶段 8：TTS Provider 接口

1. 定义统一 TTS provider 接口。
2. 实现 `none` provider。
3. 话术没有音频时，生成状态为失败。
4. 后续再接云 TTS 或本地 TTS。

### 阶段 9：硬件链路验收脚本

在完整系统之外，建议提供一个最小测试脚本：

1. 指定手机号。
2. 通过 `/dev/ttyUSB1` 拨号。
3. 等待 `VOICE CALL: BEGIN`。
4. 播放指定 WAV。
5. 记录串口日志。
6. 结束后挂断或等待对方挂断。

这个脚本用于验证电话和音频链路，不依赖 Web 和数据库。

### 阶段 10：验收标准

最小 demo 完成标准：

1. 内网浏览器可访问 Web 后台。
2. 输入单密码后进入系统。
3. 可创建客户、话术、计划。
4. 可看到调度启用/暂停状态。
5. 可点击“立即拨打一次”。
6. 系统通过 `/dev/ttyUSB1` 拨打电话。
7. 接通后播放本地 WAV。
8. 通话结束后 Web 上能看到状态、时长和事件日志。
9. cron 计划能生成未来任务。
10. 系统重启后不补历史，只跑未来。
