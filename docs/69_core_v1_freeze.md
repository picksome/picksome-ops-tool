# 69. Seller OS Core v1 Freeze

2026-08-11 · migration head **M051**

이 문서는 **Connector 개발에 들어가기 전 Core 의 상태**를 고정한다. 이후
Core 변경은 아래 §9 의 정책을 따른다.

---

## 1. Core v1 범위

실제 코드 기준으로 구현된 것.

```
상품 / 옵션 / 채널가격        product, product_variant, product_channel_*
승인 / 감사                  approval_request, audit_log
비용 / 증빙                  expenses, expense_attachment
소싱처 / 원가 버전 / FX        supplier_site, product_source,
                            product_source_cost_version, fx_rate_snapshot
판매 / 실마진                 sales_order, product_margin_actual
재고 원장 / 가져오기           inventory_transaction, inventory_import_run
발주 / 입고                   purchase_order, purchase_order_line
재주문 분석                   services/reorder.py  (테이블 없음 — 계산 결과)
정산 / 현금흐름                marketplace_settlement_record, cashflow
월 마감 / 배분 / 지급          monthly_settlement, distribution, payout
운영 이슈 / 예외 센터 / 리포트   operation_issue, services/exception_center.py,
                            services/daily_report.py
자동화                       automation_policy, automation_run, automation_action
외부 시스템 / 비밀 저장소       external_system, external_account,
                            credential_secret (Fernet)
서비스 계정 / API 토큰 / MCP    service_account, api_token, app/mcp/*
```

Connector(실제 marketplace 호출)는 **범위 밖**이다. 다음 세션의 주제다.

---

## 2. 핵심 불변 조건

깨지면 사업 숫자가 틀어진다. 전부 테스트로 고정돼 있다.

### 2-1. 세 가지 원가를 섞지 않는다

```
product_source_cost_version   지금 다시 사면 얼마인가   (관측된 사실)
inventory_transaction.unit_cost 이 재고를 얼마에 샀나   (과거 취득)
product_margin_actual         판매된 주문의 확정 원가   (움직이지 않는 과거)
product.cost_amount           현재 운영에 채택된 원가   (승인으로만 변경)
```

- 소싱 원가가 들어와도 `product.cost_amount` 는 **자동으로 바뀌지 않는다.**
  `COST_CHANGE` 승인을 거친다. (`test_product_source_import.py`)
- 입고 후 소싱 원가가 3,000 → 9,999 로 바뀌어도 그 재고의 취득원가는
  **3,000 그대로**다. (E2E `inventory-purchase.spec.ts`)

### 2-2. 재고는 원장이다

`product.stock` 같은 잔량 컬럼을 만들지 않는다. 잔량은
`SUM(inventory_transaction.quantity_delta)` 다.

가져오기도 덮어쓰지 않는다 — 원장 14, 실사 17 이면 `ADJUSTMENT +3`.

`product_variant.stock_quantity` 는 엑셀 시절 잔재이고 DB 주석에
**비권위**로 표시돼 있다(값이 든 행 0건).

### 2-3. 모르는 것을 0 으로 만들지 않는다

| 상황 | 표현 |
|---|---|
| 주문 API 미연결 | `reserved_status = NOT_CONNECTED` |
| 원장 기록 없음 | `tracked = false` |
| 재주문 기준 미설정 | `NOT_CONFIGURED` |
| 리드타임·MOQ 없음 | `NULL` (0 아님) |
| 판매 이력 없음 | `NO_SALES_HISTORY` (속도 0 아님) |
| 클레임 커넥터 없음 | 카테고리 `NOT_CONNECTED` |
| 구획 조회 실패 | `ERROR` |

`count = 0` 이 다섯 가지 뜻을 갖기 때문에 화면은 **건수가 아니라 status** 로
문구를 만든다. (`lib/api/exceptions.test.ts`)

### 2-4. 임의 임계값 없음

`velocity_days` · `coverage_days` 는 서비스·API·자동화·MCP·화면 **어디에도
기본값이 없다.** 안 정하면 계산하지 않는다.

데이터 품질 스캐너에도 "재고 5개 미만"·"3일 지연" 같은 숫자가 없다.
음수 재고(0 미만)와 기한 경과(예상일 존재 + 지남)처럼 **데이터로 참인 것**만
본다.

### 2-5. 월 마감·정산·지급

- 마감 snapshot 은 이후 current data 변경으로 재계산되지 않는다.
- 정산 대사는 fuzzy 매칭을 하지 않는다 — 틀린 매칭보다 `UNMATCHED` 가 낫다.
- 지급 `PAID` 는 실제 지급 기록 없이 될 수 없다.

### 2-6. 등록가 역산은 정방향 식의 정확한 역이다

```
sp × (1 - mfee_rate - pay - ad - ret - vat_eff - target)
    = cost + fulfill + mfee_fixed × (1 - vat_rate)

vat_eff = vat_rate × (1 - mfee_rate)
```

정액 수수료에 `(1 - vat_rate)` 가 붙는 이유: 정액 수수료는 **과세표준을
깎으므로**(`vat = (sp - mfee) × vat_rate`) 실질 부담이 액면가보다 작다.

`TIERED` 는 구간마다 풀고 **자기 구간에 들어오는 해**를 고른다. 어느 구간에도
안 맞으면 값을 지어내지 않고 거절한다.

왕복 검증(역산 → 정방향)이 RATE·FIXED·TIERED·정책없음 8개 조합에서 오차
**0.000%p** 다.

---

## 3. 보안 경계

### 3-1. 역할

```
ADMIN OPERATOR SOURCING ACCOUNTING READONLY EXTERNAL_ACCOUNTANT
```

`require_roles` 로 라우터가 지킨다. **화면 숨김은 권한이 아니다** — 역할별
직접 HTTP 호출로 검증한다.

### 3-2. 수량 ≠ 원가

```
PERM_VIEW_INVENTORY_QUANTITY   ADMIN OPERATOR SOURCING ACCOUNTING READONLY
PERM_VIEW_ACQUISITION_COST     ADMIN OPERATOR SOURCING ACCOUNTING
PERM_VIEW_SOURCE_COST          ADMIN OPERATOR SOURCING ACCOUNTING
```

`EXTERNAL_ACCOUNTANT` 는 재고를 보지 않는다(증빙만).
발주는 금액이 본문이라 `READONLY` 에게 닫혀 있다.

### 3-3. 파생 노출

단가만 지우고 합계를 남기면 나눗셈으로 복원된다. 그래서 한 묶음으로 지운다.

```
unit_cost unit_price line_total total_amount estimated_amount
price_basis estimated_total
```

`redact_acquisition_cost()` 가 **서버에서** 지운다. 테스트는 응답 본문에
금액 문자열이 아예 없는지 확인한다.

### 3-4. 조직 격리

모든 조회·쓰기가 `org_id` 로 걸린다. `record_transaction` 은 상품 소유까지
확인한다 — 이 검사가 없어 cross-org 재고 조정이 가능했던 결함을 이번에
고쳤다.

### 3-5. 비밀

- `credential_secret` 에 **ciphertext 만** 저장(Fernet, MultiFernet 회전).
- 마스터 키는 환경변수. DB 에도 백업에도 들어가지 않는다.
- 자격증명 파일의 ID/PW 열은 가져오기가 **읽지 않고** 개수만 센다.
- 값 기준 유출 스캔: git tracked / log -p / diff / test-results 전부 `false`.

---

## 4. 자동화 경계

```
REPORT_ONLY    찾아서 알리기만
PROPOSE        승인 요청 또는 DRAFT 까지
AUTO_EXECUTE   돈·외부 write 가 없는 것만
```

`REORDER_MONITOR` 에는 **`AUTO_EXECUTE` 모드가 없다** — 판매속도는 추정치이고,
추정치로 돈을 쓰는 경로를 만들지 않는다.

자동화 경로가 **없는** 것들(`NEVER_AUTOMATED`):

```
PAYOUT_EXECUTE BANK_TRANSFER MONTH_REOPEN
CREDENTIAL_CHANGE MANUAL_SETTLEMENT_MATCH
```

---

## 5. MCP 경계

- **선택 기능이다.** `MCP_ENABLED=false` 면 게이트웨이가 404 이고 재고·재주문·
  발주·자동화·리포트는 그대로 돈다. 실제로 확인했다.
- tool 은 서비스만 호출한다. SQL 직접 조회·재계산 없음.
- scope 는 **서버가** 강제한다. 목록에서 숨기는 것은 편의일 뿐이다.
- `purchasing:execute` 는 `FORBIDDEN_SCOPES` — 토큰 발급 자체가 거부된다.
- 발주 실행 tool 이 **존재하지 않는다**(`place_purchase_order` 등). 스모크가
  없음을 확인한다.
- 에이전트가 만든 DRAFT 는 `created_by_service_account` 에 남고
  감사 `actor_type = SERVICE_ACCOUNT` 다. 사람으로 위장하지 않는다.

---

## 6. 알려진 외부 blocker

| 대상 | 상태 | 막고 있는 것 |
|---|---|---|
| COUPANG | `403 IP_NOT_ALLOWED` | 서명은 통과. WING 에 이 서버 outbound IP 등록 필요 |
| NAVER | `CREDENTIAL_FORMAT_MISMATCH` | 커머스API 자격증명이 아님(개발자센터 키) |
| 11ST | `API_DOCUMENTATION_MISSING` | Base URL·주문 endpoint 문서 없음 |
| NEXT_ENGINE | `API_DOCUMENTATION_MISSING` | 자격증명 쌍·기술 문서 없음 |

전부 **우리 코드 문제가 아니다.** 추측으로 호출하지 않았다.

---

## 7. 의도적으로 제공하지 않는 것

| 항목 | 이유 |
|---|---|
| 오프사이트 백업 사본 | 미정 — 현재 로컬 디스크뿐. 운영 데이터 투입 전 결정 필요 |
| worker 3종 단위 동시성 | advisory lock 이 별도 연결을 요구해 savepoint 하네스에서 재현 불가 |
| 테넌트 복합 FK | 157개 FK 가 org 일치를 DB 로 강제하지 않음. vNext (아래) |
| 종합소득세·사업소득 리포트 | SQL 미구현. `available=false` 로 표시하고 **요청을 거절**한다 — 빈 파일이 세무 자료로 넘어가는 것이 더 위험하다 |
| 예약 수량(reserved) | 미출고 주문을 알 방법이 없다. 커넥터가 붙어야 가능 |
| 클레임(취소·반품) 집계 | 같은 이유 |
| 주문 자동 수집 | 커넥터 blocker |
| 발주 자동 전송·결제 | 설계상 만들지 않는다 |

---

## 8. Production mock

```
백엔드 production mock              0건
프론트 mock fallback                dev 전용 (아래)
```

`lib/adapters/index.ts` 의 mock fallback 은
`NODE_ENV !== "production" && NEXT_PUBLIC_ENABLE_MOCK_FALLBACK === "true"`
두 조건이 **AND** 로 묶여 있어 운영 빌드에서는 켜질 수 없다. 401 은 mock 으로
가리지 않는다(로그인 문제를 덮으면 원인을 못 찾는다).

이 가드가 코드에서 사라지지 않도록 `lib/adapters/mock-fallback.test.ts` 가
고정한다. **Freeze 시점 판단: 유지한다** — 개편 중 화면 검증에 쓰이고 운영
경로가 아니다.

### 알려진 경고

```
frontend  products/page.tsx:356  react-hooks/exhaustive-deps (기존)
```

백엔드 xfail 은 **0건**이다. 오래 남아 있던 FIXED 마켓수수료 역산 오차는
이번에 고쳤다(§2-6).

---

## 8-1. Connector 직전 점검 결과 (2026-08-11 갱신)

독립 감사와 그 후속 수정의 최종 상태다.

| 항목 | 상태 |
|---|---|
| AUD-2 (원가 미상 → 0) | **FIXED** — `margin_analytics` · `monthly_purchase` 둘 다 |
| 발주 품목 cross-org | **FIXED** — 상품·옵션·소싱처 소유 확인, 쓰기 전 검증 |
| Backup / Restore | **VERIFIED** — 실제 덤프 → 일회용 DB → 무결성 → 삭제 |
| Backup 자동 스케줄 | **READY_FOR_PRODUCTION_INSTALL** (로컬 등록 안 함, docs/70) |
| Master Key 복구 | **VERIFIED** — 8건 (분리·회전·변조·fail closed) |
| worker `automation_scan` | **PASS_WITH_DB_CONSTRAINT** — 최악은 중복이 아니라 실행 실패 |
| worker `period_recalc` | **PASS** — 재실행이 일을 복제하지 않음 |
| worker `attachment_delete` | **부분** — 빈 큐 재실행만. 행 단위 claim 미검증 |
| worker `due_approvals` | **미검증(단위)** — advisory lock 이 별도 연결 필요, E2E 에서만 |
| worker `channel_publish` | 〃 |
| worker `operation_issue_scan` | 〃 |
| 테넌트 쓰기 경로 sweep | 요청 본문 id 경로 전수 테스트 (재고·발주·importer·집계) |

### 원가 미상의 의미 (AUD-2 이후)

```
cost_complete = false
actual_margin_amount = null
actual_margin_rate = null
unknown_cost_lines = N
```

집계는 **아는 것만 더하고 빠진 개수를 함께** 돌려준다
(`margin_excluded_products`). 매입 리포트는 0 대신 **빈 칸**을 쓴다 — 0원은
"공짜로 샀다" 로 읽혀 매입세액을 과소 계상한다.

---

## 9. Freeze 이후 Core 변경 정책

**신규 기능 개발은 기본적으로 중단한다.**

허용:

1. Connector 가 실제 데이터로 발견한, 현재 모델로 **표현 불가능한** domain gap
2. 보안 수정
3. 정확성(계산·불변 조건) 수정
4. 법·세무·외부 API 정책 변경 반영
5. 운영 장애 수정

그 외는 `vNext backlog` 로 미룬다.

"있으면 좋을 것 같다" 는 근거가 아니다. **실제 데이터나 실제 사고**가 근거다.

---

## 10. vNext Hardening backlog

Freeze 대상이 아니다. Connector 이후에 판단한다.

### 테넌트 인식 복합 FK

같은 종류의 cross-org 결함이 **두 번** 나왔다(재고 조정, 발주 품목). 발견하고
고쳤다는 것보다 **구조가 그 실수를 계속 허용한다**는 사실이 중요하다.

`(org_id, id)` 복합 유니크 + 복합 FK 로 바꾸면 DB 가 막는다. 우선 후보:

```
product           product_variant     product_source
inventory_transaction                 purchase_order_line
sales_order       expenses            approval_request
credential_secret
```

전환은 마이그레이션이 크고 되돌리기 어려워 Connector 전에 하지 않는다.

### 그 밖

```
SaaS 온보딩 · 요금제
운영 모니터링 · 알림 채널 연결
오프사이트 백업
```
