# 25. DevOps Spec — PICKSOME OPS TOOL

> 작성: DevOps / SE
> 작성일: 2026-06-17
> 입력: `21_erd.md`, `22_api_spec.md`, `24_frontend.md`, `00_project_overview.md`
> 다음 역할: User/세무 검증 → PM 최종 통합

---

## 0. Reframed Request

FE/BE/DB 의 실행 환경을 정의한다. **Docker Compose 4 서비스, 환경변수, 백업, 알림(SMTP), 스케줄러, 보안 운영**의 구조를 잡고 코드는 만들지 않는다.

**2026-06-18 갱신** (Open Items 7건 Confirmed):
- **PostgreSQL extension**: `pgcrypto` 필수 (UUID v4 `gen_random_uuid()`). UUID v7 은 app-side `uuid7` 라이브러리 생성 → DB 펑션 의존 회피 (D-283)
- **MVP 알림 채널 env**: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM`, `SMTP_USE_TLS` 만 필수. `KAKAO_*`, `SLACK_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN` 등 Post-MVP 채널 secret 은 **MVP 에 포함하지 않음** (D-281)
- **Feature flags** (env): `FEATURE_EXTERNAL_ACCOUNTANT=false` (D-282), `FEATURE_NOTIFICATION_KAKAO=false`, `FEATURE_NOTIFICATION_SLACK=false`, `FEATURE_VARIANT_BULK_GENERATE=false` (Post-MVP)
- backend Docker image: Python deps 에 `uuid7` (또는 `uuid-v7`) 패키지 추가
- 보안: `.env.example` 에 위 키 모두 placeholder 로 표시. 실제 값은 organization 단위 `.env` 에서 주입

---

## 1. 런타임 토폴로지

```
        ┌─────────────────────┐
        │   Reverse Proxy     │   Caddy (또는 Traefik)
        │ ports: 80, 443      │   - 자동 HTTPS (Let's Encrypt) [Post-MVP]
        │                     │   - / → frontend
        │                     │   - /api → backend
        └──────────┬──────────┘
                   │
        ┌──────────┼──────────┐
        ▼                     ▼
   ┌─────────┐           ┌─────────┐
   │frontend │           │backend  │
   │ Next.js │           │FastAPI  │
   │ :3000   │           │:8000    │
   └─────────┘           └────┬────┘
                              │
                              ▼
                       ┌─────────────┐
                       │ postgres:16 │
                       │   :5432     │
                       └─────────────┘
```

(선택) Post-MVP 확장: Redis (캐시·큐), MinIO (영수증 파일), Mailhog (개발용 SMTP).

---

## 2. Docker Compose (구성 골격)

```yaml
version: "3.9"
name: picksome-ops-tool

services:
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
      TZ: Asia/Seoul
    volumes:
      - postgres-data:/var/lib/postgresql/data
      - ./backup:/backup
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB"]
      interval: 10s
      timeout: 5s
      retries: 5

  backend:
    build: ./backend
    restart: unless-stopped
    environment:
      DATABASE_URL: postgresql+psycopg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
      JWT_SECRET: ${JWT_SECRET}
      JWT_ACCESS_TTL_MIN: 480
      JWT_REFRESH_TTL_DAYS: 30
      TZ: Asia/Seoul
      SMTP_HOST: ${SMTP_HOST}
      SMTP_PORT: ${SMTP_PORT}
      SMTP_USER: ${SMTP_USER}
      SMTP_PASSWORD: ${SMTP_PASSWORD}
      SMTP_FROM: ${SMTP_FROM}
      APP_BASE_URL: ${APP_BASE_URL}
      LOG_LEVEL: INFO
      SENTRY_DSN: ${SENTRY_DSN:-}
    depends_on:
      postgres:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://localhost:8000/healthz"]
      interval: 15s
      timeout: 5s
      retries: 5

  frontend:
    build: ./frontend
    restart: unless-stopped
    environment:
      NEXT_PUBLIC_API_BASE: ${PUBLIC_API_BASE}    # /api/v1
      API_INTERNAL_BASE: http://backend:8000/api/v1
      TZ: Asia/Seoul
    depends_on:
      - backend
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://localhost:3000/api/health"]
      interval: 15s
      timeout: 5s
      retries: 5

  proxy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./infra/Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy-data:/data
      - caddy-config:/config
    depends_on:
      - frontend
      - backend

volumes:
  postgres-data:
  caddy-data:
  caddy-config:
```

### Caddyfile (예)
```
{$APP_HOST}:80 {
  encode gzip zstd
  @api path /api/*
  reverse_proxy @api backend:8000
  reverse_proxy frontend:3000
}
```

> MVP 는 사내망/단일 서버 가정. 외부 노출 시 HTTPS 자동화 옵션 활성.

---

## 3. 환경변수 (`.env.example`)

```env
# ===== DB =====
POSTGRES_USER=picksome
POSTGRES_PASSWORD=change-me-strong
POSTGRES_DB=picksome_ops

# ===== Backend =====
JWT_SECRET=change-me-min-32-bytes-random
APP_BASE_URL=http://localhost
LOG_LEVEL=INFO

# ===== Email (알림) =====
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=PICKSOME OPS <noreply@picksome.co.kr>

# ===== Frontend =====
PUBLIC_API_BASE=/api/v1

# ===== Proxy =====
APP_HOST=localhost

# ===== 옵션 =====
SENTRY_DSN=
```

- `.env` 은 `.gitignore`. `.env.example` 만 커밋.
- 모든 비밀값은 시크릿 매니저 권장 (단일서버 단계는 chmod 600 `.env`).

---

## 4. DB 운영

### 4-1. 마이그레이션
- Alembic. `alembic upgrade head` 를 컨테이너 entrypoint 에서 실행 (옵션 ON/OFF)
- 변경 단위: 1 PR = 1 revision

### 4-2. 백업
- 일 1회 `pg_dump` (논리). cron 컨테이너 또는 호스트 cron.
- 보관: 일별 7개, 주별 4개, 월별 6개 회전 (`backup/daily/`, `backup/weekly/`, `backup/monthly/`)
- 명령 (호스트 cron 예):
  ```
  0 3 * * *  docker exec postgres pg_dump -U $POSTGRES_USER $POSTGRES_DB \
              | gzip > /srv/backup/daily/picksome_$(date +\%F).sql.gz
  ```
- 백업 검증: 주 1회 docker 임시 컨테이너에 restore 후 `select count(*) from monthly_settlement;` 등

### 4-3. 복구 (DR)
- RTO 24h / RPO 1d (MVP)
- 절차: postgres 컨테이너 정지 → 새 볼륨 → restore → 시작 → BE alembic upgrade head

### 4-4. 확장 (Post-MVP)
- WAL 아카이빙 + Point-in-Time Recovery
- 읽기 전용 replica
- 월별 audit_log 파티셔닝 + 2년 외 데이터 archive

---

## 5. 스케줄러 / 배치

MVP 는 BE 내부 **APScheduler** 로 간단히. 별도 컨테이너 분리는 Post-MVP.

| 잡 | 주기 | 동작 |
|---|---|---|
| 정산 마감 알림 | 매일 09:00 KST | `settlement_policy.close_day` D-3, D-1 알림 |
| 부가세 신고 알림 | 매일 09:00 KST | 분기 25일 기준 D-7, D-1 |
| 마진 하락 감지 | 매일 09:00 KST | 30일 이동평균 대비 -10% |
| 정책 NEEDS_REVIEW 갱신 | 매일 03:00 KST | effective_from 180일 초과 자동 표시 |
| 백업 검증 | 매주 일 04:00 KST | restore 샘플 |

스케줄 잡 실행 결과는 `notification` 또는 `batch_run_log` 에 기록.

---

## 6. 로깅 / 모니터링

### 6-1. 로깅
- 공통 JSON 로그 (Python `structlog`)
- 민감정보 자동 마스킹 (`password`, `token`, `cookie` 키)
- 컨테이너 `stdout` → docker driver 로 호스트 파일 또는 Loki (Post-MVP)
- 보관: 30일 (호스트)

### 6-2. 메트릭 / 헬스
- BE `/healthz` (DB ping 포함), FE `/api/health`
- 운영자 대시보드는 Post-MVP (Grafana)

### 6-3. 알림 (운영)
- 헬스체크 실패 시 운영자 이메일
- 백업 실패 시 즉시 알림

### 6-4. 에러 추적
- Sentry (선택) — `SENTRY_DSN` 설정 시 자동 캡처. **민감 PII scrub 규칙 설정 필수.**

---

## 7. 보안 운영

| 영역 | 정책 |
|---|---|
| Secrets | `.env` 권한 600. SCM 커밋 금지. 운영은 시크릿 매니저(Post-MVP) |
| DB 접근 | 컨테이너 네트워크 내부만. 외부 포트 노출 금지 |
| HTTPS | 외부 노출 시 Caddy 자동 인증서 (Post-MVP) |
| JWT | `JWT_SECRET` ≥ 32바이트 랜덤. 회전 절차 문서화 |
| 비밀번호 hash | argon2 권장. bcrypt 도 가능. cost 적절 |
| Backup 암호화 | 백업 파일 gpg 암호화 (Post-MVP) |
| 로그 마스킹 | password/token/cookie/email 의 일부 마스킹 |
| audit_log | mutation 마다 자동 기록 (BE 강제) |
| Vault | `password_vault_entry` 에 평문 비밀번호 입력 시 DB CHECK + BE 검증 양방향 거부 |
| 컨테이너 | non-root user, read-only fs 가능 시 적용 |
| 업데이트 | 베이스 이미지 월 1회 갱신 (renovate/dependabot) |

---

## 8. 배포 절차 (MVP)

단일 호스트 가정.

```
1. git pull
2. cp .env.example .env  (최초 1회) + 값 채움
3. docker compose pull
4. docker compose up -d --build
5. (자동) alembic upgrade head
6. 헬스체크 OK 확인 (`docker compose ps`)
```

### 8-1. 무중단
- MVP 는 다운타임 ≤ 1분 허용 (직접 종료 후 재시작)
- Post-MVP: blue/green 또는 rolling

### 8-2. 롤백
- 직전 이미지 tag 보존 → `docker compose up -d` 로 이전 tag 지정
- DB 마이그레이션 롤백은 alembic downgrade (가능 시) 또는 백업 복구

---

## 9. 디렉토리 / 환경 표준

```
/srv/picksome-ops-tool/
├── compose.yaml
├── .env
├── backend/           (서브모듈 또는 cp from repo)
├── frontend/
├── infra/
│   └── Caddyfile
└── backup/
    ├── daily/
    ├── weekly/
    └── monthly/
```

---

## 10. 시간대 / 인코딩

- 컨테이너 `TZ=Asia/Seoul`
- Postgres `timezone = Asia/Seoul` (저장은 UTC, 표시 KST)
- 파일/CSV 인코딩 = `UTF-8` (BOM 없는 것 권장)
- Excel export 는 xlsxwriter (한국어 그대로 보장)

---

## 11. 비용 / 자원 (MVP 사이징 가정)

| 자원 | 사이즈 |
|---|---|
| CPU | 2 vCPU |
| 메모리 | 4 GiB |
| 디스크 | 50 GiB (DB+백업) |
| OS | Ubuntu LTS 또는 동급 |

추후 사용자/데이터 증가 시 수직 확장 (1단계), Redis/MinIO 분리 (2단계), 매니지드 DB (3단계).

---

## 12. 개발자 경험 (DX)

- `make up`, `make down`, `make logs`, `make test`, `make migrate`
- 사전 커밋 훅: `ruff`, `mypy`, `eslint`, `prettier`, `pytest -q`
- VS Code devcontainer (선택)
- 시드 스크립트: `make seed` → org/partner/카테고리/세금/정책 v1 입력 (운영 비밀번호 절대 포함 X)

---

## 13. Open Items 영향 (DevOps 관점)

| Open Item | 영향 |
|---|---|
| O-002 알림 채널 | SMTP 외 카카오 알림톡/슬랙 webhook 추가 시 env 확장 |
| O-003 세무사 합류 | 외부 사용자 1명 추가 = 추가 자원 영향 미미 |
| 기타 | DB ID 타입은 운영에 무관 (BIGSERIAL 가정 유지) |

---

## 14. Handoff

### Summary
4-서비스 Docker Compose (postgres / backend / frontend / proxy) + Caddy 리버스 프록시. 일 1회 백업 + 주 1회 검증. APScheduler 로 알림 4종 + 정책 검토 자동표시. 로그·헬스체크·민감정보 마스킹 정책 정의.

### Decisions (DevOps 단독 결정)
- Reverse proxy = Caddy (가벼움·자동 HTTPS 옵션). Traefik 대체 가능.
- 스케줄러 = APScheduler 내장 (MVP). Celery/RQ 는 Post-MVP.
- 백업 = pg_dump 일 1 회전(7/4/6). gpg 암호화 Post-MVP.
- 로그 = stdout JSON. Loki 는 Post-MVP.
- 모니터링 대시보드 = Post-MVP.

### Open Questions
- 외부 도메인 적용 여부 → HTTPS 자동화 ON
- 백업 호스트(별도 서버/외부 오브젝트 스토리지) 정책 → 운영 환경 확정 시 결정

### Risks
- 단일 호스트 SPOF — 백업으로 RTO 24h 보전. Post-MVP HA
- SMTP 의존 (알림 누락 시 사용자 영향) — 실패 시 재시도 + 운영자 알림
- APScheduler 가 단일 인스턴스에만 동작 — FE/BE 다중화 시 분리 필요

### Required Inputs for Next Role
- **User/세무**: 실 운영 환경의 SMTP 정보, 도메인, 운영 시간대, 백업 보관 정책 확인
- **PM**: Open Items 답변 종합 후 본 DevOps 명세 보강 (특히 알림 채널·외부 노출 여부)

### Handoff Target
**User / 세무 검증** → PM 최종 통합 (`docs/10_decision_log.md` 갱신)

---

## 15. Action Checklist

- [x] Docker Compose 4 서비스 골격
- [x] 환경변수 `.env.example`
- [x] DB 마이그레이션·백업·복구 정책
- [x] 스케줄러 5 잡
- [x] 로깅/헬스체크/Sentry
- [x] 보안 운영 정책 (Vault/HTTPS/JWT/Backup)
- [x] 배포·롤백 절차
- [x] 시간대·인코딩·사이징
- [x] DX (make, hooks, seed)
- [x] Handoff
- [ ] (Next) User/세무: `docs/26_user_verification.md`
