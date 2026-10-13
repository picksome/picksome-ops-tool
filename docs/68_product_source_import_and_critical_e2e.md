# 68. 상품↔공급처 연결 · Critical E2E · 재주문→발주

2026-08-11 · migration head M051 (신규 없음)

---

## 1. 이번에 닫은 것

```
P0 상품↔공급처 연결 가져오기   닫힘 (service + API + UI + 20건)
P1 Critical Playwright E2E     닫힘 (12건, 전체 199건 2회 연속)
P2 재주문 → 발주 초안 버튼      닫힘 (중복 방지 + E2E)
```

P3(역할 거버넌스)·P4(MCP 실 protocol)·P5·P6 은 **미착수**. §10 참고.

---

## PART B — 실제 소싱 데이터 분석 (P0)

### 7·8. 먼저 확인한 것 — 원본 데이터가 없다

작업 전에 실제 DB 와 엑셀을 전부 읽었다.

| 출처 | 결과 |
|---|---|
| `product` | 1,065건 |
| `product.supply_url` | **22건**, 전부 amazon.co.jp·rakuten.co.jp |
| `product.supplier_id` | **0건** |
| `supplier_site` | 54건 (도메인 53건) |
| `product_source` | **0건** |
| `product_source_cost_version` | **0건** |
| `아이템리스트` 시트 | 308행 중 제품명이 있는 것 **4행** (사실상 빈 양식) |
| `마진계산(구매대행)` 시트 | 1,035행, **공급처 열 자체가 없음** |
| `도매 매입 사이트들.xlsx` | 공급처 목록만 (이미 54건 등록됨) |

**상품↔공급처 매핑 데이터는 어디에도 없다.**

22개의 `supply_url` 은 일본 마켓플레이스 주소라 국내 도매 공급처 54곳과
호스트가 하나도 겹치지 않는다. 이걸로 자동 연결하면 0건이거나, 억지로
맞추면 전부 틀린다.

그래서 이 모듈은 **기존 파일을 읽는 도구가 아니라 사용자가 그 정보를 넣게
하는 도구**다. `GET /sourcing/import/template` 이 필요한 열을 알려준다.

### 11·13. 열쇠 — 완전일치만

```
상품    sku → business_key → product_id (UUID)
공급처  supplier_code → supplier_domain(정규화 호스트) → supplier_site_id
```

상품명·공급처명 열만 있는 파일은 **파일 수준에서 거부**한다. 잘못 연결하면
틀린 공급처의 원가로 발주가 나간다.

도메인은 정규화 후 완전일치다. `www.` 만 벗기고 **`m.` 은 벗기지 않는다** —
`m.petbtob.co.kr` 이 실제로 별도 공급처로 등록돼 있다. `domeggook.co.kr` 은
`domeggook.com` 이 아니다.

### 14. URL 두 개를 섞지 않는다

```
supplier_site.website_url   https://domeggook.com           공급처 사이트
product_source.source_url   https://domeggook.com/item/123  이 상품의 매입 페이지
```

### 15. 행 분류

```
NEW_MAPPING        새 연결
UPDATE_METADATA    연결은 있고 비어 있던 항목이 채워진다
EXISTING_SAME      이미 같다
CONFLICT_PRIMARY   이미 다른 주 소싱처가 있다
UNMATCHED_PRODUCT / UNMATCHED_SUPPLIER
INVALID
```

### 16·17. 주 소싱처

파일이 `Y` 로 표시한 것만 주 소싱처가 된다. **단일 소싱처라고 자동으로
확정하지 않는다** — 근거가 원본에 없으면 사람이 정할 일이고, 말없이 정하면
"왜 이 공급처로 발주가 나갔지" 에 답할 수 없다.

이미 다른 주 소싱처가 있으면 `CONFLICT_PRIMARY` 로 보고하고 바꾸지 않는다.
`uq_product_single_primary_source` 가 DB 에서도 막는다.

### 18·19. 없는 값

리드타임·MOQ 가 파일에 없으면 `NULL` 이다. `10일`·`1개` 같은 임의값을 만들지
않는다. 화면도 `0` 이 아니라 `—` 로 그린다.

### 20·21. 원가

매입가는 `product_source_cost_version` 에 **새 줄로 쌓인다.**
`product.cost_amount` 는 건드리지 않는다 — 그건 마진 계산에 쓰이는 확정값이고
`COST_CHANGE` 승인을 거쳐야 바뀐다. 파일 한 번으로 과거 마진이 흔들리면 안 된다.

같은 날 같은 값이면 버전도 안 쌓는다.

### 25·26. 멱등성과 동시성

재적용하면 새 연결 0건. `uq_product_source_pair` 에
`ON CONFLICT ... DO NOTHING` 을 걸어 **DB 가 최종 방어**한다.

계획과 반영 **사이에** 다른 경로가 같은 연결을 만드는 경우를 테스트로
고정했다 — check-then-insert 만 믿으면 동시 실행에서 둘 다 통과한다.

### 27. 자격증명

`ID`·`PW`·`비밀번호` 로 보이는 열은 **값을 읽지 않고** 개수만 센다.
`credential_detected` 로 알린다.

### 마이그레이션 없음

`uq_product_source_pair` 와 `uq_product_single_primary_source` 가 이미 있었다.
필요 없는 마이그레이션을 만들지 않았다.

---

## PART C — 완성도 UX

`/sourcing/import` 상단이 재주문이 왜 안 도는지 숫자로 보여준다.

```
상품 1,065 · 소싱처 연결됨 0 · 소싱처 없음 1,065
주 소싱처 없음 0 · 리드타임 없음 0 · MOQ 없음 0
```

각 항목이 재주문 차단 사유(`NO_SOURCE` / `NO_LEAD_TIME` …)와 짝을 이룬다.

---

## PART D — Critical E2E (P1)

실제 FastAPI + PostgreSQL + Next 로 도는 12건.

| 시나리오 | 확인한 것 |
|---|---|
| 재고 가져오기 | 검토는 안 쓰고, 적용하면 `OPENING` 1줄 |
| 재적용 | `DUPLICATE`, 원장 1줄 유지, 잔량 17 그대로 |
| 이름 매칭 거부 | 상품명 열만 있으면 거부, 재고 0 |
| 수동 조정 | 원장 `[17, -2]`, 잔량 15 — **덮어쓰기 아님** |
| 사유 없는 조정 | 버튼 잠김 |
| **소싱 연결** | 연결 전 `NO_SOURCE` → 후 리드타임 7·MOQ 10·MOQ 배수 권장 |
| 원가 보호 | 매입가 3000 넣어도 `product.cost_amount` 불변 |
| 재주문 기간 | 비어 있으면 계산 버튼 잠김 |
| 발주 → 입고 | 초안은 재고 불변, 입고해야 +8 |
| **취득원가** | 입고 직전 소싱 원가를 9999 로 바꿔도 취득원가 **3000** |
| 입고 재전송 | 같은 `receipt_key` 거부, 재고 안 늘어남 |
| 발주 생성 화면 | 초안 1건 생성 |
| 재주문 → 초안 | 버튼으로 초안 생성, 두 번째는 «기존 초안 보기» |
| 예외 센터 | 일일 리포트와 숫자 일치 |
| 미연결 표시 | "0건 정상" 으로 표시하지 않음 |
| 드릴다운 | 리포트 칩 → `?category=INVENTORY` |

### 고친 것

`e2e_db.py` 가 M048/M050 테이블을 청소하지 않고 있었다. 남겨 두면 "재고 N개"
를 확인하는 테스트가 앞 테스트의 원장을 세고 실행 순서에 결과가 좌우된다.
FK 순서대로 삭제를 추가했다.

시드 상품에는 `sku` 가 없고 `business_key` 만 있다. 테스트를 위해 시드에 sku 를
억지로 넣지 않고 있는 열쇠를 썼다.

### 결과

```
Playwright 199건 · retries=0 · 2회 연속 통과 (9.7분 / 9.8분)
외부 API 호출 없음
```

---

## PART E — 재주문 → 발주 초안 (P2)

공급처 묶음별 `[발주 초안 만들기]`. 추천값을 확정으로 가장하지 않고
"재주문 분석에서 넘어온 **추천값**" 이라고 밝히며, 수량·단가를 고칠 수 있다.

단가는 `unit_price`(소싱 원가 버전 우선)를 채우되 "소싱 원가에서 가져옴" 이라고
표시한다. 없으면 비워 둔다 — 0원 발주가 그대로 취득원가가 되면 안 된다.

같은 공급처에 처리 안 된 초안이 있으면 버튼 대신 «기존 초안 PO-… 보기»
링크가 뜬다. "이미 있습니다" 만 말하면 사용자가 찾아 헤맨다.

---

## 검증

```
백엔드          992 passed, 1 xfailed(기존)
신규 테스트      20건 (소싱 가져오기)
프론트          299 passed
Playwright      199 passed × 2회 (retries=0)
ruff / tsc / lint / build   통과
production mock  신규 코드 0건
```

```
target_database          = picksome_migcheck_13149247
is_scratch               = true
development_db_untouched = true (picksome_ops 미접속)
base → head(51) → M050 → head   전부 성공, 임시 DB 삭제
```

개발 DB 완성도는 작업 전후가 같다(연결 0건) — 실제 데이터를 건드리지 않았다.

---

## 10. 하지 않은 것

| 항목 | 상태 |
|---|---|
| P3 역할/민감원가 거버넌스 (§52~60) | **미착수** — 역할별 HTTP 테스트, 파생 노출 검토 |
| P4 MCP 실제 protocol 검증 (§61~69) | **미착수** — 도구는 있으나 stdio 로 재검증 안 함 |
| P5 데이터 품질 보강 (§70~74) | **미착수** — 기존 스캐너 3종 그대로 |
| P6 외부 API 재확인 (§75~78) | **미착수** |
| §30 대량 리드타임 편집 UX | 미착수 — 파일로만 가능 |
| §51 발주 초안의 재주문 근거 추적 | 미착수 — 메모에만 남음 |

우선순위대로 P0·P1·P2 를 끝까지 닫고 멈췄다. 반쯤 만든 범위는 없다.
