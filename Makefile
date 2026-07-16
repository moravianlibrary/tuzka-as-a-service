# taas — common dev/ops tasks. Run `make help` for the list.
.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose
# Run dev tooling in the locked env (installs the dev extra on demand).
UV := uv run --extra dev

# Override the test image with: make test IMAGE=path/to/file.jpg
IMAGE ?= test-data/sample.jpg

# Registry for `make build-push` — there is no public registry, host your own.
# Usage: make build-push REGISTRY=ghcr.io/yourorg [TAG=0.1.0]
# TAG defaults to the app version in ./VERSION.
REGISTRY ?=
TAG ?= $(shell cat VERSION 2>/dev/null || echo latest)
GIT_TAG := v$(shell cat VERSION 2>/dev/null)

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

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

.PHONY: build-push
build-push: ## Build + push api/worker/compat images (REGISTRY=... [TAG=$(TAG)])
	@test -n "$(REGISTRY)" || { echo "set REGISTRY, e.g. make build-push REGISTRY=ghcr.io/yourorg"; exit 1; }
	docker build -t $(REGISTRY)/taas-api:$(TAG)    -f api.Containerfile    .
	docker build -t $(REGISTRY)/taas-worker:$(TAG) -f worker.Containerfile .
	docker build -t $(REGISTRY)/taas-compat:$(TAG) -f compat/Containerfile compat
	docker push $(REGISTRY)/taas-api:$(TAG)
	docker push $(REGISTRY)/taas-worker:$(TAG)
	docker push $(REGISTRY)/taas-compat:$(TAG)
	@echo "pushed taas-{api,worker,compat}:$(TAG) to $(REGISTRY)"

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
test: ## Run the unit/component tests (pytest; DB/redis tests skip if absent)
	$(UV) pytest

.PHONY: coverage-report
coverage-report: ## Unit tests with a coverage summary
	$(UV) pytest --cov=app --cov=compat --cov-report=term-missing

.PHONY: test-contract
test-contract: ## Schemathesis contract/property tests vs a running instance (TAAS_URL, MASTER_KEY)
	@url=$${TAAS_URL:-http://localhost:8080}; \
	key=$$(TAAS_URL=$$url MASTER_KEY=$$MASTER_KEY bash scripts/seed-contract.sh | sed -n 's/^API_KEY=//p'); \
	args="run $$url/openapi.json -c all"; \
	[ -n "$$MASTER_KEY" ] && args="$$args -H X-Master-Key:$$MASTER_KEY"; \
	[ -n "$$key" ] && args="$$args -H X-API-Key:$$key"; \
	: "Only the two ops that rotate the fixture user's key are excluded — otherwise the"; \
	: "fuzzer reuses the username from GET /admin/users and rotates the key mid-run,"; \
	: "invalidating the X-API-Key for every later request. Everything else (incl. DELETE,"; \
	: "which is harmlessly 409-guarded by the seeded job) stays in scope."; \
	args="$$args --exclude-path /admin/users/{username}/rotate-key --exclude-path /admin/users/{username}/key"; \
	$(UV) schemathesis $$args

.PHONY: test-contract-local
test-contract-local: env ## One command: bring up the API (migrated, no workers/engine), seed, run Schemathesis
	$(COMPOSE) up -d --build --wait api
	@MASTER_KEY="$$(grep -E '^MASTER_KEY=' .env.app | cut -d= -f2-)" TAAS_URL=http://localhost:8080 \
	  $(MAKE) --no-print-directory test-contract

.PHONY: test-integration
test-integration: env ## End-to-end smoke against the full stack (IMAGE=... to override)
	IMAGE=$(IMAGE) bash scripts/test.sh

.PHONY: test-integration-fast
test-integration-fast: ## E2E against an already-running stack (no build/up)
	NO_UP=1 IMAGE=$(IMAGE) bash scripts/test.sh

.PHONY: test-compat
test-compat: ## Run the legacy-compat server e2e (stack must be up)
	IMAGE=$(IMAGE) bash scripts/test-compat.sh

# --- Local k3d deploy: Helm chart in k3d + host TuzkaOCR engine via reverse tunnel ---
# All targets share the `local-deploy-` prefix. Typical flow:
#   make local-deploy-build-cpu        # build the engine image once
#   make local-deploy-up               # cluster + chart + engine
#   make local-deploy-forward-up       # port-forwards + open UIs (background)
#   make local-deploy-forward-down / local-deploy-down / local-deploy-clean
LOCAL_DIR     := deploy/local
LOCAL_COMPOSE := $(COMPOSE) -f $(LOCAL_DIR)/engine-proxy/compose.yaml
TUZKAOCR_DIR  := TuzkaOCR

.PHONY: local-deploy-build-cpu
local-deploy-build-cpu: ## Build the local CPU engine image (tuzkaocr:local-cpu)
	docker build -t tuzkaocr:local-cpu -f $(TUZKAOCR_DIR)/Dockerfile $(TUZKAOCR_DIR)

.PHONY: local-deploy-build-gpu
local-deploy-build-gpu: ## Build the local GPU engine image (tuzkaocr:local-gpu)
	docker build -t tuzkaocr:local-gpu -f $(TUZKAOCR_DIR)/Dockerfile.gpu $(TUZKAOCR_DIR)

.PHONY: local-deploy-build
local-deploy-build: local-deploy-build-cpu local-deploy-build-gpu ## Build both engine images

.PHONY: local-deploy-up
local-deploy-up: ## Bring up the k3d stack + both host engines (build the images first)
	@for img in $$($(LOCAL_COMPOSE) config --images | grep tuzkaocr); do \
	  docker image inspect "$$img" >/dev/null 2>&1 || \
	  { echo "engine image $$img not found — run 'make local-deploy-build' first"; exit 1; }; \
	done
	bash $(LOCAL_DIR)/setup.sh
	$(LOCAL_COMPOSE) up -d

.PHONY: local-deploy-down
local-deploy-down: ## Stop the engine + uninstall the chart, KEEP the k3d cluster
	bash $(LOCAL_DIR)/teardown.sh

.PHONY: local-deploy-clean
local-deploy-clean: ## Stop the engine + chart AND delete the k3d cluster (full removal)
	bash $(LOCAL_DIR)/teardown.sh --cluster

.PHONY: local-deploy-forward-up
local-deploy-forward-up: ## Start port-forwards (background) + open the UIs
	bash $(LOCAL_DIR)/forward.sh up

.PHONY: local-deploy-forward-down
local-deploy-forward-down: ## Stop the background port-forwards
	bash $(LOCAL_DIR)/forward.sh down

.PHONY: local-deploy-seed
local-deploy-seed: ## Seed 3 test users (alice/bob/carol) with priorities + keys
	bash $(LOCAL_DIR)/seed-users.sh

.PHONY: local-deploy-scenarios
local-deploy-scenarios: ## Run the prioritization test scenarios (needs seed + forwards)
	bash $(LOCAL_DIR)/scenarios.sh

.PHONY: local-deploy-bench-db
local-deploy-bench-db: ## Seed ~10M synthetic analytics rows + time the dashboard queries (ROWS=, MODE=seed|query|clean)
	bash $(LOCAL_DIR)/bench-analytics-db.sh

.PHONY: check
check: lint typecheck version-check test ## Run the full quality gate (lint + types + version + unit tests)

.PHONY: format
format: ## Format code + auto-fix lint issues (ruff)
	$(UV) ruff format .
	$(UV) ruff check --fix .

.PHONY: lint
lint: ## Check formatting + lint without changes (ruff)
	$(UV) ruff check .
	$(UV) ruff format --check .

.PHONY: typecheck
typecheck: ## Type-check with mypy --strict
	$(UV) mypy

.PHONY: version-check
version-check: ## Fail if version literals have drifted from ./VERSION
	@bash scripts/check-version.sh

.PHONY: set-version
set-version: ## Set the version everywhere (make set-version VERSION=x.y.z)
	@test -n "$(VERSION)" || { echo "usage: make set-version VERSION=x.y.z"; exit 1; }
	@echo "$(VERSION)" > VERSION
	@sed -i 's/^version = ".*"/version = "$(VERSION)"/' pyproject.toml compat/pyproject.toml
	@sed -i 's/version="[^"]*"/version="$(VERSION)"/' app/main.py compat/app/main.py
	@sed -i 's/^version: .*/version: $(VERSION)/' deploy/helm/taas/Chart.yaml
	@sed -i 's/^appVersion: .*/appVersion: "$(VERSION)"/' deploy/helm/taas/Chart.yaml
	@echo "set version to $(VERSION); run 'make version-check' to confirm"

.PHONY: secret
secret: ## Generate a Fernet KEY_ENCRYPTION_SECRET
	@python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

.PHONY: tag-version
tag-version: ## Force-retag v$(VERSION) and push it
	@git tag -d $(GIT_TAG) 2>/dev/null || true
	@git push origin :refs/tags/$(GIT_TAG) 2>/dev/null || true
	git tag $(GIT_TAG)
	git push origin $(GIT_TAG)

.PHONY: tag-latest
tag-latest: ## Move the `latest` tag to HEAD and push it
	@git tag -d latest 2>/dev/null || true
	@git push origin :refs/tags/latest 2>/dev/null || true
	git tag latest
	git push origin latest
