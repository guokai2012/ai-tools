# AGENTS.md — txt2tts

## 项目用途
一个轻量的 FastAPI 应用，把上传的 `.md`（也支持 `.markdown` / `.txt`）文件转成 MP3。
v4 起目录布局彻底 greenfield：**所有产物都在 `outputs/<yyyymmdd>/<task_id>/` 下**，
不再有 `outputs/audio/`、`uploads/`、`chunks/`、`segments/`、`<YYYY-MM-DD>/`、
`audio_records` 表、`LibraryStore`、`AudioRecord` / `StoredAudio` dataclass、
`promote_artifacts()`、`audio_id` 概念。

听文档列表 = `tasks` 表 `status='done'` 的任务；前端所有 URL 直接用 `task_id`。

## 目录结构

```
app/
  config.py            # AppSettings / LlmSettings / MinimaxTtsSettings / EdgeTtsSettings + M3_SYSTEM_PROMPT
  main.py              # FastAPI 入口；lifespan 装配服务
  routers/tts.py       # REST 端点
  services/
    # （v5 起删除 markdown_service.py：原始 MD 直接喂给 M3，不再本地清洗）
    llm_normalizer.py     # MiniMax M3（通过 LangChain ChatAnthropic）
    minimax_tts_provider.py # MiniMax speech-2.8-hd（v3 起 OSS URL 下载 + v4 起 5 次指数退避重试 + 字段名容错）
    audio_storage.py      # AudioStorageService（task_dir 布局）+ TaskStore + SettingsStore
    task_manager.py       # 后台任务编排（v4 阶段感知重试 + subtitle_pending）
    task_watchdog.py      # 后台任务自检
    edge_tts_provider.py  # edge-tts 备选
    lrc_parser.py         # LRC 解析器（前端 JS 镜像）
    pipeline.py           # 编排辅助（minimax 6 步 / edge 5 步）
  models/schemas.py     # Pydantic DTO（含 LibraryItemDto / LibraryDetailDto）
  static/               # 前端
tests/                  # pytest + respx（asyncio_mode=auto）
samples/demo.md
outputs/                 # 运行时产物（gitignore）
run_e2e_minimax.py       # 真实 API 端到端脚本
Dockerfile / docker-compose.yml / .env.example / .dockerignore
```

## 常用命令
- **安装依赖**：`D:\anaconda3\python.exe -m pip install -r requirements.txt`
- **启动服务**：`D:\anaconda3\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
  - `app/main.py` 通过 `dotenv.load_dotenv` 加载 `.env`，**必须在导入 `app.config` 之前执行**
- **单元测试**（mock，无需 API Key）：`D:\anaconda3\python.exe -m pytest tests/ -v`
  - 预期：**277 passed**。分布：10 llm_normalizer + 47 minimax_tts_provider +
    12 pipeline（minimax 集成）+ 12 pipeline_minimax（含 subtitle_pending + 阶段感知重试）+
    4 pipeline_edge + **34 tasks（含 4 个 v6 本地清洗）+ 17 test_task_dir_layout（含 1 个 v6 split_覆写）+ 5 e2e_step_flow（v6 加 3 个 local-clean 端到端）**+
    15 task_watchdog + **30 text_cleaner（v6 新增）+ 22 lrc_parser + 17 config_accessors + 4 edge_provider**。
- **真实端到端**：`D:\anaconda3\python.exe run_e2e_minimax.py`（需 `LLM__API_KEY`）

## 环境变量（pydantic-settings，双下划线表示嵌套）
- `APP__*` —— `HOST` / `PORT` / `OUTPUT_DIR` / `MAX_MD_SIZE_KB` / `MAX_NORMALIZED_CHARS` / task_watchdog_*
- `LLM__*` —— MiniMax M3（通过 LangChain `ChatAnthropic`）。**`LLM__API_KEY` 也作为 MiniMax TTS 的回落 key**
- `MINIMAX__*` —— MiniMax speech-2.8-hd TTS（默认方案）。`API_KEY` 留空回落 `LLM__API_KEY`
- `EDGE__*` —— edge-tts 备选
- `.env` 由 `app/main.py` 中的 `dotenv` 自动加载

## 架构 / 分层约定

### 服务是无状态 async 类
在 `main.py` lifespan 中构造；路由通过 `routers.tts.configure(...)` 注入。
**不要**在 service 内部 import settings，从外部把 `*Settings` 对象传进去。

### TTS Provider 双方案（v4）
- `PROVIDERS = ["minimax", "edge"]`，默认 `"minimax"`
- `minimax`：M3 标准化 → (可选 M3 切分) → MiniMax T2A speech-2.8-hd 分块 → ffmpeg 拼接
- `edge`：`EdgeTtsClient.synthesize_segment`（WebSocket）+ ffmpeg 合并
- `tasks.provider` 列记录方案来源

### 任务目录布局（v4 全新）
所有产物在 `<output_dir>/<yyyymmdd>/<task_id>/` 下：

```
outputs/
└── 20260704/
    └── <task_id>/
        ├── <task_id>.md          ① 原始 markdown（upload 后立即落盘；v5 起不再做本地清洗）
        ├── normalization.md      ② M3 标准化；跳过时复制自 <task_id>.md
        ├── split_<N>.md         ③ M3 拆分；跳过时复制 normalization.md → split_1.md
                                  v6 本地清洗会**覆写**此文件
        ├── split_<N>.mp3        ④ TTS 转换（minimax hex 解码）
        ├── split_<N>.SRT        ④ minimax subtitle_file 解析的逐段 SRT
        ├── <task_id>.mp3        ⑤ ffmpeg 合并（最终播放文件）
        ├── <task_id>.SRT        ⑤ 累积偏移完整 SRT
        └── <task_id>.LRC        ⑥ LRC 歌词
```

不再有：
- `outputs/audio/`、`outputs/uploads/`、`outputs/chunks/`、`outputs/segments/`
- `outputs/<YYYY-MM-DD>/`（旧日期格式）
- `outputs/audio/_artifacts/`
- `audio_id` 独立 UUID（**task_id 即 audio_id**）
- `audio_records` SQLite 表（已删除；启动时 DROP TABLE IF EXISTS）

### SQLite Schema（仅 2 张表）
- `tasks` —— 唯一数据源（含 date_str 字段；列：task_id / filename / voice_id / status /
  current_stage / progress / message / date_str / error / created_at / updated_at /
  retry_count / provider / original_md_path / local_clean_text / normalized_text /
  split_prompt / split_chunks）
- `app_settings` —— 运行时可切换的应用设置

启动时 DROP TABLE IF EXISTS audio_records，并 ALTER TABLE 增量补 date_str 列兼容旧库。

### Status 状态机（v6：13+1 状态）
`draft` → `normalizing` → `normalized` → `ready_to_split` → `splitting` → `splitted` →
**`local_cleaning`** → **`local_cleaned`**（v6 本地清洗；可跳过直跳 ready_to_convert）→
`ready_to_convert` → `converting` → `done` / `error` / `failed_retryable` /
**`subtitle_pending`**（minimax 字幕拉取失败；音频已可用，可重试）

### 分步交互任务流
每个步骤显式触发（前端按钮 → POST 端点 → 后台异步 → 状态更新 → 前端轮询）：
- `POST /api/tasks` — 写 `task_dir/<task_id>.md`（v5 起原文直接落盘，无本地清洗）+ 插入 draft
- `POST /api/tasks/{id}/normalize` — async M3 标准化（写 `normalization.md`，body 可选 `{"prompt": "..."}`）
- `POST /api/tasks/{id}/skip-normalize` — 复制 `<task_id>.md` → `normalization.md`
- `POST /api/tasks/{id}/split` — async M3 拆分（写 `split_<N>.md`）
- `POST /api/tasks/{id}/local-clean` — **v6** async 本地清洗（覆写 `split_<N>.md`；body `{"options": [...]}`）
- `POST /api/tasks/{id}/skip-local-clean` — **v6** 跳过本地清洗（`splitted` → `ready_to_convert`）
- `POST /api/tasks/{id}/confirm-split` — 用户确认/编辑子文档（**v6 起也接受 `local_cleaned`**）
- `POST /api/tasks/{id}/skip-split` — 复制 `normalization.md` → `split_1.md`
- `POST /api/tasks/{id}/convert` — async TTS 转换（写 `split_<N>.mp3/.SRT` → `<task_id>.mp3/.SRT/.LRC`）

### v6 本地清洗（splitted → ready_to_convert 之间）
`app/services/text_cleaner.py` 提供 8 项规则（id / label / 默认勾选）：
- `url`：删除 URL（`https?://\S+`）
- `email`：删除邮箱
- `code`：删除代码围栏 / 行内代码 / 文件路径 / Git SHA-like 哈希
- `emoji`：删除 Unicode 表情（保留中文标点）
- `md_symbols`：删除 `**`、`#`、`*`、`_`、`~~` 等 Markdown 标记
- `list_marks`：删除有序/无序列表前缀
- `blockquote`：删除 `>` 引用前缀
- `table_pipe`：删除表格 `|` 分隔符

`GET /api/clean-options` 拉元数据；前端复选框 + 二级确认面板；
后端 `apply_local_clean(text, enabled_ids)` 对每个 chunk 跑规则后**覆写** `split_<N>.md`。
纯本地正则，无网络/IO 调用，理论不失败（仍 try/except 兜底，失败回 `splitted`）。

### 阶段感知重试（v4 起 + v6 加 local_cleaning 回退）
`POST /api/tasks/{id}/retry`：
- `subtitle_pending` → `_retry_subtitle()`：整段 convert 重跑
- `local_cleaning` → 回 `splitted` + 用历史 `clean_options` 重做 `local_clean_task`
- `error/failed_retryable` + 无 normalized_text → 重新 normalize
- `error/failed_retryable` + 有 normalized_text 无 split_chunks → 重新 split
- `error/failed_retryable` + 有 split_chunks → 重新 convert
- `done` → 409 不允许

### 删除任务（v4）
`DELETE /api/tasks/{task_id}` → 直接 `rmtree task_dir` + 删 tasks 行。
**前端弹模态对话框**：
- `status='done'`：要求输入"确认删除"四个字才启用确认按钮
- 其他状态：弹二次确认（两按钮，无文本输入）

### AudioStorageService 接口
- `task_dir(task_id, *, date_str=None) -> Path` —— 返回并创建目录
- `task_file_path(task_id, filename, *, date_str=None) -> Path`
- `resolve(task_id, *, date_str=None) -> Optional[Path]` —— 拼 `<task_id>.mp3`
- `resolve_lyrics(task_id, *, date_str=None) -> Optional[Path]` —— 拼 `<task_id>.LRC`
- `delete_task_files(task_id, *, date_str=None) -> dict` —— rmtree 整个目录

### Voice 与 Provider 绑定
- `minimax` provider：`MINIMAX_VOICES_ZH`（12 个 voice_id），`get_minimax_voices()` 读取
- `edge` provider：`EDGE_VOICES_ZH`（13 个 voice），`get_edge_voices()` 读取
- 上传 voice 跟 active provider 不匹配时自动 fallback + WARNING

### API 路径（v4）
- `GET /api/audio/{task_id}` —— 流式播放 `<task_id>.mp3`
- `GET /api/lyrics/{task_id}.lrc` —— 下载 `<task_id>.LRC`
- `GET /api/library` —— 听文档列表（status='done' 任务）
- `GET /api/library/{task_id}` —— 听文档详情（original_md + normalized_md 从 task_dir 文件系统读）

### 前端
- hash 路由：`#/`（听文档）、`#/upload`、`#/play/<task_id>`、`#/task/<task_id>`
- 状态徽章：done=绿、error/failed_retryable=红、subtitle_pending=灰
- 删除对话框：复用 `.dialog-overlay` 样式 + 输入框 + 实时校验

### 路由 HTTP 状态码
M3/TTS 失败 → 502；空 .md → 422；超大 .md → 413；找不到 → 404。

## 代码风格
- Python 3.10+（`from __future__ import annotations`、`str | None` 等）
- pytest-asyncio `auto` 模式（async 测试不需 `@pytest.mark.asyncio`）
- 网络 mock 用 **respx**；URL / Header / Body 与真实请求一致
- 日志：`logging.basicConfig` 已在 main.py 配置；模块内 `logger = logging.getLogger(__name__)`

## 已知陷阱
- **TTS 走的是专用 HTTP 端点**（不是 chat completions）。请求体必含 `voice_setting` /
  `audio_setting` / `subtitle_enable=true` / `subtitle_type=sentence`。不要把它与 M3 的
  chat completions（`messages`/`modalities`/`audio`）混淆。
- **`data.subtitle_file` 是 OSS 公开 URL**（含签名 token），客户端二次 GET 拿到字幕 JSON。
  错误/非 JSON/字段名未知 → 任务转 `subtitle_pending` 状态。
- **字幕 JSON 内部字段名文档未公开**，`_parse_subtitle_payload` 按多组候选键容错：
  `text` / `sentence_text` / `sentence` + `start_time` / `begin_time` / `start` +
  `end_time` / `finish_time` / `end`（毫秒单位）。
- **M3 与 MiniMax TTS 是同一厂商但调用栈不同**：M3 走 LangChain `ChatAnthropic`，
  MiniMax T2A 走原始 `httpx` HTTP（`Authorization: Bearer`）。混淆会导致其中一条链路静默失败。
- **API Key 回落顺序**：`MINIMAX__API_KEY` 留空 → 回落 `LLM__API_KEY`（同平台共用）。
- **`dotenv` 必须在 `app.config` 之前导入**（见 `main.py`）。
- **MP3 校验的魔数**：`ID3` / `\xff\xfb` / `\xff\xf3` / `\xff\xe3`（WAV 用 `RIFF`）。
- **生成的音频位于 `outputs/`，已被 gitignore**。

## 修改前必读
- `README.md` —— 完整流水线、环境变量、API 契约
- `app/config.py` —— 提示词、环境变量前缀、`*Settings` 类
- `app/services/audio_storage.py` —— task_dir 布局 + TaskStore + SettingsStore；v6 加 `clean_options` 列 + 2 个状态常量
- `app/services/task_manager.py` —— 后台任务编排（v4 阶段感知重试 + v6 `local_clean_task` / `skip_local_clean`）
- `app/services/text_cleaner.py` —— **v6 新增** 8 项清洗规则的元数据 + `apply_local_clean()` 纯函数
- `app/services/pipeline.py` —— pipeline 写盘到 task_dir 的逻辑；v6 加 `local_clean` 阶段权重
- `app/services/minimax_tts_provider.py` —— T2A HTTP + subtitle_file 二次 GET
- `app/routers/tts.py` —— 端点；/api/audio 与 /api/lyrics 都按 task_id 拼路径
- `app/static/{index.html,app.js,styles.css}` —— 删除确认对话框用 `.dialog-overlay` 样式