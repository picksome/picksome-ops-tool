# 21. ERD — PICKSOME OPS TOOL

> 작성: DBA
> 작성일: 2026-06-17
> 입력: `20_prd.md`, `_schema_design.md`, `00_project_overview.md`, `10_decision_log.md`
> 다음 역할: Backend (API + 계산 함수 — 본 ERD 기준)

---

## 0. Reframed Request

PRD 의 29개 Must Feature 와 5개 User Flow 를 충족하도록
**PostgreSQL 16 기준의 ERD, 테이블 정의, 컬럼·제약·인덱스, 마이그레이션 절차**를 확정한다.

**Open Items 7건 모두 Confirmed (2026-06-18, D-280~D-286)** — 본 ERD 는 결정 반영 후 갱신본:
- O-001 (D-280): `organization.business_type` enum 추가, N명 파트너 분배 지원
- O-002 (D-281): `notification_channel` 메타 테이블 + MVP=EMAIL/IN_APP
- O-003 (D-282): EXTERNAL_ACCOUNTANT enum 유지 (라우트 비활성), 세무사 export 리포트 중심
- O-004 (D-283): **모든 PK/FK = UUID** (v7 권장), 사용자 노출 ID 는 별도 `business_key`
- O-005 (D-284): `tax_policy` 확장 + `expenses.vat_inclusion_type` + tax_policy_id snapshot
- O-006 (D-285): `monthly_settlement.reopen_count` + `reopen_limit` (organization 설정), MVP 기본 3회
- O-007 (D-286): `product.option_*` 4컬럼 폐기 → `product_option_group` / `product_option_value` / `product_variant` 정규화

---

## 1. 공통 규약

### 1-1. 명명
- 테이블: 단수 snake_case (예: `product`, `sales_order`)
- **PK: `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`** (또는 app-side UUID v7)
- FK: `<참조테이블>_id UUID` (예: `product_id`)
- 사용자 노출 ID (상품코드, 주문번호, 정산번호): 별도 `business_key TEXT` 컬럼 (organization 내 UNIQUE)
- enum 컬럼: `<의미>_status`, `<의미>_type`
- 타임스탬프: `*_at TIMESTAMPTZ` (UTC 저장, 화면 Asia/Seoul 표시)
- Boolean: `is_*` (NULL 금지 + DEFAULT)

### 1-2. 모든 테이블 공통 컬럼
| 컬럼 | 타입 | 비고 |
|---|---|---|
| `id` | UUID PK DEFAULT gen_random_uuid() | v7 권장 (app-side `uuid7` 라이브러리) |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| `created_by` | UUID FK → `app_user.id` NULL | 시스템 자동행 NULL 허용 |
| `updated_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | 트리거 자동 갱신 |
| `updated_by` | UUID FK → `app_user.id` NULL | |
| `deleted_at` | TIMESTAMPTZ NULL | soft delete |
| `deleted_by` | UUID FK → `app_user.id` NULL | |

(이하 표에서는 audit 컬럼 반복 생략. **모든 테이블에 위 7컬럼 포함**)

### 1-3. 금액·비율 타입
- 금액: **`BIGINT`** (원 단위 정수). `NUMERIC` 은 외부 import 원본 보관용에 한해.
- 비율(수수료율·세율 등): **`NUMERIC(7,4)`** (예: 0.1100 = 11.00%)
- ID 타입: **`UUID`** (PK = `gen_random_uuid()` 또는 app-side v7). FK 도 UUID 통일.
- `business_key` (사용자 노출 식별자): TEXT, organization 내 UNIQUE 부분 인덱스
- 통화: 단일 `KRW` 가정. 컬럼은 추가하지 않음 (Post-MVP 시 추가).

### 1-4. 정책 테이블 공통 컬럼
| 컬럼 | 타입 |
|---|---|
| `org_id` | UUID FK → `organization.id` NOT NULL |
| `version` | INT NOT NULL |
| `effective_from` | DATE NOT NULL |
| `effective_to` | DATE NULL (`NULL` = 현재 유효) |
| `source_type` | enum_policy_source NOT NULL DEFAULT 'MANUAL' |
| `source_url` | TEXT NULL |
| `source_checked_at` | TIMESTAMPTZ NULL |
| `approved_by` | UUID FK → `app_user.id` NULL |
| `approved_at` | TIMESTAMPTZ NULL |
| `reliability_status` | enum_policy_reliability NOT NULL DEFAULT 'CONFIRMED' |
| `memo` | TEXT NULL |
| `is_active` | BOOLEAN NOT NULL DEFAULT TRUE |

### 1-5. 트리거
- 모든 테이블: `updated_at` 자동 갱신 트리거
- 정책 테이블: `effective_to` 일관성 검사 + 동시기간 중복 검사 트리거
- mutation 테이블: `audit_log` 자동 기록 트리거 (옵션) 또는 BE 서비스에서 호출

---

## 2. Enum 정의

```sql
CREATE TYPE enum_user_role AS ENUM (
  'ADMIN','OPERATOR','SOURCING','ACCOUNTING','READONLY','EXTERNAL_ACCOUNTANT'
);

CREATE TYPE enum_margin_type AS ENUM (
  'PURCHASE_AGENCY','ROCKET_GROWTH','OVERSEAS_DIRECT','GENERAL_RESELL'
);

CREATE TYPE enum_product_status AS ENUM (
  'CANDIDATE','APPROVED','ACTIVE','PAUSED','DISCONTINUED'
);

CREATE TYPE enum_sourcing_status AS ENUM (
  'CANDIDATE','APPROVED','REJECTED'
);

CREATE TYPE enum_sale_channel AS ENUM (
  'COUPANG','SMARTSTORE','ELEVENST','GMARKET','AUCTION','SELF_MALL','OTHER'
);

CREATE TYPE enum_settlement_status AS ENUM (
  'DRAFT','CONFIRMED','LOCKED','REOPENED'
);

CREATE TYPE enum_evidence_status AS ENUM (
  'NONE','NEEDS_CHECK','PRESENT','SUBMITTED_TO_ACCOUNTANT'
);

CREATE TYPE enum_evidence_type AS ENUM (
  'TAX_INVOICE','CASH_RECEIPT','CARD_SLIP','BANK_TRANSFER',
  'SIMPLE_RECEIPT','SELF_DECLARED','OTHER'
);

CREATE TYPE enum_payment_method AS ENUM (
  'BUSINESS_CARD','PERSONAL_CARD','BANK_TRANSFER','CASH','OTHER'
);

CREATE TYPE enum_expense_category_kind AS ENUM (
  'COGS','MARKETING','LOGISTICS','OFFICE','UTILITY','LABOR',
  'TAX','FEE','SUBSCRIPTION','OTHER'
);

CREATE TYPE enum_marketplace_fee_type AS ENUM (
  'RATE','FIXED','TIERED'
);

CREATE TYPE enum_tax_type AS ENUM (
  'GENERAL','SIMPLE','EXEMPT'
);

CREATE TYPE enum_policy_source AS ENUM (
  'MANUAL','CRAWLED','API','IMPORTED'
);

CREATE TYPE enum_policy_reliability AS ENUM (
  'CONFIRMED','AUTO_DETECTED','NEEDS_REVIEW','EXPIRED','MANUAL_OVERRIDE','DEPRECATED'
);

CREATE TYPE enum_audit_action AS ENUM (
  'CREATE','UPDATE','DELETE','RESTORE','STATUS_CHANGE','POLICY_OVERRIDE',
  'LOGIN','LOGOUT','PERMISSION_CHANGE','EXPORT','IMPORT'
);
-- M003: EXPORT/PERMISSION_CHANGE/IMPORT 추가 (app/core/audit.py AuditAction 과 1:1 동기화)

CREATE TYPE enum_notification_kind AS ENUM (
  'SETTLEMENT_DEADLINE','VAT_DEADLINE','MARGIN_DROP','STOCKOUT_RISK',
  'POLICY_REVIEW','ACCOUNTANT_COMMENT','SYSTEM'
);

CREATE TYPE enum_carryover_event AS ENUM (
  'LOSS_ADDED','OFFSET_APPLIED','MANUAL_ADJUST'
);

CREATE TYPE enum_distribution_kind AS ENUM (
  'PRIORITY','RATIO'
);

-- D-280: 사업 형태 (organization.business_type)
CREATE TYPE enum_business_type AS ENUM (
  'SOLE_PROPRIETOR','JOINT_BUSINESS','CORPORATION','TEAM_OPERATION'
);

-- D-281: 알림 채널 코드 (notification_channel.code)
-- 메타 테이블의 row code 와 1:1 매칭. MVP = 'EMAIL','IN_APP' 활성.
CREATE TYPE enum_notification_channel_code AS ENUM (
  'EMAIL','IN_APP','KAKAO','SLACK','TELEGRAM','WEBHOOK'
);

-- D-284: 부가세 포함 여부 (expenses, sales_order)
CREATE TYPE enum_vat_inclusion_type AS ENUM (
  'INCLUDED','EXCLUDED','EXEMPT'
);

-- D-284: 세금 계산 방식 (tax_policy.calculation_method)
CREATE TYPE enum_tax_calc_method AS ENUM (
  'STANDARD_RATIO','SIMPLE_DEEMED','EXEMPT','OVERSEAS_ZERO_RATED'
);
```

---

## 3. 영역별 테이블 정의

### 3-A. 조직 & 사용자

#### `organization`
| 컬럼 | 타입 | 제약 | 비고 |
|---|---|---|---|
| business_name | TEXT | NOT NULL | 픽썸 등 |
| business_no_masked | TEXT | NULL | 사업자번호 마스킹 표시값 |
| business_type | enum_business_type | NOT NULL DEFAULT 'JOINT_BUSINESS' | D-280: SOLE/JOINT/CORP/TEAM |
| address | TEXT | NULL | |
| primary_currency | TEXT | NOT NULL DEFAULT 'KRW' | |
| timezone | TEXT | NOT NULL DEFAULT 'Asia/Seoul' | |
| settlement_reopen_limit | INT | NOT NULL DEFAULT 3 | D-285: REOPEN 횟수 제한 (org 단위 override) |
| settings_json | JSONB | NOT NULL DEFAULT '{}' | 기타 org 설정 (확장용) |

- CHECK: `settlement_reopen_limit >= 0`

#### `app_user`
| 컬럼 | 타입 | 제약 | 비고 |
|---|---|---|---|
| org_id | UUID FK | NOT NULL | |
| email | TEXT | NOT NULL | UNIQUE (org_id, email) WHERE deleted_at IS NULL |
| password_hash | TEXT | NOT NULL | bcrypt/argon2 |
| display_name | TEXT | NOT NULL | |
| role | enum_user_role | NOT NULL | EXTERNAL_ACCOUNTANT 는 D-282 따라 MVP 라우트 비활성 |
| is_active | BOOLEAN | NOT NULL DEFAULT TRUE | |
| last_login_at | TIMESTAMPTZ | NULL | |
| invited_by | UUID FK→app_user.id | NULL | |
| invitation_token | TEXT | NULL | 가입 토큰 (해시) |
| invitation_expires_at | TIMESTAMPTZ | NULL | |

- INDEX: `(org_id, role, is_active)`, `(invitation_token)` UNIQUE
- CHECK: `password_hash !~ '^plain:'`

#### `partner`
파트너(공동대표·동업자·팀원 등 분배 대상). N명 지원.
| 컬럼 | 타입 | 제약 | 비고 |
|---|---|---|---|
| org_id | UUID FK | NOT NULL | |
| name | TEXT | NOT NULL | 파트너A / 파트너B (픽썸 seed 예시) |
| display_order | INT | NOT NULL DEFAULT 0 | |
| linked_user_id | UUID FK→app_user.id | NULL | 시스템 계정 매핑 (선택) |
| is_active | BOOLEAN | NOT NULL DEFAULT TRUE | |

- UNIQUE: `(org_id, name)` (deleted_at IS NULL)

---

### 3-B. 정책 테이블 (12종)

모든 정책 테이블은 1-4 의 공통 컬럼을 포함하며, 아래는 **고유 컬럼만** 표시.

#### `marketplace_fee_policy`
| 컬럼 | 타입 |
|---|---|
| marketplace | enum_sale_channel NOT NULL |
| category_path | TEXT NULL ("패션>여성>자켓" 같은 계층 문자열) |
| fee_type | enum_marketplace_fee_type NOT NULL |
| rate | NUMERIC(7,4) NULL |
| fixed_amount | BIGINT NULL |
| tier_json | JSONB NULL (TIERED용) |

- UNIQUE 부분: `(org_id, marketplace, category_path, version)`
- INDEX: `(org_id, marketplace, effective_from DESC)`
- CHECK: `fee_type = 'RATE' AND rate IS NOT NULL` OR `fee_type = 'FIXED' AND fixed_amount IS NOT NULL` OR `fee_type = 'TIERED' AND tier_json IS NOT NULL`

#### `payment_fee_policy`
| 컬럼 | 타입 |
|---|---|
| method | enum_payment_method NOT NULL |
| rate | NUMERIC(7,4) NOT NULL |

#### `tax_policy`
D-284 확장 — `business_type` 별, `calculation_method` 분기, 부가세 포함 여부 기본값 보관.
| 컬럼 | 타입 |
|---|---|
| tax_type | enum_tax_type NOT NULL |
| business_type | enum_business_type NULL | NULL = 전체 적용 |
| vat_rate | NUMERIC(7,4) NOT NULL DEFAULT 0.1000 | 법령 기본 10%, override 가능 |
| calculation_method | enum_tax_calc_method NOT NULL DEFAULT 'STANDARD_RATIO' |
| default_vat_inclusion | enum_vat_inclusion_type NOT NULL DEFAULT 'INCLUDED' | UI 기본값 |
| applies_to | TEXT NOT NULL | "전사" / "마켓:COUPANG" 등 단순 키 |

- UNIQUE 부분: `(org_id, tax_type, business_type, applies_to, version)`
- INDEX: `(org_id, effective_from DESC) WHERE effective_to IS NULL`

#### `fulfillment_fee_policy`
| 컬럼 | 타입 |
|---|---|
| provider | TEXT NOT NULL (예: '쿠팡 로켓그로스') |
| inbound_fee | BIGINT NULL |
| outbound_fee | BIGINT NULL |
| storage_fee_per_day | BIGINT NULL |
| return_handling_fee | BIGINT NULL |
| extra_json | JSONB NULL |

#### `return_risk_policy`
| 컬럼 | 타입 |
|---|---|
| marketplace | enum_sale_channel NULL |
| category_path | TEXT NULL |
| return_rate | NUMERIC(7,4) NOT NULL |
| reason_json | JSONB NULL (예: 변심/불량/교환 등 카테고리별 비율) |

#### `ad_cost_policy`
| 컬럼 | 타입 |
|---|---|
| marketplace | enum_sale_channel NOT NULL |
| category_path | TEXT NULL |
| target_roas | NUMERIC(7,4) NULL |
| default_ad_ratio | NUMERIC(7,4) NULL (판매가 대비 광고비 비율 기본값) |

#### `target_margin_policy`
| 컬럼 | 타입 |
|---|---|
| category_path | TEXT NULL |
| margin_type | enum_margin_type NULL |
| target_rate | NUMERIC(7,4) NOT NULL |
| warning_lower_rate | NUMERIC(7,4) NULL (이 값 미만이면 경고) |

#### `settlement_policy`
| 컬럼 | 타입 |
|---|---|
| loss_policy | TEXT NOT NULL DEFAULT 'CARRYOVER' (확장 가능) |
| priority_distribution_json | JSONB NULL ([{partner_id, fixed_amount, condition}]) |
| close_day | INT NOT NULL DEFAULT 5 (매월 N일 마감) |

#### `partner_share_policy`
| 컬럼 | 타입 |
|---|---|
| partner_id | UUID FK NOT NULL |
| share_ratio | NUMERIC(7,4) NOT NULL |
| applies_year_month | TEXT NULL ('YYYY-MM' override 시) |

- CHECK: 동일 (org_id, effective_from, applies_year_month) 의 share_ratio 합 = 1.0000 (BE 또는 deferred constraint)
- 픽썸 초기 seed (예시일 뿐): 파트너A 0.6, 파트너B 0.4. **다른 organization 은 자유롭게 N명 비율 설정 가능 (D-280)**

#### `expense_category_policy`
| 컬럼 | 타입 |
|---|---|
| name | TEXT NOT NULL |
| kind | enum_expense_category_kind NOT NULL |
| default_tax_deductible | BOOLEAN NOT NULL DEFAULT TRUE |
| default_settlement_deductible | BOOLEAN NOT NULL DEFAULT TRUE |
| sort_order | INT NOT NULL DEFAULT 0 |

#### `evidence_type_policy`
| 컬럼 | 타입 |
|---|---|
| code | enum_evidence_type NOT NULL |
| label | TEXT NOT NULL |
| is_official | BOOLEAN NOT NULL DEFAULT TRUE |
| description | TEXT NULL |

#### `policy_change_history`
모든 정책 변경의 통합 로그.
| 컬럼 | 타입 |
|---|---|
| org_id | UUID FK NOT NULL |
| policy_table | TEXT NOT NULL (테이블명 문자열) |
| policy_row_id | UUID NOT NULL |
| version | INT NOT NULL |
| change_action | enum_audit_action NOT NULL |
| before_json | JSONB NULL |
| after_json | JSONB NULL |
| reason | TEXT NULL |
| changed_by | UUID FK→app_user.id NOT NULL |
| changed_at | TIMESTAMPTZ NOT NULL DEFAULT now() |

- INDEX: `(org_id, policy_table, changed_at DESC)`

---

### 3-C. 정책 Snapshot

계산 결과 박제를 위한 하이브리드 (FK + JSONB).

#### `policy_snapshot`
하나의 계산(예: 마진 1건, 정산 1건)에 대해 사용된 모든 정책 row 를 묶음.
| 컬럼 | 타입 |
|---|---|
| org_id | UUID FK NOT NULL |
| owner_kind | TEXT NOT NULL ('MARGIN_EXPECTED' / 'MARGIN_ACTUAL' / 'SETTLEMENT_MONTHLY') |
| owner_id | UUID NOT NULL |
| computed_at | TIMESTAMPTZ NOT NULL DEFAULT now() |

- INDEX: `(owner_kind, owner_id)`, `(org_id, computed_at DESC)`

#### `policy_snapshot_ref`
하나의 snapshot 이 참조한 정책 row (FK).
| 컬럼 | 타입 |
|---|---|
| snapshot_id | UUID FK NOT NULL |
| policy_table | TEXT NOT NULL |
| policy_row_id | UUID NOT NULL |
| version | INT NOT NULL |

- INDEX: `(snapshot_id)`, `(policy_table, policy_row_id)`

#### `policy_snapshot_value`
실제 박제된 값 (JSONB) — 정책 row 가 사후 변경되어도 안전.
| 컬럼 | 타입 |
|---|---|
| snapshot_id | UUID FK NOT NULL UNIQUE |
| values_json | JSONB NOT NULL |

- `values_json` 예: `{"marketplace_fee_policy": {...전 컬럼...}, "tax_policy": {...}, ...}`

---

### 3-D. 상품

#### `product`
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| business_key | TEXT NOT NULL | 사용자 노출 상품코드 (organization 내 UNIQUE) |
| sku | TEXT NULL | 외부 마켓 SKU (참고용) |
| name_ko | TEXT NOT NULL | |
| name_en | TEXT NULL | 해외직구 |
| category_path | TEXT NULL | |
| margin_type | enum_margin_type NOT NULL | |
| status | enum_product_status NOT NULL DEFAULT 'CANDIDATE' | |
| supplier_id | UUID FK → external_partner.id NULL | |
| supply_url | TEXT NULL | |
| cost_amount | BIGINT NULL | 원가 (원). variants 가 있는 상품은 NULL 권장 (variant 별 cost) |
| cost_currency | TEXT NULL | 해외직구 시 |
| cost_fx_rate | NUMERIC(12,4) NULL | |
| has_variants | BOOLEAN NOT NULL DEFAULT FALSE | D-286: 옵션 사용 여부 |
| memo | TEXT NULL | |

- INDEX: `(org_id, status)`, `(org_id, margin_type)`
- UNIQUE 부분: `(org_id, business_key) WHERE deleted_at IS NULL`
- FTS INDEX: `name_ko` (Post-MVP)

#### `product_option_group` (D-286)
상품의 옵션 그룹 (예: "색상", "사이즈"). 상품당 N개.
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| product_id | UUID FK NOT NULL | |
| name | TEXT NOT NULL | "색상" / "사이즈" / "구성" |
| sort_order | INT NOT NULL DEFAULT 0 | |

- UNIQUE 부분: `(product_id, name) WHERE deleted_at IS NULL`
- INDEX: `(product_id, sort_order)`

#### `product_option_value` (D-286)
옵션 그룹의 값 (예: "색상" → "빨강", "파랑").
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| option_group_id | UUID FK NOT NULL | |
| value | TEXT NOT NULL | "빨강" / "M" / "10개입" |
| sort_order | INT NOT NULL DEFAULT 0 | |

- UNIQUE 부분: `(option_group_id, value) WHERE deleted_at IS NULL`
- INDEX: `(option_group_id, sort_order)`

#### `product_variant` (D-286)
옵션 조합 = 실제 판매 단위 (SKU). 옵션 없는 상품은 row 없음 또는 1행.
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| product_id | UUID FK NOT NULL | |
| variant_key | TEXT NOT NULL | "빨강/M" 같은 조합 키 (사용자 노출) |
| option_values_json | JSONB NOT NULL | `{"색상": "빨강", "사이즈": "M"}` |
| sku_external | TEXT NULL | 외부 마켓 SKU |
| cost_amount | BIGINT NULL | variant 별 원가 (NULL 시 product.cost_amount 사용) |
| sale_price | BIGINT NULL | variant 별 판매가 (NULL 시 추가금액 + product 기본가) |
| extra_price | BIGINT NOT NULL DEFAULT 0 | 옵션 추가금액 |
| stock_quantity | INT NULL | 옵션별 재고 (MVP 선택) |
| is_active | BOOLEAN NOT NULL DEFAULT TRUE | |

- UNIQUE 부분: `(product_id, variant_key) WHERE deleted_at IS NULL`
- INDEX: `(org_id, product_id, is_active)`

#### `product_sourcing_candidate`
소싱 후보 (`status = CANDIDATE` 의 상세).
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| product_id | UUID FK NULL | 승격 후 product 와 연결 |
| supplier_url | TEXT NOT NULL | |
| supplier_name_snapshot | TEXT NULL | |
| supply_price | BIGINT NULL | |
| moq | INT NULL | |
| category_path | TEXT NULL | |
| candidate_status | enum_sourcing_status NOT NULL DEFAULT 'CANDIDATE' | |
| rejected_reason | TEXT NULL | |
| simulated_margin_json | JSONB NULL | 후보 단계의 마진 시뮬레이션 결과 |
| memo | TEXT NULL | |

- INDEX: `(org_id, candidate_status)`

#### `external_partner`
공급처·거래처. (PRD 의 `/partners-external` 메뉴)
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| name | TEXT NOT NULL | |
| kind | TEXT NOT NULL | 'SUPPLIER' / 'CUSTOMER' / 'OTHER' |
| contact_masked | TEXT NULL | 마스킹 표시 |
| vault_entry_id | UUID FK→password_vault_entry.id NULL | 민감정보 보관 위치 |
| memo | TEXT NULL | |

---

### 3-E. 마진

#### `product_margin_expected`
정책 자동값 + 수동 override 기반의 **예상 마진**.
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| product_id | UUID FK NOT NULL | |
| product_variant_id | UUID FK → product_variant.id NULL | D-286: 옵션 있을 때 |
| margin_type | enum_margin_type NOT NULL | |
| marketplace | enum_sale_channel NOT NULL | |
| input_cost | BIGINT NOT NULL | |
| input_sale_price | BIGINT NOT NULL | |
| input_extra_json | JSONB NULL | 4종 타입별 추가 입력 |
| computed_marketplace_fee | BIGINT NOT NULL | |
| computed_payment_fee | BIGINT NOT NULL | |
| computed_ad_cost | BIGINT NOT NULL | |
| computed_fulfillment_fee | BIGINT NOT NULL | |
| computed_return_risk_cost | BIGINT NOT NULL | |
| computed_vat | BIGINT NOT NULL | |
| computed_margin_amount | BIGINT NOT NULL | |
| computed_margin_rate | NUMERIC(7,4) NOT NULL | |
| snapshot_id | UUID FK → policy_snapshot.id NOT NULL | |
| override_fields_json | JSONB NULL | 사용자가 수동 수정한 필드 + 사유 |
| valid_from | DATE NOT NULL | |
| valid_to | DATE NULL | |

- INDEX: `(org_id, product_id, valid_from DESC)`, `(snapshot_id)`, `(product_variant_id)`

#### `product_margin_actual`
정산 import 후 계산되는 **실 마진**.
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| product_id | UUID FK NOT NULL | |
| product_variant_id | UUID FK → product_variant.id NULL | D-286 |
| sales_order_id | UUID FK → sales_order.id NOT NULL UNIQUE | |
| settlement_record_id | UUID FK → marketplace_settlement_record.id NULL | |
| actual_revenue | BIGINT NOT NULL | |
| actual_cost | BIGINT NOT NULL | |
| actual_marketplace_fee | BIGINT NOT NULL | |
| actual_payment_fee | BIGINT NOT NULL | |
| actual_ad_cost | BIGINT NOT NULL | |
| actual_fulfillment_fee | BIGINT NOT NULL | |
| actual_return_cost | BIGINT NOT NULL | |
| actual_vat | BIGINT NOT NULL | |
| actual_margin_amount | BIGINT NOT NULL | |
| actual_margin_rate | NUMERIC(7,4) NOT NULL | |
| matched_expected_id | UUID FK → product_margin_expected.id NULL | |
| delta_margin_amount | BIGINT NULL (actual - expected, generated) | |
| delta_margin_rate | NUMERIC(7,4) NULL | |

- INDEX: `(org_id, product_id)`, `(sales_order_id)`, `(matched_expected_id)`

---

### 3-F. 판매 & 정산 import

#### `sales_order`
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| business_key | TEXT NOT NULL | 사용자 노출 주문번호 (org 내 UNIQUE) |
| sale_date | DATE NOT NULL | |
| marketplace | enum_sale_channel NOT NULL | |
| marketplace_order_no | TEXT NOT NULL | UNIQUE (org_id, marketplace, marketplace_order_no) |
| product_id | UUID FK NULL | NULL 허용 (매칭 안 된 import 건) |
| product_variant_id | UUID FK → product_variant.id NULL | D-286 |
| product_snapshot_name | TEXT NULL | import 시점 상품명 |
| quantity | INT NOT NULL CHECK (quantity > 0) | |
| sale_price | BIGINT NOT NULL | |
| ad_cost | BIGINT NOT NULL DEFAULT 0 | |
| shipping_fee | BIGINT NOT NULL DEFAULT 0 | |
| vat_inclusion_type | enum_vat_inclusion_type NOT NULL DEFAULT 'INCLUDED' | D-284 |
| tax_policy_id | UUID FK → tax_policy.id NULL | D-284: 적용 정책 snapshot 참조용 |
| memo | TEXT NULL | |
| import_source | TEXT NOT NULL DEFAULT 'MANUAL' | 'MANUAL' / 'CSV' / 'API' |
| import_batch_id | UUID FK→batch_run.id NULL | |

- INDEX: `(org_id, sale_date)`, `(org_id, marketplace, sale_date)`, `(org_id, product_id, sale_date)`

#### `marketplace_settlement_record`
쿠팡·스마트스토어 등에서 import 한 정산 기록.
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| marketplace | enum_sale_channel NOT NULL | |
| settlement_period_start | DATE NOT NULL | |
| settlement_period_end | DATE NOT NULL | |
| marketplace_order_no | TEXT NULL | |
| settled_at | DATE NULL | |
| gross_revenue | BIGINT NOT NULL | |
| marketplace_fee | BIGINT NOT NULL DEFAULT 0 | |
| ad_cost | BIGINT NOT NULL DEFAULT 0 | |
| return_cost | BIGINT NOT NULL DEFAULT 0 | |
| fulfillment_fee | BIGINT NOT NULL DEFAULT 0 | |
| other_deductions | BIGINT NOT NULL DEFAULT 0 | |
| net_settlement | BIGINT NOT NULL | |
| raw_json | JSONB NULL | 원본 CSV row |
| matched_sales_order_id | UUID FK→sales_order.id NULL | |
| import_batch_id | UUID FK→batch_run.id NULL |
| import_file_hash | TEXT NOT NULL | 중복 방지 |

- UNIQUE: `(org_id, marketplace, marketplace_order_no, settlement_period_start)` (deleted_at IS NULL)
- INDEX: `(org_id, settled_at)`, `(matched_sales_order_id)`, `(import_file_hash)`

---

### 3-G. 사업비용 & 증빙

#### `expenses`
D-284 적용 — `gross_amount` 명시, `vat_inclusion_type` 분기, `tax_policy_id` snapshot.
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| expense_date | DATE NOT NULL | |
| category_id | UUID FK→expense_category_policy.id NOT NULL | |
| description | TEXT NOT NULL | |
| gross_amount | BIGINT NOT NULL | 사용자 입력 총액 (= supply + vat 일관) |
| supply_amount | BIGINT NOT NULL | 공급가액 (자동/수동 분리) |
| vat_amount | BIGINT NOT NULL DEFAULT 0 | |
| vat_inclusion_type | enum_vat_inclusion_type NOT NULL DEFAULT 'INCLUDED' | D-284 |
| tax_type | enum_tax_type NOT NULL DEFAULT 'GENERAL' | |
| tax_policy_id | UUID FK→tax_policy.id NULL | 분리 시점 적용 정책 |
| is_manual_tax_override | BOOLEAN NOT NULL DEFAULT FALSE | 수동 수정 여부 |
| payment_method | enum_payment_method NOT NULL | |
| payment_card_holder_id | UUID FK→partner.id NULL | 결제자 (개인카드일 때) |
| is_personal_card | BOOLEAN NOT NULL DEFAULT FALSE | |
| is_business_use | BOOLEAN NOT NULL DEFAULT TRUE | 사적/사업 구분 |
| evidence_type | enum_evidence_type NOT NULL DEFAULT 'OTHER' | |
| evidence_status | enum_evidence_status NOT NULL DEFAULT 'NONE' | |
| is_tax_deductible | BOOLEAN NOT NULL DEFAULT TRUE | 세무공제 가능 |
| is_settlement_deductible | BOOLEAN NOT NULL DEFAULT TRUE | 정산공제 (순이익 차감) |
| attributable_partner_id | UUID FK→partner.id NULL | 귀속 파트너 |
| supplier_id | UUID FK→external_partner.id NULL | |
| memo | TEXT NULL | |
| linked_settlement_id | UUID FK→monthly_settlement.id NULL | 어느 월정산에 반영되었는지 |

- INDEX: `(org_id, expense_date)`, `(org_id, category_id, expense_date)`, `(org_id, evidence_status)`, `(org_id, linked_settlement_id)`
- CHECK: `is_personal_card = TRUE` 이면 `payment_card_holder_id IS NOT NULL`
- CHECK: `gross_amount = supply_amount + vat_amount` (BE 또는 트리거에서 강제. 환불·환출 음수 허용 — D-270)
- CHECK: `vat_inclusion_type = 'EXEMPT'` 이면 `vat_amount = 0`

#### `expense_attachment` (Post-MVP 슬롯)
파일 첨부 — 컬럼만 미리 둠.
| 컬럼 | 타입 |
|---|---|
| expense_id | UUID FK NOT NULL |
| storage_path | TEXT NULL |
| original_filename | TEXT NULL |
| file_hash | TEXT NULL |
| mime_type | TEXT NULL |
| uploaded_by | UUID FK→app_user.id NULL |
| uploaded_at | TIMESTAMPTZ NULL |

#### `expense_comment`
세무사·내부 코멘트. (D-282: MVP 에서는 내부 코멘트만 사용. 세무사 코멘트는 Post-MVP.)
| 컬럼 | 타입 |
|---|---|
| expense_id | UUID FK NOT NULL |
| author_id | UUID FK→app_user.id NOT NULL |
| body | TEXT NOT NULL |
| posted_at | TIMESTAMPTZ NOT NULL DEFAULT now() |

- INDEX: `(expense_id, posted_at DESC)`

---

### 3-H. 정산 (월별 + 파트너 + 손실 이월)

#### `monthly_settlement`
D-285 적용 — REOPEN 횟수 제한 + 마지막 REOPEN 메타.
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| business_key | TEXT NOT NULL | 정산번호 (예: "STL-2026-06"), org 내 UNIQUE |
| year_month | CHAR(7) NOT NULL | 'YYYY-MM' |
| status | enum_settlement_status NOT NULL DEFAULT 'DRAFT' | |
| total_revenue | BIGINT NOT NULL DEFAULT 0 | |
| total_direct_cost | BIGINT NOT NULL DEFAULT 0 | |
| total_common_expense | BIGINT NOT NULL DEFAULT 0 | |
| net_profit | BIGINT NOT NULL DEFAULT 0 | |
| carryover_offset | BIGINT NOT NULL DEFAULT 0 | 이번 달에 차감된 손실 이월 |
| distributable_amount | BIGINT NOT NULL DEFAULT 0 | 분배 대상 금액 |
| priority_distribution_total | BIGINT NOT NULL DEFAULT 0 | |
| ratio_distribution_total | BIGINT NOT NULL DEFAULT 0 | |
| reopen_count | INT NOT NULL DEFAULT 0 | D-285 |
| reopen_limit_override | INT NULL | NULL = organization.settlement_reopen_limit 사용 |
| last_reopened_at | TIMESTAMPTZ NULL | D-285 |
| last_reopened_by | UUID FK→app_user.id NULL | D-285 |
| last_reopen_reason | TEXT NULL | D-285 |
| confirmed_at | TIMESTAMPTZ NULL | |
| confirmed_by | UUID FK→app_user.id NULL | |
| locked_at | TIMESTAMPTZ NULL | |
| locked_by | UUID FK→app_user.id NULL | |
| snapshot_id | UUID FK→policy_snapshot.id NULL | |
| memo | TEXT NULL | |

- UNIQUE: `(org_id, year_month)` (deleted_at IS NULL)
- UNIQUE 부분: `(org_id, business_key) WHERE deleted_at IS NULL`
- INDEX: `(org_id, status, year_month DESC)`
- CHECK: `status IN ('CONFIRMED','LOCKED')` 이면 `confirmed_at IS NOT NULL AND confirmed_by IS NOT NULL`
- CHECK: `reopen_limit_override IS NULL OR reopen_limit_override >= 0`

#### `partner_settlement`
| 컬럼 | 타입 | 비고 |
|---|---|---|
| monthly_settlement_id | UUID FK NOT NULL | |
| partner_id | UUID FK NOT NULL | |
| distribution_kind | enum_distribution_kind NOT NULL | PRIORITY / RATIO |
| amount | BIGINT NOT NULL | |
| ratio_used | NUMERIC(7,4) NULL | RATIO 인 경우 |
| note | TEXT NULL | |

- UNIQUE: `(monthly_settlement_id, partner_id, distribution_kind)`
- INDEX: `(partner_id)`

#### `loss_carryover_ledger`
적자 누적·차감 원장.
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| event_at | DATE NOT NULL | |
| event_kind | enum_carryover_event NOT NULL | |
| source_settlement_id | UUID FK→monthly_settlement.id NULL | |
| amount | BIGINT NOT NULL | (+) 손실 추가 / (-) 차감 |
| balance_after | BIGINT NOT NULL | 잔액 (양수 = 아직 미차감 손실) |
| reason | TEXT NULL | |

- INDEX: `(org_id, event_at DESC)`

#### `settlement_reopen_audit`
D-285: 모든 REOPEN 이벤트 영구 보관.
| 컬럼 | 타입 |
|---|---|
| monthly_settlement_id | UUID FK NOT NULL |
| reopened_by | UUID FK→app_user.id NOT NULL |
| reopened_at | TIMESTAMPTZ NOT NULL DEFAULT now() |
| reason | TEXT NOT NULL |
| previous_status | enum_settlement_status NOT NULL |
| reopen_count_after | INT NOT NULL | 이 이벤트 적용 후 카운터 값 |
| snapshot_before_json | JSONB NULL |

- INDEX: `(monthly_settlement_id, reopened_at DESC)`

---

### 3-I. 세무사 협업 (리포트 묶음 전달)

#### `tax_report_bundle`
세무사 전달 묶음. D-282 MVP: download 위주 (sent_to_user_id 는 Post-MVP EXTERNAL_ACCOUNTANT 활성화 시 사용).
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| period_kind | TEXT NOT NULL | 'MONTH' / 'QUARTER' / 'YEAR' |
| period_label | TEXT NOT NULL | '2026-Q2' 등 |
| sent_to_user_id | UUID FK→app_user.id NULL | EXTERNAL_ACCOUNTANT (Post-MVP) |
| sent_at | TIMESTAMPTZ NULL | |
| received_at | TIMESTAMPTZ NULL | |
| storage_path | TEXT NULL | ZIP 위치 |
| memo | TEXT NULL | |

#### `tax_report_bundle_item`
| 컬럼 | 타입 |
|---|---|
| bundle_id | UUID FK NOT NULL |
| report_kind | TEXT NOT NULL ('MONTHLY_SALES' 등) |
| file_name | TEXT NOT NULL |
| file_hash | TEXT NULL |
| generated_at | TIMESTAMPTZ NOT NULL |

---

### 3-J. 횡단 — 감사 / 알림 / 비밀번호 vault

#### `audit_log`
모든 도메인 변경 로그.
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| domain_table | TEXT NOT NULL | |
| domain_row_id | UUID NOT NULL | |
| action | enum_audit_action NOT NULL | |
| actor_user_id | UUID FK→app_user.id NULL | 시스템 액션 NULL |
| occurred_at | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| before_json | JSONB NULL | |
| after_json | JSONB NULL | |
| reason | TEXT NULL | |
| request_id | TEXT NULL | API 요청 추적 |

- INDEX: `(org_id, domain_table, domain_row_id, occurred_at DESC)`
- INDEX: `(org_id, occurred_at DESC)`
- PARTITION: 월별 RANGE (occurred_at) — Post-MVP. MVP 는 단일 테이블 + 보관 정책 2년.

#### `notification_channel` (D-281)
알림 채널 메타 테이블 — 채널을 enum 하드코딩 대신 row 로 관리. MVP 활성 = EMAIL, IN_APP.
| 컬럼 | 타입 | 비고 |
|---|---|---|
| code | enum_notification_channel_code NOT NULL UNIQUE | EMAIL / IN_APP / KAKAO / SLACK / TELEGRAM / WEBHOOK |
| display_name | TEXT NOT NULL | "이메일" / "앱 내부 알림" |
| adapter_class | TEXT NOT NULL | BE 어댑터 클래스 path (예: 'app.services.notification.email.EmailChannel') |
| is_mvp_active | BOOLEAN NOT NULL DEFAULT FALSE | MVP 활성 여부 |
| requires_config_json | JSONB NOT NULL DEFAULT '[]' | 필요 env 키 (참고용) |
| sort_order | INT NOT NULL DEFAULT 0 |

- seed: EMAIL/is_mvp_active=true, IN_APP/is_mvp_active=true, KAKAO/SLACK/TELEGRAM/WEBHOOK/is_mvp_active=false

#### `notification`
| 컬럼 | 타입 |
|---|---|
| org_id | UUID FK NOT NULL |
| recipient_user_id | UUID FK→app_user.id NOT NULL |
| kind | enum_notification_kind NOT NULL |
| title | TEXT NOT NULL |
| body | TEXT NULL |
| channel_code | enum_notification_channel_code NOT NULL DEFAULT 'IN_APP' |
| target_url | TEXT NULL |
| sent_at | TIMESTAMPTZ NULL |
| delivery_status | TEXT NOT NULL DEFAULT 'PENDING' |
| read_at | TIMESTAMPTZ NULL | IN_APP 읽음 시각 (벨 카운트용) |
| error_message | TEXT NULL |
| meta_json | JSONB NULL |

- INDEX: `(recipient_user_id, sent_at DESC)`, `(org_id, kind, sent_at DESC)`
- INDEX: `(recipient_user_id, read_at) WHERE read_at IS NULL` (벨 미읽음 카운트)

#### `notification_pref`
| 컬럼 | 타입 |
|---|---|
| user_id | UUID FK NOT NULL UNIQUE |
| kind_settings_json | JSONB NOT NULL | `{ "SETTLEMENT_DEADLINE": true, ... }` |
| channel_settings_json | JSONB NOT NULL | `{ "EMAIL": true, "IN_APP": true, "KAKAO": false }` |

#### `password_vault_entry`
**비밀번호 평문 절대 저장 금지**. 위치·항목명만.
| 컬럼 | 타입 | 비고 |
|---|---|---|
| org_id | UUID FK NOT NULL | |
| service_name | TEXT NOT NULL | "쿠팡 Wing" |
| vault_provider | TEXT NOT NULL | "Bitwarden" / "1Password" / "기타" |
| vault_item_name | TEXT NOT NULL | Vault 안 항목명 |
| account_masked | TEXT NULL | 'cou***@picksome.co.kr' |
| memo | TEXT NULL | |

- CHECK: `memo !~* '비밀번호[ :=]*[A-Za-z0-9!@#$]+'` (단순 패턴 거부)
- CHECK: `memo !~* '(pw|pass|pwd)[ :=]+[A-Za-z0-9!@#$]+'`
- INDEX: `(org_id, service_name)`

---

### 3-K. 외부 연동 슬롯 (Post-MVP 준비)

DB·코드 구조만 미리 둠. MVP 에서는 채우지 않음.

#### `integration_account`
| 컬럼 | 타입 |
|---|---|
| org_id | UUID FK NOT NULL |
| provider | TEXT NOT NULL ('COUPANG','SMARTSTORE','DOMEGGOOK' 등) |
| account_label | TEXT NOT NULL |
| credentials_vault_entry_id | UUID FK→password_vault_entry.id NOT NULL |
| status | TEXT NOT NULL DEFAULT 'INACTIVE' |
| last_synced_at | TIMESTAMPTZ NULL |

#### `integration_job`
| 컬럼 | 타입 |
|---|---|
| account_id | UUID FK NOT NULL |
| job_kind | TEXT NOT NULL |
| schedule_cron | TEXT NULL |
| is_enabled | BOOLEAN NOT NULL DEFAULT FALSE |

#### `batch_run`
| 컬럼 | 타입 |
|---|---|
| job_id | UUID FK NULL |
| started_at | TIMESTAMPTZ NOT NULL DEFAULT now() |
| finished_at | TIMESTAMPTZ NULL |
| status | TEXT NOT NULL DEFAULT 'RUNNING' |
| affected_rows | INT NOT NULL DEFAULT 0 |
| summary_json | JSONB NULL |

#### `batch_run_log`
| 컬럼 | 타입 |
|---|---|
| batch_run_id | UUID FK NOT NULL |
| level | TEXT NOT NULL |
| message | TEXT NOT NULL |
| occurred_at | TIMESTAMPTZ NOT NULL DEFAULT now() |

#### `external_product_mapping`
| 컬럼 | 타입 |
|---|---|
| account_id | UUID FK NOT NULL |
| product_id | UUID FK NOT NULL |
| external_product_id | TEXT NOT NULL |
| last_synced_at | TIMESTAMPTZ NULL |

- UNIQUE `(account_id, external_product_id)`

#### `supplier_price_snapshot`
| 컬럼 | 타입 |
|---|---|
| supplier_id | UUID FK NOT NULL |
| external_product_id | TEXT NOT NULL |
| price | BIGINT NOT NULL |
| currency | TEXT NOT NULL |
| observed_at | TIMESTAMPTZ NOT NULL DEFAULT now() |

#### `marketplace_order_import` / `marketplace_settlement_import`
원본 CSV 행 저장 (raw_json). 정형화는 별도 변환 단계.

---

## 4. 관계 요약 (텍스트 ERD)

```
organization 1───* app_user
organization 1───* partner
organization 1───* (모든 정책 테이블, 모든 도메인 테이블)

product *───1 external_partner (supplier)
product 1───* product_option_group           [D-286]
product_option_group 1───* product_option_value
product 1───* product_variant                [D-286]
product 1───* product_margin_expected
product 1───* product_margin_actual
product 1───1 product_sourcing_candidate (옵션)

product_variant *───1 product
product_margin_* *───0..1 product_variant   [variant 있을 때]
sales_order *───0..1 product_variant

sales_order 1───1 product_margin_actual (선택)
sales_order *───1 product
sales_order 1───* marketplace_settlement_record (매칭)

notification_channel (meta) ←─ notification.channel_code

monthly_settlement 1───* partner_settlement
monthly_settlement 1───* expenses (linked_settlement_id)
monthly_settlement 1───* settlement_reopen_audit
monthly_settlement 1───1 policy_snapshot

policy_snapshot 1───* policy_snapshot_ref
policy_snapshot 1───1 policy_snapshot_value

expenses 1───* expense_attachment (Post-MVP)
expenses 1───* expense_comment
expenses *───1 expense_category_policy
expenses *───1 partner (attributable / payment_card_holder)

audit_log : 모든 도메인 mutation 1───* audit_log
notification *───1 app_user
password_vault_entry 1───* external_partner / integration_account
```

---

## 5. 인덱스 전략 (쿼리 패턴 기반)

PRD User Story 의 주 쿼리 패턴.

| Story | 쿼리 | 인덱스 |
|---|---|---|
| US-102 마진계산기 | "지금 effective 인 정책 row" | `(org_id, marketplace, effective_from DESC)` partial `effective_to IS NULL` |
| US-202 정산 import 매칭 | sales_order ↔ marketplace_settlement_record | sales_order `(org_id, marketplace, marketplace_order_no)` UNIQUE |
| US-203 월별 정산 집계 | sales_order WHERE org+sale_date 범위 | sales_order `(org_id, sale_date)` |
| US-203 expenses 합계 | expenses WHERE org+expense_date 범위 + is_settlement_deductible | expenses `(org_id, expense_date)` + partial `is_settlement_deductible = TRUE AND is_business_use = TRUE` |
| US-302 증빙 카운트 | expenses GROUP BY evidence_status | `(org_id, evidence_status)` |
| US-501 리포트 | monthly_settlement·sales_order·expenses 월/분기 범위 | 위 인덱스 + 월별 partial 인덱스 검토 |
| US-704 감사 로그 | audit_log WHERE org+domain_table+row_id ORDER BY occurred_at DESC | `(org_id, domain_table, domain_row_id, occurred_at DESC)` |

---

## 6. 데이터 정합성 규칙

### 6-1. 정책 시간 범위
1. 동일 (org_id, 정책키, effective 기간) 중복 금지 → exclusion constraint or 트리거
2. 새 버전 등록 시 이전 row 의 `effective_to = new.effective_from - 1`
3. 정책 row hard delete 금지 (`reliability_status = 'DEPRECATED'` 로 대체)

### 6-2. 정산 잠금
1. `status = LOCKED` 인 monthly_settlement 의 자식 (`partner_settlement`, `loss_carryover_ledger` 이벤트) 변경 금지 → 트리거 또는 BE 가드
2. expenses.linked_settlement_id 가 LOCKED 정산을 가리키면 그 expense 도 수정 금지

### 6-3. 분배 합계
1. partner_settlement 의 RATIO 분배 합 + PRIORITY 합 = monthly_settlement.distributable_amount (BE 검증)
2. partner_share_policy 의 share_ratio 합 = 1.0000 (effective 기간 + applies_year_month 단위)

### 6-4. 금액 단위
1. 모든 금액 컬럼 NOT NULL DEFAULT 0
2. supply + vat = total 일관성 (generated column 또는 트리거)
3. quantity, sale_price > 0 CHECK

### 6-5. 보안
1. `password_vault_entry.memo` 평문 비밀번호 패턴 거부 (CHECK)
2. `app_user.password_hash` 평문 패턴 거부 (CHECK)
3. seed/sample 데이터에 실제 계정 사용 금지 (운영 정책)

### 6-6. Soft delete
1. UNIQUE 제약은 partial: `WHERE deleted_at IS NULL`
2. 외래키 참조는 logical (BE 가 deleted_at 확인)

---

## 7. Migration Plan

### 7-1. 초기 스키마 (M001)
1. extension: `pgcrypto` (gen_random_uuid), `pg_trgm` (검색용)
2. enum 타입 전부 생성 (24종 — business_type/notification_channel_code/vat_inclusion_type/tax_calc_method 추가됨)
3. `organization`, `app_user`, `partner`
4. 12개 정책 테이블 + `policy_snapshot*`
5. 상품 + 옵션 정규화 (`product`, `product_option_group`, `product_option_value`, `product_variant`)
6. 마진 / 판매 / 정산 import / expenses (vat_inclusion_type, tax_policy_id 포함)
7. 정산 (`monthly_settlement` 등 — REOPEN 컬럼 보강)
8. 세무사 협업 (`tax_report_bundle*`)
9. 횡단 (`audit_log`, `notification_channel` 메타, `notification*`, `password_vault_entry`)
10. 외부 연동 슬롯
11. 공통 트리거 (`updated_at` 자동, 정책 effective_to 검증)

### 7-2. 초기 Seed (M002) — 픽썸 예시 (다른 organization 은 자유롭게 변경)
- `organization`: 픽썸 1행, `business_type='JOINT_BUSINESS'`, `settlement_reopen_limit=3`
- `partner`: 파트너A / 파트너B (N명 분배 예시)
- `app_user`: 관리자 1명 (초대 흐름으로 가입)
- `notification_channel`: EMAIL/IN_APP `is_mvp_active=true`, KAKAO/SLACK/TELEGRAM/WEBHOOK `is_mvp_active=false`
- `expense_category_policy`: 표준 카테고리 약 12개 (`_excel_analysis.md` 매직넘버 표 참조)
- `evidence_type_policy`: 7종
- `tax_policy`: 일반과세 v1 (`business_type='JOINT_BUSINESS'`, `vat_rate=0.10`, `calculation_method='STANDARD_RATIO'`, `default_vat_inclusion='INCLUDED'`)
- `partner_share_policy`: 파트너A 0.6, 파트너B 0.4 v1 (조직별 변경 가능)
- `settlement_policy`: loss_policy=CARRYOVER, priority_distribution=파트너A 1,000,000 v1
- `target_margin_policy`: 카테고리 미지정 기본 30%
- `marketplace_fee_policy`: COUPANG 일반 카테고리 10.8% 예시 (검증 필요)

### 7-3. 향후 Migration 정책
- Alembic 으로 관리. 한 PR = 1 migration 권장
- enum 추가는 `ALTER TYPE ADD VALUE` (PostgreSQL 12+)
- 컬럼 삭제는 deprecated 표시 후 1개 release 이상 유지
- 정책 row hard delete 금지 — DEPRECATED 로 표기

### 7-4. 백업·복구
- 일 1회 `pg_dump` (논리 백업)
- WAL 아카이빙 (Post-MVP)
- 정책 / 정산 / audit_log 는 export 후 별도 보관 권장

---

## 8. Open Items 영향 (DBA 관점) — 2026-06-18 모두 적용 완료

| Open Item | 적용 결과 |
|-----------|----------|
| O-001 사업 형태 (D-280) | `enum_business_type` 추가, `organization.business_type` NOT NULL DEFAULT JOINT_BUSINESS |
| O-002 알림 채널 (D-281) | `enum_notification_channel_code` 추가, `notification_channel` 메타 테이블 신설, `notification.channel_code` + `read_at` 컬럼 |
| O-003 세무사 합류 (D-282) | `EXTERNAL_ACCOUNTANT` enum 유지, `tax_report_bundle.sent_to_user_id` NULLABLE 유지 (MVP 미사용) |
| O-004 ID 타입 (D-283) | **모든 PK/FK = UUID**, `business_key` 컬럼 (product/sales_order/monthly_settlement) 추가, `pgcrypto` extension 필수 |
| O-005 부가세 자동분리 (D-284) | `enum_vat_inclusion_type` / `enum_tax_calc_method` 추가, `expenses.gross_amount/vat_inclusion_type/tax_type/tax_policy_id/is_manual_tax_override` 컬럼 추가, `total_amount` GENERATED 컬럼 폐기 (CHECK 로 일관성 강제), `tax_policy` 확장 (business_type/calculation_method/default_vat_inclusion) |
| O-006 REOPEN 횟수 (D-285) | `organization.settlement_reopen_limit DEFAULT 3`, `monthly_settlement.reopen_limit_override/last_reopened_at/last_reopened_by/last_reopen_reason` 컬럼, `settlement_reopen_audit.reopen_count_after` 추가 |
| O-007 옵션 슬롯 (D-286) | `product.option_*` 4컬럼 폐기, `product_option_group` / `product_option_value` / `product_variant` 3테이블 신설, `product_margin_*`·`sales_order` 에 `product_variant_id` 컬럼 추가, `product.has_variants` boolean |

**총 테이블 수**: 38 + 옵션 3 + notification_channel 1 = **약 42 테이블**
**enum 수**: 21 + 4 (business_type, notification_channel_code, vat_inclusion_type, tax_calc_method) = **25 enum**

---

## 9. Handoff

### Summary
12개 정책 + 도메인 + 횡단 = 약 **38개 테이블** 설계. 정책 snapshot 하이브리드, 정산 잠금, audit_log, 사적/사업 비용 구분, 비밀번호 vault, 외부 연동 슬롯 모두 포함.

### Decisions (DBA 단독 결정 + Open Items 7건 반영)
- **UUID PK** (D-283 — gen_random_uuid 기본, app-side v7 권장)
- 정책 snapshot = `policy_snapshot` + `policy_snapshot_ref`(FK 리스트) + `policy_snapshot_value`(JSONB)
- `expenses.total_amount` GENERATED 컬럼 **폐기** → `gross_amount` 직접 컬럼 + `CHECK (gross_amount = supply + vat)`
- `audit_log` 월별 파티셔닝은 Post-MVP, MVP 는 단일 테이블 + 인덱스
- enum **25종** 정의 (business_type/notification_channel_code/vat_inclusion_type/tax_calc_method 추가)
- 초기 seed 10개 항목 확정 (notification_channel 추가)
- 옵션 정규화 3테이블 (`product_option_group/value/variant`) + `business_key` 컬럼

### Open Questions
- ~~사용자 결정 (Open Items 7건)~~ — **2026-06-18 모두 Confirmed (D-280~D-286)**
- 정책 effective 기간 중복 방지를 트리거 vs CHECK exclusion 중 어느 방식 — BE 와 합의 필요 (현재 안: 트리거 + 부분 unique index)
- `audit_log` 자동 기록을 DB 트리거 vs BE 서비스 중 어디서 — BE 합의 완료: **서비스 레이어 명시 호출** (D-222)

### Risks
- 정책 테이블 12종 × 효력기간 관리 → 새 row 등록 시 휴먼 에러 가능 → UI(US-401) 에서 가드, BE 트랜잭션으로 보강
- snapshot JSONB 비대 → TOAST 의존, 정기 보관 정책 필요
- partition 부재 시 audit_log 폭증 → 2년 보관 후 archive 권장

### Required Inputs for Next Role
- **Backend**: 본 ERD 의 모든 테이블·enum·관계 기반으로
  - 서비스 함수 시그니처 (`get_active_policy_snapshot`, `calculate_expected_margin`, `calculate_actual_margin`, `calculate_monthly_settlement`, `generate_tax_report` 등)
  - REST endpoint 매핑 (PRD User Story → endpoint)
  - 트랜잭션 경계 / audit_log 기록 위치 정의
- **DevOps**: Postgres 16, 확장 (`pgcrypto`, `pg_trgm`), 백업 cron, env 변수

### Handoff Target
**Backend** (병렬) → Designer → Frontend → DevOps → User/세무 검증

---

## 10. Action Checklist

- [x] 25개 enum 정의 (Open Items 답변 후 +4)
- [x] 약 42개 테이블 정의 (조직/사용자/정책12/snapshot/상품+옵션3/마진/판매/정산/비용/세무사/감사/알림+notification_channel/vault/외부연동)
- [x] 모든 테이블 audit 컬럼 + soft delete + **UUID PK**
- [x] 금액 BIGINT / 비율 NUMERIC(7,4) 강제
- [x] PK/FK/UNIQUE/INDEX/CHECK 명시
- [x] 정책 snapshot 하이브리드 패턴
- [x] 정산 LOCKED 잠금 규칙 + REOPEN 3회 제한 (org 단위 override)
- [x] 보안 CHECK 제약 (vault/평문 거부)
- [x] Migration 초기 + seed 10개
- [x] business_key 컬럼 (사용자 노출 ID, UUID 노출 방지)
- [x] 옵션 정규화 (D-286)
- [x] tax_policy 확장 + vat_inclusion_type (D-284)
- [x] notification_channel 메타 테이블 + IN_APP read_at (D-281)
- [x] Handoff 작성
- [x] Open Items 7건 모두 반영 완료 (2026-06-18)
- [ ] (Next) Backend API 보강 (`docs/22_api_spec.md` 패치) → 코드 생성 진입
