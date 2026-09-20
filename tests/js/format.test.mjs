import assert from "node:assert/strict";
import { test } from "node:test";

import {
  chipTitle,
  contentNotes,
  describeCalibration,
  describeDetail,
  describeGroup,
  describePairing,
  describePriceBasis,
  describeRoleTotal,
  describeRunRow,
  describeScan,
  describeSpend,
  errorMessage,
  formatCount,
  formatDistribution,
  formatMoney,
  formatRate,
  formatSpend,
  formatTimestamp,
  groupDefinitions,
  labelNames,
  labelledId,
  priceBasis,
  roleTotals,
  shortId,
  tokenLines,
} from "../../src/dryheave/resources/viewer/format.mjs";

function parts(text) {
  return text.split(" · ");
}

function part(text, pattern) {
  const found = parts(text).filter((item) => pattern.test(item));
  assert.equal(found.length, 1, `${pattern} matched ${found.length} part(s) of "${text}"`);
  return found[0];
}

test("formatCount distinguishes zero from unknown", () => {
  assert.equal(formatCount(0), "0");
  assert.equal(formatCount(7), "7");
  assert.equal(formatCount(null), "unknown");
  assert.equal(formatCount(undefined), "unknown");
  assert.equal(formatCount(1.5), "unknown");
});

test("formatRate reports unknown for a missing completion rate", () => {
  assert.equal(formatRate(0), "0.0%");
  assert.equal(formatRate(0.625), "62.5%");
  assert.equal(formatRate(null), "unknown");
});

test("formatDistribution reports observed and unknown values separately", () => {
  const distribution = {
    count: 3,
    unknown: 2,
    minimum: 1,
    maximum: 5,
    mean: 3,
    median: 3,
    stdev: 2,
  };
  assert.equal(
    formatDistribution(distribution),
    "n=3 · min 1.00 · median 3.00 · max 5.00 · mean 3.00 · sd 2.00 · 2 unknown",
  );
  assert.equal(
    formatDistribution({
      count: 0,
      unknown: 4,
      minimum: null,
      maximum: null,
      mean: null,
      median: null,
      stdev: null,
    }),
    "no observed values · 4 unknown",
  );
  assert.equal(
    formatDistribution({
      count: 1,
      unknown: 0,
      minimum: 2,
      maximum: 2,
      mean: 2,
      median: 2,
      stdev: null,
    }),
    "n=1 · min 2.00 · median 2.00 · max 2.00 · mean 2.00 · sd unknown",
  );
});

test("formatMoney keeps the recorded currency and flags an unrecorded one", () => {
  assert.equal(formatMoney(1.5, "USD"), "1.5000 USD");
  assert.equal(formatMoney(0, "EUR"), "0.0000 EUR");
  assert.equal(formatMoney(0.25, null), "0.2500 (currency unrecorded)");
  assert.equal(formatMoney(null, "USD"), "unknown");
});

test("formatSpend orders currencies and reports no observed spend", () => {
  assert.equal(formatSpend({ USD: 1, EUR: 2 }), "2.0000 EUR · 1.0000 USD");
  assert.equal(formatSpend({}), "none observed");
  assert.equal(formatSpend(null), "none observed");
});

test("describeSpend counts every retained attempt including unknown costs", () => {
  const text = describeSpend({
    known_spend_by_currency: { USD: 0.75 },
    unknown_cost_records: 2,
    partial_cost_attempts: 1,
  });
  assert.equal(
    text,
    "observed spend 0.7500 USD · 2 usage record(s) with unknown cost · " +
      "1 attempt(s) with partial telemetry · every retained attempt counted, retries included",
  );
  assert.match(
    describeSpend({
      known_spend_by_currency: {},
      unknown_cost_records: 0,
      partial_cost_attempts: 0,
    }),
    /observed spend none observed · no usage records with unknown cost/,
  );
});

test("groupDefinitions scopes the token distributions to the subject role", () => {
  const distribution = {
    count: 1,
    unknown: 0,
    minimum: 4,
    maximum: 4,
    mean: 4,
    median: 4,
    stdev: null,
  };
  const entries = groupDefinitions({
    durations: distribution,
    input_tokens: distribution,
    output_tokens: distribution,
    accepted_turns: distribution,
    costs: distribution,
  });
  assert.deepEqual(
    entries.map(([label]) => label),
    [
      "Subject duration (eligible)",
      "Subject uncached input tokens (eligible)",
      "Subject total output tokens (eligible)",
      "Accepted turns (eligible)",
      "Per-attempt cost across every role (single currency, complete telemetry only)",
    ],
  );
  assert.match(entries[1][1], /^n=1 · min 4 · median 4 · max 4 · mean 4 · sd unknown$/);
  assert.match(entries[4][1], /min 4\.0000/);
});

test("contentNotes labels base64 content and stays silent for text", () => {
  assert.deepEqual(contentNotes({ encoding: "base64" }), [
    "Content is not valid UTF-8; the exact bytes are shown base64-encoded.",
  ]);
  assert.deepEqual(contentNotes({ encoding: "utf-8" }), []);
  assert.deepEqual(contentNotes(null), []);
});

test("labelNames returns only recorded aliases and labelledId keeps the identifier", () => {
  const index = { ["a".repeat(64)]: ["nightly", "baseline"], ["b".repeat(64)]: "nightly" };
  assert.deepEqual(labelNames(index, "a".repeat(64)), ["nightly", "baseline"]);
  assert.deepEqual(labelNames(index, "b".repeat(64)), []);
  assert.deepEqual(labelNames(index, "c".repeat(64)), []);
  assert.deepEqual(labelNames(index, "constructor"), []);
  assert.deepEqual(labelNames(null, "a".repeat(64)), []);
  assert.equal(labelledId("a".repeat(64), ["nightly"]), `${"a".repeat(64)} (label nightly)`);
  assert.equal(labelledId("a".repeat(64), []), "a".repeat(64));
  assert.equal(labelledId(null, []), "unknown");
  assert.equal(
    chipTitle("experiment", "a".repeat(64), ["nightly", "baseline"]),
    `experiment: ${"a".repeat(64)} (label nightly, baseline)`,
  );
  assert.equal(chipTitle("run", "b".repeat(32), []), `run: ${"b".repeat(32)}`);
});

test("formatTimestamp normalizes to UTC seconds and keeps unparsable text", () => {
  assert.equal(formatTimestamp("2026-09-19T20:12:31.500000+00:00"), "2026-09-19 20:12:31Z");
  assert.equal(formatTimestamp("<script>"), "<script>");
  assert.equal(formatTimestamp(null), "unknown");
});

test("shortId truncates long identifiers and reports unknown", () => {
  assert.equal(shortId("0".repeat(64)), `${"0".repeat(12)}…`);
  assert.equal(shortId("abc"), "abc");
  assert.equal(shortId(null), "unknown");
});

test("tokenLines keeps reasoning as a subset of total output", () => {
  const lines = tokenLines({
    uncached_input: 10,
    cache_read: null,
    cache_write: 0,
    output: 300,
    reasoning: 120,
    provenance: "native",
  });
  assert.deepEqual(lines, [
    ["Uncached input", "10"],
    ["Cache read", "unknown"],
    ["Cache write", "0"],
    ["Total output", "300"],
    ["Reasoning (subset of output)", "120"],
    ["Provenance", "native"],
  ]);
  assert.deepEqual(tokenLines(null), []);
});

test("describeGroup keeps finished, scored and eligible distinct", () => {
  const text = describeGroup({
    attempts: 6,
    completed: 5,
    scored: 4,
    eligible: 3,
    eligible_passes: 1,
    eligible_completion_rate: 1 / 3,
    audit_excluded: 2,
    subjects_reporting_completion: 7,
  });
  assert.equal(parts(text).length, 8);
  assert.match(part(text, /attempt\(s\)$/), /^6 /);
  assert.match(part(text, /finished$/), /^5 /);
  assert.match(part(text, /scored$/), /^4 /);
  assert.match(part(text, /^\d+ eligible$/), /^3 /);
  assert.match(part(text, /pass\(es\)$/), /^1 /);
  assert.match(part(text, /^completion rate /), /33\.3% of eligible$/);
  assert.match(part(text, /audit-excluded$/), /^2 /);
  assert.match(part(text, /claiming completion$/), /^7 /);
  assert.match(
    describeGroup({
      attempts: 1,
      completed: 0,
      scored: 0,
      eligible: 0,
      eligible_passes: 0,
      eligible_completion_rate: null,
      audit_excluded: 1,
      subjects_reporting_completion: 0,
    }),
    /completion rate unknown of eligible/,
  );
});

test("describeRunRow marks a truncated journal prefix as a lower bound", () => {
  const whole = describeRunRow({
    run_id: "a".repeat(32),
    finished: 1,
    attempts: 2,
    mode: "offline-fixture",
    durable_sequence: 9,
    observed_events: 9,
    truncated: false,
  });
  assert.equal(parts(whole).length, 3);
  assert.match(part(whole, /finished$/), /^1 of 2 attempt\(s\)/);
  assert.equal(/at least/.test(whole), false);
  assert.match(part(whole, /^mode /), /offline-fixture$/);
  assert.match(part(whole, /^durable sequence /), /^durable sequence 9$/);
  const prefix = describeRunRow({
    run_id: "a".repeat(32),
    finished: 1,
    attempts: 2,
    mode: null,
    durable_sequence: null,
    observed_events: 4,
    truncated: true,
  });
  assert.equal(parts(prefix).length, 3);
  assert.match(part(prefix, /finished$/), /^at least 1 of at least 2 attempt\(s\)/);
  assert.match(part(prefix, /^mode /), /unknown$/);
  assert.match(part(prefix, /durable sequence/), /unknown; 4 event\(s\) read$/);
  const overrun = describeRunRow({
    run_id: "a".repeat(32),
    finished: 0,
    attempts: 0,
    mode: null,
    durable_sequence: null,
    observed_events: 0,
    truncated: true,
  });
  assert.match(
    part(overrun, /durable sequence/),
    /^durable sequence unknown; no complete event fit the row budget$/,
  );
  assert.equal(
    describeRunRow({ run_id: "a".repeat(32), error: "integrity_error" }),
    "Unreadable row: integrity_error",
  );
});

test("errorMessage surfaces the typed error envelope", () => {
  assert.equal(
    errorMessage(400, { error: { code: "invalid_input", message: "Bad reference." } }),
    "Bad reference. (invalid_input)",
  );
});

test("errorMessage falls back to the status when the body is not an envelope", () => {
  assert.equal(
    errorMessage(503, null),
    "The viewer returned HTTP 503 without a readable error body.",
  );
  assert.equal(
    errorMessage(500, { error: "boom" }),
    "The viewer returned HTTP 500 without a readable error body.",
  );
});

test("describeDetail explains omitted, invalid and refused reads", () => {
  const omitted = describeDetail({
    status: "omitted",
    reason: "The capture recorded why no final patch was retained.",
    reason_code: "capture_omission",
  });
  assert.match(omitted, /^Deliberately omitted by the capture\./);
  assert.ok(omitted.includes("The capture recorded why no final patch was retained."));
  assert.ok(omitted.includes("Reason code: capture_omission."));
  const invalid = describeDetail({
    status: "invalid",
    reason: null,
    reason_code: "integrity_error",
  });
  assert.match(invalid, /^Retained but rejected as invalid\./);
  assert.ok(invalid.includes("Reason code: integrity_error."));
  const refused = describeDetail({
    status: "refused",
    reason: null,
    reason_code: "serve_limit",
    limit_bytes: 4194304,
  });
  assert.match(refused, /^Refused before reading; over the viewer limit\./);
  assert.ok(refused.includes("Reason code: serve_limit."));
  assert.ok(refused.includes("Limit: 4.0 MiB."));
  assert.match(describeDetail({ status: "teleported" }), /^Unexpected status: teleported$/);
});

test("priceBasis preserves currencies, price tables and price dates", () => {
  const basis = priceBasis([
    {
      cost_kind: "estimated",
      model_basis: "observed",
      currency: "USD",
      price_version: "2026-01",
      price_date: "2026-01-02",
    },
    {
      cost_kind: "fixture",
      model_basis: "scripted",
      currency: null,
      price_version: null,
      price_date: null,
    },
    {
      cost_kind: "estimated",
      model_basis: "observed",
      currency: "EUR",
      price_version: "2026-01",
      price_date: "2026-02-03",
    },
  ]);
  assert.deepEqual(basis.currencies, ["EUR", "USD"]);
  assert.deepEqual(basis.versions, ["2026-01"]);
  assert.deepEqual(basis.dates, ["2026-01-02", "2026-02-03"]);
  assert.deepEqual(basis.kinds, [
    ["estimated", 2],
    ["fixture", 1],
  ]);
  const text = describePriceBasis(basis);
  assert.equal(parts(text).length, 5);
  assert.match(part(text, /^cost kinds /), /estimated×2, fixture×1$/);
  assert.match(part(text, /^model basis /), /observed×2, scripted×1$/);
  assert.match(part(text, /^currencies /), /EUR, USD$/);
  assert.match(part(text, /^price tables /), /2026-01$/);
  assert.match(part(text, /^price dates /), /2026-01-02, 2026-02-03$/);
  const empty = describePriceBasis(priceBasis([]));
  assert.match(part(empty, /^cost kinds /), /none$/);
  assert.match(part(empty, /^model basis /), /none$/);
  assert.match(part(empty, /^currencies /), /unrecorded$/);
  assert.match(part(empty, /^price tables /), /unrecorded$/);
  assert.match(part(empty, /^price dates /), /unrecorded$/);
});

test("roleTotals sums spend per currency and counts unknown costs", () => {
  const totals = roleTotals([
    {
      roles: [
        {
          role: "judge",
          coverage: "partial",
          known_tokens: { uncached_input: 20, output: 100, reasoning: 40 },
          records: [
            { cost: 0.5, currency: "USD" },
            { cost: null, currency: null },
          ],
        },
      ],
      result: null,
    },
    {
      roles: [
        {
          role: "subject",
          coverage: "fixture",
          known_tokens: { uncached_input: 5, output: null, reasoning: null },
          records: [{ cost: 0.25, currency: "USD" }],
        },
      ],
      result: null,
    },
  ]);
  assert.deepEqual(
    totals.map((total) => total.role),
    ["subject", "judge"],
  );
  const judge = totals[1];
  assert.deepEqual(judge.spend, { USD: 0.5 });
  assert.equal(judge.unknownCostRecords, 1);
  assert.equal(judge.knownOutput, 100);
  assert.equal(judge.knownReasoning, 40);
  const subject = totals[0];
  assert.equal(subject.knownOutput, 0);
  assert.equal(subject.unknownOutput, 1);
  assert.equal(subject.knownInput, 5);
  assert.match(
    describeRoleTotal(subject),
    /reasoning 0 \(unknown in 1 attempt-role set\(s\)\) of that output/,
  );
  assert.match(describeRoleTotal(judge), /1 usage record\(s\) with unknown cost/);
  assert.match(describeRoleTotal(subject), /coverage fixture \(offline\)×1/);
});

test("roleTotals prefers attempt roles over assessment roles", () => {
  const totals = roleTotals([
    {
      roles: [],
      result: {
        roles: [
          {
            role: "simulator",
            coverage: "complete",
            known_tokens: { uncached_input: 1, output: 2, reasoning: 1 },
            records: [{ cost: 1, currency: "EUR" }],
          },
        ],
      },
    },
  ]);
  assert.equal(totals.length, 1);
  assert.equal(totals[0].role, "simulator");
  assert.deepEqual(totals[0].spend, { EUR: 1 });
});

test("describeCalibration reports baseline and reference outcomes", () => {
  const text = describeCalibration({
    status: "demonstrated",
    baseline: { outcome: "fail" },
    reference: { outcome: "pass" },
  });
  assert.equal(parts(text).length, 3);
  assert.match(part(text, /^status /), /demonstrated$/);
  assert.match(part(text, /^baseline /), /fail$/);
  assert.match(part(text, /^reference /), /pass$/);
  const absent = describeCalibration({ status: "unavailable", baseline: null, reference: null });
  assert.match(part(absent, /^status /), /unavailable$/);
  assert.match(part(absent, /^baseline /), /not run$/);
  assert.match(part(absent, /^reference /), /not run$/);
  assert.equal(
    describeCalibration(null),
    "Not calibrated; judge criteria are assessed separately.",
  );
});

test("describeScan separates a complete scan from a truncated one", () => {
  const truncated = describeScan({
    complete: false,
    scanned: 10,
    unreadable: 1,
    attempted: 3,
    verified: 2,
  });
  assert.equal(parts(truncated).length, 5);
  assert.match(part(truncated, /^scan /), /^scan incomplete; more objects remain$/);
  assert.match(part(truncated, /object\(s\) scanned$/), /^10 /);
  assert.match(part(truncated, /unreadable, of any kind$/), /^1 scanned object\(s\) /);
  assert.match(part(truncated, /attempted within budget$/), /^3 verification\(s\) /);
  assert.match(part(truncated, /verified$/), /^2 of those verified$/);
  const whole = describeScan({
    complete: true,
    scanned: 7,
    unreadable: 0,
    attempted: 5,
    verified: 5,
  });
  assert.match(part(whole, /^scan /), /^scan complete$/);
  assert.match(part(whole, /object\(s\) scanned$/), /^7 /);
  assert.match(part(whole, /unreadable, of any kind$/), /^0 /);
  assert.match(part(whole, /attempted within budget$/), /^5 /);
  assert.match(part(whole, /verified$/), /^5 of those verified$/);
  assert.equal(describeScan(null), "unknown");
});

test("describeScan never reports more verified records than were verified", () => {
  const failed = describeScan({
    complete: true,
    scanned: 7,
    unreadable: 0,
    attempted: 32,
    verified: 0,
  });
  assert.match(part(failed, /attempted within budget$/), /^32 verification\(s\) /);
  assert.match(part(failed, /verified$/), /^0 of those verified$/);
  assert.equal(/32 of those verified/.test(failed), false);
  assert.equal(/32 verified/.test(failed), false);
});

test("describePairing reports latest-compatible pairing counts", () => {
  const text = describePairing({
    paired_count: 2,
    excluded_before: 1,
    excluded_after: 0,
    before_sequence: 12,
    after_sequence: 14,
  });
  assert.equal(parts(text).length, 5);
  assert.match(part(text, /latest-compatible pair\(s\)$/), /^2 /);
  assert.match(part(text, /^\d+ before attempt/), /^1 before attempt\(s\) unpaired$/);
  assert.match(part(text, /^\d+ after attempt/), /^0 after attempt\(s\) unpaired$/);
  assert.match(part(text, /^before sequence /), /12$/);
  assert.match(part(text, /^after sequence /), /14$/);
});

test("roleTotals matches the backend on a zero-cost record with no currency", () => {
  const totals = roleTotals([
    {
      roles: [
        {
          role: "subject",
          coverage: "fixture",
          known_tokens: { uncached_input: 0, output: 0, reasoning: 0 },
          records: [{ cost: 0, currency: null }],
        },
      ],
      result: null,
    },
  ]);
  assert.deepEqual(totals[0].spend, {});
  assert.equal(totals[0].unknownCostRecords, 0);
  assert.match(describeRoleTotal(totals[0]), /spend none observed/);
  const priced = roleTotals([
    {
      roles: [
        {
          role: "subject",
          coverage: "complete",
          known_tokens: { uncached_input: 1, output: 1, reasoning: 0 },
          records: [
            { cost: 0, currency: "USD" },
            { cost: 0.5, currency: null },
          ],
        },
      ],
      result: null,
    },
  ]);
  assert.deepEqual(priced[0].spend, { USD: 0, UNSPECIFIED: 0.5 });
});
