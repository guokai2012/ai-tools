"""MiniMax speech-2.8-hd TTS provider.

替换原 MiMo TTS 方案（PROVIDER_MIMO）。该模型原生支持句级字幕
（``subtitle_enable=true`` + ``subtitle_type=sentence``），并与 M3 同厂商
——API key 可与 LLM 复用（``MINIMAX__API_KEY`` 留空时回落 ``LLM__API_KEY``）。

接口形态（经官方文档核对 https://platform.minimaxi.com/docs/api-reference/speech-t2a-http）：
    POST https://api.minimaxi.com/v1/t2a_v2
    Headers: Authorization: Bearer <api_key>   Content-Type: application/json
    Body:
        {
          "model": "speech-2.8-hd",
          "text": "<待合成文本>",
          "stream": false,                       # 本期固定非流式（流式 NDJSON 暂不启用）
          "voice_setting": {
            "voice_id": "male-qn-jingying",
            "speed": 1, "vol": 1, "pitch": 0,
            "emotion": "calm",
            "text_normalization": true
          },
          "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3", "channel": 1},
          "subtitle_enable": true,
          "subtitle_type": "sentence",
          "output_format": "url"                  # v3 起：要求 audio 字段返回 OSS 临时 URL
        }
    Response:
        {
          "data": {
            "audio": "https://minimax-oss.../xxx.mp3?Expires=...&Signature=...",  # OSS URL（24h 有效）
            "subtitle_file": "https://minimax-oss.../titles?...",                 # OSS URL，二次 GET
            "status": 2
          },
          "extra_info": {"audio_length": 9252, "audio_size": 149748, "word_count": 52, ...},
          "trace_id": "...",
          "base_resp": {"status_code": 0, "status_msg": "success"}
        }

OSS URL 下载策略（v3 起）：
    响应 data.audio / subtitle_file 均给 OSS 临时 URL（含签名 token，TTL 默认 24h）。
    客户端在 convert 阶段同步下载音频与字幕，存到 task_dir/<task_id>/。URL 不进 tasks 表。

OSS URL 重试策略（v4 起）：
    仅以下错误触发指数退避重试（默认 5 次）：
        - 网络层：httpx.TimeoutException / ConnectError / RemoteProtocolError /
          ReadError / SSLException / NetworkError / ConnectionError
        - HTTP 状态：429 / 5xx
    其他错误（如 4xx 业务错 / 401 / 403 / 404）→ 立即 raise MinimaxTtsError，不重试。
    重试上限：MINIMAX__URL_FETCH_MAX_RETRIES（默认 5）；5 次用尽仍失败 →
    MinimaxTtsError(... "5 次仍失败 ...")，由 pipeline 失败路径接管 task → failed_retryable。

字幕生成策略：
    API 在 ``data.subtitle_file`` 返回 OSS 公开 URL（含一次签名 token，TTL 通常几分钟）。
    客户端二次 GET 该 URL 拿到字幕 JSON 列表，按 "start_time / end_time (毫秒) + text"
    字段解析为 ``[(start_sec, end_sec, text), ...]`` cues，再渲染成 SRT/LRC。
    字幕拉取失败（5xx / 网络错 5 次用尽 / 4xx）→ 抛 :class:`MinimaxTtsError`，
    由 pipeline 失败路径接管。

字幕 JSON 字段名（文档未公开）兼容策略：
    优先尝试 ``text`` / ``sentence_text`` / ``sentence`` 作为文本字段；
    优先尝试 ``start_time`` / ``begin_time`` / ``start`` 作为起始字段；
    优先尝试 ``end_time`` / ``finish_time`` / ``end`` 作为结束字段。
    找不到任一可识别的字段组合 → 抛 :class:`SubtitleFetchError`（**不**静默退化
    到字符估算）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

from app.config import (
    MINIMAX_VOICES_ZH,
    MinimaxTtsSettings,
)
from app.services.edge_tts_provider import (
    format_lrc_timestamp,
    format_srt_timestamp,
)

logger = logging.getLogger(__name__)


class MinimaxTtsError(RuntimeError):
    """Raised when the MiniMax T2A endpoint returns an error or unusable body."""


class SubtitleFetchError(MinimaxTtsError):
    """字幕 URL 二次 GET 失败 / 解析失败 / 字段名未知。

    pipeline 捕获此异常后会把任务状态置为 ``subtitle_pending``，
    等待用户手动重试（不视为整条流水线失败，音频本身已可用）。
    """


@dataclass
class ProviderResult:
    """synthesize_segment 返回值。"""
    audio_bytes: bytes
    duration_sec: float                # 整段音频秒数（来自 extra_info.audio_length / 1000）
    srt_text: str                      # 完整 SRT 文本（多句）
    lrc_text: str                      # 完整 LRC 文本
    sentence_cues: List[tuple] = field(default_factory=list)  # [(start_sec, end_sec, text), ...]
    subtitle_fetch_error: Optional[str] = None   # 若字幕拉取失败，存错误描述（音频仍可用）


# 字幕 JSON 字段名候选（按优先级）。v5 实测 OSS .titles 返回的形态：
#   [{
#     "text": "...",
#     "pronounce_text": "...",
#     "time_begin": 0,                  # 单位：毫秒
#     "time_end": 9700,                 # 单位：毫秒
#     "text_begin": 0, "text_end": 52,
#     "pronounce_text_begin": 0, "pronounce_text_end": 52,
#     "is_final_segment": true
#   }]
# time_begin/time_end 放在第一位，因为 v5 OSS 直拉的 JSON 是这俩字段名。
# 仍保留旧 start_time/end_time 兼容路径。
_SUBTITLE_TEXT_KEYS  = ("text", "pronounce_text", "sentence_text", "sentence", "content")
_SUBTITLE_START_KEYS = ("time_begin", "start_time", "begin_time", "start", "begin", "start_ms")
_SUBTITLE_END_KEYS   = ("time_end",   "end_time",   "finish_time", "end",   "finish", "end_ms")


def _pick_key(d: Dict[str, Any], candidates: tuple) -> Optional[str]:
    for k in candidates:
        if k in d:
            return k
    return None


class MinimaxTtsClient:
    """MiniMax speech-2.8-hd TTS 客户端。

    设计目标：
      * 完全替代 MiMo 方案，对外暴露与 EdgeTtsClient 相似的接口
      * 输出 mp3 字节 + 句级字幕 SRT/LRC（**字幕来自 API 返回的 subtitle_file**）
      * 不依赖 ffmpeg，不需要客户端合并
    """

    def __init__(
        self,
        settings: MinimaxTtsSettings,
        *,
        api_key_fallback: str = "",
    ) -> None:
        self._settings = settings
        # api_key 回落：MINIMAX__API_KEY 留空 → 复用 LLM__API_KEY
        api_key = settings.api_key or api_key_fallback
        if not api_key:
            logger.warning(
                "MiniMax TTS api_key 未配置（MINIMAX__API_KEY 与 LLM__API_KEY 都为空）"
            )
        self._effective_api_key = api_key

        # ---- T2A POST client：需要 Authorization + JSON headers ----
        # 用于 POST https://api.minimaxi.com/v1/t2a_v2
        self._t2a_client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_sec),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )

        # ---- OSS 预签名 GET client：必须**完全干净** ----
        # v5 修复：旧实现把 Authorization 头复用给 OSS URL，触发阿里云
        # SignatureDoesNotMatch 403。阿里云 OSS 预签名 URL 自带完整鉴权签名，
        # 客户端再加任何 Authorization / Content-Type / Accept 等 header
        # 都会破坏签名重算。这里新建一个无自定义 header 的 client，仅
        # 用于 GET OSS 临时音频 / 字幕 URL。
        self._download_client = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout_sec),
            headers={},                # 显式空，让 httpx 用默认值
            follow_redirects=True,
        )

    # -- public API ----------------------------------------------------------

    async def synthesize_segment(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
        title: str = "",
        artist: str = "txt2tts",
    ) -> ProviderResult:
        """合成一段文本，返回 :class:`ProviderResult`。

        与 EdgeTtsClient.synthesize_segment 同形态，便于 pipeline 替换。
        字幕来源：API 返回的 ``data.subtitle_file``（OSS 公开 URL），二次 GET 后解析。
        """
        if not text or not text.strip():
            raise MinimaxTtsError("Refusing to synthesize empty text.")
        if not self._effective_api_key:
            raise MinimaxTtsError(
                "MiniMax API key is not configured. Set MINIMAX__API_KEY or LLM__API_KEY env var.",
            )

        voice_id = self._resolve_voice(voice)
        body = self._build_request_body(text=text, voice_id=voice_id)
        logger.debug(
            "MiniMax T2A request url=%s model=%s voice=%s text_len=%d",
            self._settings.t2a_url, self._settings.model, voice_id, len(text),
        )

        last_err: Optional[Exception] = None
        for attempt in range(self._settings.max_retries + 1):
            try:
                resp = await self._t2a_client.post(self._settings.t2a_url, json=body)
            except httpx.HTTPError as exc:
                last_err = exc
                logger.warning("MiniMax T2A HTTP error on attempt %d: %s", attempt + 1, exc)
                continue

            if resp.status_code >= 500:
                last_err = MinimaxTtsError(f"MiniMax 5xx: {resp.status_code} {resp.text[:200]}")
                logger.warning("MiniMax T2A 5xx on attempt %d: %s", attempt + 1, last_err)
                continue

            if resp.status_code >= 400:
                # 4xx 通常是参数错（如非法 voice_id）— 直接抛，不重试
                raise MinimaxTtsError(
                    f"MiniMax API error {resp.status_code}: {resp.text[:500]}",
                )

            return await self._parse_response(
                resp, text=text, title=title, artist=artist,
            )

        raise MinimaxTtsError(f"MiniMax T2A failed after retries: {last_err}")

    async def list_voices(self) -> tuple[List[dict], str]:
        """返回内置 voice 白名单（API 不暴露 /v1/voices）。"""
        return list(MINIMAX_VOICES_ZH), "static"

    async def aclose(self) -> None:
        await self._t2a_client.aclose()
        await self._download_client.aclose()

    # -- internals -----------------------------------------------------------

    def _resolve_voice(self, voice: Optional[str]) -> str:
        """白名单校验：不在白名单则 fallback 到 default_voice。"""
        if not voice:
            return self._settings.voice_id
        valid = {v["id"] for v in MINIMAX_VOICES_ZH}
        if voice in valid:
            return voice
        logger.warning(
            "MiniMax T2A: voice_id=%r 不在白名单，fallback 到 %r",
            voice, self._settings.voice_id,
        )
        return self._settings.voice_id

    def _build_request_body(self, text: str, *, voice_id: str) -> dict:
        return {
            "model": self._settings.model,
            "text": text,
            "stream": False,
            "voice_setting": {
                "voice_id": voice_id,
                "speed": self._settings.speed,
                "vol": self._settings.vol,
                "pitch": self._settings.pitch,
                # v3 起：语音情感 + 文本归一化（控制朗读时的情绪、是否清理掉非自然停顿字符）
                "emotion": self._settings.voice_emotion,
                "text_normalization": self._settings.voice_text_normalization,
            },
            "audio_setting": {
                "sample_rate": self._settings.sample_rate,
                "bitrate": self._settings.bitrate,
                "format": self._settings.audio_format,
                "channel": self._settings.audio_channel,
            },
            # 原生字幕：开启后 data.subtitle_file 返回 OSS 公开 URL（含签名 token）
            "subtitle_enable": True,
            "subtitle_type": self._settings.subtitle_type,
            # v3 起：要求 audio 字段以 OSS 临时 URL 形式返回（24h 过期）
            "output_format": self._settings.output_format,
        }

    async def _parse_response(
        self,
        resp: httpx.Response,
        *,
        text: str,
        title: str,
        artist: str,
    ) -> ProviderResult:
        """解析 T2A v2 响应：v3 起 audio/subtitle_file 都是 OSS 临时 URL。

        流程：
          1. JSON 解析 + base_resp.status_code 校验
          2. data.audio 是以 http(s):// 开头 → 走 ``_download_with_retry`` 下载
             否则按 hex 解码（向后兼容 ``output_format=hex`` 形态）
          3. data.subtitle_file → 走 ``_download_with_retry`` → JSON 解析 → cues
        """
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise MinimaxTtsError(f"MiniMax T2A returned non-JSON: {exc}") from exc

        base = payload.get("base_resp") or {}
        if base.get("status_code", 0) != 0:
            raise MinimaxTtsError(
                f"MiniMax T2A error {base.get('status_code')}: {base.get('status_msg', '')}",
            )

        data = payload.get("data") or {}
        audio_field = data.get("audio")
        if not isinstance(audio_field, str) or not audio_field:
            raise MinimaxTtsError(
                f"MiniMax T2A response has no audio data: {json.dumps(payload)[:300]}",
            )

        # data.audio 既可能是 OSS URL（output_format=url）也可能是 hex（向后兼容）
        if audio_field.startswith("http://") or audio_field.startswith("https://"):
            audio_bytes = await self._download_with_retry(
                audio_field,
                label="audio",
                timeout=self._settings.url_fetch_timeout_sec,
            )
        else:
            try:
                audio_bytes = bytes.fromhex(audio_field)
            except ValueError as exc:
                raise MinimaxTtsError(
                    f"Failed to decode MiniMax hex audio: {exc}",
                ) from exc

        extra = payload.get("extra_info") or {}
        # audio_length 单位是毫秒（基于响应实测 audio_length=9252 ↔ 9.25s 音频）
        audio_length_ms = float(extra.get("audio_length", 0) or 0)
        duration_sec = max(0.1, audio_length_ms / 1000.0)

        subtitle_url = data.get("subtitle_file")
        subtitle_fetch_error: Optional[str] = None
        cues: List[tuple] = []

        if subtitle_url and isinstance(subtitle_url, str):
            try:
                cues = await self._fetch_and_parse_subtitle(subtitle_url)
            except SubtitleFetchError as exc:
                # 字幕解析层面失败：4xx 业务错 / JSON 字段名未知（音频仍可用）
                logger.warning("MiniMax subtitle_file 解析失败: %s", exc)
                subtitle_fetch_error = str(exc)
            except MinimaxTtsError as exc:
                # 字幕 URL 下载 5 次仍失败（5xx/网络）→ 直接 raise
                raise
        else:
            logger.warning(
                "MiniMax T2A 响应未返回 subtitle_file（text_len=%d），字幕不可用",
                len(text),
            )
            subtitle_fetch_error = "API 响应未返回 subtitle_file 字段"

        # 即使 cues 为空也尝试渲染（产生空 SRT/LRC）；音频本身仍可用
        srt_text = self._render_srt(cues) if cues else ""
        lrc_text = self._render_lrc(cues, title=title, artist=artist) if cues else ""

        return ProviderResult(
            audio_bytes=audio_bytes,
            duration_sec=duration_sec,
            srt_text=srt_text,
            lrc_text=lrc_text,
            sentence_cues=cues,
            subtitle_fetch_error=subtitle_fetch_error,
        )

    async def _fetch_and_parse_subtitle(self, url: str) -> List[tuple]:
        """下载 subtitle_file 并解析为 cues。

        失败两类：
          * 5xx / 网络错 × 5 次仍失败 → 抛 :class:`MinimaxTtsError`
            （pipeline 失败路径接管；任务转 failed_retryable）
          * 4xx / 非 JSON / 字段名未知（业务错）→ 抛 :class:`SubtitleFetchError`
            （pipeline 走 subtitle_pending 路径；音频仍可用）

        v4 起统一走 :meth:`_download_with_retry`，URL 重试策略与 audio 共用。
        """
        try:
            body = await self._download_with_retry(
                url,
                label="subtitle",
                timeout=self._settings.subtitle_fetch_timeout_sec,
            )
        except MinimaxTtsError as exc:
            msg = str(exc)
            # 仅在 URL 本身 4xx 时转成 SubtitleFetchError（业务错）。其他
            # （5xx/网络错5次仍失败）走 MinimaxTtsError 上抛由 pipeline 处理。
            if " HTTP 4" in msg and "subtitle_url" in msg:
                raise SubtitleFetchError(msg) from exc
            raise

        try:
            subtitle_payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SubtitleFetchError(
                f"MiniMax subtitle_file 返回非 JSON: {exc}"
            ) from exc

        return self._parse_subtitle_payload(subtitle_payload)

    async def _download_with_retry(
        self, url: str, *, label: str, timeout: float,
    ) -> bytes:
        """对 OSS 临时 URL 做指数退避下载（仅网络错 / 5xx / 429 触发重试）。

        触发重试：
          * ``httpx.TimeoutException`` / ``ConnectError`` / ``RemoteProtocolError`` /
            ``ReadError`` / ``SSLException`` / ``NetworkError`` / ``ConnectionError``
          * HTTP 429 / 5xx

        不重试（立即 raise MinimaxTtsError）：
          * 其他 4xx（401/403/404/...）—— 业务错

        Backoff 序列：min(initial * 2 ** (attempt-1), cap)
        默认 base=1s cap=30s max_retries=5 → 1, 2, 4, 8, 16（最后一次 sleep 后退出）。

        用尽仍失败 → ``raise MinimaxTtsError(f"... 5 次仍失败 ...")``。
        """
        max_n = self._settings.url_fetch_max_retries
        base = self._settings.url_fetch_initial_backoff_sec
        cap = self._settings.url_fetch_max_backoff_sec

        last_status: Optional[int] = None
        last_err: Optional[Exception] = None

        # 网络层白名单（这些类才重试；其他 httpx.HTTPError 立即 raise）
        # 注：本环境 httpx 没有 SSLException（属于 httpcore.ConnectError 子类），
        # 所以 ConnectError 已覆盖 SSL 握手失败场景。
        retryable_network_errors = (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
            httpx.ReadError,
            httpx.NetworkError,
            ConnectionError,
        )

        for attempt in range(1, max_n + 1):
            try:
                resp = await self._download_client.get(url, timeout=httpx.Timeout(timeout))
            except retryable_network_errors as exc:
                last_err = exc
                if attempt >= max_n:
                    break
                wait = min(base * (2 ** (attempt - 1)), cap)
                logger.warning(
                    "MiniMax %s_url 网络错误重试 %d/%d（%s: %s），sleep %.1fs",
                    label, attempt, max_n, exc.__class__.__name__, exc, wait,
                )
                await asyncio.sleep(wait)
                continue
            except httpx.HTTPError as exc:
                # 白名单外的 HTTP 错：立即 raise
                raise MinimaxTtsError(
                    f"MiniMax {label}_url 下载 HTTP 错误: {exc}"
                ) from exc

            # 5xx / 429：可重试
            if resp.status_code == 429 or resp.status_code >= 500:
                last_status = resp.status_code
                last_err = None
                if attempt >= max_n:
                    break
                wait = min(base * (2 ** (attempt - 1)), cap)
                logger.warning(
                    "MiniMax %s_url HTTP %d 重试 %d/%d，sleep %.1fs",
                    label, resp.status_code, attempt, max_n, wait,
                )
                await asyncio.sleep(wait)
                continue

            # 其他 4xx：业务错，立即 raise（不重试）
            if 400 <= resp.status_code < 500:
                raise MinimaxTtsError(
                    f"MiniMax {label}_url HTTP {resp.status_code}: "
                    f"{resp.text[:300]}"
                )

            # 2xx
            return resp.content

        # 退出循环：max_n 次都没拿到
        if last_status is not None:
            raise MinimaxTtsError(
                f"MiniMax {label}_url 重试 {max_n} 次仍失败"
                f"（最后 HTTP {last_status}）"
            )
        err_name = last_err.__class__.__name__ if last_err else "未知错误"
        raise MinimaxTtsError(
            f"MiniMax {label}_url 重试 {max_n} 次仍失败"
            f"（最后网络错: {err_name}）"
        )

    @staticmethod
    def _parse_subtitle_payload(payload: Any) -> List[tuple]:
        """把字幕 JSON 解析成 [(start_sec, end_sec, text), ...]。

        字段名容错（v5 实测优先识别 time_begin/time_end）：
          - text → 也可能 pronounce_text / sentence_text / sentence / content
          - time_begin（毫秒）→ 也可能 start_time / begin_time / start / begin
          - time_end（毫秒）→ 也可能 end_time / finish_time / end / finish

        单位：time_begin/end 是**毫秒**，下面会 ``/1000.0`` 转成秒。
        若 payload 不是 list 或所有元素都无法识别 → 抛 SubtitleFetchError。
        """
        if not isinstance(payload, list):
            # 兼容某些 API 返回 {"subtitles": [...]} 包装
            if isinstance(payload, dict):
                for wrap in ("subtitles", "sentences", "data", "items"):
                    inner = payload.get(wrap)
                    if isinstance(inner, list):
                        return MinimaxTtsClient._parse_subtitle_payload(inner)
            raise SubtitleFetchError(
                f"字幕 JSON 顶层不是 list，实际类型 {type(payload).__name__}; "
                f"first 200 chars={json.dumps(payload)[:200] if isinstance(payload, (dict, list)) else payload!r}"
            )

        if not payload:
            raise SubtitleFetchError("字幕 JSON 是空 list")

        cues: List[tuple] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            text_key = _pick_key(entry, _SUBTITLE_TEXT_KEYS)
            start_key = _pick_key(entry, _SUBTITLE_START_KEYS)
            end_key = _pick_key(entry, _SUBTITLE_END_KEYS)
            if not (text_key and start_key and end_key):
                continue
            try:
                start_ms = float(entry[start_key])
                end_ms = float(entry[end_key])
                text = str(entry[text_key] or "").strip()
            except (TypeError, ValueError):
                continue
            if not text:
                continue
            # 毫秒 → 秒
            cues.append((start_ms / 1000.0, end_ms / 1000.0, text))

        if not cues:
            sample = payload[0] if payload else {}
            raise SubtitleFetchError(
                f"字幕 JSON 字段名无法识别（尝试了 text/sentence/start_time 等组合）；"
                f"sample entry={json.dumps(sample)[:300]}"
            )
        return cues

    @staticmethod
    def _render_srt(cues: List[tuple]) -> str:
        """构造 SRT 文本（与 edge 方案 SRT 格式一致）。"""
        if not cues:
            return ""
        blocks: List[str] = []
        for i, (start, end, text) in enumerate(cues, 1):
            blocks.append(
                f"{i}\n"
                f"{format_srt_timestamp(start)} --> {format_srt_timestamp(end)}\n"
                f"{text.strip()}\n"
            )
        return "\n".join(blocks)

    @staticmethod
    def _render_lrc(cues: List[tuple], *, title: str = "", artist: str = "txt2tts") -> str:
        """构造 LRC 文本（与 edge 方案 LRC 格式一致）。"""
        if not cues:
            return ""
        out: List[str] = []
        if title:
            out.append(f"[ti:{title}]")
        if artist:
            out.append(f"[ar:{artist}]")
        for start, _end, text in cues:
            out.append(f"{format_lrc_timestamp(start)}{text.strip()}")
        return "\n".join(out) + "\n"