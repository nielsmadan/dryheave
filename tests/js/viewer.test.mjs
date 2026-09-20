import assert from "node:assert/strict";
import { test } from "node:test";

import {
  PAGE_PATH,
  fire,
  flush,
  installFetch,
  installPage,
  matching,
  mountViewer,
  renderedText,
} from "./dom-shim.mjs";

const RUN = "a".repeat(32);
const OTHER = "b".repeat(32);
const EXPERIMENT = "e".repeat(64);
const CASE = "c".repeat(64);
const REPORT_ID = "d".repeat(64);
const CURSOR = "9".repeat(64);

function runRow(id, extra = {}) {
  return {
    run_id: id,
    created_at: "2026-09-19T20:12:31+00:00",
    mode: "offline-fixture",
    experiment_id: EXPERIMENT,
    error: null,
    truncated: false,
    durable_sequence: 4,
    observed_events: 4,
    attempts: 2,
    finished: 2,
    ...extra,
  };
}

function attempt(id, trial) {
  return {
    attempt_id: id,
    trial_id: trial,
    variant: "haiku",
    stage: "finished",
    mode: "offline-fixture",
    raw_evidence: "available",
    capture_id: null,
    quarantine_id: null,
    assessment_id: null,
    pairing_id: null,
    current_exclusions: [],
    roles: [],
    result: null,
  };
}

function report(id, extra = {}) {
  return {
    run_id: id,
    experiment_id: EXPERIMENT,
    compatibility_id: null,
    durable_sequence: 4,
    scheduled_trials: 2,
    unstarted_trials: [],
    input_error: null,
    attempts: [attempt("attempt-1", "trial-1"), attempt("attempt-2", "trial-2")],
    groups: [],
    portable: false,
    omissions: [],
    limitations: [],
    ...extra,
  };
}

function progress(id, stage = "finished") {
  return {
    run_id: id,
    experiment_id: EXPERIMENT,
    created_at: "2026-09-19T20:12:31+00:00",
    durable_sequence: 4,
    mode: "offline-fixture",
    attempts: [
      {
        attempt_id: "attempt-1",
        trial_id: "trial-1",
        stage,
        capture_id: null,
        quarantine_id: null,
        assessment_id: null,
      },
    ],
  };
}

function calibrationEntry(id) {
  return {
    calibration_id: id,
    case_id: CASE,
    verification: "verified",
    status: "demonstrated",
    created_at: null,
    evidence_complete: true,
    omissions: [],
    reason: null,
  };
}

function calibrations(id, cursor, entries) {
  return {
    run_id: id,
    experiment_id: EXPERIMENT,
    input_error: null,
    calibrations: { [CASE]: entries },
    scan: {
      complete: cursor === null,
      scanned: 2,
      unreadable: 0,
      attempted: entries.length,
      verified: entries.length,
      next_cursor: cursor,
    },
  };
}

function dialogue(text) {
  return {
    status: "ok",
    reason: null,
    reason_code: null,
    capture_id: null,
    quarantine_id: null,
    quarantined: false,
    interaction_coverage: null,
    capture_errors: [],
    evidence_omissions: [],
    messages: [
      { role: "user", text: `prompt: ${text}` },
      { role: "assistant", text },
    ],
  };
}

function patch() {
  return {
    status: "ok",
    reason: null,
    reason_code: null,
    name: "final.patch",
    workspace_complete: true,
    bytes: 4,
    sha256: "f".repeat(64),
    encoding: "utf-8",
    omissions: [],
    content: "diff",
  };
}

function evidenceListing() {
  return {
    status: "ok",
    reason: null,
    reason_code: null,
    assessment_id: null,
    evidence_id: null,
    files: ["stdout.txt"],
    omissions: [],
    complete: true,
  };
}

function evidenceFile(name, content) {
  return {
    name,
    status: "ok",
    reason: null,
    reason_code: null,
    bytes: content.length,
    sha256: "f".repeat(64),
    encoding: "utf-8",
    content,
  };
}

function listing(rows, extra = {}) {
  return { runs: rows, offset: 0, limit: 25, total: rows.length, ...extra };
}

function store(overrides = {}) {
  return {
    target: { target: null },
    labels: { labels: { [EXPERIMENT]: ["nightly"] } },
    listing: listing([runRow(RUN)]),
    reports: { [RUN]: report(RUN), [OTHER]: report(OTHER), [REPORT_ID]: report(REPORT_ID) },
    calibrations: calibrations(RUN, null, [calibrationEntry("1".repeat(64))]),
    dialogue: dialogue("hello"),
    comparison: null,
    ...overrides,
  };
}

function answer(path, data) {
  const [route] = path.split("?");
  if (route === "/api/target") {
    return { body: data.target };
  }
  if (route === "/api/labels") {
    return { body: data.labels };
  }
  if (route === "/api/runs") {
    return { body: data.listing };
  }
  if (route === "/api/compare") {
    return { body: data.comparison };
  }
  const detail = /^\/api\/(runs|reports)\/([0-9a-f]+)\/attempts\/([\w-]+)\/(\w+)(?:\/(.+))?$/.exec(
    route,
  );
  if (detail) {
    const [, , , , kind, name] = detail;
    if (kind === "dialogue") {
      return { body: data.dialogue };
    }
    if (kind === "patch") {
      return { body: patch() };
    }
    return { body: name ? evidenceFile(name, `bytes of ${name}`) : evidenceListing() };
  }
  const scoped = /^\/api\/(?:runs|cases)\/([0-9a-f]+)\/(progress|calibrations)$/.exec(route);
  if (scoped) {
    return {
      body: scoped[2] === "progress" ? progress(scoped[1], data.progressStage) : data.calibrations,
    };
  }
  const source = /^\/api\/(?:runs|reports)\/([0-9a-f]+)$/.exec(route);
  if (source) {
    return { body: data.reports[source[1]] };
  }
  throw new Error(`unrouted path ${path}`);
}

async function open(overrides = {}) {
  const data = store(overrides);
  const doc = installPage(PAGE_PATH);
  const held = [];
  const state = { deferred: new Set(), failures: new Map() };
  const calls = installFetch((path, call) => {
    const [route] = path.split("?");
    if (state.deferred.has(route)) {
      held.push(call);
      return undefined;
    }
    const failure = state.failures.get(route);
    if (failure) {
      return failure;
    }
    return answer(path, data);
  });
  const viewer = await mountViewer();
  return { data, doc, calls, held, state, viewer, paths: () => calls.map((call) => call.path) };
}

function node(doc, id) {
  return doc.getElementById(id);
}

function runButtons(doc) {
  return node(doc, "run-list").querySelectorAll("button[data-run]");
}

function attemptButtons(doc) {
  return node(doc, "attempt-list").querySelectorAll("button[data-attempt]");
}

test("the page loads its target, labels and listing, then opens the first readable run", async () => {
  const page = await open();
  assert.deepEqual(page.paths().slice(0, 3), [
    "/api/target",
    "/api/labels",
    "/api/runs?offset=0&limit=25",
  ]);
  assert.ok(page.paths().includes(`/api/runs/${RUN}`));
  assert.ok(page.paths().includes(`/api/runs/${RUN}/progress`));
  assert.ok(page.paths().includes(`/api/runs/${RUN}/calibrations`));
  assert.equal(
    node(page.doc, "target").textContent,
    "No focused target; showing every retained run in this store.",
  );
  assert.equal(runButtons(page.doc).length, 1);
  assert.match(renderedText(node(page.doc, "panel-overview")), new RegExp(RUN));
});

test("an unchanged poll leaves the focused run button and its focus in place", async () => {
  const page = await open();
  const button = runButtons(page.doc)[0];
  button.focus();
  assert.equal(page.doc.activeElement, button);
  const before = page.calls.length;
  await page.viewer.tick();
  assert.ok(page.calls.length > before);
  assert.equal(runButtons(page.doc)[0], button);
  assert.equal(page.doc.activeElement, button);
  assert.equal(button.parentNode.parentNode.parentNode, node(page.doc, "run-list"));
});

test("an unchanged poll leaves a scrolled transcript block and its scroll offset alone", async () => {
  const page = await open();
  const blocks = matching([node(page.doc, "panel-detail")], (item) => item.className === "block");
  assert.ok(blocks.length > 0);
  blocks[0].scrollTop = 120;
  await page.viewer.tick();
  const after = matching([node(page.doc, "panel-detail")], (item) => item.className === "block");
  assert.equal(after[0], blocks[0]);
  assert.equal(after[0].scrollTop, 120);
});

test("an unchanged poll leaves the polled progress panel in place", async () => {
  const page = await open();
  const tables = matching([node(page.doc, "panel-progress")], (item) => item.nodeName === "TABLE");
  assert.equal(tables.length, 1);
  const before = page.calls.length;
  await page.viewer.tick();
  assert.ok(page.paths().slice(before).includes(`/api/runs/${RUN}/progress`));
  const after = matching([node(page.doc, "panel-progress")], (item) => item.nodeName === "TABLE");
  assert.equal(after.length, 1);
  assert.equal(after[0], tables[0]);
});

test("a poll that changes progress rebuilds the progress panel", async () => {
  const page = await open();
  const tables = matching([node(page.doc, "panel-progress")], (item) => item.nodeName === "TABLE");
  page.data.progressStage = "assessed";
  await page.viewer.tick();
  const after = matching([node(page.doc, "panel-progress")], (item) => item.nodeName === "TABLE");
  assert.equal(after.length, 1);
  assert.notEqual(after[0], tables[0]);
  assert.match(renderedText(after[0]), /assessed/);
});

test("dialogue turns render the two roles the API can emit", async () => {
  const page = await open();
  const logs = matching([node(page.doc, "panel-detail")], (item) => item.className === "dialogue");
  assert.equal(logs.length, 1);
  const turns = matching(logs, (item) => item.nodeName === "LI");
  assert.deepEqual(
    turns.map((item) => item.className),
    ["turn user", "turn assistant"],
  );
  assert.deepEqual(
    matching(turns, (item) => item.className === "turn-role").map((item) => item.textContent),
    ["user", "assistant"],
  );
});

test("a poll that brings a new run rebuilds the run list", async () => {
  const page = await open();
  const button = runButtons(page.doc)[0];
  page.data.listing = listing([runRow(RUN), runRow(OTHER)]);
  await page.viewer.tick();
  const rows = runButtons(page.doc);
  assert.deepEqual(
    rows.map((row) => row.dataset.run),
    [RUN, OTHER],
  );
  assert.notEqual(rows[0], button);
  assert.equal(runButtons(page.doc).includes(button), false);
});

test("a focused target opens that run instead of the first row of the listing", async () => {
  const page = await open({
    target: { target: { kind: "run", id: OTHER } },
    labels: { labels: { [OTHER]: ["last-night"] } },
    listing: listing([runRow(RUN), runRow(OTHER)]),
  });
  assert.equal(
    node(page.doc, "target").textContent,
    `Focused run: ${OTHER} (label last-night)`,
  );
  assert.ok(page.paths().includes(`/api/runs/${OTHER}`));
  assert.equal(page.paths().includes(`/api/runs/${RUN}`), false);
  assert.match(renderedText(node(page.doc, "panel-overview")), new RegExp(OTHER));
});

test("selecting another run discards the superseded report response", async () => {
  const page = await open({ listing: listing([runRow(RUN), runRow(OTHER)]) });
  page.state.deferred.add(`/api/runs/${OTHER}`);
  const [first, second] = runButtons(page.doc);
  fire(second, "click");
  await flush();
  page.state.deferred.clear();
  assert.equal(runButtons(page.doc).includes(first), false);
  fire(runButtons(page.doc)[0], "click");
  await flush();
  const stale = page.held.find((call) => call.path === `/api/runs/${OTHER}`);
  assert.ok(stale);
  stale.settle({ body: report(OTHER) });
  await flush();
  const overview = renderedText(node(page.doc, "panel-overview"));
  assert.match(overview, new RegExp(RUN));
  assert.equal(overview.includes(OTHER), false);
  assert.equal(stale.signal?.aborted ?? false, true);
});

test("a superseded report that answers anyway never replaces the newer selection", async () => {
  const page = await open({ listing: listing([runRow(RUN), runRow(OTHER)]) });
  page.state.deferred.add(`/api/runs/${OTHER}`);
  fire(runButtons(page.doc)[1], "click");
  await flush();
  page.state.deferred.clear();
  fire(runButtons(page.doc)[0], "click");
  await flush();
  const stale = page.held.find((call) => call.path === `/api/runs/${OTHER}`);
  stale.force({ body: report(OTHER, { scheduled_trials: 99 }) });
  await flush();
  const overview = renderedText(node(page.doc, "panel-overview"));
  assert.match(overview, new RegExp(RUN));
  assert.equal(overview.includes(OTHER), false);
  assert.equal(overview.includes("99"), false);
});

test("a failed poll keeps the rendered listing and reports the failure beside it", async () => {
  const page = await open();
  const button = runButtons(page.doc)[0];
  page.state.failures.set("/api/runs", {
    ok: false,
    status: 503,
    body: { error: { code: "busy", message: "The viewer is busy." } },
  });
  await page.viewer.tick();
  assert.equal(runButtons(page.doc)[0], button);
  assert.equal(node(page.doc, "run-status").textContent, "The viewer is busy. (busy)");
});

test("the retry after a failed poll replaces the stale error before it resolves", async () => {
  const page = await open();
  page.state.failures.set("/api/runs", {
    ok: false,
    status: 503,
    body: { error: { code: "busy", message: "The viewer is busy." } },
  });
  await page.viewer.tick();
  assert.equal(node(page.doc, "run-status").textContent, "The viewer is busy. (busy)");
  page.state.failures.clear();
  page.state.deferred.add("/api/runs");
  await page.viewer.tick();
  assert.equal(node(page.doc, "run-status").textContent, "1 of 1 shown on this page.");
  const pending = page.held.find((call) => call.path.startsWith("/api/runs?"));
  assert.ok(pending);
  pending.settle({ body: page.data.listing });
  await flush();
  assert.equal(node(page.doc, "run-status").textContent, "1 of 1 shown on this page.");
});

test("clicking a tab reveals its panel and hides the others", async () => {
  const page = await open();
  fire(page.doc.getElementById("tab-progress"), "click");
  assert.equal(node(page.doc, "panel-progress").hidden, false);
  assert.equal(node(page.doc, "panel-overview").hidden, true);
  assert.equal(page.doc.getElementById("tab-progress").getAttribute("aria-selected"), "true");
  assert.equal(page.doc.getElementById("tab-overview").getAttribute("aria-selected"), "false");
  assert.equal(page.doc.activeElement, page.doc.getElementById("tab-progress"));
});

test("arrow, Home and End keys walk the tab list and move focus with it", async () => {
  const page = await open();
  const tabs = node(page.doc, "tabs");
  const right = fire(tabs, "keydown", { key: "ArrowRight" });
  assert.equal(right.defaultPrevented, true);
  assert.equal(page.doc.activeElement.id, "tab-attempts");
  fire(tabs, "keydown", { key: "ArrowLeft" });
  assert.equal(page.doc.activeElement.id, "tab-overview");
  fire(tabs, "keydown", { key: "ArrowLeft" });
  assert.equal(page.doc.activeElement.id, "tab-compare");
  fire(tabs, "keydown", { key: "Home" });
  assert.equal(page.doc.activeElement.id, "tab-overview");
  fire(tabs, "keydown", { key: "End" });
  assert.equal(page.doc.activeElement.id, "tab-compare");
  assert.equal(node(page.doc, "panel-compare").hidden, false);
});

test("arrow keys move focus down and up the run list", async () => {
  const page = await open({ listing: listing([runRow(RUN), runRow(OTHER)]) });
  const [first, second] = runButtons(page.doc);
  first.focus();
  const down = fire(first, "keydown", { key: "ArrowDown" });
  assert.equal(down.defaultPrevented, true);
  assert.equal(page.doc.activeElement, second);
  fire(second, "keydown", { key: "ArrowUp" });
  assert.equal(page.doc.activeElement, first);
});

test("typing in the attempt search narrows the attempts table", async () => {
  const page = await open();
  assert.equal(attemptButtons(page.doc).length, 2);
  node(page.doc, "attempt-search").value = "trial-2";
  fire(node(page.doc, "attempt-search"), "input");
  assert.deepEqual(
    attemptButtons(page.doc).map((button) => button.dataset.attempt),
    ["attempt-2"],
  );
  assert.equal(
    node(page.doc, "attempt-status").textContent,
    "1 of 2 retained attempt(s) shown.",
  );
});

test("the variant facet is rebuilt from the report and filters the table", async () => {
  const page = await open();
  const variants = node(page.doc, "attempt-variant");
  assert.deepEqual(
    variants.children.map((option) => option.textContent),
    ["All variants", "haiku"],
  );
  variants.value = "sonnet";
  fire(variants, "change");
  assert.equal(attemptButtons(page.doc).length, 0);
  assert.match(renderedText(node(page.doc, "attempt-list")), /No attempt matches the current filter/);
});

test("choosing an attempt opens the detail tab and loads its dialogue, patch and evidence", async () => {
  const page = await open();
  const before = page.calls.length;
  fire(attemptButtons(page.doc)[1], "click");
  await flush();
  assert.equal(node(page.doc, "panel-detail").hidden, false);
  const fresh = page.paths().slice(before);
  assert.ok(fresh.includes(`/api/runs/${RUN}/attempts/attempt-2/dialogue`));
  assert.ok(fresh.includes(`/api/runs/${RUN}/attempts/attempt-2/patch`));
  assert.ok(fresh.includes(`/api/runs/${RUN}/attempts/attempt-2/evidence`));
  assert.match(renderedText(node(page.doc, "panel-detail")), /Attempt attempt-2/);
  assert.match(renderedText(node(page.doc, "panel-detail")), /hello/);
});

test("choosing an evidence file loads and renders that file", async () => {
  const page = await open();
  const picker = matching([node(page.doc, "panel-detail")], (item) =>
    item.getAttribute?.("data-evidence"),
  );
  assert.equal(picker.length, 1);
  fire(picker[0], "click");
  await flush();
  assert.ok(
    page.paths().includes(`/api/runs/${RUN}/attempts/attempt-1/evidence/stdout.txt`),
  );
  assert.match(renderedText(node(page.doc, "panel-detail")), /bytes of stdout\.txt/);
});

test("continuing a truncated calibration scan passes the cursor and merges the result", async () => {
  const page = await open({
    calibrations: calibrations(RUN, CURSOR, [calibrationEntry("1".repeat(64))]),
  });
  const body = node(page.doc, "calibration-body");
  assert.match(renderedText(body), /scan incomplete; more objects remain/);
  page.data.calibrations = calibrations(RUN, null, [calibrationEntry("2".repeat(64))]);
  const button = matching([body], (item) => item.getAttribute?.("data-scan") === "continue");
  assert.equal(button.length, 1);
  fire(button[0], "click");
  await flush();
  assert.ok(
    page.paths().includes(`/api/runs/${RUN}/calibrations?after=${CURSOR}`),
  );
  const text = renderedText(node(page.doc, "calibration-body"));
  assert.match(text, new RegExp("1".repeat(64)));
  assert.match(text, new RegExp("2".repeat(64)));
  assert.match(text, /scan complete · 4 object\(s\) scanned/);
});

test("the report lookup refuses a reference that is not a portable report object id", async () => {
  const page = await open();
  const before = page.calls.length;
  node(page.doc, "report-id").value = "not-an-object-id";
  const event = fire(node(page.doc, "report-lookup"), "submit");
  assert.equal(event.defaultPrevented, true);
  assert.equal(
    node(page.doc, "report-status").textContent,
    "Enter a 64-character lowercase portable report object ID.",
  );
  assert.equal(page.calls.length, before);
});

test("the report lookup opens a portable report and leaves progress unavailable", async () => {
  const page = await open();
  node(page.doc, "report-id").value = REPORT_ID;
  fire(node(page.doc, "report-lookup"), "submit");
  await flush();
  assert.ok(page.paths().includes(`/api/reports/${REPORT_ID}`));
  assert.equal(page.paths().includes(`/api/reports/${REPORT_ID}/progress`), false);
  assert.match(renderedText(node(page.doc, "panel-overview")), new RegExp(REPORT_ID));
  assert.match(
    renderedText(node(page.doc, "panel-progress")),
    /Progress needs a run journal; portable reports have none\./,
  );
});

test("the compare form sends both references and both variants", async () => {
  const page = await open({
    comparison: {
      before: RUN,
      after: OTHER,
      before_input: RUN,
      after_input: OTHER,
      before_reference: "run",
      after_reference: "run",
      before_variant: "haiku",
      after_variant: "sonnet",
      paired_count: 0,
      excluded_before: 0,
      excluded_after: 0,
      before_sequence: 1,
      after_sequence: 2,
      pairs: [],
      before_metrics: [],
      after_metrics: [],
      limitations: [],
    },
  });
  node(page.doc, "compare-before").value = ` ${RUN} `;
  node(page.doc, "compare-after").value = OTHER;
  node(page.doc, "compare-before-variant").value = "haiku";
  node(page.doc, "compare-after-variant").value = "sonnet";
  fire(node(page.doc, "compare-form"), "submit");
  await flush();
  assert.ok(
    page
      .paths()
      .includes(
        `/api/compare?before=${RUN}&after=${OTHER}&before_variant=haiku&after_variant=sonnet`,
      ),
  );
  assert.match(renderedText(node(page.doc, "compare-body")), /No trial paired across these runs\./);
});

test("paging moves the listing offset forward and back", async () => {
  const page = await open({ listing: listing([runRow(RUN)], { total: 60 }) });
  assert.equal(node(page.doc, "run-range").textContent, "1–1 of 60");
  assert.equal(node(page.doc, "run-previous").disabled, true);
  assert.equal(node(page.doc, "run-next").disabled, false);
  fire(node(page.doc, "run-next"), "click");
  await flush();
  assert.ok(page.paths().includes("/api/runs?offset=25&limit=25"));
  page.data.listing = listing([runRow(OTHER)], { offset: 25, total: 60 });
  fire(node(page.doc, "run-next"), "click");
  await flush();
  fire(node(page.doc, "run-previous"), "click");
  await flush();
  assert.ok(page.paths().includes("/api/runs?offset=0&limit=25"));
});

test("clearing the refresh checkbox stops the poll and says so", async () => {
  const page = await open();
  const toggle = node(page.doc, "live-toggle");
  toggle.checked = false;
  fire(toggle, "change");
  assert.equal(node(page.doc, "poll").textContent, "Refreshing paused.");
  const before = page.calls.length;
  await page.viewer.tick();
  assert.equal(page.calls.length, before);
});

test("a hidden tab pauses the poll and says so", async () => {
  const page = await open();
  assert.equal(node(page.doc, "poll").textContent, "Refreshing every 5 seconds.");
  page.doc.hidden = true;
  page.doc.dispatchEvent({ type: "visibilitychange" });
  assert.equal(
    node(page.doc, "poll").textContent,
    "Refreshing paused while this tab is hidden.",
  );
  const before = page.calls.length;
  await page.viewer.tick();
  assert.equal(page.calls.length, before);
});

test("a refused first request leaves an error in place of every panel", async () => {
  const doc = installPage(PAGE_PATH);
  installFetch(() => ({
    ok: false,
    status: 500,
    body: { error: { code: "internal_error", message: "The viewer failed." } },
  }));
  await mountViewer();
  assert.equal(node(doc, "target").textContent, "The viewer failed. (internal_error)");
  assert.equal(node(doc, "run-status").textContent, "The viewer failed. (internal_error)");
  assert.match(renderedText(node(doc, "run-list")), /The viewer failed\./);
  assert.match(renderedText(node(doc, "panel-overview")), /Select a run to load its report\./);
});

test("a hostile report reaches the live page as inert text", async () => {
  const hostile = "<img src=x onerror=alert(1)><script>alert(2)</script>";
  const page = await open({
    listing: listing([runRow(RUN, { mode: hostile })]),
    dialogue: dialogue(hostile),
  });
  const rendered = matching(
    [node(page.doc, "run-list"), node(page.doc, "panel-detail")],
    () => true,
  );
  assert.ok(renderedText(node(page.doc, "run-list")).includes(hostile));
  assert.ok(renderedText(node(page.doc, "panel-detail")).includes(hostile));
  for (const item of rendered) {
    if (item.nodeName === "#text") {
      continue;
    }
    assert.equal(["IMG", "SCRIPT", "A", "IFRAME"].includes(item.nodeName), false, item.nodeName);
    for (const [name, value] of Object.entries(item.attributes)) {
      assert.equal(/^on/i.test(name), false, name);
      assert.equal(["href", "src", "style"].includes(name.toLowerCase()), false, name);
      assert.equal(/^\s*javascript:/i.test(value), false, value);
    }
  }
});
