/* Markdown 渲染：优先 marked，缺失时用内置的极简渲染器兜底。
   内容来源是"视频字幕 -> 大模型"，但依然做一次白名单清洗，避免意外注入。 */
(function () {
  "use strict";

  var DANGEROUS = /<\s*(script|iframe|object|embed|form|link|meta|style|base)\b[^>]*>[\s\S]*?<\s*\/\s*\1\s*>|<\s*(script|iframe|object|embed|form|link|meta|style|base)\b[^>]*\/?\s*>/gi;
  var EVENT_ATTR = /\son[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi;
  var JS_URL = /(href|src)\s*=\s*("|')\s*javascript:[^"']*\2/gi;

  function sanitize(html) {
    return String(html)
      .replace(DANGEROUS, "")
      .replace(EVENT_ATTR, "")
      .replace(JS_URL, '$1="#"');
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function inline(text) {
    return escapeHtml(text)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/~~([^~]+)~~/g, "<del>$1</del>")
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (m, label, href) {
        var safe = /^https?:\/\//i.test(href) ? href : "#";
        return '<a href="' + safe + '" target="_blank" rel="noreferrer">' + label + "</a>";
      });
  }

  /* 极简 Markdown 渲染（不依赖 marked，够本项目模板用） */
  function fallbackRender(markdown) {
    var lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
    var out = [];
    var listStack = [];
    var inCode = false;
    var codeBuffer = [];
    var inTable = false;

    function closeLists(toDepth) {
      while (listStack.length > (toDepth || 0)) {
        out.push("</" + listStack.pop() + ">");
      }
    }
    function closeTable() {
      if (inTable) { out.push("</tbody></table>"); inTable = false; }
    }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      var trimmed = line.trim();

      if (/^(```|~~~)/.test(trimmed)) {
        if (inCode) {
          out.push("<pre><code>" + escapeHtml(codeBuffer.join("\n")) + "</code></pre>");
          codeBuffer = []; inCode = false;
        } else {
          closeLists(); closeTable(); inCode = true;
        }
        continue;
      }
      if (inCode) { codeBuffer.push(line); continue; }
      if (!trimmed) { closeLists(); closeTable(); continue; }

      var heading = /^(#{1,6})\s+(.*)$/.exec(trimmed);
      if (heading) {
        closeLists(); closeTable();
        var level = heading[1].length;
        out.push("<h" + level + ">" + inline(heading[2]) + "</h" + level + ">");
        continue;
      }

      if (/^(-{3,}|\*{3,}|_{3,})$/.test(trimmed)) {
        closeLists(); closeTable(); out.push("<hr>"); continue;
      }

      // 表格
      if (trimmed.indexOf("|") === 0 && lines[i + 1] && /^\|?[\s:|-]+\|?$/.test(lines[i + 1].trim())) {
        closeLists();
        var headers = trimmed.replace(/^\||\|$/g, "").split("|");
        out.push("<table><thead><tr>");
        headers.forEach(function (h) { out.push("<th>" + inline(h.trim()) + "</th>"); });
        out.push("</tr></thead><tbody>");
        inTable = true;
        i++;
        continue;
      }
      if (inTable && trimmed.indexOf("|") >= 0) {
        var cells = trimmed.replace(/^\||\|$/g, "").split("|");
        out.push("<tr>");
        cells.forEach(function (c) { out.push("<td>" + inline(c.trim()) + "</td>"); });
        out.push("</tr>");
        continue;
      }
      closeTable();

      var bullet = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(line);
      if (bullet) {
        var indent = bullet[1].replace(/\t/g, "    ").length;
        var depth = Math.min(4, Math.floor(indent / 2) + 1);
        var ordered = /\d/.test(bullet[2]);
        var tag = ordered ? "ol" : "ul";
        while (listStack.length > depth) out.push("</" + listStack.pop() + ">");
        if (listStack.length < depth) {
          out.push("<" + tag + ">");
          listStack.push(tag);
        }
        out.push("<li>" + inline(bullet[3]) + "</li>");
        continue;
      }
      closeLists();

      if (trimmed.indexOf("> ") === 0 || trimmed === ">") {
        out.push("<blockquote>" + inline(trimmed.replace(/^>\s?/, "")) + "</blockquote>");
        continue;
      }
      out.push("<p>" + inline(trimmed) + "</p>");
    }
    if (inCode && codeBuffer.length) {
      out.push("<pre><code>" + escapeHtml(codeBuffer.join("\n")) + "</code></pre>");
    }
    closeLists(); closeTable();
    return out.join("\n");
  }

  function render(markdown) {
    var html;
    if (typeof window.marked !== "undefined" && window.marked) {
      try {
        if (typeof window.marked.parse === "function") {
          html = window.marked.parse(String(markdown || ""), { gfm: true, breaks: false });
        } else if (typeof window.marked === "function") {
          html = window.marked(String(markdown || ""), { gfm: true, breaks: false });
        }
      } catch (err) {
        console.warn("marked 渲染失败，使用内置渲染器", err);
      }
    }
    if (!html) html = fallbackRender(markdown);
    return sanitize(html);
  }

  function toPlainText(markdown, limit) {
    var text = String(markdown || "")
      .replace(/```[\s\S]*?```/g, " ")
      .replace(/[#>*`_~|-]/g, " ")
      .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")
      .replace(/\s+/g, " ")
      .trim();
    limit = limit || 160;
    return text.length > limit ? text.slice(0, limit) + "…" : text;
  }

  window.MD = { render: render, sanitize: sanitize, toPlainText: toPlainText, fallbackRender: fallbackRender };
})();
