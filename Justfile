export UV_CACHE_DIR := justfile_directory() / ".cache/uv"
export TMPDIR := justfile_directory() / ".cache/tmp"

[private]
default:
    @just --list

setup:
    @mkdir -p .cache/tmp
    @uv sync
    @lefthook install
    @just doctor

doctor:
    #!/usr/bin/env bash
    set -uo pipefail
    fail=0
    for tool in uv lefthook node; do
        if command -v "$tool" >/dev/null 2>&1; then
            printf '  ok       %s\n' "$tool"
        else
            printf '  MISSING  %s\n' "$tool"; fail=1
        fi
    done
    for hook in pre-commit pre-push; do
        if [ -f "$(git rev-parse --git-path "hooks/$hook")" ]; then
            printf '  ok       %s hook\n' "$hook"
        else
            printf '  MISSING  %s hook; run just setup\n' "$hook"; fail=1
        fi
    done
    [ "$fail" -eq 0 ] && printf 'Everything in place.\n'
    exit "$fail"

test:
    @uv run pytest tests/ -q

test-verbose:
    @uv run pytest tests/ -v

test-js:
    @node --test tests/js/*.test.mjs

coverage:
    @uv run pytest --cov --cov-report=term-missing --cov-report=html -q

lint:
    @uv run ruff check

lint-imports:
    @uv run pylint src/dryheave

format:
    @uv run ruff format

typecheck:
    @uv run mypy

check:
    @uv run ruff check
    @uv run ruff format --check
    @uv run pylint src/dryheave
    @uv run mypy
    @node --test tests/js/*.test.mjs
    @uv run pytest -q
    @just test-packaging

build:
    @uv build

test-packaging: build
    @uv run pytest -q -m packaging

install-local:
    @uv tool install .

refresh-local:
    @uv tool install --reinstall --force --no-cache .

reset-local:
    @uv tool uninstall dryheave

clean:
    @rm -rf dist build .pytest_cache htmlcov .coverage coverage.xml .ruff_cache .mypy_cache
