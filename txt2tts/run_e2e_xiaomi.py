"""Real end-to-end: M3 normalization + Xiaomi MiMo TTS + save mp3 + validate."""
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

import httpx  # noqa: E402

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
    llm_key = os.environ.get("LLM__API_KEY", "").strip()
    tts_key = os.environ.get("TTS__API_KEY", "").strip()
    if not llm_key or not tts_key:
        print("ERROR: set LLM__API_KEY and TTS__API_KEY env vars", file=sys.stderr)
        return 2
    if not SAMPLE_PATH.exists():
        print(f"ERROR: sample not found: {SAMPLE_PATH}", file=sys.stderr)
        return 2

    settings = get_settings()

    # ---- 1. Local markdown cleaning ----
    step("STEP 1 — local markdown cleaning")
    md_text = SAMPLE_PATH.read_text(encoding="utf-8")
    local_clean = MarkdownService().to_plain_text(md_text)
    print(f"md_chars      = {len(md_text)}")
    print(f"local_clean   = {len(local_clean)} chars", flush=True)

    # ---- 2. Real M3 call ----
    step("STEP 2 — real MiniMax M3 normalize")
    llm = settings.llm
    body = {
        "model": llm.model,
        "max_tokens": llm.max_tokens,
        "temperature": llm.temperature,
        "system": M3_SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": local_clean}],
    }
    headers = {
        "x-api-key": llm_key,
        "anthropic-version": llm.api_version,
        "content-type": "application/json",
    }
    print(f"POST {llm.messages_url}  model={llm.model}", flush=True)
    t0 = time.time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(llm.request_timeout_sec)) as c:
        r = await c.post(llm.messages_url, json=body, headers=headers)
    print(f"-> {r.status_code}  ({(time.time()-t0)*1000:.0f}ms)")
    if r.status_code != 200:
        print(f"BODY: {r.text[:500]}")
        return 1
    payload = r.json()
    normalized = payload["content"][0]["text"]
    print(f"M3 returned   = {len(normalized)} chars  usage={payload.get('usage')}")
    print(f"preview       = {normalized[:160]!r}...", flush=True)

    # ---- 3. Real Xiaomi MiMo TTS call ----
    step("STEP 3 — real Xiaomi MiMo TTS synthesize")
    tts = settings.tts
    body = tts.build_request_body(text=normalized)
    headers = {
        "Authorization": f"Bearer {tts_key}",
        "Content-Type": "application/json",
    }
    print(f"POST {tts.chat_url}  model={tts.model}  voice={tts.voice}", flush=True)
    print(f"text_len      = {len(normalized)} chars", flush=True)
    t0 = time.time()
    async with httpx.AsyncClient(timeout=httpx.Timeout(tts.request_timeout_sec)) as c:
        r = await c.post(tts.chat_url, json=body, headers=headers)
    print(f"-> {r.status_code}  ({(time.time()-t0)*1000:.0f}ms)")
    if r.status_code != 200:
        print(f"BODY: {r.text[:500]}")
        return 1
    import json as _json
    payload = r.json()
    audio = payload["choices"][0]["message"]["audio"]
    print(f"audio.id      = {audio.get('id')}")
    print(f"b64_len       = {len(audio['data'])} chars", flush=True)

    # ---- 4. Decode base64 -> raw mp3 bytes ----
    import base64
    raw = base64.b64decode(audio["data"])
    print(f"decoded_bytes = {len(raw)}")
    print(f"head_hex      = {raw[:16].hex()}", flush=True)

    # ---- 5. Save to outputs/<today>/<uuid>.mp3 ----
    step("STEP 4 — save to outputs/")
    today = datetime.now().strftime("%Y-%m-%d")
    out_dir = ROOT / "outputs" / today
    out_dir.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex
    out_path = out_dir / f"{file_id}.mp3"
    out_path.write_bytes(raw)
    print(f"saved         = {out_path}")
    print(f"absolute      = {out_path.resolve()}", flush=True)

    # ---- 6. Validate ----
    step("STEP 5 — validate mp3")
    info = validate_mp3(out_path)
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
    abs_path = str(out_path.resolve())
    print(f"launching: start \"\" \"{abs_path}\"", flush=True)
    os.system(f'start "" "{abs_path}"')

    # ---- summary ----
    step("SUMMARY")
    print(f"sample        = {SAMPLE_PATH}")
    print(f"M3 normalized = {len(normalized)} chars")
    print(f"MiMo TTS      = {len(raw)} bytes ({info['size']/1024:.1f} KB)")
    print(f"file path     = {abs_path}")
    print(f"format        = {'MP3' if info['valid_mp3'] else 'WAV'}")
    print("\nDONE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))