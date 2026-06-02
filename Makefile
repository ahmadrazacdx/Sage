# Sage Makefile
# Requires: uv, pnpm (>=10), make (e.g. Git Bash / MSYS2 on Windows)

TIER          ?= fast
FRONTEND_DIR  := frontend/artifacts/sage

.DEFAULT_GOAL := help
.PHONY: help install dev ui-dev lint format test build clean

help:
	@echo ""
	@echo "--------------------------------------------------"
	@echo "         Sage Development Makefile"
	@echo "--------------------------------------------------"
	@echo ""
	@echo "  Setup & Run"
	@echo "    make install     - Setup environment & pre-commit hooks"
	@echo "    make dev         - Start backend in dev mode"
	@echo "    make ui-dev      - Start frontend dev server (Vite)"
	@echo ""
	@echo "  Quality & Testing"
	@echo "    make lint        - Run Ruff and Mypy checks"
	@echo "    make format      - Auto-fix & format Python code"
	@echo "    make test        - Run full test suite"
	@echo ""

install:
	uv sync --all-extras
	uv run pre-commit install --install-hooks
	@echo "✔ Python environment and pre-commit hooks ready."

dev:
	uv run sage --dev

ui-dev:
	pnpm --dir $(FRONTEND_DIR) run dev

lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/
	uv run mypy src/

format:
	uv run ruff check --fix src/ tests/
	uv run ruff format src/ tests/

test:
	uv run pytest tests/