"""MiniMax M3 normalization via LangChain ChatAnthropic.

Pipeline position:
    MarkdownService.to_plain_text()  ->  LlmNormalizer.normalize()  ->  TtsClient.synthesize()

The normalizer is intentionally strict: if the M3 call fails for any reason
(timeouts, HTTP errors, malformed response, empty content), we raise
LlmNormalizationError. Per product decision, **no local fallback is applied**:
the route returns 502 to the client so the user is aware that the M3 step
did not complete successfully.

HTTP-level concerns (auth headers, request body serialization, retry, response
parsing) are delegated to LangChain's `ChatAnthropic` SDK; we only orchestrate
the system / user message framing and our own outer retry loop.
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage

from app.config import (
    LlmSettings,
    get_m3_system_prompt,
    get_semantic_preprocess_prompt,
    get_split_system_prompt,
)

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)


class LlmNormalizationError(RuntimeError):
    """Raised when the M3 normalization step fails."""


_FENCE_LINE_RE = re.compile(r"^\s*```", re.MULTILINE)
_OUTER_QUOTE_RE = re.compile(r'^[\s"\'`]+|[\s"\'`]+$')


# 3 个 system prompt（m3 标准化 / 歌词 / 分块 / 方案二语义预处理）现在统一
# 由 ``app.config`` 模块集中管理，并支持 env 覆盖：
#   APP__M3_SYSTEM_PROMPT
#   APP__LYRICS_SYSTEM_PROMPT
#   APP__SPLIT_SYSTEM_PROMPT
#   APP__SEMANTIC_PREPROCESS_PROMPT
# 详见 ``app.config.get_*_prompt()`` accessor。


class LlmNormalizer:
    """Async wrapper for a single M3 ChatAnthropic call."""

    def __init__(self, settings: LlmSettings) -> None:
        self._settings = settings
        self._client: Optional[ChatAnthropic] = None  # lazy

    # -- public API ----------------------------------------------------------

    async def normalize(self, text: str, *, system: Optional[str] = None) -> str:
        """Send `text` to M3 and return its normalized reply.

        Raises LlmNormalizationError on any failure. The router turns this
        into an HTTP 502.
        """
        if not text or not text.strip():
            raise LlmNormalizationError("Refusing to normalize empty text.")
        if not self._settings.api_key:
            raise LlmNormalizationError(
                "MiniMax M3 API key is not configured. Set LLM__API_KEY env var."
            )

        client = self._get_client()
        messages = [
            SystemMessage(content=system or get_m3_system_prompt()),
            HumanMessage(content=text),
        ]
        logger.debug(
            "M3 normalize via ChatAnthropic: model=%s base=%s text_len=%d",
            self._settings.model, self._settings.base_url, len(text),
        )

        last_err: Optional[BaseException] = None
        for attempt in range(self._settings.max_retries + 1):
            try:
                resp = await client.ainvoke(messages)
            except Exception as exc:  # 网络 / 5xx / SDK 错误
                last_err = exc
                logger.warning("M3 error on attempt %d: %s", attempt + 1, exc)
                continue

            content = getattr(resp, "content", None)
            if not isinstance(content, str) or not content.strip():
                raise LlmNormalizationError(
                    f"M3 response missing text content: {repr(resp)[:300]}"
                )
            return self._post_process(content)

        raise LlmNormalizationError(f"M3 failed after retries: {last_err}")

    async def make_lyrics(self, text: str, *, system: Optional[str] = None) -> str:
        """Rewrite `text` as singable lyrics. Same retry/error contract as normalize()."""
        return await self.normalize(text, system=system or get_lyrics_system_prompt())

    async def semantic_preprocess(self, text: str, *, system: Optional[str] = None) -> str:
        """方案二专用：让 M3 做语义预处理，输出含「多音字[读音]」标记和良好断句的
        文本，供 edge-tts 分段朗读。同样的 retry/error 契约。"""
        return await self.normalize(text, system=system or get_semantic_preprocess_prompt())

    async def split_text(self, text: str, *, max_chars: int = 6000,
                        system: Optional[str] = None) -> List[str]:
        """M3 语义切分：让 M3 把长文本拆成多个不超过 max_chars 的子文档。

        返回 List[str] —— 每个元素是一个语义完整的子文档。
        重要约束：
          - **不在句子中间断**：用 M3 找合适的段落 / 章节 / 场景边界
          - **不重复 / 不丢失**：所有子文档拼接后接近原文
          - **首尾平滑**：避免前一段结尾和后一段开头是同一句话的两半

        失败抛 LlmNormalizationError；外层 pipeline 应当 failed_retryable。
        """
        prompt = system or get_split_system_prompt().format(max_chars=max_chars)
        raw = await self.normalize(text, system=prompt)
        chunks = self._parse_split_output(raw)
        if not chunks:
            raise LlmNormalizationError("M3 split_text returned no chunks")
        # 防御：单 chunk 仍超长 → fallback 用硬切
        if any(len(c) > max_chars * 1.2 for c in chunks):
            logger.warning(
                "M3 split chunks exceed max_chars=%d (sizes=%s); falling back to hard split",
                max_chars, [len(c) for c in chunks],
            )
            chunks = self._hard_split_chunks(chunks, max_chars=max_chars)
        return chunks

    @staticmethod
    def _parse_split_output(raw: str) -> List[str]:
        """解析 M3 输出。约定：子文档之间用 `---SPLIT---` 分隔（独占一行）。"""
        sep_patterns = [
            "\n---SPLIT---\n",
            "\n--- SPLIT ---\n",
            "---SPLIT---",
        ]
        chunks: List[str] = [raw.strip()]
        for sep in sep_patterns:
            if sep in chunks[0]:
                chunks = [c.strip() for c in chunks[0].split(sep)]
                break
        # 过滤空块
        return [c for c in chunks if c.strip()]

    @staticmethod
    def _hard_split_chunks(chunks: List[str], *, max_chars: int) -> List[str]:
        """当 M3 切分后的单 chunk 仍过长时，按 max_chars 硬切兜底（破坏语义边界）。"""
        import re
        out: List[str] = []
        for c in chunks:
            if len(c) <= max_chars:
                out.append(c)
                continue
            # 按句末标点切
            parts = re.split(r"(?<=[。！？!?\n])", c)
            buf = ""
            for piece in parts:
                if len(piece) > max_chars:
                    if buf:
                        out.append(buf)
                        buf = ""
                    for j in range(0, len(piece), max_chars):
                        out.append(piece[j:j + max_chars])
                elif len(buf) + len(piece) > max_chars and buf:
                    out.append(buf)
                    buf = piece
                else:
                    buf += piece
            if buf:
                out.append(buf)
        return out

    async def aclose(self) -> None:
        """Release resources. ChatAnthropic owns no explicit client; no-op."""
        return None

    # -- internals -----------------------------------------------------------

    def _get_client(self) -> ChatAnthropic:
        if self._client is None:
            self._client = ChatAnthropic(
                model=self._settings.model,
                api_key=self._settings.api_key,
                base_url=self._settings.base_url,
                max_tokens=self._settings.max_tokens,
                temperature=self._settings.temperature,
                timeout=self._settings.request_timeout_sec,
                max_retries=0,  # 外层 LlmNormalizer 独占重试，避免双层重试
            )
        return self._client

    @staticmethod
    def _post_process(content: str) -> str:
        """Strip accidental code fences and outer quotes the model may emit."""
        s = content.strip()
        lines = s.splitlines()

        # Find the first and last fence lines ``` … ```
        fence_idx = [i for i, ln in enumerate(lines) if ln.lstrip().startswith("```")]
        if fence_idx:
            first = fence_idx[0]
            last = fence_idx[-1]
            if first != last:
                # Drop the first (which may carry a language tag) and the last fence.
                inner_lines = lines[first + 1:last]
                lines = inner_lines
        s = "\n".join(lines).strip()

        s = _OUTER_QUOTE_RE.sub("", s)
        return s.strip()