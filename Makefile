# GameConfig Copilot — developer entry points.
#
# These targets are thin wrappers over the same commands documented in the
# README, so CI and a Windows/PowerShell session run identical steps. On
# Windows `make` is optional: run the underlying command directly, e.g.
#   python -m pytest -q -p no:cacheprovider
#   python -m ruff check src tests
#   python -m gameconfig_copilot.cli eval --suite retrieval
#
# `install` is the only target that needs network access; it installs the
# opt-in groups from pyproject.toml (see [project.optional-dependencies]).

PYTHON ?= python
PIP    ?= $(PYTHON) -m pip
PYTEST ?= $(PYTHON) -m pytest
RUFF   ?= $(PYTHON) -m ruff
GCC    ?= $(PYTHON) -m gameconfig_copilot.cli

.DEFAULT_GOAL := help
.PHONY: help install test lint eval demo

help:
	@echo "GameConfig Copilot"
	@echo "  make install  editable install with [dev,api,rag] extras"
	@echo "  make test     pytest, no .pytest_cache written"
	@echo "  make lint     ruff over src and tests (no auto-fix)"
	@echo "  make eval     retrieval baseline (gcc eval --suite retrieval)"
	@echo "  make demo     print the offline demo walkthrough"

# Offline-safe: `-e` keeps the checkout authoritative, extras stay opt-in so a
# missing wheel only disables one feature group.
install:
	$(PIP) install -e ".[dev,api,rag]"

# `-p no:cacheprovider` keeps the tree clean in CI and in read-only checkouts
# (`addopts` in pyproject already sets the report flags).
test:
	$(PYTEST) -q -p no:cacheprovider

# Check-only on purpose: formatting/lint fixes are a human decision, and CI
# asserts a clean `ruff check`.
lint:
	$(RUFF) check src tests

# Retrieval suite is the only implemented one (S1-07); `--suite all` aggregates
# the agent/ragas suites that ship with S7.
eval:
	$(GCC) eval --suite retrieval

# Explanatory only — no provider calls, no writes. Each step is copy-pasteable
# and degrades gracefully without an API key until `ask` (which needs one).
demo:
	@echo "Offline demo (read-only loop), run from the repo root:"
	@echo "  1. $(PYTHON) -m gameconfig_copilot.cli ingest          # build BM25 index"
	@echo "  2. $(PYTHON) -m gameconfig_copilot.cli search '铁匠老陈 立场'  # retrieval + scores"
	@echo "  3. $(PYTHON) -m gameconfig_copilot.cli ask '霜港城的铁匠老陈是什么立场？'"
	@echo "     -> needs GCC_LLM_API_KEY in .env; answers carry [source] citations"
	@echo "  4. uvicorn gameconfig_copilot.api.main:app --reload   # SSE loop (extra: api)"
	@echo "     GCC_MCP_MODE=write enables commit_draft; default readonly is fail-closed"
	@echo "  5. $(PYTHON) -m gameconfig_copilot.cli eval --suite retrieval"
	@echo "See docs/05-interview-kit.md for the full 5-minute walkthrough."
