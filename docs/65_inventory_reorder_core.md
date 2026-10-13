# 65. 재고 · 발주 · 재주문 Core

M048 · 2026-08-11

---

## 1. 이번에 만든 것

```
inventory_transaction   재고 원장 (OPENING/PURCHASE_RECEIPT/SALE/RETURN/ADJUSTMENT)
purchase_order          발주 (DRAFT→ORDERED→PARTIALLY_RECEIVED→RECEIVED/CANCELLED)
purchase_order_line     발주 품목 — 주문 시점 단가 박제
REORDER_MONITOR         재주문 감시 자동화 (REPORT_ONLY / PROPOSE)
/inventory              재고·재주문 화면
/purchase-orders        발주·입고 화면
```

---

## 2. 왜 잔량 컬럼이 아니라 원장인가

`product.stock = 12` 는 "왜 12 인가" 에 답하지 못한다. 판매가 빠졌는지 입고가
두 번 들어갔는지 사람이 손으로 고쳤는지 구분이 안 되고, 한 번 어긋나면 되돌릴
근거가 없다.

```
움직임을 기록한다   +50 입고 / -3 판매 / +1 반품 / -2 조정
잔량은 합계로 구한다  SUM(quantity_delta)
```

느려지면 스냅샷을 얹는다. **먼저 만들지 않는다** — 원장과 스냅샷이 어긋날 때
어느 쪽이 진실인지 정하는 일이 생긴다.

### 기존 `product_variant.stock_quantity` 는 어떻게 했나

지우지 않고 **비권위**로 못 박았다(DB 주석). 옵션 화면이 아직 쓰고 있고, 값이
든 행은 0건이라 데이터 손실 위험이 없다. 재고 출처가 둘이면 반드시 어긋나므로
"원장이 맞다" 를 스키마에 적어 뒀다. 화면 이전 후 제거한다.

---

## 3. 세 가지 원가를 섞지 않는다

이번 작업에서 가장 조심한 부분이다.

```
product_source_cost_version      지금 다시 사면 얼마인가   (미래 구매)
inventory_transaction.unit_cost  이 재고를 얼마에 샀는가   (과거 취득)
product_margin_actual            판매 주문에 적용된 원가   (확정된 과거)
```

공급처가 오늘 값을 올려도 창고 재고의 취득원가는 안 바뀐다. 입고 시 취득원가는
**발주 라인의 단가**를 쓴다 — 그 사이 소싱 원가가 바뀌었을 수 있고, 우리가
실제로 지불한 것은 발주가다. 테스트로 고정했다
(`test_receipt_uses_order_price_not_current_cost`).

---

## 4. 모르는 것을 0 으로 만들지 않는다

| 상황 | 잘못된 표현 | 실제 표현 |
|---|---|---|
| 미출고 주문을 모름 | 예약 0개 | `reserved_status = NOT_CONNECTED` |
| 원장에 기록 없음 | 재고 0 | `tracked = false` |
| 판매 이력 없음 | 판매속도 0 | `NO_SALES_HISTORY` |
| 리드타임 없음 | 평균 7일 | `NO_LEAD_TIME` |
| 재고 음수 | 0 으로 보정 | 음수 그대로 + 경고 |

재주문을 추천할 수 없는 이유는 **각각 다른 이름**을 갖는다. 전부 "추천 불가" 로
뭉뚱그리면 사용자가 무엇을 채워야 하는지 알 수 없다.

```
NOT_TRACKED       기초 재고를 입력하세요
NO_SALES_HISTORY  판매 이력이 쌓이면 계산됩니다
NO_SOURCE         상품에 소싱처를 연결하세요
NO_LEAD_TIME      소싱처에 리드타임을 입력하세요
NO_COST           소싱 원가를 입력하세요
```

화면은 이 목록을 **접어 두지 않는다.** 숨기면 "우리 상품은 다 여유 있구나" 로
잘못 읽히고, 어느 날 갑자기 품절난다.

---

## 5. 재주문 계산

```
판매속도   = 기간 내 판매수량 / 기간일수
소진일수   = (현재고 + 미입고발주) / 판매속도
재고소진일 = 오늘 + 소진일수
발주기한   = 재고소진일 - 리드타임
권장수량   = (리드타임 + 목표커버) × 판매속도 - 현재고 - 미입고발주
           → MOQ 배수로 올림
```

**미입고 발주를 빼는 것**이 중요하다. 빼먹으면 이미 주문한 것을 또 주문하라고
권한다 (`test_reorder_subtracts_inbound`).

### 임계값이 코드에 없다

`velocity_days` · `coverage_days` 는 **필수 인자**다. 서비스에도 API 에도 화면에도
기본값이 없다.

```
서비스   calculate(..., velocity_days, coverage_days)   기본값 없음
API      Query(...)                                    필수 파라미터
자동화   guardrail 에서 읽음, 없으면 BLOCKED
화면     빈 입력으로 시작, 정해야 계산
```

"보통 30일" 을 넣는 순간 아무도 자기 회전율에 맞게 정하지 않고, 나중에 그 숫자가
어디서 왔는지 모른다.

### 단가 출처를 밝힌다

```
1순위  product_source_cost_version.purchase_price   SOURCE_COST_VERSION
2순위  product.cost_amount                          PRODUCT_COST
둘 다 없으면                                        NO_COST (추천 안 함)
```

화면에 어느 쪽을 썼는지 표시한다 — 금액이 예상과 다를 때 어디를 고칠지 알아야
한다. (현재 실 DB 는 `product_source_cost_version` 이 0건이라 전부 2순위다.)

---

## 6. 발주를 자동 실행하지 않는다

`REORDER_MONITOR` 에는 **`AUTO_EXECUTE` 모드가 아예 없다.**

```
REPORT_ONLY   찾아서 알리기만. 초안도 안 만든다
PROPOSE       DRAFT 발주까지만. 확정은 사람이 누른다
```

판매속도는 추정치다. 시즌·광고·경쟁사 품절로 흔들리고, 지난 30일이 다음 30일을
보장하지 않는다. 그 추정치로 돈을 쓰는 경로를 만들지 않았다.

`DRAFT → ORDERED` 는 ADMIN/OPERATOR 권한이고, 화면에서 "발주 확정" 이라는 별도
버튼이다. 자동 제안에는 배지가 붙는다.

중복 방지: 같은 공급처에 **처리 안 된 자동 초안**이 있으면 또 만들지 않는다
(`PENDING_DRAFT_EXISTS`). 사람이 만든 초안은 세지 않는다 — 사람이 준비 중인
발주 때문에 자동화가 침묵하면 안 된다.

---

## 7. 입고 중복 — 자동으로 막지 않는다

"같은 수량을 두 번 입고" 는 **정상**이다. 3개짜리 상자가 두 번 오면 6개가 맞다.
수량만 보고 걸러내면 진짜 입고를 잃는다.

막아야 하는 것은 *같은 입고 사건이 두 번 전송된 것*(버튼 두 번, 재시도)이고,
그건 보낸 쪽만 구분할 수 있다. 그래서 `receipt_key` 를 호출자가 준다.

```
receipt_key 있음   같은 키 재전송 → uq_inv_txn_source 가 막는다
receipt_key 없음   매번 새 사건 (중복 방지 없음, 그렇다고 명시)
```

없을 때 막는 척하지 않는다. 화면은 폼이 열릴 때 키를 하나 만들어 고정한다.

---

## 8. 검증

```
신규 테스트          30건 (재고·발주 21 + 재주문 자동화 9)  전부 통과
전체 백엔드          901 passed, 1 xfailed (기존)
프론트 빌드          성공 — /inventory, /purchase-orders 생성
ruff                 통과
마이그레이션 왕복     scratch DB 에서 48↑ → M047↓ → M048↑
실 DB 스모크         기초재고→조정→발주→부분입고→완료입고→재주문 전 구간
```

실 DB 스모크에서 재주문은 **차단 3건(NO_SOURCE)** 이 나왔다. 실제로
`product_source` 가 0건이기 때문이고, 이것이 올바른 결과다.

---

## 9. 이번에 찾은 기존 결함

### `ALEMBIC_DATABASE_URL` 이 무시되고 있었다

`env.py` 가 무조건 `settings.DATABASE_URL` 을 썼다. 임시 DB 를 지정했다고 믿고
`downgrade` 를 돌리면 **조용히 개발 DB 가 내려갔다.**

이번에 실제로 당했다. 되돌리기 검증을 하려던 명령이 개발 DB 의 M048 테이블을
드롭했다(그 안에는 내 스모크 데이터만 있어서 사용자 데이터 손실은 없었고,
`product` 1,067 / `sales_order` 374 / `product_margin_actual` 374 /
`expenses` 6 / `credential_secret` 2 전부 확인했다).

`-x db_url=` 과 `ALEMBIC_DATABASE_URL` 을 받도록 고치고, 덮었을 때 **어느 DB 인지
찍게** 했다. 고친 뒤 다시 돌리니 `picksome_migcheck` 가 존재하지 않는다는 에러가
났다 — 그동안 그 DB 는 만들어진 적도 없었다는 뜻이다.

---

## 10. 남은 것

이번 작업지시(P0~P5) 중 **완료하지 않은 항목**:

| 항목 | 상태 |
|---|---|
| P0 Daily Report 자동 생성·발송 | **미착수** — 기존 `DAILY_REPORT` 는 조회만 |
| P1 Autopilot 안전 AUTO_EXECUTE 확대 | **미착수** |
| P2 재고 Excel import (ANALYZE/DRY_RUN/APPLY) | **미착수** — 수동 입력만 |
| P3 주문 예외 센터 | **미착수** |
| P4 MCP 재고 도구 | **미착수** — `inventory:read` scope 없음 |
| P5 외부 API 재점검 | **미착수** |

재고 조정 UI 도 API 만 있고 화면 버튼이 없다. 발주 **생성** 화면도 없다 —
현재 초안은 자동화가 만들거나 API 로 만들어야 한다.

컨텍스트 한계로 재고·발주·재주문 한 덩어리를 끝까지 완성하는 쪽을 택했다.
