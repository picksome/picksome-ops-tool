# 51. Production mock 제거 작업 기록

> 시작 커밋 `3f8d67f` · 목표: 외부 데이터·credential 없이 완료 가능한
> production mock 과 미배선 운영 기능을 제거하고, OPS 콘솔을 실제 운영 데이터만
> 사용하는 상태로 만든다.
>
> 유지되는 blocker: `BLOCKED_EXTERNAL_DATA` — 실제 쇼핑몰 정산 파일이 없어
> settlement parser · monetary mapping · exact matching 은 착수하지 않는다.
> **그 blocker 때문에 독립 작업을 멈추지 않는다.**

---

## Phase 1 — 대시보드 완전 live 전환

### 제거한 것

`frontend/lib/adapters/dashboard.ts` **삭제**.

이 파일이 무엇이었는지 정확히 기록한다. 재발 방지의 근거다.

| 무엇 | 어떻게 만들어졌나 |
|---|---|
| 일별 매출 | `MOCK_PRODUCTS` × 결정론적 의사난수 × 주말 계수 0.78 |
| 순이익 | 매출 × `p.marginRate ?? 0.12` |
| 워터폴 | 매출에 **고정 비율**을 곱해 만든 7개 항목 (수수료 9.8%, 배송 4.2%, 광고 5.5%, 원가 43%, 반품 2.1%, **세금 3%**) |
| 판매처별 | 전체 매출 × 상품수 비율 × `0.85 + seed × 0.4` |
| 상품 분포 | 판매가 × `(5 + seed × 40)` |
| 경고 | `MOCK_PRODUCTS` 필터 + **하드코딩된 "11번가 인증 실패(401)" 1건** |

마지막 항목이 특히 나빴다. 연동한 적도 없는 판매처의 인증 실패를 매번 띄웠다.

세금 3%도 마찬가지다. **그런 컬럼은 스키마에 없다.** 워터폴에서 제거했다.

### 대신 만든 것

`backend/app/services/dashboard.py` + `GET /api/v1/reports/dashboard`.

핵심 설계는 **산식을 한 벌만 유지**하는 것이다.

```
OPEN / REVIEW       → close_snapshot.compute_figures()   source = LIVE
CONFIRMED / LOCKED  → monthly_close_snapshot 행           source = MONTH_CLOSE_SNAPSHOT
```

snapshot 은 `compute_figures` 의 결과를 확정 시점에 박제한 것이므로 **컬럼 이름과
산식이 자동으로 일치한다.** 대시보드가 자기 산식을 따로 갖지 않는다.

### 왜 월 단위인가

임의 날짜 구간(6/15~7/15)을 받으면 확정월의 박제 숫자와 진행월 집계를 한 값에
섞게 되고, **그 값의 source 를 말할 수 없다.** 마감 상태와 snapshot 이 월 단위로
존재하므로 대시보드도 월 단위다.

### 섞지 않기 위해 붙인 것

| 필드 | 목적 |
|---|---|
| `period.source` | 이 달의 숫자가 어디서 왔는가 |
| `kpi.source` / `kpi.previous_source` | KPI 마다 따로 |
| `kpi.comparable` | 두 기간의 source 가 다르면 `false` — 화면이 증감을 그리지 않는다 |
| `kpi.availability` | `NOT_CONNECTED` 면 `value=null`. **0원이 아니라 모르는 값** |
| `trend[].source` | 트렌드 각 점이 자기 출처를 갖는다 (확정 구간은 진하게 그린다) |
| `warnings.never_scanned` | "이슈 0건" 과 "확인한 적 없음" 을 구분 |

### 정직해진 표현

- 판매처별은 **순이익이 아니라 공헌이익**이다. 사업 공통비는 판매처에 귀속되지
  않는다. 임의 배분해서 "쿠팡 순이익" 이라고 부르면 배분 규칙이 곧 결론이 된다
- `unpriced_order_count` 를 함께 준다 — 마진 계산이 안 된 주문이 있으면
  공헌이익이 과대평가된 정도를 알 수 있다
- 순이익 라벨이 상태에 따라 바뀐다:
  `잠정 순이익 (미확정)` → `검토 확정 순이익 (정산 미연결)` → `확정 순이익`

### 경고

대시보드 전용 경고 로직을 **없앴다**. `operation_issue.summary()` 를 그대로 쓴다.
따로 두면 이슈 센터에서 처리·무시한 건이 대시보드에는 계속 떠 있게 된다.
유형 배지를 누르면 `/operations/issues?issue_type=...` 로 필터되어 들어간다
(이슈 화면에 `issue_type` 쿼리 지원을 추가했다).

### 테스트

| 계층 | 건수 | 무엇을 고정하나 |
|---|---|---|
| pytest `test_dashboard.py` | 13 | source 구분, 확정 후 불변, null≠0, 합계 정합, 조직 격리 |
| Vitest `dashboard.test.ts` | 7 | 경로 계약, 응답 무변형, null 유지 |
| Playwright `dashboard.spec.ts` | 9 | **DB 실제 값과 대조**, 화면 렌더, READONLY 접근 |

E2E 는 seed 판매 2건(2026-05, 40만원)을 심고 KPI·판매처 분해·워터폴이
그 값과 정확히 맞는지 본다. 화면 문구만 보지 않는다.

---

## Phase 2 — 수익배분 화면 live 전환

### 제거한 것

`distributionAdapter` (in `lib/adapters/index.ts`).

| 무엇 | 문제 |
|---|---|
| `saveRule` / `removeRule` | **메모리 배열.** 토스트는 뜨고 DB 는 그대로. 새로고침하면 사라짐 |
| `monthlyBase` | mock 이 비면 `11,616,000` / `2,136,700` 같은 **지어낸 금액** 반환 |
| 화면 입력 | cap · 최소보장 · 기준금액 초과 · 단계별 성과급 · 참여자 유형 — **DB 에 없는 개념** |

### 대신 쓰는 것 (전부 기존 live API)

```
확정 배분   GET/POST /distributions/{ym}          distribution_run / distribution_result
이력        GET      /distributions/{ym}/history
미리보기    GET      /distributions/{ym}/preview   저장하지 않음
지급 연결   GET      /payouts?year_month=
규칙        GET/POST /policies/partner_share      버전 + effective_from/to + 승인
우선분배    GET      /policies/settlement          priority_distribution_json
```

백엔드는 이미 전부 live 였다. **화면만 mock 을 보고 있었다.**

### 지원하지 않는 것을 지원하지 않는다고 말한다

입력 칸을 없애고 화면에 명시했다:

> 최대 지급 한도(cap) · 최소 보장액 · 기준금액 초과분 비율 · 단계별 성과급(tier) ·
> 투자 원금 우선 회수 · 참여자 유형별 규칙 — **미지원**

저장되지 않는 값을 입력받는 것보다, 못 한다고 말하는 편이 낫다.

### 발견한 기존 결함 — D6

**`lib/api/policies.ts` 가 존재하지 않는 경로를 부르고 있었다.**

| | |
|---|---|
| 재현 | `/policies/marketplace-fees`, `/policies/tax` 호출 |
| 실제 | 라우터는 `/policies/{kind}`, kind 는 snake_case (`marketplace_fee`) |
| 왜 안 터졌나 | **이 클라이언트를 쓰는 화면이 하나도 없었다.** 죽은 코드였다 |
| 응답 모양도 틀림 | `{items,total,page,page_size}` 로 기대 — 실제는 envelope `{data,meta}` |
| 조치 | 실제 라우터에 맞춰 재작성 + 11개 kind 전부 계약 테스트로 고정 |

D2(월정산 404)와 같은 종류다 — **경로는 문자열이라 tsc·lint·build 가 잡지 못한다.**

### 확인한 사실 — 비율 invariant 는 이미 두 겹이었다

테스트를 쓰다가 알게 된 것(추측이 아니라 실행 결과):

1. `close_checks.DISTRIBUTION_RATIO_INVALID` 는 **BLOCKING** 이다.
   비율 합이 100% 가 아니면 **월 확정 자체가 거부된다.** 확인 처리로 못 넘긴다
2. `distribution._compute` 도 별도로 `ConflictError` 를 던진다

프론트 검증만 믿지 않는다는 요구는 이미 충족돼 있었다. 테스트로 고정했다.

### 테스트

| 계층 | 신규 | 무엇을 고정하나 |
|---|---|---|
| pytest | 3 | 미리보기 무부작용(행 개수 직접 셈), 비율 invariant 2겹, 규칙 변경 시 과거 불변 |
| Vitest `policies.test.ts` | 5 | 경로·kind·승인/폐기 분리 |
| Playwright `distribution.spec.ts` | 9 | 확정→승인→지급 배치, 미리보기 무부작용, DRAFT≠활성, OPERATOR 지급 403 |

---

## E2E seed 변경

`backend/scripts/e2e_db.py`

| 추가 | 이유 |
|---|---|
| 판매 2건 (2026-05, 쿠팡 30만 + 스마트스토어 10만) + 실마진 | 대시보드가 0이 아닌 실제 숫자를 집계하는지 보려면 데이터가 필요하다. 판매처를 둘로 나눈 이유는 **분해 합계가 총매출과 맞는지** 보려면 하나로는 부족해서다 |
| 2026-05 `monthly_settlement` (OPEN) | 확정(`confirm`)은 정산 행을 요구한다 |
| `partner_share_policy` 100% | 없으면 배분 결과가 비어 E2E 가 무의미해진다. **합이 1.0 이 아니면 어떤 달도 확정할 수 없다** |
| `DELETE FROM worker_run` | 남겨 두면 "한 번도 스캔한 적 없다" 검증이 앞 테스트의 기록을 보고 실패한다 — 실행 순서에 결과가 좌우된다 |

**2026-05 를 쓴 이유**: 마감 흐름 E2E 가 쓰는 2026-06 에 심으면 그 테스트가 확정하는
숫자가 달라지고, 두 테스트가 실행 순서에 따라 서로를 깨뜨린다.

---

## Phase 3 — 시스템 상태 화면 (`/operations/system`)

### 무엇이 없었나

`/operations/workers` API 는 있었고 화면이 없었다. **worker 가 멈춘 것을 관리자가
알아차릴 방법이 없었다** — 승인이 자동 적용되지 않거나 증빙 삭제가 밀려도
DB 를 직접 봐야 알았다.

### 화면이 임계값을 만들지 않는다

이게 이 화면의 설계 원칙이다. "3시간 넘으면 빨간색" 같은 규칙을 프론트엔드가 정하면
그 숫자의 근거를 아무도 모르고, 화면마다 달라진다.

상태 판단을 서버로 옮겼다 (`worker_runtime.derive_state`):

| 상태 | 근거 |
|---|---|
| `NEVER_RAN` | 실행 기록 자체가 없음 |
| `RUNNING` | RUNNING 기록 있음 |
| `STALE_RUNNING` | RUNNING 인데 `STALE_RUN_MINUTES`(60분) 초과 |
| `LAST_RUN_FAILED` | 마지막 시작이 실패 (성공 기록보다 나중) |
| `PENDING_QUEUE` | 성공했지만 처리할 대기 건이 남음 |
| `IDLE` | 마지막 실행 성공 + 대기 없음 |

시간 임계값은 `STALE_RUN_MINUTES` 하나뿐이고, 서버가 그 값도 응답에 실어 준다.

`has_queue` 도 같이 준다 — 이슈 스캔은 큐 개념이 없어서 "대기 0건" 이
좋은 소식도 나쁜 소식도 아니다. 화면이 그걸 구분할 수 있어야 한다.

### 수동 실행은 CLI 와 같은 코드를 쓴다

`POST /operations/workers/{name}/run` (ADMIN 전용)이
`app.scripts.run_workers.WORKER_ADAPTERS` 를 그대로 가져다 쓴다.
화면용 구현을 따로 만들면 **cron 이 하는 일과 관리자가 누르는 버튼이 갈라지고,
그 차이는 사고가 난 뒤에야 드러난다.** 테스트로 두 표가 같은지 고정했다.

이 조직 범위로만 돈다 — 전체 조직 실행은 cron 의 일이다.

### 발견한 기존 결함 — D10

**`list_runs` 에 조직 필터가 없었다.**

`worker_health` 는 `org_id = :org OR org_id IS NULL` 로 범위를 걸고 있었는데
실행 이력만 빠져 있었다. 한 조직의 관리자가 다른 조직의 실행 기록을
(오류 메시지 포함) 볼 수 있었다. 같은 규칙으로 맞추고 회귀 테스트를 넣었다.

### 보안

응답에 stack trace·SQL·저장 경로·비밀값이 없다는 것을 E2E 가 **문자열로 직접 확인**한다
(`Traceback`, `SELECT `, `site-packages`, DB 비밀번호). 서버가 애초에
`short_error()` 로 첫 줄만 저장한다.

---

## Phase 4 — IN_APP 알림 실제 구현

### 무엇이 문제였나

알림 인프라는 D-281 에서 만들어졌는데 **아무 도메인 서비스도 부르지 않았다.**
승인이 거절돼도, 월 마감이 막혀도, worker 가 죽어도 알림이 생기지 않았다.
화면은 `MOCK_NOTIFICATIONS` 를 보고 있었으므로 그럴듯해 보였을 뿐이다.

### 발견한 기존 결함 — D9 (사용자에게 보이던 결함)

**헤더 벨의 미읽음 배지가 항상 0이었다.**

```
화면이 읽던 것        서버가 주는 것
unread_count          unread
is_read               read_at
notification_type     kind
action_url            target_url
page / page_size      limit / offset
```

`unread_count` 가 `undefined` 라 `?? 0` 으로 떨어졌다. 알림이 아무리 쌓여도
숫자가 뜨지 않았다. name vs name_ko, stock vs stock_quantity 와 같은 종류다 —
타입도 린트도 빌드도 잡지 못한다(로컬 인터페이스로 각자 선언했으므로).
필드 이름을 계약 테스트 10건으로 고정했다.

### 새 migration — M031

| 항목 | 이유 |
|---|---|
| `enum_notification_kind` +9종 | 기존 7종은 "정산 마감일" 같은 일정 알림이라 운영 사건과 결이 다르다 |
| `notification.dedupe_key` | 같은 사건 반복 생성 차단 |
| `uq_notification_dedupe` (부분 UNIQUE) | 애플리케이션 검사만으로는 동시 실행에서 뚫린다 |

**enum 라벨은 downgrade 에서 되돌리지 않는다.** PostgreSQL 이 enum 값 제거를
지원하지 않아 타입을 새로 만들고 모든 컬럼을 옮겨야 하는데, 그 과정이
데이터 손실 경로가 된다. 남은 라벨은 무해하다.

성능 인덱스는 추가하지 않았다 — `ix_notification_unread`(M001)가 이미
같은 접근 경로를 덮는다. 근거 없이 겹치는 인덱스는 쓰기 비용만 늘린다.

### 세 가지 규칙

**① 알림이 업무를 막지 않는다.** `emit()` 은 예외를 밖으로 내보내지 않는다.
`_insert` 는 `SAVEPOINT` 안에서 돈다 — 바깥 트랜잭션(승인 적용 등)이 진행 중일 때
여기서 오류가 나면 그 트랜잭션 전체가 무효화되기 때문이다. 알림 때문에 승인이
취소되면 안 된다. **다만 조용히 성공한 척하지도 않는다** — `EmitResult` 에
`created/deduped/failed` 가 사실대로 담긴다.

**② 같은 사건으로 반복 생성하지 않는다.** worker 가 5분마다 돌면서 "월 마감 차단" 을
매번 만들면 하루 288건이다. 사용자는 그 시점부터 벨을 보지 않는다.
`close:{ym}:blocked:{차단항목조합}` 처럼 **사유가 바뀌면 새 알림**이 되도록 키를 만든다.

**③ 사건마다 받는 사람이 다르다.** 반려는 **요청자**가 받는다 — 승인자에게 보내면
자기가 방금 누른 버튼을 다시 알려주는 꼴이다. worker 실패는 관리자다.

### 연결한 사건

| 사건 | 어디서 | 수신자 |
|---|---|---|
| `APPROVAL_REQUESTED` | `create_request(submit=True)` + `submit()` | 승인 권한자 |
| `APPROVAL_REJECTED` | `reject()` | **요청자** |
| `APPROVAL_APPLY_FAILED` | `_record_failure` (PERMANENT_FAILURE 만) | 관리자 + 요청자 |
| `MONTH_CLOSE_BLOCKED` / `_READY` | `run_validation` (OPEN·REVIEW 월만) | ADMIN·ACCOUNTING |
| `WORKER_FAILURE` | `worker_runtime.run_worker` | 관리자 |

`create_request(submit=True)` 는 **`submit()` 을 거치지 않고** 바로 PENDING 으로
들어간다(E2E 가 이걸 잡았다). 두 경로 모두에서 보내되 dedupe 키가 같아 중복되지 않는다.

`APPROVAL_APPLY_FAILED` 는 `RETRYING` 에는 보내지 않는다 — 다음 배치가 다시 시도한다.
매 시도마다 알리면 하나의 문제가 알림함을 채운다. `HELD_BY_LOCK` 도 실패가 아니라
의도된 보류다.

### Slack / Discord — 성공 mock 제거

이전 `notificationAdapter.connectChannel()` 은 **메모리 플래그를 켜고 "연결됨" 을
표시했고**, `testChannel()` 은 그 플래그만 보고 `SUCCESS` 를 돌려줬다.
아무 데도 아무것도 보내지 않았다.

알림에서 가장 나쁜 종류의 거짓말이다 — 사용자는 장애 알림을 받고 있다고 믿게 되고,
정작 장애가 났을 때 아무 연락도 못 받는다.

지금은 서버가 알려주는 **지원 여부**만 보여주고, 자격증명이 없는 채널에는
연결 버튼도 테스트 버튼도 만들지 않는다. `BLOCKED_CREDENTIAL` 을 유지한다.

### 알림 규칙 편집 — 미지원으로 명시

규칙 테이블이 없다. 어떤 사건이 알림을 만드는지는 코드에 있고
(`services/notification_events.py`) 화면에서 조건을 바꿀 수 없다.
mock 규칙 목록을 보여주던 자리에 그 사실을 적었다.

대신 **본인 수신 설정**은 실제 API 다 — `/admin/notification-prefs` 로
채널·종류별 on/off 를 저장한다(경로가 `/admin/` 이지만 서버가 항상 본인 것만 다룬다).

### E2E seed 변경

`notification` 을 매 reseed 마다 비운다. 남겨 두면 "이 사건으로 알림이 생겼는가" 를
확인하는 테스트가 **앞 테스트가 만든 알림**을 보고 통과한다 —
실행 순서에 결과가 좌우되는 테스트가 된다(`worker_run` 과 같은 이유).
실제로 이 문제 때문에 4건이 실패했고, 그래서 발견했다.

---

## Phase 12~13 — Production mock 감사 · 설정 화면 감사

### 제거한 production mock

| 위치 | 무엇이었나 | 왜 위험했나 |
|---|---|---|
| `integrationAdapter` | `connect()` 가 메모리 플래그를 켜고 `status:"OK"`, `sync()` 가 `productCount:42`/`orderCount:118` 반환 | **외부 API 성공 mock.** 쿠팡·네이버 자격증명도 커넥터도 없는데 "연결됨 · 상품 42건 동기화" 를 보여줬다. 사용자는 주문이 자동으로 들어온다고 믿는다 |
| `expenseAdapter` | 목록·저장·삭제·첨부가 전부 메모리, 첨부는 `objectURL` | 이미 죽은 코드였다(M026/M027 에서 실제 API 로 이관). 남겨 두면 다음 사람이 되살린다 |
| 구성원 화면 전체 | mock 배열 + **존재하지 않는 역할 체계** | 권한 화면이 거짓말을 하면 "이 사람은 못 본다" 는 판단 자체가 무너진다 |
| 프로필 화면 | `app_user` 에 없는 7개 필드를 편집시킴 + "나" 를 mock 목록 첫 사람으로 잡음 | 저장 성공 토스트가 뜨는데 아무 데도 안 남는다 |
| 판매 주문 상태 | 어댑터가 **모든 주문에 `"DELIVERED"` 를 넣었다** | 반품된 주문도 배송 중인 주문도 전부 "배송완료" 로 보였다. `sales_order` 에 상태 컬럼이 없다 |
| 상품·판매 담당자 필터 | 필터·컬럼은 있는데 서버로 값을 보내지 않고 필드는 항상 null | 필터를 걸어도 아무 일이 없다. 사용자는 "담당자별로 보고 있다" 고 믿는다 |
| `MOCK_MARKETPLACES` 라벨 | 판매처 라벨을 고정 상수에서 조회 | 조직이 추가한 커스텀 채널이 코드로 보였다. `useSaleChannels`(실제 채널 목록)로 교체 |

### 발견한 기존 결함 — D11

**`lib/api/admin.ts` 도 존재하지 않는 경로를 부르고 있었다.**

```
PATCH /admin/users/{id}/role         ← 없음. 실제는 PATCH /admin/users/{id}
PATCH /admin/users/{id}/deactivate   ← 없음. 실제는 DELETE /admin/users/{id}
PATCH /admin/users/{id}/activate     ← 없음
?page=&page_size=                    ← 서버는 limit/offset 을 읽는다
```

D6(policies)·D2(settlements)와 같은 종류다. 세 번째다 — 그래서 이번에는
**세 클라이언트 전부 계약 테스트로 고정했다.**

### 새 endpoint — `GET /users/directory`

승인 화면은 `requested_by` UUID 만 받는다. 이름을 붙이려면 사용자 목록이 필요한데
`/admin/users` 는 ADMIN 전용이라 OPERATOR 가 승인함을 열면 이름을 못 붙인다.
그 자리를 **mock 구성원 목록**이 채우고 있었다 — 실존하지 않는 이름이 표시됐다.

이름·역할만 내려준다. **이메일·마지막 로그인은 주지 않는다** — 이름을 붙이는 데
필요 없고, ADMIN 전용 화면의 정보를 모든 역할에 열어 줄 이유가 없다.
비활성 사용자도 포함한다: 퇴사자가 작년에 올린 승인 요청의 이름이 사라지면
이력을 읽을 수 없다.

### 권한 매트릭스를 코드에서 뽑았다

구성원 화면의 권한 표는 **라우터에서 그대로 옮긴 값**이다. 그리고 그 표가
실제 `require_roles(...)` 와 일치하는지 백엔드 테스트 21건이 검사한다.

표가 라우터를 정의하지 않는다 — 라우터가 진실이고 표는 그것을 옮긴 것이다.
라우터를 고치면 테스트가 먼저 깨지고, 화면 표를 함께 고치라고 알려준다.

개별 권한 체크박스는 **만들지 않았다.** 조합을 허용하면 "누가 무엇을 볼 수
있는지" 를 아무도 파악하지 못하게 되고, 화면에서 켠 값이 서버에 반영되지 않으면
권한 화면이 다시 거짓말을 시작한다.

### 남은 mock (이번 범위 밖)

| 위치 | 성격 | 위험도 |
|---|---|---|
| `settingsAdapter.taxRules / feePolicies / categories` + 저장 | 정책 3화면이 아직 메모리 배열 | **중간** — 저장 성공 mock이다. 실제 `/policies/{kind}` 로 옮겨야 한다 |
| `settlementAdapter` | 쇼핑몰 정산 목록·생성이 mock | 낮음 — `BLOCKED_EXTERNAL_DATA` 도메인이라 실데이터가 없다. 다만 `create()` 는 저장 성공 mock이다 |
| `productAdapter` / `salesAdapter` fallback | `NEXT_PUBLIC_ENABLE_MOCK_FALLBACK=true` 인 개발 환경에서만 동작 | 낮음 — production 에서는 `NODE_ENV` 로 차단된다 |
| `MOCK_CATEGORIES` | 카테고리 문자열 목록 | 낮음 — 표시용 상수. 다만 실제 카테고리 체계와 무관하다 |
