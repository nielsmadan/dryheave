import { badge, block, bullets, caseLink, definitions, el, heading, idChip, note, section, table } from "./dom.mjs";
import { NO_VARIANT } from "./filters.mjs";
import {
  CHECK_MARKS,
  INSPECTION_CLASSES,
  INSPECTION_MARKS,
  TOPLINE_LEGEND,
  UNKNOWN,
  checkLabel,
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
  formatCost,
  formatCount,
  formatDistribution,
  formatDuration,
  formatMetricValue,
  formatMoney,
  formatNumber,
  formatRate,
  formatSeconds,
  formatTimestamp,
  formatTokens,
  formatTurns,
  groupDefinitions,
  groupLabel,
  labelNames,
  labelledId,
  priceBasis,
  reasoningMap,
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

function sortCaseId(left, right) {
  if (left[0] !== right[0]) {
    return left[0] < right[0] ? -1 : 1;
  }
  return left[1] < right[1] ? -1 : left[1] > right[1] ? 1 : 0;
}

function unionCaseKeys(groups, name, idField) {
  const keys = new Map();
  for (const group of groups) {
    for (const item of group[name] ?? []) {
      keys.set(JSON.stringify([item.case_id, item[idField]]), [item.case_id, item[idField]]);
    }
  }
  return [...keys.values()].sort(sortCaseId);
}

function findByCaseAndId(items, caseId, id, idField) {
  return (items ?? []).find((item) => item.case_id === caseId && item[idField] === id) ?? null;
}

function resultsSummaryTable(groups) {
  const whole = (value) => formatNumber(value, 0);
  const money = (value) => formatNumber(value, 4);
  const currencySuffix = (group, dist) => {
    if (!dist || dist.count === 0) {
      return "";
    }
    const currencies = group.price_basis?.currencies ?? [];
    return currencies.length === 0 ? "" : ` ${currencies.join("/")}`;
  };
  const moneyCell = (selector) => (group) => {
    const dist = selector(group);
    return `${formatDistribution(dist, money)}${currencySuffix(group, dist)}`;
  };
  const RETAINED = "all retained attempts, retries included";
  const ELIGIBLE = "eligible attempts only";
  const measures = [
    [
      `Cost (API-equivalent estimate) total (${RETAINED})`,
      moneyCell((group) => group.costs),
    ],
    [
      `Cost by role: subject (API-equivalent estimate, ${RETAINED})`,
      moneyCell((group) => group.cost_by_role?.subject),
    ],
    [
      `Cost by role: simulator (API-equivalent estimate, ${RETAINED})`,
      moneyCell((group) => group.cost_by_role?.simulator),
    ],
    [
      `Cost by role: judge (API-equivalent estimate, ${RETAINED})`,
      moneyCell((group) => group.cost_by_role?.judge),
    ],
    [
      `Price basis (${RETAINED})`,
      (group) => describePriceBasis(group.price_basis),
    ],
    [
      `Total input tokens (${RETAINED})`,
      (group) => formatDistribution(group.total_input_tokens, whole),
    ],
    [
      `Cache read tokens (${RETAINED})`,
      (group) => formatDistribution(group.cache_read_tokens, whole),
    ],
    [
      `Cache write tokens (${RETAINED})`,
      (group) => formatDistribution(group.cache_write_tokens, whole),
    ],
    [`Subject output tokens (${ELIGIBLE})`, (group) => formatDistribution(group.output_tokens, whole)],
    [`Accepted turns (${ELIGIBLE})`, (group) => formatDistribution(group.accepted_turns, whole)],
    [`Subject duration (${ELIGIBLE})`, (group) => formatDistribution(group.durations, formatSeconds)],
    [
      `Simulator replies, help needed (${ELIGIBLE})`,
      (group) => formatDistribution(group.simulator_replies, whole),
    ],
  ];
  return table(
    "Cost is an API-equivalent estimate from a dated price table, not a provider invoice; "
      + "each row's label states whether it covers every retained attempt (retries included) "
      + "or eligible attempts only.",
    ["Measure", ...groups.map(groupLabel)],
    measures.map(([label, render]) => [label, ...groups.map((group) => render(group))]),
  );
}

function factsDisclosedText(summary) {
  if (!summary) {
    return "not measured";
  }
  const facts = summary.facts ?? [];
  const suffix = summary.unmeasured ? ` (${summary.unmeasured} attempt(s) not measured)` : "";
  if (facts.length === 0) {
    return `none disclosed${suffix}`;
  }
  return `${facts.map((item) => `${item.fact_id} (${item.kind} ×${item.count})`).join(", ")}${suffix}`;
}

function helpNeededTable(groups) {
  return table(
    "Simulator replies and facts disclosed by variant, among eligible attempts.",
    ["Variant", "Simulator replies", "Facts disclosed"],
    groups.map((group) => [
      groupLabel(group),
      formatDistribution(group.simulator_replies, (value) => formatNumber(value, 0)),
      factsDisclosedText(group.help_needed),
    ]),
  );
}

function criteriaResultsTable(groups, labels) {
  const keys = unionCaseKeys(groups, "criteria", "criterion_id");
  if (keys.length === 0) {
    return note("No criterion results are retained for any variant.");
  }
  return table(
    "Per-criterion pass rate and mean score by variant, among eligible attempts.",
    ["Case", "Criterion", ...groups.map(groupLabel)],
    keys.map(([caseId, criterionId]) => [
      caseLink(caseId, labelNames(labels, caseId)),
      criterionId,
      ...groups.map((group) => {
        const item = findByCaseAndId(group.criteria, caseId, criterionId, "criterion_id");
        if (!item) {
          return "not measured";
        }
        return `${formatCount(item.passes)}/${formatCount(item.graded)} pass · mean score ${formatNumber(item.scores.mean, 2)}`;
      }),
    ]),
  );
}

function checksResultsTable(groups, labels) {
  const keys = unionCaseKeys(groups, "checks", "check_id");
  if (keys.length === 0) {
    return note("No case checks are retained for any variant.");
  }
  return table(
    "Per-check outcomes and agreement across repetitions by variant, among eligible attempts.",
    ["Case", "Check", ...groups.map(groupLabel)],
    keys.map(([caseId, checkId]) => [
      caseLink(caseId, labelNames(labels, caseId)),
      checkId,
      ...groups.map((group) => {
        const item = findByCaseAndId(group.checks, caseId, checkId, "check_id");
        if (!item) {
          return "not measured";
        }
        return [
          `pass ${formatCount(item.passes)}`,
          `fail ${formatCount(item.fails)}`,
          `n/a ${formatCount(item.not_applicable)}`,
          `error ${formatCount(item.errors)}`,
          `agreement ${item.agreement === null ? UNKNOWN : formatRate(item.agreement)}`,
        ].join(" · ");
      }),
    ]),
  );
}

function metricsResultsTable(groups, labels) {
  const keys = unionCaseKeys(groups, "metrics", "metric_id");
  if (keys.length === 0) {
    return note("No case metrics are retained for any variant.");
  }
  return table(
    "Per-metric value distribution by variant, among eligible attempts.",
    ["Case", "Metric", "Unit", ...groups.map(groupLabel)],
    keys.map(([caseId, metricId]) => {
      const sample = groups
        .map((group) => findByCaseAndId(group.metrics, caseId, metricId, "metric_id"))
        .find(Boolean);
      return [
        caseLink(caseId, labelNames(labels, caseId)),
        sample ? sample.label : metricId,
        sample?.unit ?? UNKNOWN,
        ...groups.map((group) => {
          const item = findByCaseAndId(group.metrics, caseId, metricId, "metric_id");
          return item ? formatDistribution(item.values) : "not measured";
        }),
      ];
    }),
  );
}

function toplineCell(text, className = "") {
  return el("td", { className, text });
}

function turnsCell(turns) {
  return toplineCell(formatTurns(turns), "number");
}

function resourceCells(item) {
  return [formatDuration(item.seconds), formatTokens(item.tokens), formatCost(item.cost, item.currency)].map(
    (text) => toplineCell(text, "number"),
  );
}

function groupCheckCell(topline, checkId) {
  const tally = (topline.checks ?? []).find((item) => item.check_id === checkId);
  if (!tally || tally.answered === 0) {
    return toplineCell("–");
  }
  const tone = tally.passes === tally.answered ? "pass" : tally.passes === 0 ? "fail" : "";
  return toplineCell(`${tally.passes}/${tally.answered}`, `check ${tone}`.trim());
}

function attemptCheckCell(checks, checkId) {
  const outcome = (checks ?? []).find((item) => item.check_id === checkId)?.outcome;
  return outcome ? toplineCell(CHECK_MARKS[outcome] ?? "?", `check ${outcome}`) : toplineCell("");
}

function firstInspectionCell(value) {
  if (!value || value === "none") {
    return toplineCell("–");
  }
  return toplineCell(INSPECTION_MARKS[value] ?? "?", `check ${INSPECTION_CLASSES[value] ?? value}`);
}

function fixRequestsCell(value) {
  return value === null || value === undefined ? toplineCell("–") : toplineCell(formatCount(value), "number");
}

function workflowCell(value) {
  return value === null || value === undefined ? toplineCell("–") : toplineCell(value);
}

function groupFirstInspectionCell(passes, attempts) {
  if (!attempts) {
    return toplineCell("–");
  }
  const tone = passes === attempts ? "pass" : passes === 0 ? "fail" : "";
  return toplineCell(`${passes}/${attempts}`, `check ${tone}`.trim());
}

function groupFixRequestsCell(value) {
  return value === null || value === undefined ? toplineCell("–") : toplineCell(formatNumber(value), "number");
}

function groupWorkflowCell(completed, attempts) {
  if (!attempts) {
    return toplineCell("–");
  }
  const tone = completed === attempts ? "pass" : completed === 0 ? "fail" : "";
  return toplineCell(`${completed}/${attempts}`, `check ${tone}`.trim());
}

function toplineTable(report) {
  const groups = (report.groups ?? []).filter((group) => group.topline);
  if (groups.length === 0) {
    return null;
  }
  const checkIds = [
    ...new Set(groups.flatMap((group) => (group.topline.checks ?? []).map((item) => item.check_id))),
  ];
  const head = el(
    "tr",
    {},
    [
      "Variant / attempt",
      "Turns",
      "First check",
      "Fix requests",
      "Workflow",
      "Time",
      "Tokens",
      "Cost",
      ...checkIds.map(checkLabel),
    ].map((text) => el("th", { text, attrs: { scope: "col" } })),
  );
  const rows = groups.flatMap((group) => {
    const variant = group.variant || NO_VARIANT;
    const members = (report.attempts ?? []).filter(
      (attempt) => attempt.topline?.latest && (attempt.variant || NO_VARIANT) === variant,
    );
    return [
      el("tr", { className: "variant" }, [
        toplineCell(`${variant} · mean of ${group.topline.attempts}`),
        turnsCell(group.topline.turns),
        groupFirstInspectionCell(group.topline.first_inspection_passes, group.topline.first_inspection_attempts ?? 0),
        groupFixRequestsCell(group.topline.fix_requests),
        groupWorkflowCell(group.topline.workflows_completed, group.topline.workflow_attempts ?? 0),
        ...resourceCells(group.topline),
        ...checkIds.map((checkId) => groupCheckCell(group.topline, checkId)),
      ]),
      ...members.map((attempt) =>
        el("tr", {}, [
          el("td", { className: "attempt", text: shortId(attempt.attempt_id, 10), attrs: { title: attempt.attempt_id } }),
          turnsCell(attempt.topline.turns),
          firstInspectionCell(attempt.topline.first_inspection),
          fixRequestsCell(attempt.topline.fix_requests),
          workflowCell(attempt.topline.workflow),
          ...resourceCells(attempt.topline),
          ...checkIds.map((checkId) => attemptCheckCell(attempt.topline.checks, checkId)),
        ]),
      ),
    ];
  });
  return el("div", { className: "scroller" }, [
    el("table", { className: "topline" }, [
      el("caption", { text: "Per variant and attempt, agent only." }),
      el("thead", {}, [head]),
      el("tbody", {}, rows),
    ]),
  ]);
}

export function resultsView(report, labels) {
  const groups = report.groups ?? [];
  if (groups.length === 0) {
    return [stateCard("empty", "This report has no variant groups to compare.")];
  }
  const topline = toplineTable(report);
  return [
    topline ? section("Summary", [topline, note(TOPLINE_LEGEND)]) : null,
    section("Results", [
      resultsSummaryTable(groups),
    ]),
    section("Help needed", [helpNeededTable(groups)]),
    section("Criteria", [criteriaResultsTable(groups, labels)]),
    section("Checks", [checksResultsTable(groups, labels)]),
    section("Metrics", [metricsResultsTable(groups, labels)]),
  ];
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
      attempt.eligibility ?? UNKNOWN,
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

function criteriaCard(result, reasoning) {
  if (!result || result.criteria.length === 0) {
    return section("Criteria", [note("No criterion results are retained.")]);
  }
  const rationale = reasoningMap(reasoning?.criteria, "criterion_id");
  return section("Criteria", [
    table(
      "Criterion outcomes with their calibration evidence and the judge's reasoning.",
      ["Criterion", "Kind", "Required", "Outcome", "Score", "Calibration", "Error", "Rationale"],
      result.criteria.map((criterion) => [
        criterion.criterion_id,
        criterion.kind,
        criterion.required ? "required" : "optional",
        criterion.outcome,
        criterion.score === null ? UNKNOWN : formatNumber(criterion.score, 3),
        describeCalibration(criterion.calibration),
        criterion.error ?? "none",
        rationale.get(criterion.criterion_id)
          ?? (criterion.kind === "deterministic" ? "not judged (deterministic)" : "reasoning not included"),
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

function checksCard(result, reasoning) {
  const checks = result?.checks ?? [];
  if (checks.length === 0) {
    return section("Checks", [note("No case checks are retained for this attempt.")]);
  }
  const rationale = reasoningMap(reasoning?.checks, "check_id");
  return section("Checks", [
    table(
      "Case-declared check outcomes with the judge's reasoning.",
      ["Check", "Outcome", "Answer", "Rationale"],
      checks.map((check) => [
        check.check_id,
        check.outcome,
        check.answer ?? UNKNOWN,
        rationale.get(check.check_id) ?? "reasoning not included",
      ]),
    ),
  ]);
}

function metricsCard(result) {
  const metrics = result?.metrics ?? [];
  if (metrics.length === 0) {
    return section("Metrics", [note("No case metrics are retained for this attempt.")]);
  }
  return section(
    "Metrics",
    metrics.flatMap((item) => [
      heading(4, item.metrics_id),
      note(`outcome ${item.outcome}${item.error ? ` · ${item.error}` : ""}`),
      item.values.length === 0
        ? note("No metric values recorded.")
        : table(
            `Metric values for ${item.metrics_id}.`,
            ["Metric", "Label", "Value", "Unit", "Better"],
            item.values.map((value) => [
              value.metric_id,
              value.label,
              formatMetricValue(value.value),
              value.unit ?? UNKNOWN,
              value.better ?? UNKNOWN,
            ]),
          ),
    ]),
  );
}

function helpNeededCard(result) {
  const help = result?.help_needed;
  if (!help) {
    return section("Help needed", [note("No help-needed accounting is retained for this attempt.")]);
  }
  const list = (values) => (values.length === 0 ? "none" : values.join(", "));
  return section("Help needed", [
    definitions([
      ["Simulator replies", formatCount(help.simulator_replies)],
      ["Facts disclosed", list(help.facts_disclosed)],
      ["Disclosed proactively", list(help.proactive_facts)],
      ["Disclosed on request", list(help.on_request_facts)],
    ]),
  ]);
}

function workflowCard(result) {
  const workflow = result?.workflow;
  if (!workflow) {
    return section("Workflow", [note("No workflow is retained for this attempt.")]);
  }
  const events =
    workflow.events.length === 0
      ? note("No workflow events are retained for this attempt.")
      : table(
          "Workflow events in order.",
          ["Step", "Kind", "Outcome", "Turn", "Failed criteria"],
          workflow.events.map((event) => [
            event.step_id,
            event.kind,
            event.outcome,
            formatCount(event.turn),
            event.failed_criteria.length === 0 ? "none" : event.failed_criteria.join(", "),
          ]),
        );
  return section("Workflow", [
    definitions([
      ["Declared steps", formatCount(workflow.steps)],
      ["Completed steps", formatCount(workflow.completed_steps)],
      ["Inspections", formatCount(workflow.inspections)],
      ["Fix requests", formatCount(workflow.fix_requests)],
      ["Dialogs answered", formatCount(workflow.dialogs_answered)],
      ["First inspection", workflow.first_inspection],
      ["Final inspection", workflow.final_inspection],
      ["Inspection time", formatSeconds(workflow.inspection_seconds)],
    ]),
    events,
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
              "Cost (API-equivalent estimate)",
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
  const gradedCaseId = result?.graded_case_id ?? result?.case_id ?? null;
  return [
    section(`Attempt ${attempt.attempt_id}`, [
      el("p", { className: "chips" }, [
        chip("trial", attempt.trial_id),
        chip("variant", attempt.variant ?? NO_VARIANT),
        chip("capture", attempt.capture_id),
        chip("quarantine", attempt.quarantine_id),
        chip("assessment", attempt.assessment_id),
        chip("pairing", attempt.pairing_id),
        gradedCaseId
          ? caseLink(gradedCaseId, labelNames(labels, gradedCaseId))
          : chip("case", null),
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
        [
          "Graded case",
          result?.graded_case_id ? result.graded_case_id : "same as captured case (not regraded)",
        ],
        ["Rubric set", result?.rubric_set ?? UNKNOWN],
      ]),
      result && result.limitations.length > 0
        ? el("div", {}, [heading(4, "Assessment limitations"), bullets(result.limitations)])
        : null,
    ]),
    auditCard(result),
    criteriaCard(result, details.reasoning),
    checksCard(result, details.reasoning),
    metricsCard(result),
    helpNeededCard(result),
    workflowCard(result),
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

export function caseDetail(view, labels) {
  if (!view) {
    return [stateCard("empty", "Enter a case ID, or open a case chip, to look it up.")];
  }
  const chip = (label, value) => idChip(label, value, labelNames(labels, value));
  return [
    section(`Case ${view.title}`, [
      el("p", { className: "chips" }, [
        chip("case", view.case_id),
        chip("repository", view.repository_id),
        chip("persona", view.persona_id),
      ]),
      definitions([
        ["Baseline commit", view.baseline_commit ?? UNKNOWN],
        ["Reference patch", view.reference_patch ? "present" : "none"],
        ["Max turns", formatCount(view.budgets.max_turns)],
        ["Subject seconds", formatSeconds(view.budgets.subject_seconds)],
        ["Setup seconds", formatSeconds(view.budgets.setup_seconds)],
        [
          "Tool requirements",
          view.tool_requirements.length === 0 ? "none" : view.tool_requirements.join(", "),
        ],
      ]),
      heading(4, "Initial prompt"),
      block(view.initial_prompt),
    ]),
    section(
      "Facts",
      view.facts.length === 0
        ? [note("No allowed facts are recorded for this case.")]
        : [
            table(
              "Facts the subject may be given, and how they may be disclosed.",
              ["Fact", "Disclosure", "Text"],
              view.facts.map((fact) => [fact.fact_id, fact.disclosure, fact.text]),
            ),
          ],
    ),
    section("Rubrics", [
      view.disabled_rubrics.length === 0
        ? note("No default rubrics are disabled for this case.")
        : note(`Disabled default rubrics: ${view.disabled_rubrics.join(", ")}.`),
      view.rubrics.length === 0
        ? note("No rubric criteria are retained for this case.")
        : table(
            "Effective judge rubrics, marking case-authored, default and overridden criteria.",
            ["Criterion", "Origin", "Required", "Rubric"],
            view.rubrics.map((item) => [
              item.criterion_id,
              item.origin,
              item.required ? "required" : "optional",
              item.rubric,
            ]),
          ),
    ]),
    section(
      "Deterministic criteria",
      view.deterministic.length === 0
        ? [note("No deterministic (hidden-verifier) criteria are retained for this case.")]
        : [
            table(
              "Hidden-verifier criteria, by description, required flag and command argv; "
                + "the hidden entrypoint's content is never shown.",
              ["Criterion", "Required", "Command", "Description"],
              view.deterministic.map((item) => [
                item.criterion_id,
                item.required ? "required" : "optional",
                item.command.argv.join(" "),
                item.description,
              ]),
            ),
          ],
    ),
    section(
      "Checks",
      view.checks.length === 0
        ? [note("No case-declared checks are retained.")]
        : [
            table(
              "Case-declared yes/no checks the judge answers from the transcript and files.",
              ["Check", "Expect", "Only if", "Question"],
              view.checks.map((item) => [
                item.check_id,
                item.expect,
                item.only_if ? `${item.only_if.check_id} = ${item.only_if.answer}` : "none",
                item.question,
              ]),
            ),
          ],
    ),
    section(
      "Metrics commands",
      view.metrics.length === 0
        ? [note("No case metrics commands are retained.")]
        : [
            table(
              "Case-supplied metrics commands run against the final workspace.",
              ["Metrics", "Description", "Command"],
              view.metrics.map((item) => [
                item.metrics_id,
                item.description,
                item.command.argv.join(" "),
              ]),
            ),
          ],
    ),
  ];
}

export function targetLine(session, labels) {
  if (!session || !session.target) {
    return "No focused target; showing every retained run in this store.";
  }
  const target = session.target;
  return `Focused ${target.kind}: ${labelledId(target.id, labelNames(labels, target.id))}`;
}
