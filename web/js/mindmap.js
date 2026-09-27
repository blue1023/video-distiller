/* 思维导图：markmap 渲染 + 自适应抽屉布局 + 完整展开导出（SVG / PNG） */
(function () {
  "use strict";

  var state = {
    mm: null,
    markdown: "",
    ready: false,
    available: false,
    color: null,
    fileBase: "mindmap"
  };

  function readVar(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function markdownFromTree(node, depth) {
    var lines = [];
    function walk(item, level) {
      var text = String((item && (item.content || item.name)) || "").replace(/\n/g, " ").trim();
      if (!text) return;
      lines.push(new Array(level + 1).join("  ") + "- " + text);
      var children = (item && item.children) || [];
      for (var i = 0; i < children.length; i++) walk(children[i], level + 1);
    }
    var roots = (node && node.children) || [];
    for (var i = 0; i < roots.length; i++) walk(roots[i], 0);
    return lines.join("\n");
  }

  function init(svgEl) {
    if (!svgEl) return false;
    var mmNs = window.markmap;
    if (!mmNs || typeof mmNs.Markmap !== "function" || typeof mmNs.Transformer !== "function") {
      state.available = false;
      return false;
    }
    try {
      var transformer = new mmNs.Transformer();
      var deriveOptions = {};
      if (typeof mmNs.deriveOptions === "function") {
        // 只有在 options 里带 color 字段时才生效，保持默认即可
        deriveOptions = {};
      }
      var background = readVar("--panel", "#ffffff");
      state.mm = mmNs.Markmap.create(
        svgEl,
        {
          autoFit: true,
          duration: 320,
          spacingVertical: 8,
          spacingHorizontal: 90,
          paddingX: 18,
          maxWidth: 320,
          initialExpandLevel: -1,
          color: function (node) { return colorFor(node.state && node.state.path); },
          style: function (id) {
            return "#" + id + " { font-family: -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif; }";
          }
        },
        null
      );
      svgEl.style.background = background;
      state.transformer = transformer;
      state.ready = true;
      state.available = true;
      return true;
    } catch (err) {
      console.warn("markmap 初始化失败", err);
      state.available = false;
      return false;
    }
  }

  var PALETTE = ["#2563eb", "#0d9488", "#d97706", "#7c3aed", "#db2777", "#059669", "#dc2626", "#0891b2"];

  function colorFor(path) {
    if (!path) return PALETTE[0];
    var text = String(path);
    var hash = 0;
    for (var i = 0; i < text.length; i++) hash = (hash * 31 + text.charCodeAt(i)) % 100000;
    return PALETTE[hash % PALETTE.length];
  }

  function render(tree, fileBase) {
    if (!state.ready) return false;
    var markdown = markdownFromTree(tree || { children: [] }, 0);
    if (!markdown.trim()) return false;
    state.markdown = markdown;
    state.fileBase = fileBase || "mindmap";
    try {
      var result = state.transformer.transform(markdown);
      if (typeof state.mm.setData === "function") {
        state.mm.setData(result.root, result.features ? { features: result.features } : undefined);
      } else {
        state.mm.setData(result.root);
      }
      return true;
    } catch (err) {
      console.warn("markmap 渲染失败", err);
      return false;
    }
  }

  function fit() {
    if (state.mm && typeof state.mm.fit === "function") {
      try { state.mm.fit(); } catch (err) { /* 忽略 */ }
    }
  }

  function rescale(factor) {
    if (state.mm && typeof state.mm.rescale === "function") {
      try { state.mm.rescale(factor); } catch (err) { /* 忽略 */ }
    }
  }

  /* ---------------- 导出 ---------------- */

  function inlineStyle(svg, background, fontFamily) {
    var style = document.createElementNS("http://www.w3.org/2000/svg", "style");
    style.textContent =
      "text{font-family:" + fontFamily + ";}" +
      "svg{background:" + background + ";}";
    svg.insertBefore(style, svg.firstChild);
  }

  function prepareSvg(svg, width, height, background) {
    svg.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    svg.setAttribute("xmlns:xlink", "http://www.w3.org/1999/xlink");
    svg.setAttribute("width", String(Math.max(1, Math.round(width))));
    svg.setAttribute("height", String(Math.max(1, Math.round(height))));
    if (!svg.getAttribute("viewBox")) {
      svg.setAttribute("viewBox", "0 0 " + Math.max(1, Math.round(width)) + " " + Math.max(1, Math.round(height)));
    }
    inlineStyle(svg, background, "-apple-system,'Segoe UI','Microsoft YaHei',sans-serif");
  }

  /* 用一份"全部展开"的离屏 markmap 渲染完整导图，拿到真实内容尺寸 */
  function renderFullSvg(tree) {
    var mmNs = window.markmap;
    if (!mmNs || typeof mmNs.Transformer !== "function" || typeof mmNs.Markmap !== "function") return null;
    var markdown = markdownFromTree(tree || { children: [] }, 0);
    if (!markdown.trim()) return null;

    var holder = document.createElement("div");
    holder.style.cssText = "position:fixed;left:-100000px;top:0;width:2400px;height:1600px;opacity:0;pointer-events:none;";
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", "2400");
    svg.setAttribute("height", "1600");
    holder.appendChild(svg);
    document.body.appendChild(holder);

    try {
      var background = readVar("--panel", "#ffffff");
      var transformer = new mmNs.Transformer();
      var mm = mmNs.Markmap.create(svg, { autoFit: false, duration: 0, initialExpandLevel: -1, maxWidth: 360 });
      var result = transformer.transform(markdown);
      mm.setData(result.root, result.features ? { features: result.features } : undefined);

      var group = svg.querySelector("g");
      var box = { x: 0, y: 0, width: 2400, height: 1600 };
      if (group && typeof group.getBBox === "function") {
        var raw = group.getBBox();
        if (raw.width > 1 && raw.height > 1) box = raw;
      }
      var padding = 24;
      var width = box.width + padding * 2;
      var height = box.height + padding * 2;
      var clone = svg.cloneNode(true);
      var cloneGroup = clone.querySelector("g");
      var transform = "translate(" + (padding - box.x) + "," + (padding - box.y) + ")";
      if (cloneGroup) cloneGroup.setAttribute("transform", transform);
      // 去掉 markmap 内部可能存在的 width/height 100% 样式
      clone.removeAttribute("style");
      prepareSvg(clone, width, height, background);
      clone.setAttribute("viewBox", "0 0 " + width + " " + height);
      return { svg: clone, width: width, height: height, background: background };
    } catch (err) {
      console.warn("完整导图渲染失败，回退到当前视图导出", err);
      return null;
    } finally {
      holder.remove();
    }
  }

  function currentSvgPayload() {
    var svg = document.getElementById("mindmap");
    if (!svg) return null;
    var rect = svg.getBoundingClientRect();
    var width = rect.width || 1200;
    var height = rect.height || 800;
    var clone = svg.cloneNode(true);
    clone.removeAttribute("style");
    var background = readVar("--panel", "#ffffff");
    prepareSvg(clone, width, height, background);
    clone.setAttribute("viewBox", "0 0 " + width + " " + height);
    return { svg: clone, width: width, height: height, background: background };
  }

  function serialize(svg) {
    return '<?xml version="1.0" standalone="no"?>\n' + new XMLSerializer().serializeToString(svg);
  }

  function download(blob, filename) {
    var url = URL.createObjectURL(blob);
    var link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
  }

  function exportSvg(tree) {
    var payload = renderFullSvg(tree) || currentSvgPayload();
    if (!payload) return false;
    var text = serialize(payload.svg);
    download(new Blob([text], { type: "image/svg+xml;charset=utf-8" }), state.fileBase + ".svg");
    return true;
  }

  function exportPng(tree, scale) {
    var payload = renderFullSvg(tree) || currentSvgPayload();
    if (!payload) return Promise.reject(new Error("没有可导出的导图"));
    scale = scale || 2;
    var text = serialize(payload.svg);
    // 用 data URL 而不是 blob URL，避免 canvas 被跨源污染
    var dataUrl = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(text);

    return new Promise(function (resolve, reject) {
      var image = new Image();
      image.onload = function () {
        try {
          var canvas = document.createElement("canvas");
          canvas.width = Math.round(payload.width * scale);
          canvas.height = Math.round(payload.height * scale);
          var ctx = canvas.getContext("2d");
          ctx.fillStyle = payload.background;
          ctx.fillRect(0, 0, canvas.width, canvas.height);
          ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
          canvas.toBlob(function (blob) {
            if (!blob) { reject(new Error("PNG 生成失败")); return; }
            download(blob, state.fileBase + ".png");
            resolve(true);
          }, "image/png");
        } catch (err) {
          reject(err);
        }
      };
      image.onerror = function () { reject(new Error("SVG 转图片失败")); };
      image.src = dataUrl;
    });
  }

  window.Mindmap = {
    init: init,
    render: render,
    fit: fit,
    rescale: rescale,
    exportSvg: exportSvg,
    exportPng: exportPng,
    isAvailable: function () { return state.available; },
    markdownFromTree: markdownFromTree
  };
})();
