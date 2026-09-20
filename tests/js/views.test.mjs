import assert from "node:assert/strict";
import { test } from "node:test";

import { installDom, matching, renderedText } from "./dom-shim.mjs";

installDom();

const {
  attemptDetail,
  attemptsTable,
  calibrationView,
  compareView,
  overview,
  progressView,
  runList,
  stateCard,
  targetLine,
} = await import("../../src/dryheave/resources/viewer/views.mjs");

const EXPERIMENT = "e".repeat(64);
const CASE = "c".repeat(64);
const RUN = "a".repeat(32);

const attempt = {
  attempt_id: "attempt-1",
  trial_id: "trial-1",
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

function report(extra = {}) {
  return {
    run_id: RUN,
    experiment_id: EXPERIMENT,
    compatibility_id: null,
    durable_sequence: 4,
    scheduled_trials: 1,
    unstarted_trials: [],
    input_error: null,
    attempts: [attempt],
    groups: [],
    portable: false,
    omissions: [],
    limitations: [],
    ...extra,
  };
}

test("evidence content that is base64 carries the same note the patch view gets", () => {
  const nodes = attemptDetail(attempt, {
    dialogue: null,
    patch: null,
    evidence: {
      attempt_id: "attempt-1",
      assessment_id: null,
      evidence_id: null,
      status: "ok",
      files: ["stderr.bin"],
      omissions: [],
      complete: true,
    },
    evidenceFile: {
      name: "stderr.bin",
      status: "ok",
      bytes: 3,
      sha256: "f".repeat(64),
      encoding: "base64",
      content: "AAEC",
    },
  });
  const text = renderedText(nodes);
  assert.match(text, /Content is not valid UTF-8; the exact bytes are shown base64-encoded\./);
  assert.match(text, /AAEC/);
});

test("utf-8 evidence content carries no base64 note", () => {
  const nodes = attemptDetail(attempt, {
    dialogue: null,
    patch: null,
    evidence: {
      attempt_id: "attempt-1",
      assessment_id: null,
      evidence_id: null,
      status: "ok",
      files: ["stdout.txt"],
      omissions: [],
      complete: true,
    },
    evidenceFile: {
      name: "stdout.txt",
      status: "ok",
      bytes: 2,
      sha256: "f".repeat(64),
      encoding: "utf-8",
      content: "ok",
    },
  });
  assert.equal(/base64-encoded/.test(renderedText(nodes)), false);
});

test("identity chips show the human label beside the immutable id", () => {
  const labels = { [EXPERIMENT]: ["nightly", "baseline"] };
  const chips = matching(overview(report(), labels), (node) =>
    (node.className ?? "").split(" ").includes("chip"),
  );
  const experiment = chips.find((chip) => chip.textContent.includes(EXPERIMENT));
  assert.ok(experiment);
  assert.match(experiment.textContent, /nightly, baseline/);
  assert.match(experiment.textContent, new RegExp(EXPERIMENT));
  assert.equal(experiment.getAttribute("title"), `experiment: ${EXPERIMENT} (label nightly, baseline)`);
  const run = chips.find((chip) => chip.textContent.includes(RUN));
  assert.ok(run);
  assert.equal(run.getAttribute("title"), `run: ${RUN}`);
});

test("identity chips invent no label when the store records none", () => {
  const chips = matching(overview(report(), {}), (node) =>
    (node.className ?? "").split(" ").includes("chip"),
  );
  const experiment = chips.find((chip) => chip.textContent.includes(EXPERIMENT));
  assert.equal(experiment.textContent, `experiment${EXPERIMENT}`);
  assert.equal(matching(chips, (node) => node.className === "chip-name").length, 0);
});

test("the run list and the focused target line show labels with their ids", () => {
  const rows = [
    { run_id: RUN, experiment_id: EXPERIMENT, created_at: null, truncated: false, error: null },
  ];
  const labelled = renderedText(runList(rows, null, { [EXPERIMENT]: ["nightly"] }));
  assert.match(labelled, /experiment nightly/);
  assert.match(labelled, new RegExp(RUN));
  assert.equal(/nightly/.test(renderedText(runList(rows, null, {}))), false);
  assert.equal(
    targetLine({ target: { kind: "report", id: "b".repeat(64) } }, { ["b".repeat(64)]: ["import"] }),
    `Focused report: ${"b".repeat(64)} (label import)`,
  );
  assert.equal(
    targetLine({ target: { kind: "run", id: RUN } }, {}),
    `Focused run: ${RUN}`,
  );
});

function calibration(cursor) {
  return {
    run_id: RUN,
    experiment_id: EXPERIMENT,
    input_error: null,
    calibrations: {},
    scan: {
      complete: cursor === null,
      scanned: 4,
      unreadable: 0,
      attempted: 1,
      verified: 1,
      next_cursor: cursor,
    },
  };
}

test("a truncated calibration scan offers a control that continues it", () => {
  const entries = [
    {
      calibration_id: "1".repeat(64),
      case_id: CASE,
      verification: "verified",
      status: "demonstrated",
      created_at: null,
      evidence_complete: true,
      omissions: [],
      reason: null,
    },
  ];
  const truncated = calibrationView(calibration("9".repeat(64)), entries, { [CASE]: ["task"] });
  const buttons = matching(truncated, (node) => node.getAttribute?.("data-scan") === "continue");
  assert.equal(buttons.length, 1);
  assert.equal(buttons[0].textContent, "Continue scan");
  assert.match(renderedText(truncated), new RegExp(`${CASE} \\(label task\\)`));
  const complete = calibrationView(calibration(null), entries, {});
  assert.equal(
    matching(complete, (node) => node.getAttribute?.("data-scan") === "continue").length,
    0,
  );
});

const HOSTILE = [
  "<script>alert(1)</script>",
  "<img src=x onerror=alert(1)>",
  "javascript:alert(1)",
  "data:text/html,<script>alert(1)</script>",
  "\uD800",
  "innerHTML",
  '"><svg onload=alert(1)>',
  "</pre><iframe src=javascript:alert(1)>",
];

const SAFE_TAGS = new Set([
  "SECTION", "DIV", "P", "SPAN", "CODE", "PRE", "UL", "OL", "LI", "DL", "DT", "DD",
  "TABLE", "CAPTION", "THEAD", "TBODY", "TR", "TH", "TD", "BUTTON",
  "H1", "H2", "H3", "H4", "H5", "H6",
]);

const UNSAFE_ATTRS = new Set([
  "href", "src", "srcdoc", "style", "action", "formaction", "xlink:href", "background", "data",
]);

function payloads(mark) {
  let seen = 0;
  return () => {
    const value = mark(seen);
    seen += 1;
    return value;
  };
}

function fixtures(next) {
  const distribution = { count: 2, unknown: 1, minimum: 1, median: 2, maximum: 3, mean: 2, stdev: 1 };
  const record = () => ({
    identity: next(),
    observed_model: next(),
    requested_model: next(),
    model_basis: next(),
    cost: 1.5,
    currency: next(),
    cost_kind: next(),
    price_version: next(),
    price_date: next(),
    reason: next(),
    raw_evidence_sha256: next(),
  });
  const role = () => ({
    role: next(),
    coverage: next(),
    known_cost: 2.5,
    currency: next(),
    known_tokens: {
      uncached_input: 1,
      cache_read: 2,
      cache_write: 3,
      output: 4,
      reasoning: 5,
      provenance: next(),
    },
    reasons: [next()],
    records: [record()],
  });
  const group = () => ({
    variant: next(),
    mode: next(),
    scheduled_trials: 2,
    attempts: 2,
    completed: 2,
    scored: 2,
    eligible: 1,
    eligible_passes: 1,
    eligible_completion_rate: 0.5,
    audit_excluded: 1,
    subjects_reporting_completion: 1,
    exclusion_reasons: { [next()]: 1 },
    durations: distribution,
    input_tokens: distribution,
    output_tokens: distribution,
    accepted_turns: distribution,
    costs: distribution,
    known_spend_by_currency: { USD: 1 },
    unknown_cost_records: 1,
    partial_cost_attempts: 1,
  });
  const result = {
    case_id: next(),
    profile_id: next(),
    simulator_id: next(),
    scoring_id: next(),
    criteria_id: next(),
    completion: next(),
    deterministic_completion: next(),
    eligible: false,
    phase: next(),
    exclusion_reasons: [next()],
    repetition: 1,
    seed: 2,
    created_at: next(),
    accepted_turns: 3,
    subject_seconds: 1.5,
    setup_seconds: 0.5,
    previous_id: next(),
    limitations: [next()],
    stop_reason: next(),
    subject_observation: next(),
    evidence_complete: false,
    reviews: [
      {
        finding_id: next(),
        decision: next(),
        reviewer: next(),
        reason: next(),
        reviewed_at: next(),
      },
    ],
    audit: {
      input_integrity: next(),
      capture_integrity: next(),
      cleanup: next(),
      capture_complete: false,
      coverage: [next()],
      findings: [
        {
          finding_id: next(),
          rule_id: next(),
          confidence: next(),
          description: next(),
          evidence_hash: next(),
        },
      ],
    },
    criteria: [
      {
        criterion_id: next(),
        kind: next(),
        required: true,
        outcome: next(),
        score: 0.5,
        error: next(),
        calibration: {
          status: next(),
          baseline: { outcome: next() },
          reference: { outcome: next() },
        },
        execution: {
          execution_observed: true,
          returncode: 0,
          process_outcome: next(),
          elapsed_seconds: 1,
          stdout_sha256: next(),
          stderr_sha256: next(),
        },
      },
    ],
    roles: [role()],
  };
  const subject = {
    attempt_id: next(),
    trial_id: next(),
    variant: next(),
    stage: next(),
    mode: next(),
    raw_evidence: next(),
    capture_id: next(),
    quarantine_id: next(),
    assessment_id: next(),
    pairing_id: next(),
    current_exclusions: [next()],
    roles: [role()],
    result,
  };
  return {
    attempt: subject,
    report: {
      run_id: next(),
      experiment_id: next(),
      compatibility_id: next(),
      durable_sequence: 3,
      scheduled_trials: 2,
      unstarted_trials: [next()],
      input_error: next(),
      attempts: [subject],
      groups: [group()],
      portable: true,
      omissions: [next()],
      limitations: [next()],
    },
    details: {
      dialogue: {
        status: next(),
        reason: next(),
        reason_code: next(),
        capture_id: next(),
        quarantine_id: next(),
        quarantined: true,
        interaction_coverage: next(),
        capture_errors: [next()],
        evidence_omissions: [next()],
        messages: [{ role: next(), text: next() }],
      },
      patch: {
        status: next(),
        reason: next(),
        reason_code: next(),
        name: next(),
        workspace_complete: false,
        bytes: 12,
        sha256: next(),
        encoding: "base64",
        omissions: [{ path: next(), reason: next(), intentional: false }],
        content: next(),
      },
      evidence: {
        status: next(),
        reason: next(),
        reason_code: next(),
        assessment_id: next(),
        evidence_id: next(),
        files: [next()],
        omissions: [next()],
        complete: false,
      },
      evidenceFile: {
        name: next(),
        status: next(),
        reason: next(),
        reason_code: next(),
        bytes: 4,
        sha256: next(),
        encoding: "base64",
        content: next(),
      },
    },
    progress: {
      run_id: next(),
      experiment_id: next(),
      created_at: next(),
      durable_sequence: 4,
      mode: next(),
      attempts: [
        {
          attempt_id: next(),
          trial_id: next(),
          stage: next(),
          capture_id: next(),
          quarantine_id: next(),
          assessment_id: next(),
        },
      ],
    },
    calibration: {
      view: {
        run_id: next(),
        experiment_id: next(),
        input_error: next(),
        calibrations: {},
        scan: {
          complete: false,
          scanned: 2,
          unreadable: 1,
          attempted: 1,
          verified: 1,
          next_cursor: next(),
        },
      },
      entries: [
        {
          calibration_id: next(),
          case_id: next(),
          verification: next(),
          status: next(),
          created_at: next(),
          evidence_complete: false,
          omissions: [next()],
          reason: next(),
        },
      ],
    },
    comparison: {
      before: next(),
      after: next(),
      before_input: next(),
      after_input: next(),
      before_reference: next(),
      after_reference: next(),
      before_variant: next(),
      after_variant: next(),
      paired_count: 1,
      excluded_before: 1,
      excluded_after: 0,
      before_sequence: 2,
      after_sequence: 3,
      pairs: [
        {
          case_id: next(),
          repetition: 1,
          before_attempt: next(),
          after_attempt: next(),
          before_completion: next(),
          after_completion: next(),
          criterion_changes: { [next()]: [next(), next()] },
        },
      ],
      before_metrics: [group()],
      after_metrics: [group()],
      limitations: [next()],
    },
    rows: [
      {
        run_id: next(),
        created_at: next(),
        mode: next(),
        experiment_id: next(),
        error: next(),
        truncated: true,
        durable_sequence: null,
        observed_events: 2,
        attempts: 2,
        finished: 1,
      },
    ],
    session: { target: { kind: next(), id: next() } },
  };
}

function render(data) {
  return [
    ...overview(data.report, {}),
    attemptsTable([data.attempt], data.attempt.attempt_id),
    ...attemptDetail(data.attempt, data.details, {}),
    ...progressView(data.progress, {}),
    ...calibrationView(data.calibration.view, data.calibration.entries, {}),
    ...compareView(data.comparison, {}),
    runList(data.rows, data.rows[0].run_id, {}),
    stateCard("error", data.report.input_error),
  ];
}

function tagCounts(nodes) {
  const counts = {};
  for (const node of matching(nodes, (item) => item.nodeName !== "#text")) {
    counts[node.nodeName] = (counts[node.nodeName] ?? 0) + 1;
  }
  return counts;
}

test("hostile response strings reach the page as text and create no element node", () => {
  const safe = fixtures(payloads((index) => `benign-${index}`));
  const hostile = fixtures(payloads((index) => HOSTILE[index % HOSTILE.length]));
  const safeTree = render(safe);
  const hostileTree = render(hostile);
  assert.deepEqual(tagCounts(hostileTree), tagCounts(safeTree));
  const text = renderedText(hostileTree);
  for (const payload of HOSTILE) {
    assert.ok(text.includes(payload), payload);
  }
  const carriers = matching(hostileTree, (node) =>
    HOSTILE.some((payload) => (node.value ?? "").includes(payload)),
  );
  assert.ok(carriers.length >= HOSTILE.length);
  for (const carrier of carriers) {
    assert.equal(carrier.nodeName, "#text");
  }
});

test("only inert tags are created and no handler, url or style attribute is produced", () => {
  const hostile = fixtures(payloads((index) => HOSTILE[index % HOSTILE.length]));
  const nodes = matching(render(hostile), (node) => node.nodeName !== "#text");
  assert.ok(nodes.length > 0);
  for (const node of nodes) {
    assert.ok(SAFE_TAGS.has(node.nodeName), node.nodeName);
    for (const [name, value] of Object.entries(node.attributes)) {
      assert.equal(/^on/i.test(name), false, name);
      assert.equal(UNSAFE_ATTRS.has(name.toLowerCase()), false, name);
      assert.equal(/^\s*(javascript|data|vbscript):/i.test(value), false, `${name}=${value}`);
    }
  }
});

test("the focused target line quotes a hostile identity without markup", () => {
  const hostile = fixtures(payloads((index) => HOSTILE[index % HOSTILE.length]));
  const line = targetLine(hostile.session, {});
  assert.equal(typeof line, "string");
  assert.ok(line.includes(hostile.session.target.id));
  const node = stateCard("error", line);
  assert.equal(renderedText(node), line);
  assert.equal(matching(node, (item) => item.nodeName === "SCRIPT").length, 0);
});

test("an unparsable hostile timestamp is rendered as text by the real view path", () => {
  const rows = [
    {
      run_id: "a".repeat(32),
      created_at: "<script>alert(1)</script>",
      mode: "offline-fixture",
      experiment_id: EXPERIMENT,
      error: null,
      truncated: false,
      durable_sequence: 1,
      observed_events: 1,
      attempts: 1,
      finished: 1,
    },
  ];
  const tree = runList(rows, null, {});
  assert.ok(renderedText(tree).includes("<script>alert(1)</script>"));
  assert.equal(matching(tree, (node) => node.nodeName === "SCRIPT").length, 0);
});
