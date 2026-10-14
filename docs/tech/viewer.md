# Read-only local viewer

`dryheave view [RUN_OR_REPORT]` serves a read-only results website. It binds
loopback on an OS-selected port, prints the actual URL to stderr before any
optional browser opening, and serves in the foreground until Ctrl-C. `--open`
launches a browser at that URL; without it nothing is opened. An optional run ID,
portable report ID or alias focuses the session and is exposed at `/api/target`;
a 32-hex reference prefers a real run journal and otherwise falls back to alias
and object resolution, while any other reference must resolve to a portable
report. Viewing never runs, assesses, grades or calls a model.

```sh
uv run dryheave --store .dryheave view --json
uv run dryheave --store .dryheave view RUN_ID --open --json
```

## Routes and boundary

Routes are fixed: the packaged `/`, `/viewer.css`, `/viewer.js` and the
`/api.mjs`, `/dom.mjs`, `/filters.mjs`, `/format.mjs` and `/views.mjs` ES modules, plus
`/api/target`, `/api/labels`, `/api/runs`, `/api/runs/<run-id>`,
`/api/runs/<run-id>/progress`, `/api/runs/<run-id>/calibrations`,
`/api/cases/<case-id>/calibrations`, `/api/cases/<case-id>`,
`/api/reports/<object-id>`, `/api/compare` and the per-attempt
`.../attempts/<attempt-id>/{dialogue,patch,evidence,reasoning}` and
`.../evidence/<file>` details. Attempt details accept either a run or a portable
report as their source, so an imported report with omitted captures stays a valid
degraded view; case-scoped calibrations exist because a portable report owns case
identities but no run journal. `/api/cases/<case-id>` returns the frozen case
rendered as data (title, initial prompt, facts, effective rubrics marking
default/overridden criteria, deterministic (hidden-verifier) criteria by
description, required flag and command argv, checks, metrics commands, budgets,
tool requirements, repository/persona identity and baseline commit, and whether
a reference patch exists) and 404s for an id the store does not retain; it
loads only the case's own metadata and its repository's commit identity, never
the hidden verifier files or the reference patch bytes, and bounds what it
reads the same way attempt detail bounds blob reads. `.../attempts/<attempt-id>/reasoning` serves the judge's
per-criterion and per-check rationale for the attempt's CURRENT assessment,
located from the judge call recorded on that assessment's own role accounting
(never a superseded or regraded-over judge call); it reports `unavailable` when
the attempt was never judged, when no judge response was retained, or for a
portable report without that evidence. `/api/labels` reverses the store's alias
index into `{object-id: [alias, ...]}` so the page can show a human label beside
an immutable ID; it reports only aliases the store records and never invents
one. There are no write, run or grade routes, no CORS, no LAN binding, no
terminal replay and no arbitrary filesystem paths; asset paths are matched
exactly and are never joined with a directory.

Only `GET` is served. `HEAD` and every other known method return 405 with `Allow:
GET` and no body on `HEAD`; an unknown method is validated before it is refused.
A request version other than HTTP/1.0 or HTTP/1.1 is refused with 400 before any
header is read, so no response can be emitted without a status line. `Host` must
be exactly the viewer authority — one header, matching host and port — and a
supplied `Origin` must be exactly the viewer origin. Responses carry a restrictive
`Content-Security-Policy` with `frame-ancestors 'none'`, `X-Content-Type-Options:
nosniff`, `Cache-Control: no-store`, `Referrer-Policy: no-referrer`,
`Cross-Origin-Resource-Policy: same-origin`, a fixed MIME type and the exact byte
length. The 503 refusal below carries the same headers. Local user processes that
can already read the store are outside this boundary; the checks stop a browser
page, not the host.

## Bounds

The accept loop never blocks: a connection over the open-connection ceiling is
refused immediately, its unread bytes are drained under a total deadline rather
than a per-read timeout, and the concurrency slot is acquired on the handler
thread, so neither a trickling client nor a full slot table delays new
connections or shutdown. A request that waits out its slot timeout receives the
same 503. Accepted sockets carry a read timeout. `serve()` and `stop()` close the
listening socket and then join in-flight handler threads; both raise `conflict`
instead of reporting success when a handler outlives its join timeout.

Every route bounds the journal bytes it reads. Listings read a bounded prefix of
each run journal under a per-request budget and never build execution state: a row
that did not reach the end of its journal reports `truncated: true`, leaves
`durable_sequence` null and treats `observed_events` as a lower bound. Such a row
takes its attempt stages from the run's `checkpoint.json` (see storage.md), overlays
any later `attempt-state` events from the prefix, and reports `counted_through`,
the event sequence its `attempts` and `finished` counts are exact through. Without a
checkpoint those counts are lower bounds and `counted_through` is null. The budget is charged with the bytes the prefix read, not with the
smaller remainder left after trimming back to the last complete line, so a row
whose first event overruns its allowance still spends what it used and says that
no complete event fit rather than reading as an empty run. A per-row failure
degrades that row only and never blanks the collection. Object corruption or
deletion changes validity without a journal sequence change, so nothing is cached
on sequence alone. Progress, report, comparison and attempt detail routes need
whole execution state, so they read each journal under the same per-request
budget and answer `limit_exceeded` above it instead of parsing up to the 128 MiB
storage ceiling. The budget is 64 MiB per journal: a workflow run of four
attempts already journals about 8.6 MB, mostly repeated attempt-state snapshots,
which the earlier 8 MiB budget refused. Parsing that run takes about 0.6 s, so
`ViewerService` keeps the last four parsed journals keyed by run ID and the events
file's inode, size and modification time; any append changes the key and forces a
fresh parse. Only the parsed journal is reused: capture, assessment and blob
verification still run on every request. The run listing keeps its own 8 MiB
aggregate budget (`MAX_LIST_JOURNAL_BYTES`).

Retained dialogue, final patch and verifier evidence load on demand. Blob reads
apply the viewer's per-response limit before allocating, so a blob within the
256 MiB storage ceiling but over the serve limit is `refused` with
`reason_code: "serve_limit"`, not truncated silently. Detail status is one of
`ok`, `unavailable`, `invalid`, `omitted` or `refused`; a missing patch with
recorded capture omissions is `omitted` and carries those omissions, and capture
interaction coverage, capture errors and evidence omissions are surfaced rather
than inferred. Content that is not valid UTF-8 is returned with
`encoding: "base64"` and its exact bytes instead of a lossy decode. API responses
are serialised against the response ceiling as they are encoded and refused with
`response_too_large` before a whole oversized body exists.

Calibration lookup scans the object store by manifest kind and exact case
reference, never by a payload string. A scan is bounded per request and paginated
by `?limit=` and `?after=<object-id>`, reporting `scanned`, `unreadable`,
`attempted`, `verified`, `complete` and `next_cursor`. Each rendered row is
verified through the calibration loader within a per-request verification budget
and states `verified`, `failed` or `unverified`. `attempted` counts the rows the
budget allowed the loader to check and `verified` counts only the rows that
passed, so a store of corrupt calibrations reports attempts without claiming
verifications. `unreadable` counts every object in the scan window whose envelope
failed to read, of any kind, not only calibrations, and the rendered sentence says
so. The loader reads blobs under the viewer's per-response read limit as well,
shared across the rows of one request, so retained evidence above it leaves the
row `unverified` with `reason_code: "serve_limit"` instead of allocating whole
blobs up to the 256 MiB storage ceiling.

## Errors and disclosure

Every response model extends `StrictModel`; unknown fields and schema versions are
errors. Error bodies are `{"error": {"code": ..., "message": ...}}` with a fixed
sentence per error code and a machine-readable `reason_code`; identifiers stay in
typed `capture_id`, `quarantine_id`, `evidence_id`, `assessment_id` and
`calibration_id` fields. Store paths and raw exception text are never disclosed,
and absolute-path tokens raised below the viewer are scrubbed to `<path>`.
Transcripts, patches and evidence are served as JSON text and are never
interpreted as HTML or links.

## Website

The packaged page is plain HTML, CSS and ES modules; there is no runtime Node
dependency, framework, bundler or CDN, and the served `Content-Security-Policy`
allows only same-origin scripts and styles. Every asset is allowlisted by exact
path in `viewer_http.VIEWER_ASSETS` with a fixed MIME type, so a new module is
unreachable until it is listed there. One test pins the packaged directory against
that allowlist, another pins the wheel build configuration against the packaged
tree, and a third — held behind the `packaging` marker, which the default pytest
run deselects — asserts byte-for-byte that a wheel already built by `just build`
carries every asset. `just check` and CI build first and then run that marker, so
the guarantee holds without an ordinary test run invoking a build. Test-only
helpers live under `tests/js` and are neither packaged nor allowlisted.

A run list with a text filter, offset paging and a portable-report lookup drives
eight sections: overview (report identities, variant groups, per-group
distributions, aggregate role usage and cost), results (a per-variant quality
and cost comparison — cost per role and total with its price basis, token and
turn and duration distributions, help needed, and per-criterion pass rate and
mean score, per-check pass/fail/not-applicable with agreement, and per-metric
distribution, each union-ed across variants and keyed by case so a variant
missing a measure another has renders `not measured` rather than a blank cell;
a case ID in any of those rows is a clickable chip that opens the case
section), attempts (search plus variant, stage and eligibility facets), attempt
detail (identities including the assessment's `graded_case_id` and
`rubric_set`, audit, criteria with their calibration and the judge's
rationale, case-declared checks with their rationale, case metrics, help
needed, a Workflow card (declared/completed steps, inspection, fix-request
and dialog counts, first/final inspection verdict, inspection time and the
workflow's events in order — step, kind, outcome, turn, failed criteria — or
a plain note when the attempt carries no workflow), per-role usage records,
dialogue, final patch and verifier evidence —
the judge's rationale is read from a dedicated bounded route and renders
`reasoning not included` per judged row when the current assessment carries no
judge response for that criterion or check, and `not judged (deterministic)`
for a deterministic criterion, which is never judged; a case chip for a
regraded attempt opens the case that was actually graded (`graded_case_id`
when the attempt was regraded, otherwise `case_id`)), case (the frozen case
rendered as data: title, initial prompt, facts and their disclosure kind,
effective rubrics marking case-authored/default/overridden origin and any
disabled defaults, deterministic (hidden-verifier) criteria by description,
required flag and command argv — never the hidden entrypoint's content, checks
with their expectation and `only_if` condition, metrics commands with their
description and command, budgets, tool requirements, repository/persona
identity and baseline commit, and whether a reference patch exists; reachable
by a case chip anywhere in the page or by typing a case ID directly), progress,
calibration (run-scoped, with a standalone case lookup and a control that
continues a truncated scan from its cursor) and comparison. Finished, scored and
eligible attempts stay separate, and an assessed attempt that is neither eligible
nor excluded is reported as `ungraded` rather than excluded; unknown is never
rendered as zero; reasoning is labelled as a subset of total output; group token
and duration distributions are labelled as subject-scoped because the backend
computes them from the subject role alone, while per-attempt cost and spend say
that they cover every role and every retained attempt, and pairs state that they
are latest-compatible. The Results tab opens with the agent-only Summary table
described under HTML report in results.md, rendered from the report's `topline`
fields, including the First check, Fix requests and Workflow columns and their
variant-row ratios and means; `views.mjs` and `report_html.py` render the same
cells from the same derived fields. The Results summary table labels every row with the
population it covers — all retained attempts (retries included) or eligible
attempts only — rather than leaving one caption to describe rows with different
populations. Client-side role spend applies the same zero-cost,
no-currency guard as the backend. Currency, price-table version and price date
are shown as recorded, and fixture coverage keeps its label. Every cost figure
anywhere in the page — overview, results and per-attempt role usage — is an
API-equivalent estimate from a dated price table, never a provider invoice,
and the page says so beside the figure rather than leaving that to be assumed.
Where the store records an alias, identity chips, run rows, the focused-target
line and calibration rows show it beside the full immutable ID, which stays
visible and authoritative.

Loading, empty, error, corrupt and omitted states each render explicitly. Every
request handles a non-OK status and a network failure and shows the typed error
envelope rather than stalling. A failed request keeps the last good data on screen
and reports the failure beside it instead of blanking the panel, and the retry
clears that error before it resolves rather than showing a stale one. Each panel
carries at most one in-flight request: starting another aborts the first, and a
response that arrives anyway for a superseded selection is discarded, so a slow
answer cannot overwrite a newer one.

Every node the page builds comes from `document.createElement`, and every value
taken from a response is written with `textContent`; no attribute, URL or event
handler is built from response data. Node tests drive the real view builders and
the live page with hostile payloads — `<script>`, `<img src=x onerror=…>`,
`javascript:` and `data:text/html` URLs, a lone surrogate and a field whose value
is `innerHTML` — and assert that the rendered tree has exactly the same element
shape as the same fixtures filled with benign values, that every hostile byte
lands in a text node, that only inert tags are created, and that no `on*`, `href`,
`src` or `style` attribute and no navigable URL appears anywhere in the result.
Those behavioural tests are the evidence. A source lint in
`tests/test_viewer_http.py` additionally rejects the HTML-injection sinks
(`innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`,
`srcdoc`, `Function`, `createContextualFragment`), `setAttribute` with an `on*`
name, assignment to `href` or `src`, URL-bearing attribute keys and
`javascript:`/`data:text/html` literals — over the sources and over a copy with
string concatenation collapsed, so a split spelling cannot hide one. It is a cheap
guard against a regression, not a proof of the property.

Polling refreshes the listing and progress every five seconds and pauses while the
tab is hidden or the refresh checkbox is cleared. A refresh repaints a panel only
when the data behind it changed, so an unchanged poll leaves keyboard focus, text
selection and the scroll position of a long transcript exactly where the reader
left them. Tabs support arrow, Home and End keys,
the run list supports arrow keys, and the layout collapses to one column on
narrow screens.

Pure formatting, filtering and selection live in `format.mjs`, `filters.mjs` and
`api.mjs`, separate from the DOM wiring in `viewer.js`, `dom.mjs` and `views.mjs`.
`node --test tests/js/*.test.mjs` exercises all six through Node's built-in test
runner. `tests/js/dom-shim.mjs` is a minimal element, event and fetch harness: it
parses the packaged `index.html`, so `tests/js/viewer.test.mjs` imports the real
`viewer.js` and drives the page the way a reader does — tab and run-list keys,
filters, attempt and evidence selection, paging, forms, the poll and its pause
controls — while `tests/js/dom.test.mjs` and `tests/js/views.test.mjs` assert the
nodes, attributes and text the builders produce. Node is a development tool only;
`just test-js` and `just check` run these, as does CI, and no npm dependency or
`package.json` is involved.
