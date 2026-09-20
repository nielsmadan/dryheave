export const NO_VARIANT = "(no variant)";

export function matchesQuery(fields, query) {
  const terms = String(query ?? "")
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);
  if (terms.length === 0) {
    return true;
  }
  const haystack = (fields ?? [])
    .filter((field) => typeof field === "string" || typeof field === "number")
    .map((field) => String(field).toLowerCase())
    .join(" ");
  return terms.every((term) => haystack.includes(term));
}

export function runFields(row) {
  return [
    row.run_id,
    row.mode,
    row.experiment_id,
    row.error,
    row.truncated ? "truncated" : "complete",
  ];
}

export function filterRuns(rows, query) {
  return (rows ?? []).filter((row) => matchesQuery(runFields(row), query));
}

export function attemptFields(attempt) {
  const result = attempt.result ?? null;
  return [
    attempt.attempt_id,
    attempt.trial_id,
    attempt.variant ?? NO_VARIANT,
    attempt.stage,
    attempt.mode,
    attempt.raw_evidence,
    result?.completion,
    result?.case_id,
    result?.stop_reason,
    result?.subject_observation,
    result?.eligible ? "eligible" : "not-eligible",
    ...(attempt.current_exclusions ?? []),
    ...(result?.exclusion_reasons ?? []),
  ];
}

export function attemptFacets(attempts) {
  const variants = new Set();
  const stages = new Set();
  for (const attempt of attempts ?? []) {
    variants.add(attempt.variant ?? NO_VARIANT);
    if (attempt.stage) {
      stages.add(attempt.stage);
    }
  }
  return { variants: [...variants].sort(), stages: [...stages].sort() };
}

export function attemptEligibility(attempt) {
  const result = attempt.result;
  if (!result) {
    return "unassessed";
  }
  if (result.eligible && (attempt.current_exclusions ?? []).length === 0) {
    return "eligible";
  }
  const excluded = [...(attempt.current_exclusions ?? []), ...(result.exclusion_reasons ?? [])];
  return excluded.length === 0 ? "ungraded" : "excluded";
}

export function filterAttempts(attempts, criteria = {}) {
  const { query = "", variant = "", stage = "", eligibility = "" } = criteria;
  return (attempts ?? []).filter((attempt) => {
    if (variant && (attempt.variant ?? NO_VARIANT) !== variant) {
      return false;
    }
    if (stage && attempt.stage !== stage) {
      return false;
    }
    if (eligibility && attemptEligibility(attempt) !== eligibility) {
      return false;
    }
    return matchesQuery(attemptFields(attempt), query);
  });
}

export function calibrationFields(entry) {
  return [
    entry.calibration_id,
    entry.case_id,
    entry.verification,
    entry.status,
    entry.reason_code,
    ...(entry.omissions ?? []),
  ];
}

export function filterCalibrations(entries, query) {
  return (entries ?? []).filter((entry) => matchesQuery(calibrationFields(entry), query));
}

export function resolveSelection(ids, current) {
  const list = ids ?? [];
  if (list.includes(current)) {
    return current;
  }
  return list.length > 0 ? list[0] : null;
}

export function moveSelection(ids, current, delta) {
  const list = ids ?? [];
  if (list.length === 0) {
    return null;
  }
  const index = list.indexOf(current);
  if (index < 0) {
    return delta > 0 ? list[0] : list[list.length - 1];
  }
  const next = Math.min(list.length - 1, Math.max(0, index + delta));
  return list[next];
}

export function pageBounds(listing) {
  const offset = listing?.offset ?? 0;
  const limit = listing?.limit ?? 0;
  const total = listing?.total ?? 0;
  const shown = (listing?.runs ?? []).length;
  return {
    first: total === 0 ? 0 : offset + 1,
    last: offset + shown,
    total,
    hasPrevious: offset > 0,
    hasNext: offset + shown < total,
    previousOffset: Math.max(0, offset - limit),
    nextOffset: offset + limit,
  };
}

function mergeCalibrationEntries(previous, next) {
  if (Array.isArray(next)) {
    return [...(Array.isArray(previous) ? previous : []), ...next];
  }
  const merged = new Map();
  for (const source of [previous, next]) {
    for (const [caseId, entries] of Object.entries(source ?? {})) {
      merged.set(caseId, [
        ...(merged.get(caseId) ?? []),
        ...(Array.isArray(entries) ? entries : []),
      ]);
    }
  }
  return Object.fromEntries(merged);
}

export function mergeCalibrations(previous, next) {
  if (!previous) {
    return next;
  }
  const total = (name) => (previous.scan?.[name] ?? 0) + (next.scan?.[name] ?? 0);
  return {
    ...next,
    calibrations: mergeCalibrationEntries(previous.calibrations, next.calibrations),
    scan: {
      ...next.scan,
      scanned: total("scanned"),
      unreadable: total("unreadable"),
      attempted: total("attempted"),
      verified: total("verified"),
    },
  };
}
