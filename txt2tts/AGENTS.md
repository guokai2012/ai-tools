# AGENTS.md — txt2tts

## 项目用途
一个轻量的 FastAPI 应用，把上传的 `.md`（也支持 `.markdown` / `.txt`）文件转成 MP3：
`.md` → 本地 Markdown 清洗 → **MiniMax M3** 大模型标准化 → **小米 MiMo `mimo-v2.5-tts`** 语音合成 → 存到 `outputs/<YYYY-MM-DD>/<uuid>.mp3` → 通过 `/api/audio/{id}` 流式播放。

并配套一个**「听文档」**二级菜单（**默认进入**），把每次成功的合成记录写入 SQLite 索引 `outputs/library.db`，支持分页浏览、播放详情页展示原文，并按"等分时间表"高亮当前段。

还有一个**「上传转语音」**菜单，上传 MD 文件后创建后台异步任务（`asyncio.create_task`），前端通过轮询查看实时进度（按 `provider` 渲染不同步骤的进度条：mimo 6 步 / edge 5 步），完成后可跳转播放。任务元数据（含 `provider` 字段）存储在同一 `library.db` 的 `tasks` 表中。

`README.md` 是唯一的权威规格说明 —— 在修改流水线行为、环境变量或 API 字段之前，请先阅读它。

## 目录结构
```
app/
  config.py            # AppSettings / LlmSettings / TtsSettings 以及 M3_SYSTEM_PROMPT
  main.py              # FastAPI 入口，lifespan 中把服务注入到路由和 app.state
  routers/tts.py       # REST 端点（通过 configure() 注入依赖）
  services/
    markdown_service.py # md → 纯文本（基于 markdown-it-py）
    llm_normalizer.py   # MiniMax M3（通过 LangChain ChatAnthropic 调用 Anthropic Messages API）
    tts_client.py       # 小米 MiMo（chat/completions 多模态音频，仍走原始 HTTP）
    audio_storage.py    # MP3 落盘 + LibraryStore（SQLite 听文档索引） + TaskStore（后台任务索引）
    task_manager.py     # 后台异步任务管理器（消费 pipeline.run，更新 TaskStore）
    task_watchdog.py    # 后台任务自检：超过阈值自动标记 failed_retryable
    edge_tts_provider.py # edge-tts 客户端 + ffmpeg 合并 + SRT/LRC 转换（方案二，SentenceBoundary cues 累加 + voice 白名单校验 + 瞬时网络错误指数退避重试）
    lrc_parser.py        # LRC 歌词解析器（纯函数；前端 parseLrc 的 Python 镜像实现）
    pipeline.py         # 编排辅助（provider-aware async generator；mimo 6 步 / edge 5 步）
  models/schemas.py    # Pydantic 数据传输对象
  static/              # 前端（index.html 等）
tests/                 # pytest + respx mock（见 pytest.ini：asyncio_mode=auto）
samples/demo.md        # run_e2e_xiaomi.py 使用的标准样例
outputs/               # 生成的 MP3（已被 gitignore，仅保留 .gitkeep）
run_e2e_xiaomi.py      # 真实 API 端到端脚本（需要两个 API Key）
Dockerfile             # 多阶段构建：mwader/static-ffmpeg + python:3.12-slim
docker-compose.yml     # 单服务编排；卷 ./outputs → /app/outputs
.env.example           # 环境变量模板（包含 APP__*/LLM__*/TTS__*/EDGE__*）
.dockerignore          # 排除 __pycache__ / outputs / .env / 等
```

## 常用命令
- **安装依赖**：`D:\anaconda3\python.exe -m pip install -r requirements.txt`
  - 推荐 Python 3.10+（已在 3.13 验证）。
- **启动服务**：`D:\anaconda3\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
  - `app/main.py` 通过 `dotenv.load_dotenv` 加载 `.env`，**必须在导入 `app.config` 之前执行**，请勿调整这一导入顺序。
- **单元测试**（使用 mock，无需 API Key）：`D:\anaconda3\python.exe -m pytest tests/ -v`
  - 预期基线：约 175 个用例（markdown + llm_normalizer + tts_client + pipeline（含 mimo M3 切分 + ffmpeg 拼接） + library + tasks（含 provider 字段读写 4 个新用例） + task_watchdog（15 个 StallWatchdog + TaskStore stall 辅助） + task_delete（18 个 AudioStorageService promote/delete + TaskManager.delete_task + DELETE API） + config_accessors（25 个 env 覆盖 accessor：3 prompt + 2 voice 列表 + 3 ffmpeg/ffprobe 超时 + pipeline helper 三层 fallback） + lrc_parser（22 个音乐播放器 LRC 解析镜像实现，验证算法正确性） + edge_provider（33 个，含 8 个 voice 白名单校验 / fallback + 2 个 SentenceBoundary cues 累加 + 7 个瞬时网络错误自动重试：_is_transient_error 识别 / 重试成功 / 重试耗尽友好错误 / 业务错误不重试 / max_retries=0 / 指数退避时序） + pipeline_edge 端到端）。转歌词功能已移除：test_lyrics.py 整个删除（-10）。
- **真实端到端**（需要两个 Key）：先设置 `LLM__API_KEY` 和 `TTS__API_KEY`，再执行 `D:\anaconda3\python.exe run_e2e_xiaomi.py`。

## 环境变量（pydantic-settings，双下划线表示嵌套）
- `APP__*` —— `HOST`、`PORT`、`OUTPUT_DIR`、`MAX_MD_SIZE_KB`、`MAX_NORMALIZED_CHARS`。
- `LLM__*` —— MiniMax M3（通过 LangChain `ChatAnthropic` 调用 Anthropic Messages API）：`API_KEY`（由 SDK 作为鉴权凭据发送）、`BASE_URL=https://api.minimaxi.com/anthropic`、`MODEL=MiniMax-M3`、`MAX_TOKENS`、`TEMPERATURE`、`REQUEST_TIMEOUT_SEC`、`MAX_RETRIES`。响应通过 `resp.content` 获取。HTTP 头 / 请求路径 / 协议版本等由 SDK 内部处理，不再外暴露对应字段。
- `TTS__*` —— 小米 MiMo：`API_KEY`（Bearer）、`BASE_URL=https://api.xiaomimimo.com`、`CHAT_PATH=/v1/chat/completions`、`MODEL=mimo-v2.5-tts`、`VOICE`、`AUDIO_FORMAT`（`mp3` 或 `wav`）、`TEMPERATURE`、`REQUEST_TIMEOUT_SEC`、`MAX_RETRIES`。音频数据取自 `choices[0].message.audio.data`（base64）。
- `.env` 由 `app/main.py` 中的 `dotenv` 自动加载（**不**使用 `SettingsConfigDict(env_file=...)` —— 这与 `env_nested_delimiter` 存在已知冲突）。

## 架构 / 分层约定
- **服务是无状态的 async 类**，在 `main.py` 的 lifespan 中构造；路由通过 `routers.tts.configure(...)` 接收它们。**不要**在 service 内部导入 settings，要从外部把 `*Settings` 对象传进去。
- **`TtsSettings` 负责 MiMo TTS 请求体构造**（`build_request_body(...)`），修改 payload 字段时请集中在 `config.py` 调整。`LlmSettings` 仅保留 LangChain `ChatAnthropic` 客户端构造所需的字段（`api_key` / `base_url` / `model` / `max_tokens` / `temperature` / `request_timeout_sec` / `max_retries`）；HTTP 头 / 请求路径 / 协议版本由 SDK 内部处理。
- **TTS Provider 双方案**：`AppSettings.tts_provider` 默认 `"mimo"`（原方案），可设 `"edge"` 切换新方案。运行时通过 `PATCH /api/settings` 覆盖，结果写入 `outputs/library.db` 的 `app_settings` 表（跨进程保留）。Pipeline 按 `_active_provider` 走不同分支：
  - **mimo**：M3 标准化 → 持久化 `outputs/chunks/<id>/normalized.md` → M3 切分（语义保持）→ 持久化 `001.md / 002.md / ...` → MiMo 分块 → 持久化 `001.mp3 / 002.mp3 / ...` → ffmpeg concat → `final.mp3` → `AudioStorageService.save()` 写 `outputs/audio/<audio_id>.mp3`
  - **edge**：`EdgeTtsClient.synthesize_segment`（WebSocket）+ `ffmpeg` 合并 + 自动 SRT/LRC 落盘
  - `audio_records.provider` 列记录每条音频的方案来源。
- **Voice 与 Provider 绑定**：`mimo` provider 用 `get_mimo_voices()` 返回的 9 个 MiMo voice（默认含 `mimo_default` / `冰糖` 等，可被 `APP__MIMO_VOICES_JSON` 覆盖），`edge` provider 用 `get_edge_voices()` 返回的 13 个 Microsoft edge-tts 中文 voice（可被 `APP__EDGE_VOICES_JSON` 覆盖）。**上传时如果 voice 跟 active provider 不匹配**，`TtsPipeline._resolve_edge_voice()` 会**自动 fallback**到该 provider 的默认 voice（`EDGE__DEFAULT_VOICE` 或 `TTS__VOICE`），同时打 WARNING。`EdgeTtsClient.synthesize_segment` 内置白名单二次校验，万一上层漏判也能给出 `Invalid voice 'xxx' for edge provider` 友好提示，避免把 MiMo voice 喂给 edge-tts 触发原生错误。
- **env 化覆盖（v2）**：3 个 system prompt（`M3_SYSTEM_PROMPT` / `SPLIT_SYSTEM_PROMPT` / `SEMANTIC_PREPROCESS_PROMPT`）+ 2 个 voice 列表（`StaticVoices.items` / `EDGE_VOICES_ZH`）原本是 `app/config.py` 里的模块级常量。**v2 起全部支持 env 覆盖**（不设则回落默认，向后兼容）。v3 起 `LYRICS_SYSTEM_PROMPT` 随转歌词功能整体移除。
  - `APP__M3_SYSTEM_PROMPT` / `APP__SPLIT_SYSTEM_PROMPT` / `APP__SEMANTIC_PREPROCESS_PROMPT` —— 多行用 `\n` 转义；SPLIT 必须保留 `{max_chars}` 占位符
  - `APP__MIMO_VOICES_JSON` / `APP__EDGE_VOICES_JSON` —— JSON 数组，每项含 `id` / `name` / `lang`
  - 读取统一通过 `app.config` 的 accessor 函数 `get_m3_system_prompt()` / `get_split_system_prompt()` / `get_semantic_preprocess_prompt()` / `get_mimo_voices()` / `get_edge_voices()`；带 `lru_cache`，测试 / 运行中改 env 后调 `reset_settings_cache()` 重读。
  - 解析失败（JSON 不合法 / 不是 list）→ 打 WARNING + 回落默认，不阻塞主流程。
- **M3 切分 prompt**：`SPLIT_SYSTEM_PROMPT` 定义在 `llm_normalizer.py`，子文档用 `---SPLIT---` 分隔；不超阈值时走短文本直通；超阈值且单 chunk 仍超 1.2× max_chars 时 `_hard_split_chunks` 兜底。
- **Docker 部署**：`Dockerfile` 是 3 阶段多阶段构建：`mwader/static-ffmpeg:7.1.1` 提供 ffmpeg+ffprobe 静态二进制；`python:3.12-slim` builder 装 Python 依赖；runtime 镜像仅含 site-packages + ffmpeg + 应用代码 + 非 root `appuser`。`docker-compose.yml` 把宿主 `./outputs/` 挂到 `/app/outputs/` 实现数据持久化。容器内 `EDGE__FFMPEG_PATH=/usr/local/bin/ffmpeg`（由 `main.py` 启动时 `shutil.which("ffmpeg")` 兜底）。`ffprobe_path` 在 `concat_segments_with_srt` 内根据 `ffmpeg_path.name.endswith(".exe")` 推断 `.exe` 后缀，兼容 Windows 与 Linux。
- **`M3_SYSTEM_PROMPT` 定义在 `config.py`**（而非 normalizer 服务里），修改提示词请直接改这里。
- **路由的 HTTP 状态码约定**：M3/TTS 调用失败必须返回 **502**，空 `.md` 返回 **422**，超大 `.md` 返回 **413**，`GET /api/library/{id}` 和 `GET /api/tasks/{id}` 找不到返回 **404**。修改 `routers/tts.py` 时请保持这一契约。
- **MP3 落盘路径**（v2 起统一）：成功任务最终 mp3 写到 `outputs/audio/<audio_id>.mp3`，中间产物（chunks/segments/uploads.md）由 `pipeline.run` 末尾的 `AudioStorageService.promote_artifacts(task_id, audio_id)` 移到 `outputs/audio/_artifacts/<audio_id>/`。`GET /api/audio/{id}` 查找顺序：`audio/{id}.mp3` → `<YYYY-MM-DD>/{id}.mp3`（兼容旧数据）→ rglob 兜底。如要再调整磁盘布局，请同时更新 `resolve()` / `resolve_lyrics()` / `promote_artifacts()` / `delete_task_files()` 四处。
- **听文档（LibraryStore）**：`outputs/library.db`（路径由 `AppSettings.library_db_filename` 控制）存储合成记录。写入只发生在 `pipeline.run` 成功之后，**失败不影响主流程**（被 try/except 吞掉并 log）。`LibraryStore` 用 Python 标准库 `sqlite3`，不引入新依赖；连接使用 `check_same_thread=False` 容忍 FastAPI 跨线程复用。
- **后台任务（TaskStore + TaskManager）**：`outputs/library.db` 同一文件中再开 `tasks` 表，记录每个上传转语音任务的状态/进度/结果。`tasks` 表新增 `provider` 列（迁移兼容旧库），`TaskManager.create_task()` 会从 `pipeline._provider` 读取并写入；前端 `GET /api/tasks/{id}` 回传 `provider` 字段，详情页据此选择步骤条。`TaskManager.create_task()` 在 `asyncio.create_task` 中消费 `pipeline.run` 的 async generator，逐事件回写 `TaskStore`。前端通过 `GET /api/tasks/{id}` 每 2 秒轮询，无需 SSE。`pipeline.run` 自身保持不动（仍为 async generator），被 `TaskManager` 包装复用。
- **任务删除（v2）**：`DELETE /api/tasks/{task_id}` 由 `TaskManager.delete_task(task_id)` 编排：先 `AudioStorageService.delete_task_files(task_id, audio_id, keep_final_audio)`，再 `LibraryStore.delete(audio_id)`，最后 `TaskStore.delete(task_id)`。`keep_final_audio = (status == 'done')`：成功任务保留 `audio/<audio_id>.mp3` + `audio/_artifacts/<audio_id>/`（最终播放文件 + 中间产物快照），其它状态全删。前端任务列表与详情页都有「🗑 删除」按钮，弹确认对话框区分 done / 进行中语义。
- **任务自检（StallWatchdog）**：`app/services/task_watchdog.py` 提供 `StallWatchdog` 类，在 `main.py` lifespan 中以 `asyncio.create_task` 启动一个常驻协程；每 `APP__TASK_WATCHDOG_INTERVAL_SEC`（默认 10s）调用 `tick()`：通过 `TaskStore.list_processing()` 取出所有 `status='processing'` 的任务，对每个任务判定 `now - parse(updated_at) > APP__TASK_STALL_TIMEOUT_SEC`（默认 180s），超过则调 `TaskStore.mark_stalled(task_id, ...)` 标 `failed_retryable`，错误信息含 `stage=...` 与 stall 秒数。`clock` / `sleep` 全部可注入，方便单测用 fake clock。`enabled=False` 时 `start()` 直接返回；`stop()` 会 `cancel` 协程并 `await` 退出。状态常量（`pending` / `processing` / `done` / `error` / `failed_retryable`）已迁移到 `audio_storage.py`，`task_manager.py` 仍 re-export 保持向后兼容。
- **歌词生成（LyricsService，v3 已移除）**：原先用 M3 二次改写 `normalized_md` 为可演唱 LRC；依赖 `LYRICS_SYSTEM_PROMPT` + `outputs/lyrics/<audio_id>.lrc` 落盘 + `audio_records.lyrics_path` 回写。v3 整体下线：
  - `app/services/lyrics_service.py` 已删
  - `POST /api/library/{id}/lyrics` 已删
  - `LYRICS_SYSTEM_PROMPT` 常量 + `APP__LYRICS_SYSTEM_PROMPT` env + `get_lyrics_system_prompt()` accessor 已删
  - `tests/test_lyrics.py` 整个删（-10 个用例）
  - `audio_records.lyrics_path` 列**保留**以兼容历史数据（不写新值）；前端 `AudioRecordDto.has_lyrics` / `AudioDetailDto.lyrics_url` 字段保留
  - 详情页 LRC 来源改用 **edge provider 自身在流水线里落盘的 srt/lrc**（来自 SentenceBoundary cues，真实句级时间戳），通过 `GET /api/lyrics/{id}.lrc` 下载；音乐播放器模式仍然可用
  - `LRCParser`（`app/services/lrc_parser.py`）**保留**：与是否启用 LyricsService 无关，详情页前端 JS 仍用它做 LRC 解析 + 同步高亮
- **前端 hash 路由**：`#/`（听文档，默认）、`#/upload`（任务列表）、`#/play/<audio_id>`（播放详情，音乐播放器模式：LRC 同步歌词）、`#/task/<task_id>`（任务详情，含 2 秒轮询）。菜单切换无刷新；**音乐播放器模式**：当 `AudioDetailDto.lyrics_url` 非空时，前端 `parseLrc()` 解析 LRC，监听 `timeupdate` + 二分查找（`findCurrentLrcIdx`）定位当前行加 `.playing` 高亮；当前行 `transform: scale(1.02)` + text-shadow；已播行加 `.past` 灰化；自动 `scrollIntoView({block: "center"})`；无歌词时 fallback 到段落模式（按 `normalized_md` 空行拆段 + 等分时间表）。任务详情页步骤条按 `record.provider` 选 `STAGES_BY_PROVIDER`（`mimo`: 清洗→M3 标准化→M3 切分→MiMo 分块合成→ffmpeg 合并→保存落盘；`edge`: 清洗→M3 语义预处理→edge-tts 分段合成→ffmpeg 合并 + SRT→保存落盘），provider 缺失时回落旧 4 步；任务列表上方说明文案 (`#uploadHintSteps`) 也按 `state.activeProvider` 切换。
- **移动端适配**：CSS 末尾的 `@media (max-width: 720px)` 块负责手机布局。所有按钮 ≥ 44px 触摸目标；输入控件字号强制 16px 防止 iOS 自动放大；任务详情步骤条在 `≤900px` **自动纵向**堆叠（连接条动画从 `scaleX` 改为 `scaleY`，label 允许换行），整体移动布局仍以 720px 为准；上传对话框从底部弹出并占满宽度，含 `env(safe-area-inset-*)` 适配 iPhone 刘海/底部 home indicator；`hidden` 属性必须配合 `[hidden] { display:none !important; }` 覆盖 `.view { display: flex }` 的优先级。
- **禁止在代码中硬编码密钥**：`.env` 已被 gitignore，绝不能提交真实的 `LLM__API_KEY` / `TTS__API_KEY`。

## 代码风格
- 使用 Python 3.10+ 语法（`from __future__ import annotations`、`str | None` 等）。
- `pytest-asyncio` 处于 `auto` 模式（见 `pytest.ini`），编写 async 测试**不要**再加 `@pytest.mark.asyncio` 装饰器。
- 网络请求统一使用 **respx** 进行 mock，URL / Header / Body 形态要与真实请求保持一致。
- 日志：`app/main.py` 中已通过 `logging.basicConfig` 配置 INFO 级别，请在各模块中使用 `logger = logging.getLogger(__name__)`。

## 已知陷阱
- **TTS 走的是 chat completions，而不是专用 TTS 端点**：请求体必须包含 `modalities=["text","audio"]` 和 `audio={"voice": ..., "format": "mp3"}`，并且要附带两条消息历史（`user` 用于发出朗读请求，`assistant` 携带待朗读文本）。不要简化成单条消息。
- **M3 与 MiMo 是不同厂商、不同调用栈**：M3 走 LangChain `ChatAnthropic`（鉴权头与协议由 SDK 处理），MiMo 走原始 `httpx` HTTP（`Authorization: Bearer`）。混淆会导致其中一条链路静默失败。
- **M3 的 HTTP 细节已被 LangChain 接管**：`x-api-key` / `anthropic-version` / `/v1/messages` 路径等不再需要（也无法再）在 `LlmSettings` 中配置。如需更换协议版本或路径，请通过升级 / 替换 LangChain SDK 版本实现，而不是回到手动 HTTP。
- **`dotenv` 必须在 `app.config` 之前导入**（见 `main.py`）—— 调整顺序会破坏 pydantic-settings 的环境变量加载。
- **MP3 校验的魔数**定义在 `run_e2e_xiaomi.py`：`ID3`、`\xff\xfb`、`\xff\xf3`、`\xff\xe3`（WAV 用 `RIFF`）。新增校验逻辑时请复用同一组常量。
- **语音列表**：`config.py` 中的 `StaticVoices.items` / `EDGE_VOICES_ZH` 是兜底列表（**v2 起必须通过 `get_mimo_voices()` / `get_edge_voices()` accessor 读取**才能被 env 覆盖）。已验证的 9 个 MiMo voice id（`mimo_default`、`冰糖`、`茉莉`、`苏打`、`白桦`、`Mia`、`Chloe`、`Milo`、`Dean`）和 13 个 edge-tts 中文 voice 驱动 `GET /api/voices`。
- **生成的音频位于 `outputs/`，已被 gitignore**（仅 `.gitkeep` 受版本控制）—— 不要提交 MP3 文件。

## 修改前必读
- `README.md` —— 完整流水线、环境变量、API 契约以及真实 API 验证日志。
- `app/config.py` —— 提示词、环境变量前缀、`TtsSettings` 请求体的唯一权威来源；`AppSettings.library_db_filename` 控制听文档库文件位置。
- `app/main.py` —— 装配顺序（dotenv → settings → services → router configure → lifespan）；新服务（如 `LibraryStore` / `TaskStore`）需在 lifespan 中实例化并通过 `tts_router.configure(...)` 注入。
- `app/services/audio_storage.py` —— 同时承载 `AudioStorageService`（MP3 落盘，v2 起写到 `audio/<id>.mp3`，含 `promote_artifacts` / `delete_task_files` / `resolve` / `resolve_lyrics`）、`LibraryStore`（SQLite 听文档索引 + `lyrics_path`（v3 起只读兼容旧数据）+ `provider` 列 + `delete(audio_id)`）、`TaskStore`（SQLite 后台任务索引 + `provider` 列、`list_processing` / `mark_stalled` watchdog 辅助、`delete(task_id)`）、`SettingsStore`（运行时可切换的 `app_settings` 表），并集中存放 `TASK_STATUS_*` 常量；改一处时留意对另三处的影响。新增列时记得加 PRAGMA 迁移兼容旧库。
- `app/services/task_manager.py` —— 后台异步任务管理器，`TaskManager.create_task()` 会在 `asyncio.create_task` 中消费 `pipeline.run`；新功能如果需要复用"边产出边写库"模式，请参考此处的 `update_progress` 模式。`TaskManager.provider` 属性从 `pipeline._provider` 取值，传递给 `TaskRecord.provider`；`_provider` 不是字符串时（如测试传入 `MagicMock`）回落为 `"mimo"`。`TaskManager.delete_task(task_id)` 编排三阶段清理：`AudioStorageService.delete_task_files` → `LibraryStore.delete` → `TaskStore.delete`，由 `status=='done'` 决定是否保留最终播放文件。
- `app/services/task_watchdog.py` —— 后台任务自检（StallWatchdog）。`tick()` 单次扫描；`run()` 异步循环；`start()` / `stop()` 接入 FastAPI lifespan。`clock` / `sleep` 可注入便于单测。
- `app/services/edge_tts_provider.py` —— `EdgeTtsClient.synthesize_segment` 用单条 `Communicate(boundary="SentenceBoundary")` 在 stream 里**直接累加** cues（edge-tts 7.x `SubMaker` 不再提供 `get_cues()`，旧实现会 AttributeError）。`SentenceBoundary.offset` / `duration` 单位是 100ns ticks（除以 1e7 转秒）。在调用前还会做 `EDGE_VOICES_ZH` 白名单校验，不合法直接抛 `EdgeTtsError` 友好提示。**瞬时网络错误自动重试**：`EDGE__MAX_RETRIES`（默认 3）+ `EDGE__RETRY_BACKOFF_SEC`（默认 1.0s 指数退避），仅对网络类异常（`aiohttp.ClientError` / `SSLError` / `TimeoutError` / `ConnectionError` / `OSError` 等）重试，参数 / 业务错误（Invalid voice / 空音频）立即抛。重试耗尽后抛 `edge-tts 微软服务暂不可达（已重试 N 次仍失败）：<原错误>`。`max_retries=0` 时不重试。
- `app/services/pipeline.py` —— `audio_save` 成功后调用 `library.insert(...)`，写入失败被 try/except 吞掉；如果想让索引写入变成硬错误，需同步修改此处与 `main.py` 装配链。`TaskManager` 通过订阅 `pipeline.run` 的 async generator 复用同一份流程，**不要**为后台任务再实现一份 pipeline。
- `run_e2e_xiaomi.py` —— 当 mock 测试与真实接口出现偏差时，对照此文件确认 M3（LangChain）+ MiMo（HTTP）真实请求形态。
- `app/static/{index.html,app.js,styles.css}` —— 前端 SPA 在 `app/static/app.js` 中通过 hash 路由切视图；菜单与列表/高亮样式分别对应 `.menu`、`.library-list`、`.task-list`、`.play-content .segment.playing`、`.dialog-overlay`。`#/task/<id>` 视图含 2 秒轮询逻辑（`state.taskPollTimer`），路由切换或任务完成时需 `stopTaskPoll()`。移动端适配集中在 `styles.css` 末尾的 `@media (max-width: 720px)` 块，**新增 view 必须考虑小屏布局**（单列堆叠 + 触摸目标 ≥ 44px）。