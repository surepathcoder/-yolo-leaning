# VisionGuard developer commands.
#
# Windows: run these through Git Bash, or use the equivalent commands printed
# by `make help`.  Nothing here is required -- every target is a thin wrapper.

PY  ?= venv/Scripts/python.exe
NPM ?= npm
INFRA = deployment/docker/compose.infra.yml

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

# ------------------------------------------------------------------ infra ---
.PHONY: infra-up infra-down infra-logs
infra-up: ## Start PostgreSQL + Redis (host ports 5434 / 6380)
	docker compose -f $(INFRA) up -d

infra-down: ## Stop PostgreSQL + Redis
	docker compose -f $(INFRA) down

infra-logs: ## Tail infrastructure logs
	docker compose -f $(INFRA) logs -f

# ---------------------------------------------------------------- backend ---
.PHONY: install dev migrate revision seed
install: ## Install backend + vision Python dependencies
	$(PY) -m pip install -r backend/requirements-dev.txt
	$(PY) -m pip install -r vision/requirements.txt

dev: ## Run the API with autoreload on :8000
	cd backend && ../$(PY) -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

migrate: ## Apply database migrations
	$(PY) -m alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add cameras"
	$(PY) -m alembic revision --autogenerate -m "$(m)"

seed: ## Seed a demo organization, store, cameras and users
	$(PY) scripts/seed_demo_data.py

# --------------------------------------------------------------- frontend ---
.PHONY: frontend-install frontend-dev frontend-build frontend-test
frontend-install: ## Install dashboard dependencies
	cd frontend && $(NPM) install

frontend-dev: ## Run the dashboard dev server on :5173
	cd frontend && $(NPM) run dev

frontend-build: ## Production build of the dashboard
	cd frontend && $(NPM) run build

frontend-test: ## Run dashboard tests
	cd frontend && $(NPM) run test -- --run

# ------------------------------------------------------------------ tests ---
.PHONY: test test-unit test-integration test-vision lint
test: ## Run the whole backend + vision test suite
	cd backend && ../$(PY) -m pytest

test-unit: ## Run only fast unit tests
	cd backend && ../$(PY) -m pytest -m "not integration and not vision and not slow"

test-integration: ## Run tests that need PostgreSQL/Redis
	cd backend && ../$(PY) -m pytest -m integration

test-vision: ## Run tests that need the YOLO weights
	cd backend && ../$(PY) -m pytest -m vision

lint: ## Lint and format-check the Python code
	$(PY) -m ruff check backend vision scripts training
	$(PY) -m ruff format --check backend vision scripts training

fmt: ## Auto-format the Python code
	$(PY) -m ruff format backend vision scripts training
	$(PY) -m ruff check --fix backend vision scripts training

# ----------------------------------------------------------------- docker ---
.PHONY: up down build logs
up: ## Start the full stack in Docker
	docker compose up -d --build

down: ## Stop the full stack
	docker compose down

logs: ## Tail all stack logs
	docker compose logs -f
