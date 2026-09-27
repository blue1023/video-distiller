/* 依赖缺失时自动从 CDN 兜底加载（离线环境会静默失败，功能降级但页面仍可用） */
(function () {
  "use strict";
  var CDN = {
    d3: "https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js",
    "markmap-view": "https://cdn.jsdelivr.net/npm/markmap-view@0.18.12/dist/browser/index.js",
    "markmap-lib": "https://cdn.jsdelivr.net/npm/markmap-lib@0.18.12/dist/browser/index.iife.js",
    marked: "https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"
  };

  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var el = document.createElement("script");
      el.src = src;
      el.onload = resolve;
      el.onerror = function () { reject(new Error("加载失败: " + src)); };
      document.head.appendChild(el);
    });
  }

  window.__vendorsReady = (async function () {
    var missing = [];
    if (typeof window.d3 === "undefined") missing.push("d3");
    if (!window.markmap || !window.markmap.Markmap) missing.push("markmap-view");
    if (!window.markmap || !window.markmap.Transformer) missing.push("markmap-lib");
    if (typeof window.marked === "undefined") missing.push("marked");
    if (!missing.length) return { source: "local", missing: [] };

    var failed = [];
    for (var i = 0; i < missing.length; i++) {
      try {
        await loadScript(CDN[missing[i]]);
      } catch (err) {
        failed.push(missing[i]);
      }
    }
    return { source: failed.length ? "partial" : "cdn", missing: failed };
  })();
})();
