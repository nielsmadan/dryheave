# Strict Pydantic boundaries

Status: accepted. Date: 2026-09-09.

Use Pydantic 2 as the only initial runtime dependency. Strict, versioned envelopes
and domain models reject coercion, unknown fields and unsupported schema versions.
Domain services validate concrete payloads through the store's typed load API.

Pydantic supplies reusable validation and schema generation at CLI, object and
journal boundaries. Its compiled pydantic-core dependency increases wheel and
platform packaging requirements compared with a standard-library-only package.
The lockfile pins the resolved dependency tree; CI builds the distributable wheel.
