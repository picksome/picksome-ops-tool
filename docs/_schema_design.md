# DBA + Backend 합동 기술 세션

- 일시: 2026-06-17
- 참여: DBA, Backend
- 목적: 정책 테이블 패밀리·snapshot 패턴·예상/실마진 분리·정산/세무 도메인 DB·계산 함수 시그니처 합의
- 결론은 정식 ERD가 아닌 **설계 합의**(설계 합의 → 다음 단계에서 ERD 확정)

---

## 0. 공통 설계 원칙

1. 모든 금액 컬럼: `BIGINT`(원 단위 정수) 또는 `NUMERIC(18,4)` (비율). **`float` 금지.**
2. 모든 테이블 audit 컬럼: `created_at`, `updated_at`, `created_by`, `updated_by`.
3. 삭제: 가능한 한 `deleted_at` (soft delete). 정산 확정 데이터는 hard delete 절대 금지.
4. 시각: `TIMESTAMPTZ`, 저장은 UTC, 표시·집계는 `Asia/Seoul`.
5. ID: `BIGSERIAL` 또는 `UUID` — 합의 필요 (DBA 의견: 내부툴 BIGSERIAL, SaaS 확장 위해 UUID 권장)
6. 멀티 테넌트 슬롯: 모든 도메인 테이블에 `org_id BIGINT` (MVP는 단일 org=1, SaaS 확장 시 활성).

---

## 1. 정책 테이블 패밀리 (Policy Family)

모든 정책 테이블은 아래 공통 컬럼을 갖는다.

```sql
-- 공통 정책 컬럼 (모든 *_policy 테이블에 포함)
id              BIGSERIAL PK
org_id          BIGINT NOT NULL DEFAULT 1
version         INT NOT NULL                  -- 동일 정책 키의 N번째 버전
effective_from  DATE NOT NULL
effective_to    DATE NULL                     -- NULL = 현재 활성
source_type     TEXT NOT NULL                 -- CONFIRMED | AUTO_DETECTED | NEEDS_REVIEW | EXPIRED | MANUAL_OVERRIDE | DEPRECATED
source_url      TEXT NULL
source_checked_at TIMESTAMPTZ NULL
approved_by     BIGINT NULL FK users
approved_at     TIMESTAMPTZ NULL
memo            TEXT NULL
is_active       BOOLEAN NOT NULL DEFAULT TRUE
created_at, updated_at, created_by, updated_by
```

### 1-1. marketplace_fee_policy

```sql
marketplace        TEXT NOT NULL          -- coupang | smartstore | 11st | gmarket | auction | tmon | wemakeprice | shopee | ...
category_code      TEXT NULL              -- 마켓 내부 카테고리 코드
category_name      TEXT NULL              -- 한글 카테고리
fee_type           TEXT NOT NULL          -- RATE | FIXED | TIERED | RATE_PLUS_FIXED
fee_rate           NUMERIC(7,4) NULL      -- 0.1080
fixed_fee_amount   BIGINT NULL            -- 원
min_fee_amount     BIGINT NULL
max_fee_amount     BIGINT NULL
tier_config        JSONB NULL             -- TIERED일 때 단계별 정의
```

UNIQUE 제약: `(org_id, marketplace, category_code, version)`
인덱스: `(marketplace, category_code, effective_from DESC)` — 적용 시점 조회용

### 1-2. payment_fee_policy

```sql
payment_method     TEXT NOT NULL          -- CARD | CASH | TRANSFER | POINT
fee_rate           NUMERIC(7,4) NULL
fixed_fee_amount   BIGINT NULL
```

### 1-3. tax_policy

```sql
tax_type           TEXT NOT NULL          -- VAT | INCOME_TAX | LOCAL_TAX
business_type      TEXT NOT NULL          -- GENERAL | SIMPLE | EXEMPT | ZERO_RATED
tax_rate           NUMERIC(7,4) NOT NULL  -- 0.1000
calculation_method TEXT NOT NULL          -- INCLUDED_IN_PRICE | EXCLUDED_FROM_PRICE
deduction_method   TEXT NULL              -- 매입세액 공제 방식
country            TEXT NOT NULL DEFAULT 'KR'
```

### 1-4. fulfillment_fee_policy

```sql
provider               TEXT NOT NULL      -- COUPANG_ROCKET_GROWTH | COUPANG_3PL | ...
fee_name               TEXT NOT NULL      -- INBOUND | OUTBOUND | STORAGE | RETURN_HANDLING
product_size_type      TEXT NULL          -- SMALL | MEDIUM | LARGE | OVERSIZED
weight_min, weight_max NUMERIC(10,2) NULL
volume_min, volume_max NUMERIC(10,2) NULL
fee_amount             BIGINT NULL
fee_rate               NUMERIC(7,4) NULL
fee_unit               TEXT NULL          -- PER_UNIT | PER_KG | PER_DAY | PERCENT
```

### 1-5. return_risk_policy

```sql
marketplace          TEXT NULL
category_name        TEXT NULL
return_risk_rate     NUMERIC(7,4) NOT NULL   -- 0.2000 = 20% 기본
return_cost_amount   BIGINT NULL             -- 1회당 정액
calculation_method   TEXT NOT NULL           -- RATE_OF_SALES | FIXED_PER_RETURN
```

### 1-6. ad_cost_policy

```sql
marketplace          TEXT NOT NULL
ad_type              TEXT NOT NULL           -- SEARCH | DISPLAY | CPC | CPM | ROAS_BASED
target_roas          NUMERIC(7,2) NULL       -- 7.00 = 700%
default_ad_cost_rate NUMERIC(7,4) NULL       -- 매출의 N%
calculation_method   TEXT NOT NULL           -- FIXED_RATE | ROAS | INPUT_DRIVEN
```

### 1-7. target_margin_policy

```sql
category_name        TEXT NULL               -- NULL=전체 기본
target_margin_rate   NUMERIC(7,4) NOT NULL   -- 0.3000 = 30%
warning_threshold    NUMERIC(7,4) NOT NULL   -- 0.1000 = 이 이하면 경고
critical_threshold   NUMERIC(7,4) NOT NULL   -- 0.0000 = 적자 임박
```

### 1-8. settlement_policy

순이익 분배의 글로벌 룰. partner_share_policy와 함께 사용.

```sql
name                 TEXT NOT NULL
loss_policy          TEXT NOT NULL           -- CARRYOVER | SPLIT
common_expense_inclusion JSONB NOT NULL      -- {include_categories: [...], exclude_categories: [...]}
priority_distributions JSONB NULL            -- [{partner_id, amount, order}] 우선분배
```

### 1-9. partner_share_policy

월별·파트너별 분배 비율 (전월 룰 변경 가능).

```sql
settlement_month     DATE NOT NULL           -- 2026-06-01 형식 (월의 첫날)
partner_id           BIGINT NOT NULL FK partner
share_ratio          NUMERIC(7,4) NOT NULL   -- 합계=1.0 검증
partner_specific_deduction_categories JSONB NULL  -- 이 파트너에게만 귀속되는 비용 카테고리
```

UNIQUE: `(org_id, settlement_month, partner_id, version)`
CHECK: 동일 settlement_month의 share_ratio 합 = 1.0 (트리거 또는 service layer 검증)

### 1-10. expense_category_policy

```sql
code                 TEXT NOT NULL           -- OFFICE_RENT | SUBSCRIPTION | TAX_SERVICE | ...
name                 TEXT NOT NULL           -- 한글명
is_tax_deductible_default BOOLEAN NOT NULL DEFAULT TRUE
is_settlement_deductible_default BOOLEAN NOT NULL DEFAULT TRUE
parent_code          TEXT NULL               -- 계층 (대분류/소분류)
display_order        INT NOT NULL
```

### 1-11. evidence_type_policy

```sql
code                 TEXT NOT NULL           -- TAX_INVOICE | RECEIPT_CARD | RECEIPT_CASH | TRANSFER | COUPANG_SETTLEMENT | ...
name                 TEXT NOT NULL           -- 한글명
is_official_evidence BOOLEAN NOT NULL        -- 세금계산서/계산서 등 공식 증빙 여부
```

### 1-12. policy_change_history (통합 변경 로그)

```sql
id                   BIGSERIAL PK
org_id               BIGINT NOT NULL
policy_table         TEXT NOT NULL           -- 'marketplace_fee_policy' 등
policy_id            BIGINT NOT NULL         -- 변경된 row id
action               TEXT NOT NULL           -- INSERT | UPDATE | EXPIRE | DEPRECATE | APPROVE
before               JSONB NULL
after                JSONB NULL
reason               TEXT NULL
changed_by           BIGINT NOT NULL FK users
changed_at           TIMESTAMPTZ NOT NULL
```

---

## 2. 정책 Snapshot 패턴

### 원칙
계산 시점의 정책 row를 그대로 결과에 박는다. 정책이 미래에 바뀌어도 과거 결과는 불변.

### 두 가지 구현 옵션

**옵션 A — 정책 id+version 만 저장 (가벼움)**
```sql
margin_calculation
  ...
  marketplace_fee_policy_id   BIGINT NULL FK
  marketplace_fee_policy_version INT NULL
  tax_policy_id ... version ...
  fulfillment_fee_policy_id ... version ...
  return_risk_policy_id ... version ...
  ad_cost_policy_id ... version ...
```
장점: 가볍고 정규화됨.
단점: 정책 row를 hard delete 하면 snapshot 깨짐. → **정책 row hard delete 금지** 규칙으로 방어.

**옵션 B — JSONB로 전체 값 박제 (안전)**
```sql
margin_calculation
  ...
  policy_snapshot JSONB NOT NULL  -- {marketplace_fee:{...all fields}, tax:{...}, ...}
```
장점: 정책 테이블 스키마가 바뀌어도 영향 없음.
단점: 저장 용량, 정규화 깨짐.

**합의**: **하이브리드 = 옵션 A + JSONB 백업**.
정상 조회는 FK로, 안전망으로 JSONB도 박제.

```sql
policy_snapshot_refs    JSONB NOT NULL  -- {fee_policy_id:.., version:.., tax_policy_id:.., ...}
policy_snapshot_values  JSONB NOT NULL  -- 전체 값 복사
```

---

## 3. 예상 마진 vs 실마진 — 분리 모델

### 핵심 결정
두 개념을 **다른 테이블**로 저장 (조회/대시보드 성능, 책임 분리).

### 3-1. product_margin_expected

상품 등록 시점에 정책 자동값 + 수동 override 로 계산.

```sql
id                   BIGSERIAL PK
org_id               BIGINT
product_id           BIGINT FK product
marketplace          TEXT NOT NULL
margin_type          TEXT NOT NULL    -- PURCHASE_AGENCY | ROCKET_GROWTH | OVERSEAS_DIRECT | GENERAL_RESELL
calculated_at        TIMESTAMPTZ NOT NULL

-- 입력값 (정책 자동 + 사용자 입력)
selling_price        BIGINT
shipping_revenue     BIGINT
product_cost         BIGINT
purchase_shipping    BIGINT
fulfillment_costs    JSONB             -- {inbound:..., outbound:..., storage:...}
ad_cost              BIGINT
expected_return_cost BIGINT
other_costs          BIGINT

-- 정책 snapshot
fee_rate_applied     NUMERIC(7,4)
tax_rate_applied     NUMERIC(7,4)
return_risk_applied  NUMERIC(7,4)
policy_snapshot_refs JSONB
policy_snapshot_values JSONB

-- 결과
expected_margin      BIGINT
expected_margin_rate NUMERIC(7,4)
target_margin_rate   NUMERIC(7,4)     -- target_margin_policy 적용

-- override 추적
is_overridden        BOOLEAN
override_fields      JSONB NULL        -- 어떤 필드가 수동 수정됐는지
override_reason      TEXT NULL

created_at, updated_at, created_by, updated_by
```

### 3-2. product_margin_actual

실제 정산/판매 데이터로 계산.

```sql
id                   BIGSERIAL PK
org_id               BIGINT
order_id             BIGINT FK sales_order   -- 주문 단위로 계산
product_id           BIGINT
calculated_at        TIMESTAMPTZ NOT NULL

-- 실제 데이터
actual_selling_price BIGINT
actual_shipping     BIGINT
actual_product_cost BIGINT
actual_marketplace_fee BIGINT        -- 실 정산 데이터
actual_ad_cost      BIGINT
actual_return_cost  BIGINT
actual_fulfillment_costs JSONB
actual_other_costs  BIGINT
settlement_received BIGINT            -- 마켓에서 실제 받은 정산금

-- 결과
actual_margin       BIGINT
actual_margin_rate  NUMERIC(7,4)

-- 비교
expected_margin_ref_id BIGINT FK product_margin_expected
delta_margin        BIGINT             -- actual - expected
delta_rate          NUMERIC(7,4)
delta_alert_level   TEXT               -- OK | WARN | CRITICAL

created_at, updated_at, created_by, updated_by
```

### 3-3. 비교 뷰 (Materialized View 권장)

```sql
margin_comparison_v
  product_id, period, expected_total, actual_total, delta_amount, delta_pct, alert_level
```

---

## 4. 정산 도메인

### 4-1. monthly_settlement

```sql
id                       BIGSERIAL PK
org_id                   BIGINT
settlement_month         DATE NOT NULL    -- 2026-06-01
status                   TEXT NOT NULL    -- DRAFT | CONFIRMED | REOPENED | LOCKED

-- 매출 컴포넌트
total_revenue            BIGINT
shipping_revenue         BIGINT

-- 직접비
total_product_cost       BIGINT
total_marketplace_fee    BIGINT
total_ad_cost            BIGINT
total_return_cost        BIGINT
total_fulfillment_cost   BIGINT
total_other_direct       BIGINT

-- 영업이익
operating_profit         BIGINT GENERATED ALWAYS AS (...) STORED  -- 또는 service layer

-- 공통 비용
common_expense_total     BIGINT
common_expense_breakdown JSONB             -- 카테고리별 합

-- 적자 carryover
loss_carryover_in        BIGINT
loss_carryover_out       BIGINT

-- 분배 가능 순이익
distributable_net_profit BIGINT

-- 정책 snapshot
settlement_policy_id     BIGINT FK
settlement_policy_version INT
partner_share_policy_snapshot JSONB         -- 그달 적용 비율

-- 확정 정보
confirmed_at             TIMESTAMPTZ NULL
confirmed_by             BIGINT FK users NULL
locked_at                TIMESTAMPTZ NULL
locked_by                BIGINT FK users NULL
reopened_reason          TEXT NULL

created_at, updated_at, created_by, updated_by
```

UNIQUE: `(org_id, settlement_month, status != REOPENED)` — 동월 활성 정산 1건 보장
CHECK: status 전이 검증 (트리거 또는 service layer)

### 4-2. partner_settlement

```sql
id                       BIGSERIAL PK
monthly_settlement_id    BIGINT FK
partner_id               BIGINT FK
share_ratio              NUMERIC(7,4)    -- snapshot
priority_amount          BIGINT           -- snapshot (있다면)
partner_specific_deduction BIGINT         -- 이 파트너에게만 귀속된 비용
final_share_amount       BIGINT NOT NULL  -- 최종 지급액
payout_status            TEXT NOT NULL    -- PENDING | PAID | HOLD | CANCELLED
payout_date              DATE NULL
payout_evidence          TEXT NULL        -- 송금 증빙(은행 거래번호 등)
memo                     TEXT NULL
created_at, updated_at
```

### 4-3. loss_carryover_ledger

```sql
id                       BIGSERIAL PK
org_id                   BIGINT
month                    DATE NOT NULL
balance_before           BIGINT
loss_added               BIGINT           -- 이번달 발생 손실 (음수)
loss_used                BIGINT           -- 이번달 흑자에서 차감
balance_after            BIGINT
related_settlement_id    BIGINT FK monthly_settlement
created_at
```

---

## 5. 사업비용 도메인

### 5-1. expenses (강화)

```sql
id                       BIGSERIAL PK
org_id                   BIGINT
expense_date             DATE NOT NULL
settlement_month         DATE NOT NULL          -- 귀속 월 (보통 expense_date의 월)
category_id              BIGINT FK expense_category_policy
expense_name             TEXT NOT NULL
vendor_name              TEXT NULL

-- 금액
supply_amount            BIGINT NOT NULL        -- 공급가액
vat_amount               BIGINT NOT NULL        -- 부가세
total_amount             BIGINT NOT NULL        -- 합계

-- 결제
payment_method           TEXT NOT NULL          -- CARD | CASH | TRANSFER | POINT
payment_card_holder      TEXT NULL              -- 카드명의
paid_by_user_id          BIGINT FK users
is_personal_card         BOOLEAN NOT NULL DEFAULT FALSE   -- 개인 카드?
is_business_use          BOOLEAN NOT NULL DEFAULT TRUE    -- 사업 용도?

-- 증빙
evidence_type_id         BIGINT FK evidence_type_policy NULL
evidence_status          TEXT NOT NULL          -- MISSING | PENDING_CHECK | OK | SUBMITTED_TO_ACCOUNTANT | EXCLUDED
evidence_file_id         BIGINT FK attachments NULL  -- MVP에서 NULL 허용

-- 세무/정산 처리
is_tax_deductible        BOOLEAN NOT NULL       -- 부가세 매입세액 공제 가능?
is_settlement_deductible BOOLEAN NOT NULL       -- 수익배분 차감 대상?
attributable_partner_id  BIGINT FK partner NULL -- 특정 파트너에게만 귀속 (Post-MVP 가능)

memo                     TEXT NULL
created_at, updated_at, created_by, updated_by, deleted_at
```

인덱스: `(settlement_month)`, `(category_id, settlement_month)`, `(evidence_status)` 부분 인덱스 (MISSING/PENDING_CHECK)

### 5-2. attachments (Post-MVP 파일 저장. MVP는 테이블만 준비)

```sql
id, file_path, file_name, mime_type, size_bytes, uploaded_by, uploaded_at, hash
```

---

## 6. 핵심 도메인 (간략)

### product, sales_order, marketplace_settlement_record

```sql
product
  id, org_id, internal_code (PICK0000), name, category_name, margin_type,
  default_marketplace, is_active, ...

sales_order
  id, org_id, order_date, marketplace, marketplace_order_no,
  product_id, quantity, selling_price, shipping_price, ...
  status (NORMAL | CANCELLED | RETURNED), settlement_month

marketplace_settlement_record
  id, org_id, marketplace, settlement_date, settlement_amount,
  related_orders JSONB, raw_data JSONB
```

---

## 7. RBAC / 세무사 협업

```sql
users (id, org_id, email, name, password_hash, is_active, ...)
roles (id, code, name)          -- ADMIN | OPERATOR | SOURCING | ACCOUNTING | READONLY | EXTERNAL_ACCOUNTANT
permissions (id, code)          -- READ_X | WRITE_X | EXPORT_X | APPROVE_X ...
user_roles (user_id, role_id)
role_permissions (role_id, permission_id)
accountant_invites (id, email, invited_by, invited_at, accepted_at, status)
accountant_comments (id, target_table, target_id, comment, created_by, created_at, resolved_at)
```

EXTERNAL_ACCOUNTANT 역할은 READ + COMMENT + 일부 상태 변경(증빙 SUBMITTED→완료) 만 가능.

---

## 8. Audit Log

```sql
audit_log
  id BIGSERIAL PK
  org_id BIGINT
  table_name TEXT NOT NULL
  row_id BIGINT NOT NULL
  action TEXT NOT NULL            -- CREATE | UPDATE | DELETE | SOFT_DELETE | RESTORE
  before JSONB NULL
  after JSONB NULL
  changed_by BIGINT FK users
  changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
  request_id TEXT NULL            -- 요청 추적
  user_agent TEXT NULL
  ip_address INET NULL
```

구현: BE에서 SQLAlchemy event listener 또는 명시적 trigger.
민감 컬럼은 before/after에서 마스킹.

---

## 9. 리포트 도메인

```sql
tax_report_export
  id, org_id, report_type, period_start, period_end,
  parameters JSONB, file_path, file_name, file_size, file_hash (sha256),
  exported_by, exported_at,
  business_settings_snapshot JSONB

report_export_log
  id, org_id, report_type, requested_at, requested_by,
  status (QUEUED | RUNNING | DONE | FAILED), error_message
```

---

## 10. 비밀번호 보관 위치 (민감정보 금지)

```sql
password_vault_entry
  id, org_id, service_name, login_id, url,
  vault_location TEXT NOT NULL    -- 예: "Bitwarden - 쿠팡 Wing"
  responsible_user_id BIGINT FK users
  memo TEXT NULL
  created_at, updated_at, created_by, updated_by
```

**CHECK 제약**: 비밀번호/계좌비번/카드비번/주민번호 정규식 매칭 컬럼 거부 (DB 트리거로 방어).

---

## 11. BE 계산 함수 시그니처 (Service Layer)

```python
# Decimal/int만 사용. float 금지.

from datetime import date
from decimal import Decimal
from typing import Literal

MarginType = Literal["PURCHASE_AGENCY", "ROCKET_GROWTH", "OVERSEAS_DIRECT", "GENERAL_RESELL"]

@dataclass(frozen=True)
class PolicySnapshot:
    marketplace_fee: dict
    tax: dict
    fulfillment: dict | None
    return_risk: dict
    ad_cost: dict
    target_margin: dict
    refs: dict   # ids+versions

@dataclass(frozen=True)
class ExpectedMarginInput:
    org_id: int
    product_id: int
    marketplace: str
    margin_type: MarginType
    selling_price: int
    product_cost: int
    purchase_shipping: int
    overrides: dict = field(default_factory=dict)

@dataclass(frozen=True)
class ExpectedMarginResult:
    expected_margin: int
    expected_margin_rate: Decimal
    target_margin_rate: Decimal
    recommended_selling_price: int
    breakdown: dict
    snapshot: PolicySnapshot

def get_active_policy_snapshot(
    org_id: int, marketplace: str, category: str | None,
    at: date | None = None,
) -> PolicySnapshot: ...

def calculate_expected_margin(input: ExpectedMarginInput, snapshot: PolicySnapshot) -> ExpectedMarginResult: ...

@dataclass(frozen=True)
class ActualMarginInput:
    order_id: int
    settlement_record_id: int | None  # 마켓 정산 데이터 매칭됐다면

def calculate_actual_margin(input: ActualMarginInput) -> ActualMarginResult: ...

# 월별 정산
@dataclass(frozen=True)
class MonthlySettlementInput:
    org_id: int
    settlement_month: date
    settlement_policy_id: int
    partner_shares: list[tuple[int, Decimal]]   # (partner_id, ratio)
    include_categories: list[str] | None
    exclude_categories: list[str] | None
    priority_distributions: list[tuple[int, int]]  # (partner_id, amount)

@dataclass(frozen=True)
class MonthlySettlementResult:
    operating_profit: int
    common_expense_total: int
    loss_carryover_in: int
    distributable_net_profit: int
    partner_distributions: dict[int, int]   # partner_id → amount
    loss_carryover_out: int
    snapshot: dict

def calculate_monthly_settlement(input: MonthlySettlementInput) -> MonthlySettlementResult: ...

# 세무 리포트
@dataclass(frozen=True)
class TaxReportRequest:
    report_type: str   # MONTHLY_SALES | MONTHLY_PURCHASE | MONTHLY_EXPENSES | QUARTERLY_VAT | ...
    period_start: date
    period_end: date
    org_id: int

def generate_tax_report(req: TaxReportRequest) -> ExportFile: ...
```

---

## 12. 계산 의사코드 (요약)

### 예상 마진
```
snapshot = active_policy(marketplace, category, at=today)
fee = selling_price * snapshot.marketplace_fee.rate (+ fixed)
return_cost = selling_price * snapshot.return_risk.rate
fulfillment = sum(fulfillment 구성)  # ROCKET_GROWTH일 때만
ad = selling_price * snapshot.ad_cost.default_rate (or ROAS-driven)
vat_payable = max(0, selling_price*0.10 - (product_cost+purchase_shipping)*0.10 - fee*0.10)
expected_margin = selling_price + shipping_revenue
                  - product_cost - purchase_shipping
                  - fee - fulfillment - ad - return_cost - vat_payable - other
```

### 월별 정산
```
direct_components = SUM(sales_order × actual_margin breakdown)
operating_profit = total_revenue - direct_components
common_expense = SUM(expenses WHERE settlement_month=M AND is_settlement_deductible)
gross_distributable = operating_profit - common_expense

# Carryover 적용
carryover_in = last_balance(loss_carryover_ledger)
if gross_distributable < 0:
    distributable = 0
    carryover_out = carryover_in + gross_distributable   # 손실 누적
else:
    if carryover_in < 0:
        used = min(gross_distributable, -carryover_in)
        distributable = gross_distributable - used
        carryover_out = carryover_in + used
    else:
        distributable = gross_distributable
        carryover_out = carryover_in

# 우선 분배
remaining = distributable
for p, amt in priority_distributions:
    give[p] += min(remaining, amt)
    remaining -= min(remaining, amt)

# 비율 분배
for partner_id, ratio in shares:
    give[partner_id] += int((remaining * ratio).quantize(...))  # Decimal
# 잔돈은 가장 큰 share에게 (합계 = distributable 보장)
```

---

## 13. 인덱스 / 제약 핵심

- `sales_order(org_id, order_date)`
- `sales_order(settlement_month, marketplace)`
- `expenses(settlement_month)`, `expenses(category_id, settlement_month)`
- `expenses(evidence_status) WHERE evidence_status IN ('MISSING','PENDING_CHECK')` 부분 인덱스
- `monthly_settlement(org_id, settlement_month) UNIQUE WHERE status != 'REOPENED'`
- `marketplace_fee_policy(marketplace, category_code, effective_from DESC)`
- 모든 *_policy: `(effective_from, effective_to)` 인덱스

---

## 14. Open Questions

1. **ID 타입**: BIGSERIAL vs UUID — SaaS 확장성 고려해 UUID 권장? (DBA 의견: 우선 BIGSERIAL, 외부 노출 ID는 별도 nanoid)
2. **partner_share_policy의 합계=1.0 검증**: 트리거 vs service layer? (BE 의견: service layer + DB CHECK으로 이중 방어)
3. **공급가액/부가세 자동 분리**: total_amount만 입력 시 자동 분리할까? (세무사 의견 필요)
4. **정산 REOPENED 시 audit**: 재오픈 횟수 제한 둘까? (User 의견 추가 필요)
5. **product 옵션(사이즈/색상)**: Post-MVP지만 DB에 `product_option` 슬롯 미리 둘까?

---

## 15. Handoff (DBA+BE → PM/Architect)

### Summary
정책 12종 + snapshot 하이브리드(refs+values) + 예상/실마진 분리 + 정산 확정 잠금 + carryover + audit_log + RBAC + 세무사 협업 구조를 합의. 모든 금액 정수, float 금지.

### Decisions
- 정책 테이블 12종 확정 (위 1-1~1-12)
- snapshot 패턴: FK + JSONB 하이브리드
- 예상/실마진 분리 테이블
- 정산 status 전이: DRAFT → CONFIRMED → LOCKED (REOPENED은 audit 동반)
- audit_log 모든 도메인 적용
- 비밀번호 보관 위치 테이블 + CHECK 제약으로 평문 방어

### Open Questions
- ID 타입 (BIGSERIAL vs UUID)
- 공급가액/부가세 자동 분리 정책
- 정산 재오픈 횟수 제한

### Risks
- 정책 12종 = 초기 설정 부담. 관리자 정책 import 화면이 별도로 필요.
- snapshot JSONB가 커지면 행 크기 증가. → TOAST 압축 활용, 큰 snapshot 분리 저장 검토.
- Audit log 크기 증가 → 파티셔닝(월별) 또는 보관 기간 정책 필요.

### Required Inputs for Next Role
- PM: 위 Open Questions에 대한 결정
- Designer: 정책 관리 화면 와이어프레임
- FE: API 명세 받고 화면 연동

### Handoff Target
PM / Architect
