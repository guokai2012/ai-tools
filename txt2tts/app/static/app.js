// Minimal vanilla-JS client for the txt2tts backend.

const $ = (id) => document.getElementById(id);

const state = {
  voices: [],
  voicesSource: "static",
  currentFile: null,
  currentText: "",
  audioUrl: null,
  history: [],
};

// ---- API helpers ---------------------------------------------------------

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const ct = r.headers.get("content-type") || "";
  const body = ct.includes("application/json") ? await r.json() : await r.text();
  if (!r.ok) {
    const msg = (body && body.detail) || (typeof body === "string" ? body : r.statusText);
    throw new Error(`${r.status} ${msg}`);
  }
  return body;
}

// ---- Voice list ----------------------------------------------------------

async function loadVoices() {
  setStatus("加载语音列表…");
  try {
    const data = await api("/api/voices");
    state.voices = data.voices || [];
    state.voicesSource = data.source || "static";
    const sel = $("voiceSelect");
    sel.innerHTML = "";
    if (state.voices.length === 0) {
      sel.innerHTML = '<option value="">(无可用语音)</option>';
    } else {
      for (const v of state.voices) {
        const opt = document.createElement("option");
        opt.value = v.id;
        opt.textContent = v.name + (v.lang ? `  [${v.lang}]` : "");
        sel.appendChild(opt);
      }
    }
    setStatus(`已加载 ${state.voices.length} 个语音（来源: ${state.voicesSource}）`);
  } catch (e) {
    setStatus("加载语音失败: " + e.message, "error");
  }
}

// ---- File picking & preview ---------------------------------------------

function bindFilePicker() {
  const input = $("mdFile");
  input.addEventListener("change", async () => {
    const f = input.files[0];
    if (!f) return;
    state.currentFile = f;
    $("mdFileLabel").textContent = `${f.name}  (${(f.size / 1024).toFixed(1)} KB)`;
    $("synthBtn").disabled = true;
    setStatus("正在清洗 Markdown…");
    try {
      const fd = new FormData();
      fd.append("file", f);
      const data = await api("/api/preview", { method: "POST", body: fd });
      // /api/preview now returns {local_clean, normalized, length, source}
      // The M3-normalized text is what TTS will receive.
      state.currentText = data.normalized || data.cleaned || "";
      $("cleanedPreview").value = state.currentText;
      $("textMeta").textContent =
        `${data.filename} · ${data.length} 字符 · 来源: ${data.source || "?"}`;
      $("synthBtn").disabled = !state.currentText.trim();
      setStatus("准备就绪，点击「开始朗读」");
    } catch (e) {
      setStatus("预览失败: " + e.message, "error");
      $("cleanedPreview").value = "";
      $("textMeta").textContent = "—";
      state.currentText = "";
    }
  });
}

async function loadSample() {
  try {
    const r = await fetch("/static/../samples/demo.md");
    if (!r.ok) throw new Error("找不到示例文件");
    const text = await r.text();
    const blob = new Blob([text], { type: "text/markdown" });
    const file = new File([blob], "demo.md", { type: "text/markdown" });
    const dt = new DataTransfer();
    dt.items.add(file);
    $("mdFile").files = dt.files;
    $("mdFile").dispatchEvent(new Event("change"));
  } catch (e) {
    setStatus("载入示例失败: " + e.message, "error");
  }
}

// ---- Progress bar helpers ----------------------------------------------

const STAGE_ORDER = ["markdown_clean", "llm_normalize", "tts_synthesize", "audio_save"];

function resetProgress() {
  // Reset all 4 steps and the connector bars to "pending".
  STAGE_ORDER.forEach((stage) => {
    const step = document.querySelector(`.step[data-stage="${stage}"]`);
    if (step) {
      step.classList.remove("active", "done", "error");
    }
    const bar = document.querySelector(`.step-bar[data-bar="${stage}"]`);
    if (bar) {
      bar.classList.remove("active", "done", "error");
    }
  });
  const fill = $("progressBarFill");
  fill.style.width = "0%";
  fill.classList.remove("done", "error");
  setProgressMessage("准备就绪…", "info");
  $("progressPercent").textContent = "0%";
}

function applyProgressEvent(ev) {
  // ev shape: {stage, progress, message, audio_id, audio_url, error}
  const fill = $("progressBarFill");
  fill.style.width = `${Math.round(ev.progress * 100)}%`;
  $("progressPercent").textContent = `${Math.round(ev.progress * 100)}%`;
  if (ev.message) {
    const kind = ev.error ? "error" : (ev.stage === "done" ? "success" : "info");
    setProgressMessage(ev.message, kind);
  }

  if (ev.stage === "error") {
    fill.classList.add("error");
    // Mark the current pending step (the one that just failed) as error.
    // Heuristic: find the first step not yet "done".
    for (const stage of STAGE_ORDER) {
      const step = document.querySelector(`.step[data-stage="${stage}"]`);
      if (step && !step.classList.contains("done")) {
        step.classList.add("error");
        break;
      }
    }
    return;
  }

  if (ev.stage === "done") {
    fill.classList.add("done");
    STAGE_ORDER.forEach((stage) => {
      const step = document.querySelector(`.step[data-stage="${stage}"]`);
      if (step) step.classList.remove("active");
      const bar = document.querySelector(`.step-bar[data-bar="${stage}"]`);
      if (bar) bar.classList.add("done");
    });
    return;
  }

  if (STAGE_ORDER.includes(ev.stage)) {
    // Mark this stage active.
    const step = document.querySelector(`.step[data-stage="${ev.stage}"]`);
    if (step) {
      step.classList.add("active");
    }
    // Mark previous stages done + their connector bars.
    const idx = STAGE_ORDER.indexOf(ev.stage);
    for (let i = 0; i < idx; i++) {
      const s = document.querySelector(`.step[data-stage="${STAGE_ORDER[i]}"]`);
      if (s) {
        s.classList.remove("active");
        s.classList.add("done");
      }
      const b = document.querySelector(`.step-bar[data-bar="${STAGE_ORDER[i]}"]`);
      if (b) {
        b.classList.remove("active");
        b.classList.add("done");
      }
    }
    // Activate the connector bar leading into this stage.
    if (idx > 0) {
      const bar = document.querySelector(`.step-bar[data-bar="${STAGE_ORDER[idx - 1]}"]`);
      if (bar) {
        bar.classList.remove("done");
        bar.classList.add("active");
      }
    }
  }
}

function setProgressMessage(msg, kind) {
  const el = $("progressMessage");
  el.textContent = msg;
  el.dataset.kind = kind || "info";
}

// ---- Synthesis (SSE) -----------------------------------------------------

async function synthesize() {
  if (!state.currentFile) {
    setStatus("请先选择 .md 文件", "error");
    return;
  }
  const btn = $("synthBtn");
  btn.disabled = true;

  // Show + reset the progress UI.
  const wrap = $("progressWrap");
  wrap.hidden = false;
  resetProgress();

  // Build the request body. EventSource doesn't accept a body, so we POST
  // via fetch streaming (text/event-stream) instead.
  const fd = new FormData();
  fd.append("file", state.currentFile);
  const voiceId = $("voiceSelect").value;
  if (voiceId) fd.append("voice_id", voiceId);

  setStatus("调用 TTS 合成中…");
  let resp;
  try {
    resp = await fetch("/api/synthesize", { method: "POST", body: fd });
  } catch (e) {
    setStatus("❌ " + e.message, "error");
    btn.disabled = false;
    return;
  }
  if (!resp.ok || !resp.body) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch {}
    setStatus("❌ " + detail, "error");
    applyProgressEvent({ stage: "error", progress: 0, message: detail });
    btn.disabled = false;
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let finalEvent = null;
  let errored = false;

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by blank lines; split and process each.
      let idx;
      while ((idx = buffer.indexOf("\n\n")) !== -1) {
        const frame = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const line = frame.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        try {
          const ev = JSON.parse(line.slice(6));
          applyProgressEvent(ev);
          if (ev.stage === "error") {
            errored = true;
            setStatus("❌ " + (ev.error || ev.message || "合成失败"), "error");
          } else if (ev.stage === "done") {
            finalEvent = ev;
          }
        } catch (parseErr) {
          // ignore malformed frame
        }
      }
    }
  } catch (e) {
    setStatus("❌ 流读取失败: " + e.message, "error");
    errored = true;
  }

  btn.disabled = false;

  if (errored || !finalEvent) return;

  // Hide progress, show player + download row.
  const player = $("player");
  player.src = finalEvent.audio_url;
  player.hidden = false;
  player.play().catch(() => {});

  $("downloadRow").hidden = false;
  $("downloadLink").href = finalEvent.audio_url;
  $("downloadLink").download = `${state.currentFile.name.replace(/\.[^.]+$/, "")}_${finalEvent.audio_id}.mp3`;
  $("audioMeta").textContent =
    `voice: ${finalEvent.voice_id} · ${finalEvent.text_length} chars · id ${finalEvent.audio_id.slice(0, 8)}`;

  state.audioUrl = finalEvent.audio_url;
  pushHistory({
    id: finalEvent.audio_id,
    file: state.currentFile.name,
    voice: finalEvent.voice_id,
    url: finalEvent.audio_url,
    at: new Date().toLocaleTimeString(),
  });

  setStatus("✅ 已生成，可以重听");
  }
}

function pushHistory(item) {
  state.history.unshift(item);
  state.history = state.history.slice(0, 10);
  const ol = $("history");
  ol.innerHTML = "";
  for (const h of state.history) {
    const li = document.createElement("li");
    li.className = "history-item";
    li.innerHTML = `
      <span class="hist-time">${h.at}</span>
      <span class="hist-file">${h.file}</span>
      <span class="hist-voice">${h.voice}</span>
      <audio controls src="${h.url}"></audio>
      <a class="btn btn-mini" href="${h.url}" download="${h.file.replace(/\.[^.]+$/, "")}_${h.id}.mp3">下载</a>
    `;
    ol.appendChild(li);
  }
}

// ---- Health / status ----------------------------------------------------

async function checkHealth() {
  try {
    const h = await api("/api/health");
    const badge = $("configBadge");
    const ttsOk = !!h.tts_configured;
    const llmOk = !!h.llm_configured;
    if (ttsOk && llmOk) {
      badge.textContent = "M3 + TTS 已配置";
      badge.className = "badge badge-ok";
      badge.title = `M3=${h.llm_model} (${h.llm_base_url})\nTTS=${h.tts_model} (${h.tts_base_url})`;
    } else if (llmOk) {
      badge.textContent = "仅 M3 已配置";
      badge.className = "badge badge-warn";
      badge.title = "TTS key 未配置";
    } else if (ttsOk) {
      badge.textContent = "仅 TTS 已配置";
      badge.className = "badge badge-warn";
      badge.title = "M3 key 未配置";
    } else {
      badge.textContent = "API key 未配置";
      badge.className = "badge badge-warn";
      badge.title = "请设置 LLM__API_KEY 和 TTS__API_KEY 后重启";
    }
    $("ttsInfo").textContent =
      `M3: ${h.llm_model} · TTS: ${h.tts_model} (${h.tts_base_url})`;
  } catch (e) {
    $("configBadge").textContent = "后端不可达";
    $("configBadge").className = "badge badge-err";
  }
}

function setStatus(msg, kind = "info") {
  const el = $("status");
  el.textContent = msg;
  el.dataset.kind = kind;
}

// ---- Boot ----------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  bindFilePicker();
  $("synthBtn").addEventListener("click", synthesize);
  $("refreshVoicesBtn").addEventListener("click", loadVoices);
  $("loadSampleBtn").addEventListener("click", loadSample);
  $("speedRange").addEventListener("input", (e) => {
    $("speedLabel").textContent = `${(+e.target.value).toFixed(1)}×`;
  });
  checkHealth();
  loadVoices();
});