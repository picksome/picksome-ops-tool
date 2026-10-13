# 53. 주문 → 판매자 계정 귀속 (M032, Phase 5)

> 커밋: 아래 §7 · migration `M032`

---

## 1. 무엇이 문제였나

`sales_order` 가 "이 주문이 어디서 왔는가" 를 아는 유일한 방법은
`marketplace` 텍스트 컬럼이었다 (`sale_channel.code`).

그런데 **한 판매처에 판매자 계정이 여럿일 수 있다.** 쿠팡 본점과 쿠팡 2호점은
둘 다 `marketplace='COUPANG'` 이다.

```
할 수 있던 말        "쿠팡 매출 300만원"
할 수 없던 말        "본점 200만 / 2호점 100만"
```

정산서는 계정 단위로 온다. 이 구분 없이는 정산 대사도, 계정별 손익도,
Phase 6 의 현금흐름 연결도 불가능하다.

`product_channel_listing`(M022)은 이미 `integration_account_id` 로 붙어 있었다.
**주문만 문자열에 머물러 있었다.**

---

## 2. 사전 조사 — 현재 귀속 방식

| 항목 | 확인한 사실 |
|---|---|
| 유일한 단서 | `sales_order.marketplace` (TEXT) |
| 주문 생성 경로 | `services/sales.create_sale()` 하나로 수렴 (수동 API · 봇 ingest 둘 다) |
| 봇 ingest | `ingest_orders(account_id=...)` 가 **계정을 이미 알고 있는데 `create_sale` 에 넘기지 않고 버렸다** |
| 대체 추적 경로 | `import_batch_id → batch_run → integration_job → account_id` |
| 그 경로 상태 | **끊겨 있다.** `ingest_orders` 가 `batch_run.job_id = NULL` 로 넣는다 |
| 실데이터 (dev) | 주문 374건 전부 `EXCEL_IMPORT` · `import_batch_id` 전부 NULL · `integration_account` 0건 · `batch_run` 0건 |

**결론: backfill 할 근거 데이터가 구조적으로 존재하지 않는다.**

---

## 3. 변경 후 귀속 흐름

```
integration account (키 인증으로 확정)
        ↓
adapter 실행 (ingest_orders)
        ↓
SaleOrderCreate.integration_account_id      ← 명시적으로 전달
        ↓
create_sale() → resolve_account() 검증
        ↓
sales_order.integration_account_id
```

**하지 않는 것**:

```
order.marketplace = "COUPANG"
        ↓
DB 에서 쿠팡 계정 아무거나 찾기      ← 이 경로는 만들지 않았다
```

채널과 계정을 동일시하지 않는다. `marketplace` 는 "어느 장터" 이고
`integration_account_id` 는 "누구의 스토어" 다.

### 봇이 주장하는 계정을 믿지 않는다

`ingest_orders` 는 **실행 계정**(`verify_account_key` 가 확정한 것)을 쓴다.
페이로드에 `integration_account_id` 가 들어 있어도 무시한다 —
믿으면 한 계정의 키를 가진 봇이 다른 계정 매출을 만들 수 있다.

---

## 4. Legacy backfill 정책

### 채운 것

`import_batch_id → batch_run → integration_job → account_id` 가
**기록으로 남아 있는** 주문만. 계정이 주문과 같은 조직인지도 확인한다.

현재 데이터에서는 0건이 맞는다. 그래도 구문을 남긴 이유는
다른 환경에 `integration_job` 을 통해 들어온 주문이 있을 수 있고,
그건 추측이 아니라 **기록된 사실**이라 채워도 안전하기 때문이다.

### 채우지 않은 것 — 그리고 그 이유

| 유혹 | 왜 안 하나 |
|---|---|
| "쿠팡 주문이니 쿠팡 계정" | 계정이 하나뿐이어도 그 주문이 거기서 왔다는 보장이 없다. 엑셀로 넣은 과거 주문일 수 있다 |
| "첫 번째 / 가장 최근 / 기본 계정" | 임의 선택이다. 계정별 매출이 그 순간부터 거짓이 된다 |
| "여러 계정 중 매출이 가장 큰 곳" | 위와 같다. 그럴듯해서 더 나쁘다 |

**틀린 귀속이 확정 snapshot 에 박제되면 나중에 진짜 계정을 알게 돼도 되돌릴 수 없다.**
NULL 은 "모른다" 는 뜻이고, 그건 사실이다.

### NOT NULL 전환 조건

아래가 전부 참일 때 별도 migration 으로 전환한다.

1. 수동 주문 등록 화면이 계정 선택을 필수로 받는다
2. 모든 ingest 경로가 실행 계정을 넘긴다 — **M032 로 충족됨**
3. `SELECT COUNT(*) FROM sales_order WHERE integration_account_id IS NULL` = 0
4. 남은 legacy NULL 을 사람이 계정에 귀속시키는 도구가 있다

3번을 만족시키려고 legacy 주문을 임의 계정에 넣는 것은 이 설계를 정면으로
배반한다 — 그럴 바에는 NULL 이 낫다.

---

## 5. 인덱스를 만들지 않은 이유

지금 이 컬럼으로 조회하는 곳은 새로 추가한 `?integration_account_id=` 필터뿐이고,
계정별 집계 화면은 아직 없다(Phase 6 이후). 대시보드·마감·정산 집계는 전부
`org_id + sale_date` 로 돈다.

FK 는 인덱스 없이 동작한다. 자식 쪽 인덱스가 필요해지는 것은 **부모를 DELETE 할 때**인데,
이 FK 는 `ON DELETE NO ACTION` 이고 `integration_account` 는 soft delete 를 쓴다.

계정별 집계 화면이 생기면 그때 EXPLAIN 을 근거로 추가한다.

---

## 6. 데이터 무결성

| | 어떻게 보장하나 | 테스트 |
|---|---|---|
| A. 한 주문 = 최대 한 계정 | 단일 컬럼 + FK | 스키마 |
| B. 같은 채널 다계정이 안 섞임 | 계정 id 로 저장·필터 | `test_orders_from_two_accounts_on_the_same_channel_do_not_mix` |
| C. 비활성화가 과거 주문을 안 깨뜨림 | `resolve_account` 가 `status` 를 보지 않는다 | `test_deactivated_account_keeps_its_past_orders_readable` |
| D. 삭제가 추적을 안 망가뜨림 | `ON DELETE NO ACTION` + soft delete | E2E `하드 삭제가 거부된다` |
| E. 마감 후 계정 메타 변경이 금액을 안 바꿈 | 귀속은 id, 집계는 계정과 무관 | `test_close_snapshot_figures_are_unchanged_by_the_account_column` |

`resolve_account` 가 **비활성 계정을 통과시키는** 것은 의도적이다.
계정을 잠시 멈춰 둔 동안에도 그 계정으로 들어온 주문은 기록돼야 하고,
지난달 주문을 뒤늦게 고칠 때 "계정이 비활성" 이라고 거절하면
과거 데이터를 수정할 수 없게 된다. 활성 여부가 필요한 것은
**키로 인증하는 경로**(`verify_account_key`)이지 귀속 기록이 아니다.

---

## 7. Phase 6 (Cashflow) 준비 상태

Phase 6 에서 필요한 연결 고리 중 이번에 확정된 것:

```
sales_order.integration_account_id  →  integration_account.id     ✅ (M032)
integration_account.sale_channel_id →  sale_channel.id            ✅ (M009)
product_channel_listing.integration_account_id                    ✅ (M022)
```

즉 **주문 ↔ 계정 ↔ 채널** 삼각형이 id 로 이어졌다.
Phase 6 은 여기에 정산 입금과 원장을 붙이면 된다.

### Phase 6 시작 전 남은 blocker

| blocker | 성격 |
|---|---|
| `marketplace_settlement_record` 에 `integration_account_id` 가 없다 | 정산서도 계정 단위로 오는데 아직 채널 문자열만 있다. **Phase 6 의 첫 작업이 되어야 한다** |
| 실제 정산 파일 없음 | `BLOCKED_EXTERNAL_DATA` — 변동 없음 |
| legacy 주문 374건이 계정 미상 | 현금흐름을 계정별로 나누면 이 주문들은 "미확인" 버킷에 남는다. 숨기지 말고 그대로 보여야 한다 |

이번 Phase 에서는 cashflow 테이블도 ledger posting 로직도 만들지 않았다.

---

## 8. 발견한 기존 문제

### 프론트 `SaleOrder` 타입이 백엔드에 없는 필드를 선언하고 있었다

`product_variant_id` 를 선언했지만 백엔드 `SaleOrderOut` 에는 없다.
반대로 백엔드가 주는 `line_no` 는 타입에 없었다.

이 저장소에서 네 번째로 나온 같은 종류의 결함이다(D2·D6·D9·D11).
`lib/api/sales.test.ts` 로 계약을 고정했다.

### `_SELECT` 에 JOIN 을 붙이자 컬럼 이름이 모호해졌다

`sales_order` 와 `integration_account` 둘 다 `org_id`·`deleted_at`·`created_at`
을 갖고 있어서, `WHERE org_id = :o` 가 `column reference is ambiguous` 로
목록 조회 전체를 깨뜨렸다. 모든 참조를 `so.` 로 한정했다.

---

## 9. M032 invariant 최종 검증 (후속)

migration 직후 §2 의 invariant 를 **실제로 실행해서** 확인했다. 문서를 믿지 않고
DB 에 넣어 봤다.

| invariant | 검증 방법 | 결과 |
|---|---|---|
| 2-1 조직 일치 | 다른 조직 계정으로 주문 생성 시도 | ✅ 이미 막고 있었다 (`resolve_account`) |
| 2-2 판매처 일치 | **쿠팡 주문 + 네이버 계정** 실제 생성 시도 | ❌ **통과했다 — 결함(D12)** |
| 2-3 legacy NULL | 계정 없는 주문 조회·집계 | ✅ 빠지지 않는다 |
| 2-4 ingest 계정 | 페이로드가 다른 계정을 주장 | ✅ 실행 계정으로 귀속 |
| 2-5 회귀 | 위 전체 | ✅ 테스트로 고정 |

### D12 — 판매처 불일치가 허용됐다

```
sales_order.marketplace = COUPANG
integration_account     = 네이버 스토어
→ 저장됐다
```

FK 만으로는 못 막는다. 계정 id 는 유효하고 조직도 같다.
이 상태로 쌓이면 정산 대사와 스토어별 손익이 통째로 틀어지고,
나중에는 어느 쪽이 맞는지 알 방법이 없다.

**조치**: `resolve_account(..., marketplace=...)` 로 확장. 생성·수정 양쪽에서 검증한다.

수정 경로는 세 가지를 모두 막아야 한다:

| 조작 | 검증 기준 |
|---|---|
| 계정만 변경 | 기존 판매처 |
| 판매처만 변경 | **기존 계정** ← 이걸 빠뜨리면 이 경로로 우회된다 |
| 둘 다 변경 | 바뀐 뒤의 값 |

### 고치는 과정에서 드러난 것 — `provider` 는 판매처가 아니다

처음에는 계정의 판매처를 `COALESCE(sale_channel.code, provider)` 로 잡았다.
그러자 **기존 ingest 테스트 2건이 깨졌다.** 원인은 이렇다.

```python
create_bot_account(provider="BOT_GENERIC", ...)   # 실제로 쓰이는 값
```

`provider` 는 판매처가 아니라 **도구·공급자 이름**이다. 그걸 채널 코드로
취급하면 정상적인 봇 주문이 전부 거절된다.

그래서 **`sale_channel_id` 로만** 판단한다. 그 값이 NULL 인 계정은
"판매처를 선언한 적이 없다" 는 뜻이므로 비교 대상이 없어 통과시킨다 —
없는 근거로 거절하는 것은 검증이 아니라 추측이다.

> 채널을 선언하지 않은 계정 자체는 데이터 품질 문제다.
> Phase 8 스캐너의 검사 후보로 남긴다(`ACCOUNT_WITHOUT_CHANNEL`).

테스트가 이 설계 오류를 먼저 잡았다. 기존 테스트를 "고쳐서 통과시키지" 않고
**구현이 틀렸다고 판단한** 근거가 `BOT_GENERIC` 이라는 실제 값이었다.
