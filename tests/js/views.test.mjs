import assert from "node:assert/strict";
import { test } from "node:test";

import { installDom, matching, renderedText } from "./dom-shim.mjs";

installDom();

const {
  attemptDetail,
  attemptsTable,
  calibrationView,
  caseDetail,
  compareView,
  overview,
  progressView,
  resultsView,
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

function resultWithGrading(overrides = {}) {
  return {
    case_id: "c".repeat(64),
    graded_case_id: null,
    rubric_set: null,
    profile_id: null,
    stand_in_id: null,
    scoring_id: null,
    criteria_id: null,
    outcome: "pass",
    outcome_reason: "all_met",
    eligible: true,
    phase: "finished",
    exclusion_reasons: [],
    repetition: 1,
    seed: 1,
    created_at: null,
    accepted_turns: 1,
    subject_seconds: 1,
    setup_seconds: 1,
    previous_id: null,
    limitations: [],
    stop_reason: "done",
    subject_observation: "completed",
    evidence_complete: true,
    reviews: [],
    audit: {
      input_integrity: "verified",
      capture_integrity: "verified",
      cleanup: "stopped",
      capture_complete: true,
      coverage: [],
      findings: [],
    },
    criteria: [
      {
        criterion_id: "quality",
        kind: "judge",
        required: true,
        outcome: "pass",
        score: 1,
        error: null,
        calibration: null,
        execution: null,
      },
    ],
    facts: [
      {
        fact_id: "asked",
        kind: "requirement",
        phase: "fix",
        verdict: "met",
        observation: "The greeting asks first.",
        evidence: "greet.py:2",
        source: "judge",
      },
    ],
    uncovered: [],
    metrics: [],
    fact_account: null,
    roles: [],
    ...overrides,
  };
}

test("attemptDetail shows the judge's rationale for a criterion and each fact's final verdict", () => {
  const subject = { ...attempt, result: resultWithGrading() };
  const reasoning = {
    attempt_id: "attempt-1",
    assessment_id: null,
    status: "ok",
    criteria: [{ criterion_id: "quality", rationale: "Judged on its merits." }],
  };
  const text = renderedText(
    attemptDetail(subject, { reasoning, dialogue: null, patch: null, evidence: null, evidenceFile: null }, {}),
  );
  assert.match(text, /Judged on its merits\./);
  assert.match(text, /The greeting asks first\./);
  assert.match(text, /qualityjudgerubricpass/);
  assert.equal(text.includes("reasoning not included"), false);
});

function lanes(overrides = {}) {
  return {
    attempt_id: "attempt-1",
    status: "ok",
    reason: null,
    reason_code: null,
    lanes: {
      run_id: RUN,
      attempt_id: "attempt-1",
      case_id: CASE,
      outcome: "fail",
      outcome_reason: "unmet",
      stop_reason: "workflow_complete",
      conversation: [
        { turn: 1, role: "user", text: "Fix the greeting.", author: "initial", origin: null },
        { turn: 1, role: "assistant", text: "Done.", author: null, origin: null },
        { turn: 2, role: "user", text: "No comma yet.", author: "stand_in", origin: "c-fix" },
      ],
      behind_the_scenes: [
        {
          kind: "inspection",
          turn: 1,
          call_id: null,
          purpose: null,
          status: "completed",
          action: null,
          text: null,
          reason: null,
          cited: [],
          phase: null,
          handed: ["punctuation"],
          rejection: null,
          inspection_id: "i-1",
          call_ids: ["j-one", "j-two"],
          facts: [{ fact_id: "punctuation", verdict: "unmet", observation: "No comma.", evidence: "greet.py:2" }],
          detail: null,
        },
        {
          kind: "stand_in",
          turn: 1,
          call_id: "c-fix",
          purpose: "fix_request",
          status: "completed",
          action: "reply",
          text: "No comma yet.",
          reason: "The user tried it.",
          cited: ["punctuation"],
          phase: null,
          handed: ["punctuation"],
          rejection: null,
          inspection_id: null,
          facts: [],
          detail: null,
        },
      ],
      gaps: {
        assessed: true,
        facts: [
          {
            fact_id: "punctuation",
            kind: "requirement",
            phase: "fix",
            verdict: "unmet",
            text: "Use a comma after Hello.",
            observation: "Still no comma.",
            evidence: "greet.py:2",
            source: "verifier",
          },
        ],
        uncovered: [],
        unreached_phases: [],
      },
      notes: [],
      ...overrides,
    },
  };
}

test("attemptDetail renders the three review lanes in order", () => {
  const nodes = attemptDetail(
    attempt,
    { review: lanes(), reasoning: null, dialogue: null, patch: null, evidence: null, evidenceFile: null },
    {},
  );
  const text = renderedText(nodes);
  assert.ok(text.indexOf("Conversation") < text.indexOf("Behind the scenes"));
  assert.ok(text.indexOf("Behind the scenes") < text.indexOf("Gaps"));
  assert.match(text, /turn 2 · user \(stand-in c-fix\)/);
  assert.match(text, /turn 1 · stand-in · fix request/);
  assert.match(text, /HandedpunctuationCitedpunctuation/);
  assert.match(text, /Inspectioni-1Judge callsj-one, j-twoStatuscompletedUnmetpunctuation/);
  assert.match(text, /Judge callsj-one, j-two/);
  assert.match(text, /punctuationunmetNo comma\.greet\.py:2/);
  assert.match(text, /punctuation · unmet/);
  assert.match(text, /WantedUse a comma after Hello\.SawStill no comma\./);
  assert.match(text, /fail \(unmet\)/);
});

test("the review card says when lanes are empty or not assessed", () => {
  const empty = lanes({
    conversation: [],
    behind_the_scenes: [],
    gaps: { assessed: false, facts: [], uncovered: [], unreached_phases: [] },
  });
  const text = renderedText(
    attemptDetail(attempt, { review: empty, reasoning: null, dialogue: null, patch: null, evidence: null, evidenceFile: null }, {}),
  );
  assert.match(text, /No conversation retained\./);
  assert.match(text, /Nothing happened behind the scenes\./);
  assert.match(text, /Not assessed yet\./);
  const met = lanes({ gaps: { assessed: true, facts: [], uncovered: [], unreached_phases: [] } });
  assert.match(
    renderedText(attemptDetail(attempt, { review: met }, {})),
    /No gaps: every fact was met and nothing is uncovered\./,
  );
});

test("attemptDetail lists workflow events in order and falls back without a workflow", () => {
  const withoutWorkflow = { ...attempt, result: resultWithGrading() };
  const emptyText = renderedText(
    attemptDetail(withoutWorkflow, { reasoning: null, dialogue: null, patch: null, evidence: null, evidenceFile: null }, {}),
  );
  assert.match(emptyText, /No workflow is retained for this attempt\./);

  const workflow = {
    steps: 2,
    completed_steps: 2,
    inspections: 1,
    fix_requests: 1,
    dialogs_answered: 0,
    first_inspection: "passed",
    final_inspection: "passed",
    inspection_seconds: 5,
    events: [
      { step_id: "review", kind: "prompt", outcome: "sent", turn: 1, failed_criteria: [], call_ids: [], seconds: null },
      {
        step_id: "check",
        kind: "inspect",
        outcome: "failed",
        turn: 1,
        failed_criteria: ["greeting"],
        call_ids: [],
        seconds: 2,
      },
      { step_id: "check", kind: "inspect", outcome: "fix_requested", turn: 1, failed_criteria: ["greeting"], call_ids: [], seconds: null },
      { step_id: "check", kind: "inspect", outcome: "passed", turn: 2, failed_criteria: [], call_ids: [], seconds: 3 },
    ],
  };
  const withWorkflow = { ...attempt, result: resultWithGrading({ workflow }) };
  const text = renderedText(
    attemptDetail(withWorkflow, { reasoning: null, dialogue: null, patch: null, evidence: null, evidenceFile: null }, {}),
  );
  assert.match(text, /Workflow events in order\./);
  assert.match(text, /greeting/);
  assert.match(text, /fix_requested/);
  assert.match(text, /passed/);
});

test("attemptDetail keeps hostile judge rationale, fact text and metric values inert", () => {
  const hostile = "<script>alert(1)</script>";
  const result = resultWithGrading({
    metrics: [
      {
        metrics_id: hostile,
        outcome: "ok",
        error: null,
        values: [{ metric_id: hostile, label: hostile, value: hostile, unit: hostile, better: hostile }],
      },
    ],
    uncovered: [hostile],
    fact_account: {
      independence: 1,
      stand_in_replies: 1,
      fix_rounds: 0,
      reached_phases: [hostile],
      unreached_phases: [],
      corrections: [{ phase: hostile, rounds: 2, raised: [hostile] }],
      facts: [
        { fact_id: hostile, kind: "requirement", phase: hostile, credit: "own", raised: 0, elicited: false, first_inspection: null, regressions: 0 },
      ],
    },
  });
  const subject = { ...attempt, result };
  const reasoning = {
    attempt_id: "attempt-1",
    assessment_id: null,
    status: "ok",
    criteria: [{ criterion_id: "quality", rationale: hostile }],
  };
  const nodes = attemptDetail(
    subject,
    { reasoning, dialogue: null, patch: null, evidence: null, evidenceFile: null },
    {},
  );
  assert.ok(renderedText(nodes).includes(hostile));
  assert.ok(renderedText(nodes).includes(`${hostile}: 2 rounds (${hostile})`));
  const elements = matching(nodes, (node) => node.nodeName !== "#text");
  assert.equal(matching(elements, (node) => node.nodeName === "SCRIPT").length, 0);
  for (const node of elements) {
    for (const [name, value] of Object.entries(node.attributes ?? {})) {
      assert.equal(/^on/i.test(name), false, name);
      assert.equal(UNSAFE_ATTRS.has(name.toLowerCase()), false, name);
      assert.equal(/^\s*(javascript|data|vbscript):/i.test(value), false, `${name}=${value}`);
    }
  }
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
    stand_in_id: next(),
    scoring_id: next(),
    criteria_id: next(),
    outcome: next(),
    outcome_reason: next(),
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
      review: {
        attempt_id: next(),
        status: "ok",
        reason: null,
        reason_code: null,
        lanes: {
          run_id: next(),
          attempt_id: next(),
          case_id: next(),
          outcome: next(),
          outcome_reason: next(),
          stop_reason: next(),
          conversation: [{ turn: 1, role: next(), text: next(), author: next(), origin: next() }],
          behind_the_scenes: [
            {
              kind: next(),
              turn: 2,
              call_id: next(),
              purpose: next(),
              status: next(),
              action: next(),
              text: next(),
              reason: next(),
              cited: [next()],
              phase: next(),
              handed: [next()],
              rejection: next(),
              inspection_id: next(),
              facts: [
                { fact_id: next(), verdict: next(), observation: next(), evidence: next() },
              ],
              detail: next(),
            },
          ],
          gaps: {
            assessed: true,
            facts: [
              {
                fact_id: next(),
                kind: next(),
                phase: next(),
                verdict: next(),
                text: next(),
                observation: next(),
                evidence: next(),
                source: next(),
              },
            ],
            uncovered: [next()],
            unreached_phases: [next()],
          },
          notes: [next()],
        },
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
          before_outcome: next(),
          after_outcome: next(),
          fact_changes: { [next()]: [next(), next()] },
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

const distribution = { count: 2, unknown: 0, minimum: 1, median: 2, maximum: 3, mean: 2, stdev: 1 };

function group(variant, extra = {}) {
  return {
    variant,
    mode: "native",
    scheduled_trials: 2,
    attempts: 2,
    completed: 2,
    scored: 2,
    eligible: 2,
    eligible_passes: 1,
    eligible_completion_rate: 0.5,
    audit_excluded: 0,
    subjects_reporting_completion: 1,
    exclusion_reasons: {},
    durations: distribution,
    input_tokens: distribution,
    output_tokens: distribution,
    accepted_turns: distribution,
    costs: distribution,
    known_spend_by_currency: { USD: 1 },
    unknown_cost_records: 0,
    partial_cost_attempts: 0,
    ...extra,
  };
}

test("resultsView reports an empty state when a report has no variant groups", () => {
  const nodes = resultsView(report({ groups: [] }), {});
  assert.ok(renderedText(nodes).includes("no variant groups"));
});

test("resultsView renders old-shape groups as not measured rather than crash", () => {
  const nodes = resultsView(report({ groups: [group("haiku")] }), {});
  const text = renderedText(nodes);
  assert.match(text, /No criterion results are retained/);
  assert.match(text, /No fact results are retained/);
  assert.match(text, /No case metrics are retained/);
  assert.match(text, /Total input tokens \(all retained attempts, retries included\)[^|]*unknown/);
  assert.match(text, /Independence/);
});

test("resultsView renders the backend's group price basis and fact summary", () => {
  const haiku = group("haiku", {
    price_basis: {
      kinds: [["estimated", 2]],
      bases: [["observed", 2]],
      currencies: ["USD"],
      versions: ["v1"],
      dates: ["2026-10-01"],
    },
    facts: {
      attempts: 2,
      outcomes: { fail: 1, pass: 1 },
      outcome_reasons: { all_met: 1, unmet: 1 },
      pass_rate: 0.5,
      independence: distribution,
      corrections: distribution,
      unreached_attempts: 0,
      uncovered: 3,
      facts: [
        {
          case_id: CASE,
          fact_id: "budget",
          kind: "requirement",
          phase: "fix",
          attempts: 2,
          met: 1,
          unmet: 1,
          unclear: 0,
          disputed: 0,
          own: 0,
          elicited: 1,
          corrected: 0,
          unreached: 0,
          raised: 1,
          raised_rate: 0.5,
          first_inspection_met: 0,
          first_inspected: 2,
          first_inspection_met_rate: 0,
          regressions: 0,
        },
      ],
    },
  });
  const text = renderedText(resultsView(report({ groups: [haiku] }), {}));
  assert.match(text, /cost kinds estimated×2 · model basis observed×2 · currencies USD · price tables v1/);
  assert.match(text, /sd 1\.0000 USD/);
  assert.match(text, /fail 1, pass 1 \(all_met 1, unmet 1\)/);
  assert.match(text, /met 1\/2 · own 0, asked 1, corrected 0, unmet 1, unreached 0 · raised 50\.0%/);
  const rows = matching(resultsView(report({ groups: [haiku] }), {}), (node) => node.nodeName === "TR")
    .map((row) => renderedText([row]));
  assert.ok(rows.includes("Uncovered problems3"));
});

test("attemptsTable shows each attempt's reported eligibility", () => {
  const text = renderedText(attemptsTable([{ ...attempt, eligibility: "ungraded" }], null));
  assert.match(text, /ungraded/);
});

test("resultsView compares multiple variants side by side and marks missing ones not measured", () => {
  const haiku = group("haiku", {
    criteria: [{ case_id: CASE, criterion_id: "quality", passes: 1, graded: 1, scores: distribution }],
    metrics: [
      { case_id: CASE, metric_id: "changed_lines", label: "Changed lines", unit: "lines", values: distribution },
    ],
  });
  const opus = group("opus");
  const nodes = resultsView(report({ groups: [haiku, opus] }), {});
  const text = renderedText(nodes);
  assert.match(text, /haiku/);
  assert.match(text, /opus/);
  assert.match(text, /quality/);
  assert.match(text, /not measured/);
  const caseButtons = matching(nodes, (node) => node.getAttribute?.("data-case") === CASE);
  assert.ok(caseButtons.length > 0);
});

test("resultsView keeps hostile criterion, fact and metric ids as text", () => {
  const hostile = "<script>alert(1)</script>";
  const haiku = group("haiku", {
    criteria: [{ case_id: CASE, criterion_id: hostile, passes: 1, graded: 1, scores: distribution }],
    facts: {
      attempts: 1,
      outcomes: { pass: 1 },
      outcome_reasons: {},
      pass_rate: 1,
      independence: distribution,
      corrections: distribution,
      unreached_attempts: 0,
      uncovered: 0,
      facts: [
        {
          case_id: CASE,
          fact_id: hostile,
          kind: "requirement",
          phase: hostile,
          attempts: 1,
          met: 1,
          unmet: 0,
          unclear: 0,
          disputed: 0,
          own: 1,
          elicited: 0,
          corrected: 0,
          unreached: 0,
          raised: 0,
          raised_rate: 0,
          first_inspection_met: 0,
          first_inspected: 0,
          first_inspection_met_rate: null,
          regressions: 0,
        },
      ],
    },
    metrics: [{ case_id: CASE, metric_id: hostile, label: hostile, unit: null, values: distribution }],
  });
  const nodes = resultsView(report({ groups: [haiku] }), {});
  assert.ok(renderedText(nodes).includes(hostile));
  assert.equal(matching(nodes, (node) => node.nodeName === "SCRIPT").length, 0);
});

function caseView(extra = {}) {
  return {
    case_id: CASE,
    title: "Fix the greeting",
    initial_prompt: "Fix the greeting; ask which punctuation to use.",
    phases: ["fix", "docs"],
    facts: [
      {
        fact_id: "greeting",
        kind: "requirement",
        phase: "fix",
        disclosure: "on_request",
        text: "The greeting prints the expected text.",
        check: null,
        verifier: { argv: ["python3", "{verifier}/check.py"], cwd: ".", timeout_seconds: 60, max_output_bytes: 1024, environment: [] },
      },
      {
        fact_id: "punctuation",
        kind: "optional",
        phase: "docs",
        disclosure: "proactive",
        text: "Use a comma.",
        check: "The README shows Hello, NAME.",
        verifier: null,
      },
      { fact_id: "team", kind: "context", phase: null, disclosure: "on_request", text: "The team likes commas.", check: null, verifier: null },
    ],
    workflow: [{ step_id: "review", after_phase: "fix", prompt: "/review the change", slash: true }],
    rubrics: [
      { criterion_id: "quality", rubric: "The greeting keeps its interface.", origin: "case" },
      { criterion_id: "code-quality", rubric: "Default code quality rubric.", origin: "default" },
    ],
    disabled_rubrics: ["test-quality"],
    metrics: [
      {
        metrics_id: "greeting-metrics",
        description: "Measure how the final greeting differs from the baseline.",
        command: { argv: ["python3", "measure.py"], cwd: ".", timeout_seconds: 60, max_output_bytes: 1024, environment: [] },
      },
    ],
    budgets: { subject_seconds: 180, setup_seconds: 120 },
    tool_requirements: ["edit"],
    repository_id: "r".repeat(64),
    persona_id: "p".repeat(64),
    baseline_commit: "f".repeat(40),
    reference_patch: true,
    ...extra,
  };
}

test("caseDetail renders phases, facts with kinds and checks, prompt steps, rubrics and metrics", () => {
  const nodes = caseDetail(caseView(), {});
  const text = renderedText(nodes);
  assert.match(text, /Fix the greeting/);
  assert.match(text, /fix \u2192 docs/);
  const rows = matching(nodes, (node) => node.nodeName === "TR").map((row) => renderedText([row]));
  assert.ok(rows.includes("greetingrequirementfixon_requestThe greeting prints the expected text.verifier: python3 {verifier}/check.py"));
  assert.ok(rows.includes("punctuationoptionaldocsproactiveUse a comma.The README shows Hello, NAME."));
  assert.ok(rows.includes("teamcontext–on_requestThe team likes commas.not graded"));
  assert.ok(rows.includes("reviewfixslash command/review the change"));
  assert.ok(rows.includes("code-qualitydefaultDefault code quality rubric."));
  assert.match(text, /test-quality/);
  assert.match(text, /greeting-metrics/);
  assert.match(text, /measure\.py/);
});

test("caseDetail reports an empty state without a case and keeps hostile text inert", () => {
  const empty = caseDetail(null, {});
  assert.ok(renderedText(empty).includes("case ID"));
  const hostile = "<script>alert(1)</script>";
  const nodes = caseDetail(
    caseView({
      title: hostile,
      initial_prompt: hostile,
      facts: [{ fact_id: "x", kind: "requirement", phase: "fix", disclosure: "on_request", text: hostile, check: hostile, verifier: null }],
      workflow: [{ step_id: "review", after_phase: "fix", prompt: hostile }],
    }),
    {},
  );
  assert.ok(renderedText(nodes).split(hostile).length - 1 >= 5);
  assert.ok(renderedText(nodes).includes(hostile));
  assert.equal(matching(nodes, (node) => node.nodeName === "SCRIPT").length, 0);
});

test("resultsView opens with an agent summary of each variant and its latest attempts", () => {
  const measures = { turns: 2, seconds: 269.5, tokens: 1548589, cost: 0.906648, currency: "USD" };
  const sonnet = group("sonnet-high", {
    topline: { attempts: 1, ...measures, passes: 0, graded: 1, independence: 0.75, corrections: 2 },
  });
  const latest = {
    ...attempt,
    attempt_id: "a-4ff975651a324a13893fb9edf14b4bef",
    variant: "sonnet-high",
    topline: {
      latest: true,
      ...measures,
      outcome: "fail",
      outcome_reason: "budget_exhausted",
      independence: 0.75,
      corrections: 2,
    },
  };
  const retried = { ...latest, attempt_id: "a-103f02dc00", topline: { ...latest.topline, latest: false } };
  const nodes = resultsView(report({ groups: [sonnet], attempts: [retried, latest] }), {});
  const rows = matching(nodes[0], (node) => node.nodeName === "TR").map((row) => renderedText(row));
  assert.deepEqual(rows, [
    "Variant / attemptOutcomeIndependenceTurnsFirst checkCorrectionsWorkflowTimeTokensCost",
    "sonnet-high · mean of 10/10.752–2.00–4m 30s1.55M$0.91",
    "a-4ff97565…✗ budget_exhausted0.752–2–4m 30s1.55M$0.91",
  ]);
  const tones = matching(nodes[0], (node) => node.nodeName === "TD" && /check/.test(node.className)).map(
    (node) => node.className,
  );
  assert.deepEqual(tones, ["check fail", "check fail"]);
  assert.equal(renderedText(nodes[0]).startsWith("Summary"), true);
});

test("resultsView ends the summary with the pilot floor row and its budget", () => {
  const measures = { turns: 4, seconds: 60, tokens: 1000, cost: 1.5, currency: "USD" };
  const topline = (passes) => ({ attempts: 1, ...measures, passes, graded: 1, independence: 1, corrections: 0 });
  const sonnet = group("sonnet-high", { topline: topline(0) });
  const floor = {
    run_id: RUN,
    label: "pilot (n=1)",
    budget: { max_turns: 60, stand_in_replies: 40, max_inspections: 20 },
    group: group("pilot", { topline: topline(1) }),
  };
  const nodes = resultsView(report({ groups: [sonnet], attempts: [], floor }), {});
  const rows = matching(nodes[0], (node) => node.nodeName === "TR").map((row) => renderedText(row));
  assert.equal(rows.length, 3);
  assert.ok(
    rows[2].startsWith("pilot (n=1) · floor · budget 60 turns, 40 stand-in replies, 20 inspections"),
    rows[2],
  );
});

test("resultsView renders workflow topline cells matching the Python renderer's cells", () => {
  const measures = { turns: 2, seconds: 15, tokens: 1000, cost: 1.5, currency: "USD" };
  const sonnet = group("sonnet-high", {
    topline: {
      attempts: 1,
      ...measures,
      passes: 1,
      graded: 1,
      first_inspection_passes: 1,
      first_inspection_attempts: 1,
      corrections: 1,
      workflows_completed: 1,
      workflow_attempts: 1,
    },
  });
  const latest = {
    ...attempt,
    attempt_id: "a-4ff975651a324a13893fb9edf14b4bef",
    variant: "sonnet-high",
    topline: {
      latest: true,
      ...measures,
      outcome: "pass",
      outcome_reason: "all_met",
      first_inspection: "passed",
      corrections: 1,
      workflow: "2/2",
    },
  };
  const nodes = resultsView(report({ groups: [sonnet], attempts: [latest] }), {});
  const rows = matching(nodes[0], (node) => node.nodeName === "TR").map((row) => renderedText(row));
  assert.deepEqual(rows, [
    "Variant / attemptOutcomeIndependenceTurnsFirst checkCorrectionsWorkflowTimeTokensCost",
    "sonnet-high · mean of 11/1–21/11.001/115s1.0k$1.50",
    "a-4ff97565…✓–2✓12/215s1.0k$1.50",
  ]);
  const tones = matching(nodes[0], (node) => node.nodeName === "TD" && /check/.test(node.className)).map(
    (node) => node.className,
  );
  assert.deepEqual(tones, ["check pass", "check pass", "check pass", "check pass", "check pass"]);
});
