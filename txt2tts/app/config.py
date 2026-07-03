"""Application configuration.

All TTS and LLM parameters are externalized here so request payloads can
be tweaked without touching code. Override values via environment variables
prefixed with TTS__ / LLM__ / APP__ (nested sections use double underscore).
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Provider 选择常量
PROVIDER_MIMO = "mimo"          # 原始方案：M3 + MiMo
PROVIDER_EDGE = "edge"          # 新方案：M3 + edge-tts + ffmpeg
PROVIDERS: List[str] = [PROVIDER_MIMO, PROVIDER_EDGE]


# Edge-tts 中文常用 voice（来自 Microsoft Edge 官方列表）
EDGE_VOICES_ZH = [
    {"id": "zh-CN-XiaoxiaoNeural",      "name": "晓晓（女声·温柔）", "lang": "zh-CN"},
    {"id": "zh-CN-YunxiNeural",         "name": "云希（男声·阳光）", "lang": "zh-CN"},
    {"id": "zh-CN-YunjianNeural",       "name": "云健（男声·体育）", "lang": "zh-CN"},
    {"id": "zh-CN-YunyangNeural",       "name": "云扬（男声·专业）", "lang": "zh-CN"},
    {"id": "zh-CN-XiaoyiNeural",        "name": "晓伊（女声·活力）", "lang": "zh-CN"},
    {"id": "zh-CN-YunxiaNeural",        "name": "云夏（男声·少年）", "lang": "zh-CN"},
    {"id": "zh-CN-XiaomengNeural",      "name": "晓梦（女声·儿童）", "lang": "zh-CN"},
    {"id": "zh-CN-XiaomoNeural",        "name": "晓墨（女声·情感）", "lang": "zh-CN"},
    {"id": "zh-CN-XiaoruiNeural",       "name": "晓睿（女声·成熟）", "lang": "zh-CN"},
    {"id": "zh-CN-XiaoshuangNeural",    "name": "晓双（女声·儿童）", "lang": "zh-CN"},
    {"id": "zh-CN-XiaoxuanNeural",      "name": "晓萱（女声·甜美女声）", "lang": "zh-CN"},
    {"id": "zh-CN-XiaoyouNeural",       "name": "晓悠（女声·童声）", "lang": "zh-CN"},
    {"id": "zh-CN-XiaozhenNeural",      "name": "晓甄（女声·新闻）", "lang": "zh-CN"},
]


class EdgeTtsSettings(BaseSettings):
    """edge-tts 配置（方案二）。edge-tts 不需要 API key；运行依赖 ffmpeg 合并。"""

    ffmpeg_path: str = Field(
        default="./bin/ffmpeg.exe",
        description=(
            "ffmpeg 可执行文件路径；方案二需要它拼接片段音频和烧录字幕。"
            "本地 Windows 默认 ./bin/ffmpeg.exe；Docker 镜像里通常 /usr/local/bin/ffmpeg。"
        ),
    )
    ffprobe_path: str = Field(
        default="./bin/ffprobe.exe",
        description="ffprobe 可执行文件路径；用于读取片段音频时长。",
    )
    default_voice: str = Field(default="zh-CN-XiaoxiaoNeural")
    rate: str = Field(default="+0%", description="语速，edge-tts 形如 +0% / +10% / -10%")
    volume: str = Field(default="+0%", description="音量，edge-tts 形如 +0% / +10% / -10%")
    pitch: str = Field(default="+0Hz", description="音调，edge-tts 形如 +0Hz / +5Hz / -5Hz")
    max_segment_chars: int = Field(default=200, description="每段最长字符数，避免单段过长")
    request_timeout_sec: float = Field(default=30.0)

    # ---- ffmpeg / ffprobe 子进程超时 ----
    # 这两个跟 ffmpeg 操作绑死：单 mp3 探时长极快（10s 足够），
    # ffmpeg 合并受文件数 / 时长影响，按经验给 120s 缓冲。
    ffprobe_timeout_sec: float = Field(
        default=10.0, ge=1.0,
        description="ffprobe 探测单个 mp3 时长的子进程超时（EDGE__FFPROBE_TIMEOUT_SEC）",
    )
    ffmpeg_concat_timeout_sec: float = Field(
        default=120.0, ge=1.0,
        description="edge provider ffmpeg 合并片段的子进程超时（EDGE__FFMPEG_CONCAT_TIMEOUT_SEC）",
    )
    # mimo provider 流水线里的 ffmpeg concat：通常合成 5-10 段 mp3，
    # 留 600s 兜底。
    mimo_ffmpeg_concat_timeout_sec: float = Field(
        default=600.0, ge=1.0,
        description="mimo provider ffmpeg 合并的子进程超时（EDGE__MIMO_FFMPEG_CONCAT_TIMEOUT_SEC）",
    )

    # 重试：edge-tts 调用微软 speech.platform.bing.com 时偶发网络抖动
    # （DNS / TCP / SSL 任一层都可能瞬时失败）。这里配置自动重试次数与
    # 指数退避起始秒数。仅对网络类异常重试，参数 / 业务错误不重试。
    max_retries: int = Field(default=3, ge=0, description="edge-tts 瞬时网络失败重试次数")
    retry_backoff_sec: float = Field(
        default=1.0, ge=0.0,
        description="指数退避起始秒数（1s → 2s → 4s ...）",
    )

    model_config = SettingsConfigDict(env_prefix="EDGE__", case_sensitive=False)


class LlmSettings(BaseSettings):
    """MiniMax M3 settings via LangChain ChatAnthropic (Anthropic Messages API).

    Internally we delegate HTTP / auth / retry to the LangChain SDK; this class
    only carries the knobs the application cares about.
    """

    api_key: str = Field(default="", description="MiniMax M3 API key")
    base_url: str = Field(default="https://api.minimaxi.com/anthropic")
    model: str = Field(default="MiniMax-M3")
    max_tokens: int = Field(default=8192)
    temperature: float = Field(default=0.2)

    request_timeout_sec: float = Field(default=60.0)
    max_retries: int = Field(default=2)

    model_config = SettingsConfigDict(env_prefix="LLM__", case_sensitive=False)


class TtsSettings(BaseSettings):
    """Xiaomi MiMo TTS settings (chat/completions multimodal audio shape).

    Real endpoint verified by probe:
      POST {base_url}/v1/chat/completions
      Headers: Authorization: Bearer <api_key>
      Body must include:
        model: mimo-v2.5-tts
        modalities: ["text", "audio"]
        audio: {voice: <voice>, format: "mp3"}
        messages: [
          {role: "user",      content: "朗读：..."},
          {role: "assistant", content: "<the text to synthesize>"}
        ]
      Response: choices[0].message.audio.data = base64-encoded mp3 bytes.
    """

    api_key: str = Field(default="", description="Xiaomi MiMo Bearer token")
    base_url: str = Field(default="https://api.xiaomimimo.com")
    chat_path: str = Field(default="/v1/chat/completions")
    model: str = Field(default="mimo-v2.5-tts")
    voice: str = Field(default="mimo_default", description="TTS voice id")
    audio_format: str = Field(default="mp3", description="mp3 or wav")

    # 单次请求字符数上限（避免 MiMo 8K token 上下文超限）。M3 输出若超此阈值，
    # TtsClient 会自动按句末标点切分、多次合成、拼接 mp3 bytes。
    max_input_chars_per_request: int = Field(default=6000, ge=100)

    # Optional sampling params passed through to the chat completion.
    temperature: float = Field(default=0.6)

    # Network
    request_timeout_sec: float = Field(default=90.0)
    max_retries: int = Field(default=2)

    model_config = SettingsConfigDict(env_prefix="TTS__", case_sensitive=False)

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.chat_path}"

    def build_request_body(self, text: str, voice: str | None = None) -> dict:
        """Build a chat-completions body that requests TTS synthesis.

        `text` is the text to be read aloud. The user message frames it as a
        'read this' request, and the assistant message contains the literal
        text — the model then returns the assistant message's content as
        synthesized audio (base64 in `choices[0].message.audio.data`).
        """
        v = voice or self.voice
        return {
            "model": self.model,
            "modalities": ["text", "audio"],
            "audio": {"voice": v, "format": self.audio_format},
            "temperature": self.temperature,
            "messages": [
                {"role": "user", "content": f"请朗读下面这段话：{text}"},
                {"role": "assistant", "content": text},
            ],
        }


class StaticVoices:
    """Fallback voice list. Real values are listed below; fetched from MiMo
    /v1/models for the list of available TTS models."""

    items: List[dict] = [
        {"id": "mimo_default", "name": "默认女声", "lang": "zh"},
        {"id": "冰糖",          "name": "冰糖（女声）", "lang": "zh"},
        {"id": "茉莉",          "name": "茉莉（女声）", "lang": "zh"},
        {"id": "苏打",          "name": "苏打（女声）", "lang": "zh"},
        {"id": "白桦",          "name": "白桦（男声）", "lang": "zh"},
        {"id": "Mia",           "name": "Mia",          "lang": "en"},
        {"id": "Chloe",         "name": "Chloe",        "lang": "en"},
        {"id": "Milo",          "name": "Milo",         "lang": "en"},
        {"id": "Dean",          "name": "Dean",         "lang": "en"},
    ]


class AppSettings(BaseSettings):
    """Top-level application settings."""

    app_name: str = "txt2tts"
    host: str = "127.0.0.1"
    port: int = 8000
    reload: bool = True

    output_dir: Path = Field(default=Path("./outputs"))
    library_db_filename: str = Field(default="library.db", description="SQLite file name under output_dir")
    static_dir: Path = Field(default="./app/static", description="Static UI dir")

    tts: TtsSettings = Field(default_factory=TtsSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    edge: EdgeTtsSettings = Field(default_factory=EdgeTtsSettings)

    # 方案切换：默认原方案（mimo）；通过 APP__TTS_PROVIDER=edge 切换新方案
    tts_provider: str = Field(default=PROVIDER_MIMO)

    max_md_size_kb: int = 1024
    max_normalized_chars: int = 50_000

    # ---- 后台任务看门狗（防止 M3 / TTS 调用卡死导致任务永远停在 processing） ----
    # 单个任务停留在同一阶段的最大秒数；超过则 watchdog 自动标 failed_retryable。
    # 默认 180s ≈ LLM__REQUEST_TIMEOUT_SEC 的 1×，给 M3 一次重试留余地。
    task_stall_timeout_sec: float = Field(default=180.0, ge=10.0)
    # watchdog 协程扫描间隔（秒）。越小越灵敏，CPU 开销也越大。
    task_watchdog_interval_sec: float = Field(default=10.0, ge=1.0)
    # 看门狗总开关；调试时可设 False 关闭。
    task_watchdog_enabled: bool = Field(default=True)

    # ---- 可 env 化的"硬编码常量"：system prompts + voice 列表 ----
    # 默认 None；通过 env 覆盖。Accessor 函数 ``get_m3_system_prompt()`` 等
    # 优先返回 env 值，否则 fallback 到模块级常量。
    m3_system_prompt: Optional[str] = Field(
        default=None,
        description=(
            "覆盖内置的 M3 标准化 system prompt（APP__M3_SYSTEM_PROMPT）。"
            "多行用 \\n 转义。"
        ),
    )
    split_system_prompt: Optional[str] = Field(
        default=None,
        description="覆盖内置的 M3 文档分块 system prompt（APP__SPLIT_SYSTEM_PROMPT）。"
        "保留 {max_chars} 占位符。",
    )
    semantic_preprocess_prompt: Optional[str] = Field(
        default=None,
        description=(
            "覆盖内置的 edge 方案二专用 M3 语义预处理 system prompt"
            "（APP__SEMANTIC_PREPROCESS_PROMPT）。"
        ),
    )
    mimo_voices_json: Optional[str] = Field(
        default=None,
        description=(
            "覆盖 MiMo 静态 voice 列表（APP__MIMO_VOICES_JSON）。"
            "JSON 数组，每项含 id / name / lang。覆盖失败则回落默认。"
        ),
    )
    edge_voices_json: Optional[str] = Field(
        default=None,
        description=(
            "覆盖 edge-tts voice 白名单（APP__EDGE_VOICES_JSON）。"
            "JSON 数组，每项含 id / name / lang。覆盖失败则回落默认。"
        ),
    )

    model_config = SettingsConfigDict(
        env_prefix="APP__",
        env_nested_delimiter="__",
        case_sensitive=False,
    )


def get_settings() -> AppSettings:
    """Singleton accessor (cheap; pydantic-settings caches internally).

    The settings classes already declare env_file=".env" in their model_config,
    so pydantic-settings auto-loads it on first instantiation.
    """
    return AppSettings()


# ---- Accessor 函数：覆盖内置常量 ------------------------------------------
#
# 这些函数让代码通过统一的入口读取"原本硬编码"的常量：
#   * 默认值仍然是模块级常量（保持向后兼容）
#   * env 覆盖通过 APP__* 字段生效（pydantic-settings 自动加载）
#   * voice 列表从 JSON 字符串解析；解析失败 → fallback 到默认
#
# 不直接调用 ``get_settings()`` 每处都实例化，而是用 ``functools.lru_cache``
# 在第一次访问时缓存（pydantic-settings 已经很快了，缓存更多是为了让
# 测试能 ``settings.cache_clear()`` 强制重读 env）。

import functools
import json as _json
import logging as _logging

_logger = _logging.getLogger(__name__)


@functools.lru_cache(maxsize=1)
def _settings_cached() -> "AppSettings":
    return AppSettings()


@functools.lru_cache(maxsize=1)
def _mimo_voices_cached() -> List[dict]:
    raw = _settings_cached().mimo_voices_json
    if raw:
        try:
            data = _json.loads(raw)
            if isinstance(data, list) and all(isinstance(x, dict) for x in data):
                return data  # type: ignore[return-value]
            _logger.warning("APP__MIMO_VOICES_JSON 解析成功但不是 list[dict]，回落默认")
        except Exception as exc:
            _logger.warning("APP__MIMO_VOICES_JSON 解析失败（%s），回落默认", exc)
    return list(StaticVoices.items)


@functools.lru_cache(maxsize=1)
def _edge_voices_cached() -> List[dict]:
    raw = _settings_cached().edge_voices_json
    if raw:
        try:
            data = _json.loads(raw)
            if isinstance(data, list) and all(isinstance(x, dict) for x in data):
                return data  # type: ignore[return-value]
            _logger.warning("APP__EDGE_VOICES_JSON 解析成功但不是 list[dict]，回落默认")
        except Exception as exc:
            _logger.warning("APP__EDGE_VOICES_JSON 解析失败（%s），回落默认", exc)
    return list(EDGE_VOICES_ZH)


def get_m3_system_prompt() -> str:
    """获取 M3 标准化 system prompt（优先 env 覆盖，否则用内置常量）。"""
    val = _settings_cached().m3_system_prompt
    return val if val else M3_SYSTEM_PROMPT


def get_split_system_prompt() -> str:
    """获取 M3 文档分块 system prompt。注意内置版本含 ``{max_chars}`` 占位符；
    自定义 env 覆盖时也必须保留 ``{max_chars}``，否则 .format() 会 KeyError。"""
    val = _settings_cached().split_system_prompt
    return val if val else SPLIT_SYSTEM_PROMPT


def get_semantic_preprocess_prompt() -> str:
    """获取 edge 方案二专用 M3 语义预处理 system prompt。"""
    val = _settings_cached().semantic_preprocess_prompt
    return val if val else SEMANTIC_PREPROCESS_PROMPT


def get_mimo_voices() -> List[dict]:
    """获取 MiMo 静态 voice 列表。返回 list[dict]，每项含 ``id / name / lang``。"""
    return list(_mimo_voices_cached())


def get_edge_voices() -> List[dict]:
    """获取 edge-tts voice 白名单。"""
    return list(_edge_voices_cached())


def reset_settings_cache() -> None:
    """清掉 accessor 的 lru_cache（测试 / 运行中改 env 后手动调用）。"""
    _settings_cached.cache_clear()
    _mimo_voices_cached.cache_clear()
    _edge_voices_cached.cache_clear()


# M3_SYSTEM_PROMPT = """你是一个 TTS 文本预处理助手。给定一段已经从 Markdown 中提取出的半成品文本，请按以下规则输出**最终用于语音合成的纯文本**：
#
#1. **校验并修正 Markdown 残留**：去掉遗漏的 #、*、_、>、表格分隔、列表符号。
#2. **替换不可朗读内容**：
#   - URL → 读作 "网址" 或简述（如 "某网站链接"）。
#   - 邮箱 → 读作 "邮箱地址" 或按字面拼读 @ 与点。
#   - 代码片段 / 路径 / 哈希值 → 转为自然语言（"一段代码"、"文件路径"、"哈希值"）。
#   - 表情符号 → 替换为口语化描述或直接删除。
#   - 数学公式 → 用口语化中文描述（如 "x 的平方"）。
#3. **断句优化**：在长句中合适位置插入逗号或句号，避免 TTS 一口气读完。
#4. **段落保留**：保留原文的段落空行，让 TTS 有自然停顿。**不要输出任何 markdown 标记**。
#5. **长度控制**：如果文本超过 8000 字符，做合理摘要压缩，但保留核心信息。
#6. **语言保真**：如果原文主要是英文，就保留英文；如果中英混排，保留原文混排，不要强行翻译。
#7. **直接输出最终文本**：不要加任何前言、解释、代码块或引号包裹。"""

M3_SYSTEM_PROMPT = """你是一个 TTS 文本预处理助手。给定一段已经从 Markdown 中提取出的半成品文本，请按以下规则输出**最终用于语音合成的纯文本**：

1. **校验并修正 Markdown 残留**：去掉遗漏的 #、*、_、>、表格分隔、列表符号。
2. **替换不可朗读内容**：
   - URL → 直接删除。
   - 邮箱 → 直接删除。
   - 代码片段 / 路径 / 哈希值 → 直接删除。
   - 表情符号 → 替换为口语化描述或直接删除。
   - 数学公式 → 用口语化中文描述（如 "x 的平方"）。
3. **断句优化**：在长句中合适位置插入逗号或句号，避免 TTS 一口气读完。
4. **段落保留**：保留原文的段落空行，让 TTS 有自然停顿。**不要输出任何 markdown 标记**。
5. **长度控制**：如果文本超过 8000 字符，做合理摘要压缩，但保留核心信息。
6. **语言保真**：如果原文主要是英文，就保留英文；如果中英混排，保留原文混排，不要强行翻译。
7. **直接输出最终文本**：不要加任何前言、解释、代码块或引号包裹。"""


# 文档分块 system prompt（{max_chars} 是占位符，调用 .format() 注入）
SPLIT_SYSTEM_PROMPT = """你是一名文档分块助手（chunking expert）。给定一段已经被标准化的口语化长文本，请把它拆分成 **多个子文档**，每个子文档长度不超过 {max_chars} 个字符（约 6000 token）。

严格规则：
1. **语义完整**：只能在**自然的语义边界**处切分 —— 例如：段落之间、章节之间、对话角色切换、明显的"主题切换"或"时间跳转"处。**绝对禁止在句子中间断**（即使句子很长）。
2. **不重复不丢失**：所有子文档拼起来应能几乎还原原文。不要复述、补充或省略任何内容。
3. **首尾平滑**：相邻子文档的衔接处，前一块结尾应是一个完整句子（句号/问号/叹号），后一块开头应是一个新句子；不要让一句话被劈到两块。
4. **块大小均衡**：尽量让每块接近 {max_chars}，但若原文本身分段清晰，不必硬凑长度。
5. **输出格式**：子文档之间用单独一行 `---SPLIT---` 分隔（独占一行，前后空行）。**不要**输出任何前言、解释、编号、标题或 Markdown 围栏。
6. **每块是 TTS 朗读用**：保留段落空行作为自然停顿；不要在 chunk 内再插入额外的小标题。"""


# 方案二专用的语义预处理 prompt：解决 edge-tts 无语义理解、不会自动断句的短板。
# 主要任务：去除冗余、修正多音字、按句号/问号/叹号拆分长难句、为 TTS 标记朗读断句。
SEMANTIC_PREPROCESS_PROMPT = """你是一名 TTS 语义预处理助手。给定一段已经被本地清洗过的散文/教程/说明文字，请按以下规则改写后输出：

1. **删除冗余内容**：去掉不参与朗读的脚注引用标记（[1]、(2)）、Markdown 残留符号（#、*、>、表格分隔 `|---`）、连续的空白与换行。
2. **多音字校正**：根据上下文语境，输出每个多音字的"目标读音"，用形如 `行[xíng]` 或 `行[háng]` 的标记插入到原字后面，TTS 看到该标记会按目标读音朗读。常见多音字示例（不限于）：
   - 行 → xíng / háng；重 → zhòng / chóng；长 → cháng / zhǎng；得 → dé / děi / de
   - 还 → hái / huán；朝 → cháo / zhāo；觉 → jué / jiào；便 → biàn / pián
   - 藏 → cáng / zàng；薄 → báo / bó；恶 → è / wù / ě；调 → tiáo / diào
   - 尽 → jǐn / jìn；假 → jiǎ / jià；空 → kōng / kòng；乐 → lè / yuè
3. **拆分长难句**：超过 40 个汉字且只有一个逗号的句子，必须在合适位置加逗号或句号断句，确保每段 ≤ 200 字。
4. **朗读断句优化**：在长句中合适位置插入中文逗号「，」、句号「。」、问号「？」（允许）、叹号「！」（允许）来引导 TTS 节奏；删除所有英文逗号 / 句号（避免停顿过短）。
5. **段落分隔**：保留原文段落空行作为自然停顿；每段末尾用一个空行隔开。
6. **不要翻译**：原语言是中文就保持中文；中英混排也保留，不要强翻。
7. **直接输出最终文本**：不要加任何前言、解释、代码块或引号包裹。也不要输出"以下是改写后的文本"之类的话。"""