# txt2tts

把任何 `.md` 文件扔进去，**先经 MiniMax M3 标准化**，再调用 **小米 MiMo (mimo-v2.5-tts)** 合成 MP3，保存在本地随时重听。

## 处理流程

```
.md 上传
  │
  ▼
MarkdownService.to_plain_text()        ← 本地基础清洗（去 #、代码块、表格等）
  │
  ▼
LlmNormalizer.normalize(text)          ← MiniMax M3 标准化
  │   • 替换 URL / 邮箱 / 代码 / 公式 → 口语化
  │   • 长句插入标点
  │   • 控制字数 < 8000
  │   • 保留段落停顿
  │
  ▼
[mimo provider 长文档路径]
LlmNormalizer.split_text(text)         ← M3 语义切分（拆为 ≤4500 字符的多个子文档）
  │   • 仅在 normalized 超过 max_input_chars_per_request 时触发
  │   • 持久化 outputs/chunks/<id>/001.md / 002.md / ...
  │
  ▼
TtsClient.synthesize(chunk_i)          ← 小米 MiMo mimo-v2.5-tts 逐段合成
  │   POST /v1/chat/completions
  │   modalities=["text","audio"]
  │   audio={voice: <voice>, format: "mp3"}
  │   ← choices[0].message.audio.data (base64 mp3)
  │   持久化 outputs/chunks/<id>/001.mp3 / 002.mp3 / ...
  │
  ▼
ffmpeg concat (subprocess -c copy)     ← 拼接所有子 mp3
  │   写出 outputs/chunks/<id>/final.mp3
  │
  ▼
AudioStorageService.save(bytes)        ← 把 final.mp3 写到 outputs/audio/<audio_id>.mp3（v2 统一目录）
  │
  ▼
promote_artifacts()                    ← 把 chunks/segments/uploads.md 移到 outputs/audio/_artifacts/<audio_id>/
  │
  ▼
GET /api/audio/{audio_id}              ← 浏览器 <audio> 流式播放（从 outputs/audio/<id>.mp3 读）
```

## 功能

- 🎧 **「听文档」菜单**（默认进入）：分页列出已成功转语音的 MD 文档，每行点 ▶ 播放进入详情页，随播放高亮当前段
- 📝 **「上传转语音」菜单**：上传 MD 文件后创建后台任务，实时查看进度，完成后可跳转播放
- 🎵 **「转歌词」**：每条听文档可一键让 M3 把 normalized_md 改写成 LRC 同步歌词
- ⚙️ **「系统设置」**：运行时切换 TTS 方案（**方案一 M3+MiMo** / **方案二 M3+edge-tts+ffmpeg**），无需重启
- 🎙️ **双方案 TTS**：方案一用小米 MiMo `mimo-v2.5-tts`（9 个 voice）；方案二用 Microsoft **edge-tts**（13 个中文 voice，免费无 key） + 本地 **ffmpeg** 拼接 + 自动生成 SRT 字幕与 LRC 歌词
- 💾 MP3 持久化到 `outputs/audio/<audio_id>.mp3`（v2 统一目录），edge provider 顺带把 SRT/LRC 落到 `outputs/audio/_artifacts/<audio_id>/`，元数据写入 SQLite `outputs/library.db`
- ⚡ 后台异步任务：上传即返回 task_id，前端轮询进度，不阻塞 UI
- 📱 移动端友好：所有触摸目标 ≥ 44px；iPhone 安全区适配；菜单/列表/对话框自适应 360~1280 视口

## 快速开始

### 方式一：本地开发（Windows / macOS / Linux）

```powershell
# 1. 装依赖（一次性）
D:\anaconda3\python.exe -m pip install -r requirements.txt

# 2. 配置双服务凭证
$env:LLM__API_KEY = "<你的 MiniMax M3 API Key>"        # 用于标准化
$env:TTS__API_KEY = "<你的 小米 MiMo API Key>"        # 用于语音合成

# 3. 启动
cd D:\workspace\txt2tts
D:\anaconda3\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 4. 浏览器访问 http://127.0.0.1:8000
```

### 方式二：Docker 部署（推荐生产）

```bash
# 1. 准备环境变量
cp .env.example .env
# 编辑 .env，填入 LLM__API_KEY / TTS__API_KEY（方案一必需）

# 2. 构建并启动
docker compose up -d --build

# 3. 查看日志
docker compose logs -f txt2tts

# 4. 浏览器访问 http://localhost:8000
# 5. 停止
docker compose down
```

数据持久化：
- `./outputs/`（宿主机） ⇄ `/app/outputs/`（容器）：MP3 / SRT / LRC / `library.db`
- `./samples/`（宿主机）⇄ `/app/samples/`（容器，只读）：样例 `.md`

切换 TTS 方案：编辑 `docker-compose.yml` 的 `APP__TTS_PROVIDER`（`mimo` / `edge`），然后 `docker compose up -d --build`。

镜像内已内置静态 ffmpeg / ffprobe（来自 `mwader/static-ffmpeg:7.1.1`），方案二无需再装。

## 真实可用的 voice_id（小米 MiMo，已验证）

| voice_id | 说明 |
|---|---|
| `mimo_default` | 默认女声 |
| `冰糖`         | 中文女声 |
| `茉莉`         | 中文女声 |
| `苏打`         | 中文女声 |
| `白桦`         | 中文男声 |
| `Mia`          | 英文 |
| `Chloe`        | 英文 |
| `Milo`         | 英文 |
| `Dean`         | 英文 |

UI 会自动列出全部 9 个选项。

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
| `LLM__API_KEY` | _(空)_ | M3 API Key（由 LangChain SDK 作为鉴权凭据发送） |
| `LLM__BASE_URL` | `https://api.minimaxi.com/anthropic` | M3 endpoint base（Anthropic 兼容形态） |
| `LLM__MODEL` | `MiniMax-M3` | 模型 id |
| `LLM__MAX_TOKENS` | `8192` | M3 单次最大输出 token |
| `LLM__TEMPERATURE` | `0.2` | 温度 |
| `LLM__REQUEST_TIMEOUT_SEC` | `60.0` | SDK 调用超时 |
| `LLM__MAX_RETRIES` | `2` | 外层重试次数（SDK 内置重试固定为 0） |

### 小米 MiMo TTS（`TTS__*`，chat/completions 多模态）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `TTS__API_KEY` | _(空)_ | MiMo Bearer token |
| `TTS__BASE_URL` | `https://api.xiaomimimo.com` | API base |
| `TTS__CHAT_PATH` | `/v1/chat/completions` | 路径（注意：TTS 走 chat 端点！） |
| `TTS__MODEL` | `mimo-v2.5-tts` | TTS 模型 |
| `TTS__VOICE` | `mimo_default` | 默认 voice |
| `TTS__AUDIO_FORMAT` | `mp3` | `mp3` 或 `wav` |
| `TTS__TEMPERATURE` | `0.6` | 采样温度 |
| `TTS__REQUEST_TIMEOUT_SEC` | `90.0` | 超时 |
| `TTS__MAX_RETRIES` | `2` | 5xx 重试次数 |

### Edge-tts 方案二（`EDGE__*`，可选）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `EDGE__FFMPEG_PATH` | `./bin/ffmpeg.exe` | ffmpeg 可执行文件路径 |
| `EDGE__FFPROBE_PATH` | `./bin/ffprobe.exe` | ffprobe 路径（探测片段时长） |
| `EDGE__DEFAULT_VOICE` | `zh-CN-XiaoxiaoNeural` | 默认中文 voice |
| `EDGE__RATE` | `+0%` | 语速 edge-tts 形如 `+10%` / `-10%` |
| `EDGE__VOLUME` | `+0%` | 音量 |
| `EDGE__PITCH` | `+0Hz` | 音调 |
| `EDGE__MAX_SEGMENT_CHARS` | `200` | 每段最长字符数（超过则按句号切碎） |
| `EDGE__REQUEST_TIMEOUT_SEC` | `30.0` | 单段超时 |
| `EDGE__MAX_RETRIES` | `3` | edge-tts 瞬时网络错误重试次数（DNS/TCP/SSL 抖动） |
| `EDGE__RETRY_BACKOFF_SEC` | `1.0` | 指数退避起始秒数（1s → 2s → 4s ...） |
| `EDGE__FFPROBE_TIMEOUT_SEC` | `10.0` | ffprobe 探测 mp3 时长的子进程超时 |
| `EDGE__FFMPEG_CONCAT_TIMEOUT_SEC` | `120.0` | edge provider ffmpeg 合并片段的子进程超时 |
| `EDGE__MIMO_FFMPEG_CONCAT_TIMEOUT_SEC` | `600.0` | mimo provider ffmpeg 合并的子进程超时 |

### TTS Provider 切换（`APP__TTS_PROVIDER`，默认 `mimo`）

启动时此环境变量决定初始 provider；运行时可通过 `PATCH /api/settings` 覆盖。运行时的选择会持久化到 `outputs/library.db` 的 `app_settings` 表。

`.env` 文件也支持（pydantic-settings 自动加载）。

> ⚠ **Voice 与 Provider 绑定**：每个 provider 用自己的 voice 白名单：
> * `mimo` provider 用 `TTS__VOICE`（默认 `mimo_default`）以及 `StaticVoices.items`（`冰糖` / `茉莉` / `Mia` 等 9 个）。
> * `edge` provider 只能用 `EDGE_VOICES_ZH` 白名单里的 13 个 Microsoft edge-tts 中文 voice（默认 `EDGE__DEFAULT_VOICE=zh-CN-XiaoxiaoNeural`）。
>
> 上传时如果传了 voice 但跟当前 active provider 不匹配，pipeline 会**自动 fallback**到该 provider 的默认 voice（不会报 `Invalid voice`），同时打一条 WARNING 日志方便排查。`EdgeTtsClient.synthesize_segment` 还会在调用前再做一道白名单校验，万一上层漏判也能给出可读错误而不是把无效 voice 喂给 edge-tts。
>
> ℹ️ **edge-tts 字幕事件**：edge-tts 7.x 的 `SubMaker` 不再提供 `get_cues()`，因此 `EdgeTtsClient.synthesize_segment` 现在用**单条 `Communicate(boundary="SentenceBoundary")`** 直接在 stream 里收集 `SentenceBoundary` 事件作为 cues；`offset` / `duration` 是 100ns ticks（除以 1e7 得秒）。修复后 edge provider 任务能完整跑通并产出 mp3 + srt + lrc 字幕。

### 覆盖内置常量（env 化）

4 个 system prompt + 2 个 voice 列表原本是模块级常量，v2 起全部支持 env 覆盖（不设则回落默认，向后兼容）：

| env 变量 | 覆盖目标 | 默认 |
|---|---|---|
| `APP__M3_SYSTEM_PROMPT` | M3 标准化 prompt | `M3_SYSTEM_PROMPT` 常量 |
| `APP__SPLIT_SYSTEM_PROMPT` | M3 文档分块 prompt（**必须保留 `{max_chars}` 占位符**） | `SPLIT_SYSTEM_PROMPT` 常量 |
| `APP__SEMANTIC_PREPROCESS_PROMPT` | edge 方案二专用 M3 语义预处理 prompt | `SEMANTIC_PREPROCESS_PROMPT` 常量 |
| `APP__MIMO_VOICES_JSON` | MiMo 静态 voice 列表（**JSON 数组**，每项含 `id`/`name`/`lang`） | 9 个内置 voice |
| `APP__EDGE_VOICES_JSON` | edge-tts voice 白名单（**JSON 数组**） | 13 个内置 voice |

示例（`.env`）：

```ini
# 自定义 M3 标准化 prompt（多行用 \n 转义）
APP__M3_SYSTEM_PROMPT=你是一个 TTS 预处理助手。\n1. 去掉 Markdown 残留。\n2. 替换 URL 为「网址」。

# 自定义 MiMo voice 列表
APP__MIMO_VOICES_JSON=[{"id":"mimo_default","name":"默认女声","lang":"zh"},{"id":"custom_v","name":"自定义","lang":"zh"}]
```

底层通过 `app.config.get_m3_system_prompt() / get_split_system_prompt() / get_semantic_preprocess_prompt() / get_mimo_voices() / get_edge_voices()` 读取，带 `lru_cache`；测试或运行中改 env 后调用 `reset_settings_cache()` 重新加载。覆盖解析失败时打 WARNING + 回落默认，不阻塞主流程。

## API 端点

| Method | Path | 说明 |
|---|---|---|
| `GET`  | `/`                       | 跳转到 UI |
| `GET`  | `/static/index.html`      | 前端 UI |
| `GET`  | `/api/health`             | 健康检查 + M3/TTS 配置状态 |
| `GET`  | `/api/voices`             | 语音列表 |
| `POST` | `/api/preview`            | 跑 M3 标准化，返回 normalized 文本 |
| `POST` | `/api/tasks`              | 上传 MD 创建后台转语音任务（返回 task_id） |
| `GET`  | `/api/tasks`              | 分页列出转语音任务（`?page=N&size=20`） |
| `GET`  | `/api/tasks/{task_id}`    | 查询单条任务进度详情 |
| `DELETE` | `/api/tasks/{task_id}`  | 删除任务及派生文件（成功任务保留最终播放 mp3 + artifacts） |
| `GET`  | `/api/audio/{audio_id}`   | 流式播放 MP3 |
| `GET`  | `/api/storage/stats`      | outputs/ 占用统计 |
| `GET`  | `/api/library`            | 听文档列表分页（`?page=N&size=10`，按 `created_at` 倒序） |
| `GET`  | `/api/library/{audio_id}` | 听文档详情（含原文 + normalized + lyrics_url + provider） |
| `GET`  | `/api/lyrics/{filename}.lrc` | 下载 edge provider 在流水线里落盘的 LRC 字幕文件 |
| `GET`  | `/api/settings`           | 获取当前 TTS provider、可用列表、edge voices |
| `PATCH` | `/api/settings`          | 运行时切换 TTS provider（`{tts_provider: "mimo" \| "edge"}`） |
| `GET`  | `/docs`                   | FastAPI Swagger UI |

`POST /api/tasks` 是 multipart/form-data（上传 MD 后创建后台异步任务）：

```bash
curl -X POST http://127.0.0.1:8000/api/tasks \
  -F "file=@samples/demo.md" \
  -F "voice_id=冰糖"
# 返回 {"task_id":"abc123...","message":"任务已创建"}
```

## 双方案对比与切换

### 方案一：M3 + 小米 MiMo（原方案，默认）

```
.md → 本地清洗 → M3 normalize → MiMo TTS → MP3
```

- ✅ M3 大模型标准化（URL/代码/公式替换、加标点、控字数）
- ✅ MiMo 9 个 voice（`mimo_default` / `冰糖` / `茉莉` / `苏打` / `白桦` / `Mia` / `Chloe` / `Milo` / `Dean`）
- ❌ 不生成 SRT 字幕（前端播放高亮用等分时间表）

### 方案二：M3 + edge-tts + ffmpeg（新方案）

```
.md → 本地清洗 → M3 语义预处理（含多音字[拼音]、拆句、断句）
   → edge-tts 分段合成（13 个中文 voice）
   → ffmpeg 拼接完整 mp3 + 句级 SRT 字幕 + 自动转 LRC 歌词
```

- ✅ M3 语义预处理（解决 edge-tts 无语义理解的短板）
- ✅ edge-tts 13 个中文 voice（`zh-CN-XiaoxiaoNeural` / `YunxiNeural` / `YunyangNeural` / …）
- ✅ 句级 SRT 字幕（带时间戳）
- ✅ 自动 SRT→LRC 转写，手机播放器同步滚动显示

### 切换方案

- **入口**：菜单「⚙️ 系统设置」→ 选中方案卡片 → 「应用更改」
- **生效**：立即对**下一次**上传生效（当前在跑的任务继续使用旧方案）
- **持久化**：方案选择写入 SQLite `app_settings` 表，跨进程保留
- **历史数据**：听文档列表每条带 `provider` 徽章（蓝色 `mimo` / 绿色 `edge`）标识来源

### 方案二依赖安装

```bash
pip install edge-tts
# ffmpeg 内置在 bin/ffmpeg.exe；如需自定义路径：
set EDGE__FFMPEG_PATH=D:\path\to\ffmpeg.exe
set EDGE__DEFAULT_VOICE=zh-CN-XiaoxiaoNeural
```

## Docker 部署

完整支持 Docker / docker-compose 一键启动。多阶段构建 + 非 root 用户 + 健康检查。

### 快速启动

```bash
# 1. 准备环境变量
cp .env.example .env
# 编辑 .env，至少填入 LLM__API_KEY（方案一还需 TTS__API_KEY）

# 2. 构建并后台启动
docker compose up -d --build

# 3. 验证：健康检查 + 容器日志
docker compose ps
docker compose logs -f txt2tts

# 4. 浏览器访问
# http://localhost:8000
# 手机访问（局域网）：http://<本机 IP>:8000

# 5. 停止
docker compose down
```

### 镜像设计

- **多阶段构建**：3 个 stage
  - `mwader/static-ffmpeg:7.1.1` —— 静态 ffmpeg + ffprobe（含 x264/x265）
  - `python:3.12-slim` —— 仅装 Python 依赖的 builder
  - `python:3.12-slim` —— 运行时镜像（仅含 site-packages + ffmpeg + 应用代码）
- **非 root 运行**：`USER appuser` (uid 1000)
- **健康检查**：`curl /api/health` 每 30 秒一次
- **数据持久化**：通过 volume 挂载 `./outputs` 容器外的目录

### 持久化目录

| 宿主机 | 容器 | 内容 |
|---|---|---|
| `./outputs/` | `/app/outputs/` | MP3 / SRT / LRC / `library.db` |
| `./samples/` | `/app/samples/` (ro) | 样例 `.md`（可选） |

容器被删除后，数据仍保留；只需重新 `docker compose up -d --build`。

### 切换 TTS 方案

编辑 `docker-compose.yml` 的 `environment`：

```yaml
APP__TTS_PROVIDER: "edge"   # 由 mimo 改为 edge
```

然后重启：

```bash
docker compose up -d --build
```

### 自定义镜像名 / 推送到私有仓库

```bash
docker build -t my-registry.example.com/txt2tts:v0.3.0 .
docker push my-registry.example.com/txt2tts:v0.3.0
```

### 在服务器上部署（含反向代理）

```bash
# 服务器
git clone <repo> && cd txt2tts
cp .env.example .env && nano .env
docker compose up -d --build

# Nginx 反向代理（示例）
server {
    listen 80;
    server_name txt2tts.example.com;
    location / { proxy_pass http://127.0.0.1:8000; proxy_set_header Host $host; }
}
```

## 听文档（Library）

前端是带 hash 路由的 SPA，**默认进入 `#/`（听文档）**，菜单可切换到 `#/upload`（上传转语音 → 任务列表）。

- **听文档列表页 `#/`**：调用 `GET /api/library?page=N&size=10`，按 `created_at` 倒序分页。每行第一个操作是 ▶ 播放按钮，点击进入 `#/play/<audio_id>`。
- **播放详情页 `#/play/<audio_id>`**：调用 `GET /api/library/{audio_id}` 拿到 `normalized_md` + `lyrics_url`；`<audio>` 加载 `GET /api/audio/{audio_id}`。**音乐播放器模式**：如果 `lyrics_url` 存在，前端拉 `GET /api/lyrics/<id>.lrc`，用 `parseLrc()` 解析为带时间戳的歌词行，监听 `timeupdate` 通过二分查找定位当前行（`findCurrentLrcIdx`），加 `.playing` 高亮 + `transform: scale(1.02)` + 蓝色 text-shadow；已播过的行加 `.past` 灰化。当前行自动 `scrollIntoView({block: "center"})`。提供"上一句 / 下一句 / 重播"按钮，跳到对应时间戳。**段落模式（fallback）**：如果没歌词，按空行拆 `normalized_md` 为段，用等分时间表高亮当前段（保留旧逻辑）。LRC 由 edge provider 自身在流水线里根据 SentenceBoundary cues 落盘，**没有"转歌词"按钮**（v3 已移除 LyricsService）。模式指示在 `playMeta` 里显示 `🎤 歌词同步（edge provider SRT/LRC）` 或 `无歌词（段落模式）`。
- **转语音任务列表页 `#/upload`**：调用 `GET /api/tasks?page=N&size=20`，显示所有转语音任务（含状态徽章、进度百分比、provider 徽章）。点击「＋ 新增转语音」打开上传对话框，上传成功后创建后台任务并自动跳转详情页。
- **任务详情页 `#/task/<task_id>`**：调用 `GET /api/tasks/{task_id}`，按 `provider` 字段渲染不同阶段步骤进度条，每 2 秒轮询更新。完成后显示「去播放」按钮；任意状态下都显示「🗑 删除任务」按钮，删除前弹确认对话框区分 done / 进行中两种语义。步骤条分两套：
  - `provider=mimo`（方案一 · 6 步）：本地清洗 → M3 标准化 → M3 切分 → MiMo 分块合成 → ffmpeg 合并 → 保存落盘
  - `provider=edge`（方案二 · 5 步）：本地清洗 → M3 语义预处理 → edge-tts 分段合成 → ffmpeg 合并 + SRT → 保存落盘
  - 列表上方说明文案也按当前 `state.activeProvider` 切换。
- **元数据**：合成成功后由 `TtsPipeline` 写入 SQLite 数据库 `outputs/library.db`（表 `audio_records`）。**旧 MP3 没有元数据**，列表里看不到，但文件本身仍可通过 `/api/audio/{id}` 访问。
- **库文件位置**：`outputs/library.db`（已被 `.gitignore` 忽略）。

## 失败处理

| 情况 | 行为 |
|---|---|
| M3 调用失败 / 超时 / 5xx | `POST /api/preview` 立即返回 **502**；`POST /api/tasks` 后台任务标记为 `error` |
| MiMo TTS 调用失败 | 后台任务标记为 `error`，前端轮询可见 |
| **edge-tts 调用瞬时网络错误**（DNS / TCP / SSL / Timeout） | `EdgeTtsClient.synthesize_segment` 自动指数退避重试 `EDGE__MAX_RETRIES`（默认 3）次；重试耗尽后抛 `edge-tts 微软服务暂不可达（已重试 N 次仍失败）：<原错误>`，后台任务标记 `failed_retryable` 可点 ↻ 重试 |
| **任务卡在某阶段不前进（watchdog 自检）** | 后台 `StallWatchdog` 每 10s 扫一次；同一阶段停滞超过 `APP__TASK_STALL_TIMEOUT_SEC`（默认 180s）自动标记 `failed_retryable`，原始 md 仍在可重试 |
| 用户上传空 md | 返回 422 |
| 上传 > 1MB | 返回 413 |
| `GET /api/library/{audio_id}` 找不到 | 返回 404 |
| `GET /api/tasks/{task_id}` 找不到 | 返回 404 |
| `DELETE /api/tasks/{task_id}` 找不到 | 返回 404 |

## 任务删除与产物集中管理（v2）

成功的任务产物统一落到 `outputs/audio/` 下，**防止误删 / 散落丢失**：

```
outputs/
├── audio/
│   ├── {audio_id}.mp3              # 最终成品（前端 /api/audio/{id} 入口）
│   └── _artifacts/
│       └── {audio_id}/             # 该任务的中间产物快照
│           ├── normalized.md       # M3 标准化结果
│           ├── 001.md / 002.md     # mimo 子文档
│           ├── 001.mp3 / 002.mp3   # mimo 子音频
│           ├── final.mp3
│           ├── {audio_id}.md       # 原始 md（重命名后保留）
│           └── {audio_id}.srt/.lrc # edge 字幕 / 歌词
├── uploads/{task_id}.md            # 仅未完成任务的原始 md
├── chunks/{task_id}/...            # 仅进行中
├── segments/{task_id}/...          # 仅进行中
└── library.db                      # SQLite 元数据
```

`DELETE /api/tasks/{task_id}` 行为：

| 任务状态 | 删除范围 | 保留 |
|---|---|---|
| `done` | tasks 行、听文档 `audio_records` 行、`uploads/`、`chunks/`、`segments/`、旧 `<YYYY-MM-DD>/` 残留 | `audio/{audio_id}.mp3`、`audio/_artifacts/{audio_id}/`（最终播放所需） |
| 其它（pending / processing / error / failed_retryable） | **全部** | 无 |

任务成功完成时，`pipeline.run` 末尾会自动调用 `AudioStorageService.promote_artifacts(task_id, audio_id)`：把 `chunks/<task_id>/`、`segments/<task_id>/`、`uploads/<task_id>.md` 全部搬到 `audio/_artifacts/<audio_id>/`，并把旧日期目录里的最终 mp3 收口到 `audio/{audio_id}.mp3`。

`GET /api/audio/{id}` 查找顺序：`audio/{id}.mp3` → `<YYYY-MM-DD>/{id}.mp3`（兼容旧数据）→ rglob 兜底。

## 任务自检（Stall Watchdog）

M3 / MiMo HTTP 调用层面有 `request_timeout_sec` 兜底，但服务端 TCP 握手后无限等待、DNS 黑洞、SDK 未抛 `TimeoutError` 等场景会让后台协程永远停在 `await` 上，导致任务表里的 `status='processing'` 永远不变。`StallWatchdog` 在 `app/main.py` lifespan 中以 `asyncio.create_task` 起一个常驻协程，定时扫描：

- 每次扫描：`TaskStore.list_processing()` 取出所有 `status='processing'` 的任务，对每个任务判定 `now - updated_at > threshold_sec`
- 超时：调用 `TaskStore.mark_stalled(...)` 标 `failed_retryable`，错误信息形如 `stalled at stage='tts_synthesize' for 200s`，前端消息形如 `stage=tts_synthesize 已卡住 200s，超过阈值 180s，自动标记失败`
- 阈值与间隔均可在 `.env` 覆盖：
  - `APP__TASK_STALL_TIMEOUT_SEC=180.0`（默认；与 `LLM__REQUEST_TIMEOUT_SEC` 对齐，给一次重试留余地）
  - `APP__TASK_WATCHDOG_INTERVAL_SEC=10.0`（默认）
  - `APP__TASK_WATCHDOG_ENABLED=true`（调试时可设 `false` 关闭）

## 真实端到端验证（已跑通）

`run_e2e_xiaomi.py` 跑完真实 M3 + 真实 MiMo TTS 全链路。已验证：

```
STEP 2 — real MiniMax M3 normalize
  POST https://api.minimaxi.com/anthropic/v1/messages
  -> 200 (2044ms)
  M3 returned = 264 chars  usage={'input_tokens': 463, 'output_tokens': 115, ...}

STEP 3 — real Xiaomi MiMo TTS synthesize
  POST https://api.xiaomimimo.com/v1/chat/completions
  model=mimo-v2.5-tts  voice=mimo_default
  -> 200 (10943ms)
  audio.id=071dbc0e3c044235964243664e1ba5a8
  b64_len=432896 chars
  decoded_bytes=324672

STEP 4 — save to outputs/
  D:\workspace\txt2tts\outputs\2026-07-01\25bcc3a6...mp3

STEP 5 — validate mp3
  size=324672 bytes (317.1 KB)
  size > 10KB? True
  head=fff384c400000000000000000058696e   <- \xff\xf3 MP3 magic
  valid mp3? True
  format=MP3

STEP 6 — play with Windows default player
  launching: start "" "...\25bcc3a6...mp3"
```

## 目录结构

```
txt2tts\
├── app\
│   ├── config.py             # AppSettings / LlmSettings / TtsSettings / M3_SYSTEM_PROMPT
│   ├── main.py               # FastAPI 入口
│   ├── routers\tts.py        # REST 路由（含任务 API）
│   ├── services\
│   │   ├── markdown_service.py  # md → 纯文本
│   │   ├── llm_normalizer.py    # MiniMax M3（通过 LangChain ChatAnthropic）
│   │   ├── tts_client.py        # 小米 MiMo (chat/completions multimodal)
│   │   ├── audio_storage.py     # MP3 落盘 + LibraryStore + TaskStore + SettingsStore
│   │   ├── task_manager.py     # 后台异步任务管理器
│   │   ├── task_watchdog.py    # 后台任务自检：卡死自动标记 failed_retryable
│   │   ├── edge_tts_provider.py # edge-tts 客户端 + ffmpeg 合并 + SRT/LRC 转换
│   │   └── pipeline.py          # 编排辅助（provider-aware 阶段进度事件生成器；mimo 6 步 / edge 5 步）
│   ├── models\schemas.py     # Pydantic DTO
│   └── static\               # 前端（index.html / app.js / styles.css）
├── samples\demo.md
├── outputs\                  # 生成的 MP3 + library.db
├── tests\
│   ├── test_markdown.py      # 4 个本地清洗测试
│   ├── test_llm_normalizer.py# M3 测试（unittest.mock patch ChatAnthropic）
│   ├── test_tts_client.py    # 9 个 MiMo TTS 测试（respx mock）
│   ├── test_pipeline.py      # 9 个 mimo provider 集成测试（含 M3 切分 + ffmpeg 拼接）
│   ├── test_library.py      # 7 个 LibraryStore 测试
│   ├── test_tasks.py         # 14 个 TaskStore + TaskManager 测试（含 provider 字段）
│   ├── test_task_watchdog.py # 15 个 StallWatchdog + TaskStore stall 辅助测试
│   ├── test_task_delete.py   # 18 个 AudioStorageService promote/delete + TaskManager.delete_task + DELETE API 测试
│   ├── test_lrc_parser.py    # 22 个 LRC 解析器测试（音乐播放器 LRC 解析的 Python 镜像实现）
│   ├── test_config_accessors.py # 27 个 env 覆盖 accessor 测试（3 prompt + 2 voice 列表 + 3 ffmpeg/ffprobe 超时 + pipeline helper 三层 fallback）
│   ├── test_edge_provider.py # 16 个 edge-tts helper + SRT/LRC 转换测试
│   └── test_pipeline_edge.py # 4 个 edge provider 端到端集成测试（真实 ffmpeg）
├── run_e2e_xiaomi.py         # 真实 API 端到端脚本
├── requirements.txt
├── pytest.ini
└── README.md
```

## 测试

```bash
D:\anaconda3\python.exe -m pytest tests/ -v
```

预期：**~175 passed**（4 markdown + 10 llm_normalizer + 9 tts_client + 9 pipeline（含 mimo M3 切分 + ffmpeg 拼接） + 7 library + 14 tasks（含 provider 字段） + 15 task_watchdog + 18 task_delete（AudioStorageService promote/delete + TaskManager.delete_task + DELETE API） + 25 config_accessors（3 prompt + 2 voice 列表 + 3 ffmpeg/ffprobe 超时 + pipeline helper 三层 fallback）+ 22 lrc_parser + 33 edge_provider + 4 pipeline_edge 端到端 + 5 mimo pipeline 含 ffmpeg）。转歌词功能已移除：test_lyrics.py 整个删除（-10）。

实际分布（`pytest tests/ -q`）：4 + 10 + 9 + 9 + 7 + 14 + 15 + 18 + 25 + 22 + 33 + 4 + 5 = **175 passed**。

## 真实端到端验证

```bash
# 设置两个 key
set LLM__API_KEY=<your MiniMax M3 key>
set TTS__API_KEY=<your Xiaomi MiMo key>

# 跑端到端
D:\anaconda3\python.exe run_e2e_xiaomi.py
```

会：
1. 读 `samples/demo.md` → 本地清洗
2. 调真实 M3 标准化
3. 调真实 MiMo TTS 合成
4. 保存到 `outputs/<日期>/<uuid>.mp3`
5. 校验文件大小和 magic bytes
6. 调 Windows 默认播放器播放

## 关于 MiniMax / MiMo API 字段

由于训练数据无法联网验证最新文档，代码默认按以下假设构造请求（已经过真实 API 验证）：

- **MiniMax M3**（Anthropic Messages API，**通过 LangChain `ChatAnthropic`**）
  - `client = ChatAnthropic(model=..., api_key=..., base_url=LLM__BASE_URL, max_tokens=..., temperature=..., timeout=..., max_retries=0)`
  - `await client.ainvoke([SystemMessage(content=M3_SYSTEM_PROMPT), HumanMessage(content=text)])`
  - SDK 内部按 Anthropic Messages 协议拼装 `POST {base_url}/v1/messages`，鉴权头与 `anthropic-version` 由 SDK 注入
  - 响应取 `resp.content`（已解析的文本）
- **小米 MiMo TTS**（chat/completions 多模态，仍走原始 HTTP）
  - `POST {TTS__BASE_URL}/v1/chat/completions`
  - Header: `Authorization: Bearer <TTS__API_KEY>`
  - Body 必须包含 `modalities=["text","audio"]`、`audio.format="mp3"`、`messages[1].role="assistant"`
  - 响应取 `choices[0].message.audio.data`（base64）→ 解码得到 mp3 字节

## 待办

- [ ] 长文本自动分片（>8k 字符由 M3 自身压缩即可，TTS 上限 50k）
- [ ] SRT 字幕导出
- [ ] 文件夹批量朗读
- [ ] 多角色对话朗读
- [ ] 打包成 .exe / Docker 镜像