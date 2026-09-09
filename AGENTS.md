# AGENTS.md

Dryheave is a Python CLI for interactive coding-agent benchmarks. Read README.md
for the shipped CLI and docs/tech/storage.md for persistence contracts.

## Commands

Use `just setup` for dependencies and checkout-local Lefthook hooks. Before
declaring work complete, run `just check`. Run `just coverage` for the separate
80% branch coverage gate and `uv build` after packaging changes. CI uses the same
lint, formatting, cyclic-import, strict type and test checks. Tests align with
source modules under tests/ and share fixtures in conftest.py.

## Structure

Use Python >=3.13, uv/Hatchling, src/dryheave and argparse. Internal modules never
import the package root. Keep shared errors, models and filesystem operations
below services in the dependency graph. CLI registration belongs outside domain
services. Ruff owns formatting and lint; Pylint checks only cyclic imports.

Pydantic 2 validates persisted and external boundaries. Extend StrictModel for
domain payloads and validate them when loading from the immutable store. Keep
unknown fields and schema versions as errors. See docs/decisions/0001-validation.md.

## Constraints

Source logs and repositories are read-only. Workspaces isolate repository objects,
not native host access. Credentials remain runtime references. Never inspect
auth files or environment inventories. Commands use argv with finite limits.

Preserve immutable object IDs and evidence. Alias updates cannot alter historical
inputs. Native execution needs both a run lock and the store native lock. Stop
owned writers before snapshotting outputs or grading. Missing observations remain
unknown; no fake successful commands or placeholder CLI actions.

Keep development stores, fixtures, downloads, virtual environments and caches in
ignored paths in this checkout. New code defaults to no comments/docstrings.
Add meaningful behavior and failure tests in the matching module's test file.
Do not disable checks broadly to avoid fixing failures.
