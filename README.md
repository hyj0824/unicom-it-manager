# 中国联通 IT 运维客户信息管理系统

运行在 Rock Pi 3A 上的中国联通内网 IT 运维客户信息管理系统，包含业务台账、
网络设备、导入审核与电话回访 Demo。
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

## Hardware smoke test

```bash
uv run python scripts/hardware_smoke.py YOUR_TEST_PHONE /path/to/audio.wav
```

The script dials through the configured A7670E serial port, waits for
`VOICE CALL: BEGIN`, plays the WAV file with `aplay`, and prints the serial log.

## 话术音频

话术生成 WAV 的目录、命名、格式与覆盖策略（与 `app/audio.py` 的 docstring
保持一致）：

- 目录：`data/audio/`，备份任务把该目录整体打包进归档（`audio/`）。
- 命名：`script-{话术id}-{话术正文sha1前12位}.wav`。正文不变时重复生成命中
  同名文件（缓存），不重复调用 TTS；正文变化生成新文件，旧文件保留为历史
  缓存。
- 格式约定：8000 Hz / 16bit / mono，与测试音一致，可直接被 `aplay` 播放。
- 覆盖策略：原子写（同目录临时文件 + `os.replace`），不会出现半截 WAV。
- 状态：`tts_status` 为 `not_generated` / `generated` / `failed`，失败原因
  写入 `tts_error`；话术页提供「生成音频」重试入口（结果以页面提示反馈）和
  页面试听（`<audio>` 播放 `/audio/...`，仅限 `data/audio/` 下的 WAV，需登录、
  防路径穿越）。
- 离线默认 `TTS_PROVIDER=none` 不生成音频，此时点击「生成音频」会明确提示
  失败原因；接入云 TTS 时应作为独立 provider 实现同一接口。
