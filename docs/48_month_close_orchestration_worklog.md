# 48. 월 마감 오케스트레이션 · 확정 snapshot · 지급 장부 — 작업 기록

> 시작 2026-08-08 · 선행: `43_ops_stability_plan.md`, `44_ops_stability_worklog.md`,
> `47_settlement_reconciliation_design.md`
>
> **세션이 중단돼도 이 문서만 읽고 이어갈 수 있어야 한다.**

---

## 0. 이번 세션 진입 상태

| 항목 | 상태 |
|---|---|
| 마지막 커밋 | `808d008` 증빙 파일 실제 저장 + 거래처 선택 (M027) |
| 마이그레이션 head | M027 |
| 백엔드 테스트 | 376 passed, 1 xfailed |
| 브랜치 | `feat/ops-console-redesign` |
| 워킹트리 | clean |

---

## BLOCKED_EXTERNAL_DATA — 정산 대사 (§1~25)

```text
BLOCKED_EXTERNAL_DATA:
실제 쇼핑몰 정산 파일이 없어 판매처별 parser와 최종 monetary mapping을 확정할 수 없음
```

### 근거 (추측이 아니라 확인한 사실)

| 확인 항목 | 결과 |
|---|---|
| 저장소·작업폴더 전체에서 정산서 파일 검색 | 없음 |
| `마진계산기&장부.xlsx` 의 `쿠팡 정산 기록` 시트 | 컬럼이 **날짜 / 정산금 2개뿐**. 입금 메모지, 라인 단위 정산서가 아님 |
| `marketplace_settlement_record` 행 수 | 0 |
| `integration_account` 행 수 | 0 |

`docs/47` §7 이 선행 조건 5개를 정리했고 그중 **3번(실제 정산서 확보)** 이 없으면
컬럼명·주문번호 형식·라인 분리 여부를 전부 추측해야 한다. 추측으로 만든 parser 는
실물이 오면 전부 다시 만들게 된다. 그래서 **parser 와 monetary mapping 은 만들지 않았다.**

### 정산 파일 없이도 한 것

| 한 것 | 왜 가능한가 |
|---|---|
| 마감 검증에서 **정산 미연결을 명시적 WARNING 으로 노출** | 파일 형식과 무관 |
| snapshot 에 `settlement_connected` / `profit_basis` 분리 | 0원과 "모름" 을 구분하는 것은 형식 독립 |
| `UNMATCHED_SETTLEMENT` 등급 수정 (D3) | 이미 확정된 설계 결론의 구현 오류 |

### 재개 지점

실물 정산서가 확보되면 `docs/47` §7 표의 1·2·4·5 를 순서대로 진행한다.
지금 만든 `close_checks._settlement_checks` 가 그 시점에 확장 지점이 된다
(현재는 매칭 상태 컬럼이 없어 UNMATCHED 만 본다는 사실을 코드 주석에 남겨 뒀다).

---

## Phase 26~28 — 월 마감 오케스트레이션 · snapshot · 잠금

### 26-1. 분석 결과 — 중복 상태 기계 2개가 있었다

같은 `monthly_settlement` 행을 두 코드 경로가 서로 모르게 쓰고 있었다.

| | `services/monthly_close.py` (M017) | `services/settlement_crud.py` (M001) |
|---|---|---|
| 라우터 | `POST /api/monthly-close/{ym}/…` | `POST /api/settlement/{id}/…` |
| 쓰는 컬럼 | `close_state` **와** `status` | `status` 만 |
| 마감 검증 | 실행 | **안 함** |
| 이월 원장 | 안 씀 | 씀 (그런데 깨져 있었다 — D4) |

월 잠금 가드(`core/month_lock.py`)는 **`close_state` 를 읽는다.**

→ 이번 작업에서 `settlement_crud` 의 confirm/lock 을 `monthly_close` 로 **위임**시켰다.
상태 기계는 하나다. 라우터는 둘 다 유지한다(하위 호환).

### 26-2. 설계 결정

**결정 1 — 검증 run 은 측정이고, 이슈는 작업이다. 상태를 두 곳에 두지 않는다.**

`monthly_close_check_item` 에 `status`/`assigned_to` 를 두지 않았다.
"14:32 에 미매칭 정산 3건이 보였다" 는 사실은 나중에 사람이 처리했든 아니든 바뀌지 않는다.
누가 언제 처리하는지는 `operation_issue`(Phase 35)가 다루고, 둘은 `dedupe_key` 로 잇는다.
같은 상태를 두 테이블에 두면 반드시 어긋난다.

**결정 2 — snapshot 은 세대당 하나. 잠금은 새 snapshot 을 만들지 않는다.**

확정(CONFIRM)에서 만들고, 잠금(LOCK)에서는 **hash 를 다시 계산해 비교만 한다.**
확정과 잠금 사이에 숫자가 달라졌으면 그건 재확정해야 할 사건이지
조용히 다시 박제할 일이 아니다. 이 비교가 `content_hash` 의 실제 용도다.

예외: M028 이전에 확정된 달은 snapshot 이 없다. 잠금 시점에 소급 생성하고
`taken_at_state='LOCKED'` 로 구분한다.

**결정 3 — 정산 미연결이면 "확정 순이익" 이라고 부르지 않는다.**

```
profit_basis = SALES_AND_EXPENSE_ONLY   판매·비용만으로 계산
             = SETTLEMENT_RECONCILED    정산 전건 매칭 완료
```

DB CHECK 로 `SETTLEMENT_RECONCILED` 는 `settlement_connected = TRUE` 일 때만 가능하게 막았다.
API 는 `profit_label` 로 사람이 읽을 문구까지 내려준다
(`"확정 순이익 (정산 미연결 — 판매·비용 기준)"`).

**결정 4 — snapshot 불변성을 DB 트리거로 강제한다.**

service 에만 두면 다음 사람이 UPDATE 한 줄을 쓴다.

- UPDATE: `superseded_at` / `superseded_by_id` **외 컬럼 변경 거부**
- DELETE: 전면 거부
- 유지보수 탈출구: `SET LOCAL picksome.allow_snapshot_delete = 'on'`
  (테스트 teardown 전용. **애플리케이션 코드에는 이 문자열이 없다** —
  다른 곳에 생기면 확정 숫자 삭제 경로가 열린 것이므로 리뷰에서 막아야 한다)

**결정 5 — 임계값이 없는 항목을 BLOCKING 으로 만들지 않는다.**

적자 판매·미검증 정책·정산 미연결은 전부 WARNING 이다.
"몇 %부터 문제인가" 에 업무 기준이 없는 것을 차단으로 만들면
사용자는 검증 전체를 무시하게 된다.

### 26-3. 마감 검증 카탈로그 — 5개 → 16개

| 영역 | 코드 | 등급 |
|---|---|---|
| 판매 | `SALE_WITHOUT_PRODUCT` | BLOCKING |
| 판매 | `ZERO_COST_SALE` | BLOCKING |
| 판매 | `MARGIN_NOT_CALCULATED` | WARNING |
| 판매 | `LOSS_SALE` | WARNING |
| 비용 | `MISSING_EVIDENCE` | WARNING |
| 비용 | `ATTACHMENT_DELETE_PENDING` | WARNING |
| 비용 | `PERIOD_RECALC_PENDING` | **BLOCKING** |
| 정산 | `SETTLEMENT_NOT_CONNECTED` | WARNING |
| 정산 | `UNMATCHED_SETTLEMENT` | WARNING (D3 에서 변경) |
| 승인 | `APPROVAL_PENDING` | WARNING |
| 승인 | `APPROVAL_APPROVED_NOT_APPLIED` | **BLOCKING** |
| 승인 | `APPROVAL_APPLY_FAILED` | **BLOCKING** |
| 승인 | `APPROVAL_HELD_BY_LOCK` | WARNING |
| 승인 | `APPROVAL_RETRY_PENDING` | WARNING |
| 정책 | `DISTRIBUTION_RATIO_INVALID` | BLOCKING |
| 정책 | `UNVERIFIED_TAX_POLICY` | WARNING |
| 정책 | `EXPIRED_FEE_POLICY` | WARNING |
| 정책 | `UNVERIFIED_CALC_POLICY` | WARNING |
| 이슈 | `OPEN_BLOCKING_ISSUE` | BLOCKING (Phase 35 이후 활성) |

`PERIOD_RECALC_PENDING` 을 BLOCKING 으로 둔 이유: 비용이 바뀌었는데 집계를 다시
돌리지 않은 상태로 확정하면 **낡은 숫자를 박제**한다. 이건 임계값 문제가 아니라
데이터 무결성 문제다.

`APPROVAL_APPROVED_NOT_APPLIED` 를 BLOCKING 으로 둔 부작용: 잠긴 달에
미반영 승인이 남는 상태를 만들려면 **잠금을 먼저** 해야 한다. 기존 테스트 4건의
순서를 그렇게 바꿨다(그게 실제로 `HELD_BY_LOCK` 이 생기는 경로이기도 하다).

### 26-4. 확정 트랜잭션

```
① 검증 실행 + run 기록 + 커밋      ← 거부돼도 남아야 하므로 먼저 커밋
② blocking 0 / 경고 확인 여부 검사
③ 다음이 한 트랜잭션 :
     savepoint
       FOR UPDATE + CAS 로 close_state 전이
       검증 재실행 (blocking 0 재확인)
       snapshot INSERT
       audit log
     savepoint commit → db.commit()
```

`_transition(on_transition=…)` 훅을 추가했다. 상태 변경과 원자적이어야 하는 작업
(snapshot, 이월 원장)을 여기에 건다. 훅에서 예외가 나면 savepoint 가 되돌아가
상태 변경도 함께 사라진다. 테스트로 고정했다
(`test_confirm_is_atomic_with_snapshot`).

### 26-5. 재오픈

- snapshot 을 **지우지 않는다.** `superseded_at` 을 찍어 세대로 남긴다
- `monthly_settlement.close_snapshot_id` 를 NULL 로 — 재확정 전까지 확정 숫자가 없다
- `settlement_reopen_audit` 에 사유·이전 snapshot id·승인 요청 id 기록
- 재확정하면 세대 2가 생기고 세대 1의 숫자는 그대로 남는다
- D-285 한도(기본 3회) 그대로

---

## 발견한 기존 결함 4건

### D1 — 레거시 잠금 경로가 월을 실제로 잠그지 않았다 ⚠️ 심각

**재현**: `POST /api/settlement/{id}/lock` → `status='LOCKED'`, `close_state='OPEN'`
→ 그 달의 판매·비용이 그대로 수정 가능.

**원인**: `settlement_crud.lock_settlement` 이 `status` 만 UPDATE. 월 잠금 가드
(`core/month_lock.get_close_state`)는 `close_state` 를 읽는다. M017 이 새 컬럼을
도입하면서 레거시 경로를 함께 고치지 않았다.

**수정**: `confirm_settlement` / `lock_settlement` 를 `monthly_close` 로 위임.
부수 효과로 레거시 경로도 이제 마감 검증을 거친다.

**회귀 테스트**: `test_legacy_lock_path_actually_locks_the_month`

### D2 — 월정산 프론트엔드가 존재하지 않는 API 를 호출

**재현**: `frontend/lib/api/settlements.ts` 의 모든 호출이 `/settlements/monthly/…`.
백엔드에 `/settlements` 로 시작하는 라우터는 **없다**(`/settlement`, `/monthly-close`).

**상태**: Phase 9(프론트 배선)에서 처리 예정. 이 커밋에는 포함되지 않았다.

### D3 — 미매칭 정산이 마감을 차단했다

**원인**: `docs/47` §4 에서 "경고이지 차단이 아니다" 로 결론냈는데 M017 구현이
`blocking=True` 였다. 판매처 정산서 지연이 마감을 막으면 마감이 판매처 일정에
인질로 잡힌다.

**수정**: WARNING 으로. 확인 없이 넘어갈 수는 없다(ack 필요).

**회귀 테스트**: `test_unmatched_settlement_is_warning_not_blocking`

### D4 — 적자월을 잠그면 무조건 500

**재현**: `SELECT 'ACCRUE'::enum_carryover_event;`
→ `ERROR: invalid input value for enum enum_carryover_event: "ACCRUE"`

`settlement_crud.lock_settlement` 이 `event_kind` 에 `'ACCRUE'`/`'OFFSET'` 을 넣었다.
유효 라벨은 `LOSS_ADDED / OFFSET_APPLIED / MANUAL_ADJUST`.
흑자이면서 이월 잔액이 0인 달만 우연히 통과했다.

**수정**: 이월 로직을 `services/carryover.py` 로 분리. 올바른 라벨 사용 +
재잠금 시 중복 누적 방지(멱등).

**회귀 테스트**: `test_locking_a_loss_month_records_carryover`,
`test_relock_does_not_double_count_carryover`

---

## 수정·추가 파일

### 마이그레이션

| revision | down_revision | 목적 |
|---|---|---|
| M028 | M027 | `monthly_close_check_run` / `_check_item` / `monthly_close_snapshot` + 불변성 트리거 + `monthly_settlement` 3컬럼 |

up / down / up **성공** (dev DB · test DB 양쪽).

### 신규

- `backend/app/services/close_checks.py` — 검증 카탈로그 + run 영속화
- `backend/app/services/close_snapshot.py` — 집계 · hash · 세대 관리 · drift 탐지
- `backend/app/services/carryover.py` — 이월 원장 append (단일 진입점)
- `backend/app/tests/integration/test_monthly_close_snapshot.py` — 신규 17건

### 수정

- `backend/app/services/monthly_close.py` — 검증 위임, `on_transition` 훅,
  확정 원자성, 잠금 drift 검사, 재오픈 시 snapshot supersede
- `backend/app/services/settlement_crud.py` — confirm/lock 위임 (D1·D4)
- `backend/app/api/v1/monthly_close.py` — `check-runs`, `snapshot-preview`,
  `snapshots`, `drift` 엔드포인트 추가
- `backend/app/api/v1/settlement.py` · `domain/settlement_api.py` —
  `acknowledged_warnings` 전달
- `backend/app/tests/fixtures/factories.py` — `confirm_month` / `lock_month` /
  `purge_month_close`
- 기존 테스트 4개 파일 — 헬퍼 사용 + 잠금/승인 순서 조정

---

## 신규 API

| 메서드 | 경로 | 권한 |
|---|---|---|
| GET | `/api/monthly-close/{ym}/check-runs` | 조회 |
| GET | `/api/monthly-close/{ym}/check-runs/{run_id}` | 조회 |
| GET | `/api/monthly-close/{ym}/snapshot-preview` | 작업 |
| GET | `/api/monthly-close/{ym}/snapshots` | 조회 |
| GET | `/api/monthly-close/{ym}/drift` | 조회 |

`GET /api/monthly-close/{ym}` 응답에 `snapshot`, `profit_label`,
`close_generation` 추가. `GET /api/monthly-close` 목록에
`snapshot_net_profit` 추가(확정월과 진행중인 달을 같은 컬럼에 섞지 않기 위해).

---

## 검증 결과

| 항목 | 결과 |
|---|---|
| M028 up/down/up (dev) | 성공 |
| M028 up/down/up (test DB) | 성공 |
| 백엔드 pytest 전체 | **393 passed, 1 xfailed** (신규 17) |
| ruff (신규·수정 모듈) | 통과 (B008 은 프로젝트 전역 기존 패턴 316건) |

---

---

## Phase 29 — 월 재오픈 승인

잠긴 달을 여는 것은 **확정한 숫자를 무효화하는 행위**다. 그 달의 순이익으로
파트너 배분이 이미 계산됐을 수 있고 지급까지 끝났을 수도 있다. 그래서 승인을 거친다.

| 항목 | 내용 |
|---|---|
| target_type | `monthly_close` (target_id = `monthly_settlement.id`) |
| request_type | `MONTH_REOPEN` |
| 승인 권한 | **ADMIN 만.** ACCOUNTING 도 단독으로 결정하지 않는다 |
| 사유 | 5자 이상 필수 |
| 귀속월 | 그 달의 1일. 오늘로 잡으면 승인 목록에서 달이 섞인다 |
| 적용 | `monthly_close.unlock` 위임 — 한도·supersede·감사가 함께 간다 |

### 월 잠금 가드 면제 — 딱 하나만

재오픈이 **바로 그 잠금을 푸는 연산**이라 대상 월이 잠긴 것은 전제 조건이지
거부 사유가 아니다. 가드를 그대로 두면 이 요청은 영원히 `HELD_BY_LOCK` 이 된다.

면제를 안전하게 만든 것:

- `month_lock_exempt` 는 **어댑터 클래스 속성**이다. 요청 payload 로 설정할 수 없다
- 이 어댑터가 바꿀 수 있는 것은 재오픈 하나뿐. 데이터를 건드리는 경로가 없다
- `_assert_single_lock_exemption()` 이 **import 시점**에 면제 대상이 늘었는지 검사한다

### 필드명을 `close_state` 로 두지 않은 이유

그 이름은 `SERVER_MANAGED_FIELDS` 로 전역 차단돼 있다(승인 payload 가 워크플로
상태를 직접 지정하지 못하게). 요청하는 것은 컬럼 값이 아니라 **연산**이므로
`reopen_to_state` 라는 별도 이름을 쓴다.

---

## Phase 31~34 — 확정 수익배분 + 지급 장부 (M029)

```
distribution_result   "3월 순이익 중 A의 몫은 4,200,000 원이다"   계산
payout_item           "그 4,200,000 원을 지급해야 한다"           채무
payout_payment        "6/15 에 2,000,000 원 보냈다"               현금
```

### `partner_settlement` 을 재사용하지 않은 이유

`settlement_crud.create_settlement` 이 **계산할 때마다 지우고 다시 쓴다.**
즉 시뮬레이션 결과다. 확정 배분은 확정 시점 snapshot 을 근거로 만들어지고
정책이 바뀌어도 그대로여야 한다 — 성질이 달라 테이블을 나눴다.
`partner_settlement` 은 미리보기 용도로 그대로 둔다.

### 회계 효과를 DB 제약으로 고정

| 유형 | `accounting_effect` | 의미 |
|---|---|---|
| `EMPLOYEE_SALARY` | `ALREADY_EXPENSED` | 이미 LABOR 비용 |
| `PARTNER_DISTRIBUTION` | `PROFIT_APPROPRIATION` | 순이익 후 이익 처분. **비용 아님** |
| `EXPENSE_REIMBURSEMENT` | `ALREADY_EXPENSED` | 원래 비용이 이미 반영됨 |

`ck_pi_type_effect` CHECK 이 유형과 효과를 짝지어 고정한다 —
동업자 배분을 비용으로 만드는 조합은 **DB 가 거부한다**.

### PAID 를 만드는 방법은 하나뿐

`paid_amount` 는 트리거가 `payout_payment` 합계에서 계산한다. 서비스는 그 컬럼에
쓰지 않는다. 거기에 CHECK 를 걸었다:

```sql
CONSTRAINT ck_pi_paid_requires_payment CHECK (
  status <> 'PAID' OR paid_amount = due_amount
)
```

→ **지급 기록 없이 PAID 를 만드는 경로가 DB 수준에 존재하지 않는다.**

과지급도 CHECK 로 막았다(`paid_amount <= due_amount`). 서비스는 그 전에 남은
금액을 알려준다 — 제약 위반 메시지만으로는 무엇을 해야 할지 알 수 없다.

### cap / minimum 은 만들지 않았다

`settlement_policy` · `partner_share_policy` 에 상한·하한 컬럼이 **없다.**
저장할 값이 없는 컬럼을 만들면 화면이 "상한 없음" 과 "미구현" 을 구분하지 못한다.

### 개인카드 상환

- 상환이 **새 expense 를 만들지 않는다** (테스트로 고정)
- 같은 비용에 상환 항목이 두 개 생기지 않는다 (부분 UNIQUE 인덱스)
- 상환 상태는 `expenses` 컬럼이 아니라 **지급 기록에서 계산**한다.
  두 곳에 상태를 두면 반드시 어긋난다

---

## Phase 35~38 — 운영 이슈 센터 + worker 기반 (M030)

### 스캐너의 첫 번째 규칙

```
같은 dedupe_key 의 열린 이슈가 있다   → last_detected_at 만 갱신
같은 dedupe_key 가 RESOLVED/IGNORED  → 건드리지 않는다
조건이 사라졌다                       → auto_resolve 인 유형만 자동 종료
```

`IGNORED` 를 매 스캔이 `OPEN` 으로 되돌리면 사용자는 **두 번째 스캔부터**
이슈 센터를 보지 않는다. 그 순간 이 기능은 없는 것과 같아진다.

검사 로직은 `close_checks` 를 **재사용**한다. 두 벌 만들면 어긋난다.
마감 검증에 없는 상시 항목만 추가로 본다(증빙 삭제 실패, 가격 반영 실패,
connector 미연결, 재계산 장기 미처리, 승인 장기 보류, 지급 예정일 초과).

### worker CLI

```bash
python -m app.scripts.run_workers --once          # 전체 한 바퀴 (cron 이 부른다)
python -m app.scripts.run_workers --worker due_approvals
python -m app.scripts.run_workers --health        # 상태만. 아무것도 실행하지 않는다
python -m app.scripts.run_workers --reclaim-only
```

- **업무 로직을 합치지 않았다.** 실행·관측만 공통화(`worker_runtime`)
- 한 worker 실패가 다음을 막지 않는다. 종료 코드 `0` / `1` / `2` 로 구분
- 실행 순서에 의미가 있다: 승인적용 → 재계산 → 이슈스캔.
  스캔을 먼저 돌리면 방금 해결된 것이 이슈로 올라온다
- `period_recalc` **처리기를 새로 만들었다.** M026 이 표시만 남기고 처리기가
  없어서 `PERIOD_RECALC_PENDING` 이 영원히 차단으로 남을 수 있었다
- stack trace 를 저장하지 않는다 — 관리 화면에 노출되면 내부 경로와 SQL 이 샌다
- 중단된 `RUNNING` 을 `reclaim_stale_runs` 가 정리한다

**실제 실행 검증**: dev DB 에서 `--once` 5개 worker 전부 성공(`failed_workers=0`),
재실행 시 `created=0 refreshed=4` 로 dedupe 동작 확인.

---

## Phase 9 — 프론트엔드 배선

### D2 수정

`lib/api/settlements.ts` 의 모든 호출을 실제 경로로 교체하고,
**경로를 Vitest 계약 테스트로 고정**했다. 경로는 그냥 문자열이라
tsc·lint·build 가 전부 통과했던 것이 이 결함의 본질이다.

### 새 화면

| 경로 | 내용 |
|---|---|
| `/settlements/monthly` | 확정월(snapshot)과 진행중(잠정)을 컬럼에서 구분 |
| `/settlements/monthly/[ym]` | 검증 → 경고 체크 → 확정 → 잠금 → 재오픈 요청 |
| `/operations/issues` | 이슈 필터·담당·기한·해결·무시 |
| `/payouts` | 지급 배치·항목·부분 지급 기록·개인카드 상환 |

사이드바에 **월 마감·지급 장부·운영 이슈**를 추가했다. 월 마감은 흐름의
종착점인데 메뉴에 없어서 URL 을 직접 쳐야 들어갈 수 있었다.

### 화면이 지키는 규칙

- 경고를 **체크박스로 하나씩** 확인해야 확정 버튼이 열린다.
  "전부 무시" 버튼 하나면 아무도 읽지 않는다
- 확정된 달의 숫자는 snapshot 에서 읽는다. 실시간 재집계로 다른 값을 보여주지 않는다
- 정산 미연결을 0원과 구분해 표시한다
- 재오픈 버튼이 달을 **바로 열지 않는다**는 사실을 안내에 명시
- 지급에 "완료 처리" 버튼이 없다. 금액을 입력받아 기록한다
- 상태를 **색만으로** 구분하지 않는다 (아이콘 + 글자)
- 실패 응답을 "오류가 발생했습니다" 로 뭉개지 않는다 —
  차단·경고·잠금·충돌·권한을 각각 다르게 안내

### 공통 개선

`lib/api.ts` 의 `query` 가 배열을 **반복 파라미터**로 편다.
쉼표로 이어 붙이면 FastAPI 가 값 하나짜리 리스트로 파싱한다.

---

## 추가로 발견한 결함 (E2E 가 잡은 것)

### D5 — 이슈 스캔이 API 경로에서만 500

`scan()` 이 요약을 `record_audit(domain_row_id=None)` 으로 남기려 했다.
`audit_log.domain_row_id` 는 **NOT NULL** 이다 — 감사 로그는 "어느 행에 무슨 일이"
구조라 대상 행이 없는 벌크 관찰을 표현할 수 없다.

서비스를 `actor_user_id` 없이 부르는 테스트만 있어서 단위 테스트가 통과했고,
actor 가 있는 **API 경로에서만** 터졌다. E2E 가 잡았다.

**수정**: 스캔 요약 감사를 제거. `worker_run` 이 이미 언제·누가·몇 건을 기록한다.
개별 이슈 상태 변경은 행 id 가 있으므로 `update_issue` 가 감사에 남긴다.
**회귀 테스트**: `test_scan_with_actor_does_not_crash`

---

## 상태 (갱신)

| 기능 | 상태 |
|---|---|
| 마감 검증 16종 + run 이력 | **live** |
| 확정 snapshot + 불변성 + drift 탐지 | **live** |
| 이월 원장 (적자·차감) | **live** |
| 재오픈 승인 워크플로 | **live** |
| 확정 수익배분 (snapshot 기준) | **live** |
| 지급 장부 · 부분 지급 · 과지급 차단 | **live** |
| 개인카드 상환 연결 | **live** |
| 운영 이슈 센터 + 스캐너 | **live** |
| worker CLI + 관측 | **live** (외부 cron 연결은 운영 작업) |
| 월 마감 · 이슈 · 지급 화면 | **live** |
| 쇼핑몰 정산 대사 | **BLOCKED_EXTERNAL_DATA** |
| 악성코드 스캐너 · S3 · 판매처 connector | **미연결** (기존 blocker 유지) |
| 알림 실제 전송 (Slack/Discord webhook) | **미구현** (credential 없음) |
| 현금흐름 ledger (Phase 43~44) | **미구현** |
