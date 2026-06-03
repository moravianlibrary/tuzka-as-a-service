# taas — common dev/ops tasks. Run `make help` for the list.
.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose

# Override the test image with: make test IMAGE=path/to/file.jpg
IMAGE ?= test-data/sample.jpg

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

.PHONY: env
env: ## Create .env and .env.app from templates (generates a Fernet key)
	@test -f .env || cp .env.example .env
	@if [ ! -f .env.app ]; then \
	  cp .env.app.example .env.app; \
	  key=$$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"); \
	  sed -i "s|^KEY_ENCRYPTION_SECRET=.*|KEY_ENCRYPTION_SECRET=$$key|" .env.app; \
	  echo "created .env.app with a fresh KEY_ENCRYPTION_SECRET"; \
	fi
	@echo "env ready (.env, .env.app)"

.PHONY: build
build: ## Build all images
	$(COMPOSE) build

.PHONY: up
up: env ## Build + start the full stack (detached)
	$(COMPOSE) up -d --build

.PHONY: infra
infra: env ## Start only infra (postgres, redis, minio) for local uvicorn dev
	$(COMPOSE) up -d postgres redis minio-incoming minio-results minio-init

.PHONY: down
down: ## Stop the stack (keep volumes)
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop the stack and delete volumes (DB, MinIO, OCR data)
	$(COMPOSE) down -v --remove-orphans

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

.PHONY: logs
logs: ## Tail logs from all services
	$(COMPOSE) logs -f

.PHONY: seed
seed: ## Register the OCR backend + create a test user
	TAAS_URL=$${TAAS_URL:-http://localhost:8080} bash scripts/seed-backend.sh

.PHONY: test
test: env ## Run the end-to-end smoke test (IMAGE=... to override)
	IMAGE=$(IMAGE) bash scripts/test.sh

.PHONY: e2e
e2e: test ## Alias for `make test`

.PHONY: test-fast
test-fast: ## E2E against an already-running stack (no build/up)
	NO_UP=1 IMAGE=$(IMAGE) bash scripts/test.sh

.PHONY: test-compat
test-compat: ## Run the legacy-compat server e2e (stack must be up)
	IMAGE=$(IMAGE) bash scripts/test-compat.sh

.PHONY: format
format: ## Format code + auto-fix lint issues (ruff)
	ruff format .
	ruff check --fix .

.PHONY: lint
lint: ## Check formatting + lint without changes (ruff)
	ruff check .
	ruff format --check .

.PHONY: secret
secret: ## Generate a Fernet KEY_ENCRYPTION_SECRET
	@python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
