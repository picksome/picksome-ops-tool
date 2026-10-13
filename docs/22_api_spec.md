# 22. Backend API Spec — PICKSOME OPS TOOL

> 작성: Backend
> 작성일: 2026-06-17
> 입력: `20_prd.md`, `21_erd.md`, `_schema_design.md`, `00_project_overview.md`
> 다음 역할: Designer / Frontend (API 연동), DevOps (실행환경)

---

## 0. Reframed Request

PRD 의 User Story 와 DBA ERD 를 입력으로 받아,
**FastAPI 라우터·서비스 계층 구조, REST endpoint, 핵심 계산 함수, import/export 명세, 테스트 시나리오**를 정의한다.
코드는 작성하지 않는다 (구조·인터페이스만).

**2026-06-18 갱신**: Open Items 7건 Confirmed (D-280~D-286) 반영.
- ID 모두 UUID. URL/응답은 UUID 사용. 사용자 노출 ID 는 `business_key` 별도 표시.
- organization 컨텍스트 강제 (JWT 에 org_id 클레임).
- 옵션 API 신설 (`/products/{id}/options`, `/products/{id}/variants`).
- 정산 REOPEN 에 횟수 제한 검증 + `REOPEN_LIMIT_EXCEEDED` 에러코드.
- 알림 채널 어댑터 인터페이스 (MVP: EMAIL + IN_APP).
- EXTERNAL_ACCOUNTANT 라우트 비활성 (`feature flag`).

---

## 1. Backend Architecture

### 1-1. 디렉토리 구조

```
backend/
├── app/
│   ├── main.py                # FastAPI app 진입
│   ├── config.py              # pydantic-settings (env)
│   ├── deps.py                # 공통 의존성 (DB session, current_user, RBAC)
│   ├── core/
│   │   ├── security.py        # JWT, password hash, RBAC decorator
│   │   ├── audit.py           # audit_log 기록 헬퍼
│   │   ├── timezone.py        # KST 변환
│   │   ├── money.py           # 원 단위 BIGINT helper
│   │   ├── policy_snapshot.py # 정책 snapshot 박제 헬퍼
│   │   └── exceptions.py      # 공통 예외
│   ├── api/                   # 라우터 (얇게)
│   │   ├── v1/
│   │   │   ├── auth.py
│   │   │   ├── products.py
│   │   │   ├── sourcing.py
│   │   │   ├── margin.py
│   │   │   ├── sales.py
│   │   │   ├── expenses.py
│   │   │   ├── settlements.py
│   │   │   ├── policies.py
│   │   │   ├── reports.py
│   │   │   ├── dashboard.py
│   │   │   ├── admin_users.py
│   │   │   ├── admin_audit.py
│   │   │   ├── notifications.py
│   │   │   └── vault.py
│   ├── services/              # 도메인 로직
│   │   ├── policy_service.py
│   │   ├── margin_service.py
│   │   ├── sales_service.py
│   │   ├── expense_service.py
│   │   ├── settlement_service.py
│   │   ├── report_service.py
│   │   ├── dashboard_service.py
│   │   ├── user_service.py
│   │   └── notification_service.py
│   ├── repositories/          # ORM 접근
│   ├── domain/                # Pydantic 모델 (입력/출력)
│   │   ├── policy.py
│   │   ├── margin.py
│   │   ├── settlement.py
│   │   └── ...
│   ├── db/
│   │   ├── base.py
│   │   ├── session.py
│   │   ├── models/            # SQLAlchemy ORM
│   │   └── migrations/        # alembic
│   ├── io/
│   │   ├── excel_export/      # xlsxwriter 템플릿
│   │   └── excel_import/      # openpyxl 파서
│   └── tests/
│       ├── unit/
│       ├── integration/
│       └── fixtures/
└── pyproject.toml
```

### 1-2. 계층 책임
- **api/**: 입출력 검증, 권한 체크, 서비스 호출만. 비즈니스 로직 금지.
- **services/**: 트랜잭션 경계. 정책 snapshot 박제, audit_log 기록.
- **repositories/**: SQL/ORM 접근만.
- **domain/**: Pydantic 입출력 모델 (snake_case ↔ camelCase 변환).

### 1-3. 횡단 정책
- 모든 endpoint: `Depends(get_current_user)` + RBAC decorator (`@require_role(...)`)
- **모든 쿼리에 `org_id` 강제 주입** (JWT `org_id` claim → service layer scoping)
- 모든 mutation: 단일 트랜잭션 안에서 도메인 변경 + audit_log + (해당 시) snapshot 생성
- 응답: `{ "data": ..., "meta": {...} }` 표준 wrapper
- 에러: `{ "error": { "code": "...", "message": "...", "details": [...] } }` + HTTP 상태코드
- 페이지네이션: cursor 또는 offset (`?limit=50&cursor=...`)
- timezone: 응답 timestamp 는 ISO8601 + UTC. FE 에서 KST 변환.
- 금액: 응답 JSON 에서 정수(원)로 직렬화
- **ID**: 경로/쿼리에 UUID. 응답 JSON 에 `id`(UUID) + `business_key` (사용자 노출용) 모두 포함. URL 에 순차 ID 노출 금지.
- **기능 플래그** (env): `FEATURE_EXTERNAL_ACCOUNTANT=false` (D-282), `FEATURE_NOTIFICATION_KAKAO=false` (D-281) 등

---

## 2. 인증 & RBAC

### 2-1. 인증
- `POST /api/v1/auth/login` → JWT (access 8h, refresh 30d)
- `POST /api/v1/auth/refresh`
- `POST /api/v1/auth/logout`
- `POST /api/v1/auth/invite/accept` (가입 토큰 + 비밀번호 설정)

### 2-2. 권한 매트릭스 (요약)

| 도메인 | ADMIN | OPERATOR | SOURCING | ACCOUNTING | READONLY | EXTERNAL_ACCOUNTANT |
|---|---|---|---|---|---|---|
| 상품 R | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| 상품 W | ✅ | ✅ | ✅ (CANDIDATE 만) | ❌ | ❌ | ❌ |
| 마진계산기 | ✅ | ✅ | ✅ | ✅ | R | ❌ |
| 판매장부 R | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ |
| 판매장부 W | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| 사업비용 R | ✅ | ❌ | ❌ | ✅ | ✅ | ✅ |
| 사업비용 W | ✅ | ❌ | ❌ | ✅ | ❌ | 코멘트/증빙상태만 |
| 정산 R | ✅ | R | ❌ | ✅ | ✅ | ✅ |
| 정산 W (DRAFT) | ✅ | ❌ | ❌ | ✅ | ❌ | ❌ |
| 정산 CONFIRM/LOCK/REOPEN | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 정책 R | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| 정책 W | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| 리포트 download | ✅ | ✅ | ❌ | ✅ | R | ✅ |
| 사용자/감사로그 | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Vault | ✅ | R (제한) | ❌ | ❌ | ❌ | ❌ |

> **D-282**: `EXTERNAL_ACCOUNTANT` 라우트는 MVP 에서 `FEATURE_EXTERNAL_ACCOUNTANT` 플래그로 비활성. enum 정의·DB 컬럼은 유지하되 라우트 가드에서 403 반환. Post-MVP 에 활성화.

---

## 3. REST Endpoints (PRD User Story 매핑)

> 경로 prefix: `/api/v1`. 모든 응답은 표준 wrapper. 페이지네이션 파라미터 `limit`, `cursor` 생략 표기.

### 3-1. 상품 (US-101, US-102)

| Method | Path | Story | Role | 비고 |
|---|---|---|---|---|
| GET | `/products` | US-104 list | * (R) | 필터: status, margin_type, q |
| POST | `/products` | US-101 승격 | ADMIN/OPERATOR | |
| GET | `/products/{id}` | US-104 detail | * (R) | |
| PATCH | `/products/{id}` | | ADMIN/OPERATOR | |
| DELETE | `/products/{id}` | | ADMIN | soft |
| GET | `/products/{id}/margins/expected` | US-104 | * (R) | 시계열 |
| GET | `/products/{id}/margins/actual` | US-104 | * (R) | 시계열 |
| GET | `/products/{id}/margins/compare` | US-104 | * (R) | 예상/실제 + delta |

#### 3-1-1. 상품 옵션 / Variants (D-286)

| Method | Path | 비고 |
|---|---|---|
| GET | `/products/{id}/option-groups` | 옵션 그룹 + 값 트리 |
| POST | `/products/{id}/option-groups` | 새 옵션 그룹 ("색상" 등) |
| PATCH | `/products/{id}/option-groups/{gid}` | 이름·순서 변경 |
| DELETE | `/products/{id}/option-groups/{gid}` | variants 존재 시 차단 |
| POST | `/products/{id}/option-groups/{gid}/values` | 옵션 값 추가 ("빨강") |
| PATCH | `/products/{id}/option-groups/{gid}/values/{vid}` | |
| DELETE | `/products/{id}/option-groups/{gid}/values/{vid}` | variants 존재 시 차단 |
| GET | `/products/{id}/variants` | variant 목록 (재고/가격) |
| POST | `/products/{id}/variants` | 수동 1건 추가 |
| POST | `/products/{id}/variants/bulk-generate` | 옵션 조합 자동 생성 (Post-MVP) |
| PATCH | `/products/{id}/variants/{vid}` | cost/sale_price/extra_price/stock 수정 |
| DELETE | `/products/{id}/variants/{vid}` | sales_order 매칭 시 차단 |

### 3-2. 소싱 후보 (US-101)

| Method | Path | 비고 |
|---|---|---|
| GET | `/sourcing-candidates` | 필터: candidate_status |
| POST | `/sourcing-candidates` | SOURCING 가능 |
| PATCH | `/sourcing-candidates/{id}` | 본인+ADMIN |
| POST | `/sourcing-candidates/{id}/approve` | ADMIN — product 생성 |
| POST | `/sourcing-candidates/{id}/reject` | ADMIN — reason 필수 |

### 3-3. 마진계산 (US-102, US-103)

| Method | Path | 비고 |
|---|---|---|
| POST | `/margin/preview` | 휘발성 계산 (저장 X). 입력: margin_type, marketplace, cost, sale_price, extras → 결과 + 적용된 정책 메타 |
| POST | `/margin/expected` | 저장. snapshot 생성 |
| POST | `/margin/simulate-price` | US-103 등록가 시뮬레이션. 목표마진별 추천가 |
| POST | `/margin/actual/recalculate` | 정산 import 후 재계산 트리거 |

### 3-4. 판매 (US-201, US-202)

| Method | Path | 비고 |
|---|---|---|
| GET | `/sales` | 필터: 기간/마켓/상품/주문번호 |
| POST | `/sales` | 단건 |
| PATCH | `/sales/{id}` | 인라인 수정 (LOCKED 정산 연결 시 차단) |
| DELETE | `/sales/{id}` | soft |
| POST | `/sales/bulk` | 일괄 등록 (배열) |
| PATCH | `/sales/bulk` | 일괄 수정 (필드별 동일값 적용) |
| POST | `/sales/import/preview` | CSV 업로드 → 미리보기 |
| POST | `/sales/import/commit` | 미리보기 후 확정 |
| GET | `/sales/import/templates/{marketplace}` | CSV 템플릿 다운로드 |

### 3-5. 쿠팡/마켓 정산 import (US-202)

| Method | Path | 비고 |
|---|---|---|
| POST | `/marketplace-settlements/import/preview` | CSV → parse + 매칭 미리보기 |
| POST | `/marketplace-settlements/import/commit` | 확정 + 미매칭 sales 자동/수동 매칭 |
| GET | `/marketplace-settlements` | 필터: 기간, 마켓 |
| PATCH | `/marketplace-settlements/{id}/match` | 수동 매칭 |

### 3-6. 사업비용 (US-301, US-302)

| Method | Path | 비고 |
|---|---|---|
| GET | `/expenses` | 필터: 기간/카테고리/증빙상태/사적-사업/결제자 |
| POST | `/expenses` | D-284: body 에 `vat_inclusion_type`. INCLUDED 시 `gross_amount` → 1/(1+vat_rate) 로 자동 supply/vat 분리 (`is_manual_tax_override=false`). EXCLUDED 시 사용자가 supply/vat 직접 입력. EXEMPT 시 vat=0. 카테고리/tax_policy 기본값 자동 채움 |
| PATCH | `/expenses/{id}` | LOCKED 정산 연결 시 차단. `vat_inclusion_type` 변경 시 재계산 |
| DELETE | `/expenses/{id}` | |
| POST | `/expenses/bulk` | 카드명세 paste 일괄 |
| PATCH | `/expenses/bulk` | 일괄 수정 |
| GET | `/expenses/evidence-status-counts` | 단계별 카운트 (배지용) |
| PATCH | `/expenses/{id}/evidence-status` | EXTERNAL_ACCOUNTANT 전용 라우트 (전달완료) |
| GET | `/expenses/{id}/comments` | |
| POST | `/expenses/{id}/comments` | EXTERNAL_ACCOUNTANT + ACCOUNTING |

### 3-7. 정산 (US-203~205)

| Method | Path | 비고 |
|---|---|---|
| GET | `/settlements/monthly` | 목록 (연도 필터) |
| GET | `/settlements/monthly/{year_month}` | 상세 |
| POST | `/settlements/monthly/{year_month}/draft` | DRAFT 생성 또는 재계산 |
| POST | `/settlements/monthly/{year_month}/confirm` | DRAFT→CONFIRMED |
| POST | `/settlements/monthly/{year_month}/lock` | CONFIRMED→LOCKED |
| POST | `/settlements/monthly/{year_month}/reopen` | LOCKED→REOPENED (reason 필수). **D-285**: reopen_count >= reopen_limit 이면 `REOPEN_LIMIT_EXCEEDED` 409. ADMIN 전용. |
| GET | `/settlements/monthly/{year_month}/reopen-info` | reopen_count, reopen_limit, 남은 횟수, ADMIN UI 표시 |
| GET | `/settlements/monthly/{year_month}/reconcile` | 매입-매출 대사 |
| GET | `/settlements/monthly/{year_month}/partners` | 파트너별 결과 |
| PATCH | `/settlements/monthly/{year_month}/partners` | 비율·우선분배 override (DRAFT 만) |
| GET | `/settlements/carryover/ledger` | 손실 이월 원장 |
| GET | `/settlements/carryover/balance` | 현재 잔액 |

### 3-8. 정책 (US-401~403)

12종 정책 각각 동일한 패턴.

| Method | Path | 비고 |
|---|---|---|
| GET | `/policies/{kind}` | kind ∈ {marketplace-fee, payment-fee, tax, fulfillment-fee, return-risk, ad-cost, target-margin, settlement, partner-share, expense-category, evidence-type} |
| GET | `/policies/{kind}/active` | 지금 effective 인 row 만 |
| POST | `/policies/{kind}` | 새 버전 (이전 row effective_to 자동 갱신) |
| PATCH | `/policies/{kind}/{id}` | reliability_status, memo 등 |
| POST | `/policies/{kind}/{id}/approve` | AUTO_DETECTED → CONFIRMED |
| POST | `/policies/{kind}/{id}/deprecate` | hard delete 금지 |
| GET | `/policies/history` | 통합 변경 이력 |

### 3-9. 리포트 (US-501, US-502)

| Method | Path | 비고 |
|---|---|---|
| GET | `/reports` | 사용 가능 리포트 목록 |
| POST | `/reports/{kind}/generate` | 동기 (작은 기간) 또는 비동기 job |
| GET | `/reports/{kind}/download?period=...` | Excel/CSV |
| POST | `/reports/bundles` | 분기/연 묶음 ZIP 생성 → 세무사 전달 |
| GET | `/reports/bundles` | 전달 이력 |
| PATCH | `/reports/bundles/{id}/received` | EXTERNAL_ACCOUNTANT — 수신 확인 |

리포트 kind: `monthly_sales`, `by_marketplace`, `by_product_pl`, `coupang_reconcile`, `vat_quarterly`, `partner_distribution`, `evidence_missing`, `monthly_expense`, `monthly_purchase`.

### 3-10. 대시보드 (US-601)

| Method | Path | 비고 |
|---|---|---|
| GET | `/dashboard/kpi?year_month=...` | 카드용 |
| GET | `/dashboard/trend?months=6` | 추세 차트 |
| GET | `/dashboard/top-margin-gap?limit=10` | 격차 TOP |
| GET | `/dashboard/alerts` | 위젯들 |

### 3-11. 관리 (US-701~705)

| Method | Path | 비고 |
|---|---|---|
| GET/POST/PATCH | `/admin/users` | 사용자 |
| POST | `/admin/users/invite` | 이메일 초대 |
| GET/POST | `/admin/audit-logs` | 감사 로그 검색·export |
| GET/POST/PATCH | `/admin/vault` | 비밀번호 보관 위치 |
| GET/PATCH | `/admin/notification-prefs` | 본인 (`channel_settings_json` MVP 활성 채널만 표시) |
| GET | `/notifications` | 본인 수신함 (IN_APP + EMAIL 통합) |
| GET | `/notifications/unread-count` | 헤더 벨 배지용 — `read_at IS NULL` 카운트 |
| POST | `/notifications/{id}/read` | 읽음 처리 (`read_at = now()`) |
| POST | `/notifications/mark-all-read` | 일괄 읽음 |
| GET | `/notifications/channels` | 활성 채널 목록 (notification_channel 메타). MVP = EMAIL/IN_APP |

---

## 4. 핵심 계산 함수 시그니처 (service layer)

### 4-1. 정책 적용

```python
@dataclass
class PolicyContext:
    org_id: int
    at: date                              # 효력 기준일
    marketplace: enum_sale_channel | None
    category_path: str | None
    margin_type: enum_margin_type | None
    payment_method: enum_payment_method | None

@dataclass
class ResolvedPolicies:
    marketplace_fee: MarketplaceFeePolicyRow | None
    payment_fee:    PaymentFeePolicyRow | None
    tax:            TaxPolicyRow
    fulfillment:    FulfillmentFeePolicyRow | None
    return_risk:    ReturnRiskPolicyRow | None
    ad_cost:        AdCostPolicyRow | None
    target_margin:  TargetMarginPolicyRow | None
    # 메타
    snapshot_refs: list[PolicySnapshotRef]
    snapshot_values_json: dict

def resolve_active_policies(ctx: PolicyContext, db) -> ResolvedPolicies: ...

def persist_policy_snapshot(
    owner_kind: Literal['MARGIN_EXPECTED','MARGIN_ACTUAL','SETTLEMENT_MONTHLY'],
    owner_id: int,
    resolved: ResolvedPolicies,
    db,
) -> int:
    """policy_snapshot + policy_snapshot_ref* + policy_snapshot_value 1행"""
```

### 4-2. 예상 마진

```python
@dataclass
class ExpectedMarginInput:
    org_id: int
    product_id: int
    margin_type: enum_margin_type
    marketplace: enum_sale_channel
    cost_amount: int               # 원
    sale_price: int                # 원
    payment_method: enum_payment_method
    extras: dict                    # 타입별: fulfillment_provider, ad_ratio_override 등
    override_fields: dict | None    # 사용자가 수동 수정한 필드 + 사유

@dataclass
class ExpectedMarginResult:
    marketplace_fee: int
    payment_fee: int
    ad_cost: int
    fulfillment_fee: int
    return_risk_cost: int
    vat: int
    margin_amount: int
    margin_rate: Decimal           # 0.2500 = 25%
    applied_policies: ResolvedPolicies
    warnings: list[str]            # 예: 정책 NEEDS_REVIEW

def calculate_expected_margin(input: ExpectedMarginInput, db) -> ExpectedMarginResult: ...

def save_expected_margin(input: ExpectedMarginInput, result: ExpectedMarginResult, db) -> int:
    """product_margin_expected 행 + snapshot 저장"""
```

#### 계산 의사코드 (공통 부분)
```
marketplace_fee = sale_price * marketplace_fee.rate          # RATE 인 경우
                  or marketplace_fee.fixed_amount             # FIXED
                  or apply_tiered(sale_price, tier_json)      # TIERED
payment_fee     = sale_price * payment_fee.rate
ad_cost         = sale_price * (extras.ad_ratio_override or ad_cost.default_ad_ratio or 0)
fulfillment_fee = fulfillment.outbound_fee + fulfillment.inbound_fee/expected_units + ...
return_risk_cost= sale_price * return_risk.return_rate
vat             = (sale_price - marketplace_fee) * tax.vat_rate   # 단순화. 실제 세무사 검토 필요
gross_in        = sale_price - marketplace_fee - payment_fee
                                                                  - ad_cost
                                                                  - fulfillment_fee
                                                                  - return_risk_cost
margin_amount   = gross_in - cost_amount - vat
margin_rate     = margin_amount / sale_price                       # Decimal
```

#### 타입별 분기
- `PURCHASE_AGENCY`: cost = 공급가 + 구매대행 수수료 + 국제 배송. `extras.agency_fee`, `extras.intl_shipping` 가산.
- `ROCKET_GROWTH`: fulfillment_fee 가 핵심. 입고·출고·보관·반품처리 분리.
- `OVERSEAS_DIRECT`: 환율 적용. `cost_amount = supply_foreign * fx_rate + customs`.
- `GENERAL_RESELL`: 풀필먼트 없음. fulfillment_fee = 0.

#### 모든 함수
- `Decimal` 사용 (`getcontext().prec = 28`), 최종 출력 시 `int(round(...))` 로 원 단위
- `float` 절대 금지

### 4-3. 실 마진

```python
@dataclass
class ActualMarginInput:
    org_id: int
    sales_order_id: int
    settlement_record_id: int | None  # 정산 record 매칭된 경우

def calculate_actual_margin(input: ActualMarginInput, db) -> ProductMarginActualResult:
    """
    1. sales_order + 매칭된 settlement_record 로부터 실 값 추출
       - revenue = settlement_record.gross_revenue or sales_order.sale_price * qty
       - marketplace_fee = settlement_record.marketplace_fee
       - ad_cost = settlement_record.ad_cost
       - return_cost = settlement_record.return_cost
       - fulfillment_fee = settlement_record.fulfillment_fee
    2. product_margin_expected 의 최신 row 매칭 → delta 계산
    3. product_margin_actual 행 INSERT + snapshot 박제
    """
```

### 4-4. 월별 정산

```python
@dataclass
class MonthlySettlementInput:
    org_id: int
    year_month: str  # 'YYYY-MM'
    overrides: dict | None  # priority/ratio override

@dataclass
class PartnerDistributionRow:
    partner_id: int
    distribution_kind: Literal['PRIORITY','RATIO']
    amount: int
    ratio_used: Decimal | None

@dataclass
class MonthlySettlementResult:
    total_revenue: int
    total_direct_cost: int
    total_common_expense: int
    net_profit: int
    carryover_offset_applied: int
    distributable_amount: int
    distributions: list[PartnerDistributionRow]
    warnings: list[str]
    snapshot_refs: list[PolicySnapshotRef]

def calculate_monthly_settlement(input: MonthlySettlementInput, db) -> MonthlySettlementResult: ...
```

#### 의사코드
```
period_start, period_end = month_range(input.year_month)

# 1) 매출
revenue = sum(
    sales_order.sale_price * sales_order.quantity
    where org and sale_date in [period_start, period_end] and deleted_at is null
)

# 2) 상품 직접비 (실 마진 합)
direct_cost = sum(
    product_margin_actual.actual_cost + actual_marketplace_fee + actual_ad_cost
                + actual_fulfillment_fee + actual_return_cost + actual_payment_fee
    where sales_order joined and sale_date in period
)

# 실 마진이 아직 없는 sales_order 가 있으면 warnings 에 기록

# 3) 공통 사업비 (settlement 공제 가능 + 사업용)
common_expense = sum(
    expenses.total_amount
    where org and expense_date in period
      and is_settlement_deductible = true
      and is_business_use = true
      and deleted_at is null
)

net_profit = revenue - direct_cost - common_expense

# 4) carryover offset
carryover_balance = get_carryover_balance(org_id, as_of=period_end)
if net_profit > 0 and carryover_balance > 0:
    offset = min(net_profit, carryover_balance)
    distributable = net_profit - offset
    record_carryover_event('OFFSET_APPLIED', amount = -offset)
elif net_profit < 0:
    # 적자
    distributable = 0
    record_carryover_event('LOSS_ADDED', amount = abs(net_profit))
else:
    offset = 0
    distributable = net_profit

# 5) priority distribution
priority_total = 0
for entry in settlement_policy.priority_distribution_json:
    if eval_condition(entry.condition):  # 예: "distributable >= 1000000"
        amount = min(entry.fixed_amount, distributable - priority_total)
        emit PartnerDistributionRow(PRIORITY, partner_id=entry.partner_id, amount=amount)
        priority_total += amount

remaining = distributable - priority_total

# 6) ratio distribution
ratios = active_partner_share_policy(year_month=input.year_month)
# share_ratio 합 = 1.0 가정
for share in ratios:
    amount = int(round(Decimal(remaining) * share.share_ratio))
    emit PartnerDistributionRow(RATIO, partner_id=share.partner_id, amount=amount, ratio_used=share.share_ratio)
# 잔수 조정: 합계 = remaining 되도록 마지막 row 에 +/-1 보정

# 7) overrides 적용 (DRAFT 상태에서만)
if input.overrides: apply_overrides(...)

return result
```

#### 정합성 검증
- priority + ratio 합 = distributable
- ratio 합계 = 1.0 위반 시 거부
- LOCKED 상태에서 mutation 호출 시 `ConflictError`

### 4-5. 등록가 시뮬레이션 (US-103)

```python
@dataclass
class PriceSimInput:
    org_id: int
    margin_type: enum_margin_type
    marketplace: enum_sale_channel
    cost_amount: int
    extras: dict
    target_rates: list[Decimal]  # [0.15, 0.20, 0.25, 0.30, 0.35]

@dataclass
class PriceSimRow:
    target_rate: Decimal
    recommended_price: int
    expected_margin: ExpectedMarginResult

def simulate_recommended_prices(input: PriceSimInput, db) -> list[PriceSimRow]:
    """
    sale_price 를 미지수로 두고 margin_rate = target_rate 가 되는 가격 역산.
    엑셀 S5 의 수식을 재작성:
      price = (cost + agency_fee + ...) / (1 - marketplace_fee.rate - target_rate - other_ratio_costs)
    """
```

### 4-6. 리포트 생성

```python
@dataclass
class TaxReportRequest:
    org_id: int
    kind: Literal['monthly_sales','by_marketplace','by_product_pl','coupang_reconcile',
                   'vat_quarterly','partner_distribution','evidence_missing',
                   'monthly_expense','monthly_purchase']
    period_start: date
    period_end: date
    format: Literal['xlsx','csv'] = 'xlsx'

@dataclass
class ReportFile:
    file_name: str            # '2026-06_월별매출리포트.xlsx'
    content_type: str
    bytes_payload: bytes
    row_count: int
    generated_at: datetime

def generate_tax_report(req: TaxReportRequest, db) -> ReportFile: ...

def generate_tax_report_bundle(org_id, period_kind, period_label, kinds, db) -> bytes:
    """ZIP 묶음, tax_report_bundle/item 행 INSERT"""
```

### 4-7. 알림

```python
# D-281: 채널 어댑터 추상 인터페이스
class ChannelAdapter(Protocol):
    code: enum_notification_channel_code
    def send(self, notification: Notification, recipient: AppUser) -> DeliveryResult: ...

class EmailChannel(ChannelAdapter): ...   # MVP
class InAppChannel(ChannelAdapter): ...   # MVP (DB 기록 + 벨 카운트)
class KakaoChannel(ChannelAdapter): ...   # Post-MVP, is_mvp_active=false → FEATURE_DISABLED
class SlackChannel(ChannelAdapter): ...   # Post-MVP

def schedule_settlement_deadline_reminders(now: datetime, db): ...
def schedule_vat_deadline_reminders(now: datetime, db): ...
def detect_margin_drop(org_id: UUID, db): ...
def send_notification(
    rec_user_id: UUID,
    kind: enum_notification_kind,
    title: str,
    body: str | None,
    channels: list[enum_notification_channel_code] | None = None,  # None = 사용자 pref 기반
    meta: dict | None = None,
    db = None,
) -> list[Notification]:
    """사용자 notification_pref 와 채널 활성 여부를 확인 후 채널별 1행씩 INSERT + adapter.send()"""
```

cron / APScheduler 예약: 매일 09:00 KST 점검.

---

## 5. Excel Import / Export 명세

### 5-1. Export (한국어 컬럼)

| 리포트 | 시트 / 컬럼 (예시) |
|---|---|
| 월별 매출 | 판매일, 마켓, 주문번호, 상품명, 수량, 판매가, 광고비, 배송비, 마진금액, 마진율 |
| 판매처별 매출 | 마켓, 매출, 마켓수수료, 광고비, 정산금액, 순이익 |
| 상품별 손익 | 상품명, 매출, 직접비, 마진, 마진율, 판매수량, delta(예상대비) |
| 쿠팡 정산 대사 | 정산기간, 쿠팡 정산금, 판매장부 합계, 차이, 사유 |
| 부가세 참고 | 매출 부가세, 매입 부가세, 차감액, 추정 납부세액 |
| 파트너별 수익배분 | 정산월, 파트너, 우선분배, 비율분배, 합계 |
| 증빙 누락 | 비용일, 카테고리, 결제자, 공급가액, 부가세, 합계, 증빙상태, 사유 |
| 월별 사업비용 | 비용일, 카테고리, 적요, 공급가액, 부가세, 합계, 결제자, 사업/사적, 증빙유형, 증빙상태 |
| 분기별 부가세 | 분기, 매출, 매출부가세, 매입, 매입부가세, 추정 납부 |

- 첫 줄은 메타 정보(조직명, 기간, 생성일시, 생성자)
- 금액은 `#,##0` 포맷, 비율은 `0.00%`
- 파일명: `YYYY-MM_리포트명.xlsx` (분기는 `YYYY-Q{n}`)

### 5-2. Import

| 종류 | 컬럼 (한국어 헤더) | 검증 |
|---|---|---|
| 판매장부 | 판매일/마켓/주문번호/상품(SKU 또는 이름)/수량/판매가/광고비/배송비/메모 | 주문번호 중복, 상품 매칭 |
| 쿠팡 정산 | 정산기간시작/종료/주문번호/총수익/마켓수수료/광고비/반품비/풀필먼트비/기타공제/순정산금 | file_hash 중복 거부 |
| 사업비용 | 비용일/카테고리/적요/공급가액/부가세/결제수단/결제자/사업카드여부/사업사용여부/증빙유형/증빙상태/귀속파트너 | 카테고리 매칭, total 일관성 |
| 정책 (선택) | 적용일/대상/값/source_url/memo | effective_from 검증 |

import 흐름: `preview` → 미매칭/오류 리포트 반환 → 사용자 확인 → `commit`.

---

## 6. 에러 / 상태 코드

| 코드 | HTTP | 의미 |
|---|---|---|
| `AUTH_REQUIRED` | 401 | JWT 없음/만료 |
| `FORBIDDEN` | 403 | RBAC 거부 |
| `NOT_FOUND` | 404 | |
| `VALIDATION_ERROR` | 422 | Pydantic 검증 |
| `CONFLICT` | 409 | 동시 변경 / LOCKED 상태 mutation |
| `POLICY_REVIEW_REQUIRED` | 409 | 정책이 NEEDS_REVIEW/EXPIRED 상태인데 강제 진행 시 |
| `IMPORT_DUPLICATE` | 409 | file_hash 중복 |
| `LOCKED_SETTLEMENT` | 409 | LOCKED 정산 관련 mutation |
| `REOPEN_LIMIT_EXCEEDED` | 409 | D-285: 정산 REOPEN 횟수 제한 초과 |
| `VARIANT_IN_USE` | 409 | D-286: variant/option 삭제 시 sales_order 매칭 존재 |
| `FEATURE_DISABLED` | 403 | D-282: EXTERNAL_ACCOUNTANT 등 비활성 기능 호출 |
| `INVALID_VAT_INPUT` | 422 | D-284: gross ≠ supply + vat 등 정합성 위반 |
| `MASKED_SECRET_DETECTED` | 400 | vault 등에서 평문 비밀번호 감지 |
| `INTERNAL_ERROR` | 500 | |

---

## 7. 테스트 시나리오 (정상·경계·실패)

### 7-1. 마진 계산 (대표 케이스)
- ✅ RATE 정책 + 정상 입력 → 결과 일관 (소수 4자리)
- ✅ TIERED 정책 + 경계값 (구간 경계) → 올바른 구간 적용
- ✅ override_fields 시 → snapshot 에 override 반영
- ✅ 정책 EXPIRED 상태 → 결과는 계산되되 warnings 에 명시
- ❌ float 사용 시 거부 (코드 컨벤션 테스트)
- ❌ margin_rate < 0 일 때도 그대로 보고 (예외 던지지 않음)

### 7-2. 월별 정산
- ✅ 정상 흑자월 → priority(파트너A 100만) + 6:4
- ✅ 적자월 → distributions = []. carryover_ledger LOSS_ADDED
- ✅ 적자 → 흑자 이월 → OFFSET_APPLIED, distributable 감소
- ✅ DRAFT override → 비율 일시 변경 (audit_log 기록)
- ❌ LOCKED 상태 mutation → CONFLICT
- ❌ ratio 합 ≠ 1.0 → VALIDATION_ERROR
- ✅ 정산 reopen 후 재확정 → reopen_count += 1, audit_log 2건
- ❌ reopen_count >= organization.settlement_reopen_limit (기본 3) → REOPEN_LIMIT_EXCEEDED (D-285)
- ✅ org 단위 reopen_limit_override 설정 시 그 값 우선

### 7-3. import
- ✅ 정상 CSV → 미매칭 0건
- ✅ 중복 주문번호 → 미매칭 섹션
- ❌ 동일 file_hash 재업로드 → IMPORT_DUPLICATE
- ✅ 부분 매칭 → 미매칭 건 수동 처리 후 commit

### 7-4. 정책
- ✅ 새 버전 등록 → 이전 row effective_to 자동 갱신
- ❌ 중첩 effective 기간 → VALIDATION_ERROR
- ✅ 정책 변경 후 기존 정산 재계산 → snapshot 으로 불변

### 7-5. 보안
- ❌ vault.memo 에 "pw: 12345" → MASKED_SECRET_DETECTED
- ❌ app_user.password_hash 에 평문 패턴 → 거부
- ✅ EXTERNAL_ACCOUNTANT 가 정책 수정 시도 → FORBIDDEN

### 7-6. 리포트
- ✅ 1년 분량 export < 5s (NFR)
- ✅ 컬럼 한국어, 파일명 규칙
- ✅ 빈 기간 → 빈 시트 + 0건 메타

---

## 8. 트랜잭션 & audit 경계

| 작업 | 트랜잭션 안 포함 |
|---|---|
| 마진 저장 | margin row + snapshot + snapshot_ref* + snapshot_value + audit_log |
| 정산 확정 (CONFIRM) | monthly_settlement.status, confirmed_at/by, snapshot 박제, audit_log |
| 정산 LOCK | status + locked_at/by + audit_log |
| 정산 REOPEN | settlement_reopen_audit + monthly_settlement.status='REOPENED', reopen_count += 1, audit_log, 알림 |
| 정책 새 버전 | 이전 row effective_to 갱신 + 신규 row 삽입 + policy_change_history + audit_log |
| import commit | 정산 record 다건 + sales_order 매칭 갱신 + actual_margin 다건 + snapshot 다건 + audit_log |

모든 mutation 은 `audit_log` 에 최소 1행 기록. 시스템 액션은 `actor_user_id = NULL`.

---

## 9. Open Items 영향 (Backend 관점) — 2026-06-18 모두 적용

| Open Item | 적용 결과 |
|-----------|----------|
| O-001 (D-280) | 모든 service 함수에 `org_id` 강제 주입 (JWT 클레임). organization 단위 격리. business_type 별 분기는 정책 row 위임 |
| O-002 (D-281) | `notification_service` = `ChannelAdapter` 추상 인터페이스 (`EmailChannel`, `InAppChannel` MVP, 나머지 Post-MVP). `notification_channel.is_mvp_active=false` 인 채널 호출 시 `FEATURE_DISABLED` |
| O-003 (D-282) | `EXTERNAL_ACCOUNTANT` 라우트는 `feature_flag('FEATURE_EXTERNAL_ACCOUNTANT')` 가드. MVP=false → 403 FEATURE_DISABLED. 세무사 export 7종 (월별매출/비용/정산/증빙누락/부가세/종합소득세/사업소득) `generate_tax_report` kinds 확장 |
| O-004 (D-283) | 모든 모델 PK = UUID. app-side `uuid7()` 생성. `business_key` 컬럼은 service 가 자동 생성 (예: `PROD-{org_seq}`, `STL-{YYYYMM}`) |
| O-005 (D-284) | `expense_service.create` 가 `vat_inclusion_type` 분기: INCLUDED → 1/(1+vat_rate) 자동 분리 + `is_manual_tax_override=false`. EXCLUDED → 입력값 그대로 + `is_manual_tax_override=true`. EXEMPT → vat=0. `tax_policy_id` 박제 |
| O-006 (D-285) | `reopen_settlement` 가 `effective_reopen_limit = settlement.reopen_limit_override ?? organization.settlement_reopen_limit` 검증. 초과 시 `REOPEN_LIMIT_EXCEEDED`. reason/reopened_by/last_reopened_at 기록 + `settlement_reopen_audit` 1행 + audit_log |
| O-007 (D-286) | `product_option_service`, `product_variant_service` 신설. `sales_order` / `product_margin_*` 입력에 optional `product_variant_id`. variant 삭제 시 sales_order 매칭 검사 → `VARIANT_IN_USE` |

---

## 10. Handoff

### Summary
14개 라우터 그룹, 약 80+ endpoint, 핵심 계산 함수 6종 (`resolve_active_policies`, `calculate_expected_margin`, `calculate_actual_margin`, `calculate_monthly_settlement`, `simulate_recommended_prices`, `generate_tax_report`) 정의. import/export 매핑 + 테스트 시나리오 6개 그룹.

### Decisions (Backend 단독 결정)
- 디렉토리: api/services/repositories/domain/db/io 계층 분리
- 응답 wrapper `{data, meta}`, 에러 `{error{code,message,details}}`
- 모든 mutation 트랜잭션 안에 audit_log
- 정책 snapshot 박제는 service 가 명시적으로 호출 (자동 트리거 X)
- Decimal 사용, float 금지, 원 단위 BIGINT
- 정산 계산 시 LOCKED 상태 mutation 거부

### Open Questions
- audit_log 기록을 DB 트리거 vs 서비스 호출 — **현재 안: 서비스 호출 (테스트성·명확성)**. DBA 와 합의 완료
- async vs sync — FastAPI async 권장이나, SQLAlchemy 2 sync 도 가능. **MVP: sync** (psycopg2)
- 리포트 비동기 처리 임계값 — 작은 기간(<1개월): 동기. 큰 기간: background task (Post-MVP 는 Celery/RQ)

### Risks
- 정책 12종 동시 조회의 성능 — 캐싱 (org_id+at 기준 메모리 5분 캐시) 필요
- 정산 계산 시 sales_order N+1 — JOIN + bulk 처리 필수
- snapshot JSONB 비대 → 큰 정책(tier_json 등)은 sub-snapshot 분리 가능

### Required Inputs for Next Role
- **Designer**: 각 endpoint 의 화면 매핑 + 입력 폼 / 테이블 인라인 편집 / [예상|실제] 토글 / 정책 자동값 UX
- **Frontend**: 본 API spec 기반 type-safe 클라이언트 (OpenAPI 자동 생성), 라우팅
- **DevOps**: env (DATABASE_URL, JWT_SECRET, SMTP_*, BACKUP_PATH 등), Docker Compose, Postgres extension, cron (APScheduler)
- **User/세무**: 계산 결과 표본 검증 (특히 예상 vs 실제 마진, 정산 분배, 부가세 참고)

### Handoff Target
**Designer + Frontend (병렬 시작 가능)** → DevOps → User/세무 검증

---

## 11. Action Checklist

- [x] FastAPI 디렉토리 구조
- [x] 인증 / RBAC 매트릭스
- [x] 14개 라우터 그룹, 80+ endpoint
- [x] 6종 핵심 계산 함수 시그니처 + 의사코드
- [x] Excel import/export 컬럼 매핑
- [x] 에러 코드 표
- [x] 테스트 시나리오 6개 그룹
- [x] 트랜잭션 / audit 경계 표
- [x] Open Items 영향
- [x] Open Items 7건 모두 반영 완료 (2026-06-18)
- [x] 옵션/variant endpoint, REOPEN limit endpoint, in-app 알림 endpoint 추가
- [x] 에러코드 5종 추가 (REOPEN_LIMIT_EXCEEDED, VARIANT_IN_USE, FEATURE_DISABLED, INVALID_VAT_INPUT)
- [x] ChannelAdapter 추상 + EMAIL/IN_APP MVP 구체화
- [x] Handoff
- [x] Designer: `docs/23_design.md` (완료)
- [x] Frontend: `docs/24_frontend.md` (완료)
- [ ] (Next) 코드 생성 진입 — DB 마이그레이션 + seed → BE 핵심 계산 함수 6종 + 테스트 → API endpoint → FE
