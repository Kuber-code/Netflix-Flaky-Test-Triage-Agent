.DEFAULT_GOAL := help
UV ?= uv

.PHONY: help install check lint fmt typecheck test cov eval clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install: ## Sync the locked environment (including dev dependencies)
	$(UV) sync --all-groups

check: lint typecheck test ## Lint, type-check and test -- the gate CI enforces

lint: ## ruff check + format verification
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt: ## Apply ruff formatting and safe fixes
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck: ## mypy --strict
	$(UV) run mypy

test: ## Run the test suite
	$(UV) run pytest

cov: ## Run tests with coverage gates on the deterministic core
	$(UV) run pytest --cov --cov-report=term-missing --cov-report=xml

eval: ## Run the evaluation harness and rewrite eval/results/latest.md
	$(UV) run python eval/run_eval.py

clean: ## Remove caches and local runtime state
	rm -rf .pytest_cache .mypy_cache .ruff_cache .hypothesis .coverage coverage.xml htmlcov
	rm -rf .flaketriage
