# 开发历史归档

本文归档已完成的产品基线和开发事项。它不是当前实现规范；当前范围和
“已实现/规划中”状态以 [`product.md`](product.md) 与
[`hardware.md`](hardware.md) 为准。待办只保留在 [`TODO.md`](../TODO.md)。

## 2026-08：产品定位调整

- 产品从“定期电话回访 Demo”收敛为内网运营支撑助手，通知对象改为客户经理、网络维护责任人和审核人员，不再通知客户本人。
- 三个自动扫描场景落地：协议到期维系、退网设备回收、审核卡单提醒；管理员手动设置客户回访计划的入口移除。
- A7670E 直发短信接入扫描通知，短信与语音由单通道 Worker 串行处理，`/sms` 提供脱敏发送记录。

### 扫描通知闭环

- `due_renewal` 按默认 09:00、提前 14 天的窗口扫描 `business_services`，按客户经理职责通知；同业务同日去重，跨日可再次提醒。
- 到期话术支持 `{{客户名称}}`、`{{业务号码}}`、`{{协议到期日}}`、`{{负责人姓名}}`、`{{设备编码}}` 等占位符，渲染文本写入 `meta_json` 快照并生成音频；短信复用同一渲染文本。
- `/daily-renewals`（客户维系登记）与 `/daily-recycles`（设备回收登记）完成续签、退网和设备回收申请闭环。状态变化经审核应用后不再命中相应扫描；页面按 `call_tasks.meta_json` 显示累计通知次数，并阻止重复待审申请。
- `device_recycle` 扫描退网/协议过期业务下未回收设备，按网络维护责任人通知，同设备同日去重；回收审核应用后停止提醒。
- `review_stuck` 扫描全部 `submitted` 申请，通知有 `review` 权限和手机号的启用用户，同申请同日去重，不另设卡单时长阈值。
- `/scan-schedules` 取代回访计划页面，支持三种扫描类型、cron、时区、提前
  天数、启停、话术和短信开关；`call_tasks` 通过 `scan_schedule_id`、`source`、
  `meta_json` 追踪来源和快照。

## 基础应用与调度

- 完成 FastAPI + Jinja2 + SQLite + SQLAlchemy 后台骨架；登录最初为单密码，
  后续升级为用户账号、密码哈希和角色权限。完成统一布局、工作台、话术、
  通讯录、通话记录和系统管理页面。
- 完成 `once`/`cron` 时间计算、到期入队、暂停调度、cron 预览和重启恢复；重启不补打历史，遗留 `dialing` 任务可收尾。
- 完成单通道 Call Worker、任务领取、人工重新入队、自动重试 attempt 记录和 `CALL_WORKER_ENABLED` 默认关闭的硬开关。
- 完成通话筛选分页、详情事件时间线、Worker 状态和高影响操作确认。

## A7670E 与音频链路

- 完成 pyserial 客户端、`AT`/`ATD`/`AT+CHUP`、串口读循环和 `VOICE CALL: BEGIN/END`、`NO CARRIER`、`+CLCC` 等事件解析。
- 完成 `queued` → `dialing` → `connected` → 终态的状态转换，覆盖未接通、拒接、忙线、超时、短通话和播放失败分类；每条 `CallEvent` 含时间和原始串口行。
- 完成串口握手、ffmpeg/ALSA 播放、smoke test 和应用级端到端人工验收记录。仍有实机异常释放、接线和音量复验事项，见 TODO。
- 完成 TTS Provider 接口、`edge`/`none` Provider、音频命名缓存、原子写、`tts_error`、生成重试和页面试听；播放统一走系统 ffmpeg。

## 台账、导入与审核

- 完成业务台账和网络设备规范化模型：`customers`、`contacts`、`customer_contacts`、`business_services`、`network_devices` 和字典。
- 完成 XLSX 扁平行导入暂存、表头别名、多设备拆分、阻断/缺项/冲突报告、暂存更正和重新校验。
- 完成中文扁平台账导出，带填写说明、校验列、稳定 ID/版本和覆盖冲突保护。
- 完成 `business`/`network` 双域变更申请、字段级差异、
  `draft/submitted/returned/approved/rejected/applied/cancelled` 状态机、退回
  原因、版本冲突检测和事务回滚。
- 完成维系与回收工作台：续签、退网和设备回收申请，以及已有申请防重复提交和审核应用后的扫描窗口失效。
- 集成样本 3,113 行的验证结果已归档：317 行阻断、2,796 行缺项、107 个设备归属冲突；上线演练仍是人工待办。

## 用户、权限与审计

- 完成密码哈希、用户/角色管理、基于五个业务域的权限拦截和五个岗位模板。
- 完成 `real_name`/`phone` 必填校验（系统管理员可不填手机）；导入应用按
  客户经理/网络维护责任人职责自动建号，用户名为手机号、随机初始密码、首次
  登录强制改密，并记录 `auto_provisioned`/`force_password_change`。
- 完成自提交自审核二次确认、审核后单独应用确认、登录/登出/导出/拨号/管理员操作审计，以及手机号和联系人电话默认脱敏。

## 短信通知

- 完成 `SmsNotification` 记录、扫描配置 `sms_enabled` 和环境变量 `SMS_ENABLED` 双重开关。
- 完成 CPIN/CSQ 检查、CMGF 文本模式、IRA 字符集、中文 UCS2、`>` 提示符、`+CMGS` 成功和 `+CMS ERROR` 失败处理。
- 完成短信空闲串行发送、失败落库且不阻塞语音，以及 `/sms` 最近 200 条脱敏列表。

## 迁移、备份与部署

- 完成 Alembic 初始 schema、种子字典/权限/角色、`uv` 依赖管理和启动 schema 版本校验；应用不再用 `create_all` 自动改结构。
- 完成 SQLite backup API 一致性快照，将数据库、`data/imports/`、`data/audio/` 打包，支持本地保留、WebDAV 上传、保留期管理、手动触发、下载和历史页。
- 完成 Rock Pi 3A 部署手册和 systemd 单元示例。

## 2026-08 精简与清理

- 删除无数据的 `custom_field_definitions`/`custom_field_values` 预留表。
- 删除 `call_records` 中录音、ASR、LLM 和情绪分析占位列。
- 移除 `/customers` 客户页面，客户主体改由台账导入审核链路自动创建；通讯录保持 `/contacts` 独立目录。
- 清空 `data/app.db` 的测试/示例业务数据，仅保留字典、权限和角色种子。
