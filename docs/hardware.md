# A7670E 硬件与通话技术参考

本文保留硬件链路、通话状态机和通知通道的技术约束。产品范围与实现状态见
[`product.md`](product.md)；部署操作见 [`deploy-rockpi.md`](deploy-rockpi.md)。
代理不得自行运行真实拨号或硬件 smoke test。

## 1. 硬件环境与验收

- 主控为 Rock Pi 3A，实测系统为 Linux `rock-3a`、aarch64，Python 3.11.2。
- 电话模块为 SIMCom A7670E 4G 模块，使用 Raspberry Pi 40PIN GPIO 接口板。
- 串口路径必须配置化，当前常见 USB 枚举为 `/dev/ttyUSB0` 到 `/dev/ttyUSB3`，
  默认 `MODEM_PORT=/dev/ttyUSB1`，波特率默认 `MODEM_BAUD=115200`；后续可切换
  GPIO UART。
- 音频输出使用 Rock Pi 3.5mm 接口，候选声卡为 `rockchip-rk809`。电话上行音频当前先尝试 3.5mm 公对公耳麦线直连 A7670E 音频接口；直连不通时再评估 TRRS 转接、混音、衰减、隔直等硬件。

现场核对：

```bash
ls -l /dev/ttyUSB*
aplay -l
cat /proc/asound/cards
```

最小人工验收必须由操作者提供明确测试号码后执行：串口发送 `ATD` 拨号，对端接通，使用系统 `ffmpeg` 播放 WAV/MP3，对端稳定听到音频。独立脚本为：

```bash
uv run python scripts/hardware_smoke.py <测试号码> /path/to/test.wav
```

预期串口链路包含 `VOICE CALL: BEGIN`、播放进程成功、`VOICE CALL: END`、
`NO CARRIER`，并且串口干净释放。若失败，优先检查 TRRS 引脚、声卡设备、
音量和 A7670E 麦克风输入链路。Worker 开启时不得同时运行 smoke test。

## 2. A7670E 通话状态识别

采用“模块主动上报为主、应用超时兜底”。一次接通的实测日志示例：

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

未接通示例：

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

- `VOICE CALL: BEGIN` 是接通点。
- `VOICE CALL: END: xxxxxx` 是结束点，提取模块报告的通话秒数。
- `VOICE CALL: END` 表示未接通或无时长结束。
- `NO CARRIER` 表示通话释放。
- `+CLCC`、`+COLP`、`+CGEV` 作为串口事件保存，必要时辅助判断，不单独改变主状态。

清理路径始终发送 `AT+CHUP`；超时、异常和进程退出都必须尝试释放通话和串口。A7670E 指令细节以随设备提供的 AT 指令 PDF 为准。

### 未接通和短通话粗分类

配置项与默认值：

| 配置/常量 | 默认值 | 用途 |
| --- | ---: | --- |
| `REJECTED_END_SECONDS` | 20 秒 | 未出现 `BEGIN` 时，小于该时长视为疑似拒接/秒挂 |
| `NO_ANSWER_END_SECONDS` | 80 秒 | 未出现 `BEGIN` 时，达到该时长视为无人接听 |
| `CALL_CONNECT_TIMEOUT_SECONDS` | 90 秒 | 接通等待兜底，防止状态机卡死 |
| `MIN_CONNECTED_SECONDS` | 8 秒 | 接通后有效时长下限 |

未出现 `VOICE CALL: BEGIN` 时：拨号后小于 `REJECTED_END_SECONDS` 标记
`rejected` 且不自动重试；从 20 秒到 80 秒前标记 `cancelled_or_failed`，可
重试；大于等于 `NO_ANSWER_END_SECONDS` 标记 `no_answer`，可重试。模块无法
可靠区分主动拒接和无人接听，因此 20 秒是可配置的折中阈值。

出现 `BEGIN` 后，时长小于 `MIN_CONNECTED_SECONDS` 标记 `short_call`；否则在音频播放完成且正常结束时标记 `completed`。播放失败或其他不可恢复错误标记 `failed`。

## 3. 队列、重试与重启

- A7670E 只有一路通话能力，语音和短信都由同一个 Worker 串行处理，同一时刻最多一条通话。
- Worker 领取 `due_at <= now` 的最早任务，按 `due_at`、`created_at` 升序；Scheduler 只负责把到期扫描生成的任务入队，不直接拨号。
- 当前不做优先级、VIP 插队或批次暂停；调度器和 Worker 的全局运行状态由管理页控制。
- 可重试结果为 `no_answer`、`cancelled_or_failed`、`busy`、`failed`。默认重试
  1 次：`MAX_CALL_ATTEMPTS=2`（含首次），间隔 `RETRY_DELAY_SECONDS=300`
  （5 分钟）。每次重试保持可追踪的 attempt，不在原地无限循环，也不阻塞
  Scheduler。
- 重启不补打历史：过期 `once` 任务记为 `missed`，cron 只计算未来下一次；遗留的 `dialing` 任务按恢复策略收尾。
- Web 服务默认不自动启用 Worker。必须同时满足 `.env` 的 `CALL_WORKER_ENABLED=1` 和管理页运行时开关，才会访问串口。

## 4. 音频与 TTS

通话执行阶段只播放已生成的本地音频，不在拨号时或通话中实时合成。播放使用系统 `ffmpeg` 输出 ALSA：

```bash
ffmpeg -hide_banner -loglevel error -i /path/to/test.wav -f alsa plughw:1,0
```

`AUDIO_DEVICE` 原样作为 `-f alsa` 后的设备参数，编号必须以 `aplay -l` 实测为准。播放无声时检查 `alsamixer`/`amixer` 音量和静音状态，以及 3.5mm 到 A7670E 的上行链路。

TTS 通过统一 Provider 接口生成文件：当前 `TTS_PROVIDER=edge` 使用 Microsoft
Edge 在线 TTS（默认 `TTS_VOICE=zh-CN-XiaoxiaoNeural`，输出 24kHz 单声道
MP3），`none` 用于离线/测试并不生成可播放音频。Provider 决定扩展名，ffmpeg
负责运行时解码播放 WAV/MP3；不做强制转码，也不接受无音频文件的任务。页面
不提供手工上传音频兜底。未来云端或本地 Provider 也应遵守“输入话术文本、
输出本地文件”的接口，本地模型只做离线生成，不进入通话实时链路。

音频存储规范：

- 目录为 `data/audio/`，备份整体打包该目录。
- 文件名为 `script-{话术id}-{话术正文sha1前12位}{扩展名}`。正文不变时命中缓存，正文变化生成新文件，旧缓存保留。
- 同目录临时文件写入后用 `os.replace` 原子替换，不产生半截文件。
- 系统话术模板不生成音频（含占位符无法在模板层渲染）；扫描时按负责人聚合
  渲染正文后，为每条任务生成独立音频（任务级 Script），`tts_status` 在
  生成时置位、失败原因写入 `tts_error`，通话详情提供受登录保护的页面试听。

## 5. 短信发送流程

扫描配置勾选 `sms_enabled` 且环境变量 `SMS_ENABLED=1` 时，同一次扫描在
`sms_notifications` 建立 `pending` 记录，内容使用同一份渲染话术。Worker 只有
在没有进行语音任务时才发送短信，与语音共用串口，绝不并发；失败置 `failed`
并记录错误，不阻塞语音队列。`/sms` 展示最近 200 条并脱敏号码。

发送步骤：

1. 查询 `CPIN`，确认 SIM READY；查询 `CSQ`，记录信号强度。
2. `AT+CMEE=1` 开启详细错误，`AT+CMGF=1` 切换文本模式。
3. ASCII 内容用 `AT+CSCS="IRA"` 直接发送；非 ASCII 内容切到
   `AT+CSCS="UCS2"`，号码和正文都转 UTF-16BE 大写十六进制。
4. 发送 `AT+CMGS="<PHONE>"`，等待 `>` 提示符后写入正文并发送 Ctrl-Z。
5. 等待响应时忽略无关 URC；`+CMGS: <mr>` 视为成功并置 `sent`，
   `+CMS ERROR` 或超时置 `failed`，保留错误和 attempt。

## 6. 状态与事件规范

通话主状态只能使用 `app/models.py` 的 11 项 `CALL_STATUSES`：

`queued`、`dialing`、`connected`、`no_answer`、`rejected`、
`cancelled_or_failed`、`busy`、`short_call`、`failed`、`completed`、`missed`。

音频开始/结束和模块 URC 等细节不是新增主状态。每条通话相关串口收发、URC、
状态变化、拨号开始、接通、播放开始/结束、通话结束、`AT+CHUP`、错误和重试
都作为对应 `CallRecord` 的 `CallEvent` 逐条落库；原始串口行写入 `raw_line`，
不能只打印控制台。事件时间在读到串口行时盖章，以保留真实顺序。

手机号按字符串保存，拨号格式为 `ATD{phone};`，使用宽松基础校验 `^\+?[0-9]{5,20}$`，不做复杂归一化。

## 7. 相关配置

```env
MODEM_PORT=/dev/ttyUSB1
MODEM_BAUD=115200
AUDIO_DEVICE=plughw:1,0
CALL_CONNECT_TIMEOUT_SECONDS=90
REJECTED_END_SECONDS=20
MIN_CONNECTED_SECONDS=8
RETRY_DELAY_SECONDS=300
MAX_CALL_ATTEMPTS=2
SMS_ENABLED=0
CALL_WORKER_ENABLED=0
TTS_PROVIDER=none
TTS_API_KEY=
TTS_VOICE=zh-CN-XiaoxiaoNeural
DEFAULT_TIMEZONE=Asia/Shanghai
```

真实设备的串口、声卡和号码必须由操作者在人工验收时提供；自动化测试只能 mock 串口和播放程序。
