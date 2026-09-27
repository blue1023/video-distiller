/* 前端主逻辑：单条/批量提交、本地文件上传、SSE 进度、笔记与导图展示、导出 */
(function () {
  "use strict";

  var state = {
    tasks: [],
    notes: [],
    uploads: [],
    activeTaskId: null,
    activeNoteId: null,
    activeNote: null,
    tree: null,
    tab: "mindmap",
    mode: "single",
    sources: {},          // taskId -> EventSource
    pollTimer: null,
    settings: null
  };

  var el = {};
  var STAGE_LABEL = {
    queued: "排队中",
    parsing: "解析链接",
    info: "获取视频信息",
    subtitle: "获取字幕",
    cache: "命中缓存",
    asr: "语音转写",
    structuring: "AI 整理笔记",
    rendering: "生成 Markdown",
    mindmap: "生成导图",
    saving: "保存产物",
    done: "完成",
    failed: "失败",
    cancelled: "已取消",
    interrupted: "已中断"
  };
  var STATUS_META = {
    pending: { label: "排队中", cls: "pill-muted" },
    running: { label: "进行中", cls: "pill-warn" },
    success: { label: "已完成", cls: "pill-ok" },
    failed: { label: "失败", cls: "pill-err" },
    cancelled: { label: "已取消", cls: "pill-muted" }
  };
  var SOURCE_LABEL = {
    subtitle_manual: "人工字幕",
    subtitle_ai: "AI 字幕",
    asr_openai: "云端转写",
    "asr_faster-whisper": "本地转写",
    asr_local: "本地转写",
    cache: "缓存"
  };

  /* ---------------- 工具 ---------------- */

  function $(id) { return document.getElementById(id); }

  function escapeHtml(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function toast(message, kind) {
    var box = el.toast;
    box.textContent = message;
    box.className = "toast" + (kind ? " is-" + kind : "");
    box.hidden = false;
    clearTimeout(box._timer);
    box._timer = setTimeout(function () { box.hidden = true; }, kind === "error" ? 7000 : 3000);
  }

  function formatTime(seconds) {
    seconds = Math.max(0, Math.round(seconds || 0));
    var m = Math.floor(seconds / 60);
    var s = seconds % 60;
    return m + ":" + (s < 10 ? "0" : "") + s;
  }

  function formatDate(ts) {
    if (!ts) return "";
    var d = new Date(ts * 1000);
    var pad = function (n) { return n < 10 ? "0" + n : "" + n; };
    return d.getMonth() + 1 + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }

  function fileBase(title) {
    return (String(title || "note").replace(/[\\/:*?"<>|]/g, "_").trim().slice(0, 60)) || "note";
  }

  function humanSize(bytes) {
    var value = Number(bytes) || 0;
    var units = ["B", "KB", "MB", "GB"];
    var index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
    return value.toFixed(index === 0 ? 0 : 1) + " " + units[index];
  }

  /* ---------------- 健康状态 / 队列 ---------------- */

  async function refreshHealth() {
    try {
      var data = await API.health();
      if (!data.llm_configured) {
        el.health.textContent = "⚠️ 未配置大模型";
        el.health.className = "pill pill-warn";
        el.health.title = "点击右上角「设置」填写大模型 API";
      } else {
        el.health.textContent = "● 服务正常 · " + (data.llm_model || "模型就绪");
        el.health.className = "pill pill-ok";
      }
      try {
        var queue = await API.queue();
        if (queue && queue.limit) {
          el.health.title = "并发上限 " + queue.limit + " · 运行中 " + queue.running + " · 排队 " + queue.waiting;
          if (queue.running || queue.waiting) {
            el.health.textContent = "● 运行 " + queue.running + " / 排队 " + queue.waiting;
            el.health.className = "pill pill-warn";
          }
        }
      } catch (err) { /* 队列接口失败不影响健康状态 */ }
    } catch (err) {
      el.health.textContent = "✕ 服务未连接";
      el.health.className = "pill pill-err";
      el.health.title = err.message;
    }
  }

  /* ---------------- 任务列表 ---------------- */

  function taskItemHtml(task) {
    var status = STATUS_META[task.status] || { label: task.status, cls: "pill-muted" };
    var title = task.title || task.url || "（解析中）";
    var progress = Math.round((task.progress || 0) * 100);
    var isActive = task.id === state.activeTaskId ? " is-active" : "";
    var isDone = task.status === "success" ? " is-success" : (task.status === "failed" ? " is-failed" : "");

    var badges = [];
    if (task.batch_id) badges.push('<span class="pill pill-muted">批次</span>');
    if (task.from_cache) badges.push('<span class="pill pill-ok">缓存</span>');
    if (task.refresh) badges.push('<span class="pill pill-warn">重抓</span>');
    if (task.transcript_src) {
      badges.push('<span class="pill pill-muted">' + (SOURCE_LABEL[task.transcript_src] || task.transcript_src) + "</span>");
    }

    var sub = [];
    if (task.uploader) sub.push(escapeHtml(task.uploader));
    if (task.duration) sub.push(formatTime(task.duration));
    if (task.transcript_chars) sub.push(task.transcript_chars + " 字");
    sub.push(formatDate(task.created_at));

    var message = "";
    if (task.status === "failed" && task.error) message = task.error;
    else if (task.status === "running" || task.status === "pending") {
      message = task.message || ((STAGE_LABEL[task.stage] || task.stage || "") );
    }

    var actions = [];
    if (task.status === "running" || task.status === "pending") {
      actions.push('<button class="btn btn-mini" data-action="cancel" data-id="' + task.id + '">取消</button>');
    }
    if (task.status === "failed" || task.status === "success") {
      actions.push('<button class="btn btn-mini" data-action="regen" data-id="' + task.id + '" title="忽略缓存重新生成">重生成</button>');
    }
    if (task.status !== "running") {
      actions.push('<button class="btn btn-mini btn-danger" data-action="del" data-id="' + task.id + '">删除</button>');
    }

    return (
      '<div class="task-item' + isActive + isDone + '" data-task="' + task.id + '">' +
        '<div class="task-top">' +
          '<span class="task-title">' + escapeHtml(title) + "</span>" +
          '<span class="pill ' + status.cls + '">' + status.label + "</span>" +
        "</div>" +
        (badges.length ? '<div class="task-badges">' + badges.join("") + "</div>" : "") +
        '<div class="task-sub">' + sub.join(" · ") + "</div>" +
        (message ? '<div class="task-sub">' + escapeHtml(String(message).slice(0, 180)) + "</div>" : "") +
        '<div class="mini-progress"><i style="width:' + progress + '%"></i></div>' +
        (actions.length ? '<div class="task-actions">' + actions.join("") + "</div>" : "") +
      "</div>"
    );
  }

  function renderTasks() {
    el.taskCount.textContent = String(state.tasks.length);
    if (!state.tasks.length) {
      el.taskList.innerHTML = '<p class="empty">还没有任务，先粘一条链接试试。</p>';
      return;
    }
    el.taskList.innerHTML = state.tasks.map(taskItemHtml).join("");
    Array.prototype.forEach.call(el.taskList.querySelectorAll("[data-task]"), function (node) {
      node.addEventListener("click", function (event) {
        var action = event.target && event.target.getAttribute && event.target.getAttribute("data-action");
        if (action) {
          event.stopPropagation();
          handleAction(action, event.target.getAttribute("data-id"));
          return;
        }
        selectTask(node.getAttribute("data-task"));
      });
    });
  }

  async function handleAction(action, taskId) {
    if (!taskId) return;
    if (action === "cancel") {
      try { await API.cancelTask(taskId); toast("已请求取消"); } catch (err) { toast(err.message, "error"); }
      return;
    }
    if (action === "del") {
      try {
        await API.deleteTask(taskId);
        state.tasks = state.tasks.filter(function (t) { return t.id !== taskId; });
        renderTasks();
        toast("任务已删除");
      } catch (err) { toast(err.message, "error"); }
      return;
    }
    if (action === "regen") {
      try {
        var task = await API.refreshTask(taskId, false);
        upsertTask(task);
        state.activeTaskId = task.id;
        renderTasks();
        showProgressPanel(task);
        subscribe(task.id);
        toast("已提交重新生成（忽略缓存）");
      } catch (err) { toast(err.message, "error"); }
    }
  }

  async function loadTasks() {
    try {
      var data = await API.listTasks(60);
      state.tasks = data.items || [];
      renderTasks();
      state.tasks.forEach(function (task) {
        if (task.status === "running" || task.status === "pending") subscribe(task.id);
      });
    } catch (err) {
      console.error(err);
    }
  }

  function upsertTask(task) {
    if (!task || !task.id) return;
    var index = state.tasks.findIndex(function (t) { return t.id === task.id; });
    if (index >= 0) state.tasks[index] = Object.assign({}, state.tasks[index], task);
    else state.tasks.unshift(task);
    renderTasks();
  }

  /* ---------------- SSE 进度 ---------------- */

  function subscribe(taskId) {
    if (!taskId || state.sources[taskId]) return;
    var source;
    try {
      source = new EventSource("/api/tasks/" + encodeURIComponent(taskId) + "/events");
    } catch (err) {
      startPolling();
      return;
    }
    state.sources[taskId] = source;

    source.onmessage = function (event) {
      var data;
      try { data = JSON.parse(event.data); } catch (err) { return; }
      if (data.replay) return;
      handleProgress(taskId, data);
    };
    source.addEventListener("close", function () {
      closeSource(taskId);
      loadTasks();
      loadNotes();
    });
    source.onerror = function () {
      closeSource(taskId);
      startPolling();
    };
  }

  function closeSource(taskId) {
    var source = state.sources[taskId];
    if (source) {
      source.close();
      delete state.sources[taskId];
    }
  }

  function handleProgress(taskId, data) {
    var patch = {
      id: taskId,
      status: data.status || "running",
      stage: data.stage,
      progress: data.progress,
      message: data.message
    };
    if (data.transcript_source) patch.transcript_src = data.transcript_source;
    if (data.transcript_chars) patch.transcript_chars = data.transcript_chars;
    if (data.title) patch.title = data.title;
    if (data.from_cache) patch.from_cache = 1;
    upsertTask(patch);

    if (taskId === state.activeTaskId) {
      updateProgressUI(patch);
      if (data.status === "success") {
        toast("✅ 笔记生成完成", "ok");
        if (data.note_id) openNote(data.note_id);
        else openTaskNote(taskId);
      } else if (data.status === "failed") {
        renderError(data.error || data.message || "生成失败");
      }
    }
  }

  function updateProgressUI(task) {
    var progress = Math.round((task.progress || 0) * 100);
    el.progressWrap.hidden = false;
    el.progressFill.style.width = progress + "%";
    el.progressPercent.textContent = progress + "%";
    el.progressStage.textContent = STAGE_LABEL[task.stage] || task.stage || "处理中";
    el.progressMessage.textContent = task.message || "";
    if (task.status === "success") {
      setTimeout(function () { el.progressWrap.hidden = true; }, 1500);
    }
  }

  function startPolling() {
    if (state.pollTimer) return;
    state.pollTimer = setInterval(async function () {
      var running = state.tasks.filter(function (t) {
        return t.status === "running" || t.status === "pending";
      });
      if (!running.length) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        return;
      }
      for (var i = 0; i < running.length; i++) {
        try {
          var task = await API.getTask(running[i].id);
          upsertTask(task);
          if (task.id === state.activeTaskId) {
            updateProgressUI(task);
            if (task.status === "success") { clearInterval(state.pollTimer); state.pollTimer = null; loadNotes(); openTaskNote(task.id); }
            if (task.status === "failed") { clearInterval(state.pollTimer); state.pollTimer = null; renderError(task.error); }
          }
        } catch (err) { /* 忽略单次失败 */ }
      }
      refreshHealth();
    }, 2000);
  }

  /* ---------------- 提交 ---------------- */

  function setMode(mode) {
    state.mode = mode;
    Array.prototype.forEach.call(el.inputMode.querySelectorAll(".tab"), function (node) {
      node.classList.toggle("is-active", node.getAttribute("data-mode") === mode);
    });
    el.paneSingle.hidden = mode !== "single";
    el.paneBatch.hidden = mode !== "batch";
    el.btnSubmit.textContent = mode === "batch" ? "批量生成" : "生成笔记";
    el.createHint.textContent = mode === "batch"
      ? "批量任务按并发上限依次执行，可在设置里调整；最多 50 条。"
      : "生成一个 30 分钟视频的笔记通常需要 1-3 分钟。";
  }

  async function onSubmit(event) {
    event.preventDefault();
    var refresh = el.inputRefresh.checked;
    el.btnSubmit.disabled = true;
    var original = el.btnSubmit.textContent;
    el.btnSubmit.textContent = "提交中…";
    try {
      if (state.mode === "batch") {
        var text = el.inputBatch.value.trim();
        if (!text) { toast("请粘贴至少一条链接", "error"); return; }
        var result = await API.createBatch(text, el.inputStyle.value, refresh);
        el.inputBatch.value = "";
        (result.tasks || []).forEach(upsertTask);
        if (result.skipped && result.skipped.length) {
          toast("已提交 " + result.created + " 条，" + result.skipped.length + " 条被跳过", "error");
          console.warn("跳过的链接", result.skipped);
        } else {
          toast("已提交 " + result.created + " 条任务，按并发上限依次执行", "ok");
        }
        if (result.tasks && result.tasks.length) {
          state.activeTaskId = result.tasks[0].id;
          showProgressPanel(result.tasks[0]);
        }
        (result.tasks || []).forEach(function (t) { subscribe(t.id); });
        startPolling();
      } else {
        var url = el.inputUrl.value.trim();
        if (!url) { toast("请先粘贴 B 站视频链接", "error"); return; }
        var task = await API.createTask(url, el.inputStyle.value, refresh);
        el.inputUrl.value = "";
        upsertTask(task);
        state.activeTaskId = task.id;
        renderTasks();
        showProgressPanel(task);
        subscribe(task.id);
        startPolling();
        toast("任务已提交，正在后台生成…");
      }
      refreshHealth();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      el.btnSubmit.disabled = false;
      el.btnSubmit.textContent = original;
    }
  }

  function showProgressPanel(task) {
    el.resultTitle.textContent = task.title || "正在处理…";
    el.resultMeta.textContent = task.url || "";
    el.resultActions.hidden = true;
    el.progressWrap.hidden = false;
    el.progressFill.style.background = "";
    updateProgressUI(task);
    el.markdownBody.innerHTML = '<p class="empty">生成完成后这里会显示 Markdown 笔记。</p>';
  }

  function renderError(message) {
    el.progressWrap.hidden = false;
    el.progressFill.style.width = "100%";
    el.progressFill.style.background = "var(--danger)";
    el.progressStage.textContent = "失败";
    el.progressPercent.textContent = "";
    el.progressMessage.textContent = message || "未知错误";
    el.resultActions.hidden = true;
    el.resultTitle.textContent = "生成失败";
    el.resultMeta.textContent = message || "";
    toast(message || "生成失败", "error");
  }

  /* ---------------- 选择任务 / 打开笔记 ---------------- */

  async function selectTask(taskId) {
    state.activeTaskId = taskId;
    renderTasks();
    var task = state.tasks.find(function (t) { return t.id === taskId; });
    if (!task) return;
    if (task.note_id) { openNote(task.note_id); return; }
    if (task.status === "running" || task.status === "pending") {
      showProgressPanel(task);
      subscribe(taskId);
      return;
    }
    if (task.status === "failed") {
      showProgressPanel(task);
      renderError(task.error || task.message);
      return;
    }
    await openTaskNote(taskId);
  }

  async function openTaskNote(taskId) {
    try {
      var note = await API.getNote("by-task/" + taskId);
      showNote(note);
    } catch (err) {
      toast("笔记还没就绪或已被删除", "error");
    }
  }

  async function openNote(noteId) {
    try {
      var note = await API.getNote(noteId);
      showNote(note);
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function showNote(note) {
    state.activeNote = note;
    state.activeNoteId = note.id;
    state.activeTaskId = note.task_id;
    state.tree = note.tree || null;
    renderTasks();

    el.progressWrap.hidden = true;
    el.progressFill.style.background = "";
    el.resultActions.hidden = false;
    el.resultTitle.textContent = note.title || "笔记";
    var tags = (note.tags || []).slice(0, 6).map(function (t) { return "#" + t; }).join(" ");
    el.resultMeta.textContent =
      (note.uploader ? note.uploader + " · " : "") +
      (note.word_count ? note.word_count + " 字 · " : "") +
      formatDate(note.created_at) + (tags ? " · " + tags : "");

    el.markdownBody.innerHTML = window.MD.render(note.content || "");

    if (window.Mindmap.isAvailable()) {
      el.mindmapEmpty.hidden = true;
      var ok = window.Mindmap.render(state.tree, fileBase(note.title));
      el.mindmapEmpty.hidden = !!ok;
      if (!ok) el.mindmapEmpty.textContent = "该笔记没有导图数据（可重新生成）";
      setTimeout(function () { window.Mindmap.fit(); }, 60);
    } else {
      el.mindmapEmpty.hidden = false;
      el.mindmapEmpty.textContent = "导图库未加载（可联网后刷新页面）";
    }

    setTab(state.tab);
    Array.prototype.forEach.call(el.noteList.querySelectorAll("[data-note]"), function (node) {
      node.classList.toggle("is-active", node.getAttribute("data-note") === note.id);
    });
  }

  function setTab(tab) {
    state.tab = tab;
    Array.prototype.forEach.call(el.tabs.querySelectorAll(".tab"), function (node) {
      node.classList.toggle("is-active", node.getAttribute("data-tab") === tab);
    });
    el.viewMindmap.hidden = tab !== "mindmap";
    el.viewMarkdown.hidden = tab !== "markdown";
    if (tab === "mindmap") setTimeout(function () { window.Mindmap.fit(); }, 60);
  }

  /* ---------------- 笔记库 ---------------- */

  async function loadNotes() {
    try {
      var data = await API.listNotes(100);
      state.notes = data.items || [];
      el.noteCount.textContent = String(state.notes.length);
      if (!state.notes.length) {
        el.noteList.innerHTML = '<p class="empty">生成的笔记会出现在这里。</p>';
        return;
      }
      el.noteList.innerHTML = state.notes.map(function (note) {
        var active = state.activeNoteId === note.id ? " is-active" : "";
        return (
          '<div class="note-item' + active + '" data-note="' + note.id + '">' +
            '<div class="task-top"><span class="task-title">' + escapeHtml(note.title) + "</span>" +
            '<span class="pill pill-muted">' + (note.word_count || 0) + " 字</span></div>" +
            '<div class="task-sub">' + escapeHtml(note.uploader || "") +
            (note.uploader ? " · " : "") + formatDate(note.created_at) +
            ((note.tags || []).length ? " · " + note.tags.slice(0, 4).map(function (t) { return "#" + t; }).join(" ") : "") +
            "</div>" +
          "</div>"
        );
      }).join("");
      Array.prototype.forEach.call(el.noteList.querySelectorAll("[data-note]"), function (node) {
        node.addEventListener("click", function () { openNote(node.getAttribute("data-note")); });
      });
    } catch (err) {
      console.error(err);
    }
  }

  /* ---------------- 本地上传 ---------------- */

  async function onPickFiles(event) {
    var files = Array.prototype.slice.call(event.target.files || []);
    if (!files.length) return;
    el.uploadProgress.hidden = false;
    el.uploadFill.style.width = "0%";
    el.uploadPercent.textContent = "0%";
    try {
      var result = await API.uploadFiles(files, function (percent) {
        el.uploadFill.style.width = percent + "%";
        el.uploadPercent.textContent = percent + "%";
      });
      renderUploads(result.items || []);
      if (result.hint) el.uploadHint.textContent = result.hint;
      toast("已上传 " + (result.items || []).length + " 个文件", "ok");
      if (result.errors && result.errors.length) {
        toast("部分文件未通过：" + result.errors.map(function (e) { return e.name + "（" + e.reason + "）"; }).join("；"), "error");
      }
      if (state.mode === "batch") {
        var urls = (result.items || []).map(function (item) { return item.url; }).join("\n");
        el.inputBatch.value = (el.inputBatch.value ? el.inputBatch.value.trim() + "\n" : "") + urls;
        toast("已把上传文件填入批量输入框，点击「批量生成」开始", "ok");
      } else if ((result.items || []).length === 1) {
        el.inputUrl.value = result.items[0].url;
      } else if ((result.items || []).length > 1) {
        setMode("batch");
        el.inputBatch.value = (result.items || []).map(function (item) { return item.url; }).join("\n");
      }
      loadUploads();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      el.inputFile.value = "";
      setTimeout(function () { el.uploadProgress.hidden = true; }, 1200);
    }
  }

  function renderUploads(items) {
    state.uploads = items || [];
    if (!state.uploads.length) {
      el.uploadList.hidden = true;
      el.uploadList.innerHTML = "";
      return;
    }
    el.uploadList.hidden = false;
    el.uploadList.innerHTML = state.uploads.map(function (item) {
      return (
        '<div class="upload-item">' +
          '<span class="upload-name" title="' + escapeHtml(item.filename) + '">' + escapeHtml(item.filename) + "</span>" +
          '<span class="hint-inline">' + humanSize(item.size) + "</span>" +
          '<button class="btn btn-mini" data-use="' + escapeHtml(item.url) + '">填入</button>' +
          '<button class="btn btn-mini btn-danger" data-drop="' + escapeHtml(item.filename) + '">删除</button>' +
        "</div>"
      );
    }).join("");
    Array.prototype.forEach.call(el.uploadList.querySelectorAll("[data-use]"), function (node) {
      node.addEventListener("click", function () {
        var url = node.getAttribute("data-use");
        if (state.mode === "batch") {
          el.inputBatch.value = (el.inputBatch.value ? el.inputBatch.value.trim() + "\n" : "") + url;
        } else {
          el.inputUrl.value = url;
        }
        toast("已填入：" + url);
      });
    });
    Array.prototype.forEach.call(el.uploadList.querySelectorAll("[data-drop]"), function (node) {
      node.addEventListener("click", async function () {
        try {
          await API.deleteUpload(node.getAttribute("data-drop"));
          loadUploads();
          toast("已删除上传文件");
        } catch (err) { toast(err.message, "error"); }
      });
    });
  }

  async function loadUploads() {
    try {
      var data = await API.listUploads();
      renderUploads(data.items || []);
    } catch (err) { /* 忽略 */ }
  }

  /* ---------------- 导出 ---------------- */

  function activeNoteOrWarn() {
    if (!state.activeNote) { toast("请先生成或打开一篇笔记", "error"); return null; }
    return state.activeNote;
  }

  function downloadMd() {
    var note = activeNoteOrWarn();
    if (!note) return;
    var link = document.createElement("a");
    link.href = API.downloadUrl(note.id, "md");
    link.download = fileBase(note.title) + ".md";
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  function downloadXmind() {
    var note = activeNoteOrWarn();
    if (!note) return;
    if (!state.tree || !(state.tree.children || []).length) {
      toast("这篇笔记没有导图数据", "error");
      return;
    }
    var link = document.createElement("a");
    link.href = API.downloadUrl(note.id, "xmind");
    document.body.appendChild(link);
    link.click();
    link.remove();
    toast("已导出 XMind 文件（可直接用 XMind 打开）", "ok");
  }

  function exportPng() {
    var note = activeNoteOrWarn();
    if (!note) return;
    if (!window.Mindmap.isAvailable()) { toast("导图库未加载，无法导出图片", "error"); return; }
    toast("正在生成图片…");
    window.Mindmap.exportPng(state.tree || {}, 2)
      .then(function () { toast("PNG 已导出", "ok"); })
      .catch(function (err) { toast(err.message, "error"); });
  }

  function exportSvg() {
    var note = activeNoteOrWarn();
    if (!note) return;
    if (!window.Mindmap.isAvailable()) { toast("导图库未加载，无法导出", "error"); return; }
    if (window.Mindmap.exportSvg(state.tree || {})) toast("SVG 已导出", "ok");
  }

  async function deleteActiveNote() {
    var note = activeNoteOrWarn();
    if (!note) return;
    if (!window.confirm("删除笔记「" + (note.title || "") + "」？\n磁盘上的 md / 导图文件也会一并删除。")) return;
    try {
      await API.deleteNote(note.id, true);
      state.activeNote = null;
      state.activeNoteId = null;
      state.tree = null;
      el.resultActions.hidden = true;
      el.resultTitle.textContent = "等待生成…";
      el.resultMeta.textContent = "提交链接后，这里会显示 Markdown 笔记与思维导图。";
      el.markdownBody.innerHTML = '<p class="empty">笔记已删除。</p>';
      el.mindmapEmpty.hidden = false;
      loadNotes();
      toast("笔记已删除");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  /* ---------------- 设置 ---------------- */

  var SETTING_FIELDS = [
    "llm_base_url", "llm_api_key", "llm_model", "llm_mindmap_model",
    "llm_temperature", "llm_timeout", "bili_cookie", "bili_request_interval",
    "asr_provider", "asr_model", "asr_base_url", "asr_api_key",
    "asr_local_model", "asr_local_device", "note_style",
    "max_concurrency", "cache_enabled"
  ];

  async function refreshCacheInfo() {
    try {
      var data = await API.cacheStats();
      el.cacheInfo.textContent = "缓存 " + data.count + " 条，占用 " + (data.mb || 0) + " MB";
    } catch (err) {
      el.cacheInfo.textContent = "缓存统计不可用";
    }
  }

  async function openSettings() {
    try {
      var data = await API.getSettings();
      var settings = data.settings || {};
      state.settings = settings;
      SETTING_FIELDS.forEach(function (key) {
        var input = $("s-" + key);
        if (!input) return;
        var value = settings[key];
        if (key === "llm_api_key" || key === "asr_api_key" || key === "bili_cookie") {
          input.value = "";
          input.placeholder = value ? "已配置（留空表示不修改）" : (input.getAttribute("data-ph") || "");
        } else if (input.type === "checkbox") {
          input.checked = !!value;
        } else if (key === "cache_enabled") {
          // 用字符串 "true"/"false" 让下拉框正确选中
          input.value = value ? "true" : "false";
        } else {
          input.value = value == null ? "" : value;
        }
      });
      refreshCacheInfo();
      if (el.dialog.showModal) el.dialog.showModal();
      else el.dialog.setAttribute("open", "open");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function saveSettings(event) {
    var patch = {};
    SETTING_FIELDS.forEach(function (key) {
      var input = $("s-" + key);
      if (!input) return;
      if (input.type === "checkbox") { patch[key] = input.checked; return; }
      var value = input.value;
      if (value === "") return;
      if (key === "cache_enabled") { patch[key] = value === "true"; return; }
      if (input.type === "number") {
        var num = Number(value);
        if (!isNaN(num)) patch[key] = num;
      } else {
        patch[key] = value;
      }
    });
    try {
      var result = await API.saveSettings(patch);
      toast(result.message || "设置已保存", "ok");
      refreshHealth();
      return true;
    } catch (err) {
      toast(err.message, "error");
      if (event && event.preventDefault) event.preventDefault();
      return false;
    }
  }

  async function testLlm() {
    el.testLlmResult.textContent = "测试中…";
    try {
      var data = await API.testLlm();
      el.testLlmResult.textContent = "✅ 连接正常（" + data.model + "，" + data.elapsed + "s）";
    } catch (err) {
      el.testLlmResult.textContent = "❌ " + err.message;
    }
  }

  async function testBilibili() {
    el.testBiliResult.textContent = "测试中…";
    try {
      var data = await API.testBilibili();
      el.testBiliResult.textContent = "✅ 接口正常：" + data.title + "（" + data.uploader + "）" + (data.cookie ? "，已带 Cookie" : "，未配置 Cookie");
    } catch (err) {
      el.testBiliResult.textContent = "❌ " + err.message;
    }
  }

  async function clearCache() {
    if (!window.confirm("清空转写缓存？下次生成会重新抓取字幕/转写。")) return;
    try {
      var data = await API.clearCache();
      toast("已清理 " + data.removed + " 条缓存", "ok");
      refreshCacheInfo();
    } catch (err) {
      toast(err.message, "error");
    }
  }

  /* ---------------- 初始化 ---------------- */

  function bind() {
    el.form.addEventListener("submit", onSubmit);
    el.btnRefresh.addEventListener("click", function () { loadTasks(); loadNotes(); loadUploads(); refreshHealth(); });
    el.btnSettings.addEventListener("click", openSettings);
    el.formSettings.addEventListener("submit", function (event) {
      var submitter = event.submitter;
      if (submitter && submitter.value === "cancel") return;
      event.preventDefault();
      saveSettings(event).then(function (ok) { if (ok) el.dialog.close(); });
    });
    $("btn-test-llm").addEventListener("click", testLlm);
    $("btn-test-bili").addEventListener("click", testBilibili);
    $("btn-clear-cache").addEventListener("click", clearCache);

    Array.prototype.forEach.call(el.inputMode.querySelectorAll(".tab"), function (node) {
      node.addEventListener("click", function () { setMode(node.getAttribute("data-mode")); });
    });
    Array.prototype.forEach.call(el.tabs.querySelectorAll(".tab"), function (node) {
      node.addEventListener("click", function () { setTab(node.getAttribute("data-tab")); });
    });

    el.inputFile.addEventListener("change", onPickFiles);

    $("btn-download-md").addEventListener("click", downloadMd);
    $("btn-export-xmind").addEventListener("click", downloadXmind);
    $("btn-export-png").addEventListener("click", exportPng);
    $("btn-export-svg").addEventListener("click", exportSvg);
    $("btn-delete-note").addEventListener("click", deleteActiveNote);
    $("mm-fit").addEventListener("click", function () { window.Mindmap.fit(); });
    $("mm-zoom-in").addEventListener("click", function () { window.Mindmap.rescale(1.25); });
    $("mm-zoom-out").addEventListener("click", function () { window.Mindmap.rescale(0.8); });

    // Ctrl/Cmd + Enter 提交
    [el.inputUrl, el.inputBatch].forEach(function (node) {
      node.addEventListener("keydown", function (event) {
        if ((event.ctrlKey || event.metaKey) && event.key === "Enter") onSubmit(event);
      });
    });
  }

  async function boot() {
    el = {
      form: $("form-create"),
      inputUrl: $("input-url"),
      inputBatch: $("input-batch"),
      inputStyle: $("input-style"),
      inputRefresh: $("input-refresh"),
      inputMode: $("input-mode"),
      paneSingle: $("pane-single"),
      paneBatch: $("pane-batch"),
      inputFile: $("input-file"),
      uploadHint: $("upload-hint"),
      uploadProgress: $("upload-progress"),
      uploadFill: $("upload-fill"),
      uploadPercent: $("upload-percent"),
      uploadList: $("upload-list"),
      btnSubmit: $("btn-submit"),
      btnRefresh: $("btn-refresh"),
      btnSettings: $("btn-settings"),
      health: $("health"),
      taskList: $("task-list"),
      taskCount: $("task-count"),
      noteList: $("note-list"),
      noteCount: $("note-count"),
      resultTitle: $("result-title"),
      resultMeta: $("result-meta"),
      resultActions: $("result-actions"),
      tabs: $("tabs"),
      progressWrap: $("progress-wrap"),
      progressFill: $("progress-fill"),
      progressStage: $("progress-stage"),
      progressPercent: $("progress-percent"),
      progressMessage: $("progress-message"),
      viewMindmap: $("view-mindmap"),
      viewMarkdown: $("view-markdown"),
      markdownBody: $("markdown-body"),
      mindmapEl: $("mindmap"),
      mindmapEmpty: $("mindmap-empty"),
      dialog: $("dialog-settings"),
      formSettings: $("form-settings"),
      testLlmResult: $("test-llm-result"),
      testBiliResult: $("test-bili-result"),
      cacheInfo: $("cache-info"),
      createHint: $("create-hint"),
      toast: $("toast")
    };
    SETTING_FIELDS.forEach(function (key) {
      var input = $("s-" + key);
      if (input) input.setAttribute("data-ph", input.placeholder || "");
    });

    bind();
    setMode("single");
    el.markdownBody.innerHTML = '<p class="empty">提交链接后，这里会显示 Markdown 笔记。</p>';

    refreshHealth();
    await Promise.all([loadTasks(), loadNotes(), loadUploads()]);
    startPolling();
    setInterval(refreshHealth, 30000);

    if (window.__vendorsReady) {
      var info = await window.__vendorsReady;
      if (info && info.missing && info.missing.length) console.warn("以下前端依赖未能加载：", info.missing);
    }
    if (!window.Mindmap.init(el.mindmapEl)) {
      el.mindmapEmpty.textContent = "导图库未加载（可联网后刷新页面）";
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
