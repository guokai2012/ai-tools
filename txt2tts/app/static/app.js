// Minimal vanilla-JS client for the txt2tts backend.
// Single-page app with hash-based routing: #/ (listen, default), #/upload, #/play/<id>.

const $ = (id) => document.getElementById(id);

const state = {
  voices: [],
  voicesSource: "static",
  currentFile: null,
  currentText: "",
  audioUrl: null,
  history: [],
  // Library (听文档) pagination
  libPage: 1,
  libSize: 10,
  libTotal: 0,
  // Cached play detail (so the player view can re-render segments without re-fetching)
  playDetail: null,
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

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[c]
  ));
}

// ---- Hash router ---------------------------------------------------------

function currentPath() {
  const h = location.hash.replace(/^#/, "");
  return h === "" ? "/" : h;
}

function showView(name) {
  document.querySelectorAll(".view").forEach((v) => { v.hidden = true; });
  const el = $(`view-${name}`);
  if (el) el.hidden = false;
}

function highlightMenu(path) {
  document.querySelectorAll(".menu-item").forEach((a) => {
    const r = a.dataset.route;
    const active = r === "/" ? path === "/" : path.startsWith(r);
    a.classList.toggle("active", active);
  });
}

function router() {
  const path = currentPath();
  highlightMenu(path);

  if (path === "/") {
    showView("listen");
    renderListen();
  } else if (path === "/upload") {
    showView("upload");
    // The upload view's bindings are set up once on boot; nothing per-route.
  } else if (path.startsWith("/play/")) {
    const id = path.slice("/play/".length).split("/")[0];
    if (id) {
      showView("play");
      renderPlay(id);
    } else {
      location.hash = "#/";
    }
  } else {
    // Unknown route → fall back to listen.
    location.hash = "#/";
  }
}

// ---- View: 听文档 (default) ----------------------------------------------

async function renderListen() {
  const list = $("libraryList");
  list.innerHTML = '<p class="hint-inline">加载中…</p>';

  try {
    const data = await api(`/api/library?page=${state.libPage}&size=${state.libSize}`);
    state.libTotal = data.total;
    renderPagination(data);
    if (!data.items.length) {
      list.innerHTML =
        '<p class="hint-inline">还没有任何已转语音的文档。<a href="#/upload">去上传一个 .md</a>。</p>';
      return;
    }
    list.innerHTML = data.items
      .map((it) => {
        const sizeKB = (it.byte_size / 1024).toFixed(1);
        const voice = it.voice_id || "—";
        return `
          <div class="library-item">
            <button class="btn btn-primary lib-play" data-action="play" data-id="${it.audio_id}" type="button">▶ 播放</button>
            <span class="lib-name" title="${escapeHtml(it.original_filename)}">${escapeHtml(it.original_filename)}</span>
            <span class="lib-meta">voice: ${escapeHtml(voice)} · ${sizeKB} KB · ${escapeHtml(it.created_at)}</span>
          </div>
        `;
      })
      .join("");

    list.querySelectorAll(".lib-play").forEach((btn) => {
      btn.addEventListener("click", () => {
        location.hash = `#/play/${btn.dataset.id}`;
      });
    });
  } catch (e) {
    list.innerHTML = `<p class="hint-inline error">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}

function renderPagination(data) {
  const totalPages = Math.max(1, Math.ceil(data.total / data.size));
  $("libPageInfo").textContent = `第 ${data.page} / ${totalPages} 页 · 共 ${data.total} 条`;
  $("libPrev").disabled = data.page <= 1;
  $("libNext").disabled = data.page >= totalPages;
}

// ---- View: 播放详情 ------------------------------------------------------

async function renderPlay(audioId) {
  const titleEl = $("playTitle");
  const metaEl = $("playMeta");
  const contentEl = $("playContent");
  const audioEl = $("playAudio");

  titleEl.textContent = "加载中…";
  metaEl.textContent = "—";
  contentEl.innerHTML = '<p class="hint-inline">加载中…</p>';

  // Pause any previous playback and reset src so listeners don't double-fire.
  audioEl.pause();
  audioEl.removeAttribute("src");
  audioEl.load();

  let detail;
  try {
    detail = await api(`/api/library/${audioId}`);
  } catch (e) {
    titleEl.textContent = "加载失败";
    contentEl.innerHTML = `<p class="hint-inline error">${escapeHtml(e.message)}</p>`;
    return;
  }
  state.playDetail = detail;

  titleEl.textContent = detail.original_filename;
  metaEl.textContent =
    `voice: ${detail.voice_id || "—"} · ${(detail.byte_size / 1024).toFixed(1)} KB · ${detail.created_at} · id ${detail.audio_id.slice(0, 8)}`;

  // Split normalized text into segments on blank lines; drop empties.
  const segments = (detail.normalized_md || "")
    .split(/\n\s*\n/)
    .map((s) => s.trim())
    .filter(Boolean);

  if (!segments.length) {
    contentEl.innerHTML = '<p class="hint-inline">该文档没有可显示的 normalized 文本。</p>';
    return;
  }

  contentEl.innerHTML = segments
    .map((s, i) => `<p class="segment" data-idx="${i}">${escapeHtml(s).replace(/\n/g, "<br>")}</p>`)
    .join("");

  audioEl.src = detail.audio_url;
  audioEl.addEventListener("loadedmetadata", () => setupPlayHighlight(audioEl, segments), { once: true });
}

function setupPlayHighlight(audioEl, segments) {
  const dur = audioEl.duration && isFinite(audioEl.duration) ? audioEl.duration : 0;
  const segs = Array.from(document.querySelectorAll(".play-content .segment"));
  if (!segs.length) return;

  let startTimes;
  if (dur <= 0 || segments.length === 0) {
    startTimes = new Array(segments.length).fill(0);
  } else {
    startTimes = segments.map((_, i) => (dur * i) / segments.length);
  }

  function currentIdx() {
    return segs.findIndex((s) => s.classList.contains("playing"));
  }
  function applyIdx(i) {
    segs.forEach((el, k) => el.classList.toggle("playing", k === i));
    segs[i].scrollIntoView({ behavior: "smooth", block: "center" });
  }
  function update() {
    const t = audioEl.currentTime || 0;
    let idx = 0;
    for (let i = 0; i < startTimes.length; i++) {
      if (t >= startTimes[i]) idx = i;
    }
    if (!segs[idx].classList.contains("playing")) {
      applyIdx(idx);
    }
  }

  audioEl.addEventListener("timeupdate", update);
  audioEl.addEventListener("ended", () => {
    segs.forEach((s) => s.classList.remove("playing"));
  });

  $("playPrev").onclick = () => {
    const cur = currentIdx();
    const next = Math.max(0, cur < 0 ? 0 : cur - 1);
    audioEl.currentTime = startTimes[next];
    applyIdx(next);
  };
  $("playNext").onclick = () => {
    const cur = currentIdx();
    const next = Math.min(segments.length - 1, cur < 0 ? 0 : cur + 1);
    audioEl.currentTime = startTimes[next];
    applyIdx(next);
  };
  $("playRestart").onclick = () => {
    audioEl.currentTime = 0;
    audioEl.play().catch(() => {});
    applyIdx(0);
  };

  // Initial highlight (so the first segment is visibly active even before play).
  applyIdx(0);
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
  STAGE_ORDER.forEach((stage) => {
    const step = document.querySelector(`.step[data-stage="${stage}"]`);
    if (step) step.classList.remove("active", "done", "error");
    const bar = document.querySelector(`.step-bar[data-bar="${stage}"]`);
    if (bar) bar.classList.remove("active", "done", "error");
  });
  const fill = $("progressBarFill");
  fill.style.width = "0%";
  fill.classList.remove("done", "error");
  setProgressMessage("准备就绪…", "info");
  $("progressPercent").textContent = "0%";
}

function applyProgressEvent(ev) {
  const fill = $("progressBarFill");
  fill.style.width = `${Math.round(ev.progress * 100)}%`;
  $("progressPercent").textContent = `${Math.round(ev.progress * 100)}%`;
  if (ev.message) {
    const kind = ev.error ? "error" : (ev.stage === "done" ? "success" : "info");
    setProgressMessage(ev.message, kind);
  }

  if (ev.stage === "error") {
    fill.classList.add("error");
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
    const step = document.querySelector(`.step[data-stage="${ev.stage}"]`);
    if (step) step.classList.add("active");
    const idx = STAGE_ORDER.indexOf(ev.stage);
    for (let i = 0; i < idx; i++) {
      const s = document.querySelector(`.step[data-stage="${STAGE_ORDER[i]}"]`);
      if (s) { s.classList.remove("active"); s.classList.add("done"); }
      const b = document.querySelector(`.step-bar[data-bar="${STAGE_ORDER[i]}"]`);
      if (b) { b.classList.remove("active"); b.classList.add("done"); }
    }
    if (idx > 0) {
      const bar = document.querySelector(`.step-bar[data-bar="${STAGE_ORDER[idx - 1]}"]`);
      if (bar) { bar.classList.remove("done"); bar.classList.add("active"); }
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

  const wrap = $("progressWrap");
  wrap.hidden = false;
  resetProgress();

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

  setStatus("✅ 已生成，可以重听 · 也可以去「听文档」页查看");
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
      <span class="hist-file">${escapeHtml(h.file)}</span>
      <span class="hist-voice">${escapeHtml(h.voice)}</span>
      <audio controls src="${h.url}"></audio>
      <a class="btn btn-mini" href="${h.url}" download="${escapeHtml(h.file.replace(/\.[^.]+$/, ""))}_${h.id}.mp3">下载</a>
    `;
    ol.appendChild(li);
  }
}

// ---- Pagination controls ------------------------------------------------

function bindPagination() {
  $("libPrev").addEventListener("click", () => {
    if (state.libPage > 1) {
      state.libPage -= 1;
      renderListen();
    }
  });
  $("libNext").addEventListener("click", () => {
    const totalPages = Math.max(1, Math.ceil(state.libTotal / state.libSize));
    if (state.libPage < totalPages) {
      state.libPage += 1;
      renderListen();
    }
  });
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
  bindPagination();
  $("synthBtn").addEventListener("click", synthesize);
  $("refreshVoicesBtn").addEventListener("click", loadVoices);
  $("loadSampleBtn").addEventListener("click", loadSample);
  $("speedRange").addEventListener("input", (e) => {
    $("speedLabel").textContent = `${(+e.target.value).toFixed(1)}×`;
  });
  window.addEventListener("hashchange", router);

  // Force default route if none is present.
  if (!location.hash) location.hash = "#/";

  checkHealth();
  loadVoices();
  router();
});