## 问题与原因
- 终端报错 `ModuleNotFoundError: No module named 'fastapi'`，原因是虚拟环境中未安装依赖。
- 另外，启动方式不应直接运行 `main.py`，应通过 `uvicorn` 启动 FastAPI 应用。

## 配置变量
- 编辑 `.env`：
  - `TG_API_ID=21145576`
  - `TG_API_HASH=0a5566d8d9c21356c89c520257a869f8`
  - `TG_SESSION_NAME=main_account`
  - `ADMIN_TOKEN=<请设置强随机口令>`（可用 `openssl rand -hex 16` 生成）

## 安装依赖
- 激活虚拟环境并安装：
  - `source venv/bin/activate`
  - `python -m pip install -r requirements.txt`
  - 如 `python` 不可用，使用 `venv/bin/python -m pip install -r requirements.txt`

## 一次性登录（生成 .session）
- 新增一次性登录脚本 `login.py`（我来添加）：
  - 使用你提供的手机号 `+88805976102`、`api_id`、`api_hash` 发送验证码请求；你在控制台输入验证码完成登录。
- 运行：`venv/bin/python login.py`
- 完成后将生成 `main_account.session`，后续无需重复登录。

## 启动后台
- 启动：`venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000`
- 前端页面顶部填写并保存 `ADMIN_TOKEN`，点击“刷新群列表”验证会话可用。

## 我这边的具体动作（获得你的确认后执行）
1. 写入 `.env` 为你提供的参数并设置一个强口令 `ADMIN_TOKEN`。
2. 创建 `login.py` 登录脚本。
3. （可选）帮你在终端执行登录脚本，你输入验证码。
4. 安装依赖并用 `uvicorn` 正确启动服务。

## 预期结果
- 依赖安装完成，服务可启动。
- `.session` 已生成并持久化，后续不需再登录。