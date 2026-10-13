# 46. 판매가 승인 스키마 설계 (초안 — 구현하지 않음)

> **이번 작업에서 구현하지 않는다.** M018 승인 대상은 `product` 의 원가·기본정보뿐이다.
> `product.sale_price` 컬럼을 임의로 추가하지 않고, 모델 선택지를 비교해 권장안만 남긴다.

## 왜 단일 컬럼이 안 되나

판매가는 **상품 하나에 값 하나**가 아니다. 실제로는 최소 이 차원들이 곱해진다.

| 차원 | 예 |
|---|---|
| 판매처 | 쿠팡 / 스마트스토어 / 11번가 |
| 판매처 계정 | 같은 마켓에 스토어 2개 |
| 상품 / 옵션 | 옵션마다 추가금이 다르다 |
| 가격 종류 | 정상가 / 판매가 / 할인가 |
| 기간 | 적용 시작·종료일 |
| 이력 | 과거 주문이 참조한 가격 |

`product.sale_price` 하나를 두면 "쿠팡은 29,900, 스마트스토어는 28,500" 을 표현할 수 없고,
과거 주문의 가격 재현도 불가능하다.

## 모델 선택지

### A. `product_channel_price` (상품 × 판매처)
```
product_channel_price(
  id, org_id, product_id, marketplace, integration_account_id NULL,
  list_price, sale_price, discount_amount,
  effective_from DATE, effective_to DATE NULL,
  version, is_active, ...감사
)
```
- 장점: 가장 단순. 판매처별 가격을 바로 표현한다
- 단점: **옵션별 가격을 표현할 수 없다.** 옵션 추가금이 마켓마다 다르면 못 담는다

### B. `product_variant_channel_price` (옵션 × 판매처)
```
product_variant_channel_price(
  id, org_id, product_id, product_variant_id NULL, marketplace, ...
)
```
- `product_variant_id IS NULL` = 상품 기본가, NOT NULL = 옵션 가격
- 장점: 상품/옵션 단위를 하나의 테이블로 처리. 현재 `product_variant` 구조와 맞는다
- 단점: 상품 단위 가격만 쓰는 조직에도 variant 개념이 새어 들어온다

### C. `product_channel_listing` + `product_channel_price` (분리)
```
product_channel_listing(id, org_id, product_id, marketplace, marketplace_product_no, is_active)
product_channel_price(id, listing_id, product_variant_id NULL, list_price, sale_price,
                      effective_from, effective_to, ...)
```
- 장점: **"어느 마켓에 올라가 있나"(listing)** 와 **"얼마에 파나"(price)** 를 분리.
  판매처 상품번호·동기화 상태가 listing 에 자연스럽게 붙는다.
  현재 `ProductChannel` FE 타입(marketplace / marketplaceProductNo / listingUrl / lastSyncedAt)과 일치
- 단점: 테이블 2개. 조인이 늘어난다

### D. `product_price_history` 별도
어느 안을 쓰든 **이력은 별도 테이블로 두지 않는다** — `effective_from/to` + `version` 으로
정책 테이블 11종과 같은 패턴을 쓴다. 별도 history 테이블은 현재 코드베이스에 없는 개념이라
새로 도입하면 정책 snapshot 방식과 어긋난다.

## 권장안: **C (listing + price 분리)**, 가격에 `product_variant_id NULL 허용`

근거:
1. FE 는 이미 `ProductChannel`(판매처 + 상품번호 + 동기화 시각) 개념을 갖고 있다.
   listing 테이블이 그 자리에 정확히 들어간다.
2. 쇼핑몰 API 동기화(Phase 7)가 listing 단위로 동작한다 — 상품번호가 여기 있어야 한다.
3. 가격에 `effective_from/to` 를 두면 **과거 주문 가격 재현**이 정책 snapshot 과 같은 방식이 된다.
4. `product_variant_id NULL` 로 상품 단위와 옵션 단위를 한 테이블에서 처리한다.

## 승인 워크플로 연결

```
target_type = "product_channel_price"
request_type = "PRICE_CHANGE"
allowed_fields = {list_price, sale_price, discount_amount, effective_from, effective_to}
get_effective_date() → effective_from (월 잠금 판정 기준)
get_current_version() → 해당 가격 행의 스냅샷 해시
```

어댑터 하나를 추가하면 된다. **승인 서비스는 건드리지 않는다.**

## 마진 계산기 연결

`lib/calc/engine.ts` 의 `selling.salePrice` 입력이 이 테이블에서 온다.
현재는 `product.sale_price` 가 없어 FE mock 이 값을 들고 있다.

## 과거 주문 가격 snapshot

`sales_order.sale_price` 는 이미 **판매 시점 값을 그대로 박제**하고 있다(M011).
가격 테이블이 생겨도 과거 주문을 재계산하지 않는다.

## migration 초안 (실행하지 않음)

```sql
-- M0xx (번호 미정)
CREATE TABLE product_channel_listing (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL REFERENCES organization(id),
  product_id UUID NOT NULL REFERENCES product(id),
  marketplace TEXT NOT NULL,
  integration_account_id UUID REFERENCES integration_account(id),
  marketplace_product_no TEXT,
  listing_url TEXT,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  last_synced_at TIMESTAMPTZ,
  ...감사 컬럼
);
CREATE UNIQUE INDEX ux_listing_target
  ON product_channel_listing (org_id, product_id, marketplace, COALESCE(integration_account_id, '00000000-0000-0000-0000-000000000000'))
  WHERE deleted_at IS NULL;

CREATE TABLE product_channel_price (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  org_id UUID NOT NULL REFERENCES organization(id),
  listing_id UUID NOT NULL REFERENCES product_channel_listing(id),
  product_variant_id UUID REFERENCES product_variant(id),   -- NULL = 상품 기본가
  list_price BIGINT, sale_price BIGINT NOT NULL, discount_amount BIGINT,
  effective_from DATE NOT NULL, effective_to DATE,
  version INTEGER NOT NULL DEFAULT 1,
  ...감사 컬럼
);
CREATE INDEX ix_price_effective
  ON product_channel_price (org_id, listing_id, product_variant_id, effective_from DESC);
```

## 선행 확인 필요

1. 같은 마켓에 스토어 계정이 2개 이상인가 → `integration_account_id` 필요 여부
2. 옵션별 가격을 마켓마다 다르게 두는가 → `product_variant_id` 실사용 여부
3. 할인가를 별도 필드로 관리하는가, 판매가에 반영해 넣는가
4. 가격 변경이 마켓 API 로 자동 반영되는가, 사람이 마켓에서 직접 바꾸는가
   (후자면 이 테이블은 "우리가 아는 가격" 이지 "실제 게시 가격" 이 아니다 — 대사가 필요하다)

전부 업무 결정이라 코드로 확정하지 않는다.

---

## 결정 (2026-08-06) — migration 은 여전히 실행하지 않는다

앞의 4개 선행 확인은 **업무 결정**이라 코드로 확정할 수 없다. 다만 그 답이 무엇이든
바뀌지 않는 부분이 있어서, 그만큼은 확정하고 나머지는 명시적으로 열어 둔다.

| # | 항목 | 결정 | 근거 |
|---|---|---|---|
| 17-1 | 판매가를 `product` 에 단일 컬럼으로 두는가 | **아니오 — 확정** | 같은 상품이 쿠팡과 스마트스토어에서 다른 가격이다. 단일 컬럼은 둘 중 하나를 지운다. 이건 업무 답변과 무관하게 참이다 |
| 17-2 | 모델 | **C (listing + price 분리)** | 게시 상태(살아있는 리스팅)와 가격 이력은 수명이 다르다. 한 테이블에 두면 가격 이력을 쌓을 때마다 리스팅 행이 늘어난다 |
| 17-3 | 옵션별 가격 | `product_variant_id` **NULL 허용**, 실사용은 보류 | NULL = "상품 단위 가격". 컬럼을 처음부터 두면 나중에 옵션별 가격이 필요해져도 migration 이 아니라 데이터 추가로 끝난다. 지금 쓰지 않는다고 컬럼을 빼면 그때 테이블을 다시 만들어야 한다 |
| 17-4 | 승인 대상 여부 | **판매가 변경은 승인 대상** | 판매가는 마진을 직접 바꾼다. 원가를 승인 태우면서 판매가를 열어두면 승인은 절반만 작동한다. 단, 대상 필드는 스키마가 생긴 **뒤에** `FIELD_OWNERSHIP` 에 등록한다 |

### 아직 확정하지 않은 것 — 그리고 왜

| 항목 | 왜 코드로 못 정하나 |
|---|---|
| 한 마켓 복수 스토어 계정 | 픽썸이 실제로 스토어를 몇 개 쓰는지가 답이다. 하나라고 가정하고 만들면 두 번째 스토어가 생길 때 UNIQUE 제약이 막는다 |
| 할인가 별도 관리 | 쿠팡 즉시할인·쿠폰을 판매가에 녹여 관리해 왔는지, 따로 봤는지에 따라 과거 데이터 해석이 달라진다 |
| 가격 자동 반영 여부 | 사람이 마켓에서 직접 바꾼다면 이 테이블은 **"우리가 아는 가격"**이지 "실제 게시 가격"이 아니다. 그러면 대사(reconciliation) 화면이 추가로 필요하다 |

**결론: migration 은 실행하지 않는다.** 위 3개가 UNIQUE 제약과 테이블 개수를 바꾼다.
확정 전에 만들면 되돌리는 비용이 만드는 비용보다 크다.

### 그동안 화면은 어떻게 처리하나

`ProductDetail.unsupportedFields` 에 미지원 항목을 담아 내려보내고,
소싱·가격·판매처 탭 상단에 **"화면의 0 은 실제 값이 아니라 미입력"** 을 명시한다.

0 을 진짜 값처럼 보여주면 사용자가 관세가 0원이라고 믿고 판매가를 정한다.
빈 화면보다 나쁘다 — 빈 화면은 없다는 걸 알려주지만, 0 은 있다고 거짓말한다.

---

# 구현 완료 (2026-08-06) — M022 + 후속 설계

## 1. 확정된 업무 규칙 (사용자 결정)

| 항목 | 결정 | 스키마 반영 |
|---|---|---|
| 복수 스토어 | 한 판매처에 판매자 계정 여럿 지원 | `listing.integration_account_id` (마켓 문자열 아님) |
| 가격 종류 | 정상가 / 판매자 직접 할인가 분리 | `regular_price` / `sale_price` + 기간 |
| 쿠폰 | 판매처·구매자 쿠폰은 가격 테이블에 **없다** | 주문 snapshot 관심사 |
| 외부 반영 | 승인 완료와 판매처 전송 **분리**, 기본 수동 | `publish_status`, `auto_publish_price=FALSE` |
| 미입력 | 0 이 아니라 **NULL** | 가격 컬럼 NULL 허용 + `> 0` CHECK |

## 2. M022 구현 — 결정과 근거

### 유일성
```
UNIQUE(org, integration_account_id, external_product_id) WHERE external IS NOT NULL
UNIQUE(org, integration_account_id, product_id)
```
- 외부 상품번호는 **그 스토어 안에서만** 유일하다. org 전체로 걸면 다른 스토어의
  같은 번호를 넣지 못한다.
- 등록 전 리스팅은 `external_product_id IS NULL` — 부분 인덱스라 여럿 허용된다.
- 내부 상품 하나가 같은 계정에 두 번 걸리는 건 중복 등록이고 가격이 갈라진다.

### 기간 중복 — 서비스가 아니라 DB
```sql
EXCLUDE USING gist (
  listing_id WITH =, variant_scope WITH =,
  daterange(effective_from, effective_to, '[)') WITH &&
) WHERE (status = 'ACTIVE' AND deleted_at IS NULL)
```

| 결정 | 이유 |
|---|---|
| `variant_scope` 생성 컬럼 | SQL 에서 `NULL = NULL` 은 참이 아니다. 상품 기본 가격(variant NULL)끼리는 exclusion 이 그냥 통과한다. nil UUID 로 접어 비교한다 |
| 반개구간 `[from, to)` | 닫힌 구간이면 3/1 종료 · 3/1 시작이라는 **정상적인 가격 교체**가 막힌다 |
| `status='ACTIVE'` 만 검사 | 지난 가격(SUPERSEDED)은 과거 기록이다. 덮어쓰면 과거 주문 손익을 재현할 수 없다 |
| currency 를 키에서 제외 | 넣으면 같은 기간에 KRW·USD 가격이 공존해 "지금 가격" 이 둘이 된다 |

### 옵션 가격 우선순위
```
variant 가격 레코드 있음  → 그 값이 **완전 판매가** (기본가 + 추가금 아님)
variant 가격 레코드 없음  → 상품 기본 가격(variant_id IS NULL)
```
옵션 추가금 표시가 필요하면 `variant 정상가 − 기본 정상가` 로 계산한다.
**DB 의 source of truth 는 완전 가격이다.**

### 금액 단위
`BIGINT` + `currency` (ISO 3자리 CHECK). 통화의 최소 단위 정수(KRW = 원).
`float` 금지 — 프로젝트 money 규칙 그대로다.

## 3. Phase 5 주문 snapshot — **설계만, 미구현**

### 현재 `sales_order` 에 이미 있는 것
`sale_price`(실제 결제 단가), `shipping_fee`, `quantity`, `product_id`,
`product_variant_id`, `product_snapshot_name`, `purchase_cost`, `marketplace`

### 없는 것
`listing_id`, `channel_price_id`, `regular_price_snapshot`, `sale_price_snapshot`,
`seller_discount`, `marketplace_discount`, `buyer_coupon`, `currency`,
`external_product_id`, `external_variant_id`

### 이번 migration 에 넣지 않은 이유
아직 **리스팅이 하나도 없다.** 컬럼을 지금 추가하면 전부 NULL 인 채로 남고
채워 넣을 코드도 없다. 그 상태에서 "snapshot 이 있다" 고 보이면 다음 사람이
값이 들어 있다고 믿는다.

`listing_id`·`channel_price_id` 를 채우려면 주문 import 경로가 리스팅을 찾아
매칭해야 하는데, 그건 마켓 주문 import 개편과 함께 가야 할 작업이다.

**과거 손익 불변 요건은 이미 지켜지고 있다** — `sales_order.sale_price` 와
`purchase_cost` 가 주문 시점 값으로 박제되고, 가격 테이블을 참조하지 않는다.
가격을 바꿔도 과거 주문 손익은 변하지 않는다.

## 4. Phase 6 가격 승인 — **설계 확정, 어댑터 미구현**

### target 선택 비교

| 안 | 문제 |
|---|---|
| `target_type = product` | 열린 요청 UNIQUE 가 `(org, target, request_type)` 이라 **판매처가 둘이면 한쪽만 요청 가능**하다 |
| `target_type = product_channel_price` | 아직 없는 행을 대상으로 요청해야 한다 — 가격 신규 등록을 표현할 수 없다 |
| **`target_type = product_channel_listing`** ← 채택 | 리스팅은 이미 존재하고, 판매처별로 독립 요청이 된다 |

### 적용 의미
```
request_type : CHANNEL_PRICE_CHANGE
target       : product_channel_listing
적용 결과    : 기존 가격 UPDATE 가 아니라 **새 가격 버전 INSERT**
             + 이전 ACTIVE 버전을 SUPERSEDED 로 전환
```
과거 가격 레코드를 덮어쓰지 않는다. 덮어쓰면 "그때 얼마에 팔았나" 를 잃는다.

### 필드 소유권 (`FIELD_OWNERSHIP` 에 추가할 항목)
`regular_price`, `sale_price`, `sale_start_at`, `sale_end_at`,
`effective_from`, `effective_to`, `product_variant_id`, `currency`

### 민감 필드 등록
가격은 원가와 함께 마진을 결정한다 → `SENSITIVE_FIELDS["product_channel_listing"]` 에
가격 4종을 등록해야 한다. **미구현.**

### 승인 완료 ≠ 외부 전송
가격 승인이 끝나도 판매처에 즉시 보내지 않는다. 적용 결과는 **내부 가격 버전 생성**이다.

## 5. Phase 7 판매처 반영 — **상태만 구현, job 러너 미구현**

### 구현된 것
`publish_status` 6종 CHECK (`NOT_REQUESTED/QUEUED/PUBLISHING/PUBLISHED/FAILED/VERIFICATION_FAILED`),
`publish_requested_at`, `published_at`, `publish_error`, `publish_attempt_count`,
`listing.auto_publish_price` (기본 FALSE), 큐 조회용 부분 인덱스

### 미구현 — 그리고 이유
| 항목 | 상태 |
|---|---|
| publish job 러너 | **미구현.** `integration_job` 은 cron **스케줄 정의** 테이블이지 작업 큐가 아니다. 재사용할 수 없어 별도 outbox 테이블이 필요하다 |
| 쿠팡·네이버 가격 API | **미연결.** 실제 자격증명이 없다. connector interface 도 만들지 않았다 — 껍데기만 있으면 "연결됐다" 로 오해된다 |
| 반영 후 재조회 검증 | 미구현 (`VERIFICATION_FAILED` 상태값만 예약) |

**승인 적용 트랜잭션에서 외부 API 를 직접 호출하지 않는다**는 원칙은 스키마로 강제된다 —
적용은 가격 행 INSERT 까지고, `publish_status` 는 `NOT_REQUESTED` 로 남는다.

## 6. Phase 8 가격 UI — **미구현**

표시 대상(판매처 계정·외부 상품번호·정상가·기간 할인가·적용일·내부 가격 상태·
외부 반영 상태·마지막 반영 시각·반영 오류)과 "미입력" 표기 규칙은 위 스키마로 전부
표현 가능하다. 화면은 만들지 않았다.
