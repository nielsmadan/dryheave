import { errorMessage } from "./format.mjs";

export class ApiError extends Error {
  constructor(message, { status = null, code = "network_error" } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

export function buildQuery(params) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === null || value === undefined || value === "") {
      continue;
    }
    search.set(key, String(value));
  }
  const query = search.toString();
  return query === "" ? "" : `?${query}`;
}

export function attemptPath(source, attemptId, detail) {
  const root = source.kind === "run" ? "runs" : "reports";
  const name = encodeURIComponent(attemptId);
  return `/api/${root}/${source.id}/attempts/${name}/${detail}`;
}

export function isReportId(value) {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
}

export function sourcePath(source) {
  return source.kind === "run" ? `/api/runs/${source.id}` : `/api/reports/${source.id}`;
}

export async function fetchJson(path, options = {}) {
  const { fetchImpl = globalThis.fetch, signal = undefined } = options;
  let response;
  try {
    response = await fetchImpl(path, { headers: { Accept: "application/json" }, signal });
  } catch (cause) {
    if (cause && cause.name === "AbortError") {
      throw cause;
    }
    throw new ApiError("The viewer could not be reached; the server may have stopped.", {
      code: "network_error",
    });
  }
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const code = payload?.error?.code;
    throw new ApiError(errorMessage(response.status, payload), {
      status: response.status,
      code: typeof code === "string" ? code : "http_error",
    });
  }
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    throw new ApiError("The viewer returned a body that is not a JSON object.", {
      status: response.status,
      code: "invalid_response",
    });
  }
  return payload;
}
