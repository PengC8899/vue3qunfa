# TG 群发控制台

Telegram 多账号加群 + 群发控制台：Starlette（`main.py`）+ Telethon + Vue 3（`frontend/`）+ SQLite。

支持：批量导入 Session、账号健康检查、代理池（一号一代理）、群组池导入、按任务编号群发、
「边加群边群发」常驻模式、运行日志页。

---

## 一台新机器部署（Docker，推荐）

### 0. 前置

- 一台能访问 Telegram 的服务器 / 电脑
- 已安装 Docker 与 Docker Compose v2（`docker compose version` 能正常输出）

### 1. 拉代码

```bash
git clone https://github.com/PengC8899/vue3qunfa.git
cd vue3qunfa
```

### 2. 配置 .env

```bash
cp .env.example .env
```

编辑 `.env`，**至少**把这三项换成真实值：

```env
ADMIN_TOKEN=自己设一个后台令牌
TG_API_ID=你的api_id
TG_API_HASH=你的api_hash
```

> `TG_API_ID` / `TG_API_HASH` 要和导入的 session 属于同一套应用（my.telegram.org 申请）。
> 其它参数（时区、预热、节奏）都有默认值，可以先不动。

### 3. 启动

```bash
docker compose up -d --build
```

首次构建会在镜像里自动打包前端（需要能访问 npm registry）。

启动后打开：`http://<服务器IP>:8002`

### 4. 首次使用顺序

1. 输入 `.env` 里的 `ADMIN_TOKEN` 登录面板
2. **账号管理** → 上传 `.session` 文件（支持多选 / zip 批量）
3. **健康检查** → 开始检查，确认账号「可用」
4. **代理 IP**（可选）→ 导入代理并批量测试；开启「一账号一代理」后，没代理的账号不会派活
5. **全局控制台** → 目标群组池导入群链接（每行一个 `@xxx` / `https://t.me/xxx`）
6. 在控制台保存一条群发任务拿到编号（例如 `#8`）
7. 选好编号 → 点「边加群边群发」

之后加群会优先加「还没有任何账号加入的群」，新加入的群在下一轮自动进入发送列表。

### 5. 数据与备份

| 路径 | 内容 |
|---|---|
| `data-8002/data.db` | SQLite 库：账号状态、群组池、任务、发送/加群日志 |
| `sessions-8002/` | Telethon session 文件（敏感，别外传） |
| `.env` | 令牌与 API 凭据（**没有**提交到仓库） |

备份 = 打包这三个就行。迁移到新机器也是把这三样拷过去。

### 6. 更新代码

```bash
git pull
docker compose up -d --build
```

`data-8002`、`sessions-8002` 是挂载卷，重建容器不会丢数据；正在跑的常驻任务会自动恢复。

---

## 常用命令

```bash
docker compose ps                       # 容器状态
docker logs -f tg-local-web-8002        # 实时日志
docker compose restart                  # 只重启，不重建
docker compose down                     # 停止并删除容器（数据仍在挂载目录）
```

---

## `.env` 常用参数

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADMIN_TOKEN` | 无（必填） | 后台访问令牌 |
| `TG_API_ID` / `TG_API_HASH` | 无（必填） | Telegram 应用凭据 |
| `TIMEZONE` | `Asia/Singapore` | 日志与统计使用的时区 |
| `LIVE_SEND_TARGET_PER_HOUR` | `480` | 全池每小时目标条数，决定每个群多久能再发；`0` 关闭自适应 |
| `GROUP_MIN_GAP_S` | `1800` | 同一个群两次投递的最短间隔（换号也算） |
| `ACCOUNT_RESTRICT_COOLDOWN_S` | `86400` | 账号被平台限制发言后冻结多久 |
| `LIVE_JOIN_WORKERS` | `8` | 同时加群的账号数 |
| `WARMUP_ENABLED` | `1` | 新导入账号先只读预热，毕业后才允许群发 |
| `JOIN_ACCOUNTS_PER_GROUP` | compose 里设为 `1` | 每个群至少保留几个成员账号 |

完整参数见 `app/config.py`（都支持同名环境变量）。

---

## 本地开发（不用 Docker）

要求：Python 3.10+、Node.js 20+

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cd frontend && npm install && npm run build && cd ..
cp .env.example .env        # 填 ADMIN_TOKEN / TG_API_ID / TG_API_HASH

SESSION_DIR=./sessions DB_URL=sqlite:///./data.db uvicorn main:app --port 8000
```

前端热更新：`cd frontend && npm run dev`（`/api` 默认代理到 `127.0.0.1:8003`，
按需改 `frontend/vite.config.ts` 里的 `server.proxy`）。

---

## 目录结构

```
main.py                     所有 API 路由与「边加群边群发」调度
app/telegram_client.py      Telethon 多账号客户端、发送与加群
app/services/               加群、发送、代理、预热、Session 导入等
app/models.py               数据表：tasks / send_logs / join_logs / target_groups ...
frontend/                   Vue3 + TS + Pinia + Element Plus 源码
templates/vue_index.html    页面外壳（加载 /static/dist/app.js）
Dockerfile                  两阶段构建：先打包前端，再跑 Python
docker-compose.yml          端口 8002，挂载 sessions-8002 / data-8002
tests/                      后端测试（pytest）
```

---

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 启动即退出，日志 `Missing ADMIN_TOKEN` | `.env` 没配或没放在项目根目录 |
| 面板 401 | 令牌填错，或在 `.env` 改完没重启容器 |
| 账号显示「未授权」 | session 失效，需要重新导出上传；确认 `TG_API_ID/HASH` 与 session 同一套应用 |
| 账号「被平台限制发言」 | Telegram 判了 spam ban，按 `ACCOUNT_RESTRICT_COOLDOWN_S` 冻结；到期自动恢复 |
| 群发数为 0 | 看运行日志页/控制台详情：通常是预热未毕业、全部群在冷却、或账号都被限制 |
| 页面还是旧版 | 改过 `frontend/` 必须 `docker compose up -d --build` 重新打包 |
| 端口冲突 | 改 `docker-compose.yml` 里的 `"8002:8000"` 左侧端口 |
| 外网访问不了 | 云安全组 / 防火墙放行对应端口 |

---

## 安全

- `.env`、`sessions-*/`、`data-*/` 已在 `.gitignore` 中，**不要**提交到仓库
- 群发请遵守 Telegram 使用条款，合理设置间隔；高频同文案最容易触发限制
