export const UNKNOWN = "unknown";

const DETAIL_TEXT = {
  ok: "Retained and readable.",
  unavailable: "Not retained.",
  invalid: "Retained but rejected as invalid.",
  omitted: "Deliberately omitted by the capture.",
  refused: "Refused before reading; over the viewer limit.",
};

const COVERAGE_TEXT = {
  complete: "complete",
  partial: "partial",
  unknown: UNKNOWN,
  fixture: "fixture (offline)",
};

export function isNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

export function formatCount(value) {
  return Number.isInteger(value) ? String(value) : UNKNOWN;
}

export function formatNumber(value, digits = 2) {
  return isNumber(value) ? value.toFixed(digits) : UNKNOWN;
}

export function formatSeconds(value) {
  return isNumber(value) ? `${value.toFixed(2)} s` : UNKNOWN;
}

export function formatRate(value) {
  return isNumber(value) ? `${(value * 100).toFixed(1)}%` : UNKNOWN;
}

export function formatBytes(value) {
  if (!Number.isInteger(value) || value < 0) {
    return UNKNOWN;
  }
  if (value < 1024) {
    return `${value} B`;
  }
  const units = ["KiB", "MiB", "GiB"];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(1)} ${units[unit]}`;
}

export function formatTimestamp(value) {
  if (typeof value !== "string" || value === "") {
    return UNKNOWN;
  }
  const parsed = Date.parse(value);
  if (Number.isNaN(parsed)) {
    return value;
  }
  return `${new Date(parsed).toISOString().slice(0, 19).replace("T", " ")}Z`;
}

export function shortId(value, head = 12) {
  if (typeof value !== "string" || value === "") {
    return UNKNOWN;
  }
  return value.length <= head ? value : `${value.slice(0, head)}…`;
}

export function formatMoney(amount, currency) {
  if (!isNumber(amount)) {
    return UNKNOWN;
  }
  return `${amount.toFixed(4)} ${currency ? currency : "(currency unrecorded)"}`;
}

export function formatSpend(byCurrency) {
  const entries = Object.entries(byCurrency ?? {});
  if (entries.length === 0) {
    return "none observed";
  }
  entries.sort((left, right) => (left[0] < right[0] ? -1 : left[0] > right[0] ? 1 : 0));
  return entries.map(([currency, amount]) => formatMoney(amount, currency)).join(" · ");
}

export function describeSpend(group) {
  if (!group) {
    return UNKNOWN;
  }
  const parts = [`observed spend ${formatSpend(group.known_spend_by_currency)}`];
  const records = group.unknown_cost_records ?? 0;
  const attempts = group.partial_cost_attempts ?? 0;
  parts.push(
    records === 0
      ? "no usage records with unknown cost"
      : `${records} usage record(s) with unknown cost`,
  );
  if (attempts > 0) {
    parts.push(`${attempts} attempt(s) with partial telemetry`);
  }
  parts.push("every retained attempt counted, retries included");
  return parts.join(" · ");
}

export function formatDistribution(distribution, format = formatNumber) {
  if (!distribution) {
    return UNKNOWN;
  }
  const observed = distribution.count ?? 0;
  const missing = distribution.unknown ?? 0;
  const head = observed === 0 ? "no observed values" : `n=${observed}`;
  const tail = missing === 0 ? "" : ` · ${missing} unknown`;
  if (observed === 0) {
    return `${head}${tail}`;
  }
  const body = [
    `min ${format(distribution.minimum)}`,
    `median ${format(distribution.median)}`,
    `max ${format(distribution.maximum)}`,
    `mean ${format(distribution.mean)}`,
    `sd ${format(distribution.stdev)}`,
  ].join(" · ");
  return `${head} · ${body}${tail}`;
}

export function groupDefinitions(group) {
  const whole = (value) => formatNumber(value, 0);
  return [
    ["Subject duration (eligible)", formatDistribution(group?.durations, formatSeconds)],
    ["Subject uncached input tokens (eligible)", formatDistribution(group?.input_tokens, whole)],
    ["Subject total output tokens (eligible)", formatDistribution(group?.output_tokens, whole)],
    ["Accepted turns (eligible)", formatDistribution(group?.accepted_turns, whole)],
    [
      "Per-attempt cost across every role (single currency, complete telemetry only)",
      formatDistribution(group?.costs, (value) => formatNumber(value, 4)),
    ],
  ];
}

export function tokenLines(usage) {
  if (!usage) {
    return [];
  }
  return [
    ["Uncached input", formatCount(usage.uncached_input)],
    ["Cache read", formatCount(usage.cache_read)],
    ["Cache write", formatCount(usage.cache_write)],
    ["Total output", formatCount(usage.output)],
    ["Reasoning (subset of output)", formatCount(usage.reasoning)],
    ["Provenance", usage.provenance || UNKNOWN],
  ];
}

export function describeCoverage(coverage) {
  return COVERAGE_TEXT[coverage] ?? UNKNOWN;
}

export function describeRole(role) {
  if (!role) {
    return UNKNOWN;
  }
  const tokens = role.known_tokens ?? null;
  return [
    `known cost ${formatMoney(role.known_cost, role.currency)}`,
    `output ${formatCount(tokens?.output)}`,
    `reasoning ${formatCount(tokens?.reasoning)} of that output`,
    `coverage ${describeCoverage(role.coverage)}`,
    `${(role.records ?? []).length} usage record(s)`,
  ].join(" · ");
}

export function priceBasis(records) {
  const kinds = new Map();
  const currencies = new Set();
  const versions = new Set();
  const dates = new Set();
  const bases = new Map();
  for (const record of records ?? []) {
    const kind = record.cost_kind ?? UNKNOWN;
    kinds.set(kind, (kinds.get(kind) ?? 0) + 1);
    const basis = record.model_basis ?? UNKNOWN;
    bases.set(basis, (bases.get(basis) ?? 0) + 1);
    if (record.currency) {
      currencies.add(record.currency);
    }
    if (record.price_version) {
      versions.add(record.price_version);
    }
    if (record.price_date) {
      dates.add(record.price_date);
    }
  }
  const sorted = (values) => [...values].sort();
  return {
    kinds: sorted(kinds.keys()).map((kind) => [kind, kinds.get(kind)]),
    bases: sorted(bases.keys()).map((basis) => [basis, bases.get(basis)]),
    currencies: sorted(currencies),
    versions: sorted(versions),
    dates: sorted(dates),
  };
}

export function describePriceBasis(basis) {
  if (!basis) {
    return UNKNOWN;
  }
  const list = (values, empty) => (values.length === 0 ? empty : values.join(", "));
  return [
    `cost kinds ${list(
      basis.kinds.map(([kind, count]) => `${kind}×${count}`),
      "none",
    )}`,
    `model basis ${list(
      basis.bases.map(([name, count]) => `${name}×${count}`),
      "none",
    )}`,
    `currencies ${list(basis.currencies, "unrecorded")}`,
    `price tables ${list(basis.versions, "unrecorded")}`,
    `price dates ${list(basis.dates, "unrecorded")}`,
  ].join(" · ");
}

export const ROLE_ORDER = ["subject", "simulator", "judge"];

function emptyTotal(role) {
  return {
    role,
    attempts: 0,
    records: [],
    spend: {},
    unknownCostRecords: 0,
    knownOutput: 0,
    unknownOutput: 0,
    knownReasoning: 0,
    unknownReasoning: 0,
    knownInput: 0,
    unknownInput: 0,
    coverage: {},
  };
}

function addTokens(total, usage) {
  const fields = [
    ["output", "knownOutput", "unknownOutput"],
    ["reasoning", "knownReasoning", "unknownReasoning"],
    ["uncached_input", "knownInput", "unknownInput"],
  ];
  for (const [field, known, unknown] of fields) {
    const value = usage ? usage[field] : null;
    if (Number.isInteger(value)) {
      total[known] += value;
    } else {
      total[unknown] += 1;
    }
  }
}

export function attemptRoles(attempt) {
  const own = attempt?.roles ?? [];
  if (own.length > 0) {
    return own;
  }
  return attempt?.result?.roles ?? [];
}

export function roleTotals(attempts) {
  const totals = new Map();
  for (const attempt of attempts ?? []) {
    for (const metrics of attemptRoles(attempt)) {
      const total = totals.get(metrics.role) ?? emptyTotal(metrics.role);
      total.attempts += 1;
      total.coverage[metrics.coverage] = (total.coverage[metrics.coverage] ?? 0) + 1;
      addTokens(total, metrics.known_tokens);
      for (const record of metrics.records ?? []) {
        total.records.push(record);
        if (!isNumber(record.cost)) {
          total.unknownCostRecords += 1;
        } else if (record.cost || record.currency) {
          const currency = record.currency || "UNSPECIFIED";
          total.spend[currency] = (total.spend[currency] ?? 0) + record.cost;
        }
      }
      totals.set(metrics.role, total);
    }
  }
  const ordered = [...totals.values()];
  ordered.sort((left, right) => ROLE_ORDER.indexOf(left.role) - ROLE_ORDER.indexOf(right.role));
  return ordered;
}

export function describeRoleTotal(total) {
  if (!total) {
    return UNKNOWN;
  }
  const missing = (count) => (count === 0 ? "" : ` (unknown in ${count} attempt-role set(s))`);
  const coverage = Object.keys(total.coverage)
    .sort()
    .map((name) => `${describeCoverage(name)}×${total.coverage[name]}`)
    .join(", ");
  return [
    `${total.attempts} attempt-role record set(s)`,
    `spend ${formatSpend(total.spend)}`,
    `${total.unknownCostRecords} usage record(s) with unknown cost`,
    `input ${total.knownInput}${missing(total.unknownInput)}`,
    `output ${total.knownOutput}${missing(total.unknownOutput)}`,
    `reasoning ${total.knownReasoning}${missing(total.unknownReasoning)} of that output`,
    `coverage ${coverage || "none"}`,
  ].join(" · ");
}

export function describeRunRow(row) {
  if (!row) {
    return UNKNOWN;
  }
  if (row.error) {
    return `Unreadable row: ${row.error}`;
  }
  const bound = row.truncated ? "at least " : "";
  const prefix =
    row.observed_events === 0
      ? "no complete event fit the row budget"
      : `${formatCount(row.observed_events)} event(s) read`;
  const sequence = row.truncated
    ? `durable sequence unknown; ${prefix}`
    : `durable sequence ${formatCount(row.durable_sequence)}`;
  return [
    `${bound}${formatCount(row.finished)} of ${bound}${formatCount(row.attempts)} attempt(s) finished`,
    `mode ${row.mode ?? UNKNOWN}`,
    sequence,
  ].join(" · ");
}

export function describeGroup(group) {
  if (!group) {
    return UNKNOWN;
  }
  return [
    `${formatCount(group.attempts)} attempt(s)`,
    `${formatCount(group.completed)} finished`,
    `${formatCount(group.scored)} scored`,
    `${formatCount(group.eligible)} eligible`,
    `${formatCount(group.eligible_passes)} eligible pass(es)`,
    `completion rate ${formatRate(group.eligible_completion_rate)} of eligible`,
    `${formatCount(group.audit_excluded)} audit-excluded`,
    `${formatCount(group.subjects_reporting_completion)} subject(s) claiming completion`,
  ].join(" · ");
}

export function groupLabel(group) {
  if (!group) {
    return UNKNOWN;
  }
  const variant = group.variant ? group.variant : "(no variant)";
  return `${variant} · ${group.mode ?? UNKNOWN}`;
}

export function describeCompletion(result) {
  if (!result) {
    return "No assessment retained for this attempt.";
  }
  const exclusions = result.exclusion_reasons ?? [];
  return [
    `completion ${result.completion ?? UNKNOWN}`,
    `deterministic ${result.deterministic_completion ?? UNKNOWN}`,
    result.eligible ? "eligible" : "not eligible",
    `phase ${result.phase ?? UNKNOWN}`,
    exclusions.length === 0 ? "no exclusions" : `excluded: ${exclusions.join(", ")}`,
  ].join(" · ");
}

export function describeAudit(audit) {
  if (!audit) {
    return "No audit retained.";
  }
  return [
    `input integrity ${audit.input_integrity ?? UNKNOWN}`,
    `capture integrity ${audit.capture_integrity ?? UNKNOWN}`,
    `cleanup ${audit.cleanup ?? UNKNOWN}`,
    audit.capture_complete ? "capture complete" : "capture incomplete",
    `${(audit.findings ?? []).length} finding(s)`,
  ].join(" · ");
}

export function describeCalibration(calibration) {
  if (!calibration) {
    return "Not calibrated; judge criteria are assessed separately.";
  }
  const outcome = (execution) => (execution ? (execution.outcome ?? UNKNOWN) : "not run");
  return [
    `status ${calibration.status ?? UNKNOWN}`,
    `baseline ${outcome(calibration.baseline)}`,
    `reference ${outcome(calibration.reference)}`,
  ].join(" · ");
}

export function describeDetail(view) {
  if (!view) {
    return UNKNOWN;
  }
  const base = DETAIL_TEXT[view.status] ?? `Unexpected status: ${view.status}`;
  const parts = [base];
  if (view.reason) {
    parts.push(view.reason);
  }
  if (view.reason_code) {
    parts.push(`Reason code: ${view.reason_code}.`);
  }
  if (view.status === "refused" && Number.isInteger(view.limit_bytes)) {
    parts.push(`Limit: ${formatBytes(view.limit_bytes)}.`);
  }
  return parts.join(" ");
}

export function describeScan(scan) {
  if (!scan) {
    return UNKNOWN;
  }
  return [
    scan.complete ? "scan complete" : "scan incomplete; more objects remain",
    `${formatCount(scan.scanned)} object(s) scanned`,
    `${formatCount(scan.unreadable)} scanned object(s) unreadable, of any kind`,
    `${formatCount(scan.attempted)} verification(s) attempted within budget`,
    `${formatCount(scan.verified)} of those verified`,
  ].join(" · ");
}

export function errorMessage(status, payload) {
  const envelope = payload && typeof payload === "object" ? payload.error : null;
  if (envelope && typeof envelope === "object" && typeof envelope.message === "string") {
    const code = typeof envelope.code === "string" ? envelope.code : "error";
    return `${envelope.message} (${code})`;
  }
  return `The viewer returned HTTP ${status} without a readable error body.`;
}

export function describePairing(comparison) {
  if (!comparison) {
    return UNKNOWN;
  }
  return [
    `${formatCount(comparison.paired_count)} latest-compatible pair(s)`,
    `${formatCount(comparison.excluded_before)} before attempt(s) unpaired`,
    `${formatCount(comparison.excluded_after)} after attempt(s) unpaired`,
    `before sequence ${formatCount(comparison.before_sequence)}`,
    `after sequence ${formatCount(comparison.after_sequence)}`,
  ].join(" · ");
}

export const BASE64_NOTE = "Content is not valid UTF-8; the exact bytes are shown base64-encoded.";

export function contentNotes(view) {
  return view && view.encoding === "base64" ? [BASE64_NOTE] : [];
}

export function labelNames(index, value) {
  if (!index || typeof value !== "string" || value === "") {
    return [];
  }
  const names = Object.prototype.hasOwnProperty.call(index, value) ? index[value] : null;
  return Array.isArray(names) ? names.filter((name) => typeof name === "string") : [];
}

export function labelledId(value, names) {
  const identifier = typeof value === "string" && value !== "" ? value : UNKNOWN;
  return names && names.length > 0 ? `${identifier} (label ${names.join(", ")})` : identifier;
}

export function chipTitle(label, value, names) {
  return `${label}: ${labelledId(value, names)}`;
}
