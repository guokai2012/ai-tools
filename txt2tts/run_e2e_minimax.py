"""Real end-to-end: M3 normalization (via LangChain ChatAnthropic) +
MiniMax speech-2.8-hd TTS + subtitle_file 二次 GET + save mp3 + validate.

v4 写盘布局：所有产物在 outputs/<yyyymmdd>/<task_id>/ 下。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402  (MiniMax T2A 仍走 HTTP，未被 LangChain 接管)

from langchain_anthropic import ChatAnthropic  # noqa: E402
from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from app.config import M3_SYSTEM_PROMPT, get_settings  # noqa: E402
from app.services.markdown_service import MarkdownService  # noqa: E402

SAMPLE_PATH = ROOT / "samples" / "demo.md"


def step(title: str) -> None:
    bar = "=" * 64
    print(f"\n{bar}\n{title}\n{bar}", flush=True)


def validate_mp3(path: Path) -> dict:
    size = path.stat().st_size
    head = path.read_bytes()[:16]
    valid_mp3 = (
        head.startswith(b"ID3")
        or head.startswith(b"\xff\xfb")
        or head.startswith(b"\xff\xf3")
        or head.startswith(b"\xff\xe3")
    )
    valid_wav = head.startswith(b"RIFF")
    return {
        "size": size,
        "head_hex": head.hex(),
        "valid_mp3": valid_mp3,
        "valid_wav": valid_wav,
    }


async def main() -> int:
    """真实端到端：M3 normalize + MiniMax T2A + 字幕 URL 二次 GET。

    需要的 env（与 M3 复用）：
        LLM__API_KEY        MiniMax API key（同时作为 M3 和 TTS 鉴权）
        MINIMAX__API_KEY    可选；TTS 独立 key 时填这个

    输出：outputs/<YYYYMMDD>/<task_id>/<task_id>.mp3（hex 解码） + 字幕 JSON 内容预览。
    """
    llm_key = os.environ.get("LLM__API_KEY", "").strip()
    tts_key = os.environ.get("MINIMAX__API_KEY", "").strip() or llm_key
    if not llm_key:
        print("ERROR: set LLM__API_KEY env var (MiniMax API key)", file=sys.stderr)
        return 2
    if not tts_key:
        print("ERROR: no MiniMax TTS key available", file=sys.stderr)
        return 2
    if not SAMPLE_PATH.exists():
        print(f"ERROR: sample not found: {SAMPLE_PATH}", file=sys.stderr)
        return 2

    settings = get_settings()
    task_id = uuid.uuid4().hex
    date_str = datetime.now().strftime("%Y%m%d")
    task_dir = ROOT / "outputs" / date_str / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    # ---- 1. Local markdown cleaning ----
    step("STEP 1 — local markdown cleaning")
    md_text = SAMPLE_PATH.read_text(encoding="utf-8")
    local_clean = MarkdownService().to_plain_text(md_text)
    # 同步本地清洗结果写到 task_dir/<task_id>.md
    task_md = task_dir / f"{task_id}.md"
    task_md.write_text(local_clean, encoding="utf-8")
    print(f"md_chars      = {len(md_text)}")
    print(f"local_clean   = {len(local_clean)} chars  → {task_md}", flush=True)

    # ---- 2. Real M3 call (via LangChain ChatAnthropic) ----
    step("STEP 2 — real MiniMax M3 normalize (via LangChain ChatAnthropic)")
    llm = settings.llm
    client = ChatAnthropic(
        model=llm.model,
        api_key=llm_key,
        base_url=llm.base_url,
        max_tokens=llm.max_tokens,
        temperature=llm.temperature,
        timeout=llm.request_timeout_sec,
        max_retries=0,
    )
    messages = [
        SystemMessage(content=M3_SYSTEM_PROMPT),
        HumanMessage(content=local_clean),
    ]
    print(f"POST {llm.base_url}  model={llm.model}  (via ChatAnthropic)", flush=True)
    t0 = time.time()
    try:
        resp = await client.ainvoke(messages)
    except Exception as exc:
        print(f"ERROR: M3 call failed: {exc}", file=sys.stderr)
        return 1
    print(f"-> 200  ({(time.time()-t0)*1000:.0f}ms)")
    normalized = resp.content
    usage = getattr(resp, "usage_metadata", None) or getattr(resp, "response_metadata", {})
    print(f"M3 returned   = {len(normalized)} chars  usage={usage}")
    print(f"preview       = {normalized[:160]!r}...", flush=True)
    # 写 normalization.md
    (task_dir / "normalization.md").write_text(normalized, encoding="utf-8")

    # ---- 3. Real MiniMax T2A call ----
    step("STEP 3 — real MiniMax speech-2.8-hd synthesize")
    tts = settings.minimax
    body = {
        "model": tts.model,
        "text": normalized,
        "stream": False,
        "voice_setting": {
            "voice_id": tts.voice_id,
            "speed": tts.speed,
            "vol": tts.vol,
            "pitch": tts.pitch,
        },
        "audio_setting": {
            "sample_rate": tts.sample_rate,
            "bitrate": tts.bitrate,
            "format": tts.audio_format,
            "channel": tts.audio_channel,
        },
        "subtitle_enable": True,
        "subtitle_type": tts.subtitle_type,
    }
    headers = {
        "Authorization": f"Bearer {tts_key}",
        "Content-Type": "application/json",
    }
    print(f"POST {tts.t2a_url}  model={tts.model}  voice={tts.voice_id}", flush=True)
    print(f"text_len      = {len(normalized)} chars", flush=True)
    t0 = time.time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(tts.request_timeout_sec)) as c:
        r = await c.post(tts.t2a_url, json=body, headers=headers)
    print(f"-> {r.status_code}  ({(time.time()-t0)*1000:.0f}ms)")
    if r.status_code != 200:
        print(f"BODY: {r.text[:500]}")
        return 1
    payload = r.json()
    base = payload.get("base_resp") or {}
    if base.get("status_code", 0) != 0:
        print(f"ERROR: MiniMax base_resp error: {base}", file=sys.stderr)
        return 1

    data = payload.get("data") or {}
    hex_audio = data.get("audio", "")
    subtitle_url = data.get("subtitle_file")
    extra = payload.get("extra_info") or {}
    audio_length_ms = extra.get("audio_length", 0)
    print(f"audio.hex_len = {len(hex_audio)} chars")
    print(f"audio_length  = {audio_length_ms} ms")
    print(f"subtitle_file = {subtitle_url!r}", flush=True)

    # ---- 4. Decode hex -> raw mp3 bytes ----
    raw = bytes.fromhex(hex_audio)
    print(f"decoded_bytes = {len(raw)}")
    print(f"head_hex      = {raw[:16].hex()}", flush=True)

    # ---- 4b. 字幕二次 GET（验证 subtitle_file 可访问） ----
    if subtitle_url:
        step("STEP 3b — fetch subtitle_file (OSS URL)")
        t0 = time.time()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(tts.subtitle_fetch_timeout_sec)) as c:
                sub_r = await c.get(subtitle_url)
        except Exception as exc:
            print(f"WARN: subtitle_file fetch failed: {exc}", file=sys.stderr)
        else:
            print(f"-> {sub_r.status_code}  ({(time.time()-t0)*1000:.0f}ms)")
            if sub_r.status_code == 200:
                print(f"subtitle JSON preview = {sub_r.text[:500]!r}", flush=True)

    # ---- 5. Save to task_dir/<task_id>.mp3 ----
    step("STEP 4 — save to task_dir")
    final_mp3 = task_dir / f"{task_id}.mp3"
    final_mp3.write_bytes(raw)
    print(f"saved         = {final_mp3}")
    print(f"absolute      = {final_mp3.resolve()}", flush=True)

    # ---- 6. Validate ----
    step("STEP 5 — validate mp3")
    info = validate_mp3(final_mp3)
    print(f"size          = {info['size']} bytes ({info['size']/1024:.1f} KB)")
    print(f"size > 10KB?  = {info['size'] > 10*1024}")
    print(f"head          = {info['head_hex']}")
    print(f"valid mp3?    = {info['valid_mp3']}")
    print(f"valid wav?    = {info['valid_wav']}", flush=True)
    if not (info["valid_mp3"] or info["valid_wav"]):
        print("ERROR: file is neither MP3 nor WAV.", file=sys.stderr)
        return 1

    # ---- 7. Launch Windows default player ----
    step("STEP 6 — play with Windows default player")
    abs_path = str(final_mp3.resolve())
    print(f"launching: start \"\" \"{abs_path}\"", flush=True)
    os.system(f'start "" "{abs_path}"')

    # ---- summary ----
    step("SUMMARY")
    print(f"sample        = {SAMPLE_PATH}")
    print(f"M3 normalized = {len(normalized)} chars")
    print(f"MiniMax TTS   = {len(raw)} bytes ({info['size']/1024:.1f} KB)")
    print(f"task_dir      = {task_dir}")
    print(f"file path     = {abs_path}")
    print(f"format        = {'MP3' if info['valid_mp3'] else 'WAV'}")
    print("\nDONE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))