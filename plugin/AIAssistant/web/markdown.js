/* Minimal, safe Markdown -> HTML (escape first, then format).
 * Supports: headings, paragraphs, line breaks, **bold**, *italic*, `code`, ``` fences,
 * bullet/numbered lists, pipe tables and http(s) links.
 */
(function () {
  "use strict";

  function escapeHtml(text) {
    return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function inline(text) {
    let out = escapeHtml(text);
    const codes = [];
    out = out.replace(/`([^`]+)`/g, (m, code) => { codes.push(code); return "\u0000" + (codes.length - 1) + "\u0000"; });
    out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    out = out.replace(/\u0000(\d+)\u0000/g, (m, i) => "<code>" + codes[Number(i)] + "</code>");
    return out;
  }

  function splitRow(line) {
    let row = line.trim();
    if (row.startsWith("|")) row = row.slice(1);
    if (row.endsWith("|")) row = row.slice(0, -1);
    return row.split("|").map((cell) => cell.trim());
  }

  function isTableSeparator(line) {
    return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
  }

  function render(markdown) {
    const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
    const html = [];
    let paragraph = [];
    let i = 0;

    function flush() {
      if (paragraph.length) {
        html.push("<p>" + paragraph.map(inline).join("<br>") + "</p>");
        paragraph = [];
      }
    }

    while (i < lines.length) {
      const line = lines[i];
      if (/^\s*```/.test(line)) {
        flush();
        const code = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) { code.push(lines[i]); i++; }
        i++;
        html.push("<pre><code>" + escapeHtml(code.join("\n")) + "</code></pre>");
        continue;
      }
      if (line.indexOf("|") !== -1 && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
        flush();
        const head = splitRow(line);
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].indexOf("|") !== -1 && lines[i].trim() !== "") {
          rows.push(splitRow(lines[i]));
          i++;
        }
        let table = "<table><thead><tr>" + head.map((c) => "<th>" + inline(c) + "</th>").join("") + "</tr></thead><tbody>";
        rows.forEach((r) => { table += "<tr>" + r.map((c) => "<td>" + inline(c) + "</td>").join("") + "</tr>"; });
        html.push(table + "</tbody></table>");
        continue;
      }
      const heading = /^(#{1,4})\s+(.*)$/.exec(line);
      if (heading) {
        flush();
        const level = Math.min(heading[1].length + 1, 4);
        html.push("<h" + level + ">" + inline(heading[2]) + "</h" + level + ">");
        i++;
        continue;
      }
      if (/^\s*([-*•]|\d+[.)])\s+/.test(line)) {
        flush();
        const ordered = /^\s*\d+[.)]\s+/.test(line);
        const items = [];
        while (i < lines.length && /^\s*([-*•]|\d+[.)])\s+/.test(lines[i])) {
          items.push(lines[i].replace(/^\s*([-*•]|\d+[.)])\s+/, ""));
          i++;
        }
        const tag = ordered ? "ol" : "ul";
        html.push("<" + tag + ">" + items.map((t) => "<li>" + inline(t) + "</li>").join("") + "</" + tag + ">");
        continue;
      }
      if (line.trim() === "") {
        flush();
      } else {
        paragraph.push(line);
      }
      i++;
    }
    flush();
    return html.join("");
  }

  window.Markdown = { render: render, escape: escapeHtml };
})();
