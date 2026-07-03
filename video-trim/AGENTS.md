# AGENTS.md

为在 `D:\workspace\video-trim` 中工作的 AI 编码代理提供的说明。

## 项目概述

批量视频裁剪工具:一个通过 ffmpeg 批量处理视频文件的 Flask Web 应用。用户从输入文件夹中选择视频、选择处理模式,后端随之运行 ffmpeg/ffprobe 任务并把结果写入输出文件夹。

本仓库**结构扁平,没有构建系统、没有测试、没有依赖清单,也未纳入 git 版本管理**,仅有 4 个文件:

- `app.py` —— Flask HTTP 服务器 + 单个后台工作线程。
- `ad_remover.py` —— 基于图像哈希的 ffmpeg 辅助库(帧匹配、剪切/分割/拼接)。
- `index.html` —— 单页前端(原生 JS,通过 CDN 引入 Tailwind)。
- `favicon.ico`

## 运行与依赖

```bash
python app.py        # 监听 0.0.0.0:8080,debug=True,use_reloader=False
```

Python 依赖**未声明**(没有 requirements/pyproject)。需手动安装:
`flask`、`Pillow`、`imagehash`。

外部二进制程序 `ffmpeg` 与 `ffprobe` 必须在 `PATH` 中 —— 通过 `subprocess` 调用。

## 处理模式

在 `trim_worker`(`app.py`,约 97–389 行)中分发,每种模式由对应的 `validate_*` 函数(`app.py`,约 606–751 行)校验:

`start`(裁剪开头 N 秒)· `end`(裁剪结尾 N 秒)· `middle`(切掉中间片段,无损拼接)· `extract`(保留指定时间窗)· `ad_remove`(通过感知图像哈希与样本比对定位广告并剪切)· `sample_split`(在每个样本匹配的时间点处分割)· `watermark_remove`(ffmpeg `delogo`,仅对带水印片段重新编码)。

## 架构边界

- **HTTP 层**(`app.py`):路由 `/`、`/api/videos`、`/api/samples`、`/api/outputs`、`/api/trim`(POST)。
- **任务队列**(`app.py`):一个 `queue.Queue`,由**单个守护工作线程**消费 —— 任务严格串行执行,耗时较长的 ffmpeg 任务会阻塞后续任务。任务以 `(filename, mode, trim_param)` 元组表示,并按文件名去重。
- **媒体处理库**(`ad_remover.py`):采用**防御性导入**,并由 `AD_REMOVER_AVAILABLE` 守护。导入失败或缺失不会导致服务器崩溃,但会禁用 `ad_remove` 与 `sample_split` 模式。
- **前端**(`index.html`):仅通过 `/api/*` JSON 接口与后端通信;每 10 秒轮询一次 `/api/outputs`。不使用服务端模板。

## 需同步的契约

`POST /api/trim` 请求体:`{filenames: [...], mode, ...各模式参数}` → 成功返回 `{message}`,失败返回 `{error}` 并附带 HTTP 400。

`app.py` 中的校验函数与 `index.html` 中的 JS(`startTrim` / `handleModeChange`)是事实上的规范。**对模式或参数的任何改动都必须在两个文件中同步更新** —— 此外没有其它可信来源。

## 编码约定

- 界面文本、代码注释、日志信息和错误字符串均为**简体中文**(常配合 f-string 插值)。
- `app.py` 使用 `app.logger`(Flask 日志器);`ad_remover.py` 使用 `logging.getLogger(__name__)`。两者共用格式 `[%(asctime)s] %(levelname)s in %(module)s: %(message)s`。日志通过 f-string 立即拼接字符串。
- 可选/延迟导入一律**在函数内部内联进行**(例如 `trim_worker` 内的 `import uuid` 和 `from ad_remover import ...`)。新增可选功能时请保留此模式。
- ffmpeg 通过 `subprocess.run(..., check=True)` 调用;失败以 `subprocess.CalledProcessError` 捕获,并以 `app.logger.exception(...)` 记录的兜底 `except Exception` 处理。
- 失败的输出文件会被删除;临时目录使用 `uuid.uuid4().hex` 命名,并在 `try/finally` 中以 `shutil.rmtree(..., ignore_errors=True)` 清理。
- 全程使用 `snake_case`;`ad_remover.py` 使用类型注解(typing),`app.py` 不使用。

## 陷阱与平台限制

- **硬编码的 Linux 路径** `INPUT_DIR=/videos/input`、`OUTPUT_DIR=/videos/output`、`SAMPLE_DIR=/videos/sample`(`app.py`,约 38–40 行),通过 `os.makedirs(..., exist_ok=True)` 自动创建。**没有环境变量覆盖**;在 Windows/非容器化主机上会落到驱动器根目录(例如 `D:\videos\...`),这与预期不符。
- **端口 8080 为固定值**,前后端依赖一致。
- `debug=True` 会在 `0.0.0.0:8080` 上暴露 Werkzeug 调试器(reloader 已关闭)—— 在涉及部署/安全的工作中需注意这一点。
- ffmpeg 的**流拷贝拼接**(concat demuxer + `concat_list.txt`)要求各片段编解码参数一致,否则拼接会失败。
- `ad_remove`/`sample_split` 的输出带有 `_clean` 后缀;工作线程的通用成功日志引用的是原始 `output_path`,在这些模式下可能产生误导。
- **仓库中没有文档文件。** 敏感区域的权威参考只有 `app.py`(工作线程分发 + `validate_*`)与 `index.html`(请求载荷)。在修改模式、路径或 `/api/trim` 契约之前,请先阅读这两个文件。
