# UI 重构审计（PHASE 1–2）

审计对象：`全面修复.md`（1769 行，目标：把后台重构成单页「全局智能调度控制台」）
审计时间：本次会话
结论先说：**文档的设计目标可行，但它描述的技术栈与架构和本项目不一致。按原文逐字实现＝推倒重写。** 下面是逐项核对。

---

## 一、文档假设 vs 实际代码

| 文档假设 | 实际情况 | 影响 |
|---|---|---|
| 「技术栈沿用当前：Vue 3 + TypeScript + Pinia + Element Plus」 | 实际是 **无构建步骤的原生 JS**：`templates/vue_index.html` 引入 CDN 的 `vue.global.prod.js`，全部逻辑在一个 `static/vue-admin.js`（单文件，`createApp({setup(){}})`）。**没有 TS、没有 Pinia、没有 Element Plus、没有 vite/打包** | 文档 §32「不要换框架」与它自己列出的技术栈自相矛盾。要按 TS+Pinia+Element Plus 做，必须先引入构建管线和新依赖 |
| 「Worker 模块 / Queue / Redis」 | **不存在**。单进程 Starlette，任务由进程内 `asyncio.Task` 跑（`_register_task_runner` / `TASKS` 字典），队列就是 DB 表 `tasks` | §13 的 Worker 负载均衡、§17 的「Worker 6/6 正常」没有对应实体 |
| 「WebSocket/SSE 模块」 | **不存在**。前端是 `setInterval` 轮询（`scheduleLogsAutoRefresh` 5 秒 + `fetchDashboard`） | §25/§30 的实时通道需要从零建 |
| `GET /api/dashboard/overview` | 不存在 | 需新增（本次已实现，见第四节） |
| `POST /api/accounts/import`、`GET /api/accounts` | 不存在。实际是 `POST /api/accounts/upload-sessions`、`GET /api/accounts/status` | 名字不同，**不建议为了对齐文档改名** |
| `POST /api/accounts/health-check`、`DELETE /api/accounts/{id}`、`DELETE /api/accounts/batch`、`POST /api/groups/import`、`DELETE /api/groups/batch`、`POST /api/tasks/start`、`/api/scheduler/*` | 全部不存在。等价能力散在 `check-single` / `bulk-delete` / `send-async-batch` / `task-control` | §27/§38 的 REST 风格接口需新建或复用 |
| `SchedulerPlan` / `SchedulerPlanItem` / `TaskAttempt` / `Worker` / `ExecutionLog` 表 | **不存在**。现有 7 张表：`send_logs`、`group_caches`、`tasks`、`task_events`、`account_health`、`account_proxies`、`system_kv` | §28 计划快照、§30 事实源、§37 历史记录都缺表 |
| 「目标群组池」（§8 上传 TXT/CSV 导入群组、§9 群组列表） | **不存在目标群组实体**。`group_caches.data_json` 存的是**每个账号已加入的会话列表**，不是「待处理目标池」。发送流程的 `group_ids` 是用户从头像/账号已加入群里挑的 | **这是最大缺口**：文档右侧整栏（上传群组 / 去重 / 群组列表 / 群组批量删除）没有后端支撑 |
| 「当前 UI 按钮爆炸，多页面都有开始/运行/创建/检测/执行/提交/调度」 | 部分成立。确有 8 个页面、`/api/groups/join` 被 **3 个不同位置**调用（js:1337 / 1388 / 1795），三个加群流程重复 | §21 的判断有依据 |
| §31「后台重启后恢复 SchedulerPlan / RUNNING / PAUSED」 | `startup_event` 已有 `RESUME_TASKS_ON_STARTUP` 恢复逻辑 + 3 个巡检协程 | 部分已存在，不需要重做 |

---

## 二、现有 API 清单（44 个注册路由）

「前端调用位置」= `static/vue-admin.js` 行号；`✗` = 全仓库无调用方。

| METHOD | PATH | 功能 | 前端调用 | 重复 | 建议 |
|---|---|---|---|---|---|
| GET | `/` | 控制台页面 | 浏览器 | | 保留 |
| GET | `/api/accounts/status` | 账号列表 + 代理 + 授权状态 | 717 | | 保留（改为聚合版见第四节） |
| POST | `/api/accounts/check-single` | 单账号检测（联网） | 1727, 1844 | | 保留 |
| POST | `/api/accounts/delete` | 单账号删除 | **✗** | 与 bulk-delete 重复 | **删** |
| POST | `/api/accounts/bulk-update-profile` | 批量改资料 | **✗** | 与 profile 重复 | **删** |
| GET | `/api/accounts/profile` | 读资料 | 878 | | 保留 |
| GET | `/api/accounts/profiles-authorized` | 授权账号资料列表 | 817 | | 保留 |
| POST | `/api/accounts/profile` | 改资料 | 891 | | 保留 |
| GET | `/api/accounts/proxy` | 读单账号代理 | 980 | | 保留 |
| POST | `/api/accounts/proxy` | 写单账号代理 | 997 | | 保留 |
| GET | `/api/accounts/proxy/default-source` | 读默认代理源 | 962, 1061 | | 保留 |
| POST | `/api/accounts/proxy/default-source` | 写默认代理源 | (js:1067) | | 保留 |
| POST | `/api/accounts/proxy/auto-assign` | 批量分配代理 | 1090 | | 保留 |
| GET | `/api/settings/proxy-policy` | 读「强制一号一代理」开关 | (本次新增) | | 保留 |
| POST | `/api/settings/proxy-policy` | 写该开关 | 1044 | | 保留 |
| GET | `/api/proxies/overview` | 代理总览（含开关） | 1022 | | 保留 |
| POST | `/api/proxies/unassign` | 解绑代理 | 1143, 1160 | | 保留 |
| POST | `/api/proxies/test` | 测代理连通/出口 IP | 1117 | | 保留 |
| POST | `/api/system/reset` | 重置系统 | 2015 | | 保留 |
| POST | `/api/accounts/upload-sessions` | 上传 session/zip（纯本地） | 952 | | 保留 |
| POST | `/api/accounts/bulk-delete` | 批量删除账号 | 1889,1910,1942,1970,1995 | | 保留（§38 要求的形态已满足：一次请求 + 逐项结果） |
| POST | `/api/task-control` | 暂停/恢复/停止单任务 | 1524 | | 保留 |
| POST | `/api/accounts/assign-sequence` | 按顺序分配账号 | 1769 | | 保留 |
| GET | `/api/groups` | 读**某账号已加入**的群 | 750 | | 保留（注意不是目标群池） |
| POST | `/api/groups/cache/clear` | 清群缓存 | 1499 | | 保留 |
| POST | `/api/send` | 老版单账号同步发送 | **✗** | 与 test-send 重复 | **删** |
| POST | `/api/test-send` | 测试发送 | 1632 | | 保留 |
| GET | `/api/logs` | 最近日志 | 766 | | 保留 |
| POST | `/api/logs/clear` | 清日志 | 1511 | | 保留 |
| POST | `/api/send-async` | 异步发送单账号 | 1611 | | 保留 |
| GET | `/api/task-status` | 单任务状态 | 1596 | | 保留 |
| GET | `/api/tasks/summary` | 每账号任务汇总 | 806 | | 保留 |
| GET | `/api/tasks` | 任务列表 | 807 | | 保留 |
| POST | `/api/tasks/stop-all` | 停止全部 | 1538 | 与 task-control 重叠 | 保留 |
| POST | `/api/tasks/delete` | 删任务 | 1552 | | 保留 |
| POST | `/api/tasks/clear` | 清任务 | 1566 | | 保留 |
| POST | `/api/login/send-code` | 发登录码 | 908 | | 保留 |
| POST | `/api/login/default-phone` | 写默认手机号 | **✗** | | **删** |
| GET | `/api/login/default-phone` | 读默认手机号 | **✗** | | **删** |
| POST | `/api/login/submit-code` | 提交登录码 | 925 | | 保留 |
| POST | `/api/send-multi-account` | 多账号群发（分布策略） | 1692 | **与下一行同一 handler** | 保留 1 个，删另一个 path |
| POST | `/api/send-async-batch` | 同上 | 1692 | 同上 | 同上 |
| POST | `/api/groups/join` | 单账号加群 | 1337, 1388, 1795 | **3 处重复调用** | 收敛为 1 处 |
| POST | `/api/groups/join-all-accounts` | 全账号加同一群 | 1472 | | 保留 |

**可删**：`accounts/delete`、`accounts/bulk-update-profile`、`send`、`login/default-phone`×2，共 5 个路由 + 1 个重复 path。
**已在上轮删除**（记录在案，勿再引入）：`groups/debug`、`logs/export.csv`、`copy-receiver`、`copy-latest`、`task-events`、`account-status`、`session/validate`、`session/validate-batch`、`groups/join-batch`、`accounts/authorized-list`。

---

## 三、前端页面与重复入口

| 页面 key | 名称 | 内容 | 文档要求的归属 |
|---|---|---|---|
| `dashboard` | 总览 | KPI + 任务汇总 + 日志 | 全局控制台 |
| `accounts` | 账号管理 | 上传 session + 多账号平均分配加入 + 账号列表 + 资料 + 单账号代理 | 拆到 控制台（账号池）+ 设置 |
| `proxies` | 代理 IP 配置 | 默认源 / 批量分配 / 测试 / 开关 / 绑定总览 | 折叠进 设置 |
| `groups` | 群组管理 | 某账号的已加入群 + 加载 + 清缓存 | 控制台（群组池） |
| `send` | 邀请任务 | 单发 / 批量群发 / 多账号分布 / 任务列表 | 控制台（调度中心） |
| `protocols` | 协议号批量 | **第二次上传 + 第二次加群流程** | **与 accounts 重复，需合并** |
| `health` | 健康检查 | 勾选检测 + 清理未授权 | 控制台（检测按钮） |
| `system` | 系统设置 | 令牌 / 重置 / 默认手机号 | 设置（保留） |

**重复入口证据**：上传 session 有 2 套（`sessionUploadInput` / `protocolUploadInput`）；加群有 3 处调用 `/api/groups/join`；批量发送有 3 个入口（单发 / 批量 / 协议号批量）。

---

## 四、本次已实现

`GET /api/dashboard/overview` —— 文档 §26 要求的聚合接口。只做聚合，复用现有 service，不复制业务逻辑。前端打开首页从「打 4 个接口」变成「打 1 个」。

---

## 五、缺口清单（按文档章节）

| 文档章节 | 缺口 | 规模 |
|---|---|---|
| §8/§9/§10/§11 | **目标群组池**（导入 TXT/CSV/粘贴、去重、状态、批量删除）—— 无表、无接口、无 UI | 大，需新建表 + 导入接口 + UI |
| §12–§15/§27/§28 | Scheduler Plan（Dry Run + plan_id 快照 + start 幂等） | 中，可复用现有分布逻辑，但需新增 2 张表 |
| §16 | Pause/Resume | 已有 `task-control`（单任务）+ `tasks/stop-all`，缺「全局 pause」语义 |
| §18 | 最近任务流 | 数据已有（`send_logs`），只缺 UI |
| §19 | 日志普通/详细模式 | 数据已有（`logs` + `task_events`），只缺 UI |
| §20 | 错误中心（按类聚合） | 数据散在 `account_health` / `account_proxies` / `send_logs`，需聚合接口 |
| §25 | Pinia store | 需先决定是否换栈 |
| §29 | 幂等（idempotency key） | 已有 `request_id` + `_get_existing_tasks_by_request_id` 可复用 |
| §30 | 数据库事实源 | 现状**已经是** DB 为准 + 进程内 `TASKS` 做降级兜底，不需重做 |
| §31 | 重启恢复 | **已实现**（`RESUME_TASKS_ON_STARTUP` + 巡检协程） |
| §36 | 菜单 8 → 3 | 纯前端 |
| §38 | 批量删除形态 | **已满足**（`bulk-delete` 返回逐项结果） |
| §42 | 顶部流程指示器 | 纯前端 |

---

## 六、开发顺序建议（在真实栈上）

1. **后端补齐**：目标群组池（表 + 导入 + 列表 + 批量删除）→ Scheduler Plan（Dry Run + 快照 + start）
2. **前端合并**：8 页 → 3 页；合并 `protocols` 到 `accounts` 的上传流程；加群 3 处 → 1 处
3. **控制台单页**：按 §35 的纵向结构（KPI → 账号池 + 群组池 → 调度中心 → 进度 → 最近任务 → 日志）
4. **收尾**：状态映射表（§5）、流程指示器（§42）、空状态（§41）、错误中心（§20）

**不要在补第 1 步之前动 UI**：群组池和 Dry Run 没有后端时，页面画出来也是空的（文档 §48 自己也这么说）。
