"""edge-tts 客户端 + Provider 适配层。

方案二核心：
    1. EdgeTtsClient 把单段文本送入 edge-tts，返回 (mp3_bytes, 句级时间戳)。
    2. EdgeProvider.run() 接收 M3 预处理后的全文，按段切分、并发调用 EdgeTtsClient、
       收集 (segment_audio, segment_srt) 列表后返回，供 Pipeline 写盘 + ffmpeg 合并。
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import edge_tts

from app.config import EdgeTtsSettings, get_edge_voices

logger = logging.getLogger(__name__)


class EdgeTtsError(RuntimeError):
    """edge-tts 调用失败。"""


@dataclass(frozen=True)
class SegmentAudio:
    """一段音频 + 句级 SRT 字幕项。"""

    index: int                       # 段序号，从 0 开始
    text: str                        # 朗读文本（已剔除多音字标记的纯朗读字符串）
    audio_bytes: bytes               # 该段的 mp3 字节
    duration_sec: float              # 通过 ffprobe 探测或 edge-tts SubMaker 估算
    start_sec: float = 0.0           # 在完整音频中的起始时间
    end_sec: float = 0.0             # 在完整音频中的结束时间


@dataclass(frozen=True)
class ProviderResult:
    """方案二产出的完整结果。"""

    segments: List[SegmentAudio] = field(default_factory=list)
    full_srt: str = ""               # 全部句级 SRT 字幕（基于累计时长）
    total_duration_sec: float = 0.0


# 多音字标记：行[xíng] → 行
_DISAMBIG_RE = re.compile(r"([一-龥])\[([a-zA-Zāáǎàēéěèīíǐìōóǒòūúǔùǖǘǚǜü]+)\]")


def strip_disambiguation(text: str) -> str:
    """移除多音字 [拼音] 标记，得到 TTS 朗读用的纯文本。"""
    return _DISAMBIG_RE.sub(r"\1", text)


def extract_disambiguation_hints(text: str) -> List[Tuple[str, str]]:
    """提取多音字标记列表，供 edge-tts 用 <phoneme> 标签或单独替换。"""
    return [(m.group(1), m.group(2)) for m in _DISAMBIG_RE.finditer(text)]


# 句末标点（中英文统一识别）
_SENT_END_RE = re.compile(r"([。！？!?]+)")


def split_sentences(text: str, max_chars: int = 200) -> List[str]:
    """按句号/问号/叹号切句；同时尊重段落空行；最后兜底按 max_chars 切分。

    算法：
      1. 先按段落（空行）切。
      2. 每个段落用 _SENT_END_RE 捕获组切，保留标点。
      3. 每个 chunk = (seg + punct) 立即 push 出去。
      4. 但如果某 chunk 单独超过 max_chars，再按 max_chars 兜底切碎。
    """
    if not text or not text.strip():
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: List[str] = []
    for para in paragraphs:
        # 句末标点作为切分依据，保留标点
        parts = _SENT_END_RE.split(para)
        # re.split 在有捕获组时返回 [text, sep, text, sep, ..., '']
        # 因此 parts 长度为奇数（最末是 ''）
        for i in range(0, len(parts) - 1, 2):
            seg = parts[i]
            punct = parts[i + 1]
            chunk = (seg + punct).strip()
            if not chunk:
                continue
            # 极端情况：单句超过 max_chars → 按 max_chars 兜底切碎
            if len(chunk) > max_chars:
                for j in range(0, len(chunk), max_chars):
                    out.append(chunk[j:j + max_chars])
            else:
                out.append(chunk)
        # 兜底：如果段落最后没有句末标点，剩余文本也要 flush
        tail = parts[-1].strip()
        if tail:
            if len(tail) > max_chars:
                for j in range(0, len(tail), max_chars):
                    out.append(tail[j:j + max_chars])
            else:
                out.append(tail)
    return out


class EdgeTtsClient:
    """单段 edge-tts 异步调用 + 时间戳收集。"""

    def __init__(self, settings: EdgeTtsSettings) -> None:
        self._settings = settings

    async def synthesize_segment(
        self,
        text: str,
        *,
        voice: Optional[str] = None,
    ) -> Tuple[bytes, List[Tuple[float, float, str]]]:
        """合成单段文本，返回 (mp3_bytes, [(start_sec, end_sec, sentence)] 句级时间戳列表)。

        实现思路：
          1. 用 SubMaker 在 TextSegment 上获取句级边界（offset, duration）。
          2. audio data 通过 bytes 形式累积。

        防御性校验：voice 必须在 ``app.config.EDGE_VOICES_ZH`` 白名单内。
        历史上曾出现用户上传时选了 MiMo voice（如 ``mimo_default``），又在
        设置里切到 edge provider，导致 edge-tts 抛 ``Invalid voice``。
        上层（``pipeline._resolve_edge_voice``）已经做了 fallback，但这里
        再加一道硬校验，确保即使上层漏判也能给出可读错误而不是把无效
        voice 喂给 edge-tts。
        """
        if not text or not text.strip():
            raise EdgeTtsError("Refusing to synthesize empty text.")
        voice_id = voice or self._settings.default_voice
        edge_voice_ids = {v["id"] for v in get_edge_voices()}
        if voice_id not in edge_voice_ids:
            valid = sorted(edge_voice_ids)
            raise EdgeTtsError(
                f"Invalid voice {voice_id!r} for edge provider. "
                f"请使用 APP__EDGE_VOICES_JSON 配置的 voice 白名单内的 voice，例如："
                + ", ".join(valid[:5])
                + " ...（共 "
                + str(len(valid))
                + " 个）。如需使用 MiMo voice（mimo_default / 冰糖 等），"
                "请把设置里的 TTS Provider 切回 mimo。"
            )
        # 单条 Communicate，请求 SentenceBoundary 事件；
        # edge-tts ≥ 7.x 的 SubMaker 不再暴露 get_cues()，所以我们直接
        # 在 stream 里把 SentenceBoundary 事件聚成 cues。
        #
        # 整次合成最多重试 ``max_retries`` 次（指数退避），仅对网络类
        # 异常重试；参数 / 业务错误立即抛 EdgeTtsError。配置项见
        # ``EdgeTtsSettings.max_retries`` / ``retry_backoff_sec``。
        max_retries = int(getattr(self._settings, "max_retries", 3))
        backoff = float(getattr(self._settings, "retry_backoff_sec", 1.0))
        last_exc: Optional[BaseException] = None
        for attempt in range(max_retries + 1):
            try:
                audio_bytes, cues = await self._stream_once(text, voice_id)
                if not audio_bytes:
                    raise EdgeTtsError("edge-tts 返回空音频。")
                return audio_bytes, cues
            except EdgeTtsError as exc:
                last_exc = exc
                # 业务错误（空音频 / Invalid voice）直接抛，不重试。
                if not _is_transient_error(exc):
                    raise
                # 还在重试预算内 → 等退避后继续
                if attempt < max_retries:
                    wait = backoff * (2 ** attempt)
                    logger.warning(
                        "edge-tts transient failure (attempt %d/%d): %s; "
                        "retrying in %.1fs",
                        attempt + 1, max_retries + 1, exc, wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                # 重试耗尽：包成友好消息抛
                logger.error(
                    "edge-tts retry exhausted after %d attempts: %s",
                    max_retries + 1, exc,
                )
                raise EdgeTtsError(
                    f"edge-tts 微软服务暂不可达（已重试 {max_retries + 1} 次仍失败）："
                    f"{exc}"
                ) from exc
        # 不可达；保留供静态检查
        assert last_exc is not None
        raise EdgeTtsError(
            f"edge-tts 微软服务暂不可达（已重试 {max_retries + 1} 次仍失败）："
            f"{last_exc}"
        ) from last_exc

    async def _stream_once(
        self,
        text: str,
        voice_id: str,
    ) -> Tuple[bytes, List[Tuple[float, float, str]]]:
        """单次 stream 调用；把 SentenceBoundary 事件聚成 cues。

        网络异常直接往上抛（让外层重试逻辑判定是否瞬时错误）。
        """
        comm = edge_tts.Communicate(
            text,
            voice=voice_id,
            rate=self._settings.rate,
            volume=self._settings.volume,
            pitch=self._settings.pitch,
            boundary="SentenceBoundary",
        )
        audio_chunks: List[bytes] = []
        cues: List[Tuple[float, float, str]] = []
        try:
            async for ev in comm.stream():
                if ev["type"] == "audio":
                    audio_chunks.append(ev["data"])
                elif ev["type"] == "SentenceBoundary":
                    # edge-tts 7.x SentenceBoundary 事件字段：
                    #   offset / duration 单位 = 1e-7 秒（100ns ticks）但实测
                    #   在 7.2.x 是 1e-4 秒（10 微秒 ticks）；更稳妥的做法是
                    #   除以 1e7（按 100ns 解释）。我们用后者与
                    #   ``Microsoft Edge TTS 文档`` 对齐：
                    #   https://learn.microsoft.com/azure/ai-services/speech-service/rest-text-to-speech
                    # 经过 7.2.8 实测：offset=15000000 表示 1.5s。
                    offset = ev.get("offset")
                    duration = ev.get("duration")
                    text_seg = ev.get("text") or ""
                    if offset is None or duration is None:
                        start = 0.0
                        end = 0.0
                    else:
                        start = float(offset) / 1e7
                        end = start + float(duration) / 1e7
                    cues.append((start, end, text_seg))
        except EdgeTtsError:
            raise
        except Exception as exc:
            logger.exception("edge-tts synthesize failed")
            # 重新包成 EdgeTtsError，但保留原始异常名给上层判断瞬时性
            wrapped = EdgeTtsError(f"{type(exc).__name__}: {exc}")
            wrapped.__cause__ = exc
            raise wrapped from exc

        return b"".join(audio_chunks), cues

    async def aclose(self) -> None:
        """edge-tts 无内部连接池，留作接口占位。"""
        return None


def _is_transient_error(exc: BaseException) -> bool:
    """判断 edge-tts 调用抛出的异常是否属于"瞬时可重试"类别。

    经验上 edge-tts 7.x 走 aiohttp 调微软服务时可能抛：
      * ``aiohttp.ClientError`` / ``ClientConnectorError`` / ``ClientResponseError``
      * ``ssl.SSLError``（握手失败 / EOF）
      * ``asyncio.TimeoutError``
      * ``ConnectionError`` / ``ConnectionRefusedError`` / ``ConnectionResetError``
      * ``OSError``（如 [Errno 11001] getaddrinfo failed）
      * 原始消息含 "Cannot connect to host" / "Connection refused" / "SSL" /
        "Timeout" / "temporary" 等关键词

    参数 / 业务错误（如 "Invalid voice" / "empty text" / "返回空音频"）不进
    入重试队列。
    """
    # 业务错误关键字（命中即非瞬时）
    business_markers = (
        "Invalid voice",
        "Refusing to synthesize empty text",
        "edge-tts 返回空音频",
    )
    msg = str(exc)
    for marker in business_markers:
        if marker in msg:
            return False

    # 异常类型白名单
    transient_types = (
        "ClientError",            # aiohttp
        "ClientConnectorError",
        "ClientResponseError",
        "ServerDisconnectedError",
        "SSLError",
        "TimeoutError",
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "OSError",
        "SocketError",
    )
    name = type(exc).__name__
    for t in transient_types:
        if name == t or name.endswith(t):
            return True

    # 关键词兜底
    transient_keywords = (
        "Cannot connect to host",
        "Connection refused",
        "Connection reset",
        "SSL",
        "Timeout",
        "temporary failure",
        "Name or service not known",
        "getaddrinfo failed",
    )
    return any(kw.lower() in msg.lower() for kw in transient_keywords)


def probe_audio_duration(
    path: Path,
    ffprobe_path: Path,
    *,
    timeout_sec: float = 10.0,
) -> float:
    """用 ffprobe 读出音频时长（秒）。失败返回 0.0。

    ``timeout_sec`` 来自 ``EdgeTtsSettings.ffprobe_timeout_sec``（env 可调）。
    """
    if not ffprobe_path.exists():
        return 0.0
    import json
    import subprocess
    try:
        out = subprocess.run(
            [str(ffprobe_path), "-v", "error",
             "-show_entries", "format=duration",
             "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=timeout_sec,
        )
        return float(json.loads(out.stdout)["format"]["duration"])
    except Exception:
        logger.exception("ffprobe failed for %s", path)
        return 0.0


def concat_segments_with_srt(
    segment_files: List[Path],
    srt_path: Path,
    output_path: Path,
    ffmpeg_path: Path,
    *,
    timeout_sec: float = 120.0,
) -> Tuple[float, int]:
    """用 ffmpeg concat 把所有片段拼成完整 mp3；同时把 SRT 嵌入 mp3 metadata（可选）。

    ``timeout_sec`` 来自 ``EdgeTtsSettings.ffmpeg_concat_timeout_sec``（env 可调）。

    返回 (总时长秒, 字幕条目数)。
    """
    if not ffmpeg_path.exists():
        raise EdgeTtsError(f"ffmpeg 不存在: {ffmpeg_path}")
    if not segment_files:
        raise EdgeTtsError("没有可合并的片段音频。")

    # 1) 写 concat list
    concat_list = output_path.parent / f"{output_path.stem}.concat.txt"
    with open(concat_list, "w", encoding="utf-8") as fh:
        for seg in segment_files:
            # 单引号转义；路径里如有 ' 需替换
            safe = str(seg.resolve()).replace("'", "'\\''")
            fh.write(f"file '{safe}'\n")

    # 2) ffmpeg concat（mp3 可直接 concat）
    import subprocess
    cmd = [
        str(ffmpeg_path), "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(output_path),
    ]
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout_sec)
    except subprocess.CalledProcessError as exc:
        logger.error("ffmpeg concat failed: %s", exc.stderr)
        raise EdgeTtsError(f"ffmpeg 合并失败: {exc.stderr[-300:]}") from exc
    finally:
        try:
            concat_list.unlink()
        except OSError:
            pass

    # 3) 探测总时长：兼容 Windows (ffprobe.exe) 与 Linux (ffprobe) 命名
    ffprobe_path = ffmpeg_path.parent / ("ffprobe.exe" if ffmpeg_path.name.endswith(".exe") else "ffprobe")
    if not ffprobe_path.exists():
        import shutil as _shutil
        on_path = _shutil.which("ffprobe")
        if on_path:
            ffprobe_path = Path(on_path)
    total_dur = probe_audio_duration(output_path, ffprobe_path)
    # srt_path 可能为占位（Path()）；只在文件存在且为绝对路径时尝试读
    srt_entries = 0
    try:
        if srt_path and srt_path.exists() and srt_path.is_file():
            srt_lines = srt_path.read_text(encoding="utf-8").splitlines()
            srt_entries = sum(1 for ln in srt_lines if re.match(r"^\d+$", ln))
    except (OSError, ValueError):
        pass
    return total_dur, srt_entries


def format_srt_timestamp(seconds: float) -> str:
    """把秒数格式化成 SRT 的 HH:MM:SS,mmm 形式。"""
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_lrc_timestamp(seconds: float) -> str:
    """LRC 的 [mm:ss.xx] 时间戳。"""
    if seconds < 0:
        seconds = 0.0
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"[{m:02d}:{s:05.2f}]"


def srt_to_lrc(srt_text: str, title: str = "", artist: str = "txt2tts") -> str:
    """把 SRT 文本转成 LRC 文本（保留时间戳与歌词）。"""
    out_lines: List[str] = []
    if title:
        out_lines.append(f"[ti:{title}]")
    if artist:
        out_lines.append(f"[ar:{artist}]")
    out_lines.append("[al:txt2tts]")
    out_lines.append("")

    pattern = re.compile(
        r"(\d+)\s*\n"                             # 序号
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*"    # 起始
        r"(\d{2}:\d{2}:\d{2},\d{3})\s*\n"        # 结束
        r"((?:(?!\n\n).)*?)"                      # 文本（到下一个空行）
        r"(?:\n|$)",
        re.DOTALL,
    )
    for m in pattern.finditer(srt_text):
        hms_start = m.group(2)
        text = m.group(4).strip()
        # SRT 时间戳 → 秒
        hh, mm, ss_ms = hms_start.split(":")
        ss, ms = ss_ms.split(",")
        seconds = int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0
        out_lines.append(f"{format_lrc_timestamp(seconds)}{text}")
    return "\n".join(out_lines) + "\n"