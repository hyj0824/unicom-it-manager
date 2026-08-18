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
