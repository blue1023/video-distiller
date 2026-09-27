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
    queue: function () { return request("/api/queue"); },
    platforms: function () { return request("/api/platforms"); },

    createTask: function (url, noteStyle, refresh) {
      return request("/api/tasks", {
        method: "POST",
        body: { url: url, note_style: noteStyle || null, refresh: !!refresh }
      });
    },
    createBatch: function (text, noteStyle, refresh) {
      return request("/api/tasks/batch", {
        method: "POST",
        body: { text: text, note_style: noteStyle || null, refresh: !!refresh }
      });
    },
    listTasks: function (limit, offset) {
      return request("/api/tasks?limit=" + (limit || 50) + "&offset=" + (offset || 0));
    },
    getTask: function (id) { return request("/api/tasks/" + encodeURIComponent(id)); },
    cancelTask: function (id) { return request("/api/tasks/" + encodeURIComponent(id) + "/cancel", { method: "POST" }); },
    refreshTask: function (id, useCache) {
      return request("/api/tasks/" + encodeURIComponent(id) + "/refresh" + (useCache ? "?use_cache=true" : ""), { method: "POST" });
    },
    deleteTask: function (id) { return request("/api/tasks/" + encodeURIComponent(id), { method: "DELETE" }); },

    listNotes: function (limit) { return request("/api/notes?limit=" + (limit || 100)); },
    getNote: function (id) { return request("/api/notes/" + encodeURIComponent(id)); },
    deleteNote: function (id, purge) {
      return request("/api/notes/" + encodeURIComponent(id) + (purge ? "?purge=true" : ""), { method: "DELETE" });
    },
    downloadUrl: function (id, kind) {
      return "/api/notes/" + encodeURIComponent(id) + "/download?kind=" + (kind || "md");
    },

    uploadFiles: function (fileList, onProgress) {
      // 用 XHR 以便拿到上传进度
      return new Promise(function (resolve, reject) {
        var form = new FormData();
        for (var i = 0; i < fileList.length; i++) form.append("files", fileList[i]);
        var xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/upload");
        xhr.upload.onprogress = function (event) {
          if (onProgress && event.lengthComputable) {
            onProgress(Math.round((event.loaded / event.total) * 100));
          }
        };
        xhr.onload = function () {
          var payload = null;
          try { payload = JSON.parse(xhr.responseText); } catch (err) { payload = null; }
          if (xhr.status >= 200 && xhr.status < 300) resolve(payload);
          else reject(new Error((payload && (payload.detail || payload.message)) || ("上传失败 HTTP " + xhr.status)));
        };
        xhr.onerror = function () { reject(new Error("上传失败：网络错误")); };
        xhr.send(form);
      });
    },
    listUploads: function () { return request("/api/upload"); },
    deleteUpload: function (filename) {
      return request("/api/upload/" + encodeURIComponent(filename), { method: "DELETE" });
    },

    getSettings: function () { return request("/api/settings"); },
    saveSettings: function (patch) { return request("/api/settings", { method: "PATCH", body: patch }); },
    testLlm: function () { return request("/api/settings/test-llm", { method: "POST" }); },
    testBilibili: function () { return request("/api/settings/test-bilibili", { method: "POST" }); },
    cacheStats: function () { return request("/api/cache"); },
    clearCache: function () { return request("/api/cache", { method: "DELETE" }); }
  };
})();
