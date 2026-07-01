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
TtsClient.synthesize(normalized)       ← 小米 MiMo mimo-v2.5-tts 合成
  │   POST /v1/chat/completions
  │   modalities=["text","audio"]
  │   audio={voice: <voice>, format: "mp3"}
  │   ← choices[0].message.audio.data (base64 mp3)
  │
  ▼
AudioStorageService.save(bytes)        ← 写入 outputs/<日期>/<uuid>.mp3
  │
  ▼
GET /api/audio/{audio_id}              ← 浏览器 <audio> 流式播放
```

## 功能

- 📁 选择本地 `.md` 文件（也支持 `.markdown` / `.txt`）
- 🧹 本地清洗 + **MiniMax M3** LLM 标准化
- 🎙️ **小米 MiMo mimo-v2.5-tts** 多语音合成
- 💾 MP3 持久化到 `outputs/<日期>/<uuid>.mp3`
- ▶️ 内置播放器（支持进度条拖动 + 下载）
- 📜 最近 10 条会话历史

## 快速开始

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

### MiniMax M3（`LLM__*`，Anthropic Messages API）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `LLM__API_KEY` | _(空)_ | M3 API Key（用作 `x-api-key`） |
| `LLM__BASE_URL` | `https://api.minimaxi.com/anthropic` | M3 endpoint base |
| `LLM__MESSAGES_PATH` | `/v1/messages` | 路径 |
| `LLM__MODEL` | `MiniMax-M3` | 模型 id |
| `LLM__API_VERSION` | `2023-06-01` | Anthropic 版本头 |
| `LLM__MAX_TOKENS` | `8192` | M3 单次最大输出 token |
| `LLM__TEMPERATURE` | `0.2` | 温度 |
| `LLM__REQUEST_TIMEOUT_SEC` | `60.0` | 超时 |
| `LLM__MAX_RETRIES` | `2` | 5xx 重试次数 |

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

`.env` 文件也支持（pydantic-settings 自动加载）。

## API 端点

| Method | Path | 说明 |
|---|---|---|
| `GET`  | `/`                       | 跳转到 UI |
| `GET`  | `/static/index.html`      | 前端 UI |
| `GET`  | `/api/health`             | 健康检查 + M3/TTS 配置状态 |
| `GET`  | `/api/voices`             | 语音列表 |
| `POST` | `/api/preview`            | 跑 M3 标准化，返回 normalized 文本 |
| `POST` | `/api/synthesize`         | 完整 pipeline：M3 → TTS → 落盘 |
| `GET`  | `/api/audio/{audio_id}`   | 流式播放 MP3 |
| `GET`  | `/api/storage/stats`      | outputs/ 占用统计 |
| `GET`  | `/docs`                   | FastAPI Swagger UI |

`POST /api/synthesize` 是 multipart/form-data：

```bash
curl -X POST http://127.0.0.1:8000/api/synthesize \
  -F "file=@samples/demo.md" \
  -F "voice_id=冰糖"
```

## 失败处理

| 情况 | 行为 |
|---|---|
| M3 调用失败 / 超时 / 5xx | `POST /api/synthesize` 和 `POST /api/preview` 立即返回 **502** |
| MiMo TTS 调用失败 | 返回 502 |
| 用户上传空 md | 返回 422 |
| 上传 > 1MB | 返回 413 |

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
│   ├── routers\tts.py        # REST 路由
│   ├── services\
│   │   ├── markdown_service.py  # md → 纯文本
│   │   ├── llm_normalizer.py    # MiniMax M3 (Anthropic Messages)
│   │   ├── tts_client.py        # 小米 MiMo (chat/completions multimodal)
│   │   └── audio_storage.py     # MP3 落盘
│   ├── models\schemas.py     # Pydantic DTO
│   └── static\               # 前端
├── samples\demo.md
├── outputs\                  # 生成的 MP3
├── tests\
│   ├── test_markdown.py      # 4 个本地清洗测试
│   ├── test_llm_normalizer.py# 10 个 M3 测试（respx mock）
│   └── test_tts_client.py    # 9 个 MiMo TTS 测试（respx mock）
├── run_e2e_xiaomi.py         # 真实 API 端到端脚本
├── requirements.txt
├── pytest.ini
└── README.md
```

## 测试

```bash
D:\anaconda3\python.exe -m pytest tests/ -v
```

预期：**23 passed**（4 markdown + 10 llm_normalizer + 9 tts_client）。

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

- **MiniMax M3**（Anthropic Messages API）
  - `POST {LLM__BASE_URL}/v1/messages`
  - Header: `x-api-key: <LLM__API_KEY>` + `anthropic-version: 2023-06-01`
  - 响应取 `content[0].text`
- **小米 MiMo TTS**（chat/completions 多模态）
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