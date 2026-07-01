# AGENTS.md — txt2tts

## 项目用途
一个轻量的 FastAPI 应用，把上传的 `.md`（也支持 `.markdown` / `.txt`）文件转成 MP3：
`.md` → 本地 Markdown 清洗 → **MiniMax M3** 大模型标准化 → **小米 MiMo `mimo-v2.5-tts`** 语音合成 → 存到 `outputs/<YYYY-MM-DD>/<uuid>.mp3` → 通过 `/api/audio/{id}` 流式播放。

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
    audio_storage.py    # MP3 落盘
    pipeline.py         # 编排辅助（preview / synthesize）
  models/schemas.py    # Pydantic 数据传输对象
  static/              # 前端（index.html 等）
tests/                 # pytest + respx mock（见 pytest.ini：asyncio_mode=auto）
samples/demo.md        # run_e2e_xiaomi.py 使用的标准样例
outputs/               # 生成的 MP3（已被 gitignore，仅保留 .gitkeep）
run_e2e_xiaomi.py      # 真实 API 端到端脚本（需要两个 API Key）
```

## 常用命令
- **安装依赖**：`D:\anaconda3\python.exe -m pip install -r requirements.txt`
  - 推荐 Python 3.10+（已在 3.13 验证）。
- **启动服务**：`D:\anaconda3\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000`
  - `app/main.py` 通过 `dotenv.load_dotenv` 加载 `.env`，**必须在导入 `app.config` 之前执行**，请勿调整这一导入顺序。
- **单元测试**（使用 mock，无需 API Key）：`D:\anaconda3\python.exe -m pytest tests/ -v`
  - 预期基线：约 23 个用例（markdown + llm_normalizer + tts_client + pipeline）。
- **真实端到端**（需要两个 Key）：先设置 `LLM__API_KEY` 和 `TTS__API_KEY`，再执行 `D:\anaconda3\python.exe run_e2e_xiaomi.py`。

## 环境变量（pydantic-settings，双下划线表示嵌套）
- `APP__*` —— `HOST`、`PORT`、`OUTPUT_DIR`、`MAX_MD_SIZE_KB`、`MAX_NORMALIZED_CHARS`。
- `LLM__*` —— MiniMax M3（通过 LangChain `ChatAnthropic` 调用 Anthropic Messages API）：`API_KEY`（由 SDK 作为鉴权凭据发送）、`BASE_URL=https://api.minimaxi.com/anthropic`、`MODEL=MiniMax-M3`、`MAX_TOKENS`、`TEMPERATURE`、`REQUEST_TIMEOUT_SEC`、`MAX_RETRIES`。响应通过 `resp.content` 获取。HTTP 头 / 请求路径 / 协议版本等由 SDK 内部处理，不再外暴露对应字段。
- `TTS__*` —— 小米 MiMo：`API_KEY`（Bearer）、`BASE_URL=https://api.xiaomimimo.com`、`CHAT_PATH=/v1/chat/completions`、`MODEL=mimo-v2.5-tts`、`VOICE`、`AUDIO_FORMAT`（`mp3` 或 `wav`）、`TEMPERATURE`、`REQUEST_TIMEOUT_SEC`、`MAX_RETRIES`。音频数据取自 `choices[0].message.audio.data`（base64）。
- `.env` 由 `app/main.py` 中的 `dotenv` 自动加载（**不**使用 `SettingsConfigDict(env_file=...)` —— 这与 `env_nested_delimiter` 存在已知冲突）。

## 架构 / 分层约定
- **服务是无状态的 async 类**，在 `main.py` 的 lifespan 中构造；路由通过 `routers.tts.configure(...)` 接收它们。**不要**在 service 内部导入 settings，要从外部把 `*Settings` 对象传进去。
- **`TtsSettings` 负责 MiMo TTS 请求体构造**（`build_request_body(...)`），修改 payload 字段时请集中在 `config.py` 调整。`LlmSettings` 仅保留 LangChain `ChatAnthropic` 客户端构造所需的字段（`api_key` / `base_url` / `model` / `max_tokens` / `temperature` / `request_timeout_sec` / `max_retries`）；HTTP 头 / 请求路径 / 协议版本由 SDK 内部处理。
- **`M3_SYSTEM_PROMPT` 定义在 `config.py`**（而非 normalizer 服务里），修改提示词请直接改这里。
- **路由的 HTTP 状态码约定**：M3/TTS 调用失败必须返回 **502**，空 `.md` 返回 **422**，超大 `.md` 返回 **413`。修改 `routers/tts.py` 时请保持这一契约。
- **MP3 落盘路径**由 `output_dir` + 日期 + uuid 组成。如要调整磁盘布局，请同时更新 `GET /api/audio/{audio_id}` 的查找逻辑。
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
- **语音列表**：`config.py` 中的 `StaticVoices.items` 是兜底列表；已验证的 9 个 voice id（`mimo_default`、`冰糖`、`茉莉`、`苏打`、`白桦`、`Mia`、`Chloe`、`Milo`、`Dean`）驱动 `GET /api/voices`。
- **生成的音频位于 `outputs/`，已被 gitignore**（仅 `.gitkeep` 受版本控制）—— 不要提交 MP3 文件。

## 修改前必读
- `README.md` —— 完整流水线、环境变量、API 契约以及真实 API 验证日志。
- `app/config.py` —— 提示词、环境变量前缀、`TtsSettings` 请求体的唯一权威来源。
- `app/main.py` —— 装配顺序（dotenv → settings → services → router configure → lifespan）。
- `run_e2e_xiaomi.py` —— 当 mock 测试与真实接口出现偏差时，对照此文件确认 M3（LangChain）+ MiMo（HTTP）真实请求形态。