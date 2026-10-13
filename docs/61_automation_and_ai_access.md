# 61. 자동화 엔진 · 서비스 계정 · MCP 접근

M044 (서비스 계정·API 토큰), M045 (자동화 정책), M046 (승인 요청자 확장)
2026-08-10

---

## 0. 이번 세션의 전제 — 실제 Connector 는 착수하지 못했다

```
FIRST_REAL_CONNECTOR = BLOCKED
```

착수 조건 네 가지를 workspace 와 DB 에서 직접 확인했다.

| 후보 | 공식 문서 | credential | 권한 | 실제 응답 샘플 |
|---|---|---|---|---|
| NEXT_ENGINE | ❌ `documentation_url` NULL | ❌ | ❌ | ❌ |
| WINDLY | ❌ NULL | ❌ | ❌ | ❌ |
| 쿠팡·네이버·11번가·ESM·토스 | — | ❌ 카탈로그에 없음 | — | ❌ |

확인한 것:

```
external_system        2건 (문서 URL 둘 다 NULL)
external_account       0건
credential_secret      0건
integration_account    0건
환경변수                벤더 관련 키 없음
workspace              API 문서·HAR·응답 샘플 파일 없음
```

`sales_order.marketplace` 에 `COUPANG`/`SMARTSTORE` 값이 있지만 이건 **Excel 로
넣은 과거 판매 기록**이고 API 연동이 아니다.

그래서 §4 지침대로 중단하지 않고 **Priority 2**(자동화·서비스 계정·MCP·리포트)로
전환했다. 추측으로 Adapter 를 만들지 않았다.

---

## 1. 자동화 엔진 (M045)

### 새로 만든 것은 정책뿐이다

기존 구조를 먼저 읽고 재사용 여부를 판단했다.

| 이미 있는 것 | 무엇 | 판단 |
|---|---|---|
| `worker_run` | 언제 돌았나 | **재사용** |
| `operation_issue` | 사람이 봐야 할 것 (dedupe·자동해소) | **재사용** |
| `approval_request` | 승인이 필요한 변경 | **재사용** |
| `notification` | 알림 | **재사용** |

없는 것은 하나였다: **"무엇을 자동으로 할지, 어디까지 허용할지" 를 조직이 정한
기록.** worker 목록은 코드에 고정돼 있어 사용자가 켜고 끌 수 없고, 한도를 담을
곳도 없다.

`automation_run` 을 `worker_run` 과 합치지 않은 이유: worker_run 은 **프로세스
한 번**이다. 자동화 배치 한 번이 정책 다섯 개를 평가하면 그 결과가 한 run 에
뭉개진다 — "마진 감시는 3건을 찾았고 원가 감시는 실패했다" 를 구분할 수 없다.

### 세 가지 모드

```
REPORT_ONLY    관측만. 아무것도 쓰지 않는다   ← 기본값
PROPOSE        승인 요청을 만든다
AUTO_EXECUTE   한도 안에서 직접 실행한다
```

기본이 `REPORT_ONLY` 인 이유: 자동 실행은 사람이 켜는 결정이어야 한다.

**한도 없는 `AUTO_EXECUTE` 는 제안으로 떨어진다.** 무제한 자동 실행을 허용하지
않는다. 화면도 저장 전에 그 사실을 미리 말한다.

### 한도(guardrail)는 검증한다

generic JSON 잡탕을 만들지 않았다. 모르는 키는 **거부**한다.

```
max_action_per_run   ← 오타. 거부한다
max_actions_per_run  ← 정상
```

검증하지 않는 설정 칸은 있으나 마나다 — 오타 하나로 한도가 조용히 사라지고,
사용자는 한도를 걸었다고 믿는다.

### 자동화하지 않는 일

```
PAYOUT_EXECUTE · BANK_TRANSFER · MONTH_REOPEN
CREDENTIAL_CHANGE · MANUAL_SETTLEMENT_MATCH
```

한도를 걸어도 한도 계산이 틀리면 끝이다. 그래서 **경로 자체를 만들지 않았다.**
화면에 제외 목록을 보여준다 — 숨기면 "왜 지급 자동화가 없지" 를 계속 묻는다.

### 지금 도는 자동화

| 유형 | 모드 | 상태 |
|---|---|---|
| `MARGIN_MONITOR` | REPORT_ONLY / PROPOSE | 동작 |
| `SOURCE_COST_MONITOR` | REPORT_ONLY / PROPOSE | 동작 (승인 요청까지) |
| `MONTH_CLOSE_PRECHECK` | REPORT_ONLY | 동작 |
| `DAILY_REPORT` | REPORT_ONLY | 동작 |
| `ORDER_SYNC` | REPORT_ONLY / AUTO_EXECUTE | **커넥터 없음 — BLOCKED 로 보고** |

`INVENTORY_REORDER` 는 **만들지 않았다.** 재고 데이터가 없어서 동작하는 척만
하게 된다.

`ORDER_SYNC` 는 커넥터가 없을 때 성공으로도 실패로도 만들지 않는다.
`BLOCKED / CONNECTOR_UNAVAILABLE` 로 남긴다.

### 임계값을 지어내지 않는다

"마진 15% 미만이면 위험" 은 업무 판단이다. 정책에 사람이 넣은 값이 없으면
**아무것도 하지 않는다.** 기본값을 정해 두면 그 숫자가 근거처럼 굳는다.

---

## 2. 서비스 계정 · API 토큰 (M044)

### 사람 토큰을 재사용하지 않는다

AI 에게 사람의 JWT 를 주면 세 가지가 무너진다.

```
감사   "김대표가 원가를 바꿨다" 로 남는다. 실제로는 에이전트가 했다
권한   그 사람의 모든 권한을 갖는다. 필요한 건 상품 조회 하나인데
수명   사람 세션은 12시간이다. 무인 실행은 그때마다 멈춘다
```

`service_account` 에는 **role 이 없다.** "ADMIN 서비스 계정" 을 만들 수 있게
하면 사람 계정과 같아진다. 할 수 있는 일은 **토큰마다** scope 로 정한다.

### 해시가 SHA-256 인 근거

비밀번호에는 argon2 를 쓴다. 여기는 다르다 — 위협 모델이 다르기 때문이다.

```
사람 비밀번호   30~40비트   사전·규칙 공격이 통한다 → 추측 1회 비용을 올린다
기계 토큰       256비트     사전이 없다             → 2^256 앞에서 속도는 무의미
```

토큰은 `secrets.token_urlsafe(32)` 다. DB 가 유출돼도 SHA-256 을 되돌릴 방법은
전수 탐색뿐이고, 그건 해시가 얼마나 느리든 불가능하다.

반대로 argon2 를 쓰면 **모든 API·MCP 호출마다 100~300ms** 가 붙는다. 얻는 것
없이 치르는 비용이다. GitHub PAT·Stripe 가 기계 토큰에 빠른 해시를 쓰는 것과
같은 근거다.

⚠️ 이 근거는 **토큰을 우리가 만들 때만** 성립한다. 사용자가 토큰 문자열을 정하게
하면 무너지므로 그런 API 를 만들지 않았다.

`token_prefix` 는 인증에 쓰지 않는다. 행을 찾는 데만 쓰고, 판정은 전체 해시의
**상수 시간 비교**로 한다.

### 자격증명(M042)과 다른 것이다

```
외부로 나가는 자격증명   SecretStore   복호화 가능해야 한다 (우리가 써야 하니까)
들어오는 API 토큰        해시          복호화할 이유가 없다 (검증만 하면 된다)
```

**API 토큰을 SecretStore 에 넣지 않는다.** 복원 가능하게 저장할 이유가 없고,
할 수 있게 만들면 언젠가 누군가 조회 화면을 만든다.

### 발급 가능한 권한

읽기 10종 + `approvals:create` 뿐이다. **실행 scope 를 만들지 않았다** —
실행이 필요한 일은 `automation_policy` 가 한도와 함께 정하고 그 경로로만 간다.

발급 자체를 막는 것:

```
payouts:execute · month:reopen · credentials:manage · bank:transfer
```

"만들어 두고 기본 비활성" 으로 두지 않았다. 그러면 언젠가 켜진다.

---

## 3. 감사 주체 (M044 · M046)

`audit_log` 에 `actor_type` / `actor_service_account_id` / `initiated_by_user_id`
를 추가했다. 기존 행은 backfill 하지 않고 NULL 을 `USER` 로 읽는다.

M046 은 승인 요청자에 서비스 계정을 허용한다. **테스트가 이 필요성을 잡았다** —
`requested_by` 가 NOT NULL FK 라 에이전트가 제안을 만들 수 없었다.

사람 계정을 빌려 쓰지 않은 이유: 승인함에 "김대표가 요청함" 으로 뜨면 승인자가
틀린 전제 위에서 판단한다. 알림 문구도 서비스 계정 이름을 쓴다 —
`{계정명} (자동)`.

`applied_by_type` 이 이미 같은 문제를 푼 적이 있어(M019) 같은 패턴을 따랐다.

---

## 4. MCP — 선택 모듈

### 의존 방향

```
Claude/OpenClaw ──stdio(MCP)──▶ mcp_server ──HTTP+토큰──▶ Seller OS ──▶ service
```

`backend/app/` 안에서 `mcp` 패키지를 import 하는 곳은 **한 군데도 없다.**
`test_core_never_imports_the_mcp_sdk` 가 그 사실을 고정한다.

MCP SDK 를 `backend/pyproject.toml` 에 넣지 않았다. Seller OS 를 설치하는 사람이
AI SDK 를 함께 설치할 이유가 없다. 별도 `mcp_server/requirements.txt` 와 별도
venv 를 쓴다.

`MCP_ENABLED=false`(기본)면 `/api/v1/mcp/*` 가 **404** 다. "설정은 껐는데 부르면
동작한다" 를 만들지 않는다.

### tool

```
READ      search_products · get_product · compare_product_sources
          get_sales_summary · get_cashflow · list_operation_issues
          get_daily_report
PROPOSE   propose_cost_change
EXECUTE   ← 없다
```

- 목록 필터는 **보안이 아니다.** 이름만 알면 부를 수 있으므로 호출 시점에 서버가
  scope 를 다시 확인한다(테스트로 고정).
- `cost:read` 가 없으면 원가와 **파생값**(마진·차이)을 함께 가린다. 원가만 가리고
  마진을 열어 두면 역산이 된다.
- 한 번에 최대 100건. AI 에게 10만 건을 넘기지 않는다.
- SQL 을 만들지 않는다. tool 은 기존 service 를 부른다.
- tool 설명에 내부 경로·환경변수 이름을 쓰지 않는다 — 그대로 모델 컨텍스트로
  들어간다.

`propose_cost_change` 는 `product.cost_amount` 를 바꾸지 않는다. `COST_CHANGE`
승인 요청을 만들고 끝난다.

---

## 5. 일일 운영 리포트

AI 를 부르지 않는다. 숫자는 기존 service 가 계산한 것을 **모으기만** 한다.

```
deterministic dataset   ← 여기 (숫자의 진실)
        ↓
AI summary              ← 나중에 (읽기 좋은 문장)
```

순서를 뒤집으면 AI 가 raw 데이터로 재무 숫자를 새로 계산하고, 그 숫자는 화면과
다를 수 있다.

**확인이 필요한 일이 맨 위**다. 숫자를 먼저 보여주면 사용자는 매일 같은 숫자를
훑다가 결국 안 보게 된다.

모르는 것을 0 으로 만들지 않는다.

```
클레임        null + "클레임 데이터 연동이 아직 없습니다"
현금 잔액     기준 잔액 없으면 "확인 불가"
마진 감시     꺼져 있으면 "켜져 있지 않습니다"
```

**자동화 상태**도 리포트에 넣었다. "자동으로 하고 있다" 는 믿음이 틀렸을 때가
가장 위험하다 — 사용자는 확인을 멈췄는데 아무것도 안 돌고 있는 상태가 된다.

---

## 6. 이번 세션에 찾은 결함

### ① 리포트 한 구획의 실패가 나머지를 전부 죽였다

Postgres 는 실패한 트랜잭션에서 이후 쿼리를 전부 거부한다. 현금 구획이 잘못된
컬럼명으로 터지자 자동화 구획까지 `InternalError` 로 나왔고, 원인은 자동화와
무관했다. 구획 실패마다 `rollback()` 하도록 고쳤다. 스모크 테스트가 잡았다.

### ② 마진 감시가 없는 컬럼을 읽고 있었다

`product_channel_listing` 에 `sale_price`·`marketplace` 가 있다고 가정했는데,
실제 스키마는 세 겹이다.

```
integration_account       어느 판매자 계정인가 (본점 / 2호점)
product_channel_listing   그 계정에 이 상품을 올렸다
product_channel_price     그 등록의 가격
```

### ③ `sale_price` 는 판매가가 아니라 **할인가**였다

`ck_pcp_sale_window` 제약이 알려줬다 — 할인가에는 시작일이 필수다. 정상가는
`regular_price` 이고 할인은 기간 안에서만 유효하다. 할인가를 그냥 판매가로 읽으면
할인이 끝난 뒤에도 낮은 값으로 계산해 마진을 실제보다 나쁘게 본다. 거짓 경고가
쌓이면 사용자가 감시를 꺼 버린다. 기간 판정을 넣고 테스트 두 개로 고정했다.

### ④ 승인 요청에 사람이 아닌 요청자를 담을 수 없었다 → M046

### ⑤ 자동화 정책 교체 순서 (M042 세션의 연장선)

`uq_credential_secret_owner` 와 같은 방식으로, 한 소유자에 살아 있는 것이
하나여야 한다는 제약이 잘못된 삽입 순서를 잡았다.

---

## 7. Playwright 간헐 timeout — **원인 확정**

지난 세션에 심어 둔 구간 계측이 이번에 실제로 답을 줬다.

### 이전까지

```
가설: next dev 라우트 컴파일   → 측정으로 반증 (콜드 캐시에서도 /expenses 732ms)
원인: 미확정
```

### 이번 실행에서 잡힌 것

trace 의 구간별 소요:

```
   1705ms  reseed
    870ms  페이지 이동
      (빠름)  dialog 대기 · 대화상자 열기 · 입력
  43726ms  저장 왕복   ←
```

43.7초 전부가 **`조정 만들기` 버튼 클릭**에 있었다. Playwright 는 버튼이
actionable 해질 때까지 기다린다.

실패 스냅샷:

```
- textbox "조정 귀속일 필수": 2026-08-15      ← 날짜는 들어갔다
- textbox "차액 필수" [active]:               ← 비어 있다
- text: 50000E2E 화면 조정 사유                ← 두 입력값이 텍스트 노드로 떠 있다
- textbox "조정 사유 필수":                    ← 비어 있다
- button "조정 만들기" [disabled]
```

즉 **입력 요소가 타이핑 도중 교체되면서 입력이 유실됐다.** 값이 없으니
`invalid` 가 계속 참이고, 버튼은 45초 동안 비활성으로 남는다.

이것은 테스트만의 문제가 아니다 — **사용자가 이 대화상자에 입력하다가 배경
refetch 가 도착하면 같은 방식으로 입력을 잃을 수 있다.**

### 이번에 한 것

테스트가 각 입력 직후 값을 확인하도록 했다. 45초 뒤 "버튼이 비활성" 이 아니라
**입력이 유실된 그 지점에서 즉시** 실패한다. 재시도로 덮은 것이 아니라 같은
결함을 더 빨리·더 정확히 드러낸다. timeout 도 retries 도 건드리지 않았다.

### 남은 일 (미완)

**대화상자 입력 유실 자체는 아직 고치지 않았다.** 어떤 렌더가 입력 요소를
교체하는지(부모 refetch·AlertDialog 재마운트) 특정하려면 React 렌더 추적이
필요하고, 이번 세션 범위를 넘는다. 재현 조건은 확보됐다 — 반복 실행 시
드물게 발생하고, 발생 시 즉시 실패한다.

---

## 8. 검증

```
backend  ruff app scripts        통과
backend  pytest                  861 passed / 1 xfailed
frontend vitest                  291 (25 files)
frontend tsc                     통과
frontend lint                    기존 경고 1건 (products/page.tsx, 이번 변경 아님)
frontend build                   통과
migration  scratch DB head→base→head   88 테이블 · head M046
```

---

## 9. 다음

1. **실제 marketplace credential 확보** — 이게 없으면 주문·클레임·정산 자동
   수집은 계속 못 한다. 자료가 가장 쉬운 쿠팡 Wing OPEN API 를 권한다
2. 대화상자 입력 유실(§7) 원인 특정
3. `INVENTORY_REORDER` 는 재고 데이터가 생긴 뒤에
4. MCP tool 확장은 실제 사용 패턴을 보고 — 미리 늘리지 않는다
