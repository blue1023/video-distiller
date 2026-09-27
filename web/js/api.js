/* 后端接口封装：统一错误处理 + 简洁的返回值 */
(function () {
  "use strict";

  async function request(path, options) {
    options = options || {};
    var response;
    try {
      response = await fetch(path, {
        method: options.method || "GET",
        headers: options.body ? { "Content-Type": "application/json" } : undefined,
        body: options.body ? JSON.stringify(options.body) : undefined
      });
    } catch (err) {
      throw new Error("无法连接后端服务，请确认 python run.py 正在运行");
    }

    var text = await response.text();
    var payload = null;
    if (text) {
      try { payload = JSON.parse(text); } catch (err) { payload = { detail: text }; }
    }
    if (!response.ok) {
      var detail = payload && (payload.detail || payload.message);
      if (Array.isArray(detail)) {
        detail = detail.map(function (d) { return d.msg || JSON.stringify(d); }).join("；");
      }
      throw new Error(detail || ("请求失败 HTTP " + response.status));
    }
    return payload;
  }

  window.API = {
    health: function () { return request("/api/health"); },

    createTask: function (url, noteStyle) {
      return request("/api/tasks", {
        method: "POST",
        body: { url: url, note_style: noteStyle || null }
      });
    },
    listTasks: function (limit) { return request("/api/tasks?limit=" + (limit || 50)); },
    getTask: function (id) { return request("/api/tasks/" + encodeURIComponent(id)); },
    cancelTask: function (id) { return request("/api/tasks/" + encodeURIComponent(id) + "/cancel", { method: "POST" }); },
    deleteTask: function (id) { return request("/api/tasks/" + encodeURIComponent(id), { method: "DELETE" }); },

    listNotes: function (limit) { return request("/api/notes?limit=" + (limit || 100)); },
    getNote: function (id) { return request("/api/notes/" + encodeURIComponent(id)); },
    deleteNote: function (id, purge) {
      return request("/api/notes/" + encodeURIComponent(id) + (purge ? "?purge=true" : ""), { method: "DELETE" });
    },
    downloadUrl: function (id, kind) {
      return "/api/notes/" + encodeURIComponent(id) + "/download?kind=" + (kind || "md");
    },

    getSettings: function () { return request("/api/settings"); },
    saveSettings: function (patch) { return request("/api/settings", { method: "PATCH", body: patch }); },
    testLlm: function () { return request("/api/settings/test-llm", { method: "POST" }); },
    testBilibili: function () { return request("/api/settings/test-bilibili", { method: "POST" }); }
  };
})();
