import assert from "node:assert/strict";
import { test } from "node:test";

import {
  attemptEligibility,
  attemptFacets,
  filterAttempts,
  filterCalibrations,
  filterRuns,
  matchesQuery,
  mergeCalibrations,
  moveSelection,
  pageBounds,
  resolveSelection,
} from "../../src/dryheave/resources/viewer/filters.mjs";

const attempts = [
  {
    attempt_id: "a1",
    trial_id: "t1",
    variant: "haiku",
    stage: "finished",
    mode: "native",
    raw_evidence: "available",
    current_exclusions: [],
    result: {
      completion: "pass",
      eligible: true,
      exclusion_reasons: [],
      case_id: "c".repeat(64),
      stop_reason: "completed",
      subject_observation: "completed",
    },
  },
  {
    attempt_id: "a2",
    trial_id: "t1",
    variant: "sonnet",
    stage: "finished",
    mode: "native",
    raw_evidence: "unavailable",
    current_exclusions: ["unscorable"],
    result: {
      completion: "indeterminate",
      eligible: false,
      exclusion_reasons: ["unscorable"],
      case_id: "d".repeat(64),
      stop_reason: "budget",
      subject_observation: null,
    },
  },
  {
    attempt_id: "a3",
    trial_id: "t2",
    variant: null,
    stage: "preparing",
    mode: null,
    raw_evidence: "unavailable",
    current_exclusions: [],
    result: null,
  },
];

test("matchesQuery requires every term", () => {
  assert.equal(matchesQuery(["alpha beta"], ""), true);
  assert.equal(matchesQuery(["alpha", "beta"], "alpha beta"), true);
  assert.equal(matchesQuery(["alpha"], "alpha gamma"), false);
  assert.equal(matchesQuery(["Alpha"], "alpha"), true);
  assert.equal(matchesQuery([null, undefined, 42], "42"), true);
});

test("filterRuns matches run id, mode and truncation", () => {
  const rows = [
    { run_id: "a".repeat(32), mode: "native", experiment_id: "e", error: null, truncated: false },
    { run_id: "b".repeat(32), mode: null, experiment_id: "f", error: null, truncated: true },
    { run_id: "c".repeat(32), mode: null, experiment_id: "g", error: "integrity_error" },
  ];
  assert.deepEqual(
    filterRuns(rows, "native").map((row) => row.run_id),
    ["a".repeat(32)],
  );
  assert.deepEqual(
    filterRuns(rows, "truncated").map((row) => row.run_id),
    ["b".repeat(32)],
  );
  assert.deepEqual(
    filterRuns(rows, "integrity").map((row) => row.run_id),
    ["c".repeat(32)],
  );
  assert.equal(filterRuns(rows, "").length, 3);
});

test("attemptEligibility separates eligible, excluded and unassessed attempts", () => {
  assert.equal(attemptEligibility(attempts[0]), "eligible");
  assert.equal(attemptEligibility(attempts[1]), "excluded");
  assert.equal(attemptEligibility(attempts[2]), "unassessed");
  assert.equal(
    attemptEligibility({ result: { eligible: true }, current_exclusions: ["late-exclusion"] }),
    "excluded",
  );
});

test("attemptEligibility reports an audited but ungraded attempt as ungraded", () => {
  assert.equal(
    attemptEligibility({
      current_exclusions: [],
      result: { phase: "audited", eligible: false, exclusion_reasons: [] },
    }),
    "ungraded",
  );
  assert.equal(
    attemptEligibility({
      current_exclusions: [],
      result: { phase: "audited", eligible: false, exclusion_reasons: ["capture_incomplete"] },
    }),
    "excluded",
  );
  assert.deepEqual(
    filterAttempts(
      [
        ...attempts,
        {
          attempt_id: "a4",
          trial_id: "t3",
          variant: null,
          stage: "grading",
          mode: "native",
          raw_evidence: "available",
          current_exclusions: [],
          result: { phase: "audited", eligible: false, exclusion_reasons: [] },
        },
      ],
      { eligibility: "ungraded" },
    ).map((item) => item.attempt_id),
    ["a4"],
  );
});

test("mergeCalibrations accumulates a continued run scan", () => {
  const first = {
    run_id: "a".repeat(32),
    experiment_id: "e".repeat(64),
    input_error: null,
    calibrations: { ["c".repeat(64)]: [{ calibration_id: "1".repeat(64) }] },
    scan: {
      complete: false,
      scanned: 2,
      unreadable: 1,
      attempted: 2,
      verified: 1,
      next_cursor: "9".repeat(64),
    },
  };
  const second = {
    run_id: "a".repeat(32),
    experiment_id: "e".repeat(64),
    input_error: null,
    calibrations: {
      ["c".repeat(64)]: [{ calibration_id: "2".repeat(64) }],
      ["d".repeat(64)]: [{ calibration_id: "3".repeat(64) }],
    },
    scan: {
      complete: true,
      scanned: 3,
      unreadable: 0,
      attempted: 3,
      verified: 2,
      next_cursor: null,
    },
  };
  const merged = mergeCalibrations(first, second);
  assert.deepEqual(merged.calibrations["c".repeat(64)].map((entry) => entry.calibration_id), [
    "1".repeat(64),
    "2".repeat(64),
  ]);
  assert.deepEqual(merged.calibrations["d".repeat(64)].map((entry) => entry.calibration_id), [
    "3".repeat(64),
  ]);
  assert.deepEqual(merged.scan, {
    complete: true,
    scanned: 5,
    unreadable: 1,
    attempted: 5,
    verified: 3,
    next_cursor: null,
  });
  assert.equal(merged.run_id, "a".repeat(32));
});

test("mergeCalibrations accumulates a continued case scan and keeps a first page", () => {
  const merged = mergeCalibrations(
    {
      case_id: "c".repeat(64),
      calibrations: [{ calibration_id: "1".repeat(64) }],
      scan: {
        complete: false,
        scanned: 1,
        unreadable: 0,
        attempted: 1,
        verified: 1,
        next_cursor: "8".repeat(64),
      },
    },
    {
      case_id: "c".repeat(64),
      calibrations: [{ calibration_id: "2".repeat(64) }],
      scan: {
        complete: false,
        scanned: 1,
        unreadable: 2,
        attempted: 1,
        verified: 0,
        next_cursor: "9".repeat(64),
      },
    },
  );
  assert.deepEqual(
    merged.calibrations.map((entry) => entry.calibration_id),
    ["1".repeat(64), "2".repeat(64)],
  );
  assert.equal(merged.scan.scanned, 2);
  assert.equal(merged.scan.unreadable, 2);
  assert.equal(merged.scan.attempted, 2);
  assert.equal(merged.scan.verified, 1);
  assert.equal(merged.scan.next_cursor, "9".repeat(64));
  const page = { calibrations: [], scan: { complete: true, scanned: 0 } };
  assert.equal(mergeCalibrations(null, page), page);
});

test("attemptFacets lists variants including the unnamed one", () => {
  const facets = attemptFacets(attempts);
  assert.deepEqual(facets.variants, ["(no variant)", "haiku", "sonnet"]);
  assert.deepEqual(facets.stages, ["finished", "preparing"]);
});

test("filterAttempts narrows by variant, stage and eligibility", () => {
  assert.deepEqual(
    filterAttempts(attempts, { variant: "haiku" }).map((item) => item.attempt_id),
    ["a1"],
  );
  assert.deepEqual(
    filterAttempts(attempts, { variant: "(no variant)" }).map((item) => item.attempt_id),
    ["a3"],
  );
  assert.deepEqual(
    filterAttempts(attempts, { stage: "finished" }).map((item) => item.attempt_id),
    ["a1", "a2"],
  );
  assert.deepEqual(
    filterAttempts(attempts, { eligibility: "unassessed" }).map((item) => item.attempt_id),
    ["a3"],
  );
  assert.deepEqual(
    filterAttempts(attempts, { variant: "haiku", eligibility: "excluded" }),
    [],
  );
  assert.equal(filterAttempts(attempts, {}).length, 3);
});

test("filterAttempts searches exclusion reasons and case ids", () => {
  assert.deepEqual(
    filterAttempts(attempts, { query: "unscorable" }).map((item) => item.attempt_id),
    ["a2"],
  );
  assert.deepEqual(
    filterAttempts(attempts, { query: "d".repeat(64) }).map((item) => item.attempt_id),
    ["a2"],
  );
  assert.deepEqual(
    filterAttempts(attempts, { query: "t1 pass" }).map((item) => item.attempt_id),
    ["a1"],
  );
});

test("filterCalibrations matches verification and omissions", () => {
  const entries = [
    {
      calibration_id: "1".repeat(64),
      case_id: "c".repeat(64),
      verification: "verified",
      status: "demonstrated",
      reason_code: null,
      omissions: [],
    },
    {
      calibration_id: "2".repeat(64),
      case_id: "c".repeat(64),
      verification: "failed",
      status: null,
      reason_code: "integrity_error",
      omissions: ["stdout"],
    },
  ];
  assert.deepEqual(
    filterCalibrations(entries, "failed").map((entry) => entry.calibration_id),
    ["2".repeat(64)],
  );
  assert.deepEqual(
    filterCalibrations(entries, "stdout").map((entry) => entry.calibration_id),
    ["2".repeat(64)],
  );
  assert.equal(filterCalibrations(entries, "").length, 2);
});

test("resolveSelection keeps a present selection and falls back to the first", () => {
  assert.equal(resolveSelection(["a", "b"], "b"), "b");
  assert.equal(resolveSelection(["a", "b"], "z"), "a");
  assert.equal(resolveSelection([], "a"), null);
});

test("moveSelection clamps at both ends", () => {
  assert.equal(moveSelection(["a", "b", "c"], "a", -1), "a");
  assert.equal(moveSelection(["a", "b", "c"], "a", 1), "b");
  assert.equal(moveSelection(["a", "b", "c"], "c", 1), "c");
  assert.equal(moveSelection([], "a", 1), null);
});

test("moveSelection starts from either end when nothing is selected", () => {
  assert.equal(moveSelection(["a", "b"], null, 1), "a");
  assert.equal(moveSelection(["a", "b"], null, -1), "b");
});

test("pageBounds reports the visible window and paging offsets", () => {
  const bounds = pageBounds({ offset: 25, limit: 25, total: 60, runs: new Array(25).fill({}) });
  assert.deepEqual(bounds, {
    first: 26,
    last: 50,
    total: 60,
    hasPrevious: true,
    hasNext: true,
    previousOffset: 0,
    nextOffset: 50,
  });
  const empty = pageBounds({ offset: 0, limit: 25, total: 0, runs: [] });
  assert.equal(empty.first, 0);
  assert.equal(empty.hasNext, false);
  assert.equal(empty.hasPrevious, false);
  assert.equal(pageBounds(null).total, 0);
});
