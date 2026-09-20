import { readFileSync } from "node:fs";

const VOID_TAGS = new Set([
  "area",
  "base",
  "br",
  "col",
  "embed",
  "hr",
  "img",
  "input",
  "link",
  "meta",
  "param",
  "source",
  "track",
  "wbr",
]);

const TOKENS =
  /<!--[\s\S]*?-->|<![a-zA-Z][^>]*>|<\/([a-zA-Z][\w-]*)\s*>|<([a-zA-Z][\w-]*)((?:\s+[^\s>"'=/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>|([^<]+)/g;

const ATTRIBUTES = /([^\s>"'=/]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;

let active = null;
let instances = 0;

function dataKey(name) {
  return name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}

class ShimText {
  constructor(value) {
    this.nodeName = "#text";
    this.children = [];
    this.attributes = {};
    this.parentNode = null;
    this.value = value;
  }

  get textContent() {
    return this.value;
  }
}

class ShimElement {
  constructor(tag) {
    this.nodeName = tag.toUpperCase();
    this.className = "";
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.listeners = {};
    this.parentNode = null;
    this.id = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.tabIndex = 0;
    this.scrollTop = 0;
  }

  set textContent(value) {
    for (const child of this.children) {
      child.parentNode = null;
    }
    const text = new ShimText(String(value));
    text.parentNode = this;
    this.children = [text];
  }

  get textContent() {
    return this.children.map((child) => child.textContent).join("");
  }

  get firstChild() {
    return this.children.length > 0 ? this.children[0] : null;
  }

  set innerHTML(value) {
    for (const child of this.children) {
      child.parentNode = null;
    }
    this.children = [];
    this.append(...parseHtml(String(value)).children);
  }

  get innerHTML() {
    return this.textContent;
  }

  setAttribute(name, value) {
    const text = String(value);
    this.attributes[name] = text;
    if (name === "id") {
      this.id = text;
    } else if (name === "class") {
      this.className = text;
    } else if (name === "hidden") {
      this.hidden = true;
    } else if (name === "checked") {
      this.checked = true;
    } else if (name === "value") {
      this.value = text;
    } else if (name.startsWith("data-")) {
      this.dataset[dataKey(name.slice(5))] = text;
    }
  }

  getAttribute(name) {
    return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null;
  }

  append(...nodes) {
    for (const node of nodes) {
      node.parentNode = this;
      this.children.push(node);
    }
  }

  removeChild(node) {
    const index = this.children.indexOf(node);
    if (index >= 0) {
      this.children.splice(index, 1);
      node.parentNode = null;
    }
    return node;
  }

  querySelectorAll(selector) {
    const spec = compile(selector);
    return walk(this)
      .slice(1)
      .filter((node) => matchesSelector(node, spec));
  }

  closest(selector) {
    const spec = compile(selector);
    let node = this;
    while (node) {
      if (matchesSelector(node, spec)) {
        return node;
      }
      node = node.parentNode;
    }
    return null;
  }

  addEventListener(type, handler) {
    this.listeners[type] = [...(this.listeners[type] ?? []), handler];
  }

  dispatchEvent(event) {
    let node = this;
    while (node) {
      for (const handler of node.listeners?.[event.type] ?? []) {
        event.currentTarget = node;
        handler(event);
      }
      node = node.parentNode;
    }
    return !event.defaultPrevented;
  }

  focus() {
    if (active) {
      active.activeElement = this;
    }
  }
}

class ShimDocument {
  constructor(root) {
    this.root = root;
    this.hidden = false;
    this.activeElement = null;
    this.listeners = {};
  }

  createElement(tag) {
    return new ShimElement(tag);
  }

  getElementById(id) {
    return walk(this.root).find((node) => node.id === id) ?? null;
  }

  addEventListener(type, handler) {
    this.listeners[type] = [...(this.listeners[type] ?? []), handler];
  }

  dispatchEvent(event) {
    for (const handler of this.listeners[event.type] ?? []) {
      handler(event);
    }
    return true;
  }
}

function compile(selector) {
  const tag = /^[a-zA-Z][\w-]*/.exec(selector);
  const rest = selector.slice(tag ? tag[0].length : 0);
  return {
    tag: tag ? tag[0].toUpperCase() : null,
    classes: [...rest.matchAll(/\.([\w-]+)/g)].map((match) => match[1]),
    attrs: [...rest.matchAll(/\[([\w-]+)(?:=["']?([^\]"']*)["']?)?\]/g)].map((match) => [
      match[1],
      match[2],
    ]),
  };
}

function matchesSelector(node, spec) {
  if (!(node instanceof ShimElement)) {
    return false;
  }
  if (spec.tag && node.nodeName !== spec.tag) {
    return false;
  }
  const classes = String(node.className ?? "").split(/\s+/);
  if (spec.classes.some((name) => !classes.includes(name))) {
    return false;
  }
  return spec.attrs.every(([name, value]) => {
    const actual = node.getAttribute(name);
    return actual !== null && (value === undefined || actual === value);
  });
}

function parseHtml(source) {
  const root = new ShimElement("body");
  const stack = [root];
  for (const token of source.matchAll(TOKENS)) {
    const [text, closing, opening, attributes, selfClosing, content] = token;
    if (closing) {
      if (stack.length > 1 && stack[stack.length - 1].nodeName === closing.toUpperCase()) {
        stack.pop();
      }
    } else if (opening) {
      const node = new ShimElement(opening);
      for (const attribute of (attributes ?? "").matchAll(ATTRIBUTES)) {
        node.setAttribute(attribute[1], attribute[2] ?? attribute[3] ?? attribute[4] ?? "");
      }
      stack[stack.length - 1].append(node);
      if (!selfClosing && !VOID_TAGS.has(opening.toLowerCase())) {
        stack.push(node);
      }
    } else if (content !== undefined && content.trim() !== "") {
      const node = new ShimText(content);
      node.parentNode = stack[stack.length - 1];
      stack[stack.length - 1].children.push(node);
    } else if (text.startsWith("<!--") || text.startsWith("<!")) {
      continue;
    }
  }
  return root;
}

export function installDom() {
  active = new ShimDocument(new ShimElement("body"));
  globalThis.Node = ShimElement;
  globalThis.document = active;
  return active;
}

export function installPage(path) {
  active = new ShimDocument(parseHtml(readFileSync(path, "utf-8")));
  globalThis.Node = ShimElement;
  globalThis.document = active;
  return active;
}

function abortError() {
  const error = new Error("The operation was aborted.");
  error.name = "AbortError";
  return error;
}

export function installFetch(respond) {
  const calls = [];
  globalThis.fetch = (path, options = {}) => {
    const signal = options.signal;
    const call = { path, signal, settled: false };
    calls.push(call);
    return new Promise((resolve, reject) => {
      const stop = () => reject(abortError());
      if (signal) {
        if (signal.aborted) {
          stop();
          return;
        }
        signal.addEventListener("abort", stop);
      }
      call.settle = (outcome) => {
        if (signal?.aborted || call.settled) {
          return;
        }
        call.settled = true;
        if (outcome instanceof Error) {
          reject(outcome);
          return;
        }
        resolve({
          ok: outcome.ok ?? true,
          status: outcome.status ?? 200,
          json: async () => outcome.body,
        });
      };
      call.force = (outcome) => {
        call.settled = true;
        resolve({
          ok: outcome.ok ?? true,
          status: outcome.status ?? 200,
          json: async () => outcome.body,
        });
      };
      const answer = respond(path, call);
      if (answer !== undefined) {
        call.settle(answer);
      }
    });
  };
  return calls;
}

export const PAGE_PATH = new URL(
  "../../src/dryheave/resources/viewer/index.html",
  import.meta.url,
);

const VIEWER_URL = new URL("../../src/dryheave/resources/viewer/viewer.js", import.meta.url);

export async function mountViewer() {
  const timers = [];
  const real = globalThis.setInterval;
  globalThis.setInterval = (handler) => {
    timers.push(handler);
    return timers.length;
  };
  try {
    await import(`${VIEWER_URL.href}?instance=${(instances += 1)}`);
  } finally {
    globalThis.setInterval = real;
  }
  return {
    async tick() {
      for (const handler of timers) {
        handler();
      }
      await flush();
    },
  };
}

export function flush() {
  return new Promise((resolve) => {
    setTimeout(resolve, 0);
  });
}

export function fire(node, type, extra = {}) {
  const event = {
    type,
    target: node,
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
    ...extra,
  };
  node.dispatchEvent(event);
  return event;
}

export function walk(node) {
  const found = [node];
  for (const child of node.children ?? []) {
    found.push(...walk(child));
  }
  return found;
}

export function renderedText(nodes) {
  return (Array.isArray(nodes) ? nodes : [nodes])
    .filter(Boolean)
    .map((node) => node.textContent)
    .join("\n");
}

export function matching(nodes, predicate) {
  return (Array.isArray(nodes) ? nodes : [nodes])
    .filter(Boolean)
    .flatMap((node) => walk(node))
    .filter(predicate);
}
