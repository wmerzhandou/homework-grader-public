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

> **结果与产出物的两条硬规则**（2026-09-15 修正）：
> 1. **结构化结果只在"本轮写过"时才挂载**：每轮开始前记录 `grading_result.json` / `english_result.json`
>    的 mtime+size+hash，只有本轮被写过才算这一轮的结果，否则同一张批改卡片会重复出现在后续每条回复上；
>    两个文件都写过则以 grading 优先。
> 2. **模型产出的文件登记为 Artifact**（`artifact` 表）：每轮结束对比工作区快照，找出本轮新增/被修改的文件
>    （排除用户上传原文件、`keyframes/`、结果 JSON、`.git`/`.codex` 等），随消息返回，
>    前端渲染成播放器/缩略图/下载卡片；正文里指向服务器路径的 Markdown 链接会降级成文件名。

### 产出物的工程细节（2026-09-15 补强）

- **不再静默丢弃**：超过 `ARTIFACT_HARD_MAX_BYTES` 的文件会在回复末尾明确列出文件名与大小；
  超过 `ARTIFACT_PREVIEW_MAX_BYTES` 的产出物标记 `oversized`，界面只给下载、不做内嵌预览。
- **浏览器友好预览副本**：后台任务（`app/media_prep.py`）为图片生成缩放副本（最长边
  `ARTIFACT_IMAGE_MAX_DIM`）、为视频转 H.264/AAC + faststart，存为 `<stem>.web.jpg` / `<stem>.web.mp4`；
  **原文件始终保留**，`?preview=1` 取副本、不带参数取原文件。完成后发 SSE `artifact_ready` 通知前端。
- **中间产物不进聊天窗口**：逐帧命名（`frame_0001.png`、`0007.jpg`、`seg03.mp4`）与
  `.tmp/`、`tmp/`、`frames/`、`segments/`、`cache/` 等目录一律忽略；提示词也要求模型把中间产物写进 `.tmp/`。
- **配额含产出物**：每用户存储配额同时统计 Material 与 Artifact（含预览副本），防止生成的视频绕过配额。
- **删除会话连带清理**：删除会话会同时删掉 message / material / artifact 行再删 thread
  （漏删任何一类都会因外键约束直接 500 —— 曾经踩过）。
- 产出物下载接口只接受**绝对路径**且在会话工作区内，防越权与路径歧义。

### 让模型"生成"媒体（2026-09-15）

codex 的 shell 在沙箱里**没有网络**（连 127.0.0.1 也连不上），所以模型无法自己调 TTS 或 mmx。
约定用**请求文件**发起生成：模型在工作目录根下写隐藏 JSON，后端在这一轮结束时执行，
产物落回工作区 → 由产出物链路送到聊天窗口与详情页。

| 请求文件 | 内容 | 后端执行 |
| --- | --- | --- |
| `.tts_request.json` | `{"text": "…", "filename": "朗读.wav", "speed": 1.0}` | 默认调本机 TTS-Story `/api/preview`，引擎 `index_tts`（配置为 **IndexTTS-2.5**）；按 `text+voice+speed` 哈希缓存到 `data/tts_cache` |
| `.tts_request.json`（选音色） | `{"text": "…", "engine": "mmx", "voice": "female-tianmei", "emotion": "happy", "filename": "朗读.mp3"}` | 走 `mmx speech synthesize`（云端音色库，几十个中文音色 + emotion），约 1-3 秒 |
| `.image_request.json` | `{"prompt": "…", "filename": "插图.jpg", "aspect_ratio": "1:1", "n": 1}` | 调 `mmx image generate`（MiniMax image-01）；`n` 最多 4 张；按魔数纠正扩展名（mmx 实际输出 JPEG） |
| `.video_request.json` | `{"prompt": "…", "filename": "成片.mp4", "seconds": 5, "resolution": "480p", "aspect_ratio": "16:9", "reference": "可选参考图文件名"}` | 提交到 H3 农场（ComfyUI `/prompt` → 轮询 `/history` → `/view` 取回）；带 `reference` 时走 R2V（先把图上传到 ComfyUI input） |
| `.render_request.json` | `{"composition": "explain.html", "output": "讲解视频.mp4", "quality": "draft|high", "narration": {"text": "…", "voice": "default"}}` | **讲解视频**：HTML 动画（HyperFrames）→ 后端渲染成 mp4；旁白可现合成，也可用 `"audio": "已有配音.wav"` |
| `.manim_request.json` | `{"script": "explain_math.py", "scene": "MathLesson", "output": "数学讲解.mp4", "quality": "draft|high", "narration": {"text": "…"}}` | **数学动画**：Manim（真 LaTeX 排版公式）→ 后端渲染成 mp4 并合入旁白音轨 |

实现位置：`app/generation.py`（执行）+ `codex_service/context.py`（写给模型的契约）+ `manager.py`（每轮结束后、产出物扫描前调用）。
失败不会中断回合：请求文件一律删除，并在回复末尾追加 `⚠️ …` 说明。

> 已知限制：IndexTTS 当前音色由 TTS-Story 的 `index_tts_default_prompt` 决定（默认 `data/voice_prompts/duhongyang.m4a`），
> `/api/preview` 对 `index_tts` 不透传自定义参考音频，所以 `voice` 字段目前是空操作。要支持"用某段录音当音色"，
> 需要给 TTS-Story 的 preview 路由加上 `spk_audio_prompt` 透传（那是另一个项目，需单独确认）。
> 想要多音色时用 `"engine": "mmx"`（已验证可用，不受这条限制）。

### 视频走哪条路（2026-09-17 起）

**讲解/教学/图解类视频走 HTML 动画渲染，不要用 AI 生成镜头。**

| 用户要的 | 路径 | 请求文件 |
| --- | --- | --- |
| 讲题、知识点、步骤演示、图解、带字幕的口播讲解 | **HyperFrames HTML 动画渲染**（帧级可控，字幕与旁白严格对齐） | `.render_request.json` |
| 数学推导、公式变形、几何证明、数轴/数形结合（需要严格排版公式） | **Manim 数学动画**（真 LaTeX：`\frac{}{}`、`\sqrt{}`、上下标） | `.manim_request.json` |
| 写实画面、氛围镜头、真实场景短片 | AI 生成视频（H3 农场） | `.video_request.json` |

渲染在后端执行（沙箱没有网络，而 HyperFrames 工程默认引 CDN、`npx` 也可能要联网）：

1. 后端合成旁白 → 探测时长 → **改写组合根节点的 `data-duration`**（渲染时长由静态 HTML 决定）
   并把 `narrationDuration` 作为变量传给组合的 JS；
2. 组合里没写 `<audio>` 时**自动补一个根节点下的音轨**（框架只认直接子元素）；
3. 组合里若引了 CDN 的 GSAP，自动改写为工程内的 `assets/gsap.min.js`；
4. `hyperframes render --quality draft|high` → 成片（+ 配音）写回会话工作区，走产出物链路交付。

前置：本机需安装 `hyperframes`（`npm install -g hyperframes`，已验证 0.8.44；无头 Chrome 与 ffmpeg 已具备）。
模板与骨架：`backend/app/render_template/`（骨架已按"旁白时长驱动"写好，可直接复制改内容）。
模型的技能：`$CODEX_HOME/skills/explainer-video`（自研，含路由与契约）+ 软链的
`hyperframes` / `hyperframes-core` / `hyperframes-animation` / `hyperframes-creative` /
`hyperframes-cli` / `motion-graphics` / `ffmpeg` / `beautiful-mermaid`。

**数学动画（Manim）通道**（2026-09-17 起，`app/manim_render.py`）：

公式推导/几何证明这类内容 Manim 比 HTML 精致得多，所以单独开一条路：

1. 后端先合成旁白（慢速 + 留白 + 音色配置同讲解视频）；
2. 把**逐句时间轴**写成脚本同目录的 `narration_meta.json`
   （`{"duration": …, "fps": 30, "resolution": [w, h], "segments": [{text, start, end}]}`）；
3. `manim render -r W,H --fps 30 --media_dir <临时>` 渲染（draft=1280×720，high=1920×1080）；
4. 找到成片 → 用 ffmpeg 合入旁白音轨：**成片比旁白短就冻结最后一帧补齐**（不裁画面），
   画面够长则 `-c:v copy` 不重编码；成片 + 配音一起走产出物交付。

脚本按 `segments[i].start/end` 排动画（模板 `wait_until()`），所以"话没说完画面就切走"不会发生。
前置：本机装 Manim + LaTeX（`~/.local/venvs/manim` + `texlive-latex-base/extra`、`dvisvgm`、`libcairo2-dev`、`libpango1.0-dev`）。
模板：`backend/app/manim_template/explain_scene.py`。

### 技能接入策略（2026-09-17）

技能库来自 OpenMontage（`SKILL_LIBRARY_DIR`，默认 `/home/user/.agents/skills`，约 100 个）。
**精选注册 + 整库只读可查**，不把 100 个全注册：

- **精选 ~20 个**软链进 `$CODEX_HOME/skills`（codex 从这里发现技能，名字+描述会进提示词）：
  HyperFrames 全家（含 `-keyframes` / `-audio` / `-registry`）、`beautiful-mermaid`、`d3-viz`、
  `manim-composer`、`manimce-best-practices`、`gsap-core`、`gsap-timeline`、
  `svg-character-animation`、`canvas-procedural-animation`、`ffmpeg`、`video-understand`、
  `visual-style`、`motion-graphics`，加上本项目的 `explainer-video`；
- **整库软链成 `$CODEX_HOME/skill-library`**：不注册、零提示词成本，模型需要更深规范时按路径自己读；
- **降级规则写进了提示词**：技能里要求联网 / API key / 云端或农场运行时（HeyGen、fal、Azure、
  ElevenLabs、Lambda、ComfyUI 农场等）的，一律不走，改走本项目自己的请求文件，或直接说明不可用。

为什么不全注册：100 个技能的名字+描述**每轮**注入提示词约 **37 KB**，还会让模型在无关工作流里乱挑；
实测库里只有 4/100 提到密钥/外部 API，所以"知识"部分离线可用，值得精选接入。

技能库换位置时链接会自动纠正（`ensure_skills` 发现软链指向别的库会重链）。

**视频规格**（实测 H3 农场输出）：864x480 / 24fps / H.264 + AAC（自带音频），约 445KB/3 秒；
480p 草稿在农场热的时候 15 秒左右出片，冷机或 768p 会到几分钟。进度通过 SSE `generation_status`
实时显示在"🛠 处理中"那一行。

## 本地启动

### 跨会话引用（2026-09-15）

每个会话有一个**会话码**（4 位、排除易混字符，如 `#K7M2`），显示在会话列表与会话页标题栏，
点一下即复制。在任意会话里写 `#K7M2`（可多个、大小写不敏感）即可引用那个会话的上下文。

**注入策略——只给索引与路径，内容由模型按需读取**（沙箱允许读整个文件系统，所以不需要复制文件）：

- **材料索引**：文件名 / 类型 / 用途 / 原文件绝对路径 / 可读文本路径；
- **对话索引**：被引用会话的**每条消息取首行**（默认 120 字），整块上限 `REFERENCE_INDEX_MAX_CHARS`；
- **批改结果索引**：总评首行 + 知识点 + 错题题号（不搬全文）；
- **图片**：最多 `REFERENCE_IMAGE_LIMIT`（默认 3）张作为图片输入，其余的只给路径。

**让"路径"真正可读**：预处理会把可读文本落盘到会话工作区的 `.derived/`：
文档 → markitdown 全文 markdown，音频/视频 → ASR 全量转写 txt；图片不需要（以图片输入给）。
该目录以 `.` 开头，**不会被登记成产出物**。历史材料用 `scripts/backfill_reference_data.py` 补齐。

权限与边界：**只能引用自己的会话**；码不存在或不是自己的会话 → 回复里明确提示；
被引用会话删除后引用自动失效；不复制文件（零占用、零配额）。前端会在消息上显示
「引用 #K7M2《标题》」并可点击跳转。

**前端交互（第二批）**：
- 会话列表与标题栏的会话码**点一下即复制**；
- 输入框左下角的 **🔗 引用会话** 按钮打开选择器：按标题/码搜索，显示每个会话的「N 份材料 · M 条对话」，点选即把 `#码` 插进输入框；
- **发送前预览**：输入框上方出现 chip——「将引用 #GU47《…》· 1 份材料 · 4 条对话」；码写错则显示「找不到会话 #ZZZZ」；点 ✕ 可移除该引用。

```bash
# 老库补齐会话码与可读文本（幂等，可先 --dry-run 看会做什么）
cd backend && .venv/bin/python -m scripts.backfill_reference_data
```

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
| `ARTIFACT_PREVIEW_MAX_BYTES` | `41943040`（40MB） | 产出物超过这个大小就不做内嵌预览，只给下载入口 |
| `ARTIFACT_HARD_MAX_BYTES` | `1073741824`（1GB） | 产出物的硬上限；超过则不登记，并在回复里**明确写出**文件名与大小（不再静默丢弃） |
| `ARTIFACT_IMAGE_MAX_DIM` | `2000` | 图片预览副本缩放后的最长边 |
| `MMX_BIN` | `~/.local/bin/mmx` | mmx CLI 路径（systemd 的 PATH 里没有 `~/.local/bin`，所以显式配置） |
| `MMX_TIMEOUT_S` | `300` | 单张图片生成超时 |
| `TTS_GENERATION_TIMEOUT_S` | `300` | 一段语音合成超时 |
| `COMFYUI_SERVER_URL` | `http://127.0.0.1:8188` | H3 视频农场（ComfyUI HTTP API） |
| `H3_T2V_WORKFLOW` | `/opt/openmontage/assets/workflows/h3_t2v_api.json` | 文生视频工作流模板 |
| `H3_R2V_WORKFLOW` | `/opt/openmontage/assets/workflows/h3_r2v_api.json` | 参考图生视频模板 |
| `VIDEO_GENERATION_TIMEOUT_S` | `1200` | 单条视频生成超时 |
| `HYPERFRAMES_BIN` | `~/.local/bin/hyperframes` | 讲解视频渲染用的 HyperFrames CLI |
| `HF_TEMPLATE_DIR` | `backend/app/render_template` | 渲染工程模板（配置 + 本地 GSAP + 组合骨架） |
| `RENDER_TIMEOUT_S` | `1800` | 单次渲染超时 |
| `NARRATION_ENGINE` | `index_tts` | 讲解旁白的默认引擎：`index_tts`（本机免费）/ `mmx`（MiniMax 云端音色库） |
| `NARRATION_VOICE` | `default` | 讲解旁白的默认音色（mmx 时写音色名，如 `female-chengshu`） |
| `NARRATION_SPEED` | `0.85` | 讲解旁白语速（<1 更慢；不同音色天生语速不同，换音色时一起调） |
| `NARRATION_LOUDNESS_LUFS` | `-16` | 旁白目标响度（两遍 loudnorm 归一；填 0 关闭）——不同音色原始音量能差 9dB，统一它才不会忽大忽小 |
| `MANIM_BIN` | `~/.local/venvs/manim/bin/manim` | 数学动画用的 Manim CLI（独立 venv，不掺进后端依赖） |
| `MANIM_TIMEOUT_S` | `1800` | 单次数学动画渲染超时 |
| `SKILL_LIBRARY_DIR` | `/home/user/.agents/skills` | OpenMontage 技能库（精选软链进 skills，整库软链成 skill-library） |
| `REFERENCE_MESSAGE_HEAD_CHARS` | `120` | 引用会话时，对话索引里每条消息取多少字 |
| `REFERENCE_INDEX_MAX_CHARS` | `6000` | 引用索引整块上限（超出会截断并注明） |
| `REFERENCE_IMAGE_LIMIT` | `3` | 引用会话时最多带入几张图片（其余只给路径） |

## HTTP API

所有 `/api/*`（除 register/login/health）需要 `Authorization: Bearer <token>`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/auth/register` | `{username, password, invite_code}` → `{token, user:{id, username}}` |
| POST | `/api/auth/login` | `{username, password}` → 同上 |
| GET | `/api/auth/me` | 当前用户 |
| POST | `/api/auth/logout` | 吊销当前 token（真正登出，服务端删除 token） |
| POST | `/api/auth/media-ticket` | 签发材料下载短时票据 `{ticket, expires_in}` |
| POST | `/api/threads/reference-preview` | 发送前预览：`{text}` → `{references:[{code,thread_id,title,material_count,message_count}], unresolved:[码]}` |
| POST | `/api/threads` | `{title?}` → thread |
| GET | `/api/threads` | 当前用户 thread 列表 |
| GET | `/api/threads/{id}` | thread + 消息历史 + 材料列表 |
| POST | `/api/threads/{id}/materials` | multipart：多个 `files` 字段 + 可选 `purposes`（JSON 数组，与文件一一对应）→ `{materials:[{id, filename, kind, status}]}`；kind ∈ `document\|image\|audio\|video`，status ∈ `processing\|ready\|failed` |
| POST | `/api/threads/{id}/turns` | `{text, material_ids?}` → `{turn_id}`，异步执行 codex turn |
| GET | `/api/threads/{id}/events` | SSE 实时事件流 |
| GET | `/api/materials/{id}/file` | 下载原始文件；凭据用 `Authorization: Bearer` 或 `?ticket=`（短时票据） |
| GET | `/api/threads/{id}/artifacts/{artifact_id}/file` | 【仅会话所有者】下载/播放模型产出的文件（朗读音频、裁好的图片等），鉴权同材料下载 |
| — | — | 加 `?preview=1` 取"浏览器友好"的预览副本（图片缩放 / 视频转 H.264）；不加则给原文件（下载用） |
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

# 产出物与历史数据维护
.venv/bin/python -m scripts.backfill_artifacts --dry-run   # 补登历史会话里模型已产出但未登记的文件
.venv/bin/python -m scripts.cleanup_stale_results --dry-run # 清掉历史里重复挂载的同一份结构化结果
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
