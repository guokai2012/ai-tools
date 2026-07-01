# ai-tools

一个**多项目集合仓库**，把作者个人维护的几款 AI / 媒体 / 游戏小工具汇聚在一起。每个子目录都是一个相对独立的项目，拥有自己的依赖、技术栈和启动方式。

> 本仓库使用 **monorepo 风格** —— 子目录之间没有共享代码或构建系统，请进入对应目录单独运行。

## 仓库信息

| 项 | 值 |
|---|---|
| 远程地址 | `https://github.com/guokai2012/ai-tools.git` |
| 默认分支 | `main` |
| 许可证 | Apache License 2.0（见根目录 `LICENSE`） |
| 根级忽略文件 | `.gitignore`（仅忽略 `.env` / `.env.local` / `*.log` 等敏感与日志文件） |

## 子项目一览

| 子目录 | 类型 | 技术栈 | 一句话简介 |
|---|---|---|---|
| [`txt2tts/`](./txt2tts/) | 后端服务 | Python · FastAPI · httpx | 把任意 `.md` 转成 MP3：本地清洗 → **MiniMax M3** 标准化 → **小米 MiMo** TTS 合成 → 流式播放 |
| [`video-trim/`](./video-trim/) | 后端服务 | Python · Flask · ffmpeg | 批量视频裁剪工具：裁剪头尾、抽取片段、去广告、片段分割、去水印 |
| [`ultraman-m78-rescue/`](./ultraman-m78-rescue/) | 前端小游戏 | 纯 HTML/CSS/JS（无构建） | 双人 2D 闯关：赛罗与迪迦拯救 M78 星云 |

---

## 1. `txt2tts/` — Markdown 转语音

把任何 `.md` 文件扔进去，**先经 MiniMax M3 标准化**，再调用 **小米 MiMo (`mimo-v2.5-tts`)** 合成 MP3，保存在本地随时重听。

### 处理流程

```
.md 上传
  → MarkdownService.to_plain_text()       本地基础清洗（去 #、代码块、表格等）
  → LlmNormalizer.normalize(text)        MiniMax M3 标准化
        · 替换 URL / 邮箱 / 代码 / 公式 → 口语化
        · 长句插入标点
        · 控制字数 < 8000
        · 保留段落停顿
  → TtsClient.synthesize(normalized)     小米 MiMo 多模态合成
        POST /v1/chat/completions
        modalities=["text","audio"]
        audio={voice: <voice>, format: "mp3"}
        → choices[0].message.audio.data (base64 mp3)
  → AudioStorageService.save(bytes)      写入 outputs/<日期>/<uuid>.mp3
  → GET /api/audio/{audio_id}            浏览器 <audio> 流式播放
```

### 功能

- 📁 选择本地 `.md`（也支持 `.markdown` / `.txt`）
- 🧹 本地清洗 + **MiniMax M3** LLM 标准化
- 🎙️ **小米 MiMo `mimo-v2.5-tts`** 多语音合成（9 个已验证 voice_id）
- 💾 MP3 持久化到 `outputs/<日期>/<uuid>.mp3`
- ▶️ 内置播放器（支持进度条拖动 + 下载）
- 📜 最近 10 条会话历史

### 快速开始

```powershell
# 1. 装依赖
cd txt2tts
D:\anaconda3\python.exe -m pip install -r requirements.txt

# 2. 配置双服务凭证
$env:LLM__API_KEY = "<你的 MiniMax M3 API Key>"
$env:TTS__API_KEY  = "<你的 小米 MiMo API Key>"

# 3. 启动
D:\anaconda3\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 4. 浏览器访问 http://127.0.0.1:8000
```

### 验证过的 voice_id（小米 MiMo）

`mimo_default`、`冰糖`、`茉莉`、`苏打`、`白桦`、`Mia`、`Chloe`、`Milo`、`Dean`（共 9 个，UI 会自动列出）。

### API 端点（FastAPI）

| Method | Path | 说明 |
|---|---|---|
| `GET`  | `/`                     | 跳转 UI |
| `GET`  | `/static/index.html`    | 前端 UI |
| `GET`  | `/api/health`           | 健康检查 + 配置状态 |
| `GET`  | `/api/voices`           | 语音列表 |
| `POST` | `/api/preview`          | 仅跑 M3 标准化，返回 normalized 文本 |
| `POST` | `/api/synthesize`       | 完整 pipeline：M3 → TTS → 落盘（multipart/form-data） |
| `GET`  | `/api/audio/{audio_id}` | 流式播放 MP3 |
| `GET`  | `/api/storage/stats`    | outputs/ 占用统计 |
| `GET`  | `/docs`                 | FastAPI Swagger UI |

> 失败处理：M3/TTS 调用失败 → **502**；空 `.md` → **422**；超过 1MB → **413**。

### 测试与端到端验证

```bash
# 单元测试（respx mock，约 23 用例，无需 API Key）
D:\anaconda3\python.exe -m pytest tests/ -v

# 真实端到端：读 samples/demo.md → 真实 M3 → 真实 MiMo → 落盘 → Windows 播放器播放
set LLM__API_KEY=<你的 M3 key>
set TTS__API_KEY=<你的 MiMo key>
D:\anaconda3\python.exe run_e2e_xiaomi.py
```

> 详细环境变量、目录结构与实现说明见 [`txt2tts/README.md`](./txt2tts/README.md)；给 AI 代理的工程约束见 [`txt2tts/AGENTS.md`](./txt2tts/AGENTS.md)。

---

## 2. `video-trim/` — 批量视频裁剪工具

基于 **Flask + ffmpeg** 的批量视频处理 Web 应用。用户从输入文件夹选视频、选模式，后端串行运行 ffmpeg/ffprobe 任务并把结果写入输出文件夹。

### 功能（7 种处理模式）

| 模式 | 用途 |
|---|---|
| `start` | 裁剪开头 N 秒 |
| `end` | 裁剪结尾 N 秒 |
| `middle` | 切掉中间片段（流复制 + 无损拼接） |
| `extract` | 保留指定时间窗 |
| `ad_remove` | 通过感知图像哈希与样本比对定位广告并剪切（输出带 `_clean` 后缀） |
| `sample_split` | 在每个样本匹配的时间点分割 |
| `watermark_remove` | ffmpeg `delogo`，仅对带水印片段重新编码 |

### 运行

```bash
cd video-trim
python app.py        # 监听 0.0.0.0:8080（debug=True，reloader 已关闭）
```

**前置条件**：
- Python 包：`flask`、`Pillow`、`imagehash`（未声明依赖，需手动安装）。
- `ffmpeg`、`ffprobe` 必须在 `PATH` 中。
- 当前硬编码的目录为 `/videos/input`、`/videos/output`、`/videos/sample`（Linux 路径），容器外需注意。

### 架构

- **HTTP 层**（`app.py`）：路由 `/`、`/api/videos`、`/api/samples`、`/api/outputs`、`/api/trim`。
- **任务队列**：单个 `queue.Queue` + 单守护线程 → **任务严格串行执行**。
- **媒体库**（`ad_remover.py`）：防御性导入，`AD_REMOVER_AVAILABLE` 守护。
- **前端**（`index.html`）：原生 JS + Tailwind（CDN），每 10 秒轮询 `/api/outputs`。

> 完整的代理说明（契约、约定、陷阱）见 [`video-trim/AGENTS.md`](./video-trim/AGENTS.md)。

---

## 3. `ultraman-m78-rescue/` — M78 星云营救（2D 闯关）

双人 2D 闯关：**赛罗**与**迪迦**拯救 M78 星云。**纯前端、无构建**，开箱即玩。

### 运行

```bash
cd ultraman-m78-rescue
npm start            # 等价于 npx serve . -l 5173
# 或直接用任意静态服务器（如 python -m http.server 5173）打开 index.html
```

### 文件清单

```
ultraman-m78-rescue/
├── index.html              # 入口，按顺序加载 utils / fx / input / levelData / game
├── package.json            # name=ultraman-m78-rescue，仅包含启动脚本
├── css/style.css
└── js/
    ├── utils.js            # 工具函数
    ├── fx.js               # 特效
    ├── input.js            # 输入处理
    ├── levelData.js        # 关卡数据
    └── game.js             # 主循环
```

> 本项目目前**未纳入 git 版本管理**（仓库根 `.gitignore` 也未单独为它配置）。

---

## 全局约定

- **语言**：界面文本、注释、日志统一使用**简体中文**（每个子项目自定）。
- **安全**：根 `.gitignore` 忽略 `.env` / `.env.local` / `*.log`；**严禁提交任何 API Key 或 Bearer Token**。
- **子项目独立**：每个子目录自包含依赖与启动方式，不要跨项目共享代码。

## 贡献流程

1. 切到对应子目录工作（仓库根只承担"目录索引"职能）。
2. 修改前阅读该子项目的 `README.md` 与 `AGENTS.md`（如有）。
3. 提交信息建议使用 Conventional Commits，例如：
   ```
   feat(txt2tts): 新增 SRT 字幕导出
   fix(video-trim): 修复流复制拼接时 codec 不匹配的报错
   docs(ai-tools): 补全根 README 的子项目索引
   ```

## 许可证

Apache License 2.0 —— 详见 [`LICENSE`](./LICENSE)。