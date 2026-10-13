# 66. 마이그레이션 안전장치 · 예외 센터 · 재고 UX

M049 · 2026-08-11

---

## 1. 이번에 만든 것

```
app/db/migration_target.py       파괴적 마이그레이션 대상 판별·차단
scripts/verify_migrations.py     임시 DB 생성 → 왕복 → 삭제
app/services/exception_center.py 예외 읽기 모델 (새 테이블 없음)
app/api/v1/exceptions.py         GET /operations/exceptions
M049                             purchase_order.created_by_service_account
MCP tools 6종                    재고·발주 읽기 + 발주 초안 제안
/operations/exceptions           확인 필요 화면
재고 원장·조정 UI                 /inventory 행 펼침
```

---

## PART A — 마이그레이션 안전장치

### 2. `ALEMBIC_DATABASE_URL` 처리

`env.py` 는 이제 이 순서로 대상을 정한다.

```
1. -x db_url=...
2. ALEMBIC_DATABASE_URL
3. settings.DATABASE_URL
```

덮었으면 `[alembic] 대상 DB 재지정: <이름>` 을 찍는다. **비밀번호는 찍지
않는다** — DB 이름만.

단위 테스트가 이 연결을 고정한다
(`test_env_py_honours_alembic_database_url`). 문서가 아니라 코드를 읽어
override 가 `set_main_option` 보다 먼저 오는지 확인한다. 순서가 뒤집히면
조용히 무시되기 때문이다.

### 3. 대상 식별

`describe_target()` 이 URL 에서 **비밀이 아닌 것만** 뽑는다.

```
host  port  database  APP_ENV  is_scratch  is_development_main
```

`MigrationTarget` 은 URL 을 통째로 들고 다니지 않는다. 들고 다니면 로그나
예외 메시지 어딘가에서 비밀번호가 샌다.

### 4·7·8. 세 겹의 문

`assert_safe_for_destructive()` 는 셋 다 통과해야 진행한다.

```
1. APP_ENV 가 prod/production/staging 이 아닌가   ← 가장 먼저
2. 개발 메인 DB 가 아닌가
3. 이름이 _migcheck / _migration_test 로 끝나는가
```

순서가 의미가 있다. 이름 검사를 먼저 하면 `_migcheck` 로 끝나는 운영 DB 가
통과한다.

**사람이 직접 치는 `alembic downgrade` 는 막지 않는다.** 막으면 정상적인
개발이 불가능하다. 이 가드는 자동 검증 스크립트 경로에만 적용된다 — 둘을
구분하는 것이 요점이다.

### 5·6·9. 임시 DB

```
picksome_migcheck_<난수 8자>
```

난수를 붙인다. 같은 이름이 남아 있으면 앞 실행의 잔재가 결과를 오염시킨다.

만들고 → **`pg_database` 에서 확인하고** → 마이그레이션을 돌린다. 예전
사고에서 `picksome_migcheck` 는 존재하지도 않았는데 그 사실이 드러나지 않았다.

임시 DB 는 운영 역할이 아니라 `picksome_test` 로 다룬다. 운영 역할에
CREATEDB 를 주면 애플리케이션이 임의로 DB 를 만들 수 있게 되어 최소권한이
깨진다.

### 10. 회귀 테스트

`app/tests/unit/test_migration_target.py` 10건.

```
scratch 지정        → 통과
개발 메인 DB        → 거부 ("개발 메인")
APP_ENV=prod        → 거부 (이름이 scratch 여도)
모르는 DB           → 거부 (fail-closed)
비밀번호            → describe() 어디에도 없음
env.py override     → 코드로 확인
```

### 11. 실제 검증 결과

```
target_database          = picksome_migcheck_a92e7697
is_scratch               = true
development_db_untouched = true (picksome_ops 미접속)

base → head (49개)  →  M049 → M048  →  M048 → M049   전부 성공
임시 DB 삭제 완료
```

---

## PART G — 예외 센터

### 48. 새 테이블 없음

읽기 모델이다. `operation_issue` / `approval_request` /
`inventory_transaction` / `purchase_order` / `marketplace_settlement_record` /
`external_account` / `external_sync_run` 를 조합한다.

별도 `exception` 테이블을 만들면 원본과 어긋나는 두 번째 진실이 생긴다.

### 49·50. 카테고리와 "0 의 다섯 가지 뜻"

```
ORDER        NOT_CONNECTED   클레임 API 미연결 — 셀 수 없다
INVENTORY    NEGATIVE_INVENTORY / PURCHASE_OVERDUE /
             REORDER_REQUIRED / REORDER_DATA_INCOMPLETE
SETTLEMENT   UNMATCHED_SETTLEMENT
APPROVAL     PENDING_APPROVAL
INTEGRATION  CREDENTIAL_BROKEN / CONNECTOR_UNAVAILABLE / SYNC_FAILED
```

카테고리마다 `status` 가 있다.

| status | 뜻 | 화면 문구 |
|---|---|---|
| `OK` | 확인했고 없다 | 이상 없음 |
| `ATTENTION` | 손댈 것이 있다 | N건 |
| `NOT_CONNECTED` | API 가 없어 못 센다 | 미연결 |
| `NOT_CONFIGURED` | 기준을 안 정해 계산 못 한다 | 설정 필요 |
| `ERROR` | 불러오기 실패 | 불러오지 못함 |

다섯 경우 모두 `count = 0` 이 나온다. 그래서 화면은 **건수가 아니라 status
에서** 문구를 만든다. Vitest 가 이걸 고정한다 — 미연결과 이상 없음이 같은
문자열이 되면 실패한다.

### 33. 재주문 기준의 단일 출처

`velocity_days` / `coverage_days` 는 **`REORDER_MONITOR` 정책의 guardrail** 이
유일한 출처다.

```
자동화·예외 센터·일일 리포트   정책을 읽는다
/inventory 화면 입력창         탐색용 (일회성 질의)
```

정책이 없으면 예외 센터는 `NOT_CONFIGURED` 이고 계산하지 않는다. 여기서
30일을 지어내면 "기본값 금지" 원칙이 화면 뒤에서 조용히 깨진다.

### 53·54. 심각도

`operation_issue` 의 `BLOCKING` / `WARNING` / `INFO` 만 쓴다. 새 점수 체계를
만들지 않았다 — 두 체계가 생기면 같은 일이 화면마다 다른 급으로 보인다.

`REORDER_DATA_INCOMPLETE` 는 `INFO` 다. 기능이 안 되는 것이지 마감을 막는 것이
아니다(§74).

### 59. 일일 리포트와 숫자가 같다

`daily_report.build()` 가 `exception_center.summary()` 를 호출한다. 각자 세지
않으므로 갈라질 수 없고, 테스트가 두 값이 같은지 확인한다.

### 한 구획이 깨져도 나머지가 산다

구획마다 `try` + `db.rollback()`. 실제로 이 설계가 첫 스모크에서 컬럼명 오류
두 개를 잡아냈다 — 나머지 구획은 정상 동작했다.

---

## PART B/C — 재고 UX

`/inventory` 의 행을 누르면 펼쳐진다.

```
현재 보유    12
예약         주문 연동 안 됨      ← 0 이 아니다
판매가능     계산 불가            ← 예약을 모르니 계산할 수 없다
입고예정     20

재고 조정   [감소|증가] [수량] [사유(필수)]   → 차이만 입력
원장        일시 / 유형 / 증감 / 취득단가 / 사유 / 기록자
```

### 17. 잔량을 덮어쓰지 않는다

"현재 재고 = 12" 입력을 만들지 않았다. 두 사람이 동시에 세면 한쪽 결과가
조용히 사라지고, 나중에 "왜 12 가 됐나" 에 답할 수 없다.

화면은 **방향 + 크기**를 받고 부호는 코드가 만든다. 사용자가 마이너스를 직접
치게 하면 부호를 빠뜨려 반대로 조정한다.

취득이 아닌 움직임(판매·조정)의 단가는 `—` 로 표시한다. 0 이 아니다.

---

## PART J — MCP

### 65·66·67. 도구와 scope

```
get_inventory_status        inventory:read
list_inventory_movements    inventory:read
list_reorder_candidates     inventory:read
list_order_exceptions       issues:read
get_purchase_order          purchasing:read
propose_purchase_order      purchasing:propose   ← PROPOSE
```

전부 서비스를 호출한다. **MCP 안에서 재고를 다시 계산하지 않는다** — 두 곳에서
계산하면 화면과 AI 가 다른 숫자를 말한다.

`cost:read` 가 없으면 취득단가·발주금액·예상금액이 지워진다. 재고 수량과 원가는
다른 축이라 scope 를 나눴다(§77).

### 68·69. 제안까지만

`propose_purchase_order` 는 `DRAFT` 를 만들고 끝난다. `place_purchase_order`
같은 tool 은 **없다**.

`purchasing:execute` 는 `FORBIDDEN_SCOPES` 에 있어 토큰 발급 자체가 거부된다.
"만들어 두고 기본 비활성" 으로 두면 언젠가 켜진다.

### 79. 감사 주체

에이전트가 만든 발주는 `created_by = NULL`,
`created_by_service_account = <계정>` 이다(M049). 사람 칸에 넣으면 감사
로그에서 사람과 기계를 구분할 수 없다. 승인 요청이 M046 에서 같은 문제를 같은
방식으로 풀었다.

M049 는 `CHECK` 로 **셋 중 하나는 반드시 있게** 한다 — 주체를 모르는 발주가
생기면 되짚을 방법이 없다.

---

## 검증

```
백엔드          931 passed, 1 xfailed(기존)      (이전 901)
신규 테스트      30건 (마이그레이션 10 + 예외센터 9 + MCP 11)
프론트          295 + 4 passed
ruff / tsc      통과
lint            기존 경고 1건(products/page.tsx)만
build           /operations/exceptions 포함 성공
마이그레이션     실제 임시 DB 왕복 (위 §11)
production mock  신규 코드에서 0건
```

---

## 이번 세션에서 하지 않은 것

작업지시 대비 **미착수**:

| 항목 | 상태 |
|---|---|
| PART F 재고 Excel/CSV Import | **미착수** (ANALYZE/DRY_RUN/APPLY 전체) |
| PART H Daily Report 자동 생성·스케줄 | **미착수** — 조회는 되나 worker 연결 없음 |
| PART I 리포트 전달(IN_APP 포함) | **미착수** |
| PART K Autopilot 안전 AUTO_EXECUTE | **미착수** |
| PART L 재고 데이터 품질 스캐너 | **부분** — 예외 센터가 읽기로 보여주지만 `operation_issue` 를 만들지는 않음 |
| PART D 발주 생성 UI | **미착수** — 목록·상세·입고만 있고 신규 작성 화면 없음 |
| PART E 재주문 → PROPOSE 버튼 | **미착수** — 자동화/MCP 경로만 있고 화면 버튼 없음 |
| PART P 외부 API 재점검 | **미착수** |
| §89 Playwright E2E | **미실행** |
| §90 Import E2E | **미착수** |

권한(§75)은 새 `permission` 항목을 만들지 않고 기존 `require_roles` 로만
처리했다 — 현재 아키텍처가 role 기반이고, 여기에 권한 문자열 체계를 새로
얹으면 두 체계가 공존하게 된다. 별도 판단이 필요하다.
