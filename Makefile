.PHONY: up down logs build migrate seed test lint shell-be shell-db backup

COMPOSE      = docker compose -f docker-compose.yml
COMPOSE_DEV  = $(COMPOSE) -f docker-compose.dev.yml

# ── 실행 ───────────────────────────────────────────────────────────────
up:
	$(COMPOSE) up -d

up-dev:
	$(COMPOSE_DEV) up -d

down:
	$(COMPOSE) down

down-v:
	$(COMPOSE) down -v

build:
	$(COMPOSE) build --no-cache

logs:
	$(COMPOSE) logs -f

logs-be:
	$(COMPOSE) logs -f backend

logs-fe:
	$(COMPOSE) logs -f frontend

# ── DB 마이그레이션 ────────────────────────────────────────────────────
migrate:
	$(COMPOSE) exec backend alembic upgrade head

migrate-dev:
	$(COMPOSE_DEV) exec backend alembic upgrade head

seed:
	$(COMPOSE) exec backend python scripts/internal/picksome_init.py

# ── 테스트 ─────────────────────────────────────────────────────────────
test:
	$(COMPOSE_DEV) exec backend pytest -ra -q

test-cov:
	$(COMPOSE_DEV) exec backend pytest --cov=app --cov-report=term-missing

# ── 린트 ───────────────────────────────────────────────────────────────
lint:
	$(COMPOSE_DEV) exec backend ruff check app
	$(COMPOSE_DEV) exec frontend npm run lint

typecheck:
	$(COMPOSE_DEV) exec frontend npm run typecheck

# ── 셸 ─────────────────────────────────────────────────────────────────
shell-be:
	$(COMPOSE) exec backend bash

shell-db:
	$(COMPOSE) exec postgres psql -U $${POSTGRES_USER} $${POSTGRES_DB}

# ── 백업 ───────────────────────────────────────────────────────────────
backup:
	$(COMPOSE) exec postgres pg_dump -U $${POSTGRES_USER} $${POSTGRES_DB} \
		| gzip > backup/daily/picksome_$$(date +%F).sql.gz
	@echo "백업 완료: backup/daily/picksome_$$(date +%F).sql.gz"

# ── 헬스 ───────────────────────────────────────────────────────────────
ps:
	$(COMPOSE) ps
