# PICKSOME OPS TOOL

셀러 운영 자동화 SaaS — 상품 소싱부터 정산·세무 리포트까지 하나의 시스템으로 관리합니다.

> 기존 `마진계산기&장부.xlsx`의 업무 프로세스와 계산 로직을 재설계한 웹 애플리케이션입니다.

> 설치 전 필요한 프로그램/버전은 [`REQUIREMENTS.md`](REQUIREMENTS.md) 참고.
> 매일 쓰는 방법은 [`docs/71_operations_manual.md`](docs/71_operations_manual.md) 참고.

---

## 현재 상태 — Core v1 Frozen

내부 기능(Core)은 완성돼 있고, **외부 판매처 연동(Connector)만 남았습니다.**

| 영역 | 상태 |
|---|---|
| 상품 · 소싱 · 원가 · 마진 | 동작 |
| 재고 원장 · 발주 · 입고 · 재주문 | 동작 |
| 비용 · 정산 · 월 마감 · 배분 · 지급 | 동작 |
| 예외 센터 · 일일 리포트 · 자동화 | 동작 |
| 백업 · 복원 · 마이그레이션 안전장치 | 검증됨 |
| MCP (AI 접근) | 선택 기능. 꺼도 나머지가 돈다 |
| **쿠팡 · 네이버 · 11번가 주문 수집** | **미연동** — 아래 참고 |

주문·클레임·재고가 **자동으로 들어오지 않습니다.** 지금은 파일 가져오기와 수동
입력으로 돌리는 상태이고, 커넥터가 붙으면 그 부분이 자동화됩니다.

| 판매처 | 막고 있는 것 |
|---|---|
| 쿠팡 | `403 IP_NOT_ALLOWED` — 서명은 통과. WING 에 서버 IP 등록 필요 |
| 네이버 | 커머스API 자격증명이 아님 (개발자센터 키) |
| 11번가 · 넥스트엔진 | 기술 문서 없음 |

Core 는 동결 상태입니다. 변경 정책은 [`docs/69_core_v1_freeze.md`](docs/69_core_v1_freeze.md).

---

## Tech Stack

### Frontend

| 분류 | 기술 | 버전 | 역할 |
|------|------|------|------|
| Framework | **Next.js** | 15.0.3 | App Router, SSR/RSC, rewrites 기반 API 프록시 |
| Language | **TypeScript** | 5.x | strict 모드 전체 적용 |
| UI Runtime | **React** | 19.0.0-rc | Server/Client Component 분리 |
| Styling | **Tailwind CSS** | v4 beta | CSS 변수 기반 디자인 토큰 (`--color-*`) |
| Component | **shadcn/ui + Radix UI** | latest | 접근성 보장 헤드리스 컴포넌트 |
| Data Fetching | **TanStack Query** | v5 | 서버 상태 관리, 30s polling, optimistic update |
| Table | **TanStack Table** | v8 | 정렬·선택·페이지네이션 내장 |
| Form | **React Hook Form + Zod** | v7 / v3 | 스키마 기반 유효성 검사 |
| State | **Zustand** | v5 | 인증 토큰·UI 상태 |
| Icons | **Lucide React** | 0.456 | |
| Toast | **Sonner** | v1 | |

### Backend

| 분류 | 기술 | 버전 | 역할 |
|------|------|------|------|
| Framework | **FastAPI** | ≥0.115 | REST API, OpenAPI 자동 생성 |
| Language | **Python** | 3.12 | |
| ORM | **SQLAlchemy** | 2.x | async 지원, 2.0 스타일 |
| Migration | **Alembic** | ≥1.14 | 버전 관리 마이그레이션 |
| Validation | **Pydantic** | v2 | 요청/응답 스키마, Settings |
| Auth | **python-jose** + **argon2-cffi** | — | JWT RS256, bcrypt 대체 argon2 |
| DB Driver | **psycopg** | v3 (binary) | PostgreSQL async 드라이버 |
| Excel | **openpyxl + xlsxwriter** | — | 세무 리포트 Excel 생성 |
| Scheduler | **APScheduler** | ≥3.10 | 정산·부가세·마진 야간 배치 (Asia/Seoul) |
| Error Tracking | **Sentry** | — | SENTRY_DSN 설정 시 자동 캡처 |
| Linter | **Ruff** | ≥0.8 | E/F/I/B/UP/N 룰셋 |
| Type Check | **mypy** | ≥1.13 | strict 모드 |
| Test | **pytest + pytest-asyncio** | — | async 단위·통합 테스트 |

### Database

| 기술 | 버전 | 역할 |
|------|------|------|
| **PostgreSQL** | 16-alpine | 메인 DB, pgcrypto 확장 (UUID 생성) |

### Infra / DevOps

| 분류 | 기술 | 역할 |
|------|------|------|
| Container | **Docker + Docker Compose** | 서비스 오케스트레이션 (postgres / backend / frontend / proxy 4개 서비스) |
| Reverse Proxy | **Caddy 2** | HTTPS 자동 인증서, `/api/*` → backend 라우팅 |
| Build | **Makefile** | `make up / migrate / seed / test / lint` 단축 명령 |

---

## Architecture

```
Browser
  │
  └─ HTTPS :443
       │
   [Caddy]  ← infra/Caddyfile
       ├── /api/* ──────────────→ [Backend :8000]  FastAPI
       │                               │
       └── /* ─────────────────→ [Frontend :3000]  Next.js
                                        │
                                   /api/* rewrite
                                        │
                               (동일 Caddy 경유 → Backend)

[Backend]  ←→  [PostgreSQL :5432]
```

모든 서비스는 Docker 내부 `internal` bridge 네트워크로 연결됩니다. 외부에는 Caddy만 노출됩니다.

---

## Project Structure

```
picksome-ops-tool/
├── frontend/                   # Next.js 15 App Router
│   ├── app/
│   │   ├── (app)/              # 인증 필요 페이지 (AppGuard 래핑)
│   │   │   ├── dashboard/
│   │   │   ├── products/
│   │   │   ├── margin/         # calculator / actual / simulator
│   │   │   ├── settlements/    # monthly
│   │   │   ├── expenses/
│   │   │   ├── reports/        # 세무 리포트 (Excel 다운로드)
│   │   │   ├── notifications/  # 알림 센터
│   │   │   ├── policies/       # fees / tax
│   │   │   └── admin/          # users / settings
│   │   └── (auth)/             # 로그인
│   ├── components/
│   │   ├── shell/              # AppGuard, Header, Sidebar, PageHeader, NotificationBell
│   │   ├── table/              # DataTable, TablePagination, BulkActionBar
│   │   ├── ui/                 # shadcn 기반 Badge, Button, Finance 컴포넌트 등
│   │   ├── expense/            # ExpenseDrawer
│   │   └── margin/             # PolicyAutoField
│   ├── lib/
│   │   ├── api.ts              # Base fetch client (401 자동 갱신, envelope 언래핑)
│   │   ├── api/                # 도메인별 API 클라이언트
│   │   │   ├── margin.ts
│   │   │   ├── expenses.ts
│   │   │   ├── products.ts
│   │   │   ├── settlements.ts
│   │   │   ├── reports.ts
│   │   │   ├── notifications.ts
│   │   │   ├── policies.ts
│   │   │   └── admin.ts
│   │   ├── format.ts           # formatKRW, formatPercent, formatDateTime
│   │   └── auth.ts             # 토큰 로컬 스토리지 관리
│   ├── hooks/                  # useCurrentUser, useToast
│   └── stores/                 # Zustand: auth, ui
│
├── backend/                    # FastAPI + SQLAlchemy 2
│   ├── app/
│   │   ├── api/v1/             # 라우터 (엔드포인트)
│   │   ├── services/           # 비즈니스 로직 (마진 계산, 정산, 리포트 생성)
│   │   ├── repositories/       # DB 쿼리 레이어
│   │   ├── domain/             # 도메인 모델 / Pydantic 스키마
│   │   ├── core/               # 보안, 예외, 의존성
│   │   ├── db/                 # SQLAlchemy 엔진, 세션, 마이그레이션
│   │   └── io/                 # Excel 리포트 생성기 (openpyxl/xlsxwriter)
│   └── scripts/                # seed, 초기 데이터 스크립트
│
├── infra/
│   └── Caddyfile               # Caddy 리버스 프록시 설정
│
├── backup/                     # DB 백업 디렉토리 (.gitkeep)
├── docker-compose.yml          # 프로덕션
├── docker-compose.dev.yml      # 개발 오버라이드
├── Makefile                    # 단축 명령
└── guide.html                  # 사용자 가이드 (브라우저로 열기)
```

---

## Quick Start

> **Docker가 안 되는 환경(예: Windows 10 Home)이라면** 아래 대신
> [`docs/34_local_dev_setup_no_docker.md`](docs/34_local_dev_setup_no_docker.md) 를 따르세요.
>
> **개발이 아니라 운영하려는 것이라면** (매일 장부를 보려고 켠다면)
> 아이콘 하나로 끝납니다 — [`docs/85_local_operation_mode.md`](docs/85_local_operation_mode.md).
>
> ```powershell
> powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_local_shortcuts.ps1
> ```
>
> 바탕화면에 **Seller OS 시작 / 종료 / 상태** 가 생깁니다. 시작은 DB·백엔드·
> 프론트·worker 를 순서대로 띄우고, **PC 가 꺼져 있던 동안의 주문·클레임까지
> 따라잡습니다.**
> PostgreSQL 네이티브 설치 + 직접 실행 경로를 처음부터 끝까지 안내합니다.

### 1. 환경변수 설정

```bash
cp .env.example .env
# .env 파일 편집: POSTGRES_*, JWT_SECRET, SMTP_* 등 설정
```

### 2. 실행 (프로덕션)

```bash
make up        # docker compose up -d
make migrate   # alembic upgrade head
make seed      # 초기 데이터 (organization, partner, 정책 9건)
```

### 3. 개발 모드

```bash
make up-dev    # hot-reload 포함 개발 컨테이너 기동
make migrate-dev
```

### 4. 접속

| 서비스 | 주소 |
|--------|------|
| 앱 | `https://<APP_HOST>` |
| API Swagger | `https://<APP_HOST>/api/docs` |
| OpenAPI JSON | `https://<APP_HOST>/api/openapi.json` |

---

## Makefile 명령어

```bash
make up            # 전체 서비스 기동
make up-dev        # 개발 모드 기동 (hot-reload)
make down          # 서비스 중지
make down-v        # 서비스 중지 + 볼륨 삭제
make build         # 이미지 재빌드 (no-cache)
make logs          # 전체 로그 스트리밍
make logs-be       # 백엔드 로그만
make migrate       # DB 마이그레이션 실행
make seed          # 초기 데이터 삽입
make test          # pytest 실행
make test-cov      # 커버리지 포함 테스트
make lint          # ruff (BE) + eslint (FE)
make typecheck     # tsc --noEmit (FE)
make shell-be      # 백엔드 컨테이너 bash
make shell-db      # psql 접속
make backup        # DB 수동 백업
```

---

## Key Design Decisions

### 정책 기반 계산 (Policy-Driven)
마켓 수수료율, 세율, 풀필먼트비 등 모든 비즈니스 수치는 정책 테이블로 관리합니다. 각 정책은 `effective_from / effective_to + version` 을 가져 **과거 시점의 계산을 재현**할 수 있습니다.

### 순이익 기반 정산 (Net Profit Settlement)
수익배분은 매출이 아닌 **모든 비용을 차감한 순이익** 기준입니다.
```
순이익 = 총매출 − 상품 직접비 − 사업 공통비(expenses)
```
적자월은 이월(carryover)되어 다음 흑자월에서 우선 차감됩니다.

### UUID PK, business_key 별도 노출
모든 PK/FK는 UUID입니다. 사용자에게 노출되는 식별자는 별도 `business_key` 컬럼을 사용합니다.

### 보안
- 비밀번호는 argon2 해시 저장, 평문 저장 금지
- 민감 정보(계좌, 인증서)는 보관 위치만 기록
- 금액 계산은 `int`(원 단위) 또는 `Decimal` — `float` 사용 금지

---

## Environment Variables

| 변수 | 설명 | 기본값 |
|------|------|--------|
| `POSTGRES_USER` | DB 사용자 | — |
| `POSTGRES_PASSWORD` | DB 비밀번호 | — |
| `POSTGRES_DB` | DB 이름 | — |
| `JWT_SECRET` | JWT 서명 키 | — |
| `JWT_EXPIRE_MINUTES` | 토큰 만료 (분) | `720` |
| `APP_HOST` | 도메인 (Caddy) | `localhost` |
| `SMTP_HOST` | 이메일 서버 | — |
| `SMTP_PORT` | 이메일 포트 | `587` |
| `SMTP_USER` | SMTP 계정 | — |
| `SMTP_PASSWORD` | SMTP 비밀번호 | — |
| `SETTLEMENT_REOPEN_LIMIT_DEFAULT` | 정산 재개 최대 횟수 | `3` |
| `SENTRY_DSN` | Sentry 에러 추적 | — |
| `LOG_LEVEL` | 로그 레벨 | `INFO` |
| `FEATURE_EXTERNAL_ACCOUNTANT` | 외부 세무사 기능 플래그 | `false` |
| `FEATURE_NOTIFICATION_KAKAO` | 카카오 알림 플래그 | `false` |
| `ADMIN_SEED_PASSWORD` | (로컬 개발용, 선택) 초기 admin 시드 비밀번호. 미설정 시 시드 실행할 때 랜덤 생성 후 콘솔에 1회 출력 | — |

---

## User Roles

| 역할 | 설명 |
|------|------|
| `ADMIN` | 전체 관리 (정책 수정, 사용자 관리 포함) |
| `OPERATOR` | 운영 담당 (상품, 비용, 정산) |
| `SOURCING` | 상품 소싱 전담 |
| `ACCOUNTING` | 정산·세무 담당 |
| `READONLY` | 읽기 전용 |
| `EXTERNAL_ACCOUNTANT` | 외부 세무사 (리포트 조회만) |

---

## 운영 스크립트

### 로컬 운영 (Windows) — 저장소 루트에서

[`docs/85_local_operation_mode.md`](docs/85_local_operation_mode.md)

```powershell
scripts\start_seller_os.ps1        # DB·백엔드·프론트·worker + 따라잡기 + 브라우저
scripts\stop_seller_os.ps1         # 역순 종료 (DB 는 기본적으로 안 내림)
scripts\status_seller_os.ps1       # 서비스 + 데이터 최신성
scripts\restart_seller_os.ps1

scripts\install_local_shortcuts.ps1   # 바탕화면 바로가기
scripts\install_autostart.ps1         # 로그인 자동 시작 (기본 미등록)
scripts\test\Test-LocalOperations.ps1 # 셸 계층 검사
```

`.ps1` 파일은 **UTF-8 with BOM** 이어야 합니다 — BOM 이 없으면 Windows
PowerShell 5.1 이 ANSI 로 읽어 한글 주석이 깨지고 구문 오류가 납니다.

### 배치·점검 — `backend/` 에서 실행

자세한 내용은 [`docs/71_operations_manual.md`](docs/71_operations_manual.md).

```bash
# 기동 직후 못 돈 기간 따라잡기 (외부 READ only)
python -m app.scripts.startup_catchup

# 환경 점검 (DB 접속 없음) · 상태 조회 (읽기 전용)
python -m app.scripts.preflight
python -m app.scripts.ops_status

# 백업 (일7/주4/월6 회전 · 무결성 검증 포함)
python -m scripts.run_backup
python -m scripts.run_backup --dry-run

# 복원 검증 — 일회용 DB 를 만들어 복원하고 지운다 (개발 DB 안 건드림)
python -m scripts.verify_backup_restore

# 마이그레이션 되돌리기 검증 — 임시 DB 에서만 (개발 DB fail-closed)
python -m scripts.verify_migrations

# 자격증명 진단 (읽기 전용, 값 출력 안 함)
python scripts/auth_diagnose.py
```

> **손으로 `alembic downgrade` 하지 마세요.** 예전에 임시 DB 를 지정했다고 믿고
> 돌렸다가 개발 DB 가 내려간 적이 있습니다. `verify_migrations` 가 대상을
> 확인하고 막습니다.

---

## Docs

설계 문서는 `docs/` 디렉토리에 있습니다.

| 파일 | 내용 |
|------|------|
| `docs/00_project_overview.md` | PM / 아키텍처 개요 |
| `docs/20_prd.md` | PRD (기획) |
| `docs/21_erd.md` | ERD (DB 설계) |
| `docs/22_api_spec.md` | API 명세 |
| `docs/23_design.md` | UI/UX 설계 |
| `docs/24_frontend.md` | 프론트엔드 구조 |
| `docs/25_devops.md` | DevOps / 인프라 (Docker Compose 기반 정식 배포) |
| `docs/34_local_dev_setup_no_docker.md` | 로컬 개발 셋업 — Docker 없이 (Windows, AI 에이전트용 실행형 런북) |
| `docs/10_decision_log.md` | 결정 로그 (D-280~D-286) |
| `docs/69_core_v1_freeze.md` | **Core v1 동결 — 범위·불변조건·보안경계·변경정책** |
| `docs/70_backup_and_key_recovery.md` | 백업 자동화 · 마스터 키 복구 절차 |
| `docs/71_operations_manual.md` | **운영 매뉴얼 — 매일/매주/매월 무엇을 하나** |
| `REQUIREMENTS.md` | 설치 전 필요한 프로그램/버전 목록 |
| `AGENTS.md` | AI 에이전트(Codex 등) 진입점 |
| `guide.html` | 사용자 가이드 (브라우저로 열기) |

---

## License

All rights reserved. 사용·배포는 별도 협의가 필요합니다.
