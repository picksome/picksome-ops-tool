# 62. 입력 유실 수정 · MCP 실접속 · PROPOSE 실동작

2026-08-10 (이어서) · 마이그레이션 없음 (M046 유지)

---

## 1. P0 — 배경 refetch 가 편집 중인 입력을 덮었다

### 원인 (재현함)

```js
React.useEffect(() => {
  setDraft(draftOf(expense));   // ← 사용자가 입력한 값을 덮어쓴다
  setDirty(false);
}, [open, expense, expenseId, isEdit]);
```

`expense` 가 의존성에 있다. React Query 는 `refetchOnWindowFocus: true` 가
기본이고 이 앱의 `staleTime` 은 30초다. **카드 명세서를 보면서 30초 넘게
입력하다가 창을 다녀오면 입력이 사라진다** — 정확히 이 화면이 쓰이는 상황이다.

### 왜 "가끔" 이었나

structural sharing. 서버 값이 같으면 React Query 가 **같은 객체**를 돌려주므로
effect 가 아예 안 돈다. 다른 사람이 같은 행을 고쳤을 때만 새 객체가 되고, 그때만
터진다.

E2E 간헐 timeout 도 같은 뿌리였다(docs/61 §7). 입력이 유실되니 `invalid` 이
계속 참이고 저장 버튼이 45초 동안 비활성으로 남았다.

### 재현

```
before:  expect(memo).toHaveValue("내 메모")
         → Received: "다른 사람이 고친 메모"
after:   4건 통과
```

### 수정

```
dirty 인 동안        서버 응답으로 draft 를 덮지 않는다
dirty 가 아니면       최신 서버 값으로 갱신한다 (원하는 동작이다)
대상 행이 바뀌면       새로 초기화한다
서버 값이 달라졌으면    배너로 알린다 — 덮지도, 조용히 무시하지도 않는다
```

`dirty` 를 effect 의존성에 넣지 않았다(ref 로 읽는다). 넣으면 첫 글자를 치는
순간 effect 가 다시 돌아 헛된 안내가 뜬다.

**상품 drawer 에도 같은 패턴이 있어 함께 고쳤다.** 검색 결과 이 둘뿐이라
전체를 리팩터링하지 않았다.

### 테스트 위치

Vitest 4건(`expense-drawer-dirty.test.tsx`). E2E 에는 넣지 않았다 —
React Query v5 의 refetch 계기는 헤드리스에서 이벤트를 흉내 내는 것으로 켜지지
않고, 억지로 켜면 **흉내낸 것이 맞는지**를 검증하는 테스트가 된다. 시도했고
안 됐다는 사실을 spec 파일에 적어 뒀다.

timeout 도 retry 도 건드리지 않았다.

---

## 2. P1 — MCP 실제 클라이언트 연결

내부 함수 호출이 아니라 **공식 SDK 클라이언트가 stdio 로** 붙었다.

```
smoke_client ──stdio(MCP)──▶ server.py ──HTTP+토큰──▶ Seller OS ──▶ service
```

```
SDK          mcp 2.0.0 (공식 Python SDK)
transport    stdio (server.py 가 쓰는 것과 같은 전송)
initialize   ok
tools/list   4개 (토큰 scope 에 맞게)
tools/call   search_products / get_product / get_sales_summary
             / get_daily_report / propose_cost_change  전부 ok
```

### 이 스모크가 잡은 결함 2건

Python SDK 의 모델 필드는 **snake_case** 이고 camelCase 는 전선 위 alias 다.
TS 스펙 이름으로 넘기면 **조용히 무시된다.**

```
inputSchema= → input_schema=   클라이언트가 인자 없는 tool 을 본다
isError=     → is_error=       실패가 성공으로 전달된다
```

두 번째가 위험하다. 첫 실행에서 `get_cashflow` 가 "권한 없이 실행됐다" 로
나왔는데, **서버는 정확히 403 을 주고 있었고** 어댑터가 그걸 성공으로 옮기고
있었다. 에이전트는 거부당하고도 "됐다" 고 보고했을 것이다.

내부 함수 테스트로는 절대 안 잡힌다.

### 검증 결과

| 항목 | 결과 |
|---|---|
| `MCP_ENABLED=false` | 404 (경로가 없는 것처럼) |
| 인증 없음 | 401 |
| 사람 JWT | 401 (서비스 계정 토큰만) |
| 정상 scope | tool 성공 |
| scope 부족 | **403** — 목록에 없어도 이름으로 부르면 서버가 막는다 |
| org 격리 | 404 (존재 은폐) |
| `cost:read` 없음 | `cost_amount = "(권한 없음)"` |
| 파생 원가 | `compare_product_sources` 403 |
| 감사 | `actor_type=SERVICE_ACCOUNT`, `actor_user_id=NULL` |
| PROPOSE | `approval_request PENDING`, `product.cost_amount` 불변 |

---

## 3. P3 — PROPOSE 실동작

두 가지가 막고 있었다.

**① 무인 실행이 아무것도 만들지 않았다.** `actor_user_id` 가 있을 때만
제안해서 스케줄 실행은 조용히 지나갔다 — 자동화의 존재 이유가 무인 실행인데.

사람이 없으면 자동화가 **자기 이름**으로 올린다(M046 서비스 계정 요청자,
`Seller OS 자동화`). 마지막에 화면을 눌렀던 사람 이름을 빌리면 승인함에
"김대표가 새벽 3시에 원가 변경 요청" 으로 뜬다. 이 주체는 **토큰이 없어**
외부 접속이 불가능하다 — 이름일 뿐이다.

**② 거절해도 다음 스캔이 같은 제안을 다시 올렸다.** 거절은 "이 값은 아니다"
라는 답이고, 같은 답을 매일 다시 묻는 것은 자동화가 아니라 잔소리다.

```
PENDING 이 있으면        BLOCKED / PENDING_PROPOSAL_EXISTS
같은 값으로 거절됐으면    BLOCKED / ALREADY_REJECTED
원가가 또 바뀌면          새 근거 → 다시 제안
기준이 없으면             제안하지 않는다 (임의 threshold 금지)
```

막힌 이유를 `BLOCKED` 결과로 남긴다 — "안 만들었다" 와 "만들 게 없었다" 는
다르다.

---

## 4. P2 — 일일 리포트 구획 추가

- **최근 원가 변화**: 변화량만 보여준다. "급등"·"위험" 판정을 붙이지 않는다 —
  몇 %가 위험한지는 업무 기준이다. 첫 원가는 비교 대상이 없어 변화로 세지 않는다
- **연동 상태**: 미구현 / 자격증명 미설정 / 사용 가능을 **따로** 보여준다.
  미구현을 "정상" 으로 보여주면 사용자가 연동이 돌고 있다고 믿은 채 확인을 멈춘다

---

## 5. Connector readiness — 재확인

**자료가 일부 생겼다.** `D:\picksomeprod\api\` 에 자격증명 파일이 나타났다.

```
api key.txt      11번가 API key
naver.txt        네이버 client id + secret
nextengine.txt   비어 있음
```

착수 조건 네 가지 중:

| 조건 | 네이버 | 11번가 |
|---|---|---|
| 공식 API 문서 | ❌ 없음 | ❌ 없음 |
| credential | ✅ 있음 | ✅ 있음 |
| 필요한 권한 | ❓ 확인 불가(문서 없음) | ❓ |
| 실제 response 샘플 | ❌ 없음 | ❌ 없음 |

```
FIRST_REAL_CONNECTOR = BLOCKED
reason = BLOCKED_API_DOCUMENTATION + BLOCKED_API_SAMPLE
```

문서 없이 endpoint 를 추측하는 것이 정확히 금지된 일이라 착수하지 않았다.
credential 이 생겼으므로 **문서와 응답 샘플만 확보되면 바로 시작할 수 있다.**

### ⚠️ 보안 — 지금 조치가 필요하다

자격증명이 **평문 텍스트 파일**로 디스크에 있다. 이 프로젝트는 이미 이걸 위한
저장소를 갖고 있다(M042 SecretStore, Fernet 암호화 + 마스터 키 분리).

권장:

```
1. 외부 계정 등록      설정 → 외부 시스템
2. 자격증명 저장       그 화면에서 입력 (암호화되어 저장, 재조회 불가)
3. 평문 파일 삭제
```

이 작업은 사용자의 자격증명을 다루는 일이라 **대신 하지 않았다.**

---

## 6. 검증

```
backend   ruff app scripts       통과
backend   pytest                 867 passed / 1 xfailed  (2회)
frontend  vitest                 295 (26 files)
frontend  tsc                    통과
frontend  lint                   기존 경고 1건 (이번 변경 아님)
frontend  build                  통과
E2E       playwright             188 × 2 연속 (retries=0)
MCP       실접속 스모크           initialize / tools/list / tools/call 전부 통과
migration 신규 없음 (M046 유지)
```
