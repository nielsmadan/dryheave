import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ApiError,
  attemptPath,
  buildQuery,
  fetchJson,
  isReportId,
  sourcePath,
} from "../../src/dryheave/resources/viewer/api.mjs";

function stub(status, body, { json = true } = {}) {
  return async () => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => {
      if (!json) {
        throw new SyntaxError("Unexpected token");
      }
      return body;
    },
  });
}

test("buildQuery omits empty values and encodes the rest", () => {
  assert.equal(buildQuery({ offset: 0, limit: 25 }), "?offset=0&limit=25");
  assert.equal(buildQuery({ before: "a", after: "", variant: null }), "?before=a");
  assert.equal(buildQuery({}), "");
  assert.equal(buildQuery(null), "");
  assert.equal(buildQuery({ before: "a b&c" }), "?before=a+b%26c");
});

test("sourcePath routes runs and reports separately", () => {
  assert.equal(sourcePath({ kind: "run", id: "a".repeat(32) }), `/api/runs/${"a".repeat(32)}`);
  assert.equal(
    sourcePath({ kind: "report", id: "b".repeat(64) }),
    `/api/reports/${"b".repeat(64)}`,
  );
});

test("attemptPath encodes the attempt id for runs and reports", () => {
  assert.equal(
    attemptPath({ kind: "run", id: "a".repeat(32) }, "trial.1", "dialogue"),
    `/api/runs/${"a".repeat(32)}/attempts/trial.1/dialogue`,
  );
  assert.equal(
    attemptPath({ kind: "report", id: "b".repeat(64) }, "a/b", "patch"),
    `/api/reports/${"b".repeat(64)}/attempts/a%2Fb/patch`,
  );
});

test("fetchJson returns a decoded object", async () => {
  const payload = await fetchJson("/api/runs", { fetchImpl: stub(200, { total: 0, runs: [] }) });
  assert.deepEqual(payload, { total: 0, runs: [] });
});

test("fetchJson raises the typed error envelope on a non-OK status", async () => {
  const failure = await fetchJson("/api/compare", {
    fetchImpl: stub(400, {
      error: { code: "invalid_input", message: "Comparison requires before and after references." },
    }),
  }).catch((error) => error);
  assert.ok(failure instanceof ApiError);
  assert.equal(failure.status, 400);
  assert.equal(failure.code, "invalid_input");
  assert.equal(
    failure.message,
    "Comparison requires before and after references. (invalid_input)",
  );
});

test("fetchJson raises a readable error when a failure body is not an envelope", async () => {
  const failure = await fetchJson("/api/runs", {
    fetchImpl: stub(503, null, { json: false }),
  }).catch((error) => error);
  assert.ok(failure instanceof ApiError);
  assert.equal(failure.code, "http_error");
  assert.equal(failure.message, "The viewer returned HTTP 503 without a readable error body.");
});

test("fetchJson raises a network error when fetch rejects", async () => {
  const failure = await fetchJson("/api/runs", {
    fetchImpl: async () => {
      throw new TypeError("Failed to fetch");
    },
  }).catch((error) => error);
  assert.ok(failure instanceof ApiError);
  assert.equal(failure.status, null);
  assert.equal(failure.code, "network_error");
  assert.equal(
    failure.message,
    "The viewer could not be reached; the server may have stopped.",
  );
});

test("fetchJson rethrows an abort instead of reporting a network failure", async () => {
  const aborted = new Error("aborted");
  aborted.name = "AbortError";
  const failure = await fetchJson("/api/runs", {
    fetchImpl: async () => {
      throw aborted;
    },
  }).catch((error) => error);
  assert.equal(failure, aborted);
});

test("fetchJson rejects a successful body that is not a JSON object", async () => {
  for (const body of [null, [1, 2]]) {
    const failure = await fetchJson("/api/runs", { fetchImpl: stub(200, body) }).catch(
      (error) => error,
    );
    assert.ok(failure instanceof ApiError);
    assert.equal(failure.code, "invalid_response");
    assert.equal(failure.message, "The viewer returned a body that is not a JSON object.");
  }
});

test("isReportId accepts only a lowercase 64-hex report object ID", () => {
  assert.equal(isReportId("a".repeat(64)), true);
  assert.equal(isReportId("A".repeat(64)), false);
  assert.equal(isReportId("a".repeat(63)), false);
  assert.equal(isReportId("a".repeat(65)), false);
  assert.equal(isReportId("a".repeat(32)), false);
  assert.equal(isReportId("nightly"), false);
  assert.equal(isReportId(""), false);
  assert.equal(isReportId(null), false);
  assert.equal(sourcePath({ kind: "report", id: "a".repeat(64) }), `/api/reports/${"a".repeat(64)}`);
});
