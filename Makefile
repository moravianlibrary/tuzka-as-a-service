# taas — common dev/ops tasks. Run `make help` for the list.
.DEFAULT_GOAL := help
SHELL := /bin/bash
MAKEFLAGS += --no-print-directory

# ─── Tools ───────────────────────────────────────────────────────────────────────────
# Docker first, not podman: k3d, the engine proxy (host.docker.internal) and the GPU
# engine need docker. DOCKER=podman to override where that's enough.
DOCKER ?= $(shell command -v docker 2>/dev/null || command -v podman 2>/dev/null)
COMPOSE := $(DOCKER) compose
# Run dev tooling in the locked env (installs the dev extra on demand).
UV := uv run --extra dev

# Pinned scanner versions, so `make check` gives the same answer tomorrow.
PIP_AUDIT_VERSION := 2.10.1
GITLEAKS_IMAGE := ghcr.io/gitleaks/gitleaks:v8.21.2

# ─── Test env: throwaway containers, never the dev stack's DB (the tests TRUNCATE) ──
TEST_PG := taas-test-pg
TEST_REDIS := taas-test-redis
TEST_PG_PORT ?= 55432
TEST_REDIS_PORT ?= 56379
TEST_DATABASE_URL := postgresql+asyncpg://taas:taas@localhost:$(TEST_PG_PORT)/taas
TEST_REDIS_URL := redis://localhost:$(TEST_REDIS_PORT)/15

# start_container NAME, RUN-ARGS: start NAME, creating it with `run -d RUN-ARGS` first.
define start_container
$(DOCKER) start $(1) >/dev/null 2>&1 || $(DOCKER) run -d --name $(1) $(2) >/dev/null
endef

# ─── Inputs ──────────────────────────────────────────────────────────────────────────
# Override the test image with: make test IMAGE=path/to/file.jpg
IMAGE ?= test-data/sample.jpg
# Stack the e2e/admin/contract/bench targets run against: compose or the local k3d deploy.
TARGET ?= compose

# ─── Release ─────────────────────────────────────────────────────────────────────────
# Images: taas-api, taas-worker, taas-compat, each tagged :$(TAG) and :latest, under
# REGISTRY/ when it's set. There is no public registry; host your own, e.g.
# make build-push REGISTRY=ghcr.io/yourorg [TAG=0.1.0]. TAG defaults to ./VERSION.
REGISTRY ?=
TAG ?= $(shell cat VERSION 2>/dev/null || echo latest)
GIT_TAG := v$(shell cat VERSION 2>/dev/null)
IMAGE_PREFIX := $(if $(REGISTRY),$(REGISTRY)/)
IMAGES := taas-api taas-worker taas-compat

# ─── Help ────────────────────────────────────────────────────────────────────────────
.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_%-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-26s\033[0m %s\n", $$1, $$2}'

# ─── Env ─────────────────────────────────────────────────────────────────────────────

.PHONY: env
env: ## Create .env, .env.app and .env.local from templates (one fresh Fernet key for both apps)
	@test -f .env || cp .env.example .env
	@# The compose stack (.env.app) and host dev (.env.local) share the DB, so they
	@# must share the key that encrypts the backends' engine keys in it.
	@key=$$(sed -n 's/^KEY_ENCRYPTION_SECRET=//p' .env.app .env.local 2>/dev/null | head -1); \
	[ -n "$$key" ] || key=$$($(MAKE) -s secret); \
	for f in .env.app .env.local; do \
	  [ -f $$f ] && continue; \
	  sed "s|^KEY_ENCRYPTION_SECRET=.*|KEY_ENCRYPTION_SECRET=$$key|" $$f.example > $$f; \
	  echo "created $$f"; \
	done
	@echo "env ready (.env, .env.app, .env.local)"

.PHONY: secret
secret: ## Generate a Fernet KEY_ENCRYPTION_SECRET
	@$(UV) python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# ─── Compose stack ───────────────────────────────────────────────────────────────────

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

.PHONY: restart
restart: down up ## Stop the stack, then build and start it again

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

.PHONY: logs
logs: ## Tail logs from all services
	$(COMPOSE) logs -f

logs-%: ## Tail one service's logs (e.g. logs-api, logs-worker-poller)
	$(COMPOSE) logs -f $*

.PHONY: clean
clean: ## Delete local caches (pytest, mypy, ruff, __pycache__)
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage .benchmarks
	find . -name __pycache__ -type d -not -path './.venv/*' -prune -exec rm -rf {} +

.PHONY: clean-volumes
clean-volumes: ## Stop the stack and DELETE its volumes (destroys the DB, MinIO and OCR data)
	$(COMPOSE) down -v --remove-orphans

.PHONY: clean-images
clean-images: ## Stop the stack and delete the images it built
	$(COMPOSE) down --rmi local --remove-orphans

.PHONY: clean-all
clean-all: clean clean-volumes clean-images ## All of the above (destroys data)

.PHONY: admin
admin: ## Run taas-admin in the stack's api (requires ARGS="user list"; TARGET=compose|k3d)
	$(if $(ARGS),,$(error ARGS is required, e.g. make admin ARGS="user list"))
	@source scripts/lib/taas-admin.sh && TARGET=$(TARGET) taas_admin $(ARGS)

.PHONY: seed
seed: ## Register the OCR backend + create a test user
	TAAS_URL=$${TAAS_URL:-http://localhost:8080} bash scripts/seed-backend.sh

# ─── Dev (on the host, against `make infra`; settings from .env.local) ─────────────

DEV_PORT ?= 8080

.PHONY: dev-api
dev-api: env ## Run the API on the host with auto-reload (DEV_PORT=8080)
	$(UV) uvicorn app.main:app --reload --port $(DEV_PORT)

.PHONY: dev-submit
dev-submit: env ## Run the submit worker on the host
	$(UV) python -m app.workers.submit

.PHONY: dev-poller
dev-poller: env ## Run the poller worker on the host
	$(UV) python -m app.workers.poller

.PHONY: dev-cleanup
dev-cleanup: env ## Run the cleanup worker on the host
	$(UV) python -m app.workers.cleanup

# ─── Database (alembic; DATABASE_URL from the env, else .env.local) ────────────────

.PHONY: db-upgrade
db-upgrade: ## Migrate the database up to REV (default head)
	$(UV) alembic upgrade $(or $(REV),head)

.PHONY: db-downgrade
db-downgrade: ## Migrate the database down to REV (default: one migration back)
	$(UV) alembic downgrade $(or $(REV),-1)

.PHONY: db-migrate
db-migrate: ## Autogenerate the next numbered migration from the models (requires MSG=...); read it before committing
	$(if $(MSG),,$(error MSG is required, e.g. make db-migrate MSG="add job tags"))
	@# Against a database at head: the diff is between it and the models.
	@last=$$(ls alembic/versions | sed -n 's/^\([0-9]\{3\}\)_.*\.py$$/\1/p' | sort | tail -1); \
	$(UV) alembic revision --autogenerate --rev-id $$(printf '%03d' $$((10#$$last + 1))) -m "$(MSG)"

.PHONY: db-history
db-history: ## List the migrations, newest first
	$(UV) alembic history

# ─── Tests ───────────────────────────────────────────────────────────────────────────

.PHONY: test
test: ## Run the unit/component tests (pytest; DB/redis tests skip if absent)
	$(UV) pytest

.PHONY: test-env
test-env: ## Start a throwaway Postgres (:55432, migrated) and Redis (:56379) for test-integration
	@$(call start_container,$(TEST_PG),-e POSTGRES_USER=taas -e POSTGRES_PASSWORD=taas -e POSTGRES_DB=taas -p 127.0.0.1:$(TEST_PG_PORT):5432 postgres:16)
	@$(call start_container,$(TEST_REDIS),-p 127.0.0.1:$(TEST_REDIS_PORT):6379 redis:7-alpine)
	@# Over TCP: initdb's temporary server answers on the socket only.
	@until $(DOCKER) exec $(TEST_PG) pg_isready -h 127.0.0.1 -U taas -d taas >/dev/null 2>&1; do sleep 1; done
	@DATABASE_URL=$(TEST_DATABASE_URL) $(UV) alembic upgrade head
	@echo "test env ready: TEST_DATABASE_URL=$(TEST_DATABASE_URL) TEST_REDIS_URL=$(TEST_REDIS_URL)"

.PHONY: test-env-clean
test-env-clean: ## Remove the throwaway test Postgres and Redis (and their data)
	-$(DOCKER) rm -f $(TEST_PG) $(TEST_REDIS)

.PHONY: test-env-reset
test-env-reset: test-env-clean test-env ## Recreate the test env from scratch

.PHONY: test-integration
test-integration: test-env ## The unit tests plus the ones that need Postgres and Redis (none skip)
	TEST_DATABASE_URL=$(TEST_DATABASE_URL) TEST_REDIS_URL=$(TEST_REDIS_URL) $(UV) pytest

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
	@TAAS_URL=http://localhost:8080 $(MAKE) test-contract

.PHONY: load-test
load-test: ## Load-test the API with Locust, headless (USERS=, RATE=, DURATION= to tune)
	@url=$${TAAS_URL:-http://localhost:8080}; \
	key=$$(TARGET=$(TARGET) TAAS_URL=$$url bash scripts/seed-contract.sh | sed -n 's/^API_KEY=//p'); \
	TAAS_API_KEY=$$key $(UV) locust -f tests/load/locustfile.py --host $$url \
	  --headless -u $${USERS:-20} -r $${RATE:-5} -t $${DURATION:-30s}

# ─── E2E (against a running stack; TARGET=compose|k3d) ──────────────────────────────

.PHONY: e2e
e2e: env ## Bring the stack up, submit an image, fetch and preview the result (IMAGE=, FMT=)
	TARGET=$(TARGET) IMAGE=$(IMAGE) bash scripts/test.sh

.PHONY: e2e-fast
e2e-fast: ## e2e against the already-running stack (no build, no up)
	NO_UP=1 TARGET=$(TARGET) IMAGE=$(IMAGE) bash scripts/test.sh

.PHONY: e2e-batch
e2e-batch: ## A batch end to end: archive in, results archive out (PAGES=5, IMAGE=)
	TARGET=$(TARGET) PAGES=$(or $(PAGES),5) bash scripts/test-batch.sh

.PHONY: e2e-compat
e2e-compat: ## The legacy-compat server end to end
	TARGET=$(TARGET) IMAGE=$(IMAGE) bash scripts/test-compat.sh

# ─── Local k3d deploy ────────────────────────────────────────────────────────────────
# The Helm chart in a k3d cluster, plus host TuzkaOCR engines reached through a reverse
# tunnel (deploy/local/README.md). Typical flow:
#   make engine-build-cpu     # build the engine image once (engine-build: cpu + gpu)
#   make k3d-up               # cluster + chart + engines
#   make k3d-forward          # port-forwards (background) + open the UIs
#   make k3d-stop / k3d-start # pause and resume it all, keeping the cluster
#   make k3d-down / k3d-clean # uninstall the chart / delete the cluster
K3D_CLUSTER   ?= taas
# The cluster's own kubeconfig, never the shell's KUBECONFIG (which may point at a
# real cluster): every k3d target, and anything run with TARGET=k3d, uses only this.
K3D_KUBECONFIG ?= $(HOME)/.kube/k3d-$(K3D_CLUSTER).yaml
k3d-%: export KUBECONFIG := $(K3D_KUBECONFIG)
ifeq ($(TARGET),k3d)
export KUBECONFIG := $(K3D_KUBECONFIG)
endif
LOCAL_DIR     := deploy/local
LOCAL_COMPOSE := $(COMPOSE) -f $(LOCAL_DIR)/engine-proxy/compose.yaml
TUZKAOCR_DIR  := TuzkaOCR

.PHONY: k3d-up
k3d-up: ## Create the cluster, install the chart and start the host engines (engine-build first)
	@for img in $$($(LOCAL_COMPOSE) config --images | grep tuzkaocr); do \
	  $(DOCKER) image inspect "$$img" >/dev/null 2>&1 || \
	  { echo "engine image $$img not found — run 'make engine-build' first"; exit 1; }; \
	done
	CLUSTER=$(K3D_CLUSTER) bash $(LOCAL_DIR)/setup.sh
	$(LOCAL_COMPOSE) up -d

.PHONY: k3d-start
k3d-start: ## Resume a stopped cluster and its host engines
	k3d cluster start $(K3D_CLUSTER)
	@$(MAKE) k3d-kubeconfig
	$(LOCAL_COMPOSE) up -d
	kubectl wait --for=condition=Ready pods --all --timeout=300s \
	  --field-selector=status.phase!=Succeeded

.PHONY: k3d-stop
k3d-stop: ## Stop the port-forwards, the host engines and the cluster; keep them all
	-bash $(LOCAL_DIR)/forward.sh down
	$(LOCAL_COMPOSE) stop
	k3d cluster stop $(K3D_CLUSTER)

.PHONY: k3d-status
k3d-status: ## The cluster, its pods, the host engines and the forwards
	@k3d cluster list $(K3D_CLUSTER) 2>/dev/null || echo "no k3d cluster $(K3D_CLUSTER)"
	@echo; echo "kubeconfig: $(K3D_KUBECONFIG)"
	@echo; kubectl get pods 2>/dev/null || echo "cluster not reachable"
	@echo; $(LOCAL_COMPOSE) ps
	@echo; pids="$${TMPDIR:-/tmp}/taas-local-forward.pids"; \
	if [ -f "$$pids" ] && kill -0 "$$(head -1 "$$pids")" 2>/dev/null; then \
	  echo "port-forwards: running"; else echo "port-forwards: not running"; fi

.PHONY: k3d-down
k3d-down: k3d-kubeconfig ## Stop the engines and uninstall the chart; KEEP the cluster
	CLUSTER=$(K3D_CLUSTER) bash $(LOCAL_DIR)/teardown.sh

.PHONY: k3d-clean
k3d-clean: ## Stop the engines and DELETE the cluster (everything in it)
	CLUSTER=$(K3D_CLUSTER) bash $(LOCAL_DIR)/teardown.sh --cluster

.PHONY: k3d-forward
k3d-forward: k3d-kubeconfig ## Start the port-forwards (background) and open the UIs
	bash $(LOCAL_DIR)/forward.sh up

.PHONY: k3d-forward-stop
k3d-forward-stop: ## Stop the background port-forwards
	bash $(LOCAL_DIR)/forward.sh down

.PHONY: k3d-forward-bench
k3d-forward-bench: k3d-kubeconfig ## Port-forwards on shifted ports, so the latency proxy can own 8080/8081/9100
	API_PORT=18080 COMPAT_PORT=18081 RESULTS_PORT=19100 bash $(LOCAL_DIR)/forward.sh up

.PHONY: k3d-seed
k3d-seed: k3d-kubeconfig ## Seed 3 test users (alice/bob/carol) with priorities and fixed keys
	bash $(LOCAL_DIR)/seed-users.sh

.PHONY: k3d-scenarios
k3d-scenarios: k3d-kubeconfig ## Run the prioritization scenarios (needs k3d-seed and k3d-forward)
	bash $(LOCAL_DIR)/scenarios.sh

# Not in help: (re)write the cluster's kubeconfig, for the targets that run kubectl.
.PHONY: k3d-kubeconfig
k3d-kubeconfig:
	@mkdir -p $(dir $(K3D_KUBECONFIG))
	@k3d kubeconfig get $(K3D_CLUSTER) > $(K3D_KUBECONFIG)

# ─── Engines (built from ./TuzkaOCR, never pulled) ──────────────────────────────────

.PHONY: engine-build-cpu
engine-build-cpu: ## Build the CPU engine image (tuzkaocr:local-cpu)
	$(DOCKER) build -t tuzkaocr:local-cpu -f $(TUZKAOCR_DIR)/Dockerfile $(TUZKAOCR_DIR)

.PHONY: engine-build-gpu
engine-build-gpu: ## Build the GPU engine image (tuzkaocr:local-gpu)
	$(DOCKER) build -t tuzkaocr:local-gpu -f $(TUZKAOCR_DIR)/Dockerfile.gpu $(TUZKAOCR_DIR)

.PHONY: engine-build
engine-build: engine-build-cpu engine-build-gpu ## Build both engine images

# ─── Bench ───────────────────────────────────────────────────────────────────────────
# Micro-benchmarks, whole-taas measurement and profiling, then the API benchmark.

.PHONY: bench-micro
bench-micro: ## Micro-benchmarks of the hot paths (tests/perf; not part of `make check`)
	$(UV) pytest tests/perf --benchmark-only --benchmark-sort=name \
	  --benchmark-columns=min,median,mean,stddev,ops

.PHONY: bench-taas
bench-taas: ## Whole-taas measurement against a fake-engine stack (TARGET=compose|k3d, USERS=, RATE=, DURATION=, OUT=)
	TARGET=$(TARGET) USERS=$(or $(USERS),50) RATE=$(or $(RATE),10) DURATION=$(or $(DURATION),2m) bash bench/perf_taas.sh

.PHONY: bench-profile
bench-profile: ## Profile API and workers with external py-spy during bench-taas (TARGET=k3d)
	TARGET=$(TARGET) USERS=$(or $(USERS),50) RATE=$(or $(RATE),10) DURATION=$(or $(DURATION),2m) bash bench/perf_profile.sh

# API benchmark: legacy (ProArc/PERO) vs new (taas client).
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
# they are not (k3d: make k3d-forward-stop && make k3d-forward-bench).
BENCH_API_DIR ?= bench/api

.PHONY: bench-up
bench-up: ## Cold-start k3d (engines, cluster, chart) then bench (SKIP_BENCH=1, WITH_GPU=0)
	bash $(BENCH_API_DIR)/bench-up.sh $(if $(ONLY),ONLY=$(ONLY)) $(if $(N),N=$(N))

.PHONY: bench
bench: ## Run the API benchmark scenarios (TARGET=compose|k3d, ONLY=, N=, IMAGES=, DRY_RUN=1, ALL_DEVICES=1)
	@# Unbuffered: piped to a log or tee, Python block-buffers its own progress output
	@# while subprocess output streams straight through, so the log reads out of order.
	PYTHONUNBUFFERED=1 $(UV) python $(BENCH_API_DIR)/run_scenarios.py --target $(TARGET) \
	  $(if $(ONLY),--only $(ONLY)) $(if $(N),--n $(N)) $(if $(IMAGES),--images $(IMAGES)) \
	  $(if $(DRY_RUN),--dry-run) $(if $(ALL_DEVICES),--all-devices)

# ─── Quality ─────────────────────────────────────────────────────────────────────────

.PHONY: check
check: format-check lint typecheck version-check chart-lint audit secrets-scan test ## The full quality gate: format, lint, types, version, chart, CVEs, secrets, unit tests

.PHONY: format
format: ## Format the code (ruff format)
	$(UV) ruff format .

.PHONY: format-check
format-check: ## Check the formatting without changing anything
	$(UV) ruff format --check .

.PHONY: lint
lint: ## Lint without changing anything (ruff)
	$(UV) ruff check .

.PHONY: lint-fix
lint-fix: ## Lint and apply ruff's safe fixes
	$(UV) ruff check --fix .

.PHONY: typecheck
typecheck: ## Type-check with mypy --strict
	$(UV) mypy

.PHONY: chart-lint
chart-lint: ## Lint the Helm chart (renders every template)
	helm lint --strict deploy/helm/taas

.PHONY: audit
audit: ## Check the locked dependencies for known vulnerabilities (pip-audit; needs network)
	@reqs=$$(mktemp); trap 'rm -f "$$reqs"' EXIT; \
	uv export --frozen --all-extras --no-emit-project --format requirements-txt -q > "$$reqs"; \
	uvx pip-audit==$(PIP_AUDIT_VERSION) -r "$$reqs" --disable-pip --require-hashes

.PHONY: secrets-scan
secrets-scan: ## Scan the git history for committed secrets (gitleaks; .gitleaksignore lists vetted ones)
	@if command -v gitleaks >/dev/null; then \
	  gitleaks git . --no-banner --redact; \
	else \
	  $(DOCKER) run --rm -v "$(CURDIR)":/repo:ro $(GITLEAKS_IMAGE) git /repo --no-banner --redact; \
	fi

.PHONY: version-check
version-check: ## Fail if version literals have drifted from ./VERSION
	@bash scripts/check-version.sh

# ─── Release ─────────────────────────────────────────────────────────────────────────

.PHONY: build
build: ## Build the api, worker and compat images, tagged :$(TAG) and :latest (REGISTRY= to prefix)
	$(DOCKER) build -t $(IMAGE_PREFIX)taas-api:$(TAG) -t $(IMAGE_PREFIX)taas-api:latest -f api.Containerfile .
	$(DOCKER) build -t $(IMAGE_PREFIX)taas-worker:$(TAG) -t $(IMAGE_PREFIX)taas-worker:latest -f worker.Containerfile .
	$(DOCKER) build -t $(IMAGE_PREFIX)taas-compat:$(TAG) -t $(IMAGE_PREFIX)taas-compat:latest -f compat/Containerfile compat

.PHONY: push
push: ## Push the built images, :$(TAG) and :latest (requires REGISTRY=...)
	$(if $(REGISTRY),,$(error REGISTRY is required, e.g. make push REGISTRY=ghcr.io/yourorg))
	@for image in $(IMAGES); do \
	  for tag in $(TAG) latest; do \
	    $(DOCKER) push $(IMAGE_PREFIX)$$image:$$tag || exit 1; \
	  done; \
	done

.PHONY: build-push
build-push: build push ## Build, then push the images (requires REGISTRY=...)

.PHONY: release
release: build-push tag-version tag-latest ## Push the images and move the v$(VERSION) and latest git tags (requires REGISTRY=...)

.PHONY: set-version
set-version: ## Set the app version everywhere (requires VERSION=x.y.z)
	$(if $(VERSION),,$(error VERSION is required, e.g. make set-version VERSION=x.y.z))
	@echo "$(VERSION)" > VERSION
	@sed -i 's/^version = ".*"/version = "$(VERSION)"/' pyproject.toml compat/pyproject.toml
	@sed -i 's/version="[^"]*"/version="$(VERSION)"/' app/main.py compat/app/main.py
	@sed -i 's/^appVersion: .*/appVersion: "$(VERSION)"/' deploy/helm/taas/Chart.yaml
	@echo "set app version to $(VERSION). Helm chart 'version' is packaging-only and bumped separately; run 'make version-check' to confirm"

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
