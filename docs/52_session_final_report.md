# 52. 세션 최종 보고 — production mock 제거

> 시작 커밋 `3f8d67f` → 종료 커밋은 아래 §1
> 상세 판단 근거는 `51_production_mock_removal_worklog.md`

---

## 0. 이 세션이 한 일 한 줄 요약

**화면이 실제 데이터가 아닌 것을 실제처럼 보여주던 자리를 찾아 없앴다.**
그 과정에서 아무도 모르던 기존 결함 6건이 드러났고, 그중 하나는
사용자가 매일 보던 화면의 버그였다(알림 배지가 항상 0).

---

## 1. Commits

| 커밋 | 내용 |
|---|---|
| `57360ce` | 대시보드·수익배분 production mock 제거 (Phase 45, 31 잔여) |
| `3b5db8d` | 시스템 상태 화면 + IN_APP 알림 실제 구현 (Phase 52, 39) |
| `fc14ecc` | production mock 감사 — 연동·구성원·프로필 화면 정직화 (Phase 61~62) |

remote push 하지 않았다.

---

## 2. Migrations

| revision | 목적 | up/down/up |
|---|---|---|
| `M031` | `enum_notification_kind` +9종 · `notification.dedupe_key` · 부분 UNIQUE | ✅ |

head = `M031`.

**enum 라벨은 downgrade 에서 되돌리지 않는다.** PostgreSQL 이 enum 값 제거를
지원하지 않아 타입을 새로 만들고 모든 컬럼을 옮겨야 하는데, 그 과정이
데이터 손실 경로가 된다. 남은 라벨은 무해하다 — 쓰지 않으면 그만이다.

성능 인덱스는 추가하지 않았다. `ix_notification_unread`(M001)가 이미 같은
접근 경로를 덮는다 — 근거 없이 겹치는 인덱스는 쓰기 비용만 늘린다.

---

## 3. 완료한 Phase

| Phase | 내용 | 상태 |
|---|---|---|
| 1 | 대시보드 완전 live 전환 | ✅ |
| 2 | 수익배분 화면 live 전환 | ✅ |
| 3 | worker 운영자 화면 (`/operations/system`) | ✅ |
| 4 | IN_APP 알림 실제 구현 | ✅ |
| 12 | production mock 감사 | ⚠️ 부분 (아래 §8) |
| 13 | settings mock 감사 | ⚠️ 부분 (아래 §8) |

**착수하지 않은 Phase**: 5(sales integration account), 6~7(cashflow),
8~9(data quality·audit coverage), 10~11(6역할 E2E·contract 확장),
14~16(성능·export·system status 확장), 17~18(CI gate·critical path E2E).

세션 시간 안에 들어가지 않았다. 남은 작업은 §9 에 우선순위로 정리했다.

---

## 4. 발견한 기존 결함 6건

세 건(D6·D9·D11)은 **같은 뿌리**다: 프론트엔드 API 클라이언트가 존재하지 않는
경로나 필드 이름을 쓰는데 tsc·lint·build 가 전부 통과한다. 경로는 문자열이고,
응답 타입은 각 화면이 로컬 인터페이스로 따로 선언했기 때문이다.

### D6 — `policies.ts` 가 존재하지 않는 경로 호출

`/policies/marketplace-fees` 를 부르는데 라우터는 `/policies/{kind}` 다.
**이 클라이언트를 쓰는 화면이 하나도 없어서** 아무도 몰랐다. 죽은 코드였다.

### D7 — API wrapper 가 `data:null` 에서 envelope 을 통째로 반환 ⚠️ 화면이 죽었다

`body?.data ?? body` 는 null 에서 오른쪽으로 넘어간다. `GET /distributions/{ym}`
가 `{"data":null}` 을 주면 호출자가 `{data:null}` 을 받고 "값이 있다" 로 읽었다.
배분 화면이 `Unhandled Runtime Error` 로 죽었다. **키 존재 여부로 판단**하도록 고쳤다.

### D8 — `policy_crud` 주석이 구현과 정반대

주석: "생성 = DRAFT(비활성), 승인해야 활성". 구현: `create` 가 `is_active=True`,
`approve` 는 `reliability_status` 만 바꾼다.

그 주석을 믿고 화면에 "승인해야 적용됩니다" 라고 쓰면 거짓말이 된다 —
**저장하는 순간 계산이 바뀌고, 비율 합이 100% 가 아니면 그 즉시 월 마감이 막힌다.**
주석·화면 안내를 사실로 고치고 현재 동작을 E2E 로 고정했다.
승인 게이트가 필요한지는 **별도 결정 사항**으로 남긴다.

### D9 — 헤더 알림 배지가 항상 0 ⚠️ 사용자에게 보이던 결함

화면이 `unread_count` 를 읽는데 서버는 `unread` 를 준다. `undefined` 라 `?? 0` 으로
떨어졌다. 알림이 아무리 쌓여도 숫자가 뜨지 않았다.
`is_read`/`notification_type`/`action_url`/`page` 도 전부 서버와 이름이 달랐다.

### D10 — worker 실행 이력에 조직 필터 없음

`worker_health` 는 범위를 걸고 있었는데 `list_runs` 만 빠져 있었다.
한 조직의 관리자가 다른 조직의 실행 기록(오류 메시지 포함)을 볼 수 있었다.

### D11 — `admin.ts` 도 존재하지 않는 경로 호출

`PATCH /admin/users/{id}/role`, `/deactivate`, `/activate` — 전부 없다.
실제는 `PATCH /admin/users/{id}` 와 `DELETE /admin/users/{id}` 다.

**조치**: 세 클라이언트(policies·notifications·admin)를 전부 계약 테스트로 고정했다.
경로·필드 이름이 바뀌면 테스트가 먼저 깨진다.

---

## 5. 제거한 production mock

| 무엇 | 어떻게 거짓말했나 |
|---|---|
| `lib/adapters/dashboard.ts` (파일 삭제) | 매출·순이익·워터폴을 의사난수로 생성. 워터폴에 스키마에 없는 "예상 세금 3%". 경고에 **연동한 적 없는 "11번가 인증 실패(401)"** 하드코딩 |
| `distributionAdapter` | 배분 규칙 CRUD 가 메모리 배열. mock 이 비면 `11,616,000원` 반환 |
| `integrationAdapter` | **외부 API 성공 mock.** `sync()` 가 `상품 42건`/`주문 118건` 반환. 자격증명도 커넥터도 없는데 |
| `notificationAdapter` | 목록·읽음·규칙이 메모리. `connectChannel()`/`testChannel()` 이 "연결됨 · 테스트 성공" 반환 — 아무 데도 안 보내면서 |
| 구성원 화면 | **존재하지 않는 역할 체계**(OWNER/PRODUCT_MANAGER/…). 권한을 바꿔도 실제 권한은 그대로 |
| 프로필 화면 | `app_user` 에 없는 7개 필드를 편집시킴. "나" 를 mock 목록 첫 사람으로 |
| 판매 주문 상태 | **모든 주문에 `"DELIVERED"`** — 반품된 것까지 전부 "배송완료" |
| 담당자 필터 (상품·판매) | 필터는 있는데 서버로 안 보내고 필드는 항상 null |
| `expenseAdapter` | 죽은 코드 (이미 실제 API 로 이관됨) |

---

## 6. 지금 정직해진 것

### 모르는 값과 0을 구분한다
정산 미연결 KPI 는 `value=null` + `NOT_CONNECTED` 다. "0원" 이 아니다.

### 숫자마다 출처가 붙는다
```
period.source / kpi.source / kpi.previous_source / trend[].source
LIVE                    진행 중 집계
MONTH_CLOSE_SNAPSHOT    확정 시점 박제
```
출처가 다르면 `comparable=false` 로 내려가고 화면이 증감을 그리지 않는다.

### 순이익이 아닌 것을 순이익이라 부르지 않는다
판매처별은 **공헌이익**이다. 사업 공통비는 판매처에 귀속되지 않는다.

### "확인한 적 없음" 과 "문제 없음" 을 구분한다
`warnings.never_scanned`, worker `NEVER_RAN`.

### 지원하지 않는 것을 지원하는 척하지 않는다
배분 cap·최소보장·성과급 구간, 프로필 7개 필드, 주문 상태, 알림 규칙 편집 —
전부 입력란을 없애고 "미지원" 으로 명시했다.

### 화면이 임계값을 만들지 않는다
worker 상태 판정은 서버(`derive_state`)가 한다. 시간 임계값은
`STALE_RUN_MINUTES` 하나뿐이고 서버가 그 값도 응답에 실어 준다.

### 권한 표가 코드와 일치한다
구성원 화면의 권한 매트릭스는 라우터에서 옮긴 값이고,
백엔드 테스트 21건이 실제 `require_roles` 와 대조한다.

---

## 7. 테스트

| 항목 | 세션 시작 | 세션 종료 | 신규 |
|---|---|---|---|
| backend pytest | 456 passed / 1 xfailed | **516 passed / 1 xfailed** | 60 |
| Vitest | 145 passed | **181 passed** | 36 |
| Playwright (실제 FastAPI + PostgreSQL) | 73 passed | **116 passed** | 43 |
| tsc / lint:all / production build | 통과 | 통과 | — |
| migration up/down/up | — | M031 확인 | — |

E2E 는 화면 문구가 아니라 **DB 실제 값과 대조**한다. 예를 들어 대시보드 KPI 는
seed 판매 2건(40만원)과 정확히 맞는지 보고, 판매처 분해 합계가 총매출과
어긋나지 않는지 본다.

### flaky 에 대해

중간에 실패한 것들은 전부 원인을 규명했다.

| 실패 | 원인 | 조치 |
|---|---|---|
| channel-price, expense-screen 타임아웃 | **내가 pytest·vitest 를 E2E 와 동시에 돌려서** 생긴 자원 경합 | 단독 실행 시 4.0초에 통과. 이후 E2E 를 단독으로만 돌렸다 |
| dashboard 판매처 분해 | seed 가 `notification`/`worker_run` 을 안 지워 앞 테스트 상태가 남음 | seed 에서 비우도록 수정. **실행 순서에 결과가 좌우되던 문제** |
| distribution 규칙 | 정책 쓰기 권한이 ADMIN/OPERATOR 인데 ACCOUNTING 으로 호출 | 동작이 맞다. 테스트를 고치고 권한 경계를 단언으로 추가 |

retry 로 숨기지 않았다(`retries: 0` in local).

---

## 8. 남은 production mock

| 위치 | 성격 | 위험도 |
|---|---|---|
| `settingsAdapter.taxRules/feePolicies/categories` + 저장 | 정책 3화면(세금·수수료·비용 카테고리)이 메모리 배열 | **중간 — 저장 성공 mock.** `/policies/{kind}` 로 옮겨야 한다 |
| `settlementAdapter` | 쇼핑몰 정산 목록·생성 | 낮음 — `BLOCKED_EXTERNAL_DATA` 도메인. 다만 `create()` 는 저장 성공 mock |
| `productAdapter`/`salesAdapter` fallback | 개발 플래그가 켜진 개발 환경에서만 | 낮음 — production 은 `NODE_ENV` 로 차단 |
| `MOCK_CATEGORIES` | 카테고리 문자열 상수 | 낮음 — 표시용. 실제 카테고리 체계와 무관 |

**production financial mock 0개** 목표는 아직 달성하지 못했다 —
수수료·세금 정책 화면이 남아 있다. 이것이 다음 세션 1순위다.

---

## 9. 다음 세션 우선순위

1. **설정 3화면을 `/policies/{kind}` 로 배선** (Phase 13 잔여)
   — 남은 저장 성공 mock 중 유일하게 금액에 영향을 준다
2. **6역할 권한 E2E** (Phase 10) — 권한 매트릭스는 정적 검사만 있고
   실제 API 403 을 역할별로 확인하지 않았다
3. **현금흐름 ledger** (Phase 6~7) — 손익과 현금흐름 분리
4. **`sales_order.integration_account_id`** (Phase 5) — 복수 스토어 구분
5. **CI gate** (Phase 17) — 지금은 수동 실행이다
6. **성능/index EXPLAIN** (Phase 14)

---

## 10. External blocker (변동 없음)

```text
BLOCKED_EXTERNAL_DATA: 실제 쇼핑몰 정산 파일 없음
BLOCKED_CREDENTIAL:    쿠팡/네이버 API, Slack/Discord webhook, S3, 악성코드 스캐너 없음
```

이번 세션은 이 blocker 들 때문에 멈추지 않았다.
대신 **막혀 있다는 사실을 화면이 정직하게 말하도록** 바꿨다 —
성공 mock 을 만드는 대신 "미연결" 을 표시한다.

---

## 11. 최종 도메인 상태표

| Domain | Backend | Frontend | Test | Production mock | External dep | Status |
|---|---|---|---|---|---|---|
| **dashboard** | **live** | **live** | **✓** | **0** | — | **LIVE** |
| products | live | live | ✓ | 0 | — | LIVE |
| options | live | live | ✓ | 0 | — | LIVE |
| channel listing | live | live | ✓ | 0 | — | LIVE |
| channel price | live | live | ✓ | 0 | connector | PARTIAL |
| approvals | live | live | ✓ | 0 | — | LIVE |
| sales | live | live | ✓ | 주문상태 미지원 표기 | — | LIVE |
| expenses | live | live | ✓ | 0 | — | LIVE |
| attachments | live | live | ✓ | 0 | 스캐너·S3 | PARTIAL |
| partners | 조회만 | live | ✓ | 0 | — | PARTIAL |
| settlement (대사) | 스키마만 | 목록 mock | ✗ | 저장 mock 1 | **정산 파일** | **BLOCKED_EXTERNAL_DATA** |
| monthly close | live | live | ✓ | 0 | — | LIVE |
| snapshots | live | live | ✓ | 0 | — | LIVE |
| **distribution** | **live** | **live** | **✓** | **0** | — | **LIVE** |
| payout | live | live | ✓ | 0 | — | LIVE |
| issues | live | live | ✓ | 0 | — | LIVE |
| **workers** | **live** | **live** | **✓** | **0** | 외부 스케줄러 | **LIVE** |
| **notifications (IN_APP)** | **live** | **live** | **✓** | **0** | — | **LIVE** |
| notifications (SLACK/외부) | 어댑터 인터페이스 | 미연결 표시 | ✓ | 0 | **webhook** | **BLOCKED_CREDENTIAL** |
| integrations (봇) | live | live | 일부 | 0 | — | PARTIAL |
| integrations (마켓 API) | 레지스트리만 | 미연결 표시 | ✓ | 0 | **credential** | **BLOCKED_CREDENTIAL** |
| audit | live | live | ✓ | 0 | — | LIVE |
| **settings (구성원·권한)** | **live** | **live** | **✓** | **0** | — | **LIVE** |
| **settings (프로필·알림)** | **live** | **live** | **✓** | **0** | — | **LIVE** |
| settings (세금·수수료·카테고리) | live (`/policies`) | **mock** | ✗ | **저장 mock 3** | — | **MOCK_DEV_ONLY** |
| cashflow | ✗ | ✗ | ✗ | — | — | **UNIMPLEMENTED** |

> "화면이 존재한다" 와 "기능이 live 다" 를 계속 구분했다.
> `settings (세금·수수료·카테고리)` 는 **백엔드가 live 인데 화면만 mock 이다** —
> 대시보드·배분과 같은 상황이었고, 같은 방식으로 고치면 된다.
