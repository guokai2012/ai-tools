"""Application configuration.

All TTS and LLM parameters are externalized here so request payloads can
be tweaked without touching code. Override values via environment variables
prefixed with TTS__ / LLM__ / APP__ (nested sections use double underscore).
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LlmSettings(BaseSettings):
    """MiniMax M3 chat-completions settings (Anthropic Messages API shape)."""

    api_key: str = Field(default="", description="MiniMax M3 API key (x-api-key header)")
    base_url: str = Field(default="https://api.minimaxi.com/anthropic")
    messages_path: str = Field(default="/v1/messages")
    model: str = Field(default="MiniMax-M3")
    api_version: str = Field(default="2023-06-01")
    max_tokens: int = Field(default=8192)
    temperature: float = Field(default=0.2)

    response_text_path: str = Field(default="content.0.text")

    request_timeout_sec: float = Field(default=60.0)
    max_retries: int = Field(default=2)

    model_config = SettingsConfigDict(env_prefix="LLM__", case_sensitive=False)

    @property
    def messages_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.messages_path}"

    def build_request_body(self, system: str, user_text: str) -> dict:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": system,
            "messages": [{"role": "user", "content": user_text}],
        }


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
    static_dir: Path = Field(default="./app/static", description="Static UI dir")

    tts: TtsSettings = Field(default_factory=TtsSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)

    max_md_size_kb: int = 1024
    max_normalized_chars: int = 50_000

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


M3_SYSTEM_PROMPT = """你是一个 TTS 文本预处理助手。给定一段已经从 Markdown 中提取出的半成品文本，请按以下规则输出**最终用于语音合成的纯文本**：

1. **校验并修正 Markdown 残留**：去掉遗漏的 #、*、_、>、表格分隔、列表符号。
2. **替换不可朗读内容**：
   - URL → 读作 "网址" 或简述（如 "某网站链接"）。
   - 邮箱 → 读作 "邮箱地址" 或按字面拼读 @ 与点。
   - 代码片段 / 路径 / 哈希值 → 转为自然语言（"一段代码"、"文件路径"、"哈希值"）。
   - 表情符号 → 替换为口语化描述或直接删除。
   - 数学公式 → 用口语化中文描述（如 "x 的平方"）。
3. **断句优化**：在长句中合适位置插入逗号或句号，避免 TTS 一口气读完。
4. **段落保留**：保留原文的段落空行，让 TTS 有自然停顿。**不要输出任何 markdown 标记**。
5. **长度控制**：如果文本超过 8000 字符，做合理摘要压缩，但保留核心信息。
6. **语言保真**：如果原文主要是英文，就保留英文；如果中英混排，保留原文混排，不要强行翻译。
7. **直接输出最终文本**：不要加任何前言、解释、代码块或引号包裹。"""