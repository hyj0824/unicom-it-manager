# 中国联通 IT 运维支撑助手（内网运营支撑）

运行在 Rock Pi 3A 上的中国联通内网运营支撑系统，包含业务台账、网络设备、
导入审核、权限管理，以及面向运维工作人员的**电话通知**能力：

- **协议到期维系**：每日固定时段（默认 09:00）扫描协议到期前 N 天（默认
  提前 14 天）的业务，电话通知客户经理办理续签维系；续签/退网通过系统
  提交变更申请走审核链路。
- **退网设备回收**：退网业务下未回收设备每日提醒网络维护责任人回收，
  回收完成同样走变更申请与审核。
- **审核卡单提醒**：长时间未审核的变更申请会再次通知审核人员。

通话对象是**运维工作人员**（客户经理、网络维护责任人、审核人员），不是
客户本人；短信同步通知列为后续能力。通知任务由每日扫描自动生成，管理员
不再手动设置客户回访计划。

The original callback details remain in `docs/callback-demo-plan.md`; the
expanded product baselines are in:

- `docs/data-model-baseline.md`
- `docs/permission-workflow-baseline.md`
- `docs/ui-baseline.md`
- `docs/migration-plan.md`

## Run

```bash
uv sync
cp .env.example .env
# edit ADMIN_PASSWORD and SESSION_SECRET before starting
uv run alembic upgrade head
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The web app refuses to start when `ADMIN_PASSWORD` is blank or left as
`change-me`, and when the SQLite schema is not at the Alembic head revision.
Schema changes are made through `alembic` migrations; the app never mutates
the schema itself.

## 部署（Rock Pi 3A）

完整的 Rock Pi 3A 部署手册（串口权限、声卡选择、`uv sync`、`.env` 强密码
说明、`alembic upgrade head`、systemd 安装启用、验证步骤）见
[`docs/deploy-rockpi.md`](docs/deploy-rockpi.md)。

systemd 服务示例位于 [`scripts/systemd/`](scripts/systemd/)：

```bash
sudo cp scripts/systemd/callback-demo-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now callback-demo-web
journalctl -u callback-demo-web -f   # 查看日志
```

说明：Web 与调度器共用一个 uvicorn 进程；外呼 Worker 是同一进程内的线程，
由 `.env` 的 `CALL_WORKER_ENABLED` 硬开关（默认 `0`）控制是否启动，管理页
运行时开关再控制其实际工作状态。不要给 uvicorn 加 `--workers N`，也不要
启动第二个服务实例（SQLite 单写者约束）。

## 备份与灾备

- 本地备份：Web 进程内定时线程按 `BACKUP_INTERVAL_HOURS`（默认 24）执行，
  SQLite backup API 生成一致性快照，与 `data/imports/`、`data/audio/` 一起
  打包为 `callback-backup-v1-<时间戳>-<随机>.zip`（含 manifest 与 SHA-256
  校验），按 `BACKUP_RETENTION_DAYS` 保留本地副本。
- 远端备份：配置 `BACKUP_WEBDAV_URL` / `BACKUP_WEBDAV_USERNAME` /
  `BACKUP_WEBDAV_PASSWORD`（仅环境变量，不落库）后自动上传，远端旧包按
  同样保留期清理（依赖服务支持 PROPFIND/DELETE）。
- 管理入口：系统管理 → 备份管理（/admin/backups），可手动触发、下载本地
  备份、查看历史；操作记录审计日志。详细配置见 `.env.example` 注释。

## Hardware smoke test

```bash
uv run python scripts/hardware_smoke.py YOUR_TEST_PHONE /path/to/audio.wav
```

The script dials through the configured A7670E serial port, waits for
`VOICE CALL: BEGIN`, plays the audio file with the system `ffmpeg`
(`-f alsa` output), and prints the serial log.

## 话术音频

话术音频的目录、命名、格式与覆盖策略（与 `app/audio.py` 的 docstring
保持一致）：

- 目录：`data/audio/`，备份任务把该目录整体打包进归档（`audio/`）。
- 命名：`script-{话术id}-{话术正文sha1前12位}{扩展名}`。正文不变时重复生成命中
  同名文件（缓存），不重复调用 TTS；正文变化生成新文件，旧文件保留为历史
  缓存。扩展名由 Provider 决定（`.wav` / `.mp3`）。
- 格式：不做强制转码，由 TTS Provider 决定；`TTS_PROVIDER=none` 的测试音仍为
  8kHz/16bit/mono WAV。播放调用系统 `ffmpeg`（`apt install ffmpeg`，Debian
  bookworm 自带 5.1）直接输出到 ALSA 设备（`-f alsa <AUDIO_DEVICE>`），
  支持 WAV/MP3 等格式，`-f alsa` 输出为各版本标配，设备参数直接生效。
- 覆盖策略：原子写（同目录临时文件 + `os.replace`），不会出现半截文件。
- 状态：`tts_status` 为 `not_generated` / `generated` / `failed`，失败原因
  写入 `tts_error`；话术页提供「生成音频」重试入口（结果以页面提示反馈）和
  页面试听（`<audio>` 播放 `/audio/...`，仅限 `data/audio/` 下的 WAV/MP3，
  需登录、防路径穿越）。
- `TTS_PROVIDER=edge` 使用 Microsoft Edge 在线 TTS（免费、无需 API key，
  输出 24kHz mono MP3；需要能访问微软服务的网络），发音人由 `TTS_VOICE`
  配置（默认 `zh-CN-XiaoxiaoNeural`）。
- 离线默认 `TTS_PROVIDER=none` 不生成音频，此时点击「生成音频」会明确提示
  失败原因；接入其他云 TTS 时应作为独立 provider 实现同一接口。
