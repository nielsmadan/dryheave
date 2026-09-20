import { badge, block, bullets, definitions, el, heading, idChip, note, section, table } from "./dom.mjs";
import { NO_VARIANT, attemptEligibility } from "./filters.mjs";
import {
  UNKNOWN,
  describeAudit,
  describeCalibration,
  describeCompletion,
  describeCoverage,
  describeDetail,
  describeGroup,
  describePairing,
  describePriceBasis,
  describeRole,
  describeRoleTotal,
  describeRunRow,
  describeScan,
  describeSpend,
  attemptRoles,
  contentNotes,
  formatBytes,
  formatCount,
  formatMoney,
  formatNumber,
  formatSeconds,
  formatTimestamp,
  groupDefinitions,
  groupLabel,
  labelNames,
  labelledId,
  priceBasis,
  roleTotals,
  shortId,
  tokenLines,
} from "./format.mjs";

const STATE_TEXT = {
  loading: "Loading…",
  empty: "Nothing retained here.",
  error: "The viewer could not answer this request.",
};

export function stateCard(kind, message) {
  return el("section", { className: `card state state-${kind}`, attrs: { role: "status" } }, [
    el("p", { className: "state-text", text: message ?? STATE_TEXT[kind] ?? UNKNOWN }),
  ]);
}

export function runList(rows, selectedId, labels) {
  const list = el("ul", { className: "runs", attrs: { "aria-label": "Retained runs" } });
  for (const row of rows) {
    const selected = row.run_id === selectedId;
    const button = el(
      "button",
      {
        className: selected ? "run selected" : "run",
        attrs: {
          type: "button",
          "data-run": row.run_id,
          "aria-current": selected ? "true" : null,
        },
      },
      [
        el("code", { className: "run-id", text: row.run_id }),
        el("span", { className: "run-when", text: formatTimestamp(row.created_at) }),
        el("span", {
          className: row.error ? "run-detail corrupt" : "run-detail",
          text: describeRunRow(row),
        }),
      ],
    );
    const names = labelNames(labels, row.experiment_id);
    if (names.length > 0) {
      button.append(el("span", { className: "run-name", text: `experiment ${names.join(", ")}` }));
    }
    if (row.truncated) {
      button.append(badge("truncated prefix", "warn"));
    }
    if (row.error) {
      button.append(badge("unreadable", "bad"));
    }
    list.append(el("li", {}, [button]));
  }
  return list;
}

function groupsTable(groups) {
  return table(
    "Variant groups: finished, scored and eligible are distinct populations.",
    [
      "Group",
      "Scheduled",
      "Attempts",
      "Finished",
      "Scored",
      "Eligible",
      "Eligible passes",
      "Completion rate",
      "Audit-excluded",
      "Subjects claiming completion",
    ],
    groups.map((group) => [
      groupLabel(group),
      formatCount(group.scheduled_trials),
      formatCount(group.attempts),
      formatCount(group.completed),
      formatCount(group.scored),
      formatCount(group.eligible),
      formatCount(group.eligible_passes),
      group.eligible_completion_rate === null
        ? "unknown (no eligible attempts)"
        : `${(group.eligible_completion_rate * 100).toFixed(1)}%`,
      formatCount(group.audit_excluded),
      formatCount(group.subjects_reporting_completion),
    ]),
  );
}

function groupCard(group) {
  const reasons = Object.entries(group.exclusion_reasons ?? {}).sort((left, right) =>
    left[0] < right[0] ? -1 : 1,
  );
  return section(`Group ${groupLabel(group)}`, [
    note(describeGroup(group)),
    note(describeSpend(group), "note spend"),
    definitions(groupDefinitions(group)),
    reasons.length === 0
      ? note("No exclusion reasons recorded for this group.")
      : table(
          "Exclusion reasons",
          ["Reason", "Attempts"],
          reasons.map(([reason, count]) => [reason, String(count)]),
        ),
  ]);
}

function roleTotalsCard(attempts) {
  const totals = roleTotals(attempts);
  if (totals.length === 0) {
    return section("Role usage and cost", [
      note("No role accounting is retained for these attempts."),
    ]);
  }
  return section("Role usage and cost", [
    note("Spend covers every retained attempt, including superseded retries."),
    ...totals.flatMap((total) => [
      heading(4, total.role),
      note(describeRoleTotal(total)),
      note(describePriceBasis(priceBasis(total.records)), "note price"),
    ]),
  ]);
}

export function overview(report, labels) {
  const chip = (label, value) => idChip(label, value, labelNames(labels, value));
  const children = [
    section("Report", [
      el("p", { className: "chips" }, [
        chip("run", report.run_id),
        chip("experiment", report.experiment_id),
        chip("compatibility", report.compatibility_id),
      ]),
      definitions([
        ["Durable sequence", formatCount(report.durable_sequence)],
        ["Scheduled trials", formatCount(report.scheduled_trials)],
        ["Retained attempts", formatCount(report.attempts.length)],
        ["Portable report", report.portable ? "yes" : "no"],
        [
          "Unstarted trials",
          report.unstarted_trials.length === 0
            ? "none"
            : report.unstarted_trials.join(", "),
        ],
      ]),
      report.input_error ? note(`Input error: ${report.input_error}`, "note bad") : null,
      report.omissions.length > 0
        ? el("div", {}, [heading(4, "Omissions"), bullets(report.omissions)])
        : note("No omissions recorded."),
    ]),
  ];
  children.push(
    report.groups.length === 0
      ? stateCard("empty", "This report has no variant groups.")
      : section("Variant comparison", [groupsTable(report.groups)]),
  );
  for (const group of report.groups) {
    children.push(groupCard(group));
  }
  children.push(roleTotalsCard(report.attempts));
  children.push(section("Limitations", [bullets(report.limitations)]));
  return children;
}

export function attemptsTable(attempts, selectedId) {
  if (attempts.length === 0) {
    return stateCard("empty", "No attempt matches the current filter.");
  }
  const rows = attempts.map((attempt) => {
    const selected = attempt.attempt_id === selectedId;
    const button = el("button", {
      className: selected ? "pick selected" : "pick",
      text: attempt.attempt_id,
      attrs: {
        type: "button",
        "data-attempt": attempt.attempt_id,
        "aria-current": selected ? "true" : null,
      },
    });
    const exclusions = [
      ...(attempt.current_exclusions ?? []),
      ...(attempt.result?.exclusion_reasons ?? []),
    ];
    return [
      button,
      attempt.trial_id,
      attempt.variant ?? NO_VARIANT,
      attempt.stage,
      attempt.mode ?? UNKNOWN,
      attempt.result?.completion ?? "no assessment",
      attemptEligibility(attempt),
      exclusions.length === 0 ? "none" : exclusions.join(", "),
      attempt.raw_evidence,
    ];
  });
  return table(
    "Attempts, including superseded retries of the same trial.",
    [
      "Attempt",
      "Trial",
      "Variant",
      "Stage",
      "Mode",
      "Completion",
      "Eligibility",
      "Exclusions",
      "Raw evidence",
    ],
    rows,
  );
}

function criteriaCard(result) {
  if (!result || result.criteria.length === 0) {
    return section("Criteria", [note("No criterion results are retained.")]);
  }
  return section("Criteria", [
    table(
      "Criterion outcomes with their calibration evidence.",
      ["Criterion", "Kind", "Required", "Outcome", "Score", "Calibration", "Error"],
      result.criteria.map((criterion) => [
        criterion.criterion_id,
        criterion.kind,
        criterion.required ? "required" : "optional",
        criterion.outcome,
        criterion.score === null ? UNKNOWN : formatNumber(criterion.score, 3),
        describeCalibration(criterion.calibration),
        criterion.error ?? "none",
      ]),
    ),
    table(
      "Criterion check executions.",
      ["Criterion", "Observed", "Return code", "Process", "Elapsed", "stdout", "stderr"],
      result.criteria.map((criterion) => {
        const execution = criterion.execution;
        return [
          criterion.criterion_id,
          execution ? (execution.execution_observed ? "yes" : "no") : "not run",
          execution ? formatCount(execution.returncode) : UNKNOWN,
          execution?.process_outcome ?? UNKNOWN,
          execution ? formatSeconds(execution.elapsed_seconds) : UNKNOWN,
          shortId(execution?.stdout_sha256),
          shortId(execution?.stderr_sha256),
        ];
      }),
    ),
  ]);
}

function auditCard(result) {
  if (!result) {
    return section("Audit", [note("No assessment retained for this attempt.")]);
  }
  const audit = result.audit;
  const findings =
    audit.findings.length === 0
      ? note("No audit findings.")
      : table(
          "Audit findings.",
          ["Finding", "Rule", "Confidence", "Description", "Evidence hash"],
          audit.findings.map((finding) => [
            finding.finding_id,
            finding.rule_id,
            finding.confidence,
            finding.description,
            shortId(finding.evidence_hash),
          ]),
        );
  const reviews =
    result.reviews.length === 0
      ? note("No audit reviews.")
      : table(
          "Audit reviews.",
          ["Finding", "Decision", "Reviewer", "Reason", "Reviewed at"],
          result.reviews.map((review) => [
            review.finding_id,
            review.decision,
            review.reviewer,
            review.reason,
            formatTimestamp(review.reviewed_at),
          ]),
        );
  return section("Audit", [
    note(describeAudit(audit)),
    definitions([
      ["Coverage", audit.coverage.length === 0 ? "none" : audit.coverage.join(", ")],
      ["Evidence complete", result.evidence_complete ? "yes" : "no"],
      ["Stop reason", result.stop_reason],
      ["Subject observation", result.subject_observation ?? UNKNOWN],
    ]),
    findings,
    reviews,
  ]);
}

function rolesCard(attempt) {
  const roles = attemptRoles(attempt);
  if (roles.length === 0) {
    return section("Role usage and cost", [note("No role accounting retained.")]);
  }
  return section(
    "Role usage and cost",
    roles.flatMap((role) => [
      heading(4, role.role),
      note(describeRole(role)),
      definitions(tokenLines(role.known_tokens)),
      note(describePriceBasis(priceBasis(role.records)), "note price"),
      role.reasons.length === 0
        ? note("No coverage reasons recorded.")
        : bullets(role.reasons),
      role.records.length === 0
        ? note("No usage records retained.")
        : table(
            `Usage records for ${role.role}.`,
            [
              "Identity",
              "Observed model",
              "Requested model",
              "Model basis",
              "Cost",
              "Cost kind",
              "Price table",
              "Price date",
              "Reason",
              "Raw evidence",
            ],
            role.records.map((record) => [
              record.identity,
              record.observed_model ?? UNKNOWN,
              record.requested_model ?? UNKNOWN,
              record.model_basis,
              formatMoney(record.cost, record.currency),
              record.cost_kind,
              record.price_version ?? "unrecorded",
              record.price_date ?? "unrecorded",
              record.reason,
              shortId(record.raw_evidence_sha256),
            ]),
          ),
    ]),
  );
}

function dialogueCard(view) {
  if (!view) {
    return section("Dialogue", [note("Not requested.")]);
  }
  const children = [
    note(describeDetail(view)),
    el("p", { className: "chips" }, [
      idChip("capture", view.capture_id),
      idChip("quarantine", view.quarantine_id),
    ]),
  ];
  if (view.quarantined) {
    children.push(note("This capture is quarantined.", "note warn"));
  }
  if (view.interaction_coverage) {
    children.push(note(`Interaction coverage: ${view.interaction_coverage}.`));
  }
  if (view.capture_errors.length > 0) {
    children.push(heading(4, "Capture errors"), bullets(view.capture_errors));
  }
  if (view.evidence_omissions.length > 0) {
    children.push(heading(4, "Evidence omissions"), bullets(view.evidence_omissions));
  }
  if (view.messages.length === 0) {
    children.push(note("No dialogue turns retained."));
  } else {
    const log = el("ol", { className: "dialogue" });
    for (const message of view.messages) {
      log.append(
        el("li", { className: `turn ${message.role}` }, [
          el("span", { className: "turn-role", text: message.role }),
          block(message.text),
        ]),
      );
    }
    children.push(log);
  }
  return section("Dialogue", children);
}

function patchCard(view) {
  if (!view) {
    return section("Final patch", [note("Not requested.")]);
  }
  const children = [
    note(describeDetail(view)),
    definitions([
      ["Patch file", view.name ?? "none retained"],
      [
        "Workspace complete",
        view.workspace_complete === null ? UNKNOWN : view.workspace_complete ? "yes" : "no",
      ],
      ["Bytes", view.bytes === null ? UNKNOWN : formatBytes(view.bytes)],
      ["SHA-256", view.sha256 ?? UNKNOWN],
      ["Encoding", view.encoding ?? UNKNOWN],
    ]),
  ];
  if (view.omissions.length > 0) {
    children.push(
      table(
        "Recorded capture omissions.",
        ["Path", "Reason", "Intentional"],
        view.omissions.map((omission) => [
          omission.path,
          omission.reason,
          omission.intentional ? "yes" : "no",
        ]),
      ),
    );
  }
  for (const text of contentNotes(view)) {
    children.push(note(text));
  }
  children.push(view.content === null ? note("No patch content served.") : block(view.content));
  return section("Final patch", children);
}

function evidenceCard(listing, file) {
  if (!listing) {
    return section("Verifier evidence", [note("Not requested.")]);
  }
  const children = [
    note(describeDetail(listing)),
    el("p", { className: "chips" }, [
      idChip("assessment", listing.assessment_id),
      idChip("evidence", listing.evidence_id),
    ]),
    definitions([
      [
        "Complete",
        listing.complete === null ? UNKNOWN : listing.complete ? "yes" : "no",
      ],
      ["Files", formatCount(listing.files.length)],
    ]),
  ];
  if (listing.omissions.length > 0) {
    children.push(heading(4, "Omissions"), bullets(listing.omissions));
  }
  if (listing.files.length === 0) {
    children.push(note("No evidence files retained."));
  } else {
    const picker = el("ul", { className: "files" });
    for (const name of listing.files) {
      const selected = file && file.name === name;
      picker.append(
        el("li", {}, [
          el("button", {
            className: selected ? "pick selected" : "pick",
            text: name,
            attrs: {
              type: "button",
              "data-evidence": name,
              "aria-current": selected ? "true" : null,
            },
          }),
        ]),
      );
    }
    children.push(picker);
  }
  if (file) {
    children.push(heading(4, file.name));
    children.push(note(describeDetail(file)));
    children.push(
      definitions([
        ["Bytes", file.bytes === null ? UNKNOWN : formatBytes(file.bytes)],
        ["SHA-256", file.sha256 ?? UNKNOWN],
        ["Encoding", file.encoding ?? UNKNOWN],
      ]),
    );
    for (const text of contentNotes(file)) {
      children.push(note(text));
    }
    children.push(file.content === null ? note("No content served.") : block(file.content));
  }
  return section("Verifier evidence", children);
}

export function attemptDetail(attempt, details, labels) {
  const result = attempt.result;
  const chip = (label, value) => idChip(label, value, labelNames(labels, value));
  return [
    section(`Attempt ${attempt.attempt_id}`, [
      el("p", { className: "chips" }, [
        chip("trial", attempt.trial_id),
        chip("variant", attempt.variant ?? NO_VARIANT),
        chip("capture", attempt.capture_id),
        chip("quarantine", attempt.quarantine_id),
        chip("assessment", attempt.assessment_id),
        chip("pairing", attempt.pairing_id),
        chip("case", result?.case_id),
        chip("profile", result?.profile_id),
        chip("simulator", result?.simulator_id),
        chip("scoring", result?.scoring_id),
        chip("criteria", result?.criteria_id),
      ]),
      note(describeCompletion(result)),
      definitions([
        ["Stage", attempt.stage],
        ["Execution mode", attempt.mode ?? UNKNOWN],
        ["Raw evidence", attempt.raw_evidence],
        ["Repetition", formatCount(result?.repetition)],
        ["Seed", formatCount(result?.seed)],
        ["Created at", formatTimestamp(result?.created_at)],
        ["Accepted turns", formatCount(result?.accepted_turns)],
        ["Subject seconds", formatSeconds(result?.subject_seconds)],
        ["Setup seconds", formatSeconds(result?.setup_seconds)],
        ["Previous assessment", shortId(result?.previous_id)],
      ]),
      result && result.limitations.length > 0
        ? el("div", {}, [heading(4, "Assessment limitations"), bullets(result.limitations)])
        : null,
    ]),
    auditCard(result),
    criteriaCard(result),
    rolesCard(attempt),
    dialogueCard(details.dialogue),
    patchCard(details.patch),
    evidenceCard(details.evidence, details.evidenceFile),
  ];
}

export function progressView(progress, labels) {
  return [
    section("Live progress", [
      el("p", { className: "chips" }, [
        idChip("run", progress.run_id, labelNames(labels, progress.run_id)),
        idChip("experiment", progress.experiment_id, labelNames(labels, progress.experiment_id)),
      ]),
      definitions([
        ["Created at", formatTimestamp(progress.created_at)],
        ["Durable sequence", formatCount(progress.durable_sequence)],
        ["Mode", progress.mode ?? UNKNOWN],
        ["Attempts", formatCount(progress.attempts.length)],
      ]),
      progress.attempts.length === 0
        ? note("No attempts have been reserved yet.")
        : table(
            "Attempt stages recorded in the run journal.",
            ["Attempt", "Trial", "Stage", "Capture", "Quarantine", "Assessment"],
            progress.attempts.map((attempt) => [
              attempt.attempt_id,
              attempt.trial_id,
              attempt.stage,
              shortId(attempt.capture_id),
              shortId(attempt.quarantine_id),
              shortId(attempt.assessment_id),
            ]),
          ),
    ]),
  ];
}

function calibrationRows(entries, labels) {
  return table(
    "Calibration records verified within the viewer's per-request budget.",
    ["Calibration", "Case", "Verification", "Status", "Created at", "Evidence complete", "Reason"],
    entries.map((entry) => [
      entry.calibration_id,
      labelledId(entry.case_id, labelNames(labels, entry.case_id)),
      entry.verification,
      entry.status ?? UNKNOWN,
      formatTimestamp(entry.created_at),
      entry.evidence_complete === null
        ? UNKNOWN
        : entry.evidence_complete
          ? "yes"
          : `no (${entry.omissions.join(", ") || "unrecorded"})`,
      entry.reason ?? "none",
    ]),
  );
}

export function calibrationView(view, entries, labels) {
  const children = [
    note(describeScan(view.scan)),
    view.input_error ? note(`Experiment input error: ${view.input_error}`, "note bad") : null,
  ];
  if (view.scan.next_cursor) {
    children.push(
      note(`Next scan cursor: ${view.scan.next_cursor}`, "note warn"),
      el("p", { className: "pager" }, [
        el("button", {
          className: "continue",
          text: "Continue scan",
          attrs: { type: "button", "data-scan": "continue" },
        }),
      ]),
    );
  }
  children.push(
    entries.length === 0
      ? note("No calibration record matched.")
      : calibrationRows(entries, labels),
  );
  return [section("Criteria calibration", children)];
}

export function compareView(comparison, labels) {
  const metrics = (title, groups) =>
    section(title, [
      groups.length === 0 ? note("No groups retained.") : groupsTable(groups),
    ]);
  const pairs =
    comparison.pairs.length === 0
      ? note("No trial paired across these runs.")
      : table(
          "Latest-compatible attempt pairs.",
          [
            "Case",
            "Repetition",
            "Before attempt",
            "After attempt",
            "Before completion",
            "After completion",
            "Criterion changes",
          ],
          comparison.pairs.map((pair) => [
            shortId(pair.case_id),
            String(pair.repetition),
            pair.before_attempt,
            pair.after_attempt,
            pair.before_completion,
            pair.after_completion,
            Object.entries(pair.criterion_changes)
              .sort((left, right) => (left[0] < right[0] ? -1 : 1))
              .map(([name, [before, after]]) => `${name}: ${before} → ${after}`)
              .join("; ") || "none",
          ]),
        );
  return [
    section("Comparison", [
      el("p", { className: "chips" }, [
        idChip("before run", comparison.before, labelNames(labels, comparison.before)),
        idChip("after run", comparison.after, labelNames(labels, comparison.after)),
      ]),
      definitions([
        ["Before input", comparison.before_input],
        ["After input", comparison.after_input],
        ["Before reference", comparison.before_reference],
        ["After reference", comparison.after_reference],
        ["Before variant", comparison.before_variant ?? "all variants"],
        ["After variant", comparison.after_variant ?? "all variants"],
      ]),
      note(describePairing(comparison)),
      pairs,
    ]),
    metrics("Before metrics", comparison.before_metrics),
    metrics("After metrics", comparison.after_metrics),
    section("Limitations", [bullets(comparison.limitations)]),
  ];
}

export function targetLine(session, labels) {
  if (!session || !session.target) {
    return "No focused target; showing every retained run in this store.";
  }
  const target = session.target;
  return `Focused ${target.kind}: ${labelledId(target.id, labelNames(labels, target.id))}`;
}
