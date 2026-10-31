# UI 重构实施报告

对应 `全面修复.md`。审计阶段产物见 `docs/UI_REFACTOR_AUDIT.md`。
本报告只记录**已经落盘并验证过**的内容，未做的部分明确列为「未完成」。

---

## 一、关键技术决策

`全面修复.md` §32 要求「Vue3 + TS + Pinia + Element Plus」。项目原本是
`templates/vue_index.html` + CDN 引入的 `vue.global.prod.js` + 单文件 `static/vue-admin.js`（约 2100 行），
**没有 TS、没有 Pinia、没有 Element Plus、没有任何打包步骤**。

本次按用户确认的路线**真的迁到 TS + Pinia + Element Plus**，并接上构建管线：

- 前端源码在 `frontend/`，Vite 6 + Vue 3.5 + TS 5.7 + Pinia 3 + Element Plus 2.9（zh-cn locale + dark css-vars）
- 产物固定文件名 `app.js` / `app.css`，直接输出到后端读取的 `static/dist/`
  （`vite.config.ts::build.outDir = '../static/dist'`，避免「构建完忘记拷贝」导致页面还是旧前端）
- `templates/vue_index.html` 只剩外壳：一张 css + 一个 `<script type="module">`，带 `?v={{ build_id }}`
- 缓存破坏：`main.py::_frontend_build_id()` 取 `static/dist/app.js` 的 mtime 当版本号
- **Docker 多阶段构建**：`node:22-alpine` 阶段 `npm ci && npm run build` → `COPY --from=frontend /static/dist ./static/dist`
- 两个 compose 文件都**移除了 `./static` bind mount**，改前端必须 `docker compose up -d --build`

## 二、数据库

新增 1 张表（`app/models.py`）：

| 表 | 用途 |
|---|---|
| `target_groups` | 目标群组池。字段：`raw` / `username`（唯一）/ `link` / `kind`(public\|invite) / `group_id` / `title` / `status` / `last_error` / `last_run_at` |

- 与 `group_caches` **不是一回事**：后者存「某账号已加入了哪些群」，前者存「我们要往哪些群发」。
- `group_id` 是解析出来的 Telegram 数字 id。**发送链路只认数字 id**，所以解析结果缓存回库，
  同一个群只解析一次。
- `last_run_at` = 该群最后一次被投放到任务的时刻（`/api/scheduler/start` 建任务时写入）。
  它只表示「已建任务」，**不代表发送成功**，所以文案是「最后执行」，成功与否看运行日志。
- 建表依赖既有的启动期 `Base.metadata.create_all`，无迁移脚本。

## 三、接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/groups/import` | 导入目标群组。JSON `{text}` 或 multipart `file`(TXT/CSV)。**固定 200，部分成功**：返回 `total / imported / duplicate / invalid / invalid_samples` |
| GET | `/api/groups/pool` | 全量返回群组池 + `by_status` 计数（不设人为上限） |
| POST | `/api/groups/pool/delete` | `{ids:[...]}` 或 `{scope:"invalid"\|"all"}`。只删目标记录 |
| POST | `/api/groups/pool/check` | **重新检测**（§10）：忽略缓存 `group_id` 重新向 Telegram 确认可达性。没有 READY 账号时返回 400 `no_ready_accounts`，不空跑 |
| POST | `/api/groups/pool/join` | **批量加群**：`mode=preview` 只回分配预览，`mode=start` 起后台任务。群组池按轮询平均分给 READY 账号，每个群只分给一个账号 |
| GET | `/api/groups/pool/join` | 批量加群实时进度（内存态，重启即丢） |
| POST | `/api/groups/pool/join/stop` | 停止正在跑的批量加群 |
| POST | `/api/scheduler/plan` | **Dry Run**：只出方案，不建任务 |
| POST | `/api/scheduler/start` | 按方案建任务并拉起 runner，同一 `plan_id` 幂等 |

输入标准化（`app/routers/group_pool.py::normalize_group_line`）接受
`https://t.me/xxx`、`t.me/xxx`、`@xxx`、裸 `xxx`、`t.me/xxx/123`、`+invite_hash`、`t.me/joinchat/hash`；
自动去空格/BOM/引号、去重、`#` 注释跳过、`t.me/c/...` 判为无效（内部链接无法投放）。

删除的死接口：`/api/send`、`/api/login/default-phone`(GET+POST)、重复注册的 `/api/send-multi-account`、
`/api/accounts/delete`、`/api/accounts/bulk-update-profile`、`/api/accounts/assign-sequence`。

## 四、批量加群（群组池 → 账号）

`app/services/join_service.py`，解决「导入的群组平均分配给上传的账号，且不要重复加入」：

1. **分配**：`build_assignment` 轮询平均分配，第 i 个群给第 `i % n` 个 READY 账号；
   每个目标群只会落到一个账号名下，**不会两个账号加同一个群**
2. **参与账号**：只放行 READY 账号（与调度方案同一道门禁：`classify_ready_accounts`）；
   其余账号在预览里按原因列出（未检测 / 已封禁 / 登录失效 / …）
3. **不重复加入**：每个账号动手前先拉一次自己的会话列表（含频道），已在群里的直接记「已跳过」；
   真去加时把 `UserAlreadyParticipantError` 也当「已加入」，所以同一批**重跑不会重复加**
   - 预取集合同时含 peer id 与 raw id，整批只在换账号时拉一次会话列表，
     为此给 `join_group` 加了可选参数 `known_joined_ids`（不传则保持原行为，逐个群自查）
4. **后台任务**：507 个群逐个加必然超过任何 HTTP 超时，所以是 `asyncio.create_task` 后台跑，
   页面轮询 `GET /api/groups/pool/join` 看进度（已加入 / 已跳过 / 失败 + 逐条明细），支持停止
5. **节奏**：每群间隔可调（默认 4s，带抖动），换账号额外歇 3–8s；遇到 FloodWait 会等够时间重试一次
6. **落库**：只有真的加进去的群才写 `last_run_at`，并作废该账号的已加入群组缓存（避免看到旧列表）。
   进度本身是内存态，重启即丢——不伪造统计
7. **结果分类**：`已加入` / `已跳过`（本来就在群里）/ `待审批`（需要管理员审批，已提交申请）/
   `失败`（其余错误都带原文）。「待审批」不算失败，也不算加入成功，单列出来避免误导

### 账号检测结果持久化

账号检测结果原先只活在进程内存里，**每次 `docker compose up -d --build` 都会把所有账号打回「未检测」**，
而加群与调度都要求账号是可用状态 → 每次部署都要重跑一遍全量检测（几十个账号要好几分钟）。

现在检测结果顺手写进 `system_kv`（key = `account_status:<账号>`），启动时 `load_persisted_statuses()` 读回内存，
结果里仍带 `checked_at`，前端照旧能看出是不是旧的。

## 五、调度 Dry Run

`app/services/plan_service.py`，遵循 §13 的调度原则：

1. 只用 READY 账号（`cached_status().valid and status in {ok, slow}`）；其余按原因归类
   （未检测 / 已封禁 / 登录失效 / 鉴权失败 / 连接失败 / 无群发言权限 / …）
2. 已有运行中任务的账号不进方案（一账号不能同时跑两个互斥 Telegram 操作）
3. 已被运行中任务占用的群组跳过（同一目标不重复建任务）
4. 公开群按 `@username` 解析成数字 id（多账号轮询 + 并发 4，分摊 FloodWait）；
   邀请链接需先加入，单独归类；解析失败 = 「群组不可访问」
5. 剩余目标按账号**轮询平均分配**（负载均衡），不做人为的每号上限
6. 没有可用账号时，未解析的群组归类为「无可用账号」而不是「群组不可访问」（避免误报）
7. 方案快照存 `system_kv`（`dispatch_plan:<id>`），`start` 时读取；**无需新建 SchedulerPlan 表**

返回结构（对齐 §14）：

```json
{
  "plan_id": "...",
  "accounts": {"total": 146, "ready": 128, "blocked": 18, "blocked_reasons": {"尚未检测登录状态": 18}},
  "groups":   {"total": 527, "planned": 514, "blocked": 13, "blocked_reasons": {"群组不可访问": 9, "已有任务": 4}},
  "planned":  {"tasks": 128, "accounts_used": 128, "total_targets": 514},
  "assignment": [{"account": "...", "group_ids": [...]}]
}
```

`/api/scheduler/start` 先查幂等（同 `request_id` 已有任务则直接返回 `duplicate:true`），
再走限流；因此重复点击「开始全部任务」不会 429，也不会重复建任务。

## 六、前端结构

导航 7 页，首项为「全局控制台」；8 个 Pinia store 收口所有请求，视图不直接 fetch。

`frontend/src/views/ConsoleView.vue` 纵向结构（§35）：

1. **顶部**：调度状态徽标、`异常 N` 按钮（→ 错误中心抽屉）、上传账号 / 检测账号 / 刷新
2. **KPI 条**：账号 / 可用账号 / 异常账号 / 未检测 / 目标群组 / 运行任务
   —— 全部来自 `GET /api/dashboard/overview`（含 `groups` 字段），一次请求拿齐
3. **账号资源池 ｜ 目标群组池**：群组池含粘贴框 + 文件选择 + 导入按钮 + 导入结果明细
4. **群组列表**：状态下拉筛选 + 群名筛选 + 全选/勾选 + `重新检测` / `检测选中` / `删除选中` / `删除无效` / `清空全部`
5. **批量加群**：每群间隔可调 + `预览分配`（先看清哪个账号分到几个群）+ `开始批量加群`（带确认弹窗）+ `停止`；
   运行中显示进度条、已加入 / 已跳过 / 失败计数与逐条明细（可按结果筛选）
6. **智能调度**：消息内容 + 解析模式 + 每群间隔 + 每号轮数 + 轮次间隔
   → `[生成智能方案]` → 方案卡片（可用账号 / 目标群组 / 预计任务 / 可投放目标 + 不可执行明细）
   → `[开始全部任务]`（Primary，带确认弹窗）
7. **总体进度**：进度条 + 成功 / 失败 / 执行中账号 / 等待中账号 / 剩余 + 全部暂停/恢复/停止
8. **最近任务 ｜ 运行日志**：日志带「普通 / 详细」切换（§19），详细模式才显示 TASK_ID / ROUND / 解析 / 内容预览

`el-drawer` **错误中心**（§20）按 4 类统计，全部来自后端真实字段，不虚构没有数据源的分类：

| 分类 | 数据源 | 「查看」行为 |
|---|---|---|
| 账号异常 | `overview.accounts.error` | 跳「健康检查」 |
| 代理异常 | `overview.accounts.no_proxy` | 跳「代理 IP」 |
| 群组异常 | `pool.by_status.unreachable + failed` | 就地筛选群组列表 |
| 任务异常 | `overview.tasks.by_status.failed` | 跳回控制台看任务与日志 |

自动化轮询：仅停在控制台页且标签页可见时，每 5s 拉 `tasks + logs + overview`；
群组池**不进轮询**（可能几千行），只在导入/删除/重新检测/手动刷新时拉取。

已删除：`protocols` 页面、`static/vue-admin.js`、`static/vue-admin.css`（CDN 单文件方案整体作废）。

## 七、验证

- `python3 -m unittest discover -s tests`：**38 个测试全绿**，全部走 `httpx.ASGITransport`，**不联网**。
  覆盖：格式标准化、坏行/重复计数、文件导入、按 id / scope 删除、未检测账号不可用、
  轮询平均分配、运行中群组跳过、解析失败 → 不可访问、解析结果落库后二次 Dry Run 不再联网、
  start 幂等、未知 plan 404、重新检测无可用账号时 400、重新检测强制重解析并清掉失效 id、
  加群平均分配不重复分配、无可用账号拒绝加群、「无群发言权限」账号照样能加群、
  已在群内的目标被跳过（不会重复加）、只有真加入的才写「最后执行」、**检测结果重启后能恢复**。
- `npm run typecheck`（vue-tsc strict + noUnusedLocals/Parameters）：0 报错。
- `docker compose -f docker-compose.isolated.yml up -d --build`：镜像内完成前端构建，
  容器内 `/app/static/dist` 存在，首页 `?v=` 随产物 mtime 变化。
- 浏览器实测（8003）：
  - 7 个页面全部正常渲染，**console error 0 条**，无 `Failed to resolve component` / `is not a function`
  - 「异常 N」→ 错误中心抽屉正常，4 类分类与「查看」按钮正常
  - 群组列表：状态筛选 + 勾选后「检测选中（1）」计数正确
  - 运行日志「详细」模式正确多出 TASK_ID / ROUND 列，「普通」模式恢复

## 八、未完成 / 已知限制

| 项 | 说明 |
|---|---|
| 真实端到端发送 | 需要先在「健康检查」把账号检测成 READY，再「生成智能方案 → 开始全部任务」；本轮未跑真实发送 |
| Worker 异常分类 | §20 列了这一类，但当前没有 worker_id 落库，无数据源，故未做（避免造假数据） |
| 「已加入账号」列 | §9 列了这一列，但系统里没有「某账号加入了某目标群」的真实记录，按 146 个账号逐个拉群成员代价过高。宁可不显示也不展示恒为 0 的假数据，故该列与 `joined_accounts` 字段一并移除 |
| 前端产物体积 | `app.js` 1.27MB（gzip 404KB），Element Plus 全量引入未做按需分包；功能正常，暂不优化 |
