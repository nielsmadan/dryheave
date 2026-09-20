import { chipTitle } from "./format.mjs";

export function el(tag, options = {}, children = []) {
  const node = document.createElement(tag);
  if (options.className) {
    node.className = options.className;
  }
  if (options.text !== undefined && options.text !== null) {
    node.textContent = String(options.text);
  }
  for (const [name, value] of Object.entries(options.attrs ?? {})) {
    if (value === null || value === undefined || value === false) {
      continue;
    }
    node.setAttribute(name, String(value));
  }
  for (const child of children) {
    if (child) {
      node.append(child);
    }
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

export function replace(node, children) {
  clear(node);
  for (const child of children) {
    if (child) {
      node.append(child);
    }
  }
}

export function heading(level, value) {
  return el(`h${level}`, { text: value });
}

export function note(value, className = "note") {
  return el("p", { className, text: value });
}

export function idChip(label, value, names = []) {
  if (!value) {
    return el("span", { className: "chip unknown", text: `${label}: unknown` });
  }
  const children = [el("span", { className: "chip-label", text: label })];
  if (names.length > 0) {
    children.push(el("span", { className: "chip-name", text: names.join(", ") }));
  }
  children.push(el("code", { className: "chip-id", text: value }));
  return el("span", { className: "chip", attrs: { title: chipTitle(label, value, names) } }, children);
}

export function badge(value, tone = "") {
  return el("span", { className: tone ? `badge ${tone}` : "badge", text: value });
}

export function definitions(entries) {
  const list = el("dl", { className: "definitions" });
  for (const [label, value] of entries) {
    list.append(el("dt", { text: label }));
    list.append(el("dd", { text: value === null || value === undefined ? "unknown" : value }));
  }
  return list;
}

export function bullets(values, className = "bullets") {
  const list = el("ul", { className });
  for (const value of values) {
    list.append(el("li", { text: value }));
  }
  return list;
}

export function table(caption, headers, rows) {
  const head = el("tr");
  for (const header of headers) {
    head.append(el("th", { text: header, attrs: { scope: "col" } }));
  }
  const body = el("tbody");
  for (const row of rows) {
    const line = el("tr");
    for (const cell of row) {
      line.append(cell instanceof Node ? el("td", {}, [cell]) : el("td", { text: cell }));
    }
    body.append(line);
  }
  return el("div", { className: "scroller" }, [
    el("table", {}, [
      el("caption", { text: caption }),
      el("thead", {}, [head]),
      body,
    ]),
  ]);
}

export function block(value) {
  const node = el("pre", { className: "block" });
  node.textContent = String(value ?? "");
  return node;
}

export function section(title, children) {
  return el("section", { className: "card" }, [heading(3, title), ...children]);
}
