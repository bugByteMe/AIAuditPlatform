function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function unescapeHtml(value) {
  const entities = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" };
  return value.replace(/&amp;|&lt;|&gt;|&quot;|&#39;/g, (entity) => entities[entity]);
}

export function workspaceLinkPath(href) {
  const rawHref = String(href || "").trim().replace(/^<|>$/g, "");
  const prefix = ["/workspace/", "sandbox:/workspace/", "file:///workspace/"]
    .find((candidate) => rawHref.startsWith(candidate));
  if (!prefix) return null;
  let path;
  try {
    path = decodeURIComponent(rawHref.slice(prefix.length));
  } catch {
    return null;
  }
  if (!path || path.includes("\\") || /[\x00-\x1f\x7f]/.test(path) || path.split("/").some((part) => !part || part === "." || part === "..")) return null;
  return path;
}

const MATH_COMMANDS = {
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", theta: "θ", lambda: "λ", mu: "μ", pi: "π", rho: "ρ", sigma: "σ", tau: "τ", phi: "φ", omega: "ω",
  Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Pi: "Π", Sigma: "Σ", Phi: "Φ", Omega: "Ω",
  times: "×", cdot: "·", pm: "±", le: "≤", leq: "≤", ge: "≥", geq: "≥", ne: "≠", neq: "≠", approx: "≈", in: "∈", notin: "∉", infinity: "∞", infty: "∞", sum: "∑", prod: "∏", int: "∫", partial: "∂", nabla: "∇", to: "→", rightarrow: "→", leftarrow: "←",
};

class MathParser {
  constructor(source) {
    this.source = String(source || "");
    this.index = 0;
    this.depth = 0;
  }

  parse(stop = "") {
    if (this.depth > 24) throw new Error("formula nesting is too deep");
    const nodes = [];
    while (this.index < this.source.length && this.source[this.index] !== stop) {
      if (/\s/.test(this.source[this.index])) {
        this.index += 1;
        nodes.push('<mspace width="0.25em"></mspace>');
        continue;
      }
      let base = this.atom();
      let sub = "";
      let sup = "";
      while (this.source[this.index] === "_" || this.source[this.index] === "^") {
        const kind = this.source[this.index++];
        const script = this.script();
        if (kind === "_") sub = script;
        else sup = script;
      }
      if (sub && sup) base = `<msubsup>${base}${sub}${sup}</msubsup>`;
      else if (sub) base = `<msub>${base}${sub}</msub>`;
      else if (sup) base = `<msup>${base}${sup}</msup>`;
      nodes.push(base);
    }
    if (stop) {
      if (this.source[this.index] !== stop) throw new Error("unclosed formula group");
      this.index += 1;
    }
    return nodes.join("");
  }

  script() {
    if (this.source[this.index] === "{") {
      this.index += 1;
      this.depth += 1;
      const result = `<mrow>${this.parse("}")}</mrow>`;
      this.depth -= 1;
      return result;
    }
    return this.atom();
  }

  requiredGroup() {
    if (this.source[this.index] !== "{") throw new Error("formula command requires a group");
    this.index += 1;
    this.depth += 1;
    const result = `<mrow>${this.parse("}")}</mrow>`;
    this.depth -= 1;
    return result;
  }

  rawGroup() {
    if (this.source[this.index] !== "{") throw new Error("formula command requires a group");
    const start = ++this.index;
    let nesting = 1;
    while (this.index < this.source.length && nesting) {
      if (this.source[this.index] === "{") nesting += 1;
      if (this.source[this.index] === "}") nesting -= 1;
      this.index += 1;
    }
    if (nesting) throw new Error("unclosed formula group");
    return this.source.slice(start, this.index - 1);
  }

  atom() {
    const char = this.source[this.index];
    if (char === "{") {
      this.index += 1;
      this.depth += 1;
      const result = `<mrow>${this.parse("}")}</mrow>`;
      this.depth -= 1;
      return result;
    }
    if (char === "\\") {
      this.index += 1;
      const match = this.source.slice(this.index).match(/^[A-Za-z]+/);
      if (!match) return `<mo>${escapeHtml(this.source[this.index++] || "\\")}</mo>`;
      const command = match[0];
      this.index += command.length;
      if (command === "left" || command === "right") return this.atom();
      if (command === "frac") return `<mfrac>${this.requiredGroup()}${this.requiredGroup()}</mfrac>`;
      if (command === "sqrt") return `<msqrt>${this.requiredGroup()}</msqrt>`;
      if (command === "text" || command === "mathrm") return `<mtext>${escapeHtml(this.rawGroup())}</mtext>`;
      const symbol = MATH_COMMANDS[command];
      if (symbol) return /[A-Za-zα-ωΑ-Ω]/.test(symbol) ? `<mi>${symbol}</mi>` : `<mo>${symbol}</mo>`;
      return `<mtext>\\${escapeHtml(command)}</mtext>`;
    }
    if (/\d/.test(char)) {
      const match = this.source.slice(this.index).match(/^\d+(?:\.\d+)?/)[0];
      this.index += match.length;
      return `<mn>${match}</mn>`;
    }
    this.index += 1;
    if (/[A-Za-z]/.test(char)) return `<mi>${char}</mi>`;
    if (/[+\-*/=(),.<>|]/.test(char)) return `<mo>${escapeHtml(char)}</mo>`;
    return `<mtext>${escapeHtml(char)}</mtext>`;
  }
}

export function formulaToHtml(source, display = false) {
  const formula = String(source || "").trim();
  if (!formula || formula.length > 4_000) return `<code class="math-fallback">${escapeHtml(formula)}</code>`;
  try {
    const parser = new MathParser(formula);
    const body = parser.parse();
    if (parser.index !== formula.length) throw new Error("formula was not fully parsed");
    return `<math class="chat-math${display ? " chat-math-display" : ""}" xmlns="http://www.w3.org/1998/Math/MathML" display="${display ? "block" : "inline"}"><mrow>${body}</mrow></math>`;
  } catch {
    return `<code class="math-fallback">${escapeHtml(formula)}</code>`;
  }
}

function inlineMarkdown(value) {
  const protectedItems = [];
  const protect = (html) => {
    const marker = `\u0000${protectedItems.length}\u0000`;
    protectedItems.push(html);
    return marker;
  };
  let source = String(value)
    .replace(/`([^`]+)`/g, (match, code) => protect(`<code>${escapeHtml(code)}</code>`))
    .replace(/\\\((.+?)\\\)/g, (match, formula) => protect(formulaToHtml(formula)))
    .replace(/(^|[^\\])\$([^\s$\n](?:[^$\n]*?[^\s$\n])?)\$(?!\d)/g, (match, prefix, formula) => `${prefix}${protect(formulaToHtml(formula))}`);
  source = escapeHtml(source)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>")
    .replace(/\[([^\]]+)\]\(\s*(&lt;|<)?((?:https?:\/\/|\/workspace\/|sandbox:\/workspace\/|file:\/\/\/workspace\/)[^)\n]+?)(&gt;|>)?\s*\)/g, (match, label, open, escapedHref, close) => {
      if (Boolean(open) !== Boolean(close)) return match;
      const href = unescapeHtml(escapedHref).trim();
      const path = workspaceLinkPath(href);
      if (path !== null) return `<a href="#" data-workspace-file="${escapeHtml(path)}">${label}</a>`;
      if (/\s/.test(href)) return match;
      try {
        const external = new URL(href);
        if (!["http:", "https:"].includes(external.protocol)) return match;
      } catch {
        return match;
      }
      return `<a href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${label}</a>`;
    });
  return source.replace(/\u0000(\d+)\u0000/g, (match, index) => protectedItems[Number(index)] || "");
}

function splitTableRow(line) {
  const source = String(line).trim().replace(/^\|/, "").replace(/\|$/, "");
  const cells = [];
  let cell = "";
  let escaped = false;
  let inCode = false;
  for (const char of source) {
    if (escaped) {
      cell += char;
      escaped = false;
    } else if (char === "\\") {
      escaped = true;
    } else if (char === "`") {
      inCode = !inCode;
      cell += char;
    } else if (char === "|" && !inCode) {
      cells.push(cell.trim());
      cell = "";
    } else {
      cell += char;
    }
  }
  if (escaped) cell += "\\";
  cells.push(cell.trim());
  return cells;
}

function tableAlignments(line) {
  const cells = splitTableRow(line);
  if (!cells.length || cells.some((cell) => !/^:?-{3,}:?$/.test(cell))) return null;
  return cells.map((cell) => cell.startsWith(":") && cell.endsWith(":") ? "center" : cell.endsWith(":") ? "right" : "left");
}

function tableHtml(headerLine, delimiterLine, bodyLines) {
  const alignments = tableAlignments(delimiterLine);
  const headers = splitTableRow(headerLine);
  const cell = (tag, value, index) => `<${tag} class="align-${alignments[index] || "left"}">${inlineMarkdown(value)}</${tag}>`;
  const head = `<thead><tr>${headers.map((value, index) => cell("th", value, index)).join("")}</tr></thead>`;
  const body = bodyLines.map((line) => {
    const values = splitTableRow(line);
    return `<tr>${headers.map((unused, index) => cell("td", values[index] || "", index)).join("")}</tr>`;
  }).join("");
  return `<div class="event-table-wrap"><table>${head}<tbody>${body}</tbody></table></div>`;
}

function markdownBlock(value) {
  const lines = String(value).split(/\n/);
  const html = [];
  let list = [];
  const flushList = () => {
    if (!list.length) return;
    html.push(`<ul>${list.map((item) => `<li>${inlineMarkdown(item)}</li>`).join("")}</ul>`);
    list = [];
  };
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const trimmed = line.trim();
    const blockMath = trimmed.match(/^\$\$(.*?)\$\$$/) || trimmed.match(/^\\\[(.*?)\\\]$/);
    if (blockMath) {
      flushList();
      html.push(formulaToHtml(blockMath[1], true));
      continue;
    }
    if (trimmed === "$$" || trimmed === "\\[") {
      flushList();
      const closing = trimmed === "$$" ? "$$" : "\\]";
      const formula = [];
      index += 1;
      while (index < lines.length && lines[index].trim() !== closing) formula.push(lines[index++]);
      html.push(index < lines.length ? formulaToHtml(formula.join("\n"), true) : `<code class="math-fallback">${escapeHtml([trimmed, ...formula].join("\n"))}</code>`);
      continue;
    }
    const delimiterLine = lines[index + 1] || "";
    const alignments = tableAlignments(delimiterLine);
    if (alignments && splitTableRow(line).length === alignments.length) {
      flushList();
      const rows = [];
      index += 2;
      while (index < lines.length && lines[index].includes("|") && splitTableRow(lines[index]).length > 1) rows.push(lines[index++]);
      index -= 1;
      html.push(tableHtml(line, delimiterLine, rows));
      continue;
    }
    const listMatch = line.match(/^\s*[-*]\s+(.+)$/);
    if (listMatch) {
      list.push(listMatch[1]);
      continue;
    }
    flushList();
    if (!trimmed) continue;
    const heading = line.match(/^(#{1,3})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length + 2;
      html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }
    html.push(`<p>${inlineMarkdown(line)}</p>`);
  }
  flushList();
  return html.join("");
}

export function markdownToHtml(value) {
  return String(value).split(/```/).map((block, index) => {
    if (index % 2 === 1) {
      const lines = block.replace(/^\w+\n/, "").replace(/\n$/, "");
      return `<pre><code>${escapeHtml(lines)}</code></pre>`;
    }
    return markdownBlock(block);
  }).join("");
}
