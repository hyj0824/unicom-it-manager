# A7670E 串口统一 Actor 设计

本文定义 `SimModuleActor` 的职责、状态、调用关系和渐进迁移方案。目标是在不改变
现有通知业务语义的前提下，把 A7670E 串口访问收敛到一个进程内所有者。产品范围
与通知语义以 [`product.md`](product.md) 为准，硬件状态、超时和清理要求以
[`hardware.md`](hardware.md) 为准。

本文是设计文档，不代表相关代码已经实现。第一版仍不包含实时 AI 对话、ASR、
录音、DTMF、抢断或通话中动态生成。

## 1. 现状分析

### 1.1 串口访问点

底层 `ModemClient` 位于 `app/modem/client.py`。`open()` 在调用线程中创建
`serial.Serial`，`close()` 关闭并清空实例；上下文管理器在进入/退出时分别调用
二者。`send_command()`、`write_bytes()` 和 `read_line()` 都直接操作同一个
`serial.Serial`，本身没有锁，也不检查调用线程。`dial()` 和 `hangup()` 分别是
`ATD{phone};` 和 `AT+CHUP` 的薄封装。

生产路径的串口生命周期入口全部在 `app/services/call_worker.py`：

| 路径 | 调用点 | 打开/关闭方式 | 实际串口操作 | 生产调用线程 |
| --- | --- | --- | --- | --- |
| 短信批处理 | `CallWorker.process_pending_sms()` | 一个批次使用一次 `with ModemClient(...)`，最多发送 10 条后退出并关闭 | 把已打开的 client 交给 `_send_one_sms()` / `send_sms_text()` | `CallWorkerService` 创建的 `call-worker` 线程 |
| 语音呼叫 | `CallWorker._dial_and_play()` | 每次呼叫使用一次 `with ModemClient(...)`，呼叫收尾后关闭 | `dial()`、循环 `read_line()`、必要时 `hangup()` | `call-worker` 线程 |
| 重启恢复 | `CallWorkerService.recover_interrupted_tasks()` | 发现遗留 `dialing` / `connected` 任务时打开一次，发送一次 `AT+CHUP` 后关闭；没有遗留任务时不打开 | `hangup()` | 首次启用的 `_tick()`，即 `call-worker` 线程 |

`app/services/sms.py` 是第四个“命令与读取调用点”，但不是串口生命周期入口。
`send_sms_text()` 接收一个已经打开的 `ModemClient`，直接调用 `send_command()`、
`read_line()` 和 `write_bytes()`；它不打开、不关闭，也不提供并发保护。生产中它由
短信批处理在 `call-worker` 线程调用；单独调用该同步函数时，则在调用方线程执行。

`app/modem/parser.py` 不访问串口，只把已经读取的语音行解析成
`ParsedModemLine`。它识别 `VOICE CALL: BEGIN`、带或不带时长的
`VOICE CALL: END`、`NO CARRIER`、`BUSY`、`+CLCC`、`OK`、`ERROR`，其余非空行
归为 `urc`。

当前生产流程确实由一个 `call-worker` 线程串行执行，因此没有已知的同进程并发
访问故障。但是，“现在只有一个调用线程”是 `CallWorkerService._tick()` 的调度
结果，不是 `ModemClient` 或短信协议层保证的约束。直接调用 `CallWorker` 的测试在
pytest 调用线程运行；未来新增消费者、停止处理或管理命令后，任一调用方都能再建
一个 client 并同时打开相同设备。

仓库中还有人工验收脚本 `scripts/hardware_smoke.py`，它在独立进程直接创建
`ModemClient`。该脚本不是 Web 进程内 Actor 的消费者，仍必须在 Worker 关闭时由
操作者运行；进程内单一所有权不能阻止另一个进程抢占设备。

### 1.2 语音拨号流程

当前语音流程位于 `app/services/call_worker.py`：

1. `claim_next_task()` 按 `due_at`、`created_at` 升序领取一条到期 `queued` 任务，
   把任务和通话记录置为 `dialing`，添加 `dialing` 事件；`_tick()` 随即提交，防止
   重复领取。
2. `handle_task()` 先校验号码和音频文件。缺失属于永久失败，不访问串口、不自动
   重试。
3. `_dial_and_play()` 打开一个新的 `ModemClient`，先添加 `at_command` 事件，再
   发送 `ATD{phone};`。
4. 在 `CALL_CONNECT_TIMEOUT_SECONDS` 内循环读行。每个非空行立即调用
   `parse_modem_line()`，并各自创建带 `raw_line` 的 `CallEvent`。`BEGIN` 进入接通，
   `VOICE CALL: END` / `NO CARRIER` 进入未接通分类，`BUSY` 进入可重试结果。
5. 接通后把任务和记录置为 `connected`、添加事件并提交，然后由调用线程同步执行
   `ffmpeg` 播放。播放开始、结束各有独立事件。播放期间当前实现不读取串口。
6. 播放成功后继续读取最多 10 秒，等待结束事件；未收到结束事件时主动
   `AT+CHUP`。优先采用模块上报时长，否则使用本地单调时钟估算。
7. 根据阈值分类为 `rejected`、`cancelled_or_failed`、`no_answer`、`busy`、
   `short_call`、`completed` 或 `failed`。可重试结果创建可追踪的下一次 attempt，
   延后 `due_at` 回到 `queued`；不会在本次调用中原地重拨。

`_safe_hangup()` 当前在停止信号、接通超时、播放失败和播放后等待超时时调用，先
添加 `hangup` 事件，再尝试发送 `AT+CHUP`；发送失败转为 `error` 事件而不覆盖原始
结果。

这里存在一项需要显式记录的基线差异：`docs/hardware.md` 第 2 节要求清理路径
“始终发送 `AT+CHUP`”，但当前 `BUSY`、接通前收到 `VOICE CALL: END` /
`NO CARRIER`，以及正常收到结束事件的分支会直接退出上下文，只关闭串口而不显式
发送 `AT+CHUP`。关闭串口不等于释放通话。Actor 迁移应按硬件基线统一补齐
best-effort `AT+CHUP`，并用测试明确预期；这是一项可见的缺陷修复，不应伪装成
无行为变化的重构。

### 1.3 短信流程

当前短信流程横跨 `app/services/call_worker.py` 和 `app/services/sms.py`：

1. 每轮 `_tick()` 在领取语音任务前先调用 `process_pending_sms()`。仅当
   `SMS_ENABLED=1` 时，按 `SmsNotification.id` 升序取最多 10 条 `pending`。
2. 整批只打开一次串口，每条调用 `_send_one_sms()`。因此短信之间不重复打开，
   但批次完成后关闭；随后语音呼叫会另开一个 client。
3. `send_sms_text()` 先校验号码和内容，再依次执行 `AT+CMEE=1`、`AT+CPIN?`、
   `AT+CSQ`、`AT+CMGF=1` 和字符集选择。SIM 非 READY 立即失败；RSSI 小于 10 只
   记录警告并继续。
4. ASCII 内容使用 IRA；非 ASCII 内容使用 UCS2，号码和正文都编码为 UTF-16BE
   大写十六进制。`AT+CMGS` 等待固定 5 秒的 `>`，随后原样写正文和 Ctrl-Z，最终
   最多等待 40 秒的 `+CMGS` / `+CMS ERROR`。等待过程中忽略无关行。
5. 单条成功置 `sent` 和 `sent_at`；单条协议或其他异常置 `failed` 并保留错误；
   两者都把 `attempt` 加一，并可逐条提交。串口打开或批次事务级异常会回滚并中止
   批次，尚未处理的记录保持 `pending`。短信失败不阻塞后续语音任务。

短信当前没有类似 `CallEvent` 的逐行持久化模型。其命令和响应只用于协议判断或
日志，最终结果保存在 `SmsNotification`。因此“统一事件流”首先指统一产生带时间戳
的传输事件；在没有单独 schema 设计前，不应借本次重构静默新增短信事件表。

### 1.4 重启恢复流程

`CallWorkerService` 每次启动后台线程后，把 `_recovery_done` 复位。首次实际执行
`_tick()` 时先查询遗留的 `dialing` / `connected` 任务：

1. 没有遗留任务则不打开串口。
2. 有一条或多条遗留任务时，只打开一次串口并发送一次真实 `AT+CHUP`，随后关闭。
3. 无论物理清理成功与否，每条遗留任务都被收尾为 `failed`，不补打、不自动重试。
4. 每条记录添加 `recovery` 事件；物理命令成功时添加逻辑 `hangup` 事件，失败时
   添加 `error` 事件；最后再添加 `failed` 事件。一次物理清理对应多条遗留记录时，
   各记录仍保留自己的逻辑事件。
5. 恢复结果提交后，才处理短信和领取新语音任务。

## 2. 设计目标

1. **单一所有权**：Web 进程内只有一个 `SimModuleActor` 可以创建和持有
   `ModemClient`；业务服务不能导入或实例化 `ModemClient`，也不能取得底层
   `serial.Serial`。
2. **完整事务串行**：语音从拨号到释放、短信从模式设置到 Ctrl-Z 和最终响应、
   恢复清理都不可互相穿插。线程安全建立在 Actor 邮箱和会话令牌上，不依赖调用方
   “恰好只有一个线程”。
3. **统一事件流**：所有 AT 发送、原始行接收、解析结果、状态转换、超时、清理和
   串口错误使用同一种不可变事件表示，并在读取或写入发生时盖 UTC 时间戳和单调
   序号。通话相关事件仍逐条映射成 `CallEvent`，原始接收行仍写入 `raw_line`。
4. **可测试和可注入**：Actor 依赖 client factory / transport protocol，而不是在
   测试中打开真实设备。协议测试、Actor 并发测试和 Worker 业务测试可以分别使用
   fake transport、fake actor 和临时 SQLite。
5. **保持单通道语义**：仍由一个 Worker 按既有顺序处理；短信批次先于语音，语音
   任务仍按 `due_at`、`created_at` 升序领取，同一时刻最多执行一个硬件事务。
6. **保持同步架构**：调用方继续使用同步方法，数据库和 `ffmpeg` 仍在
   `call-worker` 线程；不要求 FastAPI、SQLAlchemy 或串口改为异步 I/O。

## 3. 方案设计

### 3.1 组件与所有权

建议以后在 `app/modem/sim_module_actor.py` 新增进程内组件 `SimModuleActor`，由
`CallWorkerService` 创建、启动、停止并显式注入 `CallWorker`。不要在
`app/services/sms.py`、Web 路由或 Scheduler 中再建 Actor，也不要通过隐式模块
全局变量让测试难以替换。

Actor 使用一个专属线程和 Python 标准库的有界 `queue.Queue` 作为邮箱。同步公共
方法把请求放入邮箱，并等待对应结果；只有 Actor 线程能调用 client factory、
`open()`、`send_command()`、`write_bytes()`、`read_line()` 和 `close()`。这不是新的
业务任务队列，`CallTask` / `SmsNotification` 仍由数据库调度；邮箱只用于进程内
串口命令仲裁。

为了最大限度保持当前设备行为，第一版不在 `idle` 时长期占用串口：

- 短信会话打开一次，复用到当前最多 10 条的批次结束，再关闭。
- 语音会话在拨号前打开，跨接通、播放和结束等待保持打开，最终清理后关闭。
- 恢复会话只在存在遗留任务时打开一次，发送一次清理命令后关闭。

这样既集中所有权，也保持当前打开/关闭边界，减少长连接引入的陈旧 URC、设备
重枚举和人工 smoke test 被无谓占用等风险。将来若有实机证据支持长连接，可以在
Actor 内部优化，而不改变业务调用接口。

### 3.2 职责边界

`SimModuleActor` 负责：

- 串口 client 的唯一创建、打开、关闭和异常后的丢弃；
- AT 命令、原始字节发送和串口行读取；
- 为完整语音、短信批次或恢复操作分配会话令牌，拒绝非持有者插入命令；
- 给每次操作分配 `operation_id`，生成发送、接收、解析、超时、清理和错误事件；
- 调用 `app/modem/parser.py` 解析语音相关行，并保留未丢失的 `raw_line`；
- 执行有界等待、响应匹配、取消检查和串口级 best-effort 清理；
- 在停止时拒绝新请求；若可能仍有通话，先尝试 `AT+CHUP`，再关闭串口并退出线程。

Actor 不负责：

- 查询、领取、提交或回滚 SQLAlchemy 对象；
- 决定 `CallTask` / `CallRecord` / `SmsNotification` 的业务状态；
- 未接通分类、重试次数、重试时间或是否骚扰性重拨的判断；
- 执行 `ffmpeg`、选择音频或修改 TTS；
- 扫描入队、Worker 开关和 Web 展示。

SQLAlchemy `Session` 不跨线程。Actor 线程绝不直接调用数据库回调；每个同步阶段
返回不可变结果和事件列表，由 `call-worker` 线程按事件顺序逐条创建 `CallEvent`
并决定提交点。事件的 `occurred_at` 在 Actor 实际收发时生成，不能在数据库 flush
时重新盖章。语音的“等待接通”阶段在播放前返回，因此 `connected` 状态仍可先提交，
再执行音频播放。

### 3.3 建议接口和调用关系

公共接口应表达完整会话，而不是暴露可随意组合的 `send_command()`：

```text
CallWorkerService
  └─ owns SimModuleActor
       ├─ recovery_session().hangup()
       ├─ sms_session().send_text(phone, content, timeout)
       └─ voice_session(phone)
            ├─ wait_for_connect(timeout, cancel_event)
            ├─ wait_for_end(timeout)
            └─ hangup(reason)
```

接口名称可以在实现时调整，但必须满足以下约束：

- `sms_session()` 的令牌覆盖整个批次。`send_text()` 可以继续复用
  `app/services/sms.py` 的编码和响应匹配逻辑，但该逻辑只能通过 Actor 内部 transport
  adapter 在 Actor 线程执行，不能拿到公开的 `ModemClient`。
- `voice_session()` 的令牌从打开串口一直持有到清理关闭。`wait_for_connect()` 返回
  `connected`、`ended`、`busy`、`timeout` 或 `cancelled` 这类硬件结果及有序事件，
  业务状态分类仍由 `CallWorker` 完成。
- 收到 `connected` 后，调用方先持久化事件和主状态，再同步播放音频。Actor 暂不在
  播放期间主动读取串口，以保持当前行为；会话令牌仍阻止短信或恢复命令插入。
- 播放结束后，调用方执行 `wait_for_end(10s)`；播放失败、停止、超时或正常结束都
  进入同一个 `finally` 清理路径。
- `recovery_session()` 只发送一次物理 `AT+CHUP`。`CallWorkerService` 再把该结果
  映射到所有遗留通话记录，保持现有“一次物理命令、每条记录各自留痕”的语义。

Actor 事件建议至少包含：`operation_id`、`sequence`、`occurred_at`、`direction`
（`tx` / `rx` / `internal`）、`event_type`、`message`、`raw_line` 和可选解析字段。
发送短信正文的原始字节不得写入日志或事件；号码和敏感字段沿用现有展示与落库
规则。对语音，`CallWorker` 继续把每个事件逐条转成对应 `CallEvent`；对短信，
Actor 结果用于协议判断、测试和运行日志，仍只把最终状态、错误、attempt 和
`sent_at` 写入 `SmsNotification`。

### 3.4 状态机

Actor 状态与业务主状态分离，不加入 `CALL_STATUSES`：

| 当前状态 | 触发 | 下一状态 | 说明 |
| --- | --- | --- | --- |
| `stopped` | `start()` | `idle` | 启动专属线程；尚未打开串口 |
| `idle` | 开始语音会话 | `dialing` | 打开串口并持有语音令牌 |
| `dialing` | 收到 `VOICE CALL: BEGIN` | `connected` | 返回接通事件，令牌继续持有 |
| `dialing` | 结束、忙、超时、取消或错误 | `cleaning` | 进入统一清理，不直接跳过 |
| `connected` | 播放结束、对端结束、停止或错误 | `cleaning` | 等待结束或主动清理 |
| `idle` | 开始短信批次 | `sending_sms` | 一个令牌覆盖批次内所有短信 |
| `sending_sms` | 批次结束或异常 | `cleaning` | 短信不发送 `AT+CHUP`，但必须关闭串口 |
| `idle` | 发现遗留通话 | `recovering` | 打开串口并执行一次 `AT+CHUP` |
| `recovering` | 成功或失败 | `cleaning` | 结果返回恢复服务 |
| `cleaning` | 清理和关闭完成 | `idle` | 清除令牌和 client 引用 |
| 任意运行态 | 不可恢复的关闭/线程错误 | `faulted` | 记录错误并拒绝复用旧 client；后续请求可触发一次重新初始化 |
| `idle` / `faulted` | `stop()` | `stopped` | 拒绝新请求并退出专属线程 |

`dialing` / `connected` 时只接受同一令牌的结束等待、挂断或停止请求；
`sending_sms` 时只接受同一短信批次令牌的发送和结束请求。其他请求不能排到当前
AT 序列中间，可以返回明确的 `ActorBusy`，或在调用方设定的总 deadline 内等待
当前会话结束。第一版 Worker 本来就是串行调用，正常路径不应出现 `ActorBusy`。

### 3.5 错误、超时与清理

超时分为两层：协议阶段 deadline 由现有配置和常量决定，公共同步调用另有略长的
总 deadline，防止 Actor 线程失联导致调用方永久等待。所有等待使用单调时钟；
`read_line()` 的串口 timeout 必须小于阶段 deadline，以便及时检查停止事件。

必须保持以下结果策略：

- 语音接通等待超时仍归为 `no_answer` 且可重试；停止中断仍归为
  `cancelled_or_failed` 且可重试。
- 已接通后的播放失败仍为 `failed` 且不自动重拨；短通话和拒接仍不自动重试。
- 短信提示符仍为 5 秒、最终响应默认仍为 40 秒；协议错误使当前短信 attempt 加一
  并置 `failed`，不阻塞语音。
- 短信批次打开串口失败属于阶段级失败，尚未实际尝试的短信保持 `pending`，不误增
  attempt。
- 恢复失败仍要把遗留任务收尾为 `failed`，不补打历史任务。

语音会话的 `AT+CHUP` 归 Actor 的统一 `finally` 清理路径所有。只要已经尝试拨号，
无论收到 `BUSY`、结束 URC、正常完成、超时、停止还是异常，都 best-effort 发送
`AT+CHUP`，生成独立发送或错误事件，然后关闭串口。清理失败不能覆盖更早的主要
业务结果，但必须进入事件流。Actor `stop()` 也要设置共享取消信号，使正在等待的
请求尽快进入同一清理路径；不能只依赖 `thread.join(timeout=15)` 后遗留活跃通话。

短信会话不发送 `AT+CHUP`，因为没有拨号，但任何异常都必须关闭并丢弃当前 client，
不能把可能停留在 `CMGS` 输入态的连接复用给下一次语音。下一次操作创建新 client。

## 4. 迁移路径

每一步都应可独立合并，并在该步结束时让完整自动化测试通过：

1. **建立无生产接线的 Actor 核心。** 定义内部 transport protocol、Actor 请求/
   结果/事件和 client factory；实现专属线程、邮箱、状态与确定性 shutdown。新增
   fake transport 单测，覆盖唯一线程访问、完整会话互斥、打开/关闭、超时和异常。
   现有 Worker 暂不改调用路径。
2. **迁移重启恢复。** 由 `CallWorkerService` 显式持有并注入 Actor，先把最简单的
   单次 `AT+CHUP` 恢复改为 Actor 请求。保持“无遗留任务不打开串口”“一次物理命令”
   和每条记录的 `recovery` / `hangup|error` / `failed` 事件。
3. **迁移短信批次。** 把 `app/services/sms.py` 收窄为可在 Actor transport 上运行的
   协议逻辑，或把等价协议步骤移入 Actor；`CallWorker` 不再接触 `ModemClient`。
   保持批次最多 10 条、按 id 升序、批次复用一次打开、逐条提交、attempt 和错误
   文案。此步不改变 IRA/UCS2、CPIN、CSQ、CMEE、CMGF、CMGS 或 Ctrl-Z 细节。
4. **迁移语音会话。** 用分阶段 voice session 替换 `_dial_and_play()` 中的直接
   client 操作；`CallWorker` 继续负责数据库状态、分类、重试和音频。把 Actor 事件
   逐条映射为 `CallEvent`，并保持接通先提交再播放。此步按 `docs/hardware.md` 补齐
   所有拨号退出分支的 `AT+CHUP`，增加 `BUSY`、未接通 END、正常 END、播放失败、
   超时和停止的清理测试。
5. **封死旁路并补并发回归。** 移除业务模块对 `ModemClient` 的直接导入，只允许
   Actor 模块和独立人工脚本使用它。增加两个调用线程竞争语音/短信会话时命令不
   交错、停止期间拒绝新请求、故障后不复用旧 client 的测试，并更新硬件人工验收
   步骤。人工 smoke test 仍必须由操作者明确提供号码，代理不得执行。

迁移全过程必须保持：

- A7670E 同一时刻最多一个完整硬件事务，短信与语音绝不并发；
- Worker 先处理最多 10 条短信，再领取一条最早到期语音任务；
- 所有通话相关串口收发、URC、状态、音频、错误、清理和重试各自形成
  `CallEvent`，原始接收行不丢失且按实际读取时间排序；
- `CALL_STATUSES`、未接通分类阈值、最短有效时长和是否重试的业务判断不变；
- 重试创建下一次可追踪 attempt 并延后 `due_at`，不原地循环、不阻塞 Scheduler；
- 重启恢复不补打历史，短信失败不阻塞语音，Worker 两层启用开关继续生效；
- 普通单元测试只使用 fake serial 和 fake `ffmpeg`，不访问真实硬件。

## 5. 风险与权衡

### 5.1 串口独占与共享

选择单 Actor 独占，而不是多个 service 共享一个“带锁的 `ModemClient`”。A7670E
短信和语音共用端口，协议上下文会跨多条命令：`AT+CMGS` 的 `>` 与 Ctrl-Z 之间、
`ATD` 与结束 URC 之间都不能插入另一消费者。共享 client 即使每个方法有锁，也
无法表达这种跨方法所有权。

独占的代价是所有硬件操作都经过一个故障域，长短信或长通话会让后续请求等待。
这是物理单通道的真实限制，不应通过并发伪装。第一版保持短信批次上限 10，避免
无限短信积压长期饿死语音；不在本次设计中新增优先级或抢占。

Actor 只保证当前 Web 进程内独占。`scripts/hardware_smoke.py` 或第二个 uvicorn 进程
仍可能竞争设备；部署继续禁止多个服务实例，人工测试前继续关闭 Worker。是否增加
OS 级锁文件或 pyserial `exclusive` 参数需要单独评估兼容性，不作为本次迁移前提。

### 5.2 锁粒度和线程模型

若仅给每条 `send_command()` / `read_line()` 加互斥锁，命令响应仍可能被别的线程
读走，SMS 正文也可能插在语音 AT 序列中。正确粒度是完整会话：一个短信批次或一
次语音从打开到清理。Actor 邮箱负责线程安全，会话令牌负责跨多个同步请求保持
原子性。

专属线程增加了请求应答、停止和故障传播的复杂度，但换来可验证的线程归属，也为
未来新增调用方保留安全边界。禁止 Actor 调用数据库 callback，可避免 SQLAlchemy
Session 跨线程和 callback 反向调用 Actor 造成死锁。公共 API 保持同步，现有 Worker
无需异步改造。

### 5.3 事件与数据库一致性

Actor 读取行和数据库提交不是同一事务。进程可能在 Actor 已发送命令、但 Worker
尚未提交事件时崩溃；现有直接串口实现同样存在该窗口，Actor 不能承诺 exactly-once。
通过读取时盖章、`operation_id`、序号和恢复清理，可以保留顺序并缩小诊断歧义，
但不应据此自动重放可能已触达的操作。

短信当前缺少逐行事件表。强行把短信传输事件写入 `CallEvent` 会伪造通话归属，
新增短信事件表又会扩大 schema 和迁移范围。因此初期统一事件类型但只持久化既有
字段；若以后需要短信审计时间线，应另立数据模型设计和 Alembic 迁移。

### 5.4 与 fake-serial 测试兼容

`tests/test_call_worker.py` 和 `tests/test_sms.py` 当前通过 monkeypatch
`app.services.call_worker.ModemClient`，FakeModem 在测试调用线程运行。迁移后建议
分成三层：

- `app/services/sms.py` 的协议纯测继续使用当前 `ScriptedModem` 风格，验证命令顺序、
  IRA/UCS2、提示符、Ctrl-Z 和错误映射；类型改为最小 transport protocol 即可。
- Actor 单测向 client factory 注入 scripted fake，真实启动 Actor 线程，额外断言
  所有 fake 方法只有 Actor 线程调用、会话不交错、关闭次数和事件序号正确。
- Worker 单测注入 fake actor，专注任务状态、事件映射、重试、恢复和短信不阻塞语音，
  不依赖 Actor 内部线程时序。

现有共享行队列能验证“短信命令先于拨号”，但迁移后还需为每个 operation 标记输入
脚本，防止一个 fake 实例无意消费另一个会话的响应。所有 timeout 测试使用很短的
显式 deadline 或可控时钟，不依赖真实串口 timeout；仍不得运行真实拨号或发短信。

## 6. 明确不做的事

- 不引入 Celery、RQ、消息中间件或其他队列框架；Actor 只使用标准库进程内邮箱，
  数据库仍是业务任务和重试的事实来源。
- 不把 FastAPI、SQLAlchemy、CallWorker、pyserial 或短信协议改为 asyncio；第一版
  保持同步调用和单后台 Worker 线程。
- 不改变 A7670E 短信协议细节，包括 CMEE、CPIN、CSQ、CMGF、IRA/UCS2、CMGS、
  Ctrl-Z、5 秒提示符和 40 秒最终响应规则。
- 不改变语音主状态、分类阈值、音频播放方式、重试上限/延迟、任务领取顺序或扫描
  入队规则。
- 不新增短信事件表，不修改现有数据库 schema，也不把短信事件写入 `CallEvent`。
- 不让 Web 请求、Scheduler、数据库迁移或模块导入触发真实串口操作。
- 不把 `scripts/hardware_smoke.py` 自动接入 Actor，也不由代理执行真实硬件验收。
- 不借此引入实时对话、ASR、录音、DTMF、抢断、动态生成或多通道并行能力。
