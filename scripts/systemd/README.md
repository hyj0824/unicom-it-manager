# scripts/systemd

Rock Pi 3A 部署用的 systemd 服务示例。

- `callback-demo-web.service`：Web + 调度器 + （可选）外呼 Worker 的单一
  服务单元。Worker 不是独立进程，由 `.env` 的 `CALL_WORKER_ENABLED` 硬开关
  控制是否随进程启动（默认关闭）。

完整部署步骤（串口权限、声卡选择、`.env`、数据库迁移、安装启用、验证）见
[`docs/deploy-rockpi.md`](../../docs/deploy-rockpi.md)。

快速安装（路径等示例值须按实际部署替换，见单元文件头部注释）：

```bash
sudo cp scripts/systemd/callback-demo-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now callback-demo-web

systemctl status callback-demo-web       # 状态
journalctl -u callback-demo-web -f       # 实时日志
```
