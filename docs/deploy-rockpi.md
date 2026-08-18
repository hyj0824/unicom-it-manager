# Rock Pi 3A 部署指南

本文是 Rock Pi 3A（aarch64 / Debian 系）上的部署操作手册，覆盖串口权限、
声卡选择、`uv sync`、`.env` 配置、数据库迁移、systemd 服务与验证步骤。
产品范围与硬件约束见 `docs/callback-demo-plan.md`，数据库迁移原则见
`docs/migration-plan.md`，服务单元示例见 `scripts/systemd/`。

## 进程架构（先读这一段）

应用是**单个 uvicorn 进程**：Web 服务、Scheduler（APScheduler 15 秒扫描）和
Call Worker 线程都在这一个进程内启动（`app/main.py` 的 lifespan），**不存在
独立的 Worker 进程**。因此：

- systemd 只需要一个服务单元（`callback-demo-web.service`）。
- Worker 线程是否拨号由两层开关控制：
  1. `.env` 的 `CALL_WORKER_ENABLED=1`：硬开关，默认 `0`，确认硬件后才开启；
  2. 管理页「系统监控 → 组件状态」的运行时开关（写入数据库）。
  两层都打开时 Worker 才会领取任务拨号。
- SQLite 单写者约束（`docs/migration-plan.md`）：**不要**给 uvicorn 加
  `--workers N`，**不要**启动第二个服务实例，也不要在 Worker 运行时同时跑
  硬件 smoke test（会抢串口）。
- 备份/定期任务已实现（见 README「备份与灾备」）：随 Web 进程运行，无需新增
  服务单元，配置项见 `.env.example` 的 `BACKUP_*`。

## 1. 前置条件

```bash
# 系统信息核对（示例输出）
uname -m                 # aarch64
python3 --version        # 3.11.x（项目要求 >=3.11，Rock Pi 3A Debian 实测 3.11.2）
aplay -l                 # 声卡诊断（alsa-utils），用于核对 AUDIO_DEVICE
git --version
```

> 播放依赖系统 `ffmpeg`（`-f alsa` 输出，各版本标配）：

```bash
ffmpeg --version   # 缺失时：sudo apt install ffmpeg
```

安装 `uv`（本项目统一用 `uv` 管理 Python 环境和依赖，不使用系统 Python 直接
装包）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
uv --version
```

> 内网环境提示：首次 `uv sync` 需要访问 PyPI。离线部署可以在有网环境先
> `uv sync` 完成后把整个项目目录（含 `.venv/`）拷贝到设备；或设置
> `UV_INDEX_URL` 指向内网 PyPI 镜像。

串口与声卡硬件就绪检查（详见下文第 2、3 节）：

```bash
ls -l /dev/ttyUSB*       # A7670E USB 串口（通常枚举出 ttyUSB0~ttyUSB3）
aplay -l                 # 声卡列表，确认 rockchip-rk809（3.5mm 输出）
```

## 2. 串口权限

应用以普通用户运行（systemd 单元示例里是 `radxa`），需要对该用户开放串口
读写。两个层次：

### 2.1 加入 dialout 组（必需）

```bash
sudo usermod -aG dialout radxa
# 重新登录（或重启）后生效，验证：
groups radxa
```

不重新登录的话，当前 shell 的组列表不会更新，可以用
`newgrp dialout` 临时切换，或直接重新登录。权限不生效的典型报错是
`could not open port /dev/ttyUSB1: [Errno 13] Permission denied`。

### 2.2 udev 规则（可选，推荐）

默认情况下串口节点权限依赖 `dialout` 组即可；如需固定设备名（防止
`ttyUSB*` 编号漂移）或统一权限，写一条 udev 规则。新建
`/etc/udev/rules.d/99-a7670e.rules`：

```udev
# 示例：权限统一交给 dialout 组
KERNEL=="ttyUSB[0-9]*", MODE="0660", GROUP="dialout"

# 示例：按 USB 厂商/产品 ID 固定设备名（ID 必须按实际硬件替换！）
# SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", SYMLINK+="ttyA7670E"
```

> `10c4:ea60` 只是常见 USB 转串口芯片（CP210x）的示例值，**必须替换**为你
> 设备实际枚举的 ID；A7670E 转接板常见 CP210x 或 CH340（`1a86:7523`），以
> 实机为准。查询方法：

```bash
udevadm info -a -n /dev/ttyUSB1 | grep -E 'idVendor|idProduct'
```

保存规则后生效：

```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
ls -l /dev/ttyUSB1
```

若使用了符号链接，把 `.env` 的 `MODEM_PORT` 改为固定名
（如 `/dev/ttyA7670E`）。

## 3. 声卡选择

3.5mm 音频输出对应 rockchip-rk809 声卡。先看枚举：

```bash
aplay -l
cat /proc/asound/cards
```

示例输出（编号以实机为准）：

```text
card 1: rk809 [rockchip-rk809], device 0: rk809-hifi rk809-hifi-0
```

确认设备后用 ffmpeg 试播一个音频文件（支持 WAV/MP3 等 ffmpeg 可解码格式）：

```bash
ffmpeg -hide_banner -loglevel error -i /path/to/test.wav -f alsa plughw:1,0
```

- `plughw:1,0` 中 `1` 是 `aplay -l` 显示的 card 编号，`0` 是 device 编号；
  编号因启动顺序可能变化，务必以 `aplay -l` 实机输出为准。播放链路
  （Call Worker、smoke test）会把 `AUDIO_DEVICE` 原样传给 `-f alsa` 后的
  设备参数。
- 如果试播无声，检查音量：`alsamixer -c 1`（或
  `amixer -c 1 set Master 80%`），确认 rk809 的 Playback 通道未静音、音量
  足够；后续还要验收 3.5mm → A7670E 的上行音频链路（对端能否听到），见
  `docs/callback-demo-plan.md`「硬件与运行环境」。
- 试播确认后把 `AUDIO_DEVICE` 配置为同一值（如 `plughw:1,0`），见第 5 节。
- 如果试播无声，检查音量：`alsamixer -c 1`（或
  `amixer -c 1 set Master 80%`），确认 rk809 的 Playback 通道未静音、音量
  足够；后续还要验收 3.5mm → A7670E 的上行音频链路（对端能否听到），见
  `docs/callback-demo-plan.md`「硬件与运行环境」。
- 试播确认后把 `AUDIO_DEVICE` 配置为同一值（如 `plughw:1,0`），见第 5 节。

## 4. 获取代码与安装依赖

```bash
git clone <仓库地址> /home/radxa/callback-demo   # 路径仅为示例，必须替换为实际路径
cd /home/radxa/callback-demo
uv sync
```

`uv sync` 按 `pyproject.toml` + `uv.lock` 创建 `.venv/` 并安装依赖。项目不
维护 `requirements.txt`，不要用 `pip install` 逐个装包。

## 5. 配置 .env

```bash
cp .env.example .env
chmod 600 .env   # 敏感文件，仅属主可读写
```

编辑 `.env`，重点项：

| 配置项 | 说明 |
| --- | --- |
| `ADMIN_PASSWORD` | Web 后台登录密码（用户名 `admin`）。**必须替换**为强密码；为空或保留 `change-me` 时服务拒绝启动。 |
| `SESSION_SECRET` | 签名登录会话 Cookie 的密钥。**必须替换**为不可预测的随机字符串；为空或保留 `change-me-too` 时服务拒绝启动。 |
| `CALL_WORKER_ENABLED` | 外呼 Worker 自动启动**硬开关**，默认 `0`。只有确认硬件链路（串口、声卡、音频上行）后再设为 `1`；`0` 时即使管理页开关打开 Worker 也不会启动。 |
| `MODEM_PORT` | A7670E 串口路径，默认为 `/dev/ttyUSB1`，按第 2 节核对。 |
| `AUDIO_DEVICE` | 声卡设备，默认为 `plughw:1,0`，按第 3 节核对。 |
| `DATABASE_URL` | SQLite 路径，默认 `sqlite:///./data/app.db`，一般不用改。 |
| `TTS_PROVIDER` | 默认 `none`（离线不生成音频）；`edge` = Microsoft Edge 在线 TTS（免费、无需 API key，输出 24kHz mono MP3，需要能访问微软服务的网络）。 |
| `TTS_VOICE` | edge 发音人，默认 `zh-CN-XiaoxiaoNeural`（如 `zh-CN-YunxiNeural`）。 |

生成 `SESSION_SECRET`（不要手工编一个短字符串）：

```bash
openssl rand -hex 32
```

把输出整行粘贴到 `.env` 的 `SESSION_SECRET=` 后面。`ADMIN_PASSWORD` 同理
设置一个强密码（建议 12 位以上混合字符）。两个值都**不得写入代码、提交到
git、出现在日志里**（`.env` 已被 `.gitignore` 忽略，日志有打码过滤）。

修改 `.env` 后需要重启服务才生效。

## 6. 数据库迁移

数据库结构由 Alembic 管理；应用启动时会校验 schema 版本与代码一致，不一致
会拒绝启动，因此**每次部署/更新后都要先迁移**：

```bash
uv run alembic upgrade head
uv run alembic current    # 确认已到 head
```

注意事项：

- 升级前建议先备份 `data/app.db`（备份方法见第 9 节）。
- 应用启动只校验版本，不会 `create_all` 自动改结构；结构变更一律走迁移
  脚本（`docs/migration-plan.md`）。
- 迁移时不要有其他进程在写库（先停服务再迁移）。

## 7. 启动方式

### 7.1 手动前台启动（开发/验证用）

```bash
cd /home/radxa/callback-demo
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 7.2 systemd 服务（生产/常驻用）

服务示例见 `scripts/systemd/callback-demo-web.service`。安装：

```bash
cd /home/radxa/callback-demo
# 1. 先按注释编辑单元文件：User、WorkingDirectory、.env 路径、uv 绝对路径
#    （uv 路径用 `which uv` 查，示例为 /home/radxa/.local/bin/uv）
sudo cp scripts/systemd/callback-demo-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now callback-demo-web
```

常用命令：

```bash
systemctl status callback-demo-web      # 状态
journalctl -u callback-demo-web -f      # 实时日志
journalctl -u callback-demo-web --since "10 minutes ago"
sudo systemctl restart callback-demo-web
```

关于 Worker 的开关，再次强调：

- 服务单元**只区分一个启动入口**：Web + 调度器总是随服务启动；
- 硬件 Worker 由 `.env` 的 `CALL_WORKER_ENABLED` 硬开关决定是否随进程启动，
  管理页的运行时开关再控制其实际工作状态；
- 不要 `--workers N`、不要起第二个实例，否则会违反 SQLite 单写者约束并可能
  并发拨号。

## 8. 验证步骤

按顺序执行，每步通过再进入下一步：

1. **健康检查**（无需登录）：

   ```bash
   curl http://127.0.0.1:8000/healthz
   # 期望输出：{"ok":true}
   ```

   同时可用 `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/healthz`
   确认返回 200。

2. **Web 登录**：浏览器打开 `http://<RockPiIP>:8000`，用户名 `admin`，密码为
   `.env` 中配置的 `ADMIN_PASSWORD`。登录失败时检查审计日志（管理页「系统
   监控 → 运行日志」有 `login_failed` 记录）。

3. **系统监控页**：进入「系统管理 → 系统监控 → 组件状态」，确认调度器
   "running"、最近扫描时间在刷新；Worker 状态应与 `.env` 硬开关一致
   （未开启时应显示配置未开启的引导）。

4. **调度验证**（不打真电话）：创建客户 → 话术 → 一个 `once` 计划（未来
   几分钟）→ 观察任务入队；或直接点「立即拨打一次」看任务进入队列。此时
   Worker 若未开启，任务停留在 `queued`，这是预期行为。

5. **硬件 smoke test（人工执行！）**：真实拨号只能由操作者提供明确测试号码
   后人工运行，代理和自动化测试不得执行：

   ```bash
   uv run python scripts/hardware_smoke.py <测试号码> /path/to/test.wav
   ```

   期望事件（`docs/callback-demo-plan.md` 有完整日志样例）：`VOICE CALL:
   BEGIN` → ffmpeg exit=0 → `VOICE CALL: END` → `NO CARRIER`，串口干净释放。
   **先决条件**：如果 Web 服务的 Worker 已开启，先到管理页关闭 Worker 运行时
   开关（或 `sudo systemctl stop callback-demo-web`），避免与 smoke test 抢
   串口。

6. **应用级端到端（人工验收）**：`CALL_WORKER_ENABLED=1` + 管理页打开 Worker
   开关，从「立即拨打一次」发起真实外呼，确认对端能听到完整测试音、任务
   状态变为 `completed`、通话详情时间线事件完整落库。

## 9. 更新与日常运维

```bash
cd /home/radxa/callback-demo
git pull
uv sync                 # 依赖有变化时；无网络环境跳过（.venv 已存在）
uv run alembic upgrade head
sudo systemctl restart callback-demo-web
```

- 日志：应用日志走标准输出（systemd 下用 `journalctl` 查看），日志内容对
  密钥和手机号做了打码（`app/logging.py`）。
- 数据库与运行数据在 `data/`（`data/app.db`、`data/imports/`、`data/audio/`），
  属于运行时数据，不提交 git。话术音频目录/命名/覆盖规范见 README「话术音频」；
  `TTS_PROVIDER=none` 时不生成音频，`edge` 时生成 `data/audio/script-{id}-{hash}.mp3`。
- 自动化备份已内置：Web 进程内定时线程按 `BACKUP_INTERVAL_HOURS` 执行，
  SQLite backup API 一致性快照 + `data/imports/` + `data/audio/` 打包，可选
  WebDAV 远端；配置见 `.env.example` 的 `BACKUP_*` 项，管理入口为「系统管理 →
  备份管理」（也可手动触发/下载）。旧版手动命令保留备查：

  ```bash
  # 在线一致性备份（无需停服务）
  uv run python -c "import sqlite3; sqlite3.connect('data/app.db').backup(sqlite3.connect('/tmp/app-backup.db'))"
  ```

## 10. 常见问题

| 现象 | 排查 |
| --- | --- |
| 服务启动即退出，日志提示 `ADMIN_PASSWORD must be configured` 或 `SESSION_SECRET must be configured` | `.env` 缺失、值为空或仍是 `change-me` / `change-me-too`；按第 5 节修改后重启。 |
| 启动报 `Database schema is not up to date` | 未执行迁移或代码已更新；`uv run alembic upgrade head` 后重启。 |
| 串口 `Permission denied` | 用户不在 `dialout` 组（重新登录生效），或 udev 规则未生效；见第 2 节。 |
| 串口节点不存在（`/dev/ttyUSB*` 为空） | 检查 USB 接线与 `dmesg | grep ttyUSB`；`MODEM_PORT` 与实际枚举不符时改 `.env`。 |
| 播放无声音、报设备错误或找不到 ffmpeg | `aplay -l` 核对 `AUDIO_DEVICE` 编号；`ffmpeg --version` 确认已安装（缺失时 `sudo apt install ffmpeg`）；`alsamixer -c <card>` 检查音量/静音；见第 3 节。 |
| 写 `data/` 报 PermissionError | 仓库目录属主与 systemd 的 `User=` 不一致；`sudo chown -R radxa:radxa /home/radxa/callback-demo`（路径示例）。 |
| 任务一直 `queued` 不拨号 | Worker 两层开关未同时打开：`.env` 的 `CALL_WORKER_ENABLED` + 管理页运行时开关；见第 7 节。 |
| 管理页显示 Worker「配置未开启」 | 硬开关为 `0`，属预期；确认硬件后再开启。 |
