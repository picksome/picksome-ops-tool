# 56. Sourcing Core 설계 (M037)

> 실제 Excel 을 읽고 정했다. 추측한 구조는 없다.

---

## 1. 실제 데이터에서 확인한 것

`D:\picksomeprod\마진계산기&장부.xlsx` (13 시트)

### 1-1. "구매처" 는 이름이 아니라 URL 이다

`판매 장부` 시트 3번째 열, **358종**:

```
https://domeggook.com/8618305?from=lstBiz
https://www.cheonyu.com/order/order_end_new.html
https://www.ownerclan.com/V2/service/orderList.php
https://www.tobizon.co.kr/mall/goods/goods_view.php?goodscd=DS01518184
https://domeme.domeggook.com/s/8618305
```

도메인은 소수(`domeggook.com`, `cheonyu.com`, `ownerclan.com`, `tobizon.co.kr`)이고
그 안의 개별 URL 이 많다. **사용자는 이미 "사이트" 와 "그 안의 상품" 을 구분해서
쓰고 있었다.** 그래서 스키마도 그렇게 나눈다.

```
supplier_site    사이트   (도메인 단위)
product_source   상품 URL (건별)
```

`www.` 는 정규화한다 — `domeggook.com` 과 `www.domeggook.com` 이 다른 공급처로
잡히면 같은 사이트가 둘로 갈라진다.

### 1-2. landed cost 항목

`해외 직구 마진계산기` 시트:

```
마켓명 / 마켓수수료 / 상품명 / 상품 URL / 판매금액 / 라쿠텐 비용 /
환율 / 원화 결제액 / 배대지비용 / 관부가세 / 기타비용 / 총 비용 /
정산예정금액 / 매출 / 수수료제외매출 / 부가세제외(간이) / 부가세제외(일반) / 마진률
```

여기서 원가 항목만 추린 것이 `product_source_cost_version` 이다.

**"관부가세" 를 그대로 쓰지 않았다.** 관세와 수입 부가세를 나눴다 — 수입 부가세는
공제 대상일 수 있고 관세는 아니다. 합쳐 두면 나중에 분리할 수 없다.
둘 다 nullable 이다(국내 소싱에는 없다).

### 1-3. 없는 파일

`도매 매입 사이트들.xlsx` 는 **workspace 에 없다.** 구조를 추측하지 않았고,
그 파일을 전제한 import mapping 도 만들지 않았다.

---

## 2. 세 가지 원가를 구분한다

```
SOURCE COST      지금 다시 사면 얼마인가      product_source_cost_version  ← 이번 구현
INVENTORY COST   보유 재고의 취득원가          (미구현)
ORDER COST       실제 판매에 귀속된 원가       product_margin_actual        (기존)
```

셋을 합치면 되돌릴 수 없다. 이번 구현은 **소싱 의사결정용**이다.

`test_new_source_cost_does_not_touch_past_sales_margin` 이 이 경계를 고정한다 —
새 원가를 넣어도 이미 팔린 건의 실마진은 그대로다.

---

## 3. 스키마

### supplier_site

| 컬럼 | 이유 |
|---|---|
| `supplier_code` | 이름을 바꿔도 관계가 안 깨진다. `SUP-000001`. **Excel 행 번호를 쓰지 않는다** |
| `website_domain` | 실제 데이터가 URL 이라 도메인으로 정규화 |
| `external_partner_id` | 비용 거래 상대방과 **선택적** 연결. 합치지 않는다 |
| `credential_present` | 계정이 있다는 사실만. **비밀번호 컬럼은 없다** |

`(org_id, supplier_code)` UNIQUE. **이름 중복은 허용한다** — 같은 이름의 다른
계정이 실제로 있다.

### product_source

한 상품이 소싱처를 여럿 가진다. `is_primary` 는 최대 하나:

```sql
CREATE UNIQUE INDEX uq_product_single_primary_source
    ON product_source (product_id)
 WHERE is_primary AND status = 'ACTIVE' AND deleted_at IS NULL
```

애플리케이션에서만 막으면 동시 요청 두 개가 각각 primary 를 세우고 둘 다
성공한다. 그 뒤로 예상 원가가 어느 쪽인지 알 수 없다.

교체는 **먼저 내리고 올린다.** 순서를 뒤집으면 잠깐 둘이 되어 UNIQUE 가 거부한다.

### product_source_cost_version

반열린 구간 `[valid_from, valid_to)` — 판매가 versioning 과 같은 규칙.
겹침은 DB 가 막는다:

```sql
EXCLUDE USING gist (
  product_source_id WITH =,
  daterange(valid_from, valid_to, '[)') WITH &&
) WHERE (deleted_at IS NULL)
```

같은 기간에 원가가 둘이면 "그날 얼마였나" 에 답할 수 없다.

### fx_rate_snapshot

`rate_type` 이 어느 국면의 환율인지 말한다:

```
SOURCING / PRICE_SETTING / PURCHASE / PAYMENT / ACCOUNTING
```

하나의 "현재 환율" 로 뭉개면 어느 시점 기준인지 말할 수 없다.
유형이 다르면 서로 대체하지 않는다(`test_rate_types_do_not_mix`).

---

## 4. 없는 값을 지어내지 않는다

```
환율 없음      → total_krw = None, missing = ["FX_RATE:CNY/KRW"]
원가 없음      → total_krw = None, missing = ["SOURCE_COST"]
부대비용 미입력 → 0 으로 보되 breakdown 에 provided=false 로 표시
```

앞의 둘을 0 으로 채우면 "원가 0원, 마진 100%" 가 나오고 화면에서는 그럴듯해 보인다.

부대비용은 다르다. 국내 소싱에 "국제배송비 없음" 으로 계산 불가를 띄우면
국내 상품은 영영 원가를 못 본다. 대신 무엇이 0인지 항목별로 보인다.

금액 계산에 `float` 를 쓰지 않는다 — `Decimal` + `ROUND_HALF_UP` (CLAUDE.md).

---

## 5. 통화 처리

```
매입가·현지 배송비   공급처 통화 (중국 내 배송비는 위안)
국제배송비·관세·수입VAT·포장·기타   원화 (국내에서 낸다)
```

`test_landed_cost_converts_foreign_components_only` 가 고정한다.

---

## 6. `product.cost_amount` 와의 관계 — **결정됨**

```
product_source_cost_version   공급처에서 관측된 사실
product.cost_amount           지금 계산에 공식적으로 적용 중인 값
product_margin_actual         이미 팔린 건에 귀속된 원가 (변하지 않는다)
```

**자동 동기화하지 않는다.** 새 소싱 원가가 생겨도 `product.cost_amount` 는
그대로다. 적용은 기존 `COST_CHANGE` 승인을 거친다:

```
소싱 원가 등록
  → landed cost 계산
  → 현재 적용 원가와 비교 (차이·비율만, 임계값은 만들지 않는다)
  → 사람이 판단
  → COST_CHANGE 승인 요청  (proposed_changes: {cost_amount: N})
  → 다른 사람이 승인 → apply
  → product.cost_amount 변경
```

적용 후에도 소싱 원가 이력은 남는다. 테스트가 셋을 고정한다:

| 테스트 | 고정하는 것 |
|---|---|
| `test_registering_source_cost_never_changes_product_cost` | 등록만으로는 안 바뀐다 |
| `test_applying_cost_goes_through_approval_and_preserves_history` | 승인 후에만 바뀌고 이력은 남는다 |
| `test_cost_apply_does_not_rewrite_past_sales_margin` | 과거 실마진은 그대로다 |

기존 `product.supply_url` 은 여전히 자동 backfill 하지 않는다. URL 만 보고
공급처를 만들면 근거 없는 법인이 생긴다. 데이터 품질 검사가
`PRODUCT_WITHOUT_SOURCE`(INFO)로 알려 주고 사용자가 직접 연결한다.

---

## 7. 권장가 — 새 산식을 만들지 않았다

기존 엔진이 계산한다:

```
price_sim.simulate_recommended_prices
  → reverse_calc_price_for_target_margin   (권장가 역산)
  → compute_expected_margin                (검증)
  → resolve_active_policies                (수수료·결제수수료·세금)
```

가격 구간 이름을 지어내지 않았다. 근거가 있는 것은 `target_margin_policy` 의
두 값뿐이다.

| key | 근거 |
|---|---|
| `break_even` | 목표 마진 0% (산식상 정의됨) |
| `minimum_recommended` | `warning_lower_rate` |
| `recommended` | `target_rate` |

정책이 없으면 손익분기 하나만 나온다. "공격적 판매가" 같은 이름은 만들지 않았다.

---

## 8. 없는 값을 지어내지 않는다

```
환율 없음        total_krw = null, missing = ["FX_RATE:CNY/KRW"]
원가 없음        missing = ["SOURCE_COST"]
세금 정책 없음    available = false, missing = ["POLICY:tax_policy"]   ← 500 이 아니다
부대비용 미입력   0 으로 보되 breakdown 에 provided=false
```

민감도는 **시나리오**다. `fx_rate_snapshot` 도 `cost version` 도 만들지 않는다 —
가짜 환율이 DB 에 남으면 나중에 실제 관측치처럼 보인다.

---

## 9. 권한

| | 소싱 메타데이터 | 매입가 |
|---|---|---|
| ADMIN / SOURCING | ✅ | ✅ |
| OPERATOR | ✅ | ✅ (원가 승인 주체) |
| ACCOUNTING | ✅ | ✅ |
| READONLY | ✅ | ❌ 가림 |
| EXTERNAL_ACCOUNTANT | ❌ | ❌ |

가림은 **API 응답에서** 한다. 금액을 `null` 로 지우되 `valid_from` 은 남긴다 —
아예 안 보이면 "원가 미등록" 으로 읽힌다. `landed-cost` 처럼 가릴 수 없는
계산 결과는 403 으로 아예 주지 않는다(0 으로 주면 거짓이 된다).

---

## 10. 라우트

`/sourcing/{item_id}`(기존 소싱 후보)가 `/sourcing/suppliers` 를 먼저 잡는다
— FastAPI 는 먼저 등록된 경로가 이긴다. 그래서 분리했다:

```
/supplier-sites            공급처
/product-sources/{id}      소싱처·원가·landed cost·권장가·민감도
/products/{id}/sources     상품별 소싱처
/fx-rates                  환율
```

---

## 11. 이번에 만든 것 / 만들지 않은 것

| | 상태 |
|---|---|
| supplier_site / product_source / cost version / fx snapshot (M037) | ✅ |
| Supplier API + 목록 화면 | ✅ |
| Product Source API (primary 교체 별도 endpoint) | ✅ |
| Cost Version API (append 전용, 수정 endpoint 없음) | ✅ |
| FX API (수동 입력) | ✅ |
| landed cost + 현재 원가 비교 | ✅ |
| 권장가 + 근거(basis) | ✅ |
| FX·원가 민감도 | ✅ |
| COST_CHANGE 승인 연결 | ✅ |
| 권한 + 매입가 가림 | ✅ |
| audit (5개 테이블) | ✅ |
| 데이터 품질 4종 | ✅ |
| Sourcing E2E 7건 | ✅ |
| **Product Drawer 소싱 탭** | ✅ live (소싱처·원가·환율·landed cost·권장가·민감도·이력·채택요청) |
| **공급처 Excel import** | ❌ 원본 파일 없음 |
| **external_system / mapping / connector** | ❌ |
| `SOURCE_COST_STALE` 검사 | ❌ 업무 기준 없음 |

미구현을 "완료" 로 보고하지 않는다.

---

## 12. 옛 소싱 탭이 저장하지 않던 값들

Drawer 소싱 탭을 live 로 바꾸면서 확인한 것:

옛 화면은 공급처를 **문자열 한 칸**으로 받고 관세·수입VAT·포장비·현지배송비를
입력받아 **프론트에서 총원가를 계산**해 보여줬다. 그런데 백엔드로 나가는 것은
넷뿐이었다.

```
supply_url / cost_amount / cost_currency / cost_fx_rate
```

나머지는 저장되지 않았다. 사용자가 관세를 입력하고 총원가를 확인한 뒤 저장해도
다시 열면 사라져 있었다.

지금은 그 항목들이 `product_source_cost_version` 에 실제로 저장되고,
예상 원가는 서버가 계산한다. 가격 탭의 인라인 미리보기는 **상품에 저장된
원가만** 쓴다는 사실을 화면에 명시했다.

---

## 13. E2E 날짜 함정 (2026-08-10 발견)

`new Date().toISOString()` 은 UTC 날짜를 준다. 백엔드는 `date.today()` 를
Asia/Seoul 로 쓴다. **00:00~09:00 KST 사이에는 하루 어긋난다.**

그 시간대에 "오늘" 로 만든 데이터는 서버가 보기엔 어제라서, 오늘부터 앞을 보는
조회(현금흐름 30일 등)에서 빠진다. 새벽에만 깨지는 테스트가 된다.

`e2e/db.ts` 의 `todayKst()` / `daysFromTodayKst()` 를 쓴다.
