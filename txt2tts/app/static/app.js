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
  activeProvider: "minimax",
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
        const sizeKB = it.byte_size != null ? (it.byte_size / 1024).toFixed(1) : "—";
        const voice = it.voice_id || "—";
        const provider = it.provider || "minimax";
        const providerBadge = provider === "edge"
          ? `<span class="provider-badge provider-edge" title="方案二：edge-tts + ffmpeg">edge</span>`
          : `<span class="provider-badge provider-minimax" title="方案一：M3 + MiniMax speech-2.8-hd">minimax</span>`;
        const lrcBadge = it.has_lrc
          ? `<span class="lrc-badge" title="LRC 字幕就绪">🎤 LRC</span>`
          : "";
        return `
          <div class="library-item">
            <button class="btn btn-primary lib-play" data-action="play" data-id="${it.task_id}" type="button">▶ 播放</button>
            <span class="lib-name" title="${escapeHtml(it.original_filename)}">${escapeHtml(it.original_filename)}</span>
            <span class="lib-meta">${providerBadge}${lrcBadge} voice: ${escapeHtml(voice)} · ${sizeKB} KB · ${escapeHtml(it.created_at)}</span>
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

async function renderPlay(taskId) {
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
    detail = await api(`/api/library/${taskId}`);
  } catch (e) {
    titleEl.textContent = "加载失败";
    contentEl.innerHTML = `<p class="hint-inline error">${escapeHtml(e.message)}</p>`;
    return;
  }
  state.playDetail = detail;

  titleEl.textContent = detail.original_filename;
  // 详情页用 minimax / edge provider 自动产出的 lrc（不是 LyricsService 改写的）；
  // 转歌词功能已移除。
  // v5 后 audio_id 已合并到 task_id；LRC URL 字段为 lrc_url。
  const lrcHint = detail.lrc_url
    ? "🎤 歌词同步（minimax / edge provider SRT/LRC）"
    : "无歌词（段落模式）";
  // 防御：detail.task_id / detail.byte_size 可能为 undefined（旧缓存/残留字段）；
  // 上次修复后字段必填，但浏览器可能还缓存旧 app.js，加防御总比抛错好。
  const safeId = (typeof detail.task_id === "string" && detail.task_id)
    ? detail.task_id.slice(0, 8) : "—";
  const safeKb = typeof detail.byte_size === "number"
    ? (detail.byte_size / 1024).toFixed(1) + " KB" : "—";
  metaEl.textContent =
    `voice: ${detail.voice_id || "—"} · ${safeKb} · ${detail.created_at || ""} · id ${safeId} · ${lrcHint}`;

  // 优先尝试拉 LRC → 音乐播放器模式；失败则降级到 normalized_md 段落模式
  let lrcEntries = null;
  if (detail.lrc_url) {
    try {
      const r = await fetch(detail.lrc_url);
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
          ? (t.status === "subtitle_pending"
              ? `<button class="btn btn-mini task-retry-btn" data-task-id="${t.task_id}" type="button" title="音频已生成，重试字幕拉取">🔁 重试字幕</button>`
              : `<button class="btn btn-mini task-retry-btn" data-task-id="${t.task_id}" type="button" title="后端会自动从失败阶段续跑">↻ 重试</button>`)
          : "";
        const provider = t.provider || "minimax";
        const providerBadge = provider === "edge"
          ? `<span class="provider-badge provider-edge" title="方案二：edge-tts + ffmpeg">edge</span>`
          : `<span class="provider-badge provider-minimax" title="方案一：M3 + MiniMax speech-2.8-hd">minimax</span>`;
        const deleteTitle = t.status === "done"
          ? "删除任务（含最终 mp3 + 字幕）"
          : "删除任务（含所有中间产物）";
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
    case "error":
    case "failed_retryable": return "badge-err";
    case "subtitle_pending": return "badge-warn";
    // 进行中（带 pulse 动画）
    case "normalizing":
    case "splitting":
    case "converting": return "badge-processing";
    // 决策点 / 待用户操作
    case "draft": return "badge-draft";
    case "normalized":
    case "splitted": return "badge-ready";
    case "ready_to_split":
    case "ready_to_convert": return "badge-pending";
    default: return "badge-pending";
  }
}

function statusLabel(status) {
  switch (status) {
    case "draft": return "草稿";
    case "normalizing": return "标准化中";
    case "ready_to_split": return "待拆分";
    case "splitting": return "拆分中";
    case "ready_to_convert": return "待转换";
    case "converting": return "转换中";
    case "subtitle_pending": return "字幕待重试";
    case "done": return "已完成";
    case "error": return "失败";
    case "failed_retryable": return "失败·可重试";
    default: return status || "未知";
  }
}

// 触发后端重试任务；成功后刷新当前列表/详情。
// 后端阶段感知：error/failed_retryable 从失败阶段续跑；subtitle_pending 仅重试字幕。
async function retryTask(taskId) {
  // subtitle_pending 状态语义明确（"重试字幕拉取"），不弹 confirm 兜底
  const rec = await _fetchTask(taskId);
  if (!rec) return;
  if (!["error", "failed_retryable", "subtitle_pending"].includes(rec.status)) return;
  if (rec.status !== "subtitle_pending") {
    if (!confirm("确认重试？后端会自动从失败阶段续跑。")) return;
  }
  try {
    const resp = await api(`/api/tasks/${taskId}/retry`, { method: "POST" });
    setUploadStatus("✅ " + resp.message, "success");
    if (currentPath() === "/upload") renderTaskList();
    if (currentPath() === `/task/${taskId}`) renderTaskDetail(taskId);
  } catch (e) {
    setUploadStatus("❌ 重试失败: " + e.message, "error");
  }
}

// 触发后端删除任务；成功后刷新列表/详情。
// v4：所有产物都在 outputs/<yyyymmdd>/<task_id>/ 下，统一 rmtree 删除。
//   - status="done"：弹模态要求输入"确认删除"四个字（防误删）
//   - 其他状态：弹模态二次确认（"我再想想" / "确认删除"两按钮）
async function deleteTask(taskId, filename, status) {
  const isDone = status === "done";
  const ok = await showDeleteDialog(taskId, filename, isDone);
  if (!ok) return;
  try {
    const resp = await api(`/api/tasks/${taskId}`, { method: "DELETE" });
    const removed = resp.removed_files && resp.removed_files.existed;
    setUploadStatus(
      `✅ 已删除任务 ${taskId.slice(0, 8)}${removed ? "（task_dir 已清理）" : "（目录不存在）"}`,
      "success",
    );
    if (currentPath() === "/upload") renderTaskList();
    if (currentPath() === `/task/${taskId}`) {
      location.hash = "#/upload";
    }
    if (currentPath() === "/") renderListen();
  } catch (e) {
    setUploadStatus("❌ 删除失败: " + e.message, "error");
  }
}

// 弹出删除确认对话框。返回 Promise<boolean>。
//   - isDone=true：要求输入"确认删除"四个字才启用确认按钮
//   - isDone=false：直接显示「确认删除」按钮
function showDeleteDialog(taskId, filename, isDone) {
  return new Promise((resolve) => {
    const overlay = $("deleteDialog");
    const titleEl = $("deleteDialogTitle");
    const msgEl = $("deleteDialogMessage");
    const inputArea = $("deleteDialogInputArea");
    const inputEl = $("deleteDialogInput");
    const statusEl = $("deleteDialogStatus");
    const confirmBtn = $("deleteDialogConfirmBtn");
    const cancelBtn = $("deleteDialogCancelBtn");
    const closeBtn = $("deleteDialogCloseBtn");

    titleEl.textContent = isDone ? "⚠️ 确认删除已完成任务" : "确认删除任务";
    const shortId = taskId.slice(0, 8);
    if (isDone) {
      msgEl.innerHTML = `此任务 <code>${escapeHtml(shortId)}…</code>（${escapeHtml(filename || "")}）已成功生成 MP3。\n\n` +
        "删除会清理：任务记录、听文档条目、task_dir 内所有产物（md/mp3/SRT/LRC）。\n" +
        "为防止误删，请输入 <strong>确认删除</strong> 四个字以继续：";
      inputArea.hidden = false;
      inputEl.value = "";
      confirmBtn.disabled = true;  // done：必须输入"确认删除"才启用
    } else {
      msgEl.textContent = `确认删除任务「${filename || shortId + "…"}」？所有产物将一并清除。`;
      inputArea.hidden = true;
      confirmBtn.disabled = false;  // 非 done：直接可点
    }
    statusEl.textContent = "";
    overlay.hidden = false;

    const onInputChange = () => {
      if (isDone) {
        const ok = inputEl.value.trim() === "确认删除";
        confirmBtn.disabled = !ok;
        statusEl.textContent = ok ? "✅ 已输入正确" : `（已输入 ${inputEl.value.length} / 4 个字）`;
      }
    };
    if (isDone) {
      inputEl.addEventListener("input", onInputChange);
    }

    const cleanup = () => {
      overlay.hidden = true;
      inputEl.removeEventListener("input", onInputChange);
      confirmBtn.onclick = null;
      cancelBtn.onclick = null;
      closeBtn.onclick = null;
    };
    confirmBtn.onclick = () => { cleanup(); resolve(true); };
    cancelBtn.onclick = () => { cleanup(); resolve(false); };
    closeBtn.onclick = () => { cleanup(); resolve(false); };

    // 自动聚焦输入框
    if (isDone) {
      setTimeout(() => inputEl.focus(), 50);
    }
  });
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

  // 如果任务仍在进行中（异步 M3 标准化/拆分/转换），启动轮询
  const record = await _fetchTask(taskId);
  const inFlight = new Set(["pending", "processing", "normalizing", "splitting", "converting"]);
  if (record && inFlight.has(record.status)) {
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
  const kindMap = {
    error: "error", failed_retryable: "error", subtitle_pending: "warning",
    done: "success",
  };
  msgEl.dataset.kind = kindMap[record.status] || "info";

  // 错误展示
  const errEl = $("taskDetailError");
  if ((record.status === "error" || record.status === "failed_retryable") && record.error) {
    errEl.hidden = false;
    errEl.textContent = record.error;
  } else {
    errEl.hidden = true;
  }

  // 完成后显示播放按钮；可重试时显示重试按钮（subtitle_pending 改文案）
  const doneEl = $("taskDetailDone");
  const retryBtn = $("taskDetailRetryBtn");
  // v5 后 audio_id 已合并到 task_id；done 状态统一通过 task_id 跳转播放
  if (record.status === "done" && record.task_id) {
    doneEl.hidden = false;
    $("taskDetailPlayBtn").href = `#/play/${record.task_id}`;
  } else {
    doneEl.hidden = true;
  }
  // 后端 TASK_RETRYABLE_STATUSES = error / failed_retryable / subtitle_pending
  if (record.status === "failed_retryable"
      || record.status === "error"
      || record.status === "subtitle_pending") {
    retryBtn.hidden = false;
    retryBtn.dataset.taskId = taskId;
    retryBtn.onclick = () => retryTask(taskId);
    if (record.status === "subtitle_pending") {
      retryBtn.textContent = "🔁 重试字幕拉取";
    } else if (record.status === "error") {
      retryBtn.textContent = "↻ 重试（按错误类型续跑）";
    } else {
      retryBtn.textContent = "↻ 重试（从失败阶段续跑）";
    }
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
  // 删除提示（v4：所有状态一律 rmtree 整个 task_dir，无"保留"路径）
  const deleteHint = $("taskDetailDeleteHint");
  if (deleteHint) {
    const isRunning = ["normalizing", "splitting", "converting"].includes(record.status);
    if (isRunning) {
      deleteHint.textContent = "⚠ 任务仍在处理中。删除会立刻停止并清空 task_dir 下所有产物（md / mp3 / SRT / LRC）。";
    } else if (record.status === "done") {
      deleteHint.textContent = "🗑 已完成的任务：删除会清空 task_dir 下所有产物（含最终 mp3 + 字幕）。该操作不可撤销。";
    } else if (record.status === "subtitle_pending") {
      deleteHint.textContent = "🎤 字幕待重试：删除会清空 task_dir（含已生成的音频 + 字幕尝试文件）。";
    } else {
      deleteHint.textContent = "🗑 删除会清空 task_dir 下所有产物（含中间 md / mp3 / SRT / LRC）。该操作不可撤销。";
    }
  }

  // 分步交互式操作面板
  renderTaskStepPanel(record, taskId);
}

// ---- 分步交互式操作面板（v3） ----------------------------------------

const TASK_STEP_PANEL_PRESETS_KEY = "txt2tts.splitPresets";

async function renderTaskStepPanel(record, taskId) {
  const panel = $("taskStepPanel");
  if (!panel) return;
  // 终态 / 进行中 → 隐藏面板（已通过重试/删除按钮交互）
  const terminal = new Set(["done", "error", "failed_retryable"]);
  const inFlight = new Set(["normalizing", "splitting", "local_cleaning", "converting"]);
  if (terminal.has(record.status) || inFlight.has(record.status)) {
    panel.hidden = true;
    panel.innerHTML = "";
    return;
  }

  panel.hidden = false;
  switch (record.status) {
    case "draft":
      renderDraftPanel(panel, record, taskId);
      break;
    case "ready_to_split":
      renderSplitOptionPanel(panel, record, taskId);
      break;
    case "splitted":
      renderSplittedPanel(panel, record, taskId);
      break;
    case "local_cleaned":
      renderLocalCleanedPanel(panel, record, taskId);
      break;
    case "ready_to_convert":
      renderReadyToConvertPanel(panel, record, taskId);
      break;
    case "subtitle_pending":
      renderSubtitlePendingPanel(panel, record, taskId);
      break;
    default:
      panel.hidden = true;
  }
}

function renderSubtitlePendingPanel(panel, record, taskId) {
  panel.innerHTML = `
    <div class="step-panel-inner">
      <h3>🎤 字幕待重试</h3>
      <p class="hint-inline">音频已成功生成（task_dir/<code>${escapeHtml(record.task_id)}</code>.mp3），但 MiniMax 字幕文件（subtitle_file）拉取失败。音频仍可听。</p>
      <p class="hint-inline warn" style="margin-top:8px;">失败原因：<code>${escapeHtml(record.error || "未知")}</code></p>
      <div class="row" style="gap:8px; margin-top:14px;">
        <button class="btn btn-primary" id="btnRetrySubtitle" type="button">🔁 重试字幕拉取</button>
        <a href="#/play/${escapeHtml(record.task_id)}" class="btn btn-ghost">▶ 先听音频</a>
      </div>
    </div>
  `;
  panel.querySelector("#btnRetrySubtitle").onclick = () => retryTask(taskId);
}

function renderDraftPanel(panel, record, taskId) {
  const localLen = record.local_clean_length || 0;
  panel.innerHTML = `
    <div class="step-panel-inner">
      <h3>✨ 标准化</h3>
      <p class="hint-inline">原文已就绪（${localLen} 字符）。是否用 M3 处理？</p>
      <details class="hint" style="margin-top:6px;">
        <summary>查看原文（前 300 字）</summary>
        <pre class="text-preview" id="localCleanPreview"></pre>
      </details>
      <div class="normalize-prompt-area" style="margin-top:14px;">
        <label class="hint-inline" style="font-weight:500;">
          M3 标准化提示词（选择预设或自定义）：
        </label>
        <div class="preset-row" id="normPresetsRow"
             style="margin-top:6px; display:flex; gap:6px; flex-wrap:wrap;"></div>
        <textarea id="normalizePromptInput" class="split-prompt-textarea"
                  rows="6" placeholder="选择上方预设按钮快速填充，或直接输入自定义提示词…"></textarea>
      </div>
      <div class="row" style="gap:8px; margin-top:14px;">
        <button class="btn btn-primary" id="btnStartNormalize" type="button">✨ 标准化</button>
        <button class="btn btn-ghost" id="btnSkipNormalize" type="button">⏭ 跳过</button>
      </div>
    </div>
  `;

  // 并行拉详情 + presets
  Promise.all([
    api(`/api/tasks/${taskId}`).then((full) => {
      const pre = panel.querySelector("#localCleanPreview");
      // v6：详情接口 draft 时直接返回 local_clean_text（原文）
      const txt = full.local_clean_text || "";
      if (pre) pre.textContent = txt.slice(0, 300) || "(原文为空)";
    }),
    api("/api/normalize-presets").then((presets) => {
      const row = panel.querySelector("#normPresetsRow");
      if (!row) return;
      row.innerHTML = (presets || []).map((p) =>
        `<button class="btn btn-ghost btn-mini preset-btn" type="button" `
        + `data-preset-id="${p.id}">📝 ${escapeHtml(p.name)}</button>`
      ).join("")
        + `<button class="btn btn-ghost btn-mini" id="btnClearNormPrompt" type="button">清空</button>`;

      // 默认填 "default" preset 的 prompt
      const def = (presets || []).find((p) => p.id === "default") || (presets || [])[0];
      if (def) {
        const ta = panel.querySelector("#normalizePromptInput");
        if (ta) ta.value = def.prompt || "";
      }

      row.querySelectorAll(".preset-btn").forEach((btn) => {
        btn.onclick = () => {
          const pid = btn.dataset.presetId;
          const p = (presets || []).find((x) => x.id === pid);
          const ta = panel.querySelector("#normalizePromptInput");
          if (p && ta) ta.value = p.prompt || "";
        };
      });
      const clear = panel.querySelector("#btnClearNormPrompt");
      if (clear) clear.onclick = () => {
        const ta = panel.querySelector("#normalizePromptInput");
        if (ta) ta.value = "";
      };
    }),
  ]).catch((e) => console.warn("draft panel load failed:", e));

  panel.querySelector("#btnStartNormalize").onclick = () => {
    const ta = panel.querySelector("#normalizePromptInput");
    const prompt = ta ? ta.value.trim() : "";
    triggerAction(
      taskId, "normalize", "已触发 M3 标准化",
      prompt ? { prompt } : null,
    );
  };
  panel.querySelector("#btnSkipNormalize").onclick = () => triggerAction(taskId, "skip-normalize", "已跳过标准化");
}

async function renderSplitOptionPanel(panel, record, taskId) {
  // 拉取内置 preset
  let presets = [];
  try {
    presets = await api("/api/split-presets");
  } catch (e) {
    presets = [];
  }
  const normLen = record.normalized_length || 0;
  const promptPresets = presets.map((p) =>
    `<button class="btn btn-ghost btn-mini preset-btn" type="button" data-preset-id="${p.id}">📝 ${escapeHtml(p.name)}</button>`
  ).join("");

  panel.innerHTML = `
    <div class="step-panel-inner">
      <h3>✂️ 拆分</h3>
      <p class="hint-inline">标准化完成（${normLen} 字符）。按提示词拆分为子文档（分块 TTS 合成）。</p>
      <details class="hint" style="margin-top:6px;">
        <summary>查看标准化结果（前 400 字）</summary>
        <pre class="text-preview"></pre>
      </details>
      <div class="split-prompt-area" style="margin-top:14px;">
        <label class="hint-inline" style="font-weight:500;">拆分提示词（选择预设或自定义）：</label>
        <div class="preset-row" style="margin-top:6px; display:flex; gap:6px; flex-wrap:wrap;">
          ${promptPresets}
          <button class="btn btn-ghost btn-mini" id="btnClearPrompt" type="button">清空</button>
        </div>
        <textarea id="splitPromptInput" class="split-prompt-textarea" rows="6"
                  placeholder="选择上方预设按钮快速填充，或直接输入自定义提示词…"></textarea>
      </div>
      <div class="row" style="gap:8px; margin-top:14px;">
        <button class="btn btn-primary" id="btnStartSplit" type="button">✂️ 拆分</button>
        <button class="btn btn-ghost" id="btnSkipSplit" type="button">⏭ 跳过</button>
      </div>
    </div>
  `;
  // 拉取全文显示
  api(`/api/tasks/${taskId}`).then((full) => {
    const pre = panel.querySelector(".text-preview");
    if (pre) pre.textContent = (full.normalized_text || "").slice(0, 400);
  }).catch(() => {});

  // preset 按钮：把 prompt 填到 textarea
  panel.querySelectorAll(".preset-btn").forEach((btn) => {
    btn.onclick = () => {
      const pid = btn.dataset.presetId;
      const p = presets.find((x) => x.id === pid);
      if (p) panel.querySelector("#splitPromptInput").value = p.prompt;
    };
  });
  panel.querySelector("#btnClearPrompt").onclick = () => {
    panel.querySelector("#splitPromptInput").value = "";
  };
  panel.querySelector("#btnStartSplit").onclick = async () => {
    const prompt = panel.querySelector("#splitPromptInput").value.trim();
    if (!prompt) {
      alert("请填写拆分提示词（或选择上方预设）");
      return;
    }
    await triggerAction(taskId, "split", "已触发 M3 拆分", { prompt });
  };
  panel.querySelector("#btnSkipSplit").onclick = () => triggerAction(taskId, "skip-split", "已跳过拆分");
}

function renderSplittedPanel(panel, record, taskId) {
  // 在重渲染前记录当前展开的 details 索引（避免 2 秒轮询重置展开状态）
  const previouslyOpen = new Set();
  if (panel._renderedSplitted) {
    panel.querySelectorAll("details.chunk-item").forEach((d) => {
      if (d.open) previouslyOpen.add(d.dataset.idx);
    });
  }

  const chunks = record.split_chunks || [];
  const chunkList = chunks.map((c, i) => {
    const preview = (c || "").slice(0, 200);
    const openAttr = previouslyOpen.has(String(i)) ? " open" : "";
    return `
      <details class="chunk-item" data-idx="${i}"${openAttr}>
        <summary>
          <strong>子文档 #${i + 1}</strong>
          <span class="hint-inline">（${(c || "").length} 字符）</span>
        </summary>
        <textarea class="chunk-textarea" data-idx="${i}" rows="8">${escapeHtml(c || "")}</textarea>
      </details>
    `;
  }).join("");

  panel.innerHTML = `
    <div class="step-panel-inner">
      <h3>✂️ 确认拆分</h3>
      <p class="hint-inline">已拆分为 <strong>${chunks.length}</strong> 个子文档。可编辑后保存，或重新拆分/跳过。</p>
      <div class="chunk-list" style="margin-top:12px;">${chunkList || '<p class="hint-inline">（无子文档）</p>'}</div>
      <div class="row" style="gap:8px; margin-top:14px; flex-wrap:wrap;">
        <button class="btn btn-primary" id="btnConfirmSplit" type="button">✅ 确认拆分</button>
        <button class="btn btn-secondary" id="btnStartLocalClean" type="button">🧹 本地清洗</button>
        <button class="btn btn-ghost" id="btnResplit" type="button">🔄 重新拆分</button>
        <button class="btn btn-ghost" id="btnSkipSplit2" type="button">⏭ 跳过</button>
      </div>
    </div>
  `;
  panel._renderedSplitted = true;
  panel.querySelector("#btnConfirmSplit").onclick = async () => {
    const edited = Array.from(panel.querySelectorAll(".chunk-textarea"))
      .map((ta) => ta.value.trim())
      .filter((s) => s.length > 0);
    await triggerAction(taskId, "confirm-split", "子文档已确认", { chunks: edited });
  };
  // v6：本地清洗入口：跳转到 _local_clean_options_view 选择清洗项后启动
  panel.querySelector("#btnStartLocalClean").onclick = () =>
    renderLocalCleanOptionsView(panel, record, taskId);
  panel.querySelector("#btnResplit").onclick = () => triggerAction(taskId, "skip-split", "已放弃拆分，进入待转换");
  panel.querySelector("#btnSkipSplit2").onclick = () => triggerAction(taskId, "skip-split", "已放弃拆分，进入待转换");
}

// ---- v6 本地清洗：选项选择视图 + 完成确认视图 --------------------------

async function renderLocalCleanOptionsView(panel, record, taskId) {
  // 拉取清洗项元数据
  let opts = [];
  try {
    opts = await api("/api/clean-options");
  } catch (e) {
    opts = [];
  }
  // 默认勾选：从 record.clean_options 优先，其次用 default 项
  const previouslySelected = new Set(
    (record.clean_options && record.clean_options.length)
      ? record.clean_options
      : opts.filter((o) => o.default).map((o) => o.id)
  );

  const checkboxList = opts.map((o) => {
    const checked = previouslySelected.has(o.id) ? "checked" : "";
    return `
      <label class="clean-option">
        <input type="checkbox" data-cid="${escapeHtml(o.id)}" ${checked}>
        <span class="clean-option-label">${escapeHtml(o.label)}</span>
        ${o.description ? `<span class="clean-option-desc">${escapeHtml(o.description)}</span>` : ""}
      </label>
    `;
  }).join("");

  const chunkCount = (record.split_chunks || []).length;
  const totalChars = (record.split_chunks || [])
    .reduce((s, c) => s + (c ? c.length : 0), 0);

  panel.innerHTML = `
    <div class="step-panel-inner">
      <h3>🧹 本地清洗</h3>
      <p class="hint-inline">
        选中要清洗的项目，应用到 <strong>${chunkCount}</strong> 个子文档（共 ${totalChars} 字符）。
        清洗后直接覆写 <code>split_<N>.md</code>。
      </p>
      <div class="clean-options" style="margin-top:10px;">${checkboxList || '<p class="hint-inline">（无清洗项）</p>'}</div>
      <div class="row" style="gap:8px; margin-top:14px; flex-wrap:wrap;">
        <button class="btn btn-primary" id="btnConfirmLocalClean" type="button">🧹 开始清洗</button>
        <button class="btn btn-ghost" id="btnSkipLocalCleanFromOpts" type="button">⏭ 跳过清洗</button>
        <button class="btn btn-ghost" id="btnBackToSplitted" type="button">‹ 返回</button>
      </div>
    </div>
  `;

  panel.querySelector("#btnConfirmLocalClean").onclick = async () => {
    const selected = Array.from(panel.querySelectorAll(".clean-option input:checked"))
      .map((cb) => cb.dataset.cid);
    if (selected.length === 0) {
      alert("请至少选择一项（或点「跳过清洗」）");
      return;
    }
    await triggerAction(taskId, "local-clean", "本地清洗已启动", { options: selected });
  };
  panel.querySelector("#btnSkipLocalCleanFromOpts").onclick = () =>
    triggerAction(taskId, "skip-local-clean", "已跳过本地清洗");
  panel.querySelector("#btnBackToSplitted").onclick = () => renderSplittedPanel(panel, record, taskId);
}

async function renderLocalCleanedPanel(panel, record, taskId) {
  const chunks = record.split_chunks || [];
  const opts = record.clean_options || [];
  const totalChars = chunks.reduce((s, c) => s + (c ? c.length : 0), 0);

  // 把清洗项 id → label 映射（用缓存的 clean-options，如无则用 id）
  let labelMap = {};
  try {
    const optsMeta = await api("/api/clean-options");
    optsMeta.forEach((o) => { labelMap[o.id] = o.label; });
  } catch (e) { /* ignore */ }
  const optsHtml = opts.length
    ? opts.map((cid) =>
        `<span class="clean-tag">✓ ${escapeHtml(labelMap[cid] || cid)}</span>`
      ).join("")
    : '<span class="hint-inline">（无）</span>';

  panel.innerHTML = `
    <div class="step-panel-inner">
      <h3>🧹 本地清洗完成</h3>
      <p class="hint-inline">
        已应用 <strong>${opts.length}</strong> 项清洗规则，覆盖 <strong>${chunks.length}</strong> 个子文档
        （共 ${totalChars} 字符）。
      </p>
      <div class="clean-applied" style="margin-top:8px;">${optsHtml}</div>
      <ul class="convert-summary" style="margin-top:12px;">
        <li>清洗后子文档：<strong>${chunks.length}</strong> 个</li>
        <li>语音：<strong>${escapeHtml(record.voice_id || "默认")}</strong></li>
      </ul>
      <div class="row" style="gap:8px; margin-top:14px; flex-wrap:wrap;">
        <button class="btn btn-primary" id="btnStartConvertFromCleaned" type="button">🚀 确认去转换</button>
        <button class="btn btn-secondary" id="btnReclean" type="button">↩ 重新清洗</button>
        <button class="btn btn-ghost" id="btnSkipLocalCleanFromCleaned" type="button">⏭ 跳过清洗</button>
      </div>
    </div>
  `;

  panel.querySelector("#btnStartConvertFromCleaned").onclick = async () => {
    // v6：local_cleaned → confirm-split(chunks=current) → ready_to_convert
    await triggerAction(taskId, "confirm-split", "已确认清洗结果", { chunks });
  };
  panel.querySelector("#btnReclean").onclick = () =>
    renderLocalCleanOptionsView(panel, record, taskId);
  panel.querySelector("#btnSkipLocalCleanFromCleaned").onclick = () =>
    triggerAction(taskId, "skip-local-clean", "已跳过本地清洗");
}

function renderReadyToConvertPanel(panel, record, taskId) {
  const normLen = record.normalized_length || 0;
  const chunkInfo = record.split_chunks && record.split_chunks.length
    ? `已确认 ${record.split_chunks.length} 个子文档`
    : "未拆分（自动切分）";
  panel.innerHTML = `
    <div class="step-panel-inner">
      <h3>🚀 转换</h3>
      <p class="hint-inline">确认信息后启动 TTS 合成。</p>
      <ul class="convert-summary" style="margin-top:10px;">
        <li>标准化文本：<strong>${normLen}</strong> 字符</li>
        <li>子文档：<strong>${chunkInfo}</strong></li>
        <li>语音：<strong>${escapeHtml(record.voice_id || "默认")}</strong></li>
      </ul>
      <div class="row" style="gap:8px; margin-top:14px;">
        <button class="btn btn-primary" id="btnStartConvert" type="button">🚀 转换</button>
        <a href="#/upload" class="btn btn-ghost">‹ 返回任务列表</a>
      </div>
    </div>
  `;
  panel.querySelector("#btnStartConvert").onclick = () => triggerAction(taskId, "convert", "已启动 TTS 转换");
}

async function triggerAction(taskId, action, successMsg, body) {
  try {
    const opts = { method: "POST" };
    if (body) {
      opts.headers = { "Content-Type": "application/json" };
      opts.body = JSON.stringify(body);
    }
    await api(`/api/tasks/${taskId}/${action}`, opts);
    // 立即刷新详情 + 启动轮询（异步操作后用户无需手动刷新）
    setTimeout(async () => {
      await _updateTaskDetail(taskId);
      const rec = await _fetchTask(taskId);
      if (rec && ["normalizing", "splitting", "converting"].includes(rec.status)) {
        startTaskPoll(taskId);
      }
    }, 200);
  } catch (e) {
    alert(`操作失败：${e.message || e}`);
  }
}

// 任务进度条 3 阶段（v5 起去除本地 markdown_clean）。
// stage key 与后端 task_manager 写入的 current_stage 字段对齐。
// minimax / edge 走完全相同的 3 步（区别只在 tts_synthesize 阶段的内部实现）。
// v6：步骤条不变，本地清洗是 splitted 与 tts_synthesize 之间的"分支步"，
// 进度条权重为 0.35→0.40（见 pipeline.STAGE_WEIGHTS["local_clean"]），
// 但前端步骤条不渲染 local_clean 这一格（避免与 ready_to_convert 状态混淆）。
const STAGES_BY_PROVIDER = {
  minimax: [
    { stage: "llm_normalize",  label: "M3 标准化" },
    { stage: "tts_synthesize", label: "TTS 合成" },
    { stage: "audio_save",     label: "保存产物" },
  ],
  edge: [
    { stage: "llm_normalize",  label: "M3 标准化" },
    { stage: "tts_synthesize", label: "TTS 合成" },
    { stage: "audio_save",     label: "保存产物" },
  ],
};
// 兜底（provider 未知 / 老库）：旧的 4 步
const STAGES_LEGACY = [
  { stage: "llm_normalize",  label: "M3 标准化" },
  { stage: "tts_synthesize", label: "语音合成" },
  { stage: "audio_save",     label: "保存落盘" },
];

const HINT_BY_PROVIDER = {
  minimax: "📋 草稿 → ✨ M3 标准化 → ✂️ 拆分 → 🧹 本地清洗 → 🚀 TTS 合成（含 SRT 字幕）",
  edge:    "📋 草稿 → ✨ M3 标准化 → ✂️ 拆分 → 🧹 本地清洗 → 🚀 TTS 合成",
};
const HINT_DEFAULT = "📋 草稿 → ✨ 标准化 → ✂️ 拆分 → 🧹 清洗 → 🚀 转换";

function stagesFor(record) {
  const p = (record && record.provider) || "minimax";
  return STAGES_BY_PROVIDER[p] || STAGES_LEGACY;
}

function renderTaskSteps(record) {
  const container = $("taskDetailSteps");
  const steps = stagesFor(record);
  const idx = steps.findIndex((s) => s.stage === record.current_stage);
  const terminal = new Set(["done", "error", "failed_retryable", "subtitle_pending"]);

  let html = "";
  steps.forEach((s, i) => {
    let cls = "";
    if (record.status === "done") {
      cls = "done";
    } else if (terminal.has(record.status)) {
      // error / failed_retryable：当前 stage 标 error；subtitle_pending：audio_save 阶段标 warn
      if (record.status === "subtitle_pending" && s.stage === "audio_save") {
        cls = "warn";
      } else if (i === idx) {
        cls = "error";
      } else if (idx > 0 && i < idx) {
        cls = "done";
      }
    } else if (idx >= 0 && i < idx) {
      cls = "done";
    } else if (i === idx) {
      cls = "active";
    }
    html += `<div class="step ${cls}" data-stage="${s.stage}"><div class="step-icon">${i + 1}</div><div class="step-label">${s.label}</div></div>`;
    if (i < steps.length - 1) {
      let barCls = "";
      if (cls === "done") barCls = "done";
      else if (cls === "active") barCls = "active";
      else if (cls === "warn") barCls = "warn";
      html += `<div class="step-bar ${barCls}"></div>`;
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
    if (record.status === "done" || record.status === "error" ||
        record.status === "failed_retryable" || record.status === "subtitle_pending") {
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
    state.activeProvider = data.tts_provider || "minimax";
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
