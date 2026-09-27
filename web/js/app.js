/* 前端主逻辑：任务提交 / SSE 进度 / 笔记与导图展示 / 导出 */
(function () {
  "use strict";

  var state = {
    tasks: [],
    notes: [],
    activeTaskId: null,
    activeNote: null,
    tree: null,
    tab: "mindmap",
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
    box._timer = setTimeout(function () { box.hidden = true; }, kind === "error" ? 6000 : 3000);
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

  /* ---------------- 健康状态 ---------------- */

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
        el.health.title = "语音转写：" + data.asr_provider + " · 累计任务：" + data.tasks;
      }
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
    var sub = [];
    if (task.uploader) sub.push(escapeHtml(task.uploader));
    if (task.duration) sub.push(formatTime(task.duration));
    if (task.transcript_chars) sub.push("字幕 " + task.transcript_chars + " 字");
    sub.push(formatDate(task.created_at));

    var message = "";
    if (task.status === "failed" && task.error) message = task.error;
    else if (task.status === "running" || task.status === "pending") {
      message = (STAGE_LABEL[task.stage] || task.stage || "") + (task.message ? " · " + task.message : "");
      message = task.message || message;
    }

    return (
      '<div class="task-item' + isActive + isDone + '" data-task="' + task.id + '">' +
        '<div class="task-top">' +
          '<span class="task-title">' + escapeHtml(title) + "</span>" +
          '<span class="pill ' + status.cls + '">' + status.label + "</span>" +
        "</div>" +
        '<div class="task-sub">' + sub.join(" · ") + "</div>" +
        (message ? '<div class="task-sub">' + escapeHtml(String(message).slice(0, 160)) + "</div>" : "") +
        '<div class="mini-progress"><i style="width:' + progress + '%"></i></div>' +
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
      node.addEventListener("click", function () { selectTask(node.getAttribute("data-task")); });
    });
  }

  async function loadTasks() {
    try {
      var data = await API.listTasks(50);
      state.tasks = data.items || [];
      renderTasks();
      // 继续跟踪仍在运行的任务
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
      // 流断了就退回轮询，保证状态最终一致
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
    upsertTask(patch);

    if (taskId === state.activeTaskId) {
      updateProgressUI(patch);
      if (data.status === "success") {
        var noteId = data.note_id;
        toast("✅ 笔记生成完成", "ok");
        if (noteId) openNote(noteId);
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
    }, 2000);
  }

  /* ---------------- 提交 ---------------- */

  async function onSubmit(event) {
    event.preventDefault();
    var url = el.inputUrl.value.trim();
    if (!url) { toast("请先粘贴 B 站视频链接", "error"); return; }
    el.btnSubmit.disabled = true;
    el.btnSubmit.textContent = "提交中…";
    try {
      var task = await API.createTask(url, el.inputStyle.value);
      el.inputUrl.value = "";
      upsertTask(task);
      state.activeTaskId = task.id;
      renderTasks();
      showProgressPanel(task);
      subscribe(task.id);
      startPolling();
      toast("任务已提交，正在后台生成…");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      el.btnSubmit.disabled = false;
      el.btnSubmit.textContent = "生成笔记";
    }
  }

  function showProgressPanel(task) {
    el.resultTitle.textContent = task.title || "正在处理…";
    el.resultMeta.textContent = task.url || "";
    el.resultActions.hidden = true;
    el.progressWrap.hidden = false;
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
      el.progressFill.style.background = "";
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
      el.mindmapEmpty.textContent = "导图库未加载（可联网后刷新，或在设置里检查依赖）";
    }

    setTab(state.tab);
    // 高亮笔记库中的对应项
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

  /* ---------------- 设置 ---------------- */

  var SETTING_FIELDS = [
    "llm_base_url", "llm_api_key", "llm_model", "llm_mindmap_model",
    "llm_temperature", "llm_timeout", "bili_cookie", "bili_request_interval",
    "asr_provider", "asr_model", "asr_base_url", "asr_api_key",
    "asr_local_model", "asr_local_device", "note_style"
  ];

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
          input.placeholder = value ? "已配置（留空表示不修改）" : (input.getAttribute("data-ph") || input.placeholder);
        } else {
          input.value = value == null ? "" : value;
        }
      });
      if (el.dialog.showModal) el.dialog.showModal();
      else el.dialog.setAttribute("open", "open");
    } catch (err) {
      toast(err.message, "error");
    }
  }

  async function saveSettings(event) {
    // method="dialog" 的表单：先拦截，再决定是否关闭
    var patch = {};
    SETTING_FIELDS.forEach(function (key) {
      var input = $("s-" + key);
      if (!input) return;
      var value = input.value;
      if (value === "") return;
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

  /* ---------------- 初始化 ---------------- */

  function bind() {
    el.form.addEventListener("submit", onSubmit);
    el.btnRefresh.addEventListener("click", function () { loadTasks(); loadNotes(); refreshHealth(); });
    el.btnSettings.addEventListener("click", openSettings);
    el.formSettings.addEventListener("submit", function (event) {
      // 只有点击"保存"才写入；取消直接关
      var submitter = event.submitter;
      if (submitter && submitter.value === "cancel") return;
      event.preventDefault();
      saveSettings(event).then(function (ok) { if (ok) el.dialog.close(); });
    });
    $("btn-test-llm").addEventListener("click", testLlm);
    $("btn-test-bili").addEventListener("click", testBilibili);

    Array.prototype.forEach.call(el.tabs.querySelectorAll(".tab"), function (node) {
      node.addEventListener("click", function () { setTab(node.getAttribute("data-tab")); });
    });

    $("btn-download-md").addEventListener("click", downloadMd);
    $("btn-export-xmind").addEventListener("click", downloadXmind);
    $("btn-export-png").addEventListener("click", exportPng);
    $("btn-export-svg").addEventListener("click", exportSvg);
    $("mm-fit").addEventListener("click", function () { window.Mindmap.fit(); });
    $("mm-zoom-in").addEventListener("click", function () { window.Mindmap.rescale(1.25); });
    $("mm-zoom-out").addEventListener("click", function () { window.Mindmap.rescale(0.8); });

    // 快捷键：Ctrl/Cmd + Enter 提交
    el.inputUrl.addEventListener("keydown", function (event) {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") onSubmit(event);
    });
  }

  async function boot() {
    el = {
      form: $("form-create"),
      inputUrl: $("input-url"),
      inputStyle: $("input-style"),
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
      toast: $("toast")
    };
    // 记录原始 placeholder，设置面板里要复用
    SETTING_FIELDS.forEach(function (key) {
      var input = $("s-" + key);
      if (input) input.setAttribute("data-ph", input.placeholder || "");
    });

    bind();
    el.markdownBody.innerHTML = '<p class="empty">提交链接后，这里会显示 Markdown 笔记。</p>';

    refreshHealth();
    await Promise.all([loadTasks(), loadNotes()]);
    startPolling();
    setInterval(refreshHealth, 60000);

    if (window.__vendorsReady) {
      var info = await window.__vendorsReady;
      if (info && info.missing && info.missing.length) {
        console.warn("以下前端依赖未能加载：", info.missing);
      }
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
