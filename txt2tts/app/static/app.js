// txt2tts 前端 —— 单页应用，hash 路由: #/ (听文档, 默认), #/upload (任务列表), #/play/<id>, #/task/<id>

const $ = (id) => document.getElementById(id);

const state = {
  voices: [],
  voicesSource: "static",
  // 听文档分页
  libPage: 1,
  libSize: 10,
  libTotal: 0,
  // 播放详情缓存
  playDetail: null,
  // 任务列表分页
  taskPage: 1,
  taskSize: 20,
  taskTotal: 0,
  // 任务详情轮询
  taskPollTimer: null,
  // 上传对话框文件
  uploadFile: null,
  // 当前活动 provider（前端缓存，影响说明文案）
  activeProvider: "mimo",
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

  // 停止任何正在进行的任务轮询
  stopTaskPoll();

if (path === "/") {
    showView("listen");
    renderListen();
  } else if (path === "/upload") {
    showView("upload");
    syncActiveProvider().then(() => renderTaskList());
  } else if (path === "/settings") {
    showView("settings");
    renderSettings();
  } else if (path.startsWith("/play/")) {
    const id = path.slice("/play/".length).split("/")[0];
    if (id) {
      showView("play");
      renderPlay(id);
    } else {
      location.hash = "#/";
    }
  } else if (path.startsWith("/task/")) {
    const id = path.slice("/task/".length).split("/")[0];
    if (id) {
      showView("task-detail");
      renderTaskDetail(id);
    } else {
      location.hash = "#/upload";
    }
  } else {
    location.hash = "#/";
  }
}

// ---- View: 听文档 (默认) ----------------------------------------------

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
        // 转歌词功能已移除：不再显示"已生成歌词"徽章
        const provider = it.provider || "mimo";
        const providerBadge = provider === "edge"
          ? `<span class="provider-badge provider-edge" title="方案二：edge-tts + ffmpeg">edge</span>`
          : `<span class="provider-badge provider-mimo" title="方案一：M3 + 小米 MiMo">mimo</span>`;
        return `
          <div class="library-item">
            <button class="btn btn-primary lib-play" data-action="play" data-id="${it.audio_id}" type="button">▶ 播放</button>
            <span class="lib-name" title="${escapeHtml(it.original_filename)}">${escapeHtml(it.original_filename)}</span>
            <span class="lib-meta">${providerBadge} voice: ${escapeHtml(voice)} · ${sizeKB} KB · ${escapeHtml(it.created_at)}</span>
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

// LRC 解析器：把 LRC 文本解析成 [{time, text, lineIdx}] 列表。
// 支持：
//   * 标准格式 [mm:ss.xx]、[mm:ss]、[mm:ss.xxx]（增强毫秒）
//   * 多时间戳 [00:01.00][00:05.00]同一句歌词重复 → 展开为多条
//   * 元信息行 [ti:标题] [ar:作者] [al:专辑] → 跳过
//   * 空行 / 无时间戳行 → 跳过
//   * 多行同时间戳 → 同一 lineIdx
// 失败返回 null。
function parseLrc(text) {
  if (!text || typeof text !== "string") return null;
  const lines = text.split(/\r?\n/);
  const out = [];          // {time, text, lineIdx}
  // 真实行号（连续非空行共享同一 lineIdx，让同时间的几行同步高亮）
  let lineIdx = -1;
  let lastSigTs = -1;
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    // 全部是元信息 [xx:...] 而没有时间戳
    const tsRe = /\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]/g;
    const matches = [...line.matchAll(tsRe)];
    if (!matches.length) continue;
    // 提取剩余文本
    const textPart = line.replace(tsRe, "").trim();
    // 跳过纯元信息 [ti:...] [ar:...] [al:...] [by:...]
    if (!textPart && /^\[[a-z]+:/i.test(line)) continue;
    // 把每条时间戳展开为独立条目
    let anyTs = false;
    for (const m of matches) {
      const mm = parseInt(m[1], 10);
      const ss = parseInt(m[2], 10);
      const frac = m[3] ? parseInt(m[3].padEnd(3, "0").slice(0, 3), 10) : 0;
      const t = mm * 60 + ss + frac / 1000;
      // 同一时间的多行共享 lineIdx
      if (t !== lastSigTs) { lineIdx += 1; lastSigTs = t; }
      out.push({ time: t, text: textPart || "", lineIdx });
      anyTs = true;
    }
    if (!anyTs) continue;
  }
  return out.length ? out : null;
}

// 二分查找：返回 currentTime 时刻的当前歌词行下标（最后一条 time <= t）。
function findCurrentLrcIdx(entries, t) {
  if (!entries || !entries.length) return -1;
  let lo = 0, hi = entries.length - 1, ans = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (entries[mid].time <= t) { ans = mid; lo = mid + 1; }
    else { hi = mid - 1; }
  }
  return ans;
}

async function renderPlay(audioId) {
  const titleEl = $("playTitle");
  const metaEl = $("playMeta");
  const contentEl = $("playContent");
  const audioEl = $("playAudio");

  titleEl.textContent = "加载中…";
  metaEl.textContent = "—";
  contentEl.innerHTML = '<p class="hint-inline">加载中…</p>';

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
  // 详情页用 edge provider 自动产出的 lrc（不是 LyricsService 改写的）；
  // 转歌词功能已移除。
  const lrcHint = detail.lyrics_url ? "🎤 歌词同步（edge provider SRT/LRC）" : "无歌词（段落模式）";
  metaEl.textContent =
    `voice: ${detail.voice_id || "—"} · ${(detail.byte_size / 1024).toFixed(1)} KB · ${detail.created_at} · id ${detail.audio_id.slice(0, 8)} · ${lrcHint}`;

  // 优先尝试拉 LRC → 音乐播放器模式；失败则降级到 normalized_md 段落模式
  let lrcEntries = null;
  if (detail.lyrics_url) {
    try {
      const r = await fetch(detail.lyrics_url);
      if (r.ok) {
        const txt = await r.text();
        lrcEntries = parseLrc(txt);
      }
    } catch (e) {
      console.warn("fetch LRC failed:", e);
    }
  }

  audioEl.src = detail.audio_url;
  audioEl.addEventListener("loadedmetadata", () => {
    if (lrcEntries && lrcEntries.length) {
      setupLrcSync(audioEl, lrcEntries);
    } else {
      const segments = (detail.normalized_md || "")
        .split(/\n\s*\n/)
        .map((s) => s.trim())
        .filter(Boolean);
      if (!segments.length) {
        contentEl.innerHTML = '<p class="hint-inline">该文档没有可显示的文本。</p>';
        return;
      }
      contentEl.innerHTML = segments
        .map((s, i) => `<p class="segment" data-idx="${i}">${escapeHtml(s).replace(/\n/g, "<br>")}</p>`)
        .join("");
      setupPlayHighlight(audioEl, segments);
    }
  }, { once: true });
}

function setupLrcSync(audioEl, entries) {
  const contentEl = $("playContent");
  // 按 lineIdx 分组（同一时间的多行共享 .lyric-line）
  const groups = [];
  for (const e of entries) {
    while (groups.length <= e.lineIdx) groups.push([]);
    groups[e.lineIdx].push(e);
  }
  contentEl.innerHTML = groups
    .map((grp, i) => {
      const timeAttr = grp[0].time;
      const html = grp
        .map((e) => `<span class="lyric-text">${escapeHtml(e.text || " ")}</span>`)
        .join("<br>");
      return `<p class="lyric-line" data-idx="${i}" data-time="${timeAttr}">${html}</p>`;
    })
    .join("");

  const lines = Array.from(contentEl.querySelectorAll(".lyric-line"));
  if (!lines.length) return;

  // 给 play-content 加 .lyric-mode 类，让 CSS 用居中、单行更大的样式
  contentEl.classList.add("lyric-mode");

  let currentLineIdx = -1;
  function applyLine(i) {
    if (i === currentLineIdx) return;
    lines.forEach((el, k) => {
      el.classList.toggle("playing", k === i);
      el.classList.toggle("past", k < i);
    });
    currentLineIdx = i;
    if (i >= 0 && lines[i]) {
      lines[i].scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }
  function update() {
    const t = audioEl.currentTime || 0;
    // 找最后一个 time <= t 的 entry
    const entryIdx = findCurrentLrcIdx(entries, t);
    if (entryIdx < 0) {
      // 还没到第一句
      if (currentLineIdx !== -1) applyLine(-1);
      return;
    }
    const lineIdx = entries[entryIdx].lineIdx;
    if (lineIdx !== currentLineIdx) applyLine(lineIdx);
  }

  audioEl.addEventListener("timeupdate", update);
  audioEl.addEventListener("ended", () => {
    lines.forEach((el) => el.classList.remove("playing"));
    currentLineIdx = -1;
  });

  // 上一句 / 下一句 / 重播：跳到对应时间
  $("playPrev").onclick = () => {
    const cur = currentLineIdx;
    const targetLine = Math.max(0, cur <= 0 ? 0 : cur - 1);
    const t = entries.find((e) => e.lineIdx === targetLine)?.time;
    if (typeof t === "number") {
      audioEl.currentTime = t;
      applyLine(targetLine);
    }
  };
  $("playNext").onclick = () => {
    const cur = currentLineIdx;
    const targetLine = Math.min(groups.length - 1, cur < 0 ? 0 : cur + 1);
    const t = entries.find((e) => e.lineIdx === targetLine)?.time;
    if (typeof t === "number") {
      audioEl.currentTime = t;
      applyLine(targetLine);
    }
  };
  $("playRestart").onclick = () => {
    audioEl.currentTime = 0;
    audioEl.play().catch(() => {});
    applyLine(0);
  };

  applyLine(0);
  update();
}

function setupPlayHighlight(audioEl, segments) {
  const contentEl = $("playContent");
  contentEl.classList.remove("lyric-mode");
  const segs = Array.from(contentEl.querySelectorAll(".segment"));
  if (!segs.length) return;

  const dur = audioEl.duration && isFinite(audioEl.duration) ? audioEl.duration : 0;
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
    if (segs[i]) segs[i].scrollIntoView({ behavior: "smooth", block: "center" });
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

  applyIdx(0);
}

// ---- View: 转语音任务列表 -----------------------------------------------

async function renderTaskList() {
  const list = $("taskList");
  list.innerHTML = '<p class="hint-inline">加载中…</p>';

  // 同步说明文案（按当前活动的 provider）
  updateUploadHint();

  try {
    const data = await api(`/api/tasks?page=${state.taskPage}&size=${state.taskSize}`);
    state.taskTotal = data.total;
    renderTaskPagination(data);
    if (!data.items.length) {
      list.innerHTML =
        '<p class="hint-inline">还没有任务。<button class="btn btn-primary" style="margin-left:8px;" onclick="openUploadDialog()">＋ 新增转语音</button></p>';
      return;
    }
    list.innerHTML = data.items
      .map((t) => {
        const badgeClass = statusBadgeClass(t.status);
        const progressPct = Math.round(t.progress * 100);
        const retryBadge = t.retry_count > 0
          ? `<span class="retry-badge" title="已重试 ${t.retry_count} 次">↻${t.retry_count}</span>`
          : "";
        const retryBtn = t.can_retry
          ? `<button class="btn btn-mini task-retry-btn" data-task-id="${t.task_id}" type="button" title="从原始 md 重跑">↻ 重试</button>`
          : "";
        const provider = t.provider || "mimo";
        const providerBadge = provider === "edge"
          ? `<span class="provider-badge provider-edge" title="方案二：edge-tts + ffmpeg">edge</span>`
          : `<span class="provider-badge provider-mimo" title="方案一：M3 + 小米 MiMo">mimo</span>`;
        const deleteTitle = t.status === "done"
          ? "删除任务；最终播放文件保留"
          : "删除任务及所有派生文件";
        return `
          <div class="task-item">
            <span class="status-badge ${badgeClass}">${escapeHtml(statusLabel(t.status))}</span>
            <span class="task-name" title="${escapeHtml(t.filename)}">${escapeHtml(t.filename)}</span>
            ${providerBadge}
            <span class="task-voice">${escapeHtml(t.voice_id || "默认")}</span>
            <span class="task-progress">${progressPct}% ${retryBadge}</span>
            <button class="btn btn-ghost btn-mini task-detail-btn" data-task-id="${t.task_id}" type="button">详情</button>
            ${retryBtn}
            <button class="btn btn-mini btn-danger task-delete-btn" data-task-id="${t.task_id}" data-filename="${escapeHtml(t.filename)}" data-status="${t.status}" type="button" title="${deleteTitle}">🗑 删除</button>
            <span class="task-time">${escapeHtml(t.created_at)}</span>
          </div>
        `;
      })
      .join("");

    list.querySelectorAll(".task-detail-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        location.hash = `#/task/${btn.dataset.taskId}`;
      });
    });
    list.querySelectorAll(".task-retry-btn").forEach((btn) => {
      btn.addEventListener("click", () => retryTask(btn.dataset.taskId));
    });
    list.querySelectorAll(".task-delete-btn").forEach((btn) => {
      btn.addEventListener("click", () => deleteTask(
        btn.dataset.taskId, btn.dataset.filename, btn.dataset.status,
      ));
    });
  } catch (e) {
    list.innerHTML = `<p class="hint-inline error">加载失败: ${escapeHtml(e.message)}</p>`;
  }
}

function updateUploadHint() {
  const el = $("uploadHintSteps");
  if (!el) return;
  el.textContent = HINT_BY_PROVIDER[state.activeProvider] || HINT_DEFAULT;
}

function renderTaskPagination(data) {
  const totalPages = Math.max(1, Math.ceil(data.total / data.size));
  $("taskPageInfo").textContent = `第 ${data.page} / ${totalPages} 页 · 共 ${data.total} 条`;
  $("taskPrev").disabled = data.page <= 1;
  $("taskNext").disabled = data.page >= totalPages;
}

function statusBadgeClass(status) {
  switch (status) {
    case "done": return "badge-ok";
    case "error": return "badge-err";
    case "processing": return "badge-processing";
    default: return "badge-pending";
  }
}

function statusLabel(status) {
  switch (status) {
    case "pending": return "等待中";
    case "processing": return "处理中";
    case "done": return "已完成";
    case "error": return "失败";
    case "failed_retryable": return "失败（可重试）";
    default: return status;
  }
}

// 触发后端重试任务；成功后刷新当前列表/详情
async function retryTask(taskId) {
  if (!confirm("确认重试此任务？将从磁盘原始 md 重新跑 pipeline。")) return;
  try {
    const resp = await api(`/api/tasks/${taskId}/retry`, { method: "POST" });
    setUploadStatus("✅ " + resp.message, "success");
    // 立即刷新列表（如果用户在任务列表页）
    if (currentPath() === "/upload") renderTaskList();
    // 如果在详情页，刷新详情
    if (currentPath() === `/task/${taskId}`) renderTaskDetail(taskId);
  } catch (e) {
    setUploadStatus("❌ 重试失败: " + e.message, "error");
  }
}

// 触发后端删除任务；成功后刷新列表/详情。
// status="done" 时后端只清中间产物 + 元数据，最终播放 mp3 + artifacts 保留。
async function deleteTask(taskId, filename, status) {
  const isDone = status === "done";
  const msg = isDone
    ? `确认删除任务「${filename || taskId.slice(0, 8)}」？\n\n` +
      "此任务已成功完成：\n" +
      "• 会删除：任务记录、听文档条目、中间产物（chunks/segments/uploads.md）\n" +
      "• 会保留：最终 mp3（outputs/audio/<id>.mp3）+ 中间产物快照（outputs/audio/_artifacts/<id>/）"
    : `确认删除任务「${filename || taskId.slice(0, 8)}」？\n\n` +
      "此任务尚未完成，所有派生文件将一并清除。";
  if (!confirm(msg)) return;
  try {
    const resp = await api(`/api/tasks/${taskId}`, { method: "DELETE" });
    const kept = resp.kept_final_audio
      ? `（最终 mp3 + artifacts 已保留在 outputs/audio/）`
      : `（全部清除）`;
    setUploadStatus(`✅ 已删除任务 ${taskId.slice(0, 8)} ${kept}`, "success");
    // 列表页 → 刷新；详情页 → 跳回列表
    if (currentPath() === "/upload") renderTaskList();
    if (currentPath() === `/task/${taskId}`) {
      location.hash = "#/upload";
    }
    // 同时刷新听文档页（如停留在那）
    if (currentPath() === "/") renderListen();
  } catch (e) {
    setUploadStatus("❌ 删除失败: " + e.message, "error");
  }
}

// ---- View: 任务详情（含轮询） -------------------------------------------

async function renderTaskDetail(taskId) {
  const titleEl = $("taskDetailTitle");
  const metaEl = $("taskDetailMeta");
  titleEl.textContent = "加载中…";
  metaEl.textContent = "—";
  $("taskDetailError").hidden = true;
  $("taskDetailDone").hidden = true;
  const _retryBtnInit = $("taskDetailRetryBtn");
  if (_retryBtnInit) _retryBtnInit.hidden = true;

  await _updateTaskDetail(taskId);

  // 如果任务仍在进行中，启动轮询
  const record = await _fetchTask(taskId);
  if (record && (record.status === "pending" || record.status === "processing")) {
    startTaskPoll(taskId);
  }
}

async function _fetchTask(taskId) {
  try {
    return await api(`/api/tasks/${taskId}`);
  } catch {
    return null;
  }
}

async function _updateTaskDetail(taskId) {
  const record = await _fetchTask(taskId);
  if (!record) {
    $("taskDetailTitle").textContent = "任务不存在";
    $("taskDetailMessage").textContent = "—";
    return;
  }

  $("taskDetailTitle").textContent = record.filename;
  $("taskDetailMeta").textContent =
    `voice: ${record.voice_id || "默认"} · id ${record.task_id.slice(0, 8)} · 创建于 ${record.created_at}`;

  // 步骤进度条
  renderTaskSteps(record);

  // 进度条
  const pct = Math.round(record.progress * 100);
  $("taskDetailPercent").textContent = `${pct}%`;
  $("taskDetailMessage").textContent = record.message || "—";
  const bar = $("taskDetailBarFill");
  bar.style.width = `${pct}%`;
  bar.classList.remove("done", "error");
  if (record.status === "done") bar.classList.add("done");
  if (record.status === "error") bar.classList.add("error");

  // 消息颜色
  const msgEl = $("taskDetailMessage");
  msgEl.dataset.kind = record.status === "error" ? "error" : (record.status === "done" ? "success" : "info");

  // 错误展示
  const errEl = $("taskDetailError");
  if ((record.status === "error" || record.status === "failed_retryable") && record.error) {
    errEl.hidden = false;
    errEl.textContent = record.error;
  } else {
    errEl.hidden = true;
  }

  // 完成后显示播放按钮；可重试时显示「↻ 重试」按钮
  const doneEl = $("taskDetailDone");
  const retryBtn = $("taskDetailRetryBtn");
  if (record.status === "done" && record.audio_id) {
    doneEl.hidden = false;
    $("taskDetailPlayBtn").href = `#/play/${record.audio_id}`;
  } else {
    doneEl.hidden = true;
  }
  if (record.status === "failed_retryable") {
    retryBtn.hidden = false;
    retryBtn.dataset.taskId = taskId;
    retryBtn.onclick = () => retryTask(taskId);
  } else {
    retryBtn.hidden = true;
  }

  // 删除按钮：始终可见（processing 状态也可删除，会标 aborted）
  const deleteBtn = $("taskDetailDeleteBtn");
  if (deleteBtn) {
    deleteBtn.dataset.taskId = taskId;
    deleteBtn.dataset.filename = record.filename;
    deleteBtn.dataset.status = record.status;
    deleteBtn.onclick = () => deleteTask(taskId, record.filename, record.status);
  }
  // 处理中的任务额外提示一下：删除会让后台协程继续跑但产物会被立即清掉
  const deleteHint = $("taskDetailDeleteHint");
  if (deleteHint) {
    if (record.status === "processing" || record.status === "pending") {
      deleteHint.textContent = "⚠ 任务仍在进行中：删除会让听文档/任务列表立刻移除该条；后台协程产生的中间产物也会一并清掉。";
    } else if (record.status === "done") {
      deleteHint.textContent = "✅ 任务已成功完成：删除仅清理任务记录与中间产物，最终播放文件（outputs/audio/<id>.mp3 + _artifacts/）会保留。";
    } else {
      deleteHint.textContent = "删除任务会同时清除中间产物；已成功完成的任务，最终播放文件会保留在 outputs/audio/ 目录。";
    }
  }
}

const STAGES_BY_PROVIDER = {
  mimo: [
    { stage: "markdown_clean",    label: "本地清洗" },
    { stage: "llm_normalize",     label: "M3 标准化" },
    { stage: "m3_split",          label: "M3 切分" },
    { stage: "tts_synthesize",    label: "MiMo 分块合成" },
    { stage: "ffmpeg_concat",     label: "ffmpeg 合并" },
    { stage: "audio_save",        label: "保存落盘" },
  ],
  edge: [
    { stage: "markdown_clean",    label: "本地清洗" },
    { stage: "llm_normalize",     label: "M3 语义预处理" },
    { stage: "tts_synthesize",    label: "edge-tts 分段合成" },
    { stage: "ffmpeg_concat",     label: "ffmpeg 合并 + SRT" },
    { stage: "audio_save",        label: "保存落盘" },
  ],
};
// 兜底（provider 未知 / 老库）：旧的 4 步
const STAGES_LEGACY = [
  { stage: "markdown_clean", label: "本地清洗" },
  { stage: "llm_normalize",  label: "M3 标准化" },
  { stage: "tts_synthesize", label: "语音合成" },
  { stage: "audio_save",     label: "保存落盘" },
];

const HINT_BY_PROVIDER = {
  mimo: "清洗 → M3 标准化 → M3 切分 → MiMo 分块合成 → ffmpeg 合并 → 保存",
  edge: "清洗 → M3 语义预处理 → edge-tts 分段合成 → ffmpeg 合并 + SRT → 保存",
};
const HINT_DEFAULT = "清洗 → M3 标准化 → TTS 合成 → 保存";

function stagesFor(record) {
  const p = (record && record.provider) || "mimo";
  return STAGES_BY_PROVIDER[p] || STAGES_LEGACY;
}

function renderTaskSteps(record) {
  const container = $("taskDetailSteps");
  const steps = stagesFor(record);
  let html = "";
  steps.forEach((s, i) => {
    const stage = s.stage;
    const stageIdx = steps.findIndex((x) => x.stage === record.current_stage);
    let cls = "";
    if (record.status === "error" || record.status === "failed_retryable") {
      // 当前阶段及后续都标错
      if (stage === record.current_stage || (record.current_stage === null && i === 0)) cls = "error";
      else if (stageIdx >= 0 && i < stageIdx) cls = "done";
    } else if (record.status === "done") {
      cls = "done";
    } else if (stageIdx >= 0 && i < stageIdx) {
      cls = "done";
    } else if (stage === record.current_stage) {
      cls = "active";
    }
    html += `<div class="step ${cls}" data-stage="${stage}"><div class="step-icon">${i + 1}</div><div class="step-label">${s.label}</div></div>`;
    if (i < steps.length - 1) {
      let barCls = "";
      if (cls === "done") barCls = "done";
      else if (cls === "active") barCls = "active";
      html += `<div class="step-bar ${barCls}" data-bar="${stage}"></div>`;
    }
  });
  container.innerHTML = html;
}

function startTaskPoll(taskId) {
  stopTaskPoll();
  state.taskPollTimer = setInterval(async () => {
    const record = await _fetchTask(taskId);
    if (!record) {
      stopTaskPoll();
      return;
    }
    await _updateTaskDetail(taskId);
    // 任务结束后停止轮询
    if (record.status === "done" || record.status === "error") {
      stopTaskPoll();
    }
  }, 2000);
}

function stopTaskPoll() {
  if (state.taskPollTimer) {
    clearInterval(state.taskPollTimer);
    state.taskPollTimer = null;
  }
}

// ---- 上传对话框 -----------------------------------------------------------

function openUploadDialog() {
  $("uploadDialog").hidden = false;
  $("taskFileInput").value = "";
  $("taskFileLabel").textContent = "点击选择 .md 文件…";
  $("dialogSubmitBtn").disabled = true;
  state.uploadFile = null;
  setUploadStatus("选择文件后点击提交");
  // 填充语音下拉
  populateVoiceSelect($("taskVoiceSelect"));
}

function closeUploadDialog() {
  $("uploadDialog").hidden = true;
}

async function submitTask() {
  if (!state.uploadFile) {
    setUploadStatus("请先选择文件", "error");
    return;
  }
  const btn = $("dialogSubmitBtn");
  btn.disabled = true;
  setUploadStatus("提交中…");

  const fd = new FormData();
  fd.append("file", state.uploadFile);
  const voiceId = $("taskVoiceSelect").value;
  if (voiceId) fd.append("voice_id", voiceId);

  try {
    const resp = await api("/api/tasks", { method: "POST", body: fd });
    setUploadStatus("✅ 任务已提交，关闭对话框…");
    closeUploadDialog();
    // 刷新任务列表并跳转到第一页
    state.taskPage = 1;
    renderTaskList();
    // 自动进入详情页
    setTimeout(() => {
      location.hash = `#/task/${resp.task_id}`;
    }, 300);
  } catch (e) {
    setUploadStatus("❌ " + e.message, "error");
    btn.disabled = false;
  }
}

function setUploadStatus(msg, kind = "info") {
  const el = $("uploadStatus");
  el.textContent = msg;
  el.dataset.kind = kind;
}

// ---- Voice list ----------------------------------------------------------

async function loadVoices() {
  try {
    const data = await api("/api/voices");
    state.voices = data.voices || [];
    state.voicesSource = data.source || "static";
  } catch (e) {
    console.warn("加载语音列表失败:", e);
  }
}

function populateVoiceSelect(sel) {
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
  $("taskPrev").addEventListener("click", () => {
    if (state.taskPage > 1) {
      state.taskPage -= 1;
      renderTaskList();
    }
  });
  $("taskNext").addEventListener("click", () => {
    const totalPages = Math.max(1, Math.ceil(state.taskTotal / state.taskSize));
    if (state.taskPage < totalPages) {
      state.taskPage += 1;
      renderTaskList();
    }
  });
}

// ---- Health / status ----------------------------------------------------

// ---- View: 系统设置 ------------------------------------------------------

let settingsCache = null;

async function syncActiveProvider() {
  try {
    const data = await api("/api/settings");
    state.activeProvider = data.tts_provider || "mimo";
  } catch (e) {
    console.warn("syncActiveProvider failed:", e);
  }
}

async function renderSettings() {
  $("settingsApplyBtn").disabled = true;
  $("settingsStatus").textContent = "加载中…";
  try {
    const data = await api("/api/settings");
    settingsCache = data;
    state.activeProvider = data.tts_provider || "mimo";
    $("settingsCurrentProvider").textContent =
      `${data.tts_provider}（${data.tts_provider === "edge" ? "方案二" : "方案一"}）`;
    // 选中单选框
    document.querySelectorAll('input[name="provider"]').forEach((el) => {
      el.checked = (el.value === data.tts_provider);
    });
    // 启用应用按钮当用户改了选项
    document.querySelectorAll('input[name="provider"]').forEach((el) => {
      el.addEventListener("change", () => {
        const newVal = document.querySelector('input[name="provider"]:checked')?.value;
        $("settingsApplyBtn").disabled = (newVal === data.tts_provider);
      });
    });
    $("settingsStatus").textContent = "就绪";
  } catch (e) {
    $("settingsStatus").textContent = "❌ " + e.message;
    $("settingsStatus").dataset.kind = "error";
  }
}

async function applySettings() {
  const newVal = document.querySelector('input[name="provider"]:checked')?.value;
  if (!newVal) return;
  $("settingsApplyBtn").disabled = true;
  $("settingsStatus").textContent = "应用更改中…";
  try {
    const data = await api("/api/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tts_provider: newVal }),
    });
    settingsCache = data;
    $("settingsCurrentProvider").textContent =
      `${data.tts_provider}（${data.tts_provider === "edge" ? "方案二" : "方案一"}）`;
    $("settingsStatus").textContent = "✅ 已切换；下次上传将使用新方案。";
    $("settingsStatus").dataset.kind = "success";
    // 如果用户在听文档页，刷新列表（provider 徽章）
    if (currentPath() === "/") renderListen();
  } catch (e) {
    $("settingsStatus").textContent = "❌ " + e.message;
    $("settingsStatus").dataset.kind = "error";
    $("settingsApplyBtn").disabled = false;
  }
}

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
    } else if (ttsOk) {
      badge.textContent = "仅 TTS 已配置";
      badge.className = "badge badge-warn";
    } else {
      badge.textContent = "API key 未配置";
      badge.className = "badge badge-warn";
    }
    $("ttsInfo").textContent =
      `M3: ${h.llm_model} · TTS: ${h.tts_model} (${h.tts_base_url})`;
  } catch (e) {
    $("configBadge").textContent = "后端不可达";
    $("configBadge").className = "badge badge-err";
  }
}

// ---- Boot ----------------------------------------------------------------

document.addEventListener("DOMContentLoaded", () => {
  bindPagination();

  // 歌词对话框关闭
  // 上传对话框绑定
  $("newTaskBtn").addEventListener("click", openUploadDialog);
  $("settingsApplyBtn").addEventListener("click", applySettings);
  $("dialogCloseBtn").addEventListener("click", closeUploadDialog);
  $("dialogCancelBtn").addEventListener("click", closeUploadDialog);
  $("taskFileInput").addEventListener("change", () => {
    const f = $("taskFileInput").files[0];
    if (!f) return;
    state.uploadFile = f;
    $("taskFileLabel").textContent = `${f.name}  (${(f.size / 1024).toFixed(1)} KB)`;
    $("dialogSubmitBtn").disabled = false;
    setUploadStatus("已选择文件，点击提交");
  });
  $("dialogSubmitBtn").addEventListener("click", submitTask);

  // 点击 overlay 背景关闭
  $("uploadDialog").addEventListener("click", (e) => {
    if (e.target === $("uploadDialog")) closeUploadDialog();
  });

  window.addEventListener("hashchange", router);

  // 强制默认路由
  if (!location.hash) location.hash = "#/";

  checkHealth();
  loadVoices();
  router();
});

// 转歌词功能已移除：LyricsService / /api/library/{id}/lyrics 端点都下线了。
// 详情页 LRC 由 edge provider 流水线自动产出（SentenceBoundary cues → SRT/LRC），
// 音乐播放器模式直接 fetch /api/lyrics/{id}.lrc 即可。
