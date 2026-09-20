import { ApiError, attemptPath, buildQuery, fetchJson, isReportId, sourcePath } from "./api.mjs";
import { clear, el, replace } from "./dom.mjs";
import {
  filterAttempts,
  filterCalibrations,
  filterRuns,
  attemptFacets,
  mergeCalibrations,
  moveSelection,
  pageBounds,
  resolveSelection,
} from "./filters.mjs";
import {
  attemptDetail,
  attemptsTable,
  calibrationView,
  compareView,
  overview,
  progressView,
  runList,
  stateCard,
  targetLine,
} from "./views.mjs";

const POLL_MILLISECONDS = 5000;
const PAGE_LIMIT = 25;

function area() {
  return { data: null, error: null, loading: false, revision: 0, encoded: null, controller: null };
}

const state = {
  session: area(),
  labels: area(),
  listing: area(),
  offset: 0,
  runQuery: "",
  source: null,
  report: area(),
  attemptId: null,
  attemptFilters: { query: "", variant: "", stage: "", eligibility: "" },
  dialogue: area(),
  patch: area(),
  evidence: area(),
  evidenceFile: area(),
  evidenceName: null,
  progress: area(),
  calibration: area(),
  calibrationQuery: "",
  caseId: "",
  comparison: area(),
  tab: "overview",
  live: true,
};

const nodes = {};

const painted = {};

function bind() {
  for (const id of [
    "target",
    "poll",
    "live-toggle",
    "run-search",
    "run-status",
    "run-list",
    "run-previous",
    "run-next",
    "run-range",
    "report-lookup",
    "report-id",
    "report-status",
    "tabs",
    "panel-overview",
    "panel-attempts",
    "panel-detail",
    "panel-progress",
    "panel-calibration",
    "panel-compare",
    "attempt-search",
    "attempt-variant",
    "attempt-stage",
    "attempt-eligibility",
    "attempt-status",
    "attempt-list",
    "attempt-filters",
    "calibration-search",
    "calibration-filters",
    "case-id",
    "case-clear",
    "calibration-body",
    "compare-form",
    "compare-before",
    "compare-after",
    "compare-before-variant",
    "compare-after-variant",
    "compare-body",
    "main",
  ]) {
    nodes[id] = document.getElementById(id);
  }
}

function failure(error) {
  if (error instanceof ApiError) {
    return error.message;
  }
  return "The viewer failed unexpectedly while rendering this request.";
}

function abort(slot) {
  if (slot && slot.controller) {
    slot.controller.abort();
    slot.controller = null;
  }
}

function reset(name) {
  abort(state[name]);
  state[name] = area();
}

function accept(slot, next) {
  const encoded = JSON.stringify(next);
  if (encoded === slot.encoded) {
    return;
  }
  slot.data = next;
  slot.encoded = encoded;
  slot.revision += 1;
}

async function load(name, path, merge = null) {
  const slot = state[name];
  abort(slot);
  const controller = new AbortController();
  slot.controller = controller;
  slot.loading = true;
  slot.error = null;
  render();
  try {
    const payload = await fetchJson(path, { signal: controller.signal });
    if (slot.controller === controller) {
      accept(slot, merge && slot.data ? merge(slot.data, payload) : payload);
    }
  } catch (error) {
    if (slot.controller === controller) {
      slot.error = failure(error);
    }
  } finally {
    if (slot.controller === controller) {
      slot.controller = null;
      slot.loading = false;
      render();
    }
  }
}

function slotKey(slot) {
  if (slot.data !== null) {
    return `${slot.revision}/data/${slot.error ?? ""}`;
  }
  return `${slot.revision}/${slot.loading ? "loading" : "empty"}/${slot.error ?? ""}`;
}

function paint(name, key, build) {
  if (painted[name] === key) {
    return;
  }
  painted[name] = key;
  build();
}

function labelIndex() {
  return state.labels.data ? state.labels.data.labels : null;
}

function panelBody(slot, build, emptyText) {
  if (slot.data !== null) {
    return slot.error ? [stateCard("error", slot.error), ...build(slot.data)] : build(slot.data);
  }
  if (slot.error) {
    return [stateCard("error", slot.error)];
  }
  if (slot.loading) {
    return [stateCard("loading", "Loading…")];
  }
  return [stateCard("empty", emptyText)];
}

function visibleAttempts() {
  const report = state.report.data;
  if (!report) {
    return [];
  }
  return filterAttempts(report.attempts, state.attemptFilters);
}

function renderTarget() {
  const session = state.session;
  paint("target", `${slotKey(session)}|${state.labels.revision}`, () => {
    nodes.target.textContent = session.error
      ? session.error
      : targetLine(session.data, labelIndex());
  });
}

function renderRuns() {
  const slot = state.listing;
  const selected = state.source && state.source.kind === "run" ? state.source.id : null;
  paint("run-status", `${slotKey(slot)}|${state.runQuery}`, () => paintRunStatus(slot));
  const key =
    slot.data === null
      ? `none|${slot.error ?? ""}|${slot.loading ? "loading" : ""}`
      : `${slot.revision}|${state.runQuery}|${selected ?? ""}|${state.labels.revision}`;
  paint("run-list", key, () => paintRunList(slot, selected));
}

function paintRunStatus(slot) {
  if (slot.error) {
    nodes["run-status"].textContent = slot.error;
    return;
  }
  if (slot.data === null) {
    nodes["run-status"].textContent = "Loading runs…";
    return;
  }
  if (pageBounds(slot.data).total === 0) {
    nodes["run-status"].textContent = "This store retains no runs.";
    return;
  }
  const rows = filterRuns(slot.data.runs, state.runQuery);
  nodes["run-status"].textContent = `${rows.length} of ${slot.data.runs.length} shown on this page.`;
}

function paintRunList(slot, selected) {
  if (slot.data === null) {
    replace(nodes["run-list"], [
      slot.error ? stateCard("error", slot.error) : stateCard("loading", "Loading runs…"),
    ]);
    return;
  }
  const bounds = pageBounds(slot.data);
  nodes["run-range"].textContent =
    bounds.total === 0 ? "no runs" : `${bounds.first}–${bounds.last} of ${bounds.total}`;
  nodes["run-previous"].disabled = !bounds.hasPrevious;
  nodes["run-next"].disabled = !bounds.hasNext;
  if (bounds.total === 0) {
    replace(nodes["run-list"], [stateCard("empty", "This store retains no runs.")]);
    return;
  }
  const rows = filterRuns(slot.data.runs, state.runQuery);
  if (rows.length === 0) {
    replace(nodes["run-list"], [stateCard("empty", "No run matches this filter.")]);
    return;
  }
  replace(nodes["run-list"], [runList(rows, selected, labelIndex())]);
}

function renderOverview() {
  paint("overview", `${slotKey(state.report)}|${state.labels.revision}`, paintOverview);
}

function paintOverview() {
  replace(
    nodes["panel-overview"],
    panelBody(
      state.report,
      (data) => overview(data, labelIndex()),
      "Select a run to load its report.",
    ),
  );
}

function optionList(select, values, current, allLabel) {
  clear(select);
  select.append(el("option", { text: allLabel, attrs: { value: "" } }));
  for (const value of values) {
    select.append(el("option", { text: value, attrs: { value } }));
  }
  select.value = values.includes(current) ? current : "";
}

function renderAttempts() {
  const key = `${slotKey(state.report)}|${JSON.stringify(state.attemptFilters)}|${state.attemptId ?? ""}`;
  paint("attempts", key, paintAttempts);
}

function paintAttempts() {
  const report = state.report.data;
  if (!report) {
    nodes["attempt-status"].textContent = "";
    replace(
      nodes["attempt-list"],
      panelBody(state.report, () => [], "Select a run to load its attempts."),
    );
    return;
  }
  const facets = attemptFacets(report.attempts);
  optionList(nodes["attempt-variant"], facets.variants, state.attemptFilters.variant, "All variants");
  optionList(nodes["attempt-stage"], facets.stages, state.attemptFilters.stage, "All stages");
  nodes["attempt-eligibility"].value = state.attemptFilters.eligibility;
  const attempts = visibleAttempts();
  nodes["attempt-status"].textContent =
    `${attempts.length} of ${report.attempts.length} retained attempt(s) shown.`;
  replace(nodes["attempt-list"], [attemptsTable(attempts, state.attemptId)]);
}

function detailKey() {
  const slots = [state.dialogue, state.patch, state.evidence, state.evidenceFile];
  return [
    slotKey(state.report),
    state.attemptId ?? "",
    state.labels.revision,
    ...slots.map(slotKey),
  ].join("|");
}

function renderDetail() {
  paint("detail", detailKey(), paintDetail);
}

function paintDetail() {
  const report = state.report.data;
  if (!report) {
    replace(nodes["panel-detail"], [stateCard("empty", "Select a run, then an attempt.")]);
    return;
  }
  const attempt = report.attempts.find((item) => item.attempt_id === state.attemptId);
  if (!attempt) {
    replace(nodes["panel-detail"], [stateCard("empty", "Select an attempt to see its detail.")]);
    return;
  }
  const errors = [];
  for (const [label, slot] of [
    ["dialogue", state.dialogue],
    ["patch", state.patch],
    ["evidence", state.evidence],
    ["evidence file", state.evidenceFile],
  ]) {
    if (slot.error) {
      errors.push(stateCard("error", `Could not load the ${label}: ${slot.error}`));
    }
  }
  replace(nodes["panel-detail"], [
    ...errors,
    ...attemptDetail(
      attempt,
      {
        dialogue: state.dialogue.data,
        patch: state.patch.data,
        evidence: state.evidence.data,
        evidenceFile: state.evidenceFile.data,
      },
      labelIndex(),
    ),
  ]);
}

function renderProgress() {
  const source = state.source ? `${state.source.kind}:${state.source.id}` : "";
  const key = `${source}|${slotKey(state.progress)}|${state.labels.revision}`;
  paint("progress", key, paintProgress);
}

function paintProgress() {
  if (!state.source || state.source.kind !== "run") {
    replace(nodes["panel-progress"], [
      stateCard("empty", "Progress needs a run journal; portable reports have none."),
    ]);
    return;
  }
  replace(
    nodes["panel-progress"],
    panelBody(
      state.progress,
      (data) => progressView(data, labelIndex()),
      "Select a run to watch its progress.",
    ),
  );
}

function renderCalibration() {
  const key = `${slotKey(state.calibration)}|${state.calibrationQuery}|${state.labels.revision}`;
  paint("calibration", key, paintCalibration);
}

function paintCalibration() {
  nodes["calibration-search"].value = state.calibrationQuery;
  replace(
    nodes["calibration-body"],
    panelBody(
      state.calibration,
      (data) =>
        calibrationView(
          { input_error: null, ...data },
          filterCalibrations(
            data.calibrations instanceof Array
              ? data.calibrations
              : Object.values(data.calibrations).flat(),
            state.calibrationQuery,
          ),
          labelIndex(),
        ),
      "Select a run, or look up a case ID, to scan for calibration records.",
    ),
  );
}

function renderCompare() {
  paint("compare", `${slotKey(state.comparison)}|${state.labels.revision}`, paintCompare);
}

function paintCompare() {
  replace(
    nodes["compare-body"],
    panelBody(
      state.comparison,
      (data) => compareView(data, labelIndex()),
      "Enter a before and an after reference, then compare.",
    ),
  );
}

function renderTabs() {
  paint("tabs", state.tab, paintTabs);
}

function paintTabs() {
  for (const tab of nodes.tabs.querySelectorAll(".tab")) {
    const name = tab.id.slice("tab-".length);
    const active = name === state.tab;
    tab.setAttribute("aria-selected", active ? "true" : "false");
    tab.tabIndex = active ? 0 : -1;
    nodes[`panel-${name}`].hidden = !active;
  }
}

function renderPoll() {
  paint("poll", `${state.live}|${document.hidden}`, paintPoll);
}

function paintPoll() {
  if (!state.live) {
    nodes.poll.textContent = "Refreshing paused.";
    return;
  }
  nodes.poll.textContent = document.hidden
    ? "Refreshing paused while this tab is hidden."
    : "Refreshing every 5 seconds.";
}

function render() {
  renderTarget();
  renderRuns();
  renderTabs();
  renderOverview();
  renderAttempts();
  renderDetail();
  renderProgress();
  renderCalibration();
  renderCompare();
  renderPoll();
}

function resetDetails() {
  reset("dialogue");
  reset("patch");
  reset("evidence");
  reset("evidenceFile");
  state.evidenceName = null;
}

async function loadDetails() {
  resetDetails();
  if (!state.source || !state.attemptId) {
    render();
    return;
  }
  const source = state.source;
  const attemptId = state.attemptId;
  await Promise.all([
    load("dialogue", attemptPath(source, attemptId, "dialogue")),
    load("patch", attemptPath(source, attemptId, "patch")),
    load("evidence", attemptPath(source, attemptId, "evidence")),
  ]);
}

async function loadEvidenceFile(name) {
  state.evidenceName = name;
  const path = `${attemptPath(state.source, state.attemptId, "evidence")}/${encodeURIComponent(name)}`;
  await load("evidenceFile", path);
}

async function loadReport() {
  if (!state.source) {
    return;
  }
  state.attemptId = null;
  resetDetails();
  await load("report", sourcePath(state.source));
  const report = state.report.data;
  if (report) {
    state.attemptId = resolveSelection(
      visibleAttempts().map((attempt) => attempt.attempt_id),
      null,
    );
    render();
    await loadDetails();
  }
}

async function loadProgress() {
  if (state.source && state.source.kind === "run") {
    await load("progress", `/api/runs/${state.source.id}/progress`);
  }
}

async function loadCalibrations(after = null) {
  const query = buildQuery({ after });
  const merge = after ? mergeCalibrations : null;
  if (state.caseId) {
    const path = `/api/cases/${encodeURIComponent(state.caseId)}/calibrations${query}`;
    await load("calibration", path, merge);
    return;
  }
  if (state.source && state.source.kind === "run") {
    await load("calibration", `/api/runs/${state.source.id}/calibrations${query}`, merge);
  }
}

async function continueCalibrations() {
  const data = state.calibration.data;
  const cursor = data && data.scan ? data.scan.next_cursor : null;
  if (cursor) {
    await loadCalibrations(cursor);
  }
}

async function selectRun(runId) {
  state.source = { kind: "run", id: runId };
  reset("calibration");
  reset("progress");
  await loadReport();
  await Promise.all([loadProgress(), loadCalibrations()]);
}

async function selectReport(reportId) {
  state.source = { kind: "report", id: reportId };
  reset("calibration");
  reset("progress");
  await loadReport();
  await loadCalibrations();
}

async function loadListing() {
  await load("listing", `/api/runs${buildQuery({ offset: state.offset, limit: PAGE_LIMIT })}`);
}

function focusTab(name) {
  state.tab = name;
  renderTabs();
  const tab = document.getElementById(`tab-${name}`);
  if (tab) {
    tab.focus();
  }
}

function wireTabs() {
  nodes.tabs.addEventListener("click", (event) => {
    const tab = event.target.closest(".tab");
    if (tab) {
      focusTab(tab.id.slice("tab-".length));
    }
  });
  nodes.tabs.addEventListener("keydown", (event) => {
    const names = [...nodes.tabs.querySelectorAll(".tab")].map((tab) =>
      tab.id.slice("tab-".length),
    );
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault();
      const index = names.indexOf(state.tab);
      const next = (index + (event.key === "ArrowRight" ? 1 : names.length - 1)) % names.length;
      focusTab(names[next]);
    } else if (event.key === "Home") {
      event.preventDefault();
      focusTab(names[0]);
    } else if (event.key === "End") {
      event.preventDefault();
      focusTab(names[names.length - 1]);
    }
  });
}

function wireRuns() {
  nodes["run-list"].addEventListener("click", (event) => {
    const button = event.target.closest("button[data-run]");
    if (button) {
      void selectRun(button.dataset.run);
    }
  });
  nodes["run-list"].addEventListener("keydown", (event) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") {
      return;
    }
    const buttons = [...nodes["run-list"].querySelectorAll("button[data-run]")];
    const ids = buttons.map((button) => button.dataset.run);
    const current = event.target.closest("button[data-run]");
    const next = moveSelection(ids, current ? current.dataset.run : null, event.key === "ArrowDown" ? 1 : -1);
    const target = buttons[ids.indexOf(next)];
    if (target) {
      event.preventDefault();
      target.focus();
    }
  });
  nodes["run-search"].addEventListener("input", () => {
    state.runQuery = nodes["run-search"].value;
    renderRuns();
  });
  nodes["run-previous"].addEventListener("click", () => {
    state.offset = pageBounds(state.listing.data).previousOffset;
    void loadListing();
  });
  nodes["run-next"].addEventListener("click", () => {
    state.offset = pageBounds(state.listing.data).nextOffset;
    void loadListing();
  });
  nodes["report-lookup"].addEventListener("submit", (event) => {
    event.preventDefault();
    const reference = nodes["report-id"].value.trim();
    if (!isReportId(reference)) {
      nodes["report-status"].textContent =
        "Enter a 64-character lowercase portable report object ID.";
      return;
    }
    nodes["report-status"].textContent = "";
    void selectReport(reference);
  });
}

function wireAttempts() {
  nodes["attempt-filters"].addEventListener("submit", (event) => event.preventDefault());
  nodes["attempt-search"].addEventListener("input", () => {
    state.attemptFilters.query = nodes["attempt-search"].value;
    renderAttempts();
  });
  for (const [id, key] of [
    ["attempt-variant", "variant"],
    ["attempt-stage", "stage"],
    ["attempt-eligibility", "eligibility"],
  ]) {
    nodes[id].addEventListener("change", () => {
      state.attemptFilters[key] = nodes[id].value;
      renderAttempts();
    });
  }
  nodes["attempt-list"].addEventListener("click", (event) => {
    const button = event.target.closest("button[data-attempt]");
    if (!button) {
      return;
    }
    state.attemptId = button.dataset.attempt;
    focusTab("detail");
    void loadDetails();
  });
  nodes["panel-detail"].addEventListener("click", (event) => {
    const button = event.target.closest("button[data-evidence]");
    if (button) {
      void loadEvidenceFile(button.dataset.evidence);
    }
  });
}

function wireCalibration() {
  nodes["calibration-filters"].addEventListener("submit", (event) => {
    event.preventDefault();
    state.caseId = nodes["case-id"].value.trim();
    void loadCalibrations();
  });
  nodes["calibration-search"].addEventListener("input", () => {
    state.calibrationQuery = nodes["calibration-search"].value;
    renderCalibration();
  });
  nodes["calibration-body"].addEventListener("click", (event) => {
    if (event.target.closest("button[data-scan]")) {
      void continueCalibrations();
    }
  });
  nodes["case-clear"].addEventListener("click", () => {
    state.caseId = "";
    nodes["case-id"].value = "";
    reset("calibration");
    void loadCalibrations();
  });
}

function wireCompare() {
  nodes["compare-form"].addEventListener("submit", (event) => {
    event.preventDefault();
    const query = buildQuery({
      before: nodes["compare-before"].value.trim(),
      after: nodes["compare-after"].value.trim(),
      before_variant: nodes["compare-before-variant"].value.trim(),
      after_variant: nodes["compare-after-variant"].value.trim(),
    });
    void load("comparison", `/api/compare${query}`);
  });
}

function wireLive() {
  nodes["live-toggle"].addEventListener("change", () => {
    state.live = nodes["live-toggle"].checked;
    renderPoll();
  });
  document.addEventListener("visibilitychange", renderPoll);
  setInterval(() => {
    if (!state.live || document.hidden) {
      return;
    }
    void loadListing();
    void loadProgress();
  }, POLL_MILLISECONDS);
}

async function start() {
  bind();
  wireTabs();
  wireRuns();
  wireAttempts();
  wireCalibration();
  wireCompare();
  wireLive();
  render();
  await load("session", "/api/target");
  await load("labels", "/api/labels");
  await loadListing();
  const target = state.session.data ? state.session.data.target : null;
  if (target) {
    state.source = target;
    state.caseId = "";
    await loadReport();
    await Promise.all([loadProgress(), loadCalibrations()]);
    return;
  }
  const rows = state.listing.data ? state.listing.data.runs : [];
  const readable = rows.find((row) => !row.error);
  if (readable) {
    await selectRun(readable.run_id);
  }
}

try {
  await start();
} catch (error) {
  const banner = document.getElementById("target");
  if (banner) {
    banner.textContent = failure(error);
  }
}
