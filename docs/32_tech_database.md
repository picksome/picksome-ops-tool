# 32. 데이터베이스 기술 스택 (DBA)

> PICKSOME OPS TOOL 데이터베이스 기술과 채택 이유.
> 작성일 2026-06-22 / 작성자 DBA Role.

---

## 1. 핵심

| 분류 | 기술 | 버전 | 채택 이유 |
|------|------|------|-----------|
| RDBMS | **PostgreSQL** | 16 (Alpine) | JSONB / TIMESTAMPTZ / partial index / generated column / row-level security 표준 지원 |
| 마이그레이션 | **Alembic** | 1.14+ | 코드형 DB 변경. autogenerate 미사용 (의도된 hand-written SQL) |
| 드라이버 | psycopg | v3.2+ | binary 모드. SQLAlchemy 2 와 권장 조합 |
| 백업 | `pg_dump` cron 일 1회 + 주 1회 복원 검증 | docs/25_devops.md §4-3 |

---

## 2. 확장 (Extensions)

M001 가 자동 활성화:

| 확장 | 용도 |
|------|------|
| **pgcrypto** | `gen_random_uuid()` — UUID v4 fallback (DB default) |
| **pg_trgm** | trigram index. 한국어 LIKE 검색 가속 (상품/사용자) |

UUID v7 은 앱(`core/ids.py`) 가 생성 — PG native 미지원이라 펑션 의존 회피 (D-283).

---

## 3. 명명 규칙

| 대상 | 규칙 | 예시 |
|------|------|------|
| 테이블 | snake_case, 단수형 | `monthly_settlement`, `app_user`, `product_variant` |
| 컬럼 | snake_case | `created_at`, `effective_from`, `business_key` |
| PK | `id` (UUID) | 모든 테이블 동일 |
| FK | `<참조테이블>_id` | `org_id`, `partner_id`, `monthly_settlement_id` |
| Enum | `enum_<용도>` | `enum_business_type`, `enum_audit_action` |
| Unique idx | `uq_<table>_<cols>` | `uq_ecp_org_name_ver` |
| 일반 idx | `ix_<table>_<cols>` | `ix_tax_policy_org_eff` |
| Check | `ck_<table>_<rule>` | `ck_organization_reopen_limit` |

---

## 4. 표준 컬럼 그룹

### 4-1. AUDIT_COLS (모든 테이블)
```sql
created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
created_by UUID NULL,
updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
updated_by UUID NULL,
deleted_at TIMESTAMPTZ NULL,   -- soft delete
deleted_by UUID NULL
```

- `updated_at` 트리거가 BEFORE UPDATE 시 자동 갱신
- soft delete: `deleted_at IS NOT NULL` 행은 일반 조회에서 제외 (서비스 단 WHERE 절 필수)

### 4-2. POLICY_COLS (정책 11종)
```sql
org_id UUID NOT NULL REFERENCES organization(id),
version INT NOT NULL,
effective_from DATE NOT NULL,
effective_to DATE NULL,
source_type enum_policy_source NOT NULL DEFAULT 'MANUAL',
source_url TEXT NULL,
source_checked_at TIMESTAMPTZ NULL,
approved_by UUID NULL REFERENCES app_user(id),
approved_at TIMESTAMPTZ NULL,
reliability_status enum_policy_reliability NOT NULL DEFAULT 'CONFIRMED',
memo TEXT NULL,
is_active BOOLEAN NOT NULL DEFAULT TRUE
```

- **박제 원칙**: 정책은 수정이 아니라 새 버전 INSERT (version + effective_from)
- `effective_to IS NULL` 인 row 만 현재 활성. `resolve_active_policies()` 가 시점 기반 조회

---

## 5. UUID 전략 (D-283)

| 종류 | 생성 방식 | 비고 |
|------|-----------|------|
| PK/FK | **UUID v7** (앱 `uuid7` 라이브러리) | 시간순 정렬 가능 → B-tree hot page 효율 |
| DB default | `gen_random_uuid()` (v4) | 앱이 죽어도 row 삽입은 fallback |
| 사용자 노출 | `business_key` (별도 컬럼) | `SETTLE-2026-06`, `PROD-00012` — UUID 직접 노출 금지 |
| SYSTEM 템플릿 | `00000000-...-0000000000ff` | M002 baseline sentinel |
| PICKSOME | `00000000-...-000000000001` | 내부 init 전용 |

---

## 6. 멀티테넌트 격리

- 모든 운영 테이블은 `org_id UUID NOT NULL REFERENCES organization(id)` 강제 (43개 중 거의 전부)
- 모든 service 함수는 `WHERE org_id = :o` 를 첫 번째 필터로 포함
- 라우터 `CurrentUser.org_id` 가 IDOR 차단 base

예외 (org_id 없음):
- `organization` — 자기 자신
- `notification_channel` — 글로벌 메타
- `audit_log` — org_id 컬럼 있음 (org 별 격리)

---

## 7. SYSTEM 템플릿 패턴 (M002)

신규 조직 가입 시 `INSERT...SELECT` 로 SYSTEM org 의 활성 정책 row 들을 신규 org_id 로 복사.

복사 대상 테이블:
- expense_category_policy
- evidence_type_policy
- tax_policy
- target_margin_policy
- settlement_policy
- marketplace_fee_policy

복사 제외 (조직 고유):
- partner / app_user / partner_share_policy / payment_fee_policy / monthly_settlement 등

---

## 8. 트랜잭션 / 격리수준

| 항목 | 정책 |
|------|------|
| 격리수준 | PostgreSQL 기본 `READ COMMITTED` |
| 정산 LOCK | `monthly_settlement.status = 'LOCKED'` 후 모든 관련 expense/sales_order 수정 차단 (앱 단 검증, FK 무결성 보조) |
| 멱등 INSERT | `picksome_init.py` 는 org 중복 시 즉시 종료 |
| 정책 활성 전환 | `effective_to = now()::date` + 새 row INSERT (replace 아닌 append) |

---

## 9. 인덱스 전략

| 패턴 | 예시 | 의도 |
|------|------|------|
| 부분 인덱스 (`WHERE deleted_at IS NULL`) | uq_ecp_org_name_ver | soft delete row 무시 |
| 정책 활성 조회 | `(org_id, effective_from DESC) WHERE effective_to IS NULL` | resolve_active_policies 가속 |
| 외래키 인덱스 | 모든 FK 컬럼 자동 부여 권장 | JOIN 성능 |
| trigram | 한국어 `name` LIKE | pg_trgm GIN index (필요 시) |

---

## 10. 시간대 / 인코딩

| 항목 | 값 |
|------|----|
| 저장 | UTC (`TIMESTAMPTZ`) |
| 표시 | Asia/Seoul (BE 가 환산, FE 가 포맷) |
| 인코딩 | UTF-8 (DB / CSV 모두) |
| 통화 | KRW (`int` 또는 `Decimal`. float 사용 금지) |

---

## 11. 데이터 타입 매핑

| 의미 | PostgreSQL 타입 | 비고 |
|------|----------------|------|
| ID (PK/FK) | UUID | v7 권장 |
| 금액 (KRW) | BIGINT | 원 단위 정수. 절대 float/numeric 금액 누락 금지 |
| 비율/율 | NUMERIC(7,4) | 0.1234 (12.34%) — 4자리 |
| 한국어 텍스트 | TEXT | VARCHAR 길이 제한 미사용 (UTF-8 안전) |
| 코드/enum | enum 타입 | 25종 enum 정의됨 |
| JSON | JSONB | 검색 가능. `tier_json`, `settings_json` 등 |
| 날짜 | DATE / TIMESTAMPTZ | 회계 일자 = DATE, 이벤트 = TIMESTAMPTZ |

---

## 12. 채택하지 않은 기술 (의도적)

| 선택지 | 채택 안 함 이유 |
|--------|---------------|
| MySQL / MariaDB | JSONB / 부분 인덱스 / TIMESTAMPTZ 가 PG 보다 약함 |
| MongoDB | 정산 / 환율 / FK 무결성 핵심 — RDBMS 필수 |
| Oracle / MSSQL | 라이선스 비용. SaaS 보급 어려움 |
| Citus / Sharding | 단일 org 데이터 규모 < 1억 row 예상 — 수년 후 검토 |
| Row Level Security (RLS) | 앱 단에서 `WHERE org_id` 강제로 충분. 운영 복잡도 추가됨 |
| Logical replication 슬레이브 | 단일 인스턴스 + 일 1회 dump 로 충분 (MVP) |
| pgbouncer | uvicorn 단일 인스턴스라 불필요. 다중 워커 시 검토 |

---

## 13. 백업 / 복원

| 항목 | 정책 |
|------|------|
| 일별 백업 | `pg_dump | gzip` — 03:00 KST cron |
| 보관 | 일 30개 / 월 12개 / 분기 4개 |
| 복원 검증 | 주 1회 별도 docker volume 에 restore 후 row count 비교 |
| 재해복구 | 컨테이너 정지 → 새 볼륨 → restore → alembic upgrade head |

---

## 14. 알려진 한계 / 후속 작업

- partial unique index 가 많아서 마이그레이션 시간 증가 → 데이터 규모 100만 row 이상 시 CONCURRENTLY 옵션 검토
- JSONB 컬럼 (`tier_json`, `settings_json`) 의 스키마 검증은 앱 단 pydantic 만 — DB CHECK 미적용
