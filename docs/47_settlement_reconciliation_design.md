# 47. 쇼핑몰 정산 대사 — 설계 (구현 전)

> 작성 2026-08-07 · 선행: `43_ops_stability_plan.md`, `44_ops_stability_worklog.md`
> **이 문서는 설계까지다.** 승인 target 도, 매칭 로직도 이번 작업에서 만들지 않았다.

---

## 0. 요약 — 지금 상태에서 바로 말할 수 있는 것

| 질문 | 결론 |
|---|---|
| A. 자동 매칭 key | **라인 단위 exact key 가 없다.** 주문번호는 1:N 이다. 주문 단위 exact match 만 가능하고, 그마저 다건이면 `AMBIGUOUS` 로 남긴다 |
| B. 수동 매칭 | 승인 대상. `SETTLEMENT_MANUAL_MATCH` (신규 target_type `settlement_record`) |
| C. 확정 vs 월 마감 | **3단계를 분리한다** — 정산 건 confirmed / 월 close confirmed / 월 locked |
| D. 확정 정산 정정 | UPDATE 금지. **비용 조정과 같은 모델**의 `settlement_adjustment` 가 필요하다 |
| E. import 재실행 | 현재 UNIQUE 키로는 **덮어쓰기 사고가 난다.** raw source identity + idempotency key 컬럼이 선행 조건 |

---

## 1. 현재 구조 — 코드·스키마 확인 결과

### 1-1. 테이블

| 테이블 | 행 수(개발 DB) | 상태 |
|---|---|---|
| `sales_order` | **374** | live. 실데이터 있음 |
| `marketplace_settlement_record` | **0** | 스키마만. 한 번도 쓰이지 않음 |
| `marketplace_settlement_import` | 0 | `raw_json` 만 있는 빈 껍데기 |
| `integration_account` | **0** | API import 경로가 실질적으로 없음 |
| `batch_run` / `batch_run_log` | — | import 배치 기록용, 연결 안 됨 |

`marketplace_settlement_record` 의 실제 컬럼:

```
marketplace, settlement_period_start/end, marketplace_order_no(NULL 허용),
settled_at, gross_revenue, marketplace_fee, ad_cost, return_cost,
fulfillment_fee, other_deductions, net_settlement,
raw_json, matched_sales_order_id, import_batch_id, import_file_hash
```

**없는 것**(이번 분석의 핵심):
- `matching_status` — 지금은 `matched_sales_order_id IS NULL` 하나로만 구분한다.
  "아직 안 해봄", "찾았는데 없음", "후보가 여럿", "사람이 강제 연결함" 이
  **전부 같은 NULL** 이다
- `expected_settlement_amount` — 기대 정산액을 저장할 자리가 없다
- 정산 건 자체의 확정 상태 (`confirmed_at` 등)
- 외부 레코드 식별자 / idempotency key
- 라인 식별자 (아래 §2)

### 1-2. 현재 대사 리포트

`coupang_reconcile`(세무 리포트 7종 중 하나)가 이미 있다.
`marketplace_settlement_record LEFT JOIN sales_order ON matched_sales_order_id` 로
정산액과 매칭된 판매금액을 나란히 보여준다.

**즉 대사 "표시" 는 있고 대사 "판정" 이 없다.** 매칭을 누가 어떻게 채우는지가 빈칸이다.

### 1-3. 월 마감 (M017 · 이미 live)

`monthly_settlement.close_state` = `OPEN / REVIEW / CONFIRMED / LOCKED`
`monthly_settlement.status` = `DRAFT / CONFIRMED / LOCKED / REOPENED`
`reopen_count` + 한도 3회(D-285).

이 구조는 **월** 단위다. 정산 건 단위 상태는 여기에 없다.

---

## 2. A. 자동 매칭 key — **exact key 가 없다**

### 2-1. 주문번호는 1:N 이다

```sql
SELECT count(*), count(DISTINCT marketplace_order_no) FROM sales_order;
-- 374,  364
```

10건이 중복이다. M012 가 `line_no` 를 추가한 이유가 이것이다 —
**한 마켓 주문에 여러 상품 라인이 들어온다.**

```
sales_order  UNIQUE (org_id, marketplace, marketplace_order_no, line_no)
msr          UNIQUE (org_id, marketplace, COALESCE(order_no,''), period_start)
```

정산 레코드에는 `line_no` 도, 상품 식별자도 없다. 그래서:

| 상황 | 매칭 가능성 |
|---|---|
| 주문에 라인이 1개 | `(marketplace, order_no)` 로 **exact 1:1** |
| 주문에 라인이 2개 이상 | 어느 라인의 정산인지 **알 수 없다** |
| 정산서가 주문 단위 합계 | 라인에 나눠 붙일 근거가 없다 |

### 2-2. 그래서 하지 않을 것

**추측 매칭을 구현하지 않는다.** 구체적으로 다음을 전부 금지한다.

- 금액 근사치 매칭 (`ABS(정산액 - 판매액) < 1000`)
- 날짜 근접 매칭 (정산일 ±N일 안의 주문)
- 상품명 문자열 유사도
- 라인이 여럿일 때 "첫 번째 라인" 에 붙이기
- 금액 비례 배분

이유는 하나다. **틀린 매칭은 틀린 손익이 되고, 그 손익이 파트너 수익배분이 된다.**
그리고 잘못 붙은 매칭은 나중에 사람이 봐도 "원래 그런 줄" 알기 때문에
발견되지 않는다. 매칭이 없는 것은 눈에 보이지만, 잘못된 매칭은 보이지 않는다.

### 2-3. 매칭 상태 모델 (제안)

```
UNMATCHED        아직 시도 안 함 / 후보 없음
AMBIGUOUS        exact key 후보가 2건 이상 (라인 분리 주문)
MATCHED_EXACT    (marketplace, order_no) 로 유일하게 찾음
MATCHED_MANUAL   사람이 승인을 거쳐 연결함
EXCLUDED         대사 대상 아님(광고비 정산, 보증금 등 주문과 무관한 행)
```

`AMBIGUOUS` 를 `UNMATCHED` 와 **구분하는 것이 핵심**이다. 전자는 사람이 고르면
끝나는 일이고, 후자는 데이터가 없는 일이다. 화면이 다른 작업을 안내해야 한다.

### 2-4. 판정 규칙 (구현 시)

```
1. marketplace_order_no 가 NULL        → EXCLUDED 후보로 분류(주문 무관 정산행)
2. (org, marketplace, order_no) 로 sales_order 조회
   · 0건 → UNMATCHED
   · 1건 → MATCHED_EXACT
   · 2건 이상 → AMBIGUOUS   ← **여기서 멈춘다. 고르지 않는다**
3. 그 외 어떤 추론도 하지 않는다
```

---

## 3. B. 수동 매칭 승인 — `SETTLEMENT_MANUAL_MATCH`

사람이 정산 건을 주문에 강제로 연결하는 것은 **손익 귀속을 바꾸는 행위**다.
비용 금액 변경과 같은 무게이므로 승인을 거친다.

```
target_type   settlement_record        (신규 어댑터)
request_type  SETTLEMENT_MANUAL_MATCH
소유 필드      matched_sales_order_id, matching_status, match_reason
effective     msr.settled_at (없으면 settlement_period_end) 기준 월 잠금 판정
```

`expense_attachment` 어댑터(M027)와 같은 모양이다 — 대상이 비용/상품이 아니라
정산 레코드 한 건이고, 바꿀 수 있는 필드를 **명시적으로 좁힌다.**

**승인 없이 되는 것**: `EXCLUDED` 표시(주문과 무관한 정산행 제외).
금액에 영향이 없고, 되돌리기 쉽고, 매일 나오는 작업이다.
**승인이 필요한 것**: 주문 연결·해제.

---

## 4. C. 정산 확정 vs 월 마감 — 합치지 않는다

```
① 정산 건 confirmed   "이 정산 레코드의 숫자와 매칭을 인정한다"    ← 레코드 단위
② 월 close confirmed  "이 달의 손익 집계를 확정한다"               ← 월 단위
③ 월 locked           "더 이상 아무도 못 고친다"                   ← 월 단위
```

**왜 분리하나**

- 정산서는 **월 경계를 넘는다.** 쿠팡 주정산은 7/29~8/4 처럼 걸쳐 있다.
  정산 건 확정을 월 마감과 묶으면 그 주는 어느 달에도 확정될 수 없다
- 정산 건은 **부분 확정**이 정상이다. 100건 중 80건은 매칭 끝, 20건은
  판매처 문의 중 — 월 마감은 그 상태로도 진행돼야 한다
- 되돌리는 비용이 다르다. 정산 건 하나를 다시 여는 것은 국소적이지만
  월 재오픈은 배분까지 다시 계산해야 하고 그래서 한도가 3회다(D-285)

**월 마감 검증(`validation_result_json`)에 경고로 넣을 것**:
`UNMATCHED`/`AMBIGUOUS` 가 남은 채 월을 확정하려 하면 경고.
**차단이 아니라 경고**다 — 판매처가 정산서를 늦게 주는 일이 흔하고,
그걸 차단으로 만들면 마감이 판매처 일정에 인질로 잡힌다.
`acknowledged_warnings_json` 이 이미 있으므로 "알고도 확정" 을 기록할 수 있다.

---

## 5. D. 확정 정산 정정 — `settlement_adjustment` 가 필요하다

**확정된 정산 레코드를 UPDATE 하지 않는다.** M026 에서 비용에 대해 내린 결론과
같은 이유다:

- 확정 시점의 숫자로 순이익·수익배분이 이미 계산됐다
- 원본을 고치면 과거 배분의 근거가 사라지고, 재현이 불가능해진다

### 5-1. 비용 조정(M026)과 같은 모델

```
7월 정산서에 반품 차감이 5만원 누락됐다
  → 7월 정산 레코드를 고치지 않는다
  → 8월(열린 달)에 +5만원 조정 레코드를 만들고 7월 원본을 가리킨다
  → 7월 확정 순이익·배분은 그대로, 8월 손익에 차액이 반영된다
```

`expense_adjustment` 를 그대로 베낀다:

| 컬럼 | 의미 |
|---|---|
| `original_record_id` | 원본 정산 레코드 |
| `adjustment_record_id` | 조정으로 생긴 레코드 |
| `adjustment_type` | `LATE_ENTRY` / `CORRECTION` / `REVERSAL` / `RECLASSIFICATION` |
| `adjustment_reason` | 필수. 사유 없는 조정은 나중에 아무도 설명 못 한다 |
| `original_period` / `adjustment_period` | 두 달 모두 기록 |
| `amount_delta` | **차액**. 원본 금액을 그대로 넣으면 두 배가 된다 |

### 5-2. 비용 조정과 다른 점

정산 금액은 **여러 항목**이다(`gross_revenue`, `marketplace_fee`, `ad_cost`,
`return_cost`, `fulfillment_fee`, `other_deductions`, `net_settlement`).
비용은 `gross_amount` 하나였다.

→ `amount_delta` 단일 컬럼으로는 부족하다. **항목별 delta** 가 필요하다.
JSONB 로 뭉치지 말고 원본과 같은 컬럼 구조를 쓴다 —
집계 쿼리가 원본과 조정을 `UNION ALL` 로 그대로 더할 수 있어야 한다.

---

## 6. E. import 재실행 — 지금 구조로는 **덮어쓰기 사고가 난다**

### 6-1. 현재 UNIQUE 키의 문제

```sql
UNIQUE (org_id, marketplace, COALESCE(marketplace_order_no,''), settlement_period_start)
```

- `marketplace_order_no` 가 NULL 인 행(광고비 정산 등)은 **전부 같은 키**가 된다.
  같은 기간의 주문 무관 정산행이 2건이면 두 번째가 UNIQUE 위반으로 들어가지 않는다
- 판매처가 정산서를 **재발행**하면(금액 정정) 같은 키로 다시 온다.
  지금 코드에는 upsert 도 없고 재실행 처리도 없다

### 6-2. 필요한 것 — raw source identity

```
external_record_id     판매처가 준 정산 레코드 고유 id (있으면)
source_file_hash       파일 단위 해시 (import_file_hash 가 이미 있다)
source_row_fingerprint 행 내용 해시 — 판매처가 id 를 안 줄 때의 대체 식별자
import_idempotency_key (org, marketplace, external_record_id | fingerprint)
```

`import_file_hash` 는 이미 있지만 **파일 단위**라 행을 식별하지 못한다.

### 6-3. 재수집 시 보존 규칙

같은 레코드가 다시 들어왔을 때 **무엇을 지키고 무엇을 갱신하는가**:

| 항목 | 재수집 시 |
|---|---|
| 금액 항목 | 값이 달라졌으면 **덮어쓰지 않는다.** 새 버전으로 쌓고 차이를 표시 |
| `matching_status = MATCHED_MANUAL` | **보존.** 사람이 승인을 거쳐 붙인 결정이다 |
| `matching_status = MATCHED_EXACT` | 재판정 허용(규칙이 결정론적이라 같은 결과가 나온다) |
| `EXCLUDED` 표시 | **보존** |
| 조정(`settlement_adjustment`) | **보존.** 조정은 원본과 독립된 사실이다 |
| 정산 건 confirmed | **보존.** 확정을 import 가 뒤집으면 확정이 의미를 잃는다 |

> 핵심 원칙: **import 는 사람이 내린 결정을 덮어쓸 수 없다.**
> 금액이 달라졌다면 그건 "새 사실" 이고, 사람에게 보여줘서 조정으로 처리하게 한다.

---

## 7. 선행 조건 (구현 착수 전에 반드시)

| # | 항목 | 이유 |
|---|---|---|
| 1 | 마이그레이션: `matching_status`, `expected_settlement_amount`, `external_record_id`, `source_row_fingerprint`, 정산 건 확정 컬럼 | 지금 스키마로는 상태를 표현할 수 없다 |
| 2 | UNIQUE 키 재설계 (§6-1) | NULL 주문번호 충돌 · 재발행 처리 |
| 3 | **실제 쿠팡 정산서 파일 확보** | 컬럼명·주문번호 형식·라인 분리 여부를 추측으로 설계하면 전부 다시 만든다 |
| 4 | `settlement_adjustment` 테이블 (§5) | 확정 정정 경로 없이 확정을 열면 UPDATE 유혹이 생긴다 |
| 5 | `settlement_record` 승인 어댑터 (§3) | 수동 매칭이 승인 밖으로 새면 매칭이 곧 우회 경로가 된다 |

**3번이 가장 중요하다.** 지금 `marketplace_settlement_record` 는 0건이고
`integration_account` 도 0건이다 — 즉 이 도메인은 **실데이터를 한 번도 본 적이 없다.**
엑셀 실데이터로 상품·비용·마진을 검증했던 것과 같은 과정을 정산서에도 먼저 거쳐야 한다.

---

## 8. 이번 작업에서 하지 않은 것 (명시)

- 매칭 로직 구현 — 없음
- `settlement_record` 승인 어댑터 — 없음
- 마이그레이션 — 없음 (M027 은 증빙·거래처용이고 정산과 무관)
- 화면 — 없음
- 자동 추측 매칭 — **의도적으로 없음** (§2-2)
