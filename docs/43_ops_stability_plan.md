# 43. 운영 안정성 확장 — 작업 계획

> **선행 문서**: `40_ops_improvement_plan.md`(기획) · `41_redesign_worklog.md`(개편 로그) ·
> `42_redesign_report.md`(개편 보고) · `29_global_search.md` · `CLAUDE.md`
> **작성**: 2026-08-06
> **범위**: Phase 1~9. 기존 OPS 콘솔 개편을 **유지·확장**한다. 재설계·재작성이 아니다.

---

## 0. 분석 결과 — 이미 있는 것과 없는 것

계획을 세우기 전에 코드와 DB를 직접 확인했다. **신규 테이블을 만들기 전에 기존 테이블 확장으로
해결되는지 먼저 봤다.**

### 이미 있어서 확장만 하면 되는 것 ✅

| 요구 | 기존 자산 | 판단 |
|---|---|---|
| Phase 2 월 마감·잠금 | `monthly_settlement` — `status`(DRAFT/CONFIRMED/LOCKED/REOPENED), `locked_at/by`, `confirmed_at/by`, `reopen_count`, `reopen_limit_override`, `last_reopen_reason`, `snapshot_id` | **신규 테이블 불필요.** `REVIEW` 상태와 검증 결과 저장만 추가 |
| Phase 5 감사 로그 | `audit_log` — `domain_table`, `domain_row_id`, `action`, `before_json`, `after_json`, `reason`, `request_id` + 헬퍼 `core/audit.py` + 조회 서비스 `audit_query.py` + 라우터 `/admin/audit` | **신규 테이블 불필요.** 복원 관련 3컬럼만 추가 |
| Phase 1 이익 단계 | `product_margin_expected`(예상) / `product_margin_actual`(실제) 2테이블 + `policy_snapshot` 3테이블 | **신규 테이블 불필요.** `sales_order` 에 상태 컬럼, `product_margin_actual` 에 단계별 금액 추가 |
| Phase 6 import | `marketplace_order_import`, `marketplace_settlement_import`, `batch_run`, `batch_run_log`, `import_file_hash` 컬럼 | 공통 `import_job` 으로 통합 필요 (현재는 도메인별로 흩어짐) |
| Phase 7 동기화 | `integration_account`, `integration_job`, `batch_run`, `batch_run_log` | `integration_job` 확장 |
| 정책 버전·유효기간 | 11개 정책 테이블 전부 `version`/`effective_from`/`effective_to`/`reliability_status`/`source_url` 보유 | 그대로 사용 |
| RBAC | `core/rbac.require_roles`, 6개 role, `CurrentUser.org_id` 로 전 쿼리 격리 | 그대로 사용 |
| 응답 envelope | `core/response.envelope` + 예외 핸들러 | 그대로 사용 |
| 테스트 | pytest — unit 5파일 / integration 7파일 | 그대로 확장 |

### 새로 만들어야 하는 것 ❌

| Phase | 신규 테이블 | 이유 |
|---|---|---|
| 3 | `approval_request` | 승인 워크플로가 전혀 없다 |
| 4 | `operation_issue` | 대시보드 경고가 계산 결과일 뿐 저장되지 않는다 |
| 6 | `import_job`, `import_job_row`, `import_mapping_profile` | 도메인별 import 가 흩어져 있고 매핑·롤백·실패행 개념이 없다 |
| 9-1 | `return_claim` | 반품 데이터가 없다 |
| 9-2 | `ad_spend` | 광고비가 `expenses` 안에만 있고 캠페인·ROAS 개념이 없다 |
| 9-4 | `inventory_snapshot` | 재고 없음 |

### 프론트엔드 mock → 실연결 대상 (42번 문서 §4 표 기준)

`productAdapter.detail/save/options` · `settlementAdapter` · `expenseAdapter` ·
`settingsAdapter.{categories,taxRules,feePolicies,members}` · `distributionAdapter` ·
`notificationAdapter` · `integrationAdapter` · `dashboardAdapter`

**전환 원칙**: 어댑터 함수 본문만 교체한다. 화면 코드는 건드리지 않는다.
`withFallback` 은 **401/403 을 그대로 던지도록 이미 되어 있다**(mock 이 인증 오류를 가리지 않음).
여기에 더해 `NEXT_PUBLIC_ALLOW_MOCK_FALLBACK` 플래그를 도입해
**운영 빌드에서는 fallback 자체를 끈다.**

---

## 1. 마이그레이션 목록 (M016 ~ M027)

현재 최신은 **M015**. 순서와 의존성을 먼저 확정한다.

| # | 이름 | 내용 | 위험 | 롤백 |
|---|---|---|---|---|
| **M016** | `profit_status_and_stages` | `sales_order`: `profit_status`, `profit_calculated_at`, `profit_finalized_at`, `return_window_ends_at`<br>`product_margin_actual`: `provisional_profit_amount`, `settled_profit_amount`, `final_profit_amount`, `calculation_snapshot_json`, `applied_policy_versions_json`<br>신규 enum `enum_profit_status` | 낮음 (NULL 허용 추가) | 컬럼 DROP |
| **M017** | `monthly_close_extension` | `monthly_settlement`: `close_state`(OPEN/REVIEW/CONFIRMED/LOCKED), `review_requested_at/by`, `validation_result_json`, `validated_at`<br>기존 `status` 는 **삭제하지 않고 유지** — 하위 호환 | 낮음 | 컬럼 DROP |
| **M018** | `approval_request` | 신규 테이블 + `enum_approval_status`, `enum_approval_action` | 낮음 (신규) | DROP TABLE |
| **M019** | `operation_issue` | 신규 테이블 + `enum_issue_status`, `enum_issue_severity`, `dedupe_key` UNIQUE 부분 인덱스 | 낮음 | DROP TABLE |
| **M020** | `audit_restore_support` | `audit_log`: `is_restorable`, `restored_from_id`, `approval_request_id`, `effective_from` | 낮음 | 컬럼 DROP |
| **M021** | `import_job` | `import_job`, `import_job_row`, `import_mapping_profile` 3테이블 | 낮음 | DROP TABLE |
| **M022** | `integration_job_extension` | `integration_job`: `job_kind`, `job_status`, `requested_range_from/to`, `read/created/updated/skipped/failed_count`, `retry_count`, `next_run_at`, `error_code`, `error_message_user`, `correlation_id`, `raw_payload_ref` | 낮음 | 컬럼 DROP |
| **M023** | `fee_policy_vat_flag` | `marketplace_fee_policy.fee_vat_included BOOLEAN NOT NULL DEFAULT false` + 기존 행 보정 SQL | **중간** — 기본값 선택이 계산에 영향 | 컬럼 DROP |
| **M024** | `tax_policy_extension` | `tax_policy`: `deemed_value_added_rate`, `input_credit_flat_rate`, `income_tax_effective_rate`, `verified_at`, `verified_by`, `output_vat_policy`, `input_vat_credit_policy`, `import_vat_credit_policy` | 중간 | 컬럼 DROP |
| **M025** | `member_profile_extension` | `app_user`: `job_title`, `department`, `phone`, `employee_no`, `joined_at`, `avatar_url`<br>신규 `user_assignment`(담당 판매처·카테고리) | 낮음 | 컬럼 DROP |
| **M026** | `distribution_rule` | `partner`: `participation_type`<br>신규 `distribution_rule` (participantType/calculationBase/threshold/cap/minimum/priority/effectiveFrom) — 기존 `partner_share_policy` 는 유지하고 신규 규칙과 병존 | 중간 | DROP TABLE + 컬럼 DROP |
| **M027** | `notification_channel_config` | `enum_notification_channel_code` 에 `DISCORD` 추가<br>신규 `notification_channel_config`(암호화 저장) + `notification_rule` | 낮음 | — enum 값은 되돌리지 않음(§1-2 참고) |

### 1-1. 마이그레이션 원칙 적용

- **기존 데이터 손실 금지** — 모든 신규 컬럼은 `NULL` 허용 또는 안전한 `DEFAULT` 로 추가한다.
- **기존 컬럼을 지우지 않는다** — `monthly_settlement.status` 는 `close_state` 와 병존시킨다.
  기존 코드와 정산 로직이 `status` 를 읽고 있어 한 번에 갈아타면 회귀가 난다.
  M017 에서 `close_state` 를 `status` 기준으로 backfill 하고, 이후 코드가 `close_state` 를 쓴다.
- **기존 파트너는 전부 `PARTNER`** — M026 backfill (`participation_type = 'PARTNER'`).
- **기존 월은 `status` 를 그대로 매핑** — `DRAFT→OPEN`, `CONFIRMED→CONFIRMED`, `LOCKED→LOCKED`,
  `REOPENED→OPEN`(REOPEN 은 감사 이벤트로 취급, 요구사항대로).
- **정책 snapshot 유지** — `policy_snapshot`/`policy_snapshot_ref`/`policy_snapshot_value` 3테이블은 건드리지 않는다.
  과거 확정 데이터는 재계산하지 않는다.

### 1-2. enum 처리 방침 (PostgreSQL 16.9 기준 — 실측 확인)

> ⚠️ **정정(2026-08-06)**: 이 문서의 초판에 "PostgreSQL 은 `ALTER TYPE ADD VALUE` 를
> 트랜잭션 안에서 못 쓴다" 고 적었는데 **틀렸다.** PostgreSQL 12 부터 가능하다.
> 아래는 이 프로젝트가 쓰는 **16.9 에서 직접 실행해 확인한 결과**다.

| # | 시도 | 결과 |
|---|---|---|
| 1 | 트랜잭션 안에서 `ALTER TYPE t ADD VALUE 'C'` | ✅ 성공 |
| 2 | **같은 트랜잭션에서** 그 새 값을 사용 (`SELECT 'C'::t`) | ❌ `UnsafeNewEnumValueUsage` |
| 3 | `ALTER TYPE t DROP VALUE 'B'` | ❌ 문법 자체가 없음 (`SyntaxError`) |

**그래서 TEXT + CHECK 를 쓴다.** 근거는 다음 네 가지다.

1. **추가한 값을 같은 마이그레이션에서 쓸 수 없다.**
   상태 컬럼을 추가하면서 기존 행을 backfill 해야 하는데, enum 이면
   "값 추가" 와 "그 값으로 UPDATE" 를 서로 다른 트랜잭션으로 쪼개야 한다.
   M016/M017 은 컬럼 추가 → backfill → NOT NULL → CHECK 를 한 트랜잭션에서 끝낸다.
2. **enum 값 제거가 불가능하다 → downgrade 가 원상복구되지 않는다.**
   CHECK 는 `DROP CONSTRAINT` 로 정확히 되돌아간다. 롤백 가능한 마이그레이션 요구를
   enum 으로는 만족할 수 없다.
3. **downgrade 복잡도.** enum 을 되돌리려면 그 타입을 참조하는 모든 컬럼을 재생성해야 한다.
   M004 의 주석이 그 위험을 이미 기록하고 있다(컬럼 6개 재생성 필요, 데이터 유실 위험).
4. **상태 정책이 바뀔 가능성이 높다.** `REVIEW` 를 넣을지, `REOPENED` 를 상태로 둘지는
   운영하면서 바뀐다. 실제로 이번에 `REOPENED` 를 상태에서 빼고 감사 이벤트로 돌렸다.
   자주 바뀌는 값 목록에 되돌릴 수 없는 타입을 쓰는 건 위험하다.

대상: `profit_status`, `close_state`, `approval_status`, `issue_status`, `job_status`.

예외: `enum_notification_channel_code` 에 `DISCORD` 추가는 기존 enum 확장이 불가피하다
(다른 코드가 이미 enum 을 참조). 위 2번 때문에 **값 추가와 사용을 별도 마이그레이션으로 나눈다.**
down 에서는 값을 되돌리지 않고 주석으로 남긴다.

### 1-3. M023 기본값 결정 (계산에 영향)

`fee_vat_included` 의 기본값을 잘못 잡으면 **모든 상품의 수수료가 10% 어긋난다.**

- 전역 기본값은 **`false`(VAT 별도)** 로 둔다. 대부분의 마켓이 별도 표기이고,
  틀렸을 때 마진이 **보수적으로(낮게)** 나오기 때문이다. 마진을 낙관적으로 보이게 하는 쪽으로
  틀리는 것보다 낫다.
- 스마트스토어는 요율(5.74%)이 이미 VAT 포함 관행이라 `true` 로 보정한다.
- **모든 행에 `reliability_status='NEEDS_REVIEW'` 를 세우지는 않는다** — 기존 CONFIRMED 정책의
  신뢰도를 임의로 떨어뜨리면 다른 경고가 오염된다. 대신 화면(수수료 정책)에서
  "근거 없음" 경고로 표시하고, 세무 확인 항목으로 42번 문서 §5-6 에 이미 기록되어 있다.

---

## 2. Phase 별 설계

### Phase 1. 이익 4단계

**상태 전이** (역방향 없음. 되돌리려면 월 잠금 해제가 선행)

```
EXPECTED ──주문 발생──> PROVISIONAL ──정산 매칭──> SETTLED ──반품기한+월마감──> FINAL
```

**데이터 모델** — 신규 테이블 없음.

```sql
-- sales_order
profit_status          TEXT NOT NULL DEFAULT 'PROVISIONAL'
  CHECK (profit_status IN ('EXPECTED','PROVISIONAL','SETTLED','FINAL'))
profit_calculated_at   TIMESTAMPTZ
profit_finalized_at    TIMESTAMPTZ
return_window_ends_at  DATE          -- 반품 가능 기한. FINAL 전환의 선행 조건

-- product_margin_actual  (이미 actual_* 컬럼 보유 → 단계별 금액만 추가)
provisional_profit_amount BIGINT
settled_profit_amount     BIGINT
final_profit_amount       BIGINT
calculation_snapshot_json JSONB   -- 계산 입력값 전체
applied_policy_versions_json JSONB -- {"tax":3,"marketplace_fee":2,...}
```

기존 `product_margin_expected.computed_margin_amount` 가 `EXPECTED` 값이다. 그대로 쓴다.

**FINAL 전환 조건** (전부 만족해야 함)
1. `return_window_ends_at < today`
2. 해당 월 `monthly_settlement.close_state = 'LOCKED'`
3. 정산 매칭 완료 (`settlement_record_id IS NOT NULL`)

**서비스**: `services/profit_status.py`
- `recalculate_provisional(sales_order_id)` — 주문 등록/수정 시
- `apply_settlement(sales_order_id, settlement_record_id)` — 정산 매칭 시
- `finalize_month(year_month)` — 월 잠금 시 일괄

**API**: `GET /api/v1/profit-status/{sales_order_id}` — 4단계 금액 + 차이 + 정책 버전 + 미확정 사유

**UI**: 판매장부 drawer 에 단계 배지 + 단계별 금액 + 예상 대비 차이.
대시보드 KPI 는 **예상이익과 확정이익을 별도 타일**로 분리한다(혼합 금지).

---

### Phase 2. 월 마감 및 잠금

**상태**: `OPEN → REVIEW → CONFIRMED → LOCKED`. `REOPENED` 는 상태가 아니라 감사 이벤트
(`audit_log.action='STATUS_CHANGE'` + `reason` 필수).

**검증 규칙** (`services/monthly_close.py::validate_month`)

| 코드 | 내용 | 등급 |
|---|---|---|
| `UNMATCHED_SETTLEMENT` | 미매칭 정산 | **blocking** |
| `SETTLEMENT_MISMATCH` | 정산 차이 > 임계값 | **blocking** |
| `SALE_WITHOUT_PRODUCT` | 상품 미연결 판매 | **blocking** |
| `ZERO_COST_SALE` | 원가 0원 판매 | **blocking** |
| `DISTRIBUTION_RATIO_INVALID` | 배분 비율 합 ≠ 100% | **blocking** |
| `UNAPPROVED_EXPENSE` | 승인 대기 비용 | warning |
| `MISSING_EVIDENCE` | 증빙 없는 비용 | warning |
| `UNVERIFIED_TAX_POLICY` | `verified_at IS NULL` | warning |
| `SYNC_FAILED` | API 동기화 실패 | warning |
| `OPEN_BLOCKING_ISSUE` | 미처리 blocking 운영 이슈 | **blocking** |

blocking 이 하나라도 있으면 `CONFIRMED` 로 못 넘어간다. warning 은 확인 체크 후 진행 가능.

**잠금 후 쓰기 차단** — 프론트가 아니라 **백엔드에서 강제**한다.
`core/month_lock.py::assert_month_writable(db, org_id, date)` 를
sales/expenses/settlement 매칭/배분 규칙의 모든 mutation 서비스 진입점에서 호출한다.
`SettlementLockedError` 는 이미 있다(`core/exceptions.py`).

**잠금 해제**: `ADMIN` 만. 사유·요청자·승인자·일시 필수 → `approval_request` 를 경유한다.

**API**: `GET/POST /api/v1/monthly-close/{ym}` (`/validate`, `/request-review`, `/confirm`, `/lock`, `/unlock`)

---

### Phase 3. 승인 워크플로

**단일 공통 테이블**로 간다. 엔티티별 테이블은 만들지 않는다.

```sql
CREATE TABLE approval_request (
  id UUID PK, org_id UUID NOT NULL,
  business_key TEXT NOT NULL,          -- APR-000001
  entity_type TEXT NOT NULL,           -- product/policy/expense/settlement/monthly_close/...
  entity_id UUID,
  action_type TEXT NOT NULL,           -- COST_CHANGE/PRICE_CHANGE/POLICY_CHANGE/...
  status TEXT NOT NULL DEFAULT 'PENDING'
    CHECK (status IN ('DRAFT','PENDING','APPROVED','REJECTED','CANCELLED','APPLIED')),
  requested_by UUID NOT NULL, requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  reviewed_by UUID, reviewed_at TIMESTAMPTZ,
  reason TEXT, review_comment TEXT,
  before_snapshot_json JSONB, after_snapshot_json JSONB,
  effective_from DATE,                 -- NULL = 승인 즉시 적용
  applied_at TIMESTAMPTZ,
  ...공통 감사 컬럼
);
```

**적용 시점 처리**
- `effective_from IS NULL` → 승인 즉시 `APPLIED`
- `effective_from > today` → `APPROVED` 상태로 대기, 스케줄러가 해당 일자에 적용
  (`services/scheduler.py` 가 이미 있다)

**자기 승인 차단**: org 설정 `settings_json.approval.self_approve_allowed` (기본 `false`).
`requested_by == reviewed_by` 이면 `ForbiddenError`.

**API**: `/api/v1/approvals` (목록/상세/생성/승인/반려/취소)

---

### Phase 4. 운영 이슈

**라우트 결정**: 사이드바에 독립 메뉴를 추가하지 않는다.
현재 IA 에서 가장 자연스러운 곳은 **알림·연동 그룹의 `운영 이슈`** 다 —
알림 규칙이 이슈를 만들어내는 관계라 같은 그룹에 있는 게 맞다.
대시보드 `확인이 필요한 항목` 은 이 목록의 필터된 뷰로 딥링크한다.

```sql
CREATE TABLE operation_issue (
  id UUID PK, org_id UUID NOT NULL, business_key TEXT NOT NULL,
  issue_type TEXT NOT NULL, severity TEXT NOT NULL CHECK (severity IN ('INFO','WARNING','CRITICAL')),
  entity_type TEXT, entity_id UUID,
  title TEXT NOT NULL, description TEXT,
  status TEXT NOT NULL DEFAULT 'OPEN'
    CHECK (status IN ('OPEN','IN_PROGRESS','WAITING','RESOLVED','IGNORED')),
  assigned_to UUID, due_date DATE,
  resolved_at TIMESTAMPTZ, resolution_note TEXT,
  source_rule TEXT, dedupe_key TEXT NOT NULL,
  first_detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_detected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  blocks_month_close BOOLEAN NOT NULL DEFAULT false,
  ...
);
-- 열려 있는 이슈에 대해서만 dedupe 유일성 — 해결된 이슈는 재발 가능해야 한다
CREATE UNIQUE INDEX ux_issue_open_dedupe ON operation_issue (org_id, dedupe_key)
  WHERE status IN ('OPEN','IN_PROGRESS','WAITING');
```

**중복 방지**: 감지기가 `dedupe_key` 로 UPSERT. 이미 열린 이슈면 `last_detected_at` 만 갱신한다.
`IGNORED` 는 `resolution_note` 필수.

---

### Phase 5. 감사 로그와 복원

기존 `audit_log` 에 4컬럼 추가:
`is_restorable BOOLEAN`, `restored_from_id UUID`, `approval_request_id UUID`, `effective_from DATE`

**복원 정책** — 화이트리스트 방식. 코드에 표로 고정한다.

```python
RESTORABLE = {
  ("product", "name_ko"), ("product", "memo"), ("product", "category_path"),
  ("product", "supply_url"), ("notification_rule", "*"), ("user_preference", "*"),
}
# 정책·금액은 복원 대신 "이전 값으로 새 버전 생성"
NEW_VERSION_ONLY = {"tax_policy", "marketplace_fee_policy", "distribution_rule", ...}
FORBIDDEN = {"sales_order(FINAL)", "monthly_settlement(LOCKED)", ...}
```

민감정보는 `before_json`/`after_json` 에서 마스킹한다 — `core/audit.py` 에 마스킹 필터를 추가한다
(`api_key`, `webhook_url`, `password`, `secret`, `token` 키 이름 매칭).

---

### Phase 6~9

지면상 요약. 상세는 구현 시 `44_ops_stability_worklog.md` 에 기록한다.

- **Phase 6 import**: `import_job`(파일 hash·매핑·상태) + `import_job_row`(행별 결과) +
  `import_mapping_profile`(판매처별 매핑 저장). 자연 키는 도메인별로 명시
  (판매 = 판매처+주문번호+주문상품번호 / 정산 = 판매처+정산번호 / 상품 = SKU /
  비용은 자동 확정하지 않고 **중복 후보로만 표시**).
- **Phase 7 동기화**: `integration_job` 확장. 지원하지 않는 동기화 종류는 비활성 + 사유 표시.
  raw payload 는 개인정보를 최소화해 보존하고 재처리 가능하게 한다.
- **Phase 8 테스트**: 백엔드 pytest 확장(이미 12개 파일 존재). 프론트는 **Vitest** 도입
  (Jest 는 Next 15 + React 19 RC 조합에서 설정 비용이 크고, Vitest 가 기존 Vite 계열 도구와 충돌이 적다).
  E2E(Playwright)는 CI 환경이 없어 **시나리오 정의까지만** 하고 실행은 후속으로 남긴다.
- **Phase 9**: 데이터 모델 + 어댑터 + 라우트 스텁 + TODO 만 남긴다.

---

## 3. API 설계

기존 `/api/v1` + envelope 규칙을 그대로 따른다. 목록 API 는 전부
`page/page_size` + `limit/offset` 양쪽 수용, `id` tie-breaker 정렬, org 스코프, 권한 필터.

| 경로 | 메서드 | 권한 |
|---|---|---|
| `/profit-status/{sales_order_id}` | GET | ADMIN/OPERATOR/ACCOUNTING |
| `/monthly-close` | GET (목록) | ADMIN/OPERATOR/ACCOUNTING/READONLY |
| `/monthly-close/{ym}` | GET | 동일 |
| `/monthly-close/{ym}/validate` | POST | ADMIN/OPERATOR/ACCOUNTING |
| `/monthly-close/{ym}/request-review` | POST | ADMIN/OPERATOR/ACCOUNTING |
| `/monthly-close/{ym}/confirm` | POST | ADMIN/ACCOUNTING |
| `/monthly-close/{ym}/lock` | POST | ADMIN |
| `/monthly-close/{ym}/unlock` | POST | ADMIN (+ 사유 필수) |
| `/approvals` | GET/POST | 전 역할(본인 요청) / 승인은 ADMIN·ACCOUNTING |
| `/approvals/{id}/approve` `/reject` `/cancel` | POST | 위와 동일 |
| `/operation-issues` | GET | ADMIN/OPERATOR/ACCOUNTING/SOURCING |
| `/operation-issues/{id}` | PATCH | 동일 |
| `/operation-issues/detect` | POST | ADMIN (수동 재감지) |
| `/audit-logs` | GET | ADMIN (기존 `/admin/audit` 유지, alias 추가) |
| `/audit-logs/{id}/restore` | POST | ADMIN |
| `/import-jobs` | GET/POST | ADMIN/OPERATOR |
| `/import-jobs/{id}/rollback` | POST | ADMIN |
| `/integration-jobs` | GET | ADMIN/OPERATOR |
| `/integration-jobs/{id}/retry` | POST | ADMIN/OPERATOR |

---

## 4. 수용 기준 (요약)

각 Phase 의 상세 수용 기준은 요구사항 원문을 그대로 따른다. 검증 방법만 여기 명시한다.

| Phase | 검증 |
|---|---|
| 1 | pytest: 주문→정산매칭→월잠금 시나리오에서 4단계 금액이 각각 산출되고 FINAL 전제조건이 지켜지는지 |
| 2 | pytest: blocking 검증이 CONFIRMED 를 막는지 / LOCKED 월의 sales·expense mutation 이 403 인지 |
| 3 | pytest: 승인 전 원본 미변경 / `effective_from` 미래일 때 미적용 / 자기승인 차단 |
| 4 | pytest: 같은 dedupe_key 재감지 시 행이 늘지 않고 `last_detected_at` 만 갱신되는지 |
| 5 | pytest: 복원 화이트리스트 밖 요청이 거부되는지 / 감사 로그에 secret 원문이 없는지 |
| 6 | pytest: 같은 파일 hash 재업로드 차단 / 롤백 후 원상복구 |
| 7 | pytest: 동일 기간 재동기화 시 중복 미생성 |
| 8 | 계산 18케이스 + 배분 12케이스 + 정산 8케이스 + 권한 6케이스 |

---

## 5. 우선순위와 진행 순서

요구사항의 구현 순서를 따르되, **백엔드 기반이 없으면 프론트가 mock 을 못 벗어나므로**
migration → service → router → adapter 순으로 수직 완성한다.

| 우선 | 항목 | 근거 |
|---|---|---|
| **P0** | M016·M017 + Phase 1·2 백엔드 | 이익 단계와 월 잠금이 다른 모든 Phase 의 전제. 잠금이 없으면 승인·감사가 의미 없다 |
| **P1** | M018 + Phase 3 승인 | 잠금 해제가 승인을 경유하므로 Phase 2 직후 |
| **P2** | M019 + Phase 4 이슈 | 월 마감 검증이 이슈를 만들어낸다 |
| **P3** | M020 + Phase 5 감사·복원 | 위 3개가 만든 변경을 기록 |
| **P4** | M023·M024·M025·M026·M027 + 어댑터 실연결 | mock 제거 |
| **P5** | M021 + Phase 6 import | 독립적. 뒤로 미뤄도 안전 |
| **P6** | M022 + Phase 7 동기화 모니터 | 외부 계정이 없어 실검증 불가 |
| **P7** | Phase 8 테스트 | 각 Phase 와 함께 작성하되 전체 스위트는 마지막에 |
| **P8** | Phase 9 2차 기능 | 모델 + 스텁 + TODO |

---

## 6. 코드만으로 확정할 수 없어 설정값·`검증 필요` 로 남기는 것

임의로 확정하지 않는다. 전부 정책 테이블 또는 org 설정으로 두고 화면에 경고를 띄운다.

1. 간이과세 업종별 부가가치율 / 매입세액 공제율 / 적용 기준금액 / 납부의무 면제 기준
2. 소득세 실효세율
3. **마켓별 수수료 VAT 포함 여부** (M023 — 잘못되면 마진 10% 오차)
4. 픽썸의 현재 과세 유형 (일반/간이)
5. 반품 가능 기간 (마켓·카테고리별로 다름) → org 설정 기본 15일, 상품별 override
6. 정산 불일치 허용 오차 → org 설정 기본 50,000원
7. 외부 API 자격증명 (쿠팡 Wing / 네이버 커머스 / Slack / Discord)
8. 파일 스토리지 위치 (`STORAGE_ROOT` 환경변수)

---

## 7. 이번 작업에서 하지 않는 것

- 기존 화면 재디자인 (개편 완료분 유지)
- 화면 안에 새 계산 함수 작성 — `lib/calc/engine.ts`, `lib/calc/distribution.ts` 가 단일 진실
- 과거 확정 데이터의 현재 정책 재계산
- 정산 미매칭 건의 자동 추측 매칭
- Playwright E2E 실행 (시나리오 정의까지)

---

## 8. M018 승인 워크플로 — 상태 머신과 불변조건 (구현 전 설계)

> P0.5 검증 완료 후 착수. **아래 "미결정" 항목은 임의로 확정하지 않고 설정값으로 둔다.**

### 8-1. 상태 머신

```
        ┌──────────────── cancel ─────────────┐
        ▼                                     │
     DRAFT ──submit──> PENDING ──approve──> APPROVED ──apply──> APPLIED
                          │                     │
                          ├──reject──> REJECTED │
                          └──cancel──> CANCELLED└──cancel(적용 전)──> CANCELLED
```

허용 전이만 표로 고정한다. 여기 없는 전이는 409.

| from | to | 조건 |
|---|---|---|
| `DRAFT` | `PENDING`, `CANCELLED` | 요청자 본인 |
| `PENDING` | `APPROVED`, `REJECTED`, `CANCELLED` | 승인 권한 / 취소는 요청자 |
| `APPROVED` | `APPLIED`, `CANCELLED` | 적용은 시스템, 취소는 **적용 전에만** |
| `REJECTED` / `CANCELLED` / `APPLIED` | — | 종착 상태. 다시 하려면 새 요청 |

### 8-2. 불변조건 (invariants)

| # | 불변조건 | 위반 시 |
|---|---|---|
| I-1 | `PENDING` 인 동안 대상 데이터는 변경되지 않는다 | 승인이 무의미해짐 |
| I-2 | 같은 (entity_type, entity_id, action_type) 에 대해 **열린 요청은 최대 1건** | 부분 인덱스로 강제 |
| I-3 | 요청 생성 시 `before_snapshot_json` 을 박제한다 | 적용 시점에 대상이 바뀌었는지 비교할 근거 |
| I-4 | 적용 시점에 대상의 현재 값 ≠ `before_snapshot` 이면 **적용하지 않고 CONFLICT** | 덮어쓰기 사고 방지 |
| I-5 | 잠긴 월(`LOCKED`)에 속한 데이터는 승인해도 적용되지 않는다 | 잠금이 최우선 |
| I-6 | 실패한 전이는 감사 로그를 남기지 않는다 | 허위 기록 방지 |
| I-7 | 모든 전이는 `audit_log` 1행 (before/after + reason) | 추적성 |
| I-8 | 상태 전이는 행 잠금 + CAS 로 직렬화한다 | Phase 2 와 동일 패턴 |

### 8-3. 결정한 것

| 항목 | 결정 | 근거 |
|---|---|---|
| 자기 승인 | **기본 금지.** `organization.settings_json.approval.self_approve_allowed` 로 허용 가능 | 1인 사업자는 자기 승인 외에 방법이 없다. 그래서 금지를 기본으로 두되 끌 수 있게 한다 |
| 승인 권한 | `ADMIN`(전부) / `ACCOUNTING`(비용·정책·정산) / `OPERATOR`(상품 원가·판매가) | 기존 RBAC 6종을 그대로 쓴다. 새 역할을 만들지 않는다 |
| 적용일 | `effective_from IS NULL` = 승인 즉시, 미래일 = 스케줄러가 그날 적용 | `services/scheduler.py` 기존 자산 활용 |
| 중복 요청 | 부분 UNIQUE 인덱스로 **DB 레벨 차단** (`WHERE status IN ('DRAFT','PENDING','APPROVED')`) | 애플리케이션 체크만으로는 동시 요청을 못 막는다 |
| 대상 변경 감지 | `before_snapshot_json` 과 현재 값 비교 (낙관적 잠금) | 별도 version 컬럼을 모든 테이블에 추가하는 것보다 침습적이지 않다 |
| 취소 가능 시점 | `APPLIED` 전까지. 적용 후에는 **되돌리기가 아니라 새 요청** | 적용된 값을 취소로 되돌리면 감사 추적이 끊긴다 |
| 멱등성 | `Idempotency-Key` 헤더 → `approval_request.idempotency_key` UNIQUE | 재시도로 요청이 두 건 생기는 것 방지 |
| 잠긴 월 | 승인은 되지만 **적용 시점에 `assert_period_writable` 로 차단** → `APPROVED` 상태로 남음 | 잠금 해제 후 자동 적용되게 |

### 8-4. 미결정 — 설정값 또는 unresolved decision

코드만으로 확정할 수 없다. **기본값을 두되 "미확정" 으로 표시한다.**

| 항목 | 기본값 | 왜 확정할 수 없나 |
|---|---|---|
| 승인 필요 기준 — 원가 변동 | `settings_json.approval.cost_change_threshold_rate` = 0.10 | 업종·마진 구조에 따라 다르다. 10% 는 출발점일 뿐 |
| 승인 필요 기준 — 판매가 변동 | `price_change_threshold_rate` = 0.05 | 위와 동일 |
| 승인 필요 기준 — 저마진 등록 | `min_margin_rate_for_auto` = 0.10 | 픽썸의 목표 마진 구조를 모른다 |
| 승인 필요 기준 — 고액 비용 | `expense_approval_threshold` = 500,000원 | 사업 규모에 따라 다르다 |
| 승인자 지정 방식 | 역할 기반 (지정 승인자 없음) | 조직이 2명인지 10명인지에 따라 갈린다 |
| 승인 SLA / 에스컬레이션 | 없음 | 운영 데이터가 쌓여야 판단 가능 |

### 8-5. 테이블 (M018)

`43 §2 Phase 3` 의 DDL 에 다음을 추가한다.

```sql
idempotency_key TEXT,             -- 재시도 방어
applied_by      UUID,             -- 적용 주체(시스템이면 NULL)
apply_error     TEXT,             -- 적용 실패 사유

-- I-2: 같은 대상에 열린 요청은 1건만
CREATE UNIQUE INDEX ux_approval_open_target
  ON approval_request (org_id, entity_type, entity_id, action_type)
  WHERE status IN ('DRAFT','PENDING','APPROVED');

-- 멱등성
CREATE UNIQUE INDEX ux_approval_idempotency
  ON approval_request (org_id, idempotency_key)
  WHERE idempotency_key IS NOT NULL;
```

---

## 9. M018 구현 결과 (2026-08-06) — 설계 대비 실제

### 9-1. 실제 schema

`approval_request` 단일 테이블. 상태는 **TEXT + CHECK** (`ck_approval_status`).

핵심 컬럼: `target_type` / `target_id` / `request_type` / `status` /
`requested_by·at` / `submitted_at` / `reviewed_by·at` / `applied_by·at` / `cancelled_at` /
`reason` / `review_comment` / `apply_error` /
`before_snapshot_json` / `proposed_changes_json` / `approval_policy_snapshot_json` /
`base_version` / `effective_from` / `version` / `idempotency_key`

**JSONB 3개는 구조를 컬럼 COMMENT 에 고정했다.** 임의 payload 저장소가 아니다.
- `before_snapshot_json` — 어댑터 `build_snapshot()` 결과. 허용 필드만. 민감정보는 어댑터가 마스킹.
- `proposed_changes_json` — before 와 **같은 키 집합의 부분집합**. 어댑터가 키를 검증.
- `approval_policy_snapshot_json` — `self_approve_allowed`, `required_roles`,
  `thresholds`, `thresholds_resolved`, `policy_source`

인덱스 8개 + PK. 조회용 5개(`org+status`, `org+target`, `requested_by`, `requested_at`,
PENDING 부분 인덱스) + 유일성 3개(`open_target` 부분, `idempotency` 부분, `business_key`).

**전이 규칙을 DB CHECK 로 구현하지 않았다.** DB 에는 되돌릴 수 없는 사실만 건다
(상태 도메인, 유일성, FK). 전이표는 `services/approval._ALLOWED` 가 검증한다 —
CHECK 로 흉내 내면 규칙이 두 곳에 흩어지고 정책이 바뀔 때마다 마이그레이션을 새로 파야 한다.

### 9-2. 구현한 상태 전이

`DRAFT→PENDING·CANCELLED` / `PENDING→APPROVED·REJECTED·CANCELLED` /
`APPROVED→APPLIED·CANCELLED` / `REJECTED→CANCELLED` / `APPLIED·CANCELLED` = 종착

모든 전이: 조직 격리 · 현재 상태 검증 · `FOR UPDATE` + `version` CAS ·
권한 검증 · 사유 검증 · 멱등성 · 감사 로그 · SAVEPOINT 격리.

### 9-3. 지원 대상 (target adapter)

`product` 하나만 등록했다. 지원 필드:
`name_ko, cost_amount, cost_currency, cost_fx_rate, cost_amount_foreign,
category_path, supply_url, memo, status`
요청 유형: `COST_CHANGE`, `PRODUCT_INFO_CHANGE`, `STATUS_CHANGE`

> **`sale_price` 는 뺐다.** `product` 테이블에 판매가 컬럼이 없다(판매가는 판매 시점
> 값이라 `sales_order` 에 있고, 상품 단위 정가 개념이 아직 스키마에 없다).
> 없는 컬럼을 승인 대상으로 열어두면 적용 단계에서 조용히 실패한다.
> **판매가 승인은 스키마 확장이 선행돼야 한다 — 미해결 항목.**

미지원 target_type 은 422 + `supported` 목록을 함께 반환한다.

### 9-4. 권한

| 동작 | 역할 |
|---|---|
| 생성·수정(DRAFT)·제출·취소 | ADMIN / OPERATOR / ACCOUNTING / SOURCING (본인 요청) |
| 승인·반려·적용 | target_type 별 — `product` 는 ADMIN / OPERATOR |
| 전체 조회 | 전 역할 (조직 스코프) |

**권한은 요청 시점이 아니라 승인 시점의 것을 본다.** 권한이 회수된 사람이
예전 정책으로 승인하면 안 된다. `approval_policy_snapshot_json` 은
"그때 무슨 규칙이었나" 를 남기는 용도지 판정 근거가 아니다.

### 9-5. 확정된 설정값

`organization.settings_json.approval.self_approve_allowed` — **기본 `false`**.
1인 사업자는 자기 승인 외에 방법이 없으므로 설정으로 열 수 있게 했다.
하드코딩된 영구 정책이 아니다.

### 9-6. 여전히 미결정 (unresolved configuration)

`load_approval_policy()` 가 값을 읽되 **기본값을 정답으로 취급하지 않는다.**
`thresholds_resolved: false` 로 미확정임을 응답에 표시한다.

- `cost_change_threshold_rate` — 원가 몇 % 변동부터 승인 필요한가
- `price_change_threshold_rate`
- `min_margin_rate_for_auto` — 저마진 등록 기준
- `expense_approval_threshold` — 고액 비용 기준
- 승인자 지정 방식 (현재 역할 기반, 지정 승인자 없음)
- 승인 SLA / 에스컬레이션

**"승인 필요 여부 자동 판정" 은 구현하지 않았다.** 위 임계값이 미확정이라
지금은 사용자가 명시적으로 승인 요청을 만든다. 임계값이 정해지면
`create_request` 앞단에 판정 훅을 붙인다.

### 9-7. 적용 실패 처리

`status` 는 `APPROVED` 로 유지하고 `apply_error` 에 사유를 기록한다.
**실패를 별도 상태로 만들지 않았다** — 승인이라는 사실은 사라지면 안 되고,
실패는 상태가 아니라 사건이다. (§8-3 결정 그대로)

### 9-8. API

| 경로 | 메서드 | 권한 |
|---|---|---|
| `/api/v1/approvals/target-types` | GET | 전 역할 — 화면이 대상 목록을 하드코딩하지 않게 |
| `/api/v1/approvals` | GET | 전 역할. 필터: status·target_type·target_id·request_type·requested_by·reviewed_by·mine·pending_for_me·q·from·to |
| `/api/v1/approvals` | POST | 생성. `Idempotency-Key` 헤더 지원 |
| `/api/v1/approvals/{id}` | GET / PATCH | 조회 / DRAFT 수정 |
| `/api/v1/approvals/{id}/submit` | POST | 본인만 |
| `/api/v1/approvals/{id}/approve` `/reject` | POST | 승인 권한 + 자기승인 정책 |
| `/api/v1/approvals/{id}/cancel` | POST | 본인 또는 ADMIN |
| `/api/v1/approvals/{id}/apply` | POST | 승인 권한. 적용 시점 재검증 |

HTTP: 403 권한 / 404 없음·타 조직 / 409 전이·stale·중복 대상 / 422 검증·미지원 대상.
멱등 재요청은 **기존 요청을 그대로 반환**한다(409 아님).

### 9-9. 수용 기준 달성 여부

| 기준 | 결과 |
|---|---|
| 중요한 변경을 승인 요청으로 보낼 수 있다 | 충족 (product 대상) |
| 승인 전에는 운영값이 변경되지 않는다 | 충족 — 테스트로 고정 |
| 승인 시 지정된 적용일에 반영된다 | **부분** — `effective_from` 저장·월잠금 판정에 사용. **미래일 자동 적용 스케줄러는 미구현** |
| 반려 사유가 요청자에게 표시된다 | 충족 (`review_comment`) |
| 변경 전후 값이 감사 로그에 남는다 | 충족 |
| 본인 요청 자기 승인 차단 설정 가능 | 충족 |

---

## 10. 마이그레이션 번호 변경 (2026-08-06)

M018 안정화가 Phase 4 보다 먼저 필요해 **M019 를 `approval_apply_tracking`** 으로 썼다.
초안의 `operation_issue`(Phase 4)는 **M020 이후**로 밀린다.

| 초안 | 실제 |
|---|---|
| M019 operation_issue | → M020 이후 |
| — | **M019 approval_apply_tracking** (fingerprint + 자동 적용 추적) |

자세한 내용은 `44_ops_stability_worklog.md` 의 "M018 안정화 + M019 자동 적용기" 절.
판매가 스키마 설계는 `46_sale_price_schema_design.md`.
