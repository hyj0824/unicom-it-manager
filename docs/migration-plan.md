# 数据库迁移方案（草案）

## 1. Rock Pi 3A 上的数据库选择

PostgreSQL 支持 ARM64，可以运行在 Rock Pi 3A 上，但它不是因为设备是嵌入式就自动不适合。对于少量用户、低写入量的内网试验，Rock Pi 上使用 SQLite WAL 最简单；对于多人同时编辑、批量导入、审核历史和后续扩展，优先把 PostgreSQL 放在办公网已有的服务器、虚拟机或 NAS 上，Rock Pi 只运行 Web、调度器和 A7670E Worker。

不建议把 PostgreSQL 数据目录放在不稳定的 microSD 卡上。若必须单机部署，应使用可靠的 eMMC/SSD、UPS 或断电保护、定期备份，并限制连接数和日志写入量。PostgreSQL 不能替代备份，也不能解决 Rock Pi 整机故障。

默认部署建议：

1. P0 原型继续 SQLite，开启 WAL，单进程 Web、单 Scheduler、单 Worker。
2. 多人试用前使用 PostgreSQL；优先部署在稳定的局域网主机。
3. PostgreSQL 连接中断时，禁止 Worker 在无法可靠记录任务和事件的情况下拨号。

## 2. 迁移工具与原则

现在引入 Alembic。生产启动不再依赖 `create_all` 自动变更结构；每次结构变化都有可审查、可回滚的版本。

采用增量兼容方案：保留现有 `customers`、`scripts`、`callback_plans`、`call_tasks`、`call_records`、`call_events` 和 `app_settings`，新增用户权限、业务台账、设备、联系人、字典、变更申请、暂存导入和审计表。这样旧的回访 Demo 数据仍可查询，回访模块不必一次性重写。

## 3. 具体步骤

1. 停止 Web、Scheduler 和 Worker，复制 `data/app.db`，并校验备份可读取。
2. 建立代表当前 SQLite 结构的 Alembic 初始版本；已有数据库使用 `stamp`，新数据库使用 `upgrade`。
3. 增加新领域表和权限表，先运行结构迁移，不导入 Excel。
4. Excel 导入暂存表，生成字段映射、重复、格式和缺项报告。
5. 由业务/网络稽核人员审核导入批次；通过后分事务写入正式表，并保留原始行号和批次号。
6. 对旧 `Customer` 数据保留原表和外键关系，不在首次迁移中强行改名或删除；后续再将回访对象与客户主体建立明确映射。
7. 上线前执行行数、业务号码、通话记录、计划和事件数量校验，并进行一次恢复演练。

## 4. 两种数据库切换方式

若当前 `data/app.db` 只是开发数据，建议创建全新数据库、执行全部迁移，再从 Excel 导入，旧库只作为归档备份。

若其中已有真实回访数据，采用 SQLite -> PostgreSQL 的停机迁移：导出所有表、导入 PostgreSQL、重建自增序列、运行外键和数量校验，确认无误后切换 `DATABASE_URL`。切换期间不允许双写。

## 5. 备份与失败处理

- SQLite 至少每日复制数据库并保留多个版本；复制前暂停写入或使用 SQLite 在线备份 API。
- PostgreSQL 至少每日逻辑备份，重要试验期间增加频率，并定期验证恢复。
- 迁移失败时保留原 SQLite，不覆盖原文件；回滚通过切回旧服务和旧数据库完成。
- 迁移脚本不得删除原始导入文件、暂存行或审计记录。
