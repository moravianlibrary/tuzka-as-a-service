# taas — common dev/ops tasks. Run `make help` for the list.
.DEFAULT_GOAL := help
SHELL := /bin/bash
COMPOSE := docker compose
# Run dev tooling in the locked env (installs the dev extra on demand).
UV := uv run --extra dev

# Override the test image with: make test IMAGE=path/to/file.jpg
IMAGE ?= test-data/sample.jpg
# Stack the e2e/contract/bench targets run against: compose or the local k3d deploy.
TARGET ?= compose

# Registry for `make build-push` — there is no public registry, host your own.
# Usage: make build-push REGISTRY=ghcr.io/yourorg [TAG=0.1.0]
# TAG defaults to the app version in ./VERSION.
REGISTRY ?=
TAG ?= $(shell cat VERSION 2>/dev/null || echo latest)
GIT_TAG := v$(shell cat VERSION 2>/dev/null)

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-26s\033[0m %s\n", $$1, $$2}'

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

.PHONY: up-clickstack
up-clickstack: env ## Full stack plus ClickHouse; the api and workers emit job events to it
	CLICKHOUSE_URL=http://clickhouse:8123 $(COMPOSE) --profile clickstack up -d --build

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

.PHONY: check-alto-schema
check-alto-schema: ## Validate TuzkaOCR's ALTO against the vendored ALTO 4.4 XSD (FILES=... to pick files)
	@# Not part of `make check`: the generated cases need the ./TuzkaOCR clone, which is
	@# optional (gitignored, see README), so `check` has to stay green without it.
	$(UV) python scripts/check-alto-schema.py $(FILES)

.PHONY: test-contract
test-contract: ## Schemathesis contract/property tests vs a running stack (TARGET=compose|k3d, TAAS_URL)
	@url=$${TAAS_URL:-http://localhost:8080}; \
	key=$$(TARGET=$(TARGET) TAAS_URL=$$url bash scripts/seed-contract.sh | sed -n 's/^API_KEY=//p'); \
	args="run $$url/openapi.json -c all"; \
	[ -n "$$key" ] && args="$$args -H X-API-Key:$$key"; \
	$(UV) schemathesis $$args

.PHONY: test-contract-local
test-contract-local: env ## One command: bring up the API (migrated, no workers/engine), seed, run Schemathesis
	$(COMPOSE) up -d --build --wait api
	@TAAS_URL=http://localhost:8080 $(MAKE) --no-print-directory test-contract

.PHONY: load-test
load-test: ## Load-test the API with Locust, headless (USERS=, RATE=, DURATION= to tune)
	@url=$${TAAS_URL:-http://localhost:8080}; \
	key=$$(TARGET=$(TARGET) TAAS_URL=$$url bash scripts/seed-contract.sh | sed -n 's/^API_KEY=//p'); \
	TAAS_API_KEY=$$key $(UV) locust -f tests/load/locustfile.py --host $$url \
	  --headless -u $${USERS:-20} -r $${RATE:-5} -t $${DURATION:-30s}

.PHONY: bench-micro
bench-micro: ## Micro-benchmarks of the hot paths (tests/perf; not part of `make check`)
	$(UV) pytest tests/perf --benchmark-only --benchmark-sort=name \
	  --benchmark-columns=min,median,mean,stddev,ops

.PHONY: perf-taas
perf-taas: ## Whole-taas measurement against a fake-engine stack (USERS=, RATE=, DURATION=, OUT=)
	USERS=$(or $(USERS),50) RATE=$(or $(RATE),10) DURATION=$(or $(DURATION),2m) bash bench/perf_taas.sh

.PHONY: test-integration
test-integration: env ## End-to-end smoke against the full stack (TARGET=compose|k3d, IMAGE=)
	TARGET=$(TARGET) IMAGE=$(IMAGE) bash scripts/test.sh

.PHONY: test-batch
test-batch: ## E2E batch run against a running stack: archive in, results archive out (TARGET=, PAGES=, IMAGE=)
	TARGET=$(TARGET) PAGES=$(or $(PAGES),5) bash scripts/test-batch.sh

.PHONY: test-integration-fast
test-integration-fast: ## E2E against an already-running stack (no build/up; TARGET=compose|k3d)
	NO_UP=1 TARGET=$(TARGET) IMAGE=$(IMAGE) bash scripts/test.sh

.PHONY: test-compat
test-compat: ## Run the legacy-compat server e2e (stack must be up; TARGET=compose|k3d)
	TARGET=$(TARGET) IMAGE=$(IMAGE) bash scripts/test-compat.sh

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

.PHONY: local-deploy-forward-bench
local-deploy-forward-bench: ## Port-forwards on shifted ports, so the latency proxy can own 8080/8081/9100
	API_PORT=18080 COMPAT_PORT=18081 RESULTS_PORT=19100 bash $(LOCAL_DIR)/forward.sh up

# --- Legacy (ProArc/PERO) vs new (taas client) API benchmark ---
# Two entry points, both driven by bench/api/scenarios.toml — which is the file you
# edit to change what runs. Full method and findings: bench/api/README.md.
#
#   make bench                        # every enabled matrix, docker-compose stack
#   make bench ONLY=protocol N=25     # one matrix, 25 pages per case
#   make bench TARGET=k3d             # against the k3d/k3s deploy
#   make bench DRY_RUN=1              # print the plan + its estimated cost, run nothing
#
# `bench` assumes a running stack. `bench-up` is the cold-start path for k3d: it
# builds the engine images, creates the cluster, installs the chart and starts the
# host engines, then hands off to `bench TARGET=k3d`. Idempotent — safe to re-run.
#
#   make bench-up                     # cold start, then the default sweep
#   SKIP_BENCH=1 make bench-up        # bring the stack up and stop there
#   WITH_GPU=0 make bench-up          # CPU-only rig (~4.5 GB less to build)
#
# Latency cases need the ports rewired first; the runner prints the exact fix if
# they are not (k3d: make local-deploy-forward-down && make local-deploy-forward-bench).
BENCH_API_DIR ?= bench/api

.PHONY: bench-up
bench-up: ## Cold-start k3d (engines, cluster, chart) then bench (SKIP_BENCH=1, WITH_GPU=0)
	bash $(BENCH_API_DIR)/bench-up.sh $(if $(ONLY),ONLY=$(ONLY)) $(if $(N),N=$(N))

.PHONY: bench
bench: ## Run the API benchmark scenarios (TARGET=compose|k3d, ONLY=, N=, IMAGES=, DRY_RUN=1)
	@# Unbuffered: piped to a log or tee, Python block-buffers its own progress output
	@# while subprocess output streams straight through, so the log reads out of order.
	PYTHONUNBUFFERED=1 $(UV) python $(BENCH_API_DIR)/run_scenarios.py --target $(TARGET) \
	  $(if $(ONLY),--only $(ONLY)) $(if $(N),--n $(N)) $(if $(IMAGES),--images $(IMAGES)) \
	  $(if $(DRY_RUN),--dry-run)

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
set-version: ## Set the app version everywhere (make set-version VERSION=x.y.z)
	@test -n "$(VERSION)" || { echo "usage: make set-version VERSION=x.y.z"; exit 1; }
	@echo "$(VERSION)" > VERSION
	@sed -i 's/^version = ".*"/version = "$(VERSION)"/' pyproject.toml compat/pyproject.toml
	@sed -i 's/version="[^"]*"/version="$(VERSION)"/' app/main.py compat/app/main.py
	@sed -i 's/^appVersion: .*/appVersion: "$(VERSION)"/' deploy/helm/taas/Chart.yaml
	@echo "set app version to $(VERSION). Helm chart 'version' is packaging-only and bumped separately; run 'make version-check' to confirm"

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
