function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function unescapeHtml(value) {
  const entities = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" };
  return value.replace(/&amp;|&lt;|&gt;|&quot;|&#39;/g, (entity) => entities[entity]);
}

export function workspaceLinkPath(href) {
  if (!href.startsWith("/workspace/")) return null;
  let path;
  try {
    path = decodeURIComponent(href.slice("/workspace/".length));
  } catch {
    return null;
  }
  if (!path || path.includes("\\") || /[\x00-\x1f\x7f]/.test(path) || path.split("/").some((part) => !part || part === "." || part === "..")) {
    return null;
  }
  return path;
}

function inlineMarkdown(value) {
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\(((?:https?:\/\/|\/workspace\/)[^)\s]+)\)/g, (match, label, escapedHref) => {
      if (escapedHref.startsWith("/workspace/")) {
        const path = workspaceLinkPath(unescapeHtml(escapedHref));
        return path === null ? match : `<a href="#" data-workspace-file="${escapeHtml(path)}">${label}</a>`;
      }
      return `<a href="${escapedHref}" target="_blank" rel="noreferrer">${label}</a>`;
    });
}

export function markdownToHtml(value) {
  const blocks = String(value).split(/```/);
  return blocks
    .map((block, index) => {
      if (index % 2 === 1) {
        const lines = block.replace(/^\w+\n/, "").replace(/\n$/, "");
        return `<pre><code>${escapeHtml(lines)}</code></pre>`;
      }
      const lines = block.split(/\n/);
      const html = [];
      let list = [];
      const flushList = () => {
        if (!list.length) return;
        html.push(`<ul>${list.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</ul>`);
        list = [];
      };
      lines.forEach((line) => {
        const listMatch = line.match(/^\s*[-*]\s+(.+)$/);
        if (listMatch) {
          list.push(listMatch[1]);
          return;
        }
        flushList();
        if (!line.trim()) return;
        const heading = line.match(/^(#{1,3})\s+(.+)$/);
        if (heading) {
          const level = heading[1].length + 2;
          html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
          return;
        }
        html.push(`<p>${inlineMarkdown(line)}</p>`);
      });
      flushList();
      return html.join("");
    })
    .join("");
}
