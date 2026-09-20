import assert from "node:assert/strict";
import { test } from "node:test";

import { installDom, matching, renderedText, walk } from "./dom-shim.mjs";

installDom();

const {
  badge,
  block,
  bullets,
  clear,
  definitions,
  el,
  heading,
  idChip,
  note,
  replace,
  section,
  table,
} = await import("../../src/dryheave/resources/viewer/dom.mjs");

const HOSTILE = "<img src=x onerror=alert(1)>";

test("el writes text as a single text node and never as markup", () => {
  const node = el("span", { text: HOSTILE });
  assert.equal(node.nodeName, "SPAN");
  assert.equal(node.children.length, 1);
  assert.equal(node.children[0].nodeName, "#text");
  assert.equal(node.textContent, HOSTILE);
  assert.equal(walk(node).filter((child) => child.nodeName !== "#text").length, 1);
});

test("el coerces non-string text and omits it when null or undefined", () => {
  assert.equal(el("span", { text: 0 }).textContent, "0");
  assert.equal(el("span", { text: false }).textContent, "false");
  assert.equal(el("span", { text: null }).children.length, 0);
  assert.equal(el("span", { text: undefined }).children.length, 0);
});

test("el sets only the attributes it is given and drops empty ones", () => {
  const node = el("button", {
    className: "pick",
    attrs: { type: "button", "data-attempt": "a1", "aria-current": null, title: undefined, hidden: false },
  });
  assert.deepEqual(Object.keys(node.attributes).sort(), ["data-attempt", "type"]);
  assert.equal(node.getAttribute("type"), "button");
  assert.equal(node.dataset.attempt, "a1");
  assert.equal(node.className, "pick");
});

test("el appends only truthy children in order", () => {
  const first = el("i", { text: "one" });
  const second = el("i", { text: "two" });
  const parent = el("p", {}, [first, null, second, undefined, false]);
  assert.deepEqual(parent.children, [first, second]);
});

test("clear detaches every child and replace installs a new set", () => {
  const parent = el("div", {}, [el("i", { text: "old" })]);
  const old = parent.children[0];
  clear(parent);
  assert.equal(parent.children.length, 0);
  assert.equal(old.parentNode, null);
  const fresh = el("i", { text: "new" });
  replace(parent, [fresh, null]);
  assert.deepEqual(parent.children, [fresh]);
  assert.equal(parent.textContent, "new");
});

test("definitions pairs labels with values and renders unknown for missing ones", () => {
  const list = definitions([
    ["Stage", "finished"],
    ["Seed", null],
    ["Mode", undefined],
  ]);
  assert.equal(list.nodeName, "DL");
  assert.deepEqual(
    list.children.map((child) => [child.nodeName, child.textContent]),
    [
      ["DT", "Stage"],
      ["DD", "finished"],
      ["DT", "Seed"],
      ["DD", "unknown"],
      ["DT", "Mode"],
      ["DD", "unknown"],
    ],
  );
});

test("bullets renders one list item per value", () => {
  const list = bullets(["first", HOSTILE]);
  assert.equal(list.nodeName, "UL");
  assert.deepEqual(
    list.children.map((child) => child.textContent),
    ["first", HOSTILE],
  );
  assert.equal(matching(list, (node) => node.nodeName === "IMG").length, 0);
});

test("table builds a captioned head and body and wraps element cells in a data cell", () => {
  const cell = el("button", { text: "pick" });
  const scroller = table("Attempts.", ["Attempt", "Trial"], [[cell, "trial-1"]]);
  assert.equal(scroller.className, "scroller");
  const grid = scroller.children[0];
  assert.deepEqual(
    grid.children.map((child) => child.nodeName),
    ["CAPTION", "THEAD", "TBODY"],
  );
  assert.equal(grid.children[0].textContent, "Attempts.");
  const headers = matching(grid.children[1], (node) => node.nodeName === "TH");
  assert.deepEqual(
    headers.map((header) => [header.textContent, header.getAttribute("scope")]),
    [
      ["Attempt", "col"],
      ["Trial", "col"],
    ],
  );
  const cells = matching(grid.children[2], (node) => node.nodeName === "TD");
  assert.equal(cells.length, 2);
  assert.deepEqual(cells[0].children, [cell]);
  assert.equal(cells[1].textContent, "trial-1");
});

test("block keeps content as preformatted text and renders nothing for null", () => {
  const node = block("line one\n<b>two</b>");
  assert.equal(node.nodeName, "PRE");
  assert.equal(node.className, "block");
  assert.equal(node.textContent, "line one\n<b>two</b>");
  assert.equal(matching(node, (child) => child.nodeName === "B").length, 0);
  assert.equal(block(null).textContent, "");
});

test("idChip shows the label, any names and the full id, and titles the whole chip", () => {
  const value = "a".repeat(64);
  const chip = idChip("experiment", value, ["nightly", "baseline"]);
  assert.equal(chip.className, "chip");
  assert.deepEqual(
    chip.children.map((child) => [child.className, child.textContent]),
    [
      ["chip-label", "experiment"],
      ["chip-name", "nightly, baseline"],
      ["chip-id", value],
    ],
  );
  assert.equal(chip.getAttribute("title"), `experiment: ${value} (label nightly, baseline)`);
});

test("idChip omits the name span when the store records none", () => {
  const value = "b".repeat(32);
  const chip = idChip("run", value);
  assert.deepEqual(
    chip.children.map((child) => child.className),
    ["chip-label", "chip-id"],
  );
  assert.equal(chip.getAttribute("title"), `run: ${value}`);
});

test("idChip reports an absent identity as unknown", () => {
  const chip = idChip("capture", null, ["ignored"]);
  assert.equal(chip.className, "chip unknown");
  assert.equal(chip.textContent, "capture: unknown");
  assert.equal(chip.getAttribute("title"), null);
});

test("badge, heading, note and section carry their content as text", () => {
  assert.equal(badge("truncated prefix", "warn").className, "badge warn");
  assert.equal(badge("plain").className, "badge");
  assert.equal(heading(4, "Audit").nodeName, "H4");
  assert.equal(heading(3, "Audit").textContent, "Audit");
  assert.equal(note("careful", "note warn").className, "note warn");
  assert.equal(note("plain").className, "note");
  const card = section("Audit", [note("body")]);
  assert.equal(card.nodeName, "SECTION");
  assert.equal(card.className, "card");
  assert.equal(renderedText(card), "Auditbody");
});
