# txt2tts

把任何 `.md` 文件扔进去，**先经 MiniMax M3 标准化**，再调用 **MiniMax speech-2.8-hd**（T2A HTTP）合成 MP3，**原生支持 SRT 字幕**，保存在本地随时重听。

v5 起去掉了"本地 Markdown 清洗"步骤——原先的清洗会把 `#` `*` `>` 列表缩进等全部抹平，反倒让 M3 难以做语义判断。原始 MD 现在一字不动地交给 M3 自己处理。

## 处理流程

```
.md 上传
  │
  ▼
LlmNormalizer.normalize(raw_md)        ← MiniMax M3 标准化（看到原文 # / 代码块 / 表格）
  │   • 替换 URL / 邮箱 / 代码 / 公式 → 口语化
  │   • 长句插入标点
  │   • 控制字数 < 8000
  │   • 保留段落停顿
  │
  ▼
[minimax provider 长文档路径]
LlmNormalizer.split_text(text)         ← M3 语义切分（拆为 ≤10000 字符的多个子文档）
  │   • 仅在 normalized 超过 max_input_chars_per_request 时触发
  │   • 持久化到 outputs/20260704/<task_id>/split_<N>.md
  │
  ▼
[v6 本地清洗] (可选，用户勾选)
TextCleaner.apply_local_clean(chunk_i, options)   ← 8 项规则可勾选
  │   • 删除 URL / 邮箱 / 代码片段 / 表情 / Markdown 符号 / 列表标记 / 引用 / 表格分隔
  │   • 纯本地正则，**覆写** split_<N>.md
  │   • 状态：splitted → local_cleaning → local_cleaned
  │
  ▼
MinimaxTtsClient.synthesize_segment(chunk_i)
  │   POST /v1/t2a_v2  (body 含 output_format=url)
  │   model=speech-2.8-hd, subtitle_enable=true, subtitle_type=sentence
  │   ← data.audio（OSS 临时 URL, 24h 过期）+ data.subtitle_file（OSS URL）
  │   _download_with_retry() 二次 GET → bytes（仅 5xx/429/网络错 5 次指数退避）
  │   subtitle_file 解析为 cues → 渲染 SRT/LRC
  │   持久化到 outputs/20260704/<task_id>/split_<N>.mp3 / .SRT
  │
  ▼
ffmpeg concat (subprocess -c copy)     ← 拼接所有 split_<N>.mp3
  │   写出 outputs/20260704/<task_id>/<task_id>.mp3
  │
  ▼
听文档：GET /api/audio/{task_id}       ← 浏览器 <audio> 读 outputs/20260704/<task_id>/<task_id>.mp3
听文档：GET /api/lyrics/{task_id}.lrc  ← 浏览器音乐播放器模式：LRC 同步高亮
```

## 功能

- 🎧 **「听文档」菜单**（默认进入）：分页列出已成功转语音的 MD 文档（status='done' 的任务），每行点 ▶ 播放进入详情页，随播放高亮当前段
- 📝 **「上传转语音」菜单**：上传 MD 文件后创建后台任务，实时查看进度，完成后可跳转播放
- 🧹 **本地清洗（v6）**：拆分后、转换前可勾选清洗项（删 URL / 邮箱 / 代码 / 表情 / Markdown 符号等 8 项），纯本地正则直接覆写 `split_<N>.md`，**可跳过**
- ⚙️ **「系统设置」**：运行时切换 TTS 方案（**方案一 M3+MiniMax speech-2.8-hd（默认）** / **方案二 M3+edge-tts+ffmpeg**），无需重启
- 🎙️ **双方案 TTS**：方案一用 **MiniMax speech-2.8-hd**（与 M3 同厂商，API key 复用，12 个真实 voice_id，原生 SRT/LRC 字幕）；方案二用 Microsoft **edge-tts**（13 个中文 voice，免费无 key） + 本地 **ffmpeg** 拼接 + 自动生成 SRT 字幕与 LRC 歌词
- 💾 **任务目录布局**：所有产物在 `outputs/<yyyymmdd>/<task_id>/` 下（一个任务一个目录）：
  - `<task_id>.md` 原始 markdown（upload 后立即落盘，v5 起不再做本地清洗）
  - `normalization.md` M3 标准化结果（跳过时复制自 `<task_id>.md`）
  - `split_<N>.md` / `split_<N>.mp3` / `split_<N>.SRT` M3 拆分 + TTS 转换
  - `<task_id>.mp3` / `<task_id>.SRT` / `<task_id>.LRC` ffmpeg 合并后最终播放文件 + 完整字幕
- ⚡ 后台异步任务：上传即返回 task_id，前端轮询进度，不阻塞 UI
- 🔄 **阶段感知重试**（v4 起）：后端根据 status + 已有字段自动决定从哪一阶段续跑：
  - `subtitle_pending`（minimax 字幕拉取失败）→ 整段 convert 重跑（音频仍可用）
  - `error / failed_retryable` + 无 normalized_text → 重新 normalize
  - `error / failed_retryable` + 有 normalized_text 无 split_chunks → 重新 split
  - `error / failed_retryable` + 有 split_chunks → 重新 convert
- 🗑️ **删除确认**：done 状态需输入"确认删除"四个字，其他状态二次确认弹窗
- 📱 移动端友好：所有触摸目标 ≥ 44px；iPhone 安全区适配；菜单/列表/对话框自适应 360~1280 视口

## 快速开始

### 方式一：本地开发（Windows / macOS / Linux）

```powershell
# 1. 装依赖（一次性）
D:\anaconda3\python.exe -m pip install -r requirements.txt

# 2. 配置 API Key（同一 MiniMax 平台，LLM 与 TTS 共用 key）
$env:LLM__API_KEY = "<你的 MiniMax API Key>"          # 用于标准化 + TTS（API key 复用）
# 可选：TTS 用独立 key 时填 MINIMAX__API_KEY
# $env:MINIMAX__API_KEY = "<独立 MiniMax TTS Key>"

# 3. 启动
cd D:\workspace\txt2tts
D:\anaconda3\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 4. 浏览器访问 http://127.0.0.1:8000
```

### 方式二：Docker 部署（推荐生产）

```bash
# 1. 准备环境变量
cp .env.example .env
# 编辑 .env，填入 LLM__API_KEY（与 MiniMax TTS 共用；或独立填 MINIMAX__API_KEY）

# 2. 构建并启动
docker compose up -d --build

# 3. 查看日志
docker compose logs -f txt2tts

# 4. 浏览器访问 http://localhost:8000
# 5. 停止
docker compose down
```

数据持久化：
- `./outputs/`（宿主机） ⇄ `/app/outputs/`（容器）：所有产物 + `library.db`
- `./samples/`（宿主机）⇄ `/app/samples/`（容器，只读）：样例 `.md`

切换 TTS 方案：编辑 `docker-compose.yml` 的 `APP__TTS_PROVIDER`（`minimax` / `edge`），然后 `docker compose up -d --build`。

镜像内已内置静态 ffmpeg / ffprobe（来自 `mwader/static-ffmpeg:7.1.1`），方案二无需再装。

## 真实可用的 voice_id（MiniMax speech-2.8-hd，已验证）

| voice_id | 说明 |
|---|---|
| `male-qn-qingse`             | 中文 · 青涩青年男声 |
| `male-qn-jingying`           | 中文 · 精英青年男声 |
| `male-qn-badao`              | 中文 · 霸道青年男声 |
| `Chinese (Mandarin)_HK_Flight_Attendant` | 港普 · 空少 |
| `female-shaonv`              | 中文 · 少女女声 |
| `female-yujie`               | 中文 · 御姐女声 |
| `female-chengshu`            | 中文 · 成熟女声 |
| `Chinese (Mandarin)_Lyrical_Voice` | 中文 · 抒情女声 |
| `English_Graceful_Lady`      | 英文 · Graceful Lady |
| `English_Insightful_Speaker` | 英文 · Insightful Speaker |
| `English_radiant_girl`       | 英文 · Radiant Girl |
| `English_Persuasive_Man`     | 英文 · Persuasive Man |

UI 会自动列出全部 12 个选项（也可通过 `APP__MINIMAX_VOICES_JSON` 覆盖）。

## 全部环境变量

### 应用层（`APP__*`）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `APP__HOST` | `127.0.0.1` | 监听地址 |
| `APP__PORT` | `8000` | 端口 |
| `APP__OUTPUT_DIR` | `./outputs` | MP3 落盘目录 |
| `APP__MAX_MD_SIZE_KB` | `1024` | 上传 md 大小上限 |
| `APP__MAX_NORMALIZED_CHARS` | `50000` | M3 标准化后文本长度上限 |

### MiniMax M3（`LLM__*`，通过 LangChain `ChatAnthropic` 调用 Anthropic Messages API）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `LLM__API_KEY` | _(空)_ | M3 API Key（也可作为 MiniMax TTS 的回落 key） |
| `LLM__BASE_URL` | `https://api.minimaxi.com/anthropic` | M3 endpoint base（Anthropic 兼容形态） |
| `LLM__MODEL` | `MiniMax-M3` | 模型 id |
| `LLM__MAX_TOKENS` | `8192` | M3 单次最大输出 token |
| `LLM__TEMPERATURE` | `0.2` | 温度 |
| `LLM__REQUEST_TIMEOUT_SEC` | `60.0` | SDK 调用超时 |
| `LLM__MAX_RETRIES` | `2` | 外层重试次数（SDK 内置重试固定为 0） |

### MiniMax speech-2.8-hd TTS（`MINIMAX__*`，默认方案）

> 该模型原生支持句级字幕：`subtitle_enable=true` + `subtitle_type=sentence`。
> 字幕 URL 是 OSS 公开链接，二次 GET 拿到 JSON cues 后渲染 SRT/LRC。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `MINIMAX__API_KEY` | _(空)_ | MiniMax TTS Bearer token；**留空则回落 `LLM__API_KEY`** |
| `MINIMAX__BASE_URL` | `https://api.minimaxi.com` | API base |
| `MINIMAX__T2A_PATH` | `/v1/t2a_v2` | T2A 端点路径 |
| `MINIMAX__MODEL` | `speech-2.8-hd` | 模型 id |
| `MINIMAX__VOICE_ID` | `male-qn-qingse` | 默认 voice（必须在白名单内） |
| `MINIMAX__AUDIO_FORMAT` | `mp3` | `mp3` / `pcm` / `flac` / `wav` 等 |
| `MINIMAX__SAMPLE_RATE` | `32000` | 采样率（8000/16000/22050/24000/32000/44100） |
| `MINIMAX__BITRATE` | `128000` | 比特率（仅 mp3：32000/64000/128000/256000） |
| `MINIMAX__AUDIO_CHANNEL` | `1` | 单声道 / 立体声 |
| `MINIMAX__SPEED` | `1.0` | 语速（0.5~2） |
| `MINIMAX__VOL` | `1.0` | 音量（0~10） |
| `MINIMAX__PITCH` | `0` | 音调（-12~12） |
| `MINIMAX__SUBTITLE_TYPE` | `sentence` | `sentence` / `word` / `word_streaming` |
| `MINIMAX__SUBTITLE_FETCH_TIMEOUT_SEC` | `15.0` | 字幕 URL 二次 GET 超时 |
| `MINIMAX__MAX_INPUT_CHARS_PER_REQUEST` | `10000` | 单次请求字符上限 |
| `MINIMAX__REQUEST_TIMEOUT_SEC` | `180.0` | T2A 主请求超时 |
| `MINIMAX__MAX_RETRIES` | `2` | 5xx 重试次数 |

### Edge-tts 备选方案（`EDGE__*`，APP__TTS_PROVIDER=edge 时生效）

容器里 ffmpeg 已被镜像内置；本地开发用 `./bin/ffmpeg.exe`：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `EDGE__FFMPEG_PATH` | `/usr/local/bin/ffmpeg` | ffmpeg 路径 |
| `EDGE__FFPROBE_PATH` | `/usr/local/bin/ffprobe` | ffprobe 路径 |
| `EDGE__DEFAULT_VOICE` | `zh-CN-XiaoxiaoNeural` | 默认中文 voice |
| `EDGE__RATE` / `EDGE__VOLUME` / `EDGE__PITCH` | `+0%` / `+0%` / `+0Hz` | 语速 / 音量 / 音调 |
| `EDGE__MAX_SEGMENT_CHARS` | `200` | 每段最长字符数 |
| `EDGE__REQUEST_TIMEOUT_SEC` | `30.0` | 单段超时 |
| `EDGE__MAX_RETRIES` | `3` | 瞬时网络错误重试 |
| `EDGE__RETRY_BACKOFF_SEC` | `1.0` | 指数退避起始 |
| `EDGE__FFPROBE_TIMEOUT_SEC` / `EDGE__FFMPEG_CONCAT_TIMEOUT_SEC` / `EDGE__MIMO_FFMPEG_CONCAT_TIMEOUT_SEC` | `10.0` / `120.0` / `600.0` | 子进程超时 |

`.env` 文件由 `app/main.py` 中的 `dotenv` 自动加载（**不**使用 `SettingsConfigDict(env_file=...)`）。

启动时此环境变量决定初始 provider；运行时可通过 `PATCH /api/settings` 覆盖，结果写入 SQLite `app_settings` 表（跨进程保留）。

## 目录结构

```
txt2tts\
├── app\
│   ├── config.py                  # AppSettings / LlmSettings / MinimaxTtsSettings / EdgeTtsSettings + M3_SYSTEM_PROMPT
│   ├── main.py                    # FastAPI 入口（lifespan 装配服务）
│   ├── routers\tts.py             # REST 端点
│   ├── services\
│   │   ├── （v5 起删除 markdown_service.py：原始 MD 直接送 M3，不再本地清洗）
│   │   ├── llm_normalizer.py      # MiniMax M3（通过 LangChain ChatAnthropic）
│   │   ├── minimax_tts_provider.py # MiniMax speech-2.8-hd（v3 OSS URL 下载 + v4 5 次指数退避 + 字段名容错）
│   │   ├── audio_storage.py       # task_dir + TaskStore + SettingsStore（无 LibraryStore / 无 promote_artifacts）
│   │   ├── task_manager.py        # 后台任务编排（v4 阶段感知重试 + subtitle_pending）
│   │   ├── task_watchdog.py       # 后台任务自检
│   │   ├── edge_tts_provider.py   # edge-tts 备选方案 + ffmpeg 合并 + SRT/LRC 转换
│   │   ├── lrc_parser.py          # LRC 解析器（前端 JS 镜像）
│   │   └── pipeline.py            # 编排辅助（minimax 6 步 / edge 5 步）
│   ├── models\schemas.py         # Pydantic DTO（含 LibraryItemDto / LibraryDetailDto）
│   └── static\                   # 前端（index.html / app.js / styles.css）
├── samples\demo.md
├── outputs\                      # 运行时产物（已被 gitignore）
│   ├── 20260704\                  # 一个日期一个目录（"yyyyMMdd"）
│   │   └── <task_id>\             # 一个任务一个目录
│   │       ├── <task_id>.md       # ① 原始 MD（v5 起一字不动落盘）
│   │       ├── normalization.md   # ② M3 标准化结果
│   │       ├── split_<N>.md       # ③ M3 拆分
│   │       ├── split_<N>.mp3      # ④ TTS 转换
│   │       ├── split_<N>.SRT      # ④ TTS 逐段字幕
│   │       ├── <task_id>.mp3      # ⑤ ffmpeg 合并（最终播放文件）
│   │       ├── <task_id>.SRT      # ⑤ 完整字幕（累积偏移）
│   │       └── <task_id>.LRC      # ⑥ LRC 歌词
│   └── library.db                 # SQLite（tasks + app_settings 两张表）
├── tests\                        # pytest + respx mock（asyncio_mode=auto）
├── run_e2e_minimax.py            # 真实 API 端到端脚本（M3 + MiniMax TTS）
├── requirements.txt
├── pytest.ini
├── Dockerfile                     # 多阶段构建（mwader/static-ffmpeg + python:3.12-slim）
├── docker-compose.yml             # 单服务编排
├── .env.example                   # 环境变量模板
└── .dockerignore
```

## 测试

```bash
D:\anaconda3\python.exe -m pytest tests/ -v
```

预期：**221 passed**（v4 重构后）。分布：
- 4 markdown + 10 llm_normalizer + 37 minimax_tts_provider + 12 pipeline（minimax 集成）
- 12 pipeline_minimax（含 subtitle_pending + 阶段感知重试）+ 4 pipeline_edge
- 30 task_manager / task_store（含 v4 TaskRecord）+ 15 task_watchdog + 16 test_task_dir_layout
- 22 lrc_parser + 17 config_accessors + 4 edge_provider
- 2 e2e_step_flow（真实 HTTP 端到端 + 6 步产物验证）
- 其他 helpers

`test_library.py` 已删除（v4 无 LibraryStore）；`test_task_delete.py` 已删除（合并到 `test_task_dir_layout.py`）。

## 真实端到端验证

```bash
# 设置 MiniMax API Key（LLM 与 TTS 共用）
set LLM__API_KEY=<your MiniMax API key>

# 跑端到端
D:\anaconda3\python.exe run_e2e_minimax.py
```

会：
1. 读 `samples/demo.md` → 直接喂 M3（v5 起无本地清洗）
2. 调真实 M3 标准化
3. 调真实 MiniMax speech-2.8-hd TTS（OSS URL 下载 + 字幕 JSON 拉取）
4. 保存到 `outputs/<YYYYMMDD>/<uuid>/<uuid>.mp3`（v4：任务目录布局）
5. 校验文件大小和 magic bytes
6. 调 Windows 默认播放器播放

## 关于 MiniMax API 字段

- **MiniMax M3**（Anthropic Messages API，**通过 LangChain `ChatAnthropic`**）
  - `client = ChatAnthropic(model=..., api_key=..., base_url=LLM__BASE_URL, max_tokens=..., temperature=..., timeout=..., max_retries=0)`
  - `await client.ainvoke([SystemMessage(content=M3_SYSTEM_PROMPT), HumanMessage(content=text)])`
  - SDK 内部按 Anthropic Messages 协议拼装 `POST {base_url}/v1/messages`，鉴权头与 `anthropic-version` 由 SDK 注入
  - 响应取 `resp.content`（已解析的文本）
- **MiniMax T2A speech-2.8-hd**（HTTP 多模态）
  - `POST https://api.minimaxi.com/v1/t2a_v2`
  - Header: `Authorization: Bearer <MINIMAX__API_KEY 或 LLM__API_KEY>`
  - Body: `model=speech-2.8-hd`, `text=...`, `voice_setting={voice_id, speed, vol, pitch}`, `audio_setting={sample_rate, bitrate, format, channel}`, `subtitle_enable=true`, `subtitle_type=sentence`
  - 响应：`data.audio`（hex 编码 mp3 字节） + `data.subtitle_file`（OSS 公开 URL，含签名 token，TTL 通常几分钟）
  - 字幕：二次 GET `data.subtitle_file` → 解析 JSON cues（毫秒单位）→ 渲染 SRT/LRC
  - 字段名容错：`text`/`sentence_text`/`sentence` + `start_time`/`begin_time`/`start` + `end_time`/`finish_time`/`end`

## API 端点

| Method | Path | 说明 |
|---|---|---|
| `GET`  | `/`                       | 跳转到 UI |
| `GET`  | `/static/index.html`      | 前端 UI |
| `GET`  | `/api/health`             | 健康检查 + M3/TTS 配置状态 |
| `GET`  | `/api/voices`             | 语音列表（按 active provider 返回） |
| `POST` | `/api/preview`            | 跑 M3 标准化，返回 normalized 文本 |
| `POST` | `/api/tasks`              | 上传 MD 创建后台转语音任务（返回 task_id） |
| `GET`  | `/api/tasks`              | 分页列出转语音任务（`?page=N&size=20`） |
| `GET`  | `/api/tasks/{task_id}`    | 查询单条任务详情（含 `date_str` / `normalized_text` / `split_chunks`） |
| `POST` | `/api/tasks/{task_id}/normalize` | 触发 M3 标准化（仅 `draft`） |
| `POST` | `/api/tasks/{task_id}/skip-normalize` | 跳过标准化（复制 `<task_id>.md` → `normalization.md`） |
| `POST` | `/api/tasks/{task_id}/split` | 触发 M3 拆分（body: `{"prompt": "..."}`） |
| `POST` | `/api/tasks/{task_id}/confirm-split` | 确认子文档（body 可选 `{"chunks": [...]}`） |
| `POST` | `/api/tasks/{task_id}/skip-split` | 跳过拆分（复制 `normalization.md` → `split_1.md`） |
| `POST` | `/api/tasks/{task_id}/local-clean` | v6 触发本地清洗（body: `{"options": ["url","email",...]}`，仅 `splitted`） |
| `POST` | `/api/tasks/{task_id}/skip-local-clean` | v6 跳过本地清洗（`splitted` → `ready_to_convert`） |
| `POST` | `/api/tasks/{task_id}/convert` | 启动 TTS 转换（仅 `ready_to_convert`） |
| `POST` | `/api/tasks/{task_id}/retry` | 阶段感知重试（`error` / `failed_retryable` / `subtitle_pending` / `local_cleaning`） |
| `DELETE` | `/api/tasks/{task_id}`  | 删除任务（rmtree task_dir + 删 tasks 行） |
| `GET`  | `/api/split-presets`     | 内置拆分提示词列表（`chapter` / `qa` / `topic`） |
| `GET`  | `/api/normalize-presets` | 内置标准化提示词列表（`default` / `minimal` / `verbatim`） |
| `GET`  | `/api/clean-options`     | v6 本地清洗项元数据（8 项清洗规则） |
| `GET`  | `/api/audio/{task_id}`   | 流式播放 `task_dir/<task_id>.mp3` |
| `GET`  | `/api/storage/stats`      | outputs/ 占用统计 |
| `GET`  | `/api/library`            | 听文档列表（`status='done'` 的任务，`?page=N&size=10`） |
| `GET`  | `/api/library/{task_id}` | 听文档详情（task_id 当 key，文件路径从 task_record.date_str 拼） |
| `GET`  | `/api/lyrics/{task_id}.lrc` | 下载 `task_dir/<task_id>.LRC` |
| `GET`  | `/api/settings`           | 当前 TTS provider / 可用列表 / edge voices |
| `PATCH` | `/api/settings`          | 运行时切换 TTS provider（`{tts_provider: "minimax" \| "edge"}`） |
| `GET`  | `/docs`                   | FastAPI Swagger UI |

## 待办

- [ ] 长文本自动分片（M3 单次输出上限 8k token；TTS 上限 50k 字符自动按 max_chars 切分）
- [x] **SRT 字幕导出**（v4 起 minimax provider 原生支持，subtitle_file → SRT/LRC 落盘到 `task_dir/<task_id>/<task_id>.{SRT,LRC}`）
- [ ] 文件夹批量朗读
- [ ] 多角色对话朗读
- [ ] 打包成 .exe / Docker 镜像