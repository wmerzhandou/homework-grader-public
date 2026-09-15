# homework-grader

多模态作业批改 Web 应用：用户（家长/孩子，小范围邀请制）上传作业照片、文档、录音、视频和文本指令，后端预处理后交给本机的 codex app-server（OpenAI Codex CLI）执行批改，结果通过 SSE 流式回推前端。

## 架构

```
                         ┌─────────────────────────────────────────────────┐
  frontend (separate)    │                    backend/ (FastAPI)           │
  ────────────────       │                                                 │
  HTTP /api/*  ─────────▶│  api/auth.py      注册 / 登录 / me (邀请制)      │
  GET .../events ───────▶│  api/threads.py   会话 / 消息历史 / 发起 turn    │
        (SSE)            │  api/materials.py 上传(multipart) / 原文件下载   │
        ▲                │  api/events.py    SSE 出口                      │
        │                │       │                                         │
        │                │       ▼                                         │
        │                │  pipeline/        异步预处理（不阻塞上传）       │
        │                │    media.py      ffmpeg: 转 16k f32 PCM、抽帧   │
        │                │    asr_client.py Qwen3-ASR 三步 API (start/     │
        │                │                  chunk/finish)                  │
        │                │    docs.py       markitdown → markdown 摘要     │
        │                │    images.py     图片原样直通                    │
        │                │       │                                         │
        │  内存事件总线   │       ▼                                         │
        └────────────────┤  codex_service/                                  │
                         │    manager.py    每用户 1 个 codex app-server    │
                         │                  进程 + 独立 CODEX_HOME，        │
                         │                  懒启动、闲置 30 分钟回收         │
                         │    workspace.py  per-user config.toml(600)/     │
                         │                  models.json/工作区目录          │
                         │    context.py    材料清单 prompt + 图片输入     │
                         │    grading.py    grading_result.json 校验       │
                         │       │                                         │
                         │       ▼ stdio JSON-RPC (openai-codex SDK)       │
                         │  codex app-server ──▶ DeepSeek (deepseek-flash) │
                         │                                                 │
                         │  data/app.db (SQLite)  用户/会话/材料/消息       │
                         │  data/users/<uid>/threads/<tid>/workspace/      │
                         └─────────────────────────────────────────────────┘
```

- **进程级多租户**：app-server 没有多租户概念，每个用户一个 `codex app-server --listen stdio://` 子进程和独立 `CODEX_HOME`（含 deepseek provider 配置，token 从后端环境变量注入，文件权限 600，绝不写日志）。
- **素材策略**：原始文件落 thread 工作区；prompt 只放材料清单（文件名/类型/用途/摘要，摘要限 500 字）；音频/视频转写全文放入；图片与视频关键帧（首/中/尾 3 张）以 `localImage` 输入传给模型。
- **批改契约**：thread 的 developer instructions 要求 codex 把结果写入工作区 `grading_result.json`，turn 完成后后端读取并用 pydantic 校验，随 `turn_completed` 事件推送。

## 本地启动

```bash
# 1. 环境变量（DEEPSEEK_API_KEY 已在 ~/.bashrc 的机器上）
set -a; . ~/.bashrc; set +a

# 2. 安装依赖
cd backend
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'

# 3. 启动（首次启动自动创建 admin 用户并打印 3 个邀请码）
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

前置要求：`codex` CLI 在 PATH（0.154.0）、`ffmpeg`/`ffprobe` 在 PATH、`~/.codex/models.json` 存在（模型目录，无密钥，会被复制到每个用户的 CODEX_HOME）、ASR 服务可达。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | （必填） | DeepSeek API key，只存在后端进程内存，注入 per-user config.toml(600) |
| `ADMIN_PASSWORD` | `admin123` | **首次启动**创建 admin 用户的密码（已有库不会覆盖，改密码用 `scripts/set_password.py`） |
| `DATA_DIR` | `<repo>/data` | SQLite 与用户工作区根目录 |
| `DATABASE_URL` | `sqlite:///<DATA_DIR>/app.db` | 数据库连接串 |
| `ASR_BASE_URL` | `http://127.0.0.1:8020` | Qwen3-ASR 服务 |
| `CODEX_BIN` | `codex` | codex CLI 路径 |
| `CODEX_MODELS_JSON` | `~/.codex/models.json` | 复制进 per-user CODEX_HOME 的模型目录 |
| `CODEX_IDLE_TIMEOUT_S` | `1800` | app-server 闲置回收秒数 |
| `FFMPEG_BIN` | `ffmpeg` | ffmpeg 路径（ffprobe 按同路径推导） |
| `API_DOCS_ENABLED` | `false` | 是否暴露 `/docs` `/redoc` `/openapi.json`（仅调试时打开） |
| `CORS_ALLOW_ORIGINS` | 空 | 逗号分隔的跨域白名单；留空则完全不启用 CORS（同源部署不需要） |
| `AUTH_TOKEN_TTL_S` | `604800`（7 天） | 登录 token 有效期（手机可能丢失/借人，窗口越短越稳） |
| `MEDIA_TICKET_TTL_S` | `900`（15 分钟） | 材料下载短时票据有效期 |
| `APP_SECRET` | 自动生成 | 票据签名密钥；留空时生成在 `DATA_DIR/.app_secret`(600) |
| `MAX_UPLOAD_FILES` | `10` | 单次上传文件数上限 |
| `MAX_UPLOAD_TOTAL_BYTES` | `314572800`（300MB） | 单次上传总大小上限 |
| `USER_STORAGE_QUOTA_BYTES` | `5368709120`（5GB） | 每用户材料总占用上限 |
| `AUTH_RATE_LIMIT_ATTEMPTS` | `10` | 登录/注册限流：窗口内允许次数 |
| `AUTH_RATE_LIMIT_WINDOW_S` | `300` | 登录/注册限流窗口（秒） |
| `ACCESS_LOG_RETENTION_DAYS` | `90` | 访问记录（监控页数据）保留天数 |
| `GEOIP_CITY_DB` | `<DATA_DIR>/geoip/GeoLite2-City.mmdb` | 离线 IP 归属库（城市/省份/国家） |
| `GEOIP_ASN_DB` | `<DATA_DIR>/geoip/GeoLite2-ASN.mmdb` | 离线 ASN 库（运营商/机构，用于推断网络类型） |

## HTTP API

所有 `/api/*`（除 register/login/health）需要 `Authorization: Bearer <token>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/register` | `{username, password, invite_code}` → `{token, user:{id, username}}` |
| POST | `/api/auth/login` | `{username, password}` → 同上 |
| GET | `/api/auth/me` | 当前用户 |
| POST | `/api/auth/logout` | 吊销当前 token（真正登出，服务端删除 token） |
| POST | `/api/auth/media-ticket` | 签发材料下载短时票据 `{ticket, expires_in}` |
| POST | `/api/threads` | `{title?}` → thread |
| GET | `/api/threads` | 当前用户 thread 列表 |
| GET | `/api/threads/{id}` | thread + 消息历史 + 材料列表 |
| POST | `/api/threads/{id}/materials` | multipart：多个 `files` 字段 + 可选 `purposes`（JSON 数组，与文件一一对应）→ `{materials:[{id, filename, kind, status}]}`；kind ∈ `document\|image\|audio\|video`，status ∈ `processing\|ready\|failed` |
| POST | `/api/threads/{id}/turns` | `{text, material_ids?}` → `{turn_id}`，异步执行 codex turn |
| GET | `/api/threads/{id}/events` | SSE 实时事件流 |
| GET | `/api/materials/{id}/file` | 下载原始文件；凭据用 `Authorization: Bearer` 或 `?ticket=`（短时票据） |
| GET | `/api/admin/visitors` | 【仅 admin】按 IP 聚合访问来源（`?days=1|7|30`） |
| — | — | 响应含归属地/网络类型、设备画像、数据访问审计（看过哪些会话、下载过哪些材料）、可疑度与风险标签 |
| GET | `/api/admin/devices` | 【仅 admin】设备列表（浏览器随机 ID + IP 数 + 可信标记） |
| POST | `/api/admin/devices/{id}` | 【仅 admin】标记设备可信 / 写备注 |
| POST | `/api/admin/sources/{ip}` | 【仅 admin】把来源 IP 标记为可信（自家网络），打分时降权 |
| GET/POST | `/api/admin/blocks` | 【仅 admin】封禁名单 / 新增封禁（`{ip, reason, ttl_hours?}`） |
| DELETE | `/api/admin/blocks/{ip}` | 【仅 admin】解封 |
| GET | `/api/admin/sessions` | 【仅 admin】登录设备列表（只给 token 的 SHA-256 前 12 位指纹） |
| DELETE | `/api/admin/sessions/{id}` | 【仅 admin】吊销单个会话（踢掉某台设备） |
| POST | `/api/admin/sessions/revoke-all` | 【仅 admin】吊销除当前设备外的所有会话 |
| GET | `/api/health` | 健康检查 |

> `<img>/<audio>/<video>` 无法携带 Authorization 头，所以材料下载额外接受 `?ticket=`。
> 票据由 `/api/auth/media-ticket` 签发，HMAC-SHA256 签名、绑定用户、默认 15 分钟有效，
> **不要再把长期登录 token 拼进 URL**（历史行为，会把凭据漏进浏览器历史和访问日志）。

### SSE 事件

```
event: material_status  data: {"material_id", "status", "error"?}
event: message_delta    data: {"turn_id", "delta"}
event: turn_completed   data: {"turn_id", "message_id", "grading_result": {...}|null}
event: error            data: {"turn_id"?, "message"}
```

### grading_result.json schema

```json
{
  "summary": "string",
  "knowledge_points": [{"name": "string", "mastery": "good|weak|poor"}],
  "questions": [{
    "index": 1, "question": "string", "student_answer": "string",
    "correct": true, "correct_answer": "string",
    "error_reason": "string|null", "explanation": "string"
  }],
  "suggestions": ["string"]
}
```

## 测试与验证脚本

```bash
cd backend && .venv/bin/python -m pytest          # 单测（外部服务全部 mock）

set -a; . ~/.bashrc; set +a                        # 以下脚本需要真实环境
backend/.venv/bin/python scripts/smoke_e2e.py      # codex_service 全链路（真实调 DeepSeek）
scripts/validate_deepseek.sh                       # codex CLI 冒烟：文本/apply_patch/图片
python3 scripts/validate_asr.py <音频或视频文件>    # ASR 三步流程验证
```

## 数据目录

```
data/
├── app.db
└── users/<uid>/
    ├── codex_home/          # config.toml(600, 含 token) + models.json
    └── threads/<tid>/
        └── workspace/       # 原始材料、keyframes/、grading_result.json
```

## 安全加固（2026-09-15）

已落地的约束（实现见 `backend/app/security.py`）：

- **攻击面**：`/docs` `/redoc` `/openapi.json` 默认 404；CORS 默认关闭（`CORS_ALLOW_ORIGINS` 按需开启）。
- **响应头**：CSP（`script-src 'self'`、`frame-ancestors 'none'`）、HSTS（HTTPS 下）、`X-Frame-Options: DENY`、
  `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`Permissions-Policy`；`/api/*` 一律 `no-store`。
- **凭据**：登录 token 有过期时间（默认 7 天），`POST /api/auth/logout` 服务端吊销；
  材料下载走 15 分钟签名票据，长期 token 不再出现在 URL 里。
- **会话过期体验**：任何鉴权请求返回 401（token 过期或被吊销）时前端自动清 token 并回登录页。
- **日志**：uvicorn 访问日志过滤查询串，`?ticket=`/`?token=` 不会落进 journald。
- **限流**：登录/注册按 IP 滑动窗口（默认 10 次 / 5 分钟），超限 429 + `Retry-After`。
- **代理头**：unit 里带 `--no-proxy-headers`。当前没有反向代理，而 uvicorn 默认信任
  `127.0.0.1` 传来的 `X-Forwarded-For`（实测本机伪造头会被采纳）；将来加 nginx/caddy 时
  改回 `--proxy-headers`，并用 `$remote_addr` **替换**（不是追加透传）该头。
- **SPA 回退**：`/api/*` 与点号开头的路径不回退到 index.html，返回 404 JSON——
  未知 API 路径不会拿到 200 HTML，扫描器也不会把 `/.env` 的 200 误读成文件暴露。
- **配额**：单次上传文件数与总大小、每用户存储上限（见环境变量表）。
- **权限**：启动时把 `data/` 收紧为 750、`app.db` 与 `.app_secret` 为 600。
- **邀请码**：只在没有可用码时补 3 个，写进 `data/invite_codes.txt`(600)，不再打进日志。
- **访问监控（`/admin`，仅 admin 可见）**：中间件把"页面加载 + /api/*"写进 `accesslog` 表
  （跳过静态资源、健康检查、本机回环、管理页自身；查询串不入库），按 IP 聚合展示，
  支持封禁/解封 IP 与吊销登录会话。数据保留 `ACCESS_LOG_RETENTION_DAYS` 天（默认 90）。
  封禁在中间件最外层判断（早于鉴权），被封 IP 直接 403。
- **来源分析（离线、不外发）**：
  - 归属地/运营商/网络类型：GeoLite2-City + GeoLite2-ASN 离线查询（结果缓存进 `ipgeo` 表），
    `network_type` 是**推断值**（云主机/VPN 用机构关键字匹配），页面上标注为推断。
  - 设备画像：UA 纯正则解析（机型/系统/浏览器，不引第三方依赖）。
  - 设备标识：前端本地生成随机 ID（`X-Client-Id`），用于区分"同一设备换 IP"与"同一 IP 多设备"；
    它是随机值而非浏览器指纹，也支持标记"可信设备"。
  - 数据访问审计：聚合出「看过哪些会话、下载过哪些材料原文件、上传/批改/登录成功失败次数、探测过的敏感路径」。
  - 可疑度：0-100 分 + 解释性标签（云主机/VPN 出口、无 UA、敏感路径探测、登录失败、未登录高频请求等），
    可信设备/可信来源会降权；管理页可「只看可疑来源」。
- **MCP 依赖锁版本**：`uvx duckduckgo-mcp-server==0.7.0`（MCP server 是 codex 拉起的普通子进程，
  不受沙箱约束，未锁版本等于敞开供应链风险）。升级时改 `backend/app/codex_service/workspace.py`
  里 `CONFIG_TOML_TEMPLATE` 的版本号并重启服务，存量用户的 config.toml 会自动重写。

配套脚本：

```bash
cd backend
# 存量库补 authtoken.expires_at（幂等，自动备份 .bak；历史 token 按 created_at+30天回填）
.venv/bin/python -m scripts.migrate_security_hardening

# 重置账号口令（保留 user_id、会话、材料与已登录设备）
.venv/bin/python -m scripts.set_password admin '<新口令>'

# 安全回归测试（15 项：文档关闭/安全头/限流/token 过期登出/票据/配额）
.venv/bin/python -m pytest tests/test_security.py

# 把历史 uvicorn 访问日志回填进 accesslog（监控页初始数据；journal 需要 root 读取）
sudo journalctl -u homework-grader -o json --no-pager \
  | .venv/bin/python -m scripts.backfill_access_log

# 下载/更新离线 IP 归属库（约 75MB，放在 data/geoip/，已 gitignore）
.venv/bin/python -m scripts.fetch_geoip

# 监控相关的存量库迁移（补 accesslog.device_id、ipgeo.trusted/note 等列，幂等）
.venv/bin/python -m scripts.migrate_access_enrichment
```

> 给已存在的表加字段时，`create_all` 不会自动补列 —— 记得在 `migrate_access_enrichment.py`
> 里同步加一条 `ALTER TABLE`，否则线上会报 `no such column`。

> unit 里的 `--timeout-graceful-shutdown 5` 是必需的：SSE 是长连接，不加这个参数时
> `systemctl restart` 会一直等浏览器断开，实测要 70 秒以上。

## 局域网 / 公网访问（HTTPS）

浏览器规定摄像头和麦克风（getUserMedia/MediaRecorder）只在**安全上下文**下可用：localhost 或 HTTPS。因此前端 dev server 已配置为 HTTPS：

- 证书：`frontend/certs/`（自签名，SAN 覆盖 localhost / 127.0.0.1 / 192.168.1.10 / 203.0.113.10，有效期 10 年，已 gitignore）
- 访问地址：`https://192.168.1.10:8040`（局域网）、`https://203.0.113.10:8040`（公网映射）
- 后端只监听 127.0.0.1:8000，由 vite 代理 `/api`，不直接暴露

### 自签名证书的信任问题

首次访问浏览器会弹"不安全"警告，点"高级 → 继续前往"即可使用。但要注意：**部分浏览器（尤其 iOS Safari）对绕过警告的页面仍可能限制摄像头/麦克风**。稳妥做法二选一：

1. 给家人设备安装自签 CA（mkcert 生成 CA + 各设备安装并信任，一次性操作）
2. 正式对外时买个域名解析到 203.0.113.10，用 acme.sh DNS 挑战签免费证书（不需要 80/443 端口）

重新生成证书：`cd frontend/certs && openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 3650 -nodes -subj "/CN=homework-grader" -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:192.168.1.10,IP:203.0.113.10"`

## 运行模式

**生产模式（日常使用，当前方式）**：后端单进程托管一切 —— `npm run build` 产出 `frontend/dist/`，uvicorn 同时提供 API + 静态文件 + SPA 回退，HTTPS 监听 8040：

```bash
cd frontend && npm run build          # 前端有改动后必须重新 build
cd backend && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8040 \
  --ssl-keyfile ../frontend/certs/key.pem --ssl-certfile ../frontend/certs/cert.pem
```

**开发模式（改前端时用）**：后端跑在 8040，另起 `npm run dev`（5173 端口，/api 代理到 8040）。注意 vite HMR 偶发模块缓存错乱（报 "does not provide an export named 'default'"），强刷浏览器即可；生产模式无此问题。

## 生产部署（systemd）

已安装为系统服务，开机自启、崩溃自愈、卡死自愈：

- `homework-grader.service` — 主服务（uvicorn, HTTPS 0.0.0.0:8040），`Restart=always`，内存上限 4G
- `homework-grader-watchdog.service` — 看门狗：每 30s 探测 `/api/health`，连续 2 次失败（进程卡死但端口还在的情况）自动重启主服务
- `homework-grader-routing.service` — 策略路由（192.168.1.10 回程绕开本机代理的 TUN，公网映射必需）
- 密钥在 `/etc/homework-grader/homework-grader.env`（root 600），不在仓库里

unit 文件和看门狗脚本在 `deploy/` 下有存档副本。常用命令：

```bash
sudo systemctl restart homework-grader     # 重启（前端改动需先 npm run build）
sudo journalctl -u homework-grader -f      # 看日志
```

**前端改动生效流程**：`cd frontend && npm run build && sudo systemctl restart homework-grader`

### 环境变量（/etc/homework-grader/homework-grader.env）

```bash
DEEPSEEK_API_KEY=sk-xxx        # 必填，驱动 codex 的模型 key
CODEX_BIN=/usr/local/bin/codex  # 必填：systemd 没有用户 PATH
PATH=/usr/local/bin:/usr/local/bin:/usr/bin:/bin  # 必填：codex 是 node 脚本，需要 node 在 PATH
```

> 教训：systemd 服务的环境变量和用户 shell 完全不同，交互式能跑不代表 systemd 能跑。
