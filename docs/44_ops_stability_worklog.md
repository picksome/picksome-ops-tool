# 44. 운영 안정성 확장 — 작업 로그

> 계획: `43_ops_stability_plan.md` · 브랜치: `feat/ops-console-redesign`

## 진행 상태

| Phase | 상태 | 비고 |
|---|---|---|
| 분석 · 계획 | ✅ 완료 | `43_ops_stability_plan.md` |
| **P0 — Phase 1 이익 4단계 (BE)** | ✅ 완료 | M016 + service + API |
| **P0 — Phase 2 월 마감·잠금 (BE)** | ✅ 완료 | M017 + service + API + 쓰기 가드 |
| P1 — Phase 3 승인 워크플로 | ✅ 완료 (백엔드) | M018 + service + adapter + router + 29 테스트. 프론트 미구현 |
| P2 — Phase 4 운영 이슈 | ⬜ 대기 | M019 |
| P3 — Phase 5 감사·복원 | ⬜ 대기 | M020 |
| P4 — 어댑터 실연결 | ⬜ 대기 | M023~M027 |
| P5 — Phase 6 import | ⬜ 대기 | M021 |
| P6 — Phase 7 동기화 모니터 | ⬜ 대기 | M022 |
| P7 — Phase 8 테스트 | 🔶 부분 | 백엔드 스위트 복구 완료. 신규 케이스는 미작성 |
| P8 — Phase 9 2차 기능 | ⬜ 대기 | |

---

## 마이그레이션

| 파일 | 내용 | up/down |
|---|---|---|
| `M016_profit_status_and_stages.py` | `sales_order`: profit_status(TEXT+CHECK), profit_calculated_at, profit_finalized_at, return_window_ends_at, 인덱스<br>`product_margin_actual`: provisional/settled/final_profit_amount, calculation_snapshot_json, applied_policy_versions_json | ✅ 검증 |
| `M017_monthly_close_extension.py` | `monthly_settlement`: close_state(TEXT+CHECK), review_requested_at/by, validation_result_json, validated_at, acknowledged_warnings_json, 인덱스 | ✅ 검증 |

**backfill 결과** — sales_order 374건 → PROVISIONAL, product_margin_actual 374건 → provisional 금액 복사.
정산 매칭이 있는 행은 SETTLED 로 올라간다(현재 데이터에는 매칭 건이 없어 전부 PROVISIONAL).

### enum 대신 TEXT + CHECK 를 쓴 이유
PostgreSQL 은 `ALTER TYPE ... ADD VALUE` 를 트랜잭션에서 못 쓰고 값 제거도 지원하지 않아
**롤백이 불가능**하다. CHECK 는 `DROP CONSTRAINT` 로 되돌아간다. M007 이 marketplace 를
ENUM→TEXT 로 바꾼 것과 같은 판단이다.

### 기존 컬럼을 지우지 않은 이유
`monthly_settlement.status` 는 settlement.py / settlement_crud.py / reports.py 가 읽고 있고
REOPEN 한도(D-285)가 걸려 있다. `close_state` 를 새로 두고 backfill 한 뒤 **양쪽을 동기화**한다.

---

## 부수적으로 고친 것 — 마이그레이션이 fresh DB 에서 통과하지 못하던 문제

통합 테스트 39건이 전부 setup 에러였다. 원인이 두 겹으로 쌓여 있었다.

1. **`picksome` 롤에 CREATEDB 권한이 없었다** (메모리에 "CREATEDB 권한 이슈"로 남아 있던 항목).
   → superuser 로 `ALTER ROLE picksome CREATEDB` 실행.

2. 권한을 풀고 나니 **마이그레이션 자체가 신규 DB 에서 실패**했다. 1번에 가려 안 보이던 문제다.
   - `M004`/`M006`/`M010` 이 `PICKSOME_ORG_ID` 에 INSERT 하는데, 그 organization 행은
     마이그레이션이 아니라 `scripts/internal/picksome_init.py` 가 만든다.
     dev DB 에는 있고 신규 DB 에는 없어서 **FK 위반**으로 전체가 중단됐다.
   - → 존재하는 org 에만 넣도록 고쳤다(`_existing_orgs` 헬퍼 / `SELECT ... FROM organization WHERE id=`).
     dev DB 에서는 두 org 가 다 있어 결과가 동일하다.
   - M004 의 `marketplace` 는 그 시점에 아직 `enum_sale_channel` 이라 명시 캐스팅을 추가했다
     (M007 이 나중에 TEXT 로 바꾼다).

3. `test_sale_channel_integration.py` 가 커스텀 채널 코드로 `OHOUSE` 를 쓰는데,
   **어제 M015 가 오늘의집을 시스템 채널로 시드**하면서 충돌했다. 테스트가 검증하려는 건
   "org 전용 커스텀 채널" 이므로 시스템 카탈로그에 없는 `MYSHOP` 으로 바꿨다.

**결과: 39 errors → 0. 93 passed / 1 xfailed.**
통합 테스트가 fresh DB 에서 통과하는 것은 이번이 처음이다.

---

## 신규 파일

| 파일 | 역할 |
|---|---|
| `app/core/month_lock.py` | `assert_month_writable` — 잠긴 월 쓰기 차단. **서비스 진입점에서 백엔드가 강제**한다 |
| `app/services/monthly_close.py` | 검증(blocking/warning 8종) · 검토요청 · 확정 · 잠금 · 잠금해제 |
| `app/services/profit_status.py` | 4단계 전이 · 단계별 금액 · 미확정 사유 · 월 확정 일괄 처리 |
| `app/api/v1/monthly_close.py` | `/monthly-close/*` + `/profit-status/*` (권한 4단계 분리) |

## 변경 파일

- `app/api/v1/__init__.py` — 라우터 2개 등록 (110 routes)
- `app/services/sales.py` — create/update/delete 에 월 잠금 가드
- `app/services/expense.py` — create/update/delete 에 월 잠금 가드
  (기준일은 등록 시각이 아니라 `expense_date`. 8월에 7월 비용을 넣는 일이 흔하다)
- `M004`/`M006`/`M010` — org 존재 확인
- `test_sale_channel_integration.py` — 채널 코드 충돌 해소

---

## 설계 판단 기록

**월 마감 검증을 blocking / warning 으로 나눴다.**
증빙 하나 빠졌다고 마감을 통째로 막으면 사용자는 결국 검증을 끄거나 무시한다.
숫자가 틀리게 되는 것만 막고(미매칭 정산·원가 0원 판매·배분 비율 오류·상품 미연결 판매),
나머지는 **확인 후 진행**을 요구한다. warning 을 넘기려면 코드를 명시해야 하고 그 기록이 남는다 —
"모르고 넘어간 것"과 "보고 넘긴 것"을 구분하기 위해서다.

**CONFIRMED 에서는 아직 수정할 수 있다.** 확정은 "숫자를 맞췄다", 잠금은 "더 못 건드린다"로
층위가 다르다. 쓰기를 막는 건 LOCKED 뿐이다.

**REOPENED 를 상태로 두지 않았다.** 재개된 달은 결국 열린 달(OPEN)이다. 별도 상태로 두면
"재개 후 다시 확정한 달"을 표현할 수 없다. 재개 사실은 `reopen_count` + audit_log 로 남긴다.

**FINAL 은 자동으로만 만들어진다.** 월 잠금 시점에 조건(반품기한 경과 + 정산 매칭 완료)을
만족한 주문만 올린다. 못 채운 건 **건너뛰고 카운트로 돌려준다** — 억지로 올리면 틀린 숫자가 확정된다.

**이익 단계는 역행하지 않는다.** 되돌리려면 월 잠금 해제가 선행돼야 한다.
FINAL 을 임의로 낮추면 확정 보고한 숫자가 조용히 달라진다.

---

## 검증 로그

| 시각 | 검증 | 결과 |
|---|---|---|
| 2026-08-06 | `alembic upgrade head` (dev DB) | ✅ M017 |
| 2026-08-06 | `alembic downgrade M015` → `upgrade head` | ⚠️ **정정됨 — 아래 P0.5 §2 참조** |
| 2026-08-06 | backfill 확인 (374/374) | ✅ |
| 2026-08-06 | `pytest app/tests` | ✅ 93 passed, 1 xfailed |
| 2026-08-06 | 라우터 import + route 수 | ✅ 110 |

---

# P0.5 안정성 검증 (2026-08-06)

Phase 3(M018) 착수 전 수행. **Phase 1~2 를 재작성하지 않고 검증만** 했다.

## §1. DB 역할과 CREATEDB 권한 — 회수 완료

### 무엇이 잘못됐나
어제 통합 테스트를 돌리려고 `ALTER ROLE picksome CREATEDB` 를 실행했다.
`picksome` 은 `.env` 의 `DATABASE_URL` 이 쓰는 **운영 애플리케이션 역할**이다.
애플리케이션이 임의로 데이터베이스를 만들 수 있으면 최소권한 원칙이 깨진다.

### 조치

| 역할 | 용도 | 권한 |
|---|---|---|
| `picksome` | 운영 애플리케이션 (런타임 DML + 자기 DB 내 alembic DDL) | `NOCREATEDB NOCREATEROLE NOSUPERUSER` |
| `picksome_test` | 테스트/CI 전용 (테스트 DB 생성) | `CREATEDB`, 운영 DB `CONNECT` 회수 |

- `backend/scripts/bootstrap_roles.sql` **신규** — superuser 로 1회 실행하는 인프라 스크립트.
  **마이그레이션에 넣지 않았다.** 운영 DB 에서 alembic 이 `ALTER ROLE`/`GRANT` 를 실행하면
  배포가 권한 경계를 조용히 바꾸게 된다.
- `app/tests/conftest.py` — 테스트 DB 접속을 `picksome_test` 로 전환.
  `TEST_ADMIN_DATABASE_URL` 로 CI bootstrap 계정을 따로 줄 수 있다.
  권한이 없으면 **명확한 안내와 함께 skip** 한다(전에는 39개 에러 스택이 쏟아졌다).

### 확인
```
picksome       rolsuper=f  rolcreatedb=f  rolcreaterole=f
picksome_test  rolsuper=f  rolcreatedb=t  rolcreaterole=f
```
이 상태에서 전체 테스트 통과 — 운영 역할에 CREATEDB 없이도 테스트가 돈다.

## §2. downgrade/upgrade — "무손실" 표현 정정

**이전 기록이 부정확했다.** "downgrade M015 → upgrade head 무손실" 은
*마이그레이션이 에러 없이 끝났다* 는 뜻이었지 데이터가 보존됐다는 뜻이 아니었다.

실측 테스트를 작성했다 — `app/tests/integration/test_migration_data_safety.py`
(전용 scratch DB 를 만들어 실행. 공용 테스트 DB 에서 downgrade 하면 다른 테스트가 스키마를 잃는다)

| 구분 | 대상 | 결과 |
|---|---|---|
| **schema downgrade/upgrade 성공** | M017 → M015 → head | 성공 |
| **기존 M015 데이터 보존** | `sales_order` 행·business_key·sale_price·quantity<br>`monthly_settlement` 행·net_profit·**기존 `status`** | 보존됨 |
| **M016 이후 상태 데이터 보존** | `profit_status`(수동 SETTLED) | **소실** → backfill 기본값 `PROVISIONAL` 로 되돌아감 |
| | `return_window_ends_at` | **소실** (복원 근거 없음 → NULL) |
| | `close_state` = `REVIEW` | **소실** → 기존 `status`(DRAFT)에서 backfill 되어 `OPEN` |
| | `validation_result_json` | **소실** |
| | `acknowledged_warnings_json` | **소실** |

**결론**: downgrade 는 스키마를 되돌리지만 M016/M017 전용 컬럼의 값은 되살리지 않는다.
당연한 동작이지만 "무손실" 이라고 부르면 안 된다. 운영에서 downgrade 를 실행하려면
**M016 이후 상태값을 별도 백업**해야 한다.

## §3. ENUM 선택 근거 정정 (PostgreSQL 16.9)

이전 기록의 "PostgreSQL 은 `ALTER TYPE ADD VALUE` 를 트랜잭션에서 못 쓴다" 는 **틀렸다.**
PG 12 부터 가능하다. 실제 16.9 에서 실행해 확인했다.

| 시도 | 결과 |
|---|---|
| 트랜잭션 안 `ALTER TYPE t ADD VALUE 'C'` | 성공 |
| 같은 트랜잭션에서 새 값 사용 | 실패 — `UnsafeNewEnumValueUsage` |
| `ALTER TYPE t DROP VALUE 'B'` | 실패 — 문법 없음 (`SyntaxError`) |

TEXT + CHECK 선택은 유지하되 근거를 4가지로 다시 썼다 (43번 문서 §1-2):
① 추가한 값을 같은 마이그레이션에서 backfill 에 쓸 수 없다
② 값 제거가 불가능해 downgrade 가 원상복구되지 않는다
③ enum downgrade 는 참조 컬럼 전체 재생성이 필요하다
④ 상태 정책이 자주 바뀐다 (실제로 `REOPENED` 를 상태에서 뺐다)

## §4. 쓰기 가드 범위 — 공통 함수로 통합

컨트롤러별 중복 검사 대신 **서비스 계층 단일 함수**로 통합했다.

```python
assert_period_writable(db, org_id=..., effective_date=..., operation=..., what=...)
```

| 쓰기 경로 | 위치 | operation | 가드 |
|---|---|---|---|
| 판매 등록 | `sales.create_sale` | `CREATE` | 적용 |
| 판매 수정 | `sales.update_sale` | `UPDATE` | 적용 |
| 판매 삭제(soft) | `sales.delete_sale` | `DELETE` | 적용 |
| 비용 등록 | `expense.create_expense` | `CREATE` | 적용 |
| 비용 수정 | `expense.update_expense` | `UPDATE` | 적용 |
| 비용 삭제(soft) | `expense.delete_expense` | `UPDATE` | 적용 |
| **실 마진 재계산** | `margin.save_actual_margin` | `RECALC_MARGIN` | **신규 추가** |
| **광고비 배분(bulk)** | `ad_cost_allocation.allocate_ad_cost_for_period` | `ALLOCATE_AD_COST` | **신규 추가** |
| **잠정 이익 재계산** | `profit_status.recalculate_provisional` | `RECALC_PROFIT` | **신규 추가** |
| 봇/API 주문 수집 | `integration_ingest.ingest_orders` | `sales.create_sale` 경유 | 간접 적용 |
| 월 잠금 시 이익 확정 | `profit_status.finalize_month` | `FINALIZE_PROFIT` | **의도적 통과** |
| 월 마감 상태 전이 | `monthly_close._transition` | `CLOSE_TRANSITION` | **의도적 통과** |

`SYSTEM_OPERATIONS` 화이트리스트만 통과한다.
**잠금 절차 자체가 잠금에 막히면 잠금이 완료되지 않는다.**

미적용(해당 기능이 아직 없음): bulk update API, import wizard, marketplace sync,
background job, internal admin endpoint, backfill —
Phase 6·7 에서 만들 때 같은 함수를 호출한다.

## §5. 동시성·멱등성 — 실제 버그 2건 발견·수정

`app/tests/integration/test_monthly_close_concurrency.py` **신규 (18 케이스)**

### 발견한 버그

**① 상태 전이에 원자성이 없었다.**
`_transition` 이 SELECT 후 UPDATE 라 두 요청이 모두 통과할 수 있었다.
→ `SELECT ... FOR UPDATE` 행 잠금 + **compare-and-swap**
(`WHERE id = :id AND close_state = :expected_from`, rowcount 0 이면 409) 추가.

**② 전이가 거부될 때 행 잠금을 놓지 않았다.**
`_ALLOWED` 검사 실패로 raise 하면 `FOR UPDATE` 잠금이 커넥션에 남아
같은 행을 건드리는 다른 커넥션이 **무한 대기**했다. 테스트에서 실제 교착이 재현됐다.
→ 거부 경로에서 `db.rollback()` 후 raise.

### 테스트가 잡아준 것
- `actor_user_id` 에 임의 UUID → `app_user` FK 위반. 실제 사용자 없이는 전이가 안 된다.
- `close_state='CONFIRMED'` 를 직접 INSERT → `ck_ms_confirmed_meta` 위반.
  DB 가 "확정 메타 없는 확정 상태" 를 막고 있다.
  테스트를 **실제 전이 경로(confirm→lock)** 로 바꿨다.

### 커버리지
동시 확정(한 쪽만 성공) · 중복 잠금 거부 · `reopen_count` 중복 증가 방지 ·
실패 전이의 감사 로그 미생성 · 사용자 쓰기 연산 10종 전면 차단(parametrize) ·
시스템 연산 통과 · OPEN/CONFIRMED 는 쓰기 허용 · 정산 행 없는 달은 열린 달

## §6. backfill 안전성

`test_migration_data_safety.py` 로 검증.

| 항목 | 결과 |
|---|---|
| 대상 건수 (dev DB) | `sales_order` 374 / `product_margin_actual` 374 |
| 최초 변경 건수 | 374 / 374 |
| **2회차 변경 건수** | **0** (모든 backfill 이 `WHERE ... IS NULL` 조건) |
| 미변경 건수 | 전체 (2회차) |
| 실패 건수 | 0 |
| 수동값 보호 | 수동 지정한 `FINAL` 이 재실행으로 덮어써지지 않음 |
| NULL·불완전 데이터 | `NOT NULL` + `DEFAULT` 로 신규 행은 항상 값 보유 |
| organization 격리 | 다른 org 행이 서로의 값을 가져오지 않음 |
| CHECK 강제 | 정의되지 않은 상태값 INSERT 는 `CheckViolation` |

**감사 정보 한계**: 마이그레이션 backfill 은 `audit_log` 를 남기지 않는다.
스키마 변경의 일부이고 actor 가 없어서다. 마이그레이션 파일 자체가 기록이다.

## §7. fresh DB 마이그레이션 수정 방식

| 질문 | 답 |
|---|---|
| 과거 migration 파일을 직접 수정했나 | **그렇다** — `M004`, `M006`, `M010` |
| 신규 보정 migration 을 만들었나 | 아니다 |
| seed 를 migration 내부에 넣었나 | 아니다. 기존 seed 를 **조건부**로 바꿨을 뿐 |
| 외부 init script 의존성을 제거했나 | **아니다** — `PICKSOME_ORG_ID` 는 여전히 `scripts/internal/picksome_init.py` 가 만든다 |
| 기존 운영 DB 와 fresh DB 의 최종 schema 가 같은가 | **schema 는 같다.** seed 데이터는 다르다(fresh 에는 PICKSOME org 없음) |
| 기존 배포 환경에 부작용이 있나 | **없다** — dev DB 에는 두 org 가 다 있어 `_existing_orgs` 가 둘 다 반환한다 |

### 왜 과거 파일을 고쳤나
보정 마이그레이션을 새로 만들면 **fresh DB 에서는 M004 가 여전히 실패**한다.
실패 지점 자체를 고쳐야 신규 환경에서 `upgrade head` 가 끝까지 간다.
이미 적용된 환경에서는 재실행되지 않으므로 영향이 없다.

**남은 위험**: `PICKSOME_ORG_ID` 가 마이그레이션 밖에 있는 구조는 그대로다.
fresh DB 로 운영을 시작하려면 `picksome_init.py` 를 반드시 실행해야 한다.
근본 해결(org seed 를 마이그레이션으로 이관)은 별도 작업으로 남긴다.

### M015 오늘의집 채널 충돌
`test_sale_channel_integration.py` 가 커스텀 채널 코드로 `OHOUSE` 를 썼는데
M015 가 오늘의집을 **시스템 채널**로 시드하면서 충돌했다.
테스트가 검증하려는 건 "org 전용 커스텀 채널" 이므로
시스템 카탈로그에 없는 `MYSHOP` 으로 바꿨다.

**시스템 채널 seed 현황 점검**

| 조건 | 상태 |
|---|---|
| 반복 실행 가능 | 충족 — M015 가 중복 방지 조건 사용 |
| 다른 조직 custom channel 을 덮어쓰지 않음 | 충족 — 시스템 채널은 `org_id IS NULL` |
| code 충돌 시 원인을 숨기지 않음 | 충족 — `ValidationError("이미 존재하는 채널 코드입니다: …")` |
| 시스템/사용자 채널 namespace 구분 | **미흡** — `org_id NULL 여부`로만 구분하고 코드 네임스페이스는 공유한다. org 가 특정 코드를 먼저 만들어 두면 나중에 같은 코드의 시스템 채널을 추가할 때 충돌한다. **미해결 위험으로 기록** |

## §8. 테스트 실행 결과

실행한 명령:

```
# 역할 부트스트랩 (superuser 1회)
psql -U postgres -f backend/scripts/bootstrap_roles.sql

# 기존 테스트 DB 제거 → picksome_test 소유로 재생성되게
DROP DATABASE picksome_ops_test;

cd backend
python -m pytest app/tests -q -p no:cacheprovider
```

| 실행 | 결과 |
|---|---|
| 전체 스위트 | **115 passed, 1 xfailed** (20.8s) |
| — 기존 | 93 passed |
| — 신규 `test_migration_data_safety.py` | 4 passed |
| — 신규 `test_monthly_close_concurrency.py` | 18 passed |
| `alembic upgrade head` (fresh DB) | 성공 — M017 |
| `alembic downgrade M015` → `upgrade head` | 스키마 왕복 성공 (데이터는 §2 표 참조) |
| backfill 2회 실행 | 2회차 변경 0건 |
| 운영 역할 CREATEDB 없이 전체 통과 | 확인 |

기존 xfail 1건은 `reverse_calc_price_for_target_margin` 의 FIXED 수수료 처리 오차로,
이번 작업과 무관한 기존 알려진 항목이다.

## §9. M018 사전 설계 — 완료 (코드는 미구현)

상태 머신·불변조건 8개·결정 8건·미결정 6건을 `43_ops_stability_plan.md §8` 에 작성했다.

핵심 결정: 자기 승인 기본 금지(설정으로 해제 가능) / 열린 요청은 대상당 1건(부분 UNIQUE 인덱스) /
`before_snapshot` 비교로 낙관적 잠금 / 적용 후 취소 대신 새 요청 / `Idempotency-Key` /
잠긴 월은 승인은 되되 적용이 차단되어 `APPROVED` 로 대기.

**미결정(설정값으로 둠)**: 원가·판매가 변동 임계값, 저마진 기준, 고액 비용 기준,
승인자 지정 방식, SLA. 전부 사업 구조를 알아야 정할 수 있어 임의 확정하지 않았다.

**M018 코드는 구현하지 않았다.**

---

# Phase 3 — M018 승인 워크플로 (2026-08-06)

## 사전 안전성 확인 (구현 전)

### ① SYSTEM_OPERATIONS 우회 — 구조적으로 제거

**현황 점검**: `assert_period_writable(operation=...)` 호출부를 전수 조사했다.
전부 서비스 내부의 리터럴 문자열이고, API 라우터에서 `operation` 을 받는 곳은 없다.
**즉 지금 당장 악용 가능한 경로는 없었다.**

**그럼에도 고친 이유**: `operation` 이 평범한 `str` 이라 M018 의 적용 경로가
요청 payload 값을 그대로 넘기는 순간 `"FINALIZE_PROFIT"` 하나로 월 잠금이 조용히 뚫린다.
설계가 "호출자가 실수하지 않기" 에 의존하고 있었다.

**조치**
- `SYSTEM_OPERATIONS` 화이트리스트를 **삭제**했다. 우회 경로 자체가 없다.
- `USER_WRITE_OPERATIONS` 화이트리스트를 두고, **모르는 값은 통과가 아니라 `ValueError`** 다.
  오타나 주입이 "통과" 로 해석되면 안 된다.
- 잠금 절차 내부(`monthly_close._transition`, `profit_status.finalize_month`)는
  가드를 **아예 호출하지 않는다**. 문자열로 통과 여부를 판정하지 않는다.
  `assert_close_machinery_only()` 는 런타임 동작 없는 표식으로, "여기는 의도적으로
  가드를 부르지 않는 자리" 라는 사실을 코드에 남긴다.
- 승인 적용은 `APPLY_APPROVAL` 이라는 **일반 사용자 쓰기 연산**이다.
  승인만으로 잠긴 달을 바꿀 수 있으면 잠금이 의미를 잃는다.

### ② transaction rollback 소유권 — SAVEPOINT 로 전환

**현황 점검**: `deps.get_db` 는 세션만 넘기고 커밋/롤백을 하지 않는다.
즉 **서비스는 요청 트랜잭션을 소유하지 않고 공유**한다.
그런데 `monthly_close._transition` 의 거부 경로가 `db.rollback()` 을 호출했다 —
같은 요청에서 앞서 한 무관한 변경까지 통째로 되돌린다.

**조치**: `db.begin_nested()` SAVEPOINT 로 감싸고 거부 시 `savepoint.rollback()` 만 한다.
`FOR UPDATE` 로 잡은 행 잠금도 함께 풀리므로 교착도 생기지 않는다
(PostgreSQL 은 savepoint 이후 획득한 잠금을 함께 해제한다).
`unlock` 의 한도 초과 경로는 잠금을 잡지 않았으므로 rollback 을 제거했다.

**M018 에도 같은 패턴을 적용**했다 (`approval._transition`).
테스트로 고정: `test_rejected_transition_does_not_rollback_unrelated_work`

## 실제 수정한 파일

### 신규
| 파일 | 역할 |
|---|---|
| `app/db/migrations/versions/M018_approval_request.py` | 마이그레이션 |
| `app/services/approval.py` | 상태 머신 · 정책 · 적용 |
| `app/services/approval_targets.py` | target adapter + 레지스트리 |
| `app/api/v1/approvals.py` | 라우터 9개 엔드포인트 |
| `app/tests/integration/test_approval_workflow.py` | 29 케이스 |

### 수정
| 파일 | 변경 |
|---|---|
| `app/core/month_lock.py` | `SYSTEM_OPERATIONS` 제거, `USER_WRITE_OPERATIONS` 도입, 미지 연산 `ValueError` |
| `app/services/monthly_close.py` | `db.rollback()` → SAVEPOINT |
| `app/api/v1/__init__.py` | approvals 라우터 등록 (120 routes) |
| `app/tests/integration/test_monthly_close_concurrency.py` | 우회 테스트 → 우회 불가 테스트로 교체 (+1 케이스) |
| `app/tests/integration/test_migration_data_safety.py` | M018 왕복 + 제약·인덱스 검증 (+1), seed key 유일화 |

## migration revision

**M018** (`down_revision = M017`). up/down/up 모두 실행 가능.
`DROP TABLE` 로 되돌아가며, 이 테이블에만 있던 승인 이력은 사라진다 —
M016/M017 과 같은 성질이고 P0.5 §2 의 "downgrade 는 무손실이 아니다" 원칙이 그대로 적용된다.

## 실행한 명령

```
# 테스트 DB 완전 재생성
python -c "psycopg ... DROP DATABASE picksome_ops_test"   # picksome_test 소유로 재생성됨

cd backend
python -m alembic upgrade head          # M018
python -m alembic downgrade M017        # 되돌림
python -m alembic upgrade head          # 재적용
python -m pytest app/tests -q -p no:cacheprovider
```

## 테스트 결과

| 실행 | 결과 |
|---|---|
| **fresh DB 전체 스위트** | **146 passed, 1 xfailed** (44.7s) |
| — P0.5 이전 기존 | 93 |
| — P0.5 신규 (migration 안전성 + 동시성) | 23 → 24 (우회 불가 테스트 추가) |
| — **M018 신규** | **29** |
| — M018 migration 왕복 | 1 (`test_migration_data_safety` 5번째) |
| `alembic upgrade head` (fresh) | 성공 — M018 |
| `alembic downgrade M017` → `upgrade head` | 성공, M016/M017 데이터 영향 없음 |
| 인덱스 확인 | 8 + PK = 9개 존재 |

xfail 1건은 기존 알려진 항목(FIXED 수수료 역산 오차), 이번 작업과 무관.

## 발견하고 해결한 결함

| # | 결함 | 발견 경로 | 조치 |
|---|---|---|---|
| 1 | 월 잠금에 문자열 우회 경로 존재 (잠재) | 사전 점검 | 화이트리스트 반전 + 미지 연산 `ValueError` |
| 2 | 서비스가 공유 트랜잭션을 무조건 rollback | 사전 점검 | SAVEPOINT 전환 |
| 3 | `product` 에 판매가 컬럼이 없음 | 어댑터 작성 중 | `sale_price` 를 승인 대상에서 제외. 없는 컬럼을 열어두면 적용 단계에서 조용히 실패한다 |
| 4 | 테스트 seed 의 `jsonb_set` 이 중첩 경로를 못 만듦 | 테스트 실패 | `jsonb_build_object` 병합으로 교체 |
| 5 | 마이그레이션 테스트에서 `app_user` 없이 INSERT → 0행이라 CHECK 미평가 | 테스트가 통과해버림 | 실제 actor 를 넣고 검증 |
| 6 | scratch DB 공유로 seed business_key 충돌 | 테스트 실패 | 호출마다 유일 키 |

## 남은 위험

- **판매가 승인 불가** — `product` 에 판매가 컬럼이 없다. 스키마 확장이 선행돼야 한다.
  현재는 원가·기본정보·상태만 승인 대상이다.
- **`effective_from` 미래일 자동 적용 스케줄러 미구현** — 값은 저장되고 월 잠금 판정에
  쓰이지만, 지정일에 자동으로 적용하는 배치는 없다. 지금은 수동 `/apply` 가 필요하다.
- **승인 필요 여부 자동 판정 미구현** — 임계값이 전부 미확정이라 판정 훅을 붙이지 않았다.
  사용자가 명시적으로 요청을 만든다.
- **감사 로그 실패 시 롤백 검증 미흡** — `record_audit` 실패를 강제로 재현하는 테스트를
  넣지 않았다. 같은 트랜잭션이라 구조적으로는 함께 롤백되지만 실측하지 않았다.
- `ux_approval_open_target` 위반을 문자열 매칭(`"ux_approval_open_target" in str(exc)`)으로
  잡는다. 제약 이름이 바뀌면 조용히 500 이 된다.

## 구현하지 않은 항목

- Phase 4 이후 승인 대상 (비용 승인, 정산 강제 매칭, 정책 변경, 월 마감/잠금해제 연동)
- 미래 적용일 스케줄러
- 승인 필요 여부 자동 판정
- 프론트엔드 (승인 대기함 화면, 승인/반려 drawer, 변경 전후 비교)
- `revise` 흐름 (현재는 취소 후 새 요청)

---

# M018 안정화 + M019 자동 적용기 + 승인 프론트 (2026-08-06)

## 1. 제약조건 오류 처리 — 문자열 매칭 제거

**전**: `if "ux_approval_open_target" in str(exc)`
**후**: `app/core/db_errors.py` — `psycopg` 의 `diag.constraint_name` 으로 분기.
SQLAlchemy 를 거치면 `exc.orig.diag.constraint_name`, 중첩 wrapper 도 한 단계 더 본다.

왜 고쳤나: 이 환경의 PostgreSQL 은 **한국어 오류 메시지**를 낸다. 로케일이 바뀌거나
다른 위반이 우연히 같은 문자열을 포함하면 오분류된다.

**알 수 없는 무결성 오류는 삼키지 않는다.** 매핑에 없으면 `None` 을 돌려주고
원래 예외를 그대로 올려보내 500 이 되게 한다 — 409 로 뭉개면 진짜 버그가 숨는다.

응답:
```json
{ "code": "APPROVAL_OPEN_REQUEST_EXISTS",
  "message": "이 대상에는 이미 진행 중인 승인 요청이 있습니다.",
  "details": { "existing_request_id": "...", "target_type": "product", "target_id": "..." } }
```

## 2. idempotency fingerprint

**전**: 같은 키면 payload 를 보지 않고 기존 요청 반환 → 다른 내용의 요청이 조용히 무시됨.
클라이언트는 자기 요청이 반영됐다고 믿는다.

**후**: `request_fingerprint` (M019 컬럼).
`canonical_fingerprint()` 가 `target_type / target_id / request_type / proposed_changes /
effective_from` 을 `sort_keys=True` + 고정 구분자로 직렬화해 SHA-256.
key 순서·공백이 달라도 같은 해시가 나온다.

| 상황 | 결과 |
|---|---|
| 같은 키 + 같은 내용 | 기존 요청 반환 |
| 같은 키 + 다른 내용 | **409 `IDEMPOTENCY_KEY_REUSED`** |
| 같은 키 + 다른 대상 | 409 |
| 다른 조직 같은 키 | 충돌 없음 (유일 범위가 `(org_id, idempotency_key)`) |

원문 payload 를 중복 저장하지 않는다. 해시만 남긴다.

## 3. JSONB 검증 — `app/domain/approval.py`

COMMENT 는 문서지 검증 장치가 아니다. 실제 검증을 도메인 모델로 옮겼다.

- 임의 key 금지 (어댑터 `allowed_fields` 화이트리스트)
- **`SERVER_MANAGED_FIELDS`** 차단 — `id/org_id/version/status/created_at/...`
  JSONB 로 들어오면 조용히 UPDATE 문에 섞일 수 있어 이름 단위로 막는다
- 빈 변경 거부
- 중첩 구조·리스트 거부 (평면 dict 만) — 중첩을 허용하면 어떤 컬럼이 바뀌는지
  정적으로 알 수 없고 전후 비교 UI 도 못 만든다
- **service 진입점에서 검증한다.** 라우터만 검증하면 자동 적용기·배치·테스트가 뚫린다

`build_diff()` 가 서버에서 비교 구조를 만들어 준다 — 프론트가 JSON 을 그대로 노출하지 않게.

## 4. 활성 요청 UNIQUE 정책 — **선택지 A 유지** (결정)

현재 인덱스:
```sql
CREATE UNIQUE INDEX ux_approval_open_target
  ON approval_request (org_id, target_type, target_id, request_type)
  WHERE status IN ('DRAFT','PENDING','APPROVED') AND deleted_at IS NULL;
```

열린 상태 = `DRAFT | PENDING | APPROVED`. `REJECTED/APPLIED/CANCELLED` 는 제외.

**대상당 1건이 아니라 (대상 + request_type) 당 1건**이다. 즉 이미 선택지 A 와 B 의
중간이다. `COST_CHANGE` 가 미래 적용 대기 중이어도 `PRODUCT_INFO_CHANGE` 는 만들 수 있다.

| 선택지 | 채택 | 이유 |
|---|---|---|
| A 대상당 1건 | ✗ | 미래 적용 건이 있으면 다른 필드 변경까지 막힌다 |
| **B 대상+action type 당 1건** | **✓ 현행** | 미래 적용과 다른 유형 변경이 공존한다. 충돌 검사도 단순 |
| C 변경 필드 겹침 검사 | ✗ | 가장 유연하지만 부분 인덱스로 표현할 수 없고 애플리케이션 검사는 동시성에 뚫린다 |

바꾸지 않았다. 대신 충돌 응답에 **기존 요청 id** 를 담아 UI 가 "기존 요청 보기" 로
바로 이동할 수 있게 했다.

## 5. 상태 전이 의미 — `REJECTED → CANCELLED` 검토 결과

**전이를 유지한다.** 단, 업무 의미를 명시한다.

- `REJECTED` 는 **최종 이력**이다. 수정하려면 새 요청을 만든다.
- `CANCELLED` 는 제출자·관리자가 **진행 중** 요청을 철회한 상태다.
- `REJECTED → CANCELLED` 는 **목록 정리 목적**의 아카이빙이다. 반려 사실을 지우지 않는다:
  - `reviewed_by` / `reviewed_at` / `review_comment` 가 그대로 남는다
  - `audit_log` 에 `REJECTED` 전이가 별도 행으로 남는다
  - UI 는 `CANCELLED` 여도 `review_comment` 가 있으면 반려 사유를 계속 표시한다

**UI 에서는 `REJECTED` 에 취소 버튼을 노출하지 않는다.** 반려 이력을 가리지 않기 위해
"수정하려면 새 승인 요청을 만드세요" 안내만 보여준다.

## 6. 자기 승인 설정 키 — 오타 없음 (확인 결과)

이전 보고서에 `settings_json.approval.self_approv` 라고 적혀 있었으나
**코드는 처음부터 `self_approve_allowed` 로 일관**되어 있었다. 보고서 서술이 잘렸던 것이다.

- `services/approval.py` — `approval.get("self_approve_allowed", False)`
- M018 컬럼 COMMENT — `self_approve_allowed(bool)`
- 조직 설정 실데이터 확인: 두 org 모두 `settings_json = {}` → 저장된 키 없음

**하위 호환 코드를 추가하지 않았다.** 존재한 적 없는 키에 fallback 을 넣으면
읽는 사람이 "예전에 그 키가 있었나" 하고 오해한다. canonical key 는 `self_approve_allowed`.

자기 승인 판단은 **승인 시점의 현재 설정 + 현재 권한**을 본다.
`approval_policy_snapshot_json` 은 "그때 무슨 규칙이었나" 를 남기는 기록이지 판정 근거가 아니다.

## 7. 감사 로그 실패 시 전체 롤백 — 강제 실패 테스트

`monkeypatch` 로 `record_audit` 이 `domain_table='product'` 일 때 예외를 던지게 했다.
**mock 호출 횟수가 아니라 실제 DB 상태**를 확인한다.

| 검증 | 결과 |
|---|---|
| 대상 product 변경 없음 | ✓ cost_amount 그대로 |
| 승인 요청 상태 미변경 | ✓ APPROVED 유지, applied_at NULL |
| version 미증가 | ✓ |
| 감사 로그 부분 잔존 없음 | ✓ product 감사 0건 |
| 재시도 시 정상 적용 | ✓ |

## 8. M019 자동 적용기

### 시간 기준
`effective_from` 은 **DATE**(M018 스키마). 시각 정보가 없다.
→ **KST 그 날짜 00:00 부터 적용 대상**. `today_kst()` 가 UTC + 9h 로 직접 계산한다.
서버 로컬 시간대에 의존하지 않는다 (UTC 서버에서 `date.today()` 를 쓰면 한국 자정 직후
9시간 동안 하루 전 날짜가 나온다).

### 실행 방식 (3가지)
1. `POST /api/v1/approvals/run-due` — 보호된 admin batch endpoint (ADMIN)
2. `python -m app.scripts_run_due [--at YYYY-MM-DD] [--batch-size N]` — CLI
3. 프론트 승인 화면의 "적용 예정분 실행" 버튼 (ADMIN)

cron/워커 플랫폼이 없어도 돌아간다. 중요한 건 **여러 인스턴스가 동시에 실행해도
한 번만 적용되는 구조**다.

### 작업 선점
```sql
SELECT ... FROM approval_request
 WHERE status = 'APPROVED' AND effective_from <= :at
   AND (next_retry_at IS NULL OR next_retry_at <= now())
   AND (auto_apply_status IS NULL OR auto_apply_status IN ('PENDING','RETRYING','HELD_BY_LOCK'))
 ORDER BY effective_from, requested_at, id
 FOR UPDATE SKIP LOCKED LIMIT :batch_size
```
건별로 독립 커밋한다 — **한 건 실패가 배치 전체를 막지 않는다.**

### 수동/자동 공존
**같은 `approval.apply()` 함수를 쓴다.** 별도 적용 로직을 만들지 않았다.
실행기는 "누구를 언제 부를지" 와 "실패를 어떻게 기록할지" 만 한다.
자동은 `applied_by_type='SYSTEM'`, 수동은 `'USER'` (apply 가 `COALESCE` 로 보존).

### 실패 처리 — 반려로 바꾸지 않는다
`status` 는 `APPROVED` 유지. `auto_apply_status` 로만 구분한다.

| 상황 | auto_apply_status | 재시도 |
|---|---|---|
| 월 잠금 | `HELD_BY_LOCK` | **없음** — 자동으로 잠금을 풀지 않는다. 사람이 결정할 일 |
| 미지원 대상 / 검증 실패 / stale / 대상 없음 | `PERMANENT_FAILURE` | 없음 |
| 그 외 일시적 오류 | `RETRYING` | 지수 backoff 5→15→60→240→720분, 최대 5회 |
| 성공 | `DONE` | — |

`last_error_message` 에는 **사용자용 문장만** 넣는다. SQL·비밀값·개인정보 금지.

### 관측성
`BatchResult` — `batch_id / started_at / finished_at / scanned / applied /
already_applied / held_by_lock / validation_failed / other_failed / details[]`.
CLI 는 실패가 있으면 exit 1 로 끝나 스케줄러가 알림을 띄울 수 있다.

## 9. 프론트엔드

### 라우트·위치
`/approvals`. 사이드바 **최상위 단일 항목**, 알림·연동 그룹 바로 앞.
승인은 "내가 처리해야 할 일" 이라 알림과 성격이 같고, 건수가 적을 때 독립 그룹은 과하다.

### 파일
| 파일 | 역할 |
|---|---|
| `lib/api/approvals.ts` | API 클라이언트 + 오류 안내 + diff 생성. **mock 없음** — 실제 API 만 |
| `components/approvals/approval-drawer.tsx` | 상세 drawer + 확인 dialog + 상태 배지 |
| `app/(app)/approvals/page.tsx` | 목록 5탭 + 필터 + 자동 적용 실행 |
| `lib/api.ts` | `RequestOptions.headers` 추가 (Idempotency-Key 전달) |

### 탭 / 필터 / 열
탭: 승인 필요 · 내가 요청한 항목 · 적용 예정 · 완료 · 전체
필터: 기간(요청일) · 상태 · 변경 유형 · 요청자 · 키워드
열: 요청번호 · 대상 · 변경 유형 · 요청자 · 요청일 · 적용 예정일 · 상태 · 승인자 · 적용 상태 · 최근 변경

`target_type` 기반으로 라벨을 매핑한다. **미지원 대상 UI 를 미리 노출하지 않는다**
(`FIELD_LABELS_BY_TARGET` 에 `product` 만 등록).

### 변경 전후 비교
JSON 을 그대로 노출하지 않는다. 필드별 비교표:
- 바뀐 필드만 기본 표시 + "전체 필드 보기" 토글
- 금액은 원화 포맷, 날짜는 KST, URL 은 클릭 가능한 외부 링크(truncate)
- **`null`(없음) 과 `""`(빈 값) 을 구분**해 표시
- 서버 관리 필드는 아예 제외

### 상태별 액션
`DRAFT` 제출·취소(본인) / `PENDING` 승인·반려(권한자)·취소(본인) /
`APPROVED` 지금 적용·취소 + 자동 적용 예정 표시 / `APPLIED` 대상 보기 /
`REJECTED` 반려 사유 표시 + "새 요청을 만드세요" 안내

자기 승인 차단 시 버튼이 disabled 되고 이유를 문장으로 표시한다.
**버튼 숨김이 권한 제어가 아니다** — 백엔드가 403 을 낸다.

### 오류별 UX
`describeApprovalError()` 가 코드마다 제목·설명·재조회 여부·기존 요청 id 를 준다.
10개 코드 전부 개별 안내. 409 계열은 목록·상세를 자동 재조회한다.

## 10. 상품 drawer 연동 — **미구현**

`components/products/product-drawer.tsx` 에 "승인 요청으로 저장" 경로를 붙이지 않았다.
현재 상품 drawer 는 `productAdapter.save()`(mock)를 호출하는 구조라,
승인 연동을 붙이려면 어댑터 실연결이 선행돼야 한다.

임계값 자동 판정은 이번 범위 밖이므로, 붙일 때는 사용자가 명시적으로
`[초안 저장] [승인 요청]` 을 고르는 방식으로 한다 (`ThresholdNotice` 컴포넌트가 준비돼 있다).

## 11. 판매가 승인 스키마 — 설계 문서 별도 (`docs/46_sale_price_schema_design.md`)

`product.sale_price` 를 임의로 추가하지 않았다.

## 12. 남은 미결정

`cost_change_threshold_rate` · `price_change_threshold_rate` · `min_margin_rate_for_auto` ·
`expense_approval_threshold` · 승인자 지정 방식 · SLA — 전부 `thresholds_resolved: false` 로 표시.
프론트는 "자동 승인 판정 기준이 아직 설정되지 않았습니다" 안내만 띄운다.

## 13. 권한 정책 검토 결과

현재 `product` 승인·반려·적용 = `ADMIN` / `OPERATOR`.

| 질문 | 판단 |
|---|---|
| OPERATOR 가 타인 요청을 승인해도 되는가 | **유지.** 상품 원가는 운영 담당이 판단하는 게 자연스럽다. 자기 승인만 막는다 |
| 승인과 적용 권한이 같아야 하는가 | **현재 같다.** 분리하면 "승인은 됐는데 아무도 적용 못 하는" 교착이 생긴다. `can_apply` 를 별도 정책 키로 뽑아둬 나중에 분리 가능 |
| 월 잠금 해제 권한과 적용 권한 | **다르다.** 잠금 해제는 ADMIN 만, 적용은 ADMIN/OPERATOR. 잠긴 달은 적용이 보류된다 |
| 조회 전용·외부 세무사 노출 | 목록·상세 조회는 전 역할. **원가 값은 diff 에 그대로 보인다** — `can_view_sensitive_diff` 분리는 미구현 |

`can_create / can_submit / can_approve / can_reject / can_cancel / can_apply` 는
`_APPROVE_ROLES` + 소유자 검사로 서비스에 정리돼 있다.
`can_view_sensitive_diff` 는 **미구현 — 남은 위험**.

---

# 승인 거버넌스 강화 (2026-08-06) — M020 + 우회 차단 + 민감정보

## 1. 현재 상품 API 분석 결과

| 항목 | 실제 |
|---|---|
| 목록 | `GET /api/v1/products` (q/status/page) |
| 상세 | `GET /api/v1/products/{id}` |
| 수정 | `PATCH /api/v1/products/{id}` — `ProductUpdate` |
| 지원 필드 | sku, name_ko, name_en, category_path, margin_type, status, supplier_id, supply_url, cost_amount, cost_currency, cost_fx_rate, cost_amount_foreign, has_variants, memo |
| **version / optimistic concurrency** | **없음** — 승인은 `base_version`(스냅샷 해시)으로 대신한다 |
| 원가 상세(중국내배송·해외배송·관세·수입VAT·포장비) | **DB 에 없다.** FE mock 에만 존재 |
| 원가 적용일 / 판매 개시일 / 담당자 | **DB 에 없다** |
| 변경 이력 | 별도 테이블 없음 — `audit_log` 가 담당 |

**없는 컬럼을 임의로 추가하지 않았다.** 원가 세부·적용일·담당자는 `docs/46` 의
스키마 결정이 선행돼야 한다. 현재 승인 대상은 **실재하는 컬럼만**이다.

## 2. 승인 우회 차단 — 가장 중요한 구멍

`PATCH /products/{id}` 가 `cost_amount`·`name_ko`·`supply_url`·`status` 를
**그대로 수정할 수 있었다.** 승인 워크플로를 만들어 놓고 일반 수정 API 를 열어두면
승인은 장식이다.

`update_product()` 진입점에서 승인 대상 필드를 거부한다:

```python
blocked = set(fields) & all_approval_fields("product")
if blocked: raise ApprovalRequiredError(...)   # 403 APPROVAL_REQUIRED
```

- **403** 을 쓴다. 422 로 내면 "값이 잘못됐다" 로 읽혀 사용자가 값을 고치려 든다.
  실제로는 값이 아니라 **경로**가 문제다.
- 섞어 보내면 **전부 거부**한다. 일부만 저장하면 무엇이 반영됐는지 알 수 없다.
- 승인 적용 경로만 `_internal_approval_apply=True` 로 통과한다.
  **이 인자는 라우터에 노출되지 않아 클라이언트가 설정할 수 없다.**

### 어댑터가 raw SQL 대신 서비스를 쓴다
`ProductAdapter.apply_changes` 가 `update_product()` 를 호출한다. raw UPDATE 를 쓰면
환율 자동 채움(`_resolve_fx_rate`) 같은 기존 로직이 빠져 원가가 어긋난다.

### 커밋 소유권 — 테스트가 잡은 회귀
`update_product` 는 원래 마지막에 `db.commit()` 한다. 승인 적용에서 그대로 부르면
**상품 변경이 먼저 커밋되어** 뒤이은 감사 기록 실패 시 롤백되지 않는다.
감사 실패 롤백 테스트가 이 회귀를 즉시 잡았다 → 승인 경로에서는 커밋하지 않는다.
감사 로그도 중복되지 않게 승인 경로에서는 `update_product` 쪽 기록을 생략한다.

## 3. 요청 유형별 필드 소유권

열린 요청 UNIQUE 가 `(org, target_type, target_id, request_type)` 이라
**서로 다른 request_type 은 동시에 열린다.** 두 유형이 같은 필드를 건드리면
나중에 적용되는 쪽이 앞의 승인을 조용히 덮어쓴다.

| request_type | 소유 필드 |
|---|---|
| `PRODUCT_INFO_CHANGE` | `name_ko`, `name_en`, `category_path`, `supply_url`, `supplier_id` |
| `COST_CHANGE` | `cost_amount`, `cost_currency`, `cost_fx_rate`, `cost_amount_foreign` |
| `STATUS_CHANGE` | `status`, `margin_type` |

- **겹치면 import 시점에 `RuntimeError`** (`_assert_no_field_overlap`).
  런타임에 발견하면 이미 두 승인이 같은 필드를 물고 있는 상태다.
- 검증은 2단계: ① 전체 allowlist(아예 승인 대상 아님) ② 유형 소유권(소유자를 알려줌).
  오류 메시지가 달라야 사용자가 무엇을 해야 할지 안다.
- **적용 시점에도 같은 검증**을 한다. DB 를 직접 조작해 다른 유형 필드를 심어도 막힌다.

`memo` 는 **승인 대상이 아니다** — 직접 저장. 운영 영향이 없는 값까지 승인을 태우면
승인함이 잡음으로 가득 차고 정작 중요한 원가 변경이 묻힌다.

### `SERVER_MANAGED_FIELDS` 범위 조정
`status` 를 범용 차단 목록에서 뺐다. 상품 상태는 정당한 승인 대상(`STATUS_CHANGE`)인데
범용 목록에 있으면 그 요청 유형이 통째로 막힌다. 이름이 흔한 필드는
**대상별 allowlist** 가 통제한다.

## 4. 민감정보 — 서버에서 값을 지운다

`app/core/sensitive.py`

- **permission code**: `approval.view_sensitive_diff`. 역할 이름 하드코딩이 아니라
  `PERMISSION_MATRIX` 로 매핑한다.

| 역할 | 민감 diff 조회 | 근거 |
|---|---|---|
| ADMIN | ✓ | 전 권한 |
| ACCOUNTING | ✓ | 원가·마진이 업무 대상 |
| OPERATOR | ✓ | 원가 승인 주체 — 값을 봐야 승인이 성립 |
| SOURCING | ✓ | 매입가를 직접 다룸 |
| READONLY | ✗ | |
| EXTERNAL_ACCOUNTANT | ✗ | 정산 자료는 보되 상품 원가는 볼 이유가 없다 |

- **민감 필드**: `cost_amount`, `cost_amount_foreign`, `cost_fx_rate`, `cost_currency`
  \+ `docs/46` 에서 들어올 원가 세부(purchase_price, customs_duty, import_vat …)를
  **미리 등록**했다. 컬럼이 생기는 순간 자동으로 보호된다.
- **상품명·URL·상태는 민감이 아니다.** 승인자가 판단하려면 봐야 한다.

### redaction 방식
```json
{ "field": "cost_amount", "label": "원가", "sensitive": true, "redacted": true,
  "before": null, "after": null, "message": "권한이 없어 금액을 확인할 수 없습니다." }
```
- **키는 남긴다** — 지우면 "변경 없음" 과 "볼 수 없음" 이 구분되지 않는다.
- `redacted` 와 `before: null` 을 구분한다. 값이 없는 것과 가려진 것은 다른 상태다.
- **목록·상세·모든 상태 전이 응답**이 같은 함수를 거친다. 목록만 가리면 상세 API 로,
  조회만 가리면 승인 버튼 응답으로 뚫린다.

## 5. 반려 통계 보존 — M020 `decision_outcome`

`REJECTED → CANCELLED` 아카이빙을 유지하면 **현재 상태만 세면 반려 건이 취소로 분류된다.**

선택지 A(별도 컬럼) 채택. B(감사 로그 집계)는 쿼리가 복잡하고 보존 정책에 종속되며,
C(`reviewed_*` 재사용)는 `reviewed_at` 이 승인·반려 모두에 채워져 결과를 구분하지 못한다.

```
status           = 지금 어디에 있나   (CANCELLED)
decision_outcome = 무엇으로 판정됐나  (REJECTED)  ← 한 번 정해지면 유지
```

반려 통계는 `decision_outcome` 으로 센다. 테스트가 이를 고정한다 —
`status='REJECTED'` 로 세면 아카이빙된 반려가 누락된다.

## 6. 테스트

### 신규 `test_approval_governance.py` — 31건
우회 차단 6(parametrize) + 직접저장 + 혼합거부 + 정식경로 + 조직격리 +
필드소유권 8 + executor 우회 + 민감정보 11(권한 매트릭스 6 포함) + 반려통계 3

### 발견하고 고친 결함
| # | 결함 | 발견 |
|---|---|---|
| 1 | `PATCH /products` 로 승인 대상 필드 직접 수정 가능 | 코드 분석 |
| 2 | 승인 적용이 `update_product` 커밋 때문에 원자성 상실 | 감사 롤백 테스트 |
| 3 | 감사 로그 중복 기록 (product UPDATE 2줄) | 기존 테스트 |
| 4 | `status` 가 범용 차단 목록에 있어 STATUS_CHANGE 불가 | 신규 테스트 |
| 5 | **테스트 자체의 flakiness** — `hash() % 9` 로 만든 year_month 가 1/9 확률 충돌.
     `PYTHONHASHSEED` 가 매 실행 랜덤이라 간헐 실패(3회 중 1회) | 반복 실행 |

## 7. 검증

```
python -m alembic upgrade head / downgrade M019 / upgrade head   # M020 왕복
python -m pytest app/tests -q -p no:cacheprovider                # 3회 반복
cd frontend && npx tsc --noEmit && npx next build
```

| 항목 | 결과 |
|---|---|
| 백엔드 전체 | **208 passed, 1 xfailed** (3회 연속 동일) |
| M020 up/down/up | 성공 |
| 프론트 tsc / build | 통과 (47 라우트) |


---

# 상품 화면 ↔ 승인 거버넌스 실연결 (2026-08-06)

## 1. 어댑터 상태 — 이번 변경 전/후

| 항목 | 이전 | 이번 변경 | 근거 |
|---|---|---|---|
| product list | fallback | **live** (mock 은 개발 플래그에서만) | 운영에서 BE 장애를 mock 목록으로 덮으면 사용자가 존재하지 않는 상품을 본다 |
| product detail | **mock** | **live** — `GET /products/{id}` | mock 상수를 읽고 있었다. 저장 대상과 화면이 다른 데이터였다 |
| product save | **mock** | **live** — 직접저장 `PATCH` + 승인요청 `POST /approvals` | mock 성공은 "저장됐다" 는 거짓말이다 |
| product options | mock | 그대로 mock | BE 엔드포인트(`/option-groups`, `/variants`)는 있으나 이번 범위 밖 |
| 승인 생성 | live | live | |
| 승인 적용 | live | live | |

`NEXT_PUBLIC_ENABLE_MOCK_FALLBACK=true` 는 **개발 환경에서만** 동작한다.
`NODE_ENV === "production"` 이면 플래그를 켠 채 배포해도 무시된다 —
플래그를 끄는 걸 잊는 사고가 실제로 일어나기 때문이다.

## 2. 동시성 토큰 — 컬럼을 새로 만들지 않았다

`product` 에 `version` 컬럼이 **없다.** 두 가지 길이 있었다.

| 선택지 | 문제 |
|---|---|
| `version` 컬럼 추가 (migration) | 기존 행 backfill + 모든 UPDATE 경로에 증가 로직 |
| **`updated_at` 을 토큰으로** ← 채택 | 마이그레이션 0, 보호 범위 동일 |

같은 상품 행의 **모든 쓰기가 `updated_at` 을 갱신**하므로 보호 범위는 version 컬럼과 같다.
토큰은 ISO 문자열로 주고받는다 — 숫자로 주면 JSON 왕복에서 정밀도가 깎인다.

```
detail  → version_token: "2026-08-06T05:12:33.481Z"
save    → expected_version 로 되돌려줌
불일치  → 409 PRODUCT_VERSION_CONFLICT (detail.current_version 동봉)
```

**충돌이면 아무것도 쓰지 않는다.** 일부만 반영하면 무엇이 저장됐는지 알 수 없다.
`expected_version` 을 안 보내면 검사하지 않는다 — 기존 호출자·스크립트 호환.

테스트가 이걸 고정한다: A 가 읽고 → B 가 저장 → A 가 저장 시 **B 의 값이 살아 있어야** 한다.

## 3. 필드 소유권을 프론트가 들고 있지 않다

`GET /approvals/field-ownership` 을 추가했다. 화면이 표를 상수로 들고 있으면
언젠가 백엔드와 어긋나고, 그때 사용자는 **입력을 다 끝낸 뒤에야** 403 을 본다.

응답에는 `request_types`(유형별 소유 필드) · `approval_fields` · `sensitive_fields` 가 들어간다.
권한 검사를 대신하지 않는다 — **화면 구성용 힌트**고 실제 차단은 서버가 한다.

표를 못 받으면(로딩 실패) **전부 직접 저장으로 보낸다.** 추측으로 승인 요청을 만들면
사용자가 의도하지 않은 요청이 승인함에 쌓인다. 직접 저장은 백엔드가 403 으로 정확히 거절한다.

## 4. 저장 경로 분기

```
draft ─ diffAgainst(원본) ─→ 변경분 ─ classifyProductChanges(소유권표) ─┬─ direct  → PATCH /products/{id}
                                                                      └─ approval → POST /approvals (유형별로 분리)
```

- **승인 요청을 먼저 만든다.** 직접 저장이 먼저 성공한 뒤 승인 생성이 실패하면
  일부만 반영된 상태로 남고, 사용자는 무엇이 저장됐는지 알 수 없다.
- 유형별로 **나눠서** 요청을 만든다. 한 요청에 섞으면 필드 소유권이 깨진다.
- 성공 안내를 **구분**한다. "저장했습니다" 하나로 뭉치면 사용자는 원가까지 바뀐 줄 안다.
  → `일부는 저장하고, N건은 승인 요청으로 올렸습니다`

### `""` 와 `null`
사용자 입장에서 같은 "빈 값"이다. 구분하면 아무것도 안 고쳤는데 승인 요청이 만들어진다.
반면 `undefined`(안 건드림)와 `null`(비움)은 **구분한다** — 안 그러면 값을 지울 수 없다.
`0`/`false` 는 절대 빠뜨리지 않는다 — `if (value)` 로 걸렀다면 원가 0원 변경이 통째로 사라진다.

## 5. 열린 승인 요청을 미리 알린다

상품 상세 응답에 `open_approvals` 요약을 실었다. 없으면 사용자는 저장 버튼을 눌러서야
"이미 열린 요청이 있다"(409)를 알게 된다 — 입력을 다 끝낸 다음에.

요약이지 diff 가 아니다 — **민감값은 싣지 않는다.** 값이 필요하면 승인 상세 API 가
권한에 따라 redaction 을 거쳐 내려준다.

## 6. executor 중복 실행 방지

`FOR UPDATE SKIP LOCKED` 는 **같은 건이 두 번 적용되는 것**만 막는다.
배치가 동시에 여러 개 도는 것 자체는 막지 못한다.

**조직 단위 advisory lock** 채택. `approval_executor_run` 테이블을 쓰지 않은 이유:

| | 문제 |
|---|---|
| 실행 이력 테이블 | 프로세스가 죽으면 RUNNING 행이 남아 **영원히 잠긴다.** heartbeat + stale 회수 로직이 따라붙는다 |
| **advisory lock** ← 채택 | 연결이 끊기면 DB 가 자동으로 푼다. migration 불필요 |

실행 **이력**이 필요해지면 그때 테이블을 추가한다 — 잠금과 별개 관심사다.

**Session 이 아니라 전용 connection 에서** 잡는다. executor 는 요청마다 커밋하고
Session 은 커밋 후 커넥션을 풀에 반납할 수 있다 → 잠금이 실행 도중 풀리거나(방어 실패)
반납된 커넥션에 남는다(잠금 누수). 전용 connection 을 끝까지 쥐면 둘 다 안 일어난다.

실패해도 `finally` 로 푼다 — 없으면 한 번 터진 뒤 영구히 잠긴다. 테스트가 이걸 고정한다.

## 7. 프론트 테스트 — tsc 와 build 로는 안 잡히는 것

Vitest + RTL + jsdom 도입. `tsc` 와 `next build` 는 **타입이 맞는지**만 본다.
원가가 승인 경로로 가는지, 빈 문자열과 null 을 구분하는지는 타입 검사로 절대 안 잡힌다.

| 파일 | 건수 | 무엇을 고정하나 |
|---|---|---|
| `lib/api/products.test.ts` | 20 | 분류 규칙 · diff 경계(`""`/`null`/`0`/`false`/`undefined`) |
| `lib/api/approvals.test.ts` | 14 | 오류 코드별 안내와 재조회 여부 · diff 행 생성 |
| `components/products/product-drawer.test.tsx` | 8 | drawer 가 **실제로** 그 규칙을 쓰는지 |

마지막 파일이 핵심이다. 분류는 정확한데 저장 버튼이 그걸 무시하고 전부 PATCH 로 보내면
순수 함수 테스트는 그대로 통과한다.

### 설정에서 걸린 것
- `vitest.config.ts` → **`.mts`**. `@vitejs/plugin-react` 와 `vite-tsconfig-paths` 가
  ESM 전용이라 CJS 로 로드되면서 config 자체가 안 읽혔다.
- `esbuild: { jsx: "automatic" }`. tsconfig 의 `jsx: "preserve"` 는 Next 빌드용이라
  테스트에서는 classic 으로 떨어져 `import React` 가 없는 컴포넌트마다 터졌다.

## 8. 실수와 복구

`lib/api/products.ts` 가 **이미 존재하는 파일이었다.** 새 파일인 줄 알고 `Write` 로
덮어써서 옵션 그룹·variant API 전체(약 124줄)를 날렸다. `tsc` 가 즉시 잡았고
`git show HEAD:` 로 복구한 뒤, 덮어쓰기가 아니라 **추가**로 다시 작업했다.

교훈: 파일을 만들기 전에 존재 여부를 확인한다. 이 저장소는 git 이 있어서 복구됐다.

## 9. 검증

```
backend : python -m pytest app/tests -q -p no:cacheprovider
frontend: npx vitest run && npx tsc --noEmit && npx next build
```

| 항목 | 결과 |
|---|---|
| 백엔드 | **223 passed, 1 xfailed** (이전 208 → 신규 15) |
| 프론트 단위 테스트 | **42 passed** (3 파일) |
| tsc / next build | 통과 (47 라우트) |

## 10. 이번에 하지 않은 것

| 항목 | 상태 | 이유 |
|---|---|---|
| Playwright E2E | **미구현** | 실 백엔드 기동이 필요한데, 그 준비까지 이번 세션에서 못 했다. mock API 만으로 구성하는 건 금지 사항이라 절반만 만들지 않았다 |
| `productAdapter.options` live | 미구현 | BE 엔드포인트는 존재. 이번 범위(상품 저장·승인)에서 벗어난다 |
| 승인/적용 상태 탭 재정의 | 미구현 | |
| `revise` 흐름 · 승인자 지정 UI | 미구현 | |
| 자동 승인 임계값 | 미확정 | `thresholds_resolved: false` 유지 — 업무 결정이라 임의 확정하지 않는다 |
| 판매가 스키마 migration | **의도적 미실행** | `docs/46` 참조. 선행 업무 결정 3건이 UNIQUE 제약과 테이블 개수를 바꾼다 |

---

# E2E · 옵션 live · 판매가 스키마 (2026-08-06 오후)

## 1. `updated_at` 토큰 검증 — 결함 2건을 찾았다

지난 세션에 "`product` 에 version 컬럼이 없으니 `updated_at` 을 토큰으로 쓴다" 고
결정했다. 그 선택이 **실제로 성립하는지** 검증했고, 성립하지 않는 지점 두 곳을 찾았다.

### 결함 ① 트리거가 `now()` 를 썼다 → M021

`set_updated_at()` 이 `NEW.updated_at = now()` 였다. PostgreSQL 의 `now()` 는
**트랜잭션 시작 시각**이라 한 트랜잭션 안에서 상수다.

```
BEGIN;
  UPDATE product SET memo='1' WHERE ...;   -- updated_at = T
  UPDATE product SET memo='2' WHERE ...;   -- updated_at = T  ← 같다
COMMIT;
```

첫 수정 뒤 발급한 토큰으로 두 번째 수정을 통과시킬 수 있다. 낙관적 잠금이 뚫린다.
→ `clock_timestamp()` 로 교체. 함수 하나만 바꾸면 `updated_at` 을 가진 모든 테이블에 적용된다.

트리거가 애플리케이션 값을 **덮어쓴다**는 점도 확인했다. 오히려 이게 낫다 —
raw SQL 로 상품을 고치는 경로가 `updated_at` 을 빠뜨려도 토큰은 갱신된다.
토큰의 신뢰 근거가 애플리케이션 규율이 아니라 **DB 제약**이 된다.
그래서 `update_product` 의 SET 절에서 `updated_at = :now` 를 뺐다 — 어차피 덮어쓰이는데
남겨두면 "애플리케이션이 정한다" 는 잘못된 인상을 준다.

### 결함 ② 검사와 쓰기 사이에 틈 (TOCTOU)

```
SELECT ... → 토큰 비교 → UPDATE
                ↑ 여기서 다른 커넥션이 커밋하면
```
READ COMMITTED 에서 SELECT 는 옛 값을 봤으므로 검사를 통과하고, UPDATE 가
남의 수정을 그대로 덮어쓴다. **조용한 데이터 손실**이다.

→ `expected_version` 을 UPDATE 의 WHERE 에 넣고 영향 행 0 이면 409.
검사와 쓰기가 한 문장이라 끼어들 틈이 없다.

행이 안 맞은 이유를 다시 조회해 **404 와 409 를 구분**한다 — 삭제된 상품에
"다시 불러오세요" 를 보여주면 사용자는 새로고침하고 나서야 상품이 없는 걸 안다.

사전 SELECT 검사는 남겼다. 방어가 아니라 **한 번의 왕복으로 오류를 알려주기** 위해서다.

### 검증 방법
`test_updated_at_token.py` 7건. TOCTOU 는 `_now()` 를 seam 으로 써서
검사와 UPDATE 사이에 다른 커넥션의 커밋을 강제로 끼워 넣는다 — 경합에 기대지 않는다.

**결론: version 컬럼 도입은 불필요하다.** 위 두 수정으로 `updated_at` 이
안정적인 토큰이 된다.

## 2. Playwright E2E — mock 없이 실제 백엔드

| 요구 | 구현 |
|---|---|
| 운영 DB 금지 | DB 이름이 `_e2e` 로 안 끝나면 스크립트가 즉시 중단 |
| 전용 DB | `picksome_e2e`, `picksome_test` 역할(운영 role 은 CREATEDB 없음) |
| migration head | globalSetup 이 `alembic upgrade head` |
| 결정적 seed | 고정 UUID. 실패 시 어떤 행이 문제인지 로그로 추적 가능 |
| 데이터 정리 | 테스트마다 reseed(idempotent). DB 는 기본 보존 — 실패 원인 조사용 |
| 자동 기동 | `webServer` 로 FastAPI + Next dev |
| 포트 충돌 | `E2E_BACKEND_PORT`/`E2E_FRONTEND_PORT`, 기본 8131/3131 (개발 기본 포트 회피) |
| 실패 보존 | trace / video / screenshot `retain-on-failure`, 서버 stdout/stderr pipe |

**모든 단언이 화면과 DB 를 둘 다 본다.** `e2e/db.ts` 가 백엔드 스크립트를 통해
실제 SELECT 를 돌린다. 화면 문구만 보면 "저장했습니다" 토스트는 뜨는데 DB 는
옛날 값인 상태를 그대로 통과시킨다 — 그게 이 프로젝트에서 실제로 있었던 사고다.

### E2E 가 잡아준 것 (전부 실제 결함)
| # | 발견 |
|---|---|
| 1 | `.local` 도메인은 pydantic `EmailStr` 이 special-use TLD 로 거절한다. seed 는 통과하고 **로그인만** 실패해 원인 찾기가 어렵다 |
| 2 | `page.request` 는 브라우저 밖 클라이언트라 `localStorage` 인증을 안 쓴다 → 인증된 API 컨텍스트를 따로 만든다 |
| 3 | 월 잠금 판정은 `status` 가 아니라 **`close_state`** 컬럼이 한다. 헷갈리면 "잠갔는데 안 잠긴다" |
| 4 | `LOCKED` 은 `confirmed_at`/`confirmed_by` 를 요구한다(`ck_ms_confirmed_meta`). 누가 언제 잠갔는지 없는 잠금은 추적 불가 |
| 5 | 옵션 API URL 이 전부 틀려 있었다(아래 참조) |

### executor 중복 실행 — 경합에 기대지 않는다
요청 두 개를 동시에 던져 겹치기를 기대하면 안 된다. 배치가 비어 있으면 몇 ms 만에
끝나 순서대로 처리되고 **간헐적으로 통과**한다. 가끔 통과하는 테스트는 방어가 아니다.
→ advisory lock 을 별도 프로세스가 확실히 붙잡은 뒤(`LOCKED` 출력 대기) 한 번 던진다.

## 3. 옵션 Adapter live — 기존 클라이언트가 깨져 있었다

`lib/api/products.ts` 를 **덮어쓰지 않고** 경로·타입만 고쳤다(직전 세션 덮어쓰기 사고 재발 방지).

| 발견 | 실제 |
|---|---|
| `PATCH /products/{pid}/option-groups/{gid}` | 실제는 `/products/option-groups/{gid}` → **전부 404** |
| `OptionGroup.values` nested | 서버는 그룹만 준다. 값은 별도 엔드포인트 → 화면이 `undefined.map()` 으로 터졌다 |
| `ProductVariant.stock` | 실제 컬럼은 `stock_quantity` |

### 백엔드 — 옵션 조회가 상품 소유를 확인하지 않았다
다른 조직 상품의 옵션을 요청하면 200 + 빈 배열이었다. 상세 조회는 404 인데 옵션만 200 이면
화면이 "옵션 없는 내 상품" 으로 읽고 추가를 시도한다.
→ `list_option_groups` / `list_variants` 가 `get_product` 로 소유를 먼저 확인한다.

### 일괄 저장 API 가 없다
그래서 **바뀐 행만** 골라 하나씩 보낸다. 전부 보내면 안 바꾼 행까지 `updated_at` 이
갱신되어 변경 이력이 의미를 잃고, 옵션 100개 상품에서 요청이 100번 나간다.

## 4. field ownership 실패 폐쇄

이전에는 `/approvals/field-ownership` 실패 시 null 을 돌려주고 모든 변경을
직접 저장으로 보냈다. **네트워크 장애를 "승인이 필요 없다" 로 해석한 것이다.**

- 어댑터가 오류를 삼키지 않는다. 표 없이 `save()` 를 부르면 거부(2차 방어)
- drawer 가 저장·승인요청 버튼을 비활성화하고 사유 + [다시 불러오기] 표시
- **입력 내용은 유지한다.** 저장을 막는다고 쓴 걸 날리면 두 번 일한다
- 서버 403 은 최종 안전장치로 그대로 둔다

## 5. Adapter 상태

| 기능 | 상태 |
|---|---|
| product list / detail / save | **live** |
| options list (variants) | **live** |
| option group create / update | **live** |
| option value create / update | **live** |
| variant create / update | **live** |
| variant disable | **live** (물리 삭제 아님) |
| variant bulk update | **미지원** — 백엔드에 일괄 API 없음. 변경 행만 순차 전송 |
| 옵션 customName / sourceOptionUrl / marketplaceOptionNo | **미지원** — DB 컬럼 없음. 입력 비활성 + 사유 표시 |
| 판매처 가격 조회/저장 | **미구현** (스키마만) |
| 판매처 publish | **미연결** |

## 6. 검증

```
backend : python -m pytest app/tests -q -p no:cacheprovider     # 3회 반복
frontend: npx vitest run && npx tsc --noEmit
e2e     : npx playwright test                                    # 실제 BE + 전용 DB
migration: alembic upgrade head / downgrade / upgrade            # M021, M022
```

| 항목 | 결과 |
|---|---|
| 백엔드 | **257 passed, 1 xfailed** (3회 연속 동일) |
| 프론트 Vitest | **54 passed** (4 파일) |
| Playwright E2E | **12 passed** (실제 FastAPI + PostgreSQL) |
| M021 / M022 up·down·up | 성공 |
| tsc / next build | 통과 |

### 알려진 불안정 1건
`test_every_service_update_path_changes_the_token` 이 전체 실행 중 **1회 ERROR**
(FAILED 아님 — 픽스처 단계)를 냈다. 이후 전체 실행 3회 연속 재현되지 않았고
단독 실행·통합 폴더 실행에서도 재현되지 않는다. **원인 미규명이라 해결됐다고 하지 않는다.**
다시 나오면 픽스처의 engine/connection 정리 순서를 먼저 본다.

---

# 판매처 가격 운영 흐름 완성 (2026-08-06 저녁)

## 1. updated_at 정밀도·단조성 — 측정하고 보강했다

### 문자열 정밀도
프론트는 토큰을 **문자열 그대로** 보관해 되돌려준다. 코드 전수 확인 결과
`Date` 변환 경로가 없다(`versionToken: string | null`, 표시용 `updatedAt` 과 분리).

테스트로 계약을 고정했다.
- 마이크로초 포함 토큰의 왕복 성공
- **밀리초로 잘린 토큰은 거절** — JS `new Date().toISOString()` 을 거치면
  마이크로초가 잘린다. 그게 통과하면 낙관적 잠금이 밀리초 해상도로 내려앉는다

### 단조 증가 (M023)
측정: `clock_timestamp()` 해상도 약 63µs. 왕복 없는 서버측 연속 UPDATE 2,000회에서
고유 2,000개·단조 위반 0건. **측정은 통과했다.**

그래도 보강했다. 남는 위험이 둘 있고, 낙관적 잠금 전체가 이 값에 걸려 있는데
근거가 "측정해 보니 괜찮더라" 인 건 약하다.

```sql
NEW.updated_at = GREATEST(clock_timestamp(), OLD.updated_at + interval '1 microsecond')
```

`updated_at` 을 1시간 미래로 밀어 "시계가 뒤로 간" 상황을 만들어 검증했다 —
M023 이전에는 토큰이 1시간 **작아졌고**, 이후에는 정확히 1µs 증가한다.

**version 컬럼 도입은 불필요하다.**

### nil UUID (M023)
`variant_scope` 가 variant NULL 을 nil UUID 로 접는다. 실제 variant 가 그 id 를
가지면 두 가격이 같은 scope 로 취급된다. `ck_pv_id_not_nil` 로 고정하고,
migration 이 기존 데이터도 확인해 있으면 중단한다.

## 2. 불안정 테스트 — **원인을 찾았다**

`test_every_service_update_path_changes_the_token` 의 간헐적 fixture ERROR.

| 조사 | 결과 |
|---|---|
| 대상 테스트 단독 30회 | 30/30 통과 |
| 파일 전체 | 통과 |
| 전체 suite | 재현 (약 5회 중 1회) |
| 테스트 DB 상태 확인 | **누적 상품 2,661건** |

원인: `business_key = f"PROD-{uuid.hex[:6]}"` — 6자리 hex(16.7M)에
UNIQUE(org_id, business_key). 생일 문제로 삽입당 약 0.016%, 전체 실행당 약 1.6%.
관측 빈도와 일치하고, 행이 쌓일수록 확률이 올라간다.

→ hex 를 자르지 않는다. PK 와 같은 값이라 충돌이 **구조적으로 불가능**하다.
같은 패턴을 쓰던 테스트 파일 8개를 모두 고쳤다. 이후 전체 실행 반복에서 재현 없음.

`hash()` 기반 식별자는 이 저장소에 없다(이전 세션에서 이미 제거).

## 3. 가격 상태 모델 — 세 축을 합치지 않는다

```
승인 상태      approval_request.status        ← 승인의 단일 진실
내부 적용 상태  가격 버전의 effective 기간      ← 적용 중 / 적용 예정 / 종료
외부 반영 상태  product_channel_price.publish_status
```

가격 레코드에 승인 상태를 **중복 저장하지 않는다.** 두 곳에 두면 언젠가 어긋난다.
가격 행의 `status`(ACTIVE/SUPERSEDED/CANCELLED)는 "이 버전이 유효한가" 만 담고,
"지금 적용 중인가" 는 기간이 정한다.

### 작업 중 고친 것
기간을 닫으면서 `status='SUPERSEDED'` 로 바꿨더니, 종료일은 있지만 아직 유효한
구간에서 `resolve_price` 가 "가격 없음" 을 돌려줬다. 종료일이 생겼다고 그 가격이
무효해진 게 아니다 — 새 가격이 시작하기 전까지는 여전히 팔리는 가격이다.

## 4. 가격 resolution 규칙 (서버 소유)

```
① variant 의 유효한 ACTIVE 가격
② 없으면 product_variant_id IS NULL 인 기본 가격
③ 할인 기간 안이면 sale_price, 아니면 regular_price
④ 둘 다 없으면 null (미입력) — 0 이 아니다
```

응답에 `variant_fallback`, `source_price_id`, `on_sale` 을 함께 준다.
**화면이 이 계산을 다시 하지 않는다** — 두 군데 있으면 언젠가 갈린다.

## 5. CHANNEL_PRICE_CHANGE

| 항목 | 결정 |
|---|---|
| target_type | `product_channel_listing` |
| 왜 product 가 아닌가 | 열린 요청 UNIQUE 가 (org, target, request_type) 이라 판매처가 둘이면 한쪽만 요청 가능 |
| 왜 price 가 아닌가 | 아직 없는 행을 대상으로 못 해서 **신규 가격 등록을 표현 불가** |
| 적용 결과 | 기존 UPDATE 가 아니라 **새 버전 INSERT** + 앞 버전 기간 닫기 |
| 적용일 단일 진실 | **가격 payload 의 effective_from**. 월 잠금은 가격이 적용되는 달로 판정 |
| payload 검증 | `ChannelPriceIn` 으로 요청 시점에 검증 — JSONB 를 임의 저장소로 쓰지 않는다 |
| 추적 | 가격 버전에 `approval_request_id` 를 남긴다 (approval.apply 가 어댑터로 전달) |

민감정보: `regular_price`·`sale_price` + 마진 영향값 5종을 등록했다.
원가를 가리면서 판매가를 열어두면 마진이 역산돼 가린 의미가 없다.

## 6. outbox (M024)

`integration_job` 은 cron **스케줄 정의**(schedule_cron, is_enabled)라 재사용하지 않았다.

- 승인 적용과 **같은 트랜잭션**에서 적재. 외부 API 는 부르지 않는다 —
  느리면 승인 트랜잭션이 잠금을 쥐고, 실패하면 승인이 통째로 롤백된다
- `FOR UPDATE SKIP LOCKED` + **lease**. 워커가 죽으면 lease 만료 후 자동 회수 —
  SKIP LOCKED 만으로는 PROCESSING 이 영원히 남는다
- 활성 작업 부분 UNIQUE. 중복 요청은 오류가 아니라 **무시**한다(버튼 두 번 눌렀다고
  승인이 실패하면 안 된다)
- runner 중복 실행은 advisory lock, **개별 작업 선점은 row 상태와 lease** 가 한다

## 7. connector — 전부 미연결

레지스트리가 지원 여부를 정직하게 답한다(`GET /channel-listings/connector-support`).

**성공 mock 을 만들지 않았다.** 구체적으로:
- `auto_publish_price` 는 connector 없는 계정에서 켤 수 없다 (생성·수정 양쪽)
- 수동 반영도 큐에 넣지 않고 403 — 넣으면 "처리 중" 처럼 보인다
- runner 는 `CONNECTOR_UNAVAILABLE` 로 끝내고 **재시도하지 않는다.**
  기다린다고 해결되지 않고, 실패 카운트만 부풀어 진짜 오류가 묻힌다
- 화면은 "미요청" 이 아니라 **"미연결"** 로 표시하고 사유는 서버 문장을 그대로 쓴다

## 8. 주문 snapshot — 이번에도 연결하지 않았다

`sales_order.sale_price` 가 주문 시점 값을 박제하므로 **과거 손익 불변은 이미 보장된다.**
가격을 바꿔도 과거 주문 손익은 변하지 않는다.

`product_channel_listing_id` / `product_channel_price_id` 를 nullable 로 추가하는 것은
**보류**했다. 판단 근거:

| 기준 | 현재 |
|---|---|
| 주문 수집 코드가 listing 을 식별하는가 | **아니다.** `sales_order.marketplace` 는 문자열이고 계정 정보가 없다 |
| 외부 상품번호·옵션번호가 있는가 | 주문에는 없다. listing 에만 있다 |
| 잘못 매칭될 위험 | 같은 판매처에 계정이 둘이면 **어느 스토어 주문인지 알 수 없다** |

자동 추측 매칭은 금지 사항이라 추가하지 않았다. 컬럼만 넣으면 전부 NULL 로 남고
채울 코드가 없으므로, 주문 수집이 계정을 식별하게 되는 시점에 함께 간다.

## 9. M025 — FK 의미 수정

`product_channel_price → approval_request` 가 RESTRICT 라 승인 요청 정리를 가격이 막았다.
가격 이력을 먼저 지워야 하는 건 순서가 거꾸로다 → `ON DELETE SET NULL`.
가격 버전은 기록이고 승인 링크는 출처다. 출처를 잃는 게 이력을 잃는 것보다 낫다.

## 10. E2E 가 잡은 실제 결함

중복 `variant_key` 가 **500** 을 반환했다. `uq_variant_product_key` 위반을 번역하지
않아서다. 사용자는 "서버 오류" 를 보고 다시 시도하는데 몇 번을 해도 같은 결과다.
→ 409 + "같은 옵션 조합이 이미 있습니다".

전체 E2E 를 순서대로 돌렸을 때만 드러났다(단일 파일 실행은 통과) — 테스트 파일
사이의 데이터 누적이 실제 사용 패턴을 재현한 셈이다.

## 11. 검증

| 항목 | 결과 |
|---|---|
| 백엔드 | **302 passed, 1 xfailed** |
| Vitest | **75 passed** (5 파일) |
| Playwright E2E (실제 FastAPI + PostgreSQL) | **23 passed** (2회 연속) |
| M021~M025 up·down·up | 성공 |
| tsc / lint / next build | 통과 |

## 12. Adapter · 기능 상태

| 기능 | 상태 |
|---|---|
| product list / detail / save | live |
| options (group·value·variant CRUD, disable) | live |
| variant bulk update | **미지원** (백엔드 일괄 API 없음) |
| listing 생성·조회·수정·비활성화 | **live** |
| 가격 이력 조회 | **live** |
| 가격 resolution | **live** |
| CHANNEL_PRICE_CHANGE 승인 | **live** |
| publish outbox 적재·runner | **live** |
| 쿠팡·네이버 가격 API connector | **미연결** |
| 반영 후 재조회 검증 | **미구현** (`VERIFICATION_FAILED` 상태값만 예약) |
| 마진 영향 미리보기 | **미구현** |
| 옵션 순차 저장 부분 성공 UX | **미구현** (전건 성공/실패만) |
| 주문 ↔ 가격 버전 연결 | **미구현** (판단 근거는 §8) |

---

# 가격 UX 마무리 + 사업비용 승인 (2026-08-06 심야)

## 1. 사업비용 도메인 분석

테이블은 **`expenses`**(복수형). 승인 관련 컬럼은 없다.

| 필드 | 현재 DB | 현재 UI | 민감 | 직접 수정 | 승인 대상 |
|---|---|---|---|---|---|
| `gross_amount` / `supply_amount` / `vat_amount` | ○ | ○ | **○** | ✗ | FINANCIAL |
| `vat_inclusion_type` / `tax_type` / `is_manual_tax_override` | ○ | ○ | | ✗ | FINANCIAL |
| `expense_date` (귀속월 파생) | ○ | ○ | | ✗ | FINANCIAL |
| `category_id` | ○ | ○ | | ✗ | FINANCIAL |
| `payment_method` / `payment_card_holder_id` / `is_personal_card` | ○ | ○ | **○** | ✗ | FINANCIAL |
| `is_business_use` | ○ | ○ | | ✗ | FINANCIAL |
| `is_tax_deductible` / `is_settlement_deductible` | ○ | ○ | | ✗ | FINANCIAL |
| `attributable_partner_id` / `supplier_id` | ○ | ○ | **○** | ✗ | FINANCIAL |
| `description` | ○ | ○ | | ✗ | FINANCIAL |
| `evidence_type` / `evidence_status` | ○ | ○ | | ✗ | EVIDENCE |
| `memo` | ○ | ○ | | **○** | — |
| `linked_settlement_id` | ○ | ✗ | | ✗ | **승인 대상 아님**(시스템 관리) |

**귀속월 컬럼은 없다.** `expense_date` 에서 파생한다 — 별도 컬럼을 만들면 두 값이 어긋난다.
**`approval_status` 컬럼도 만들지 않았다.** `approval_request` 가 승인의 단일 진실이다.

## 2. 승인 우회 차단 — 상품에서 겪은 구멍의 재발

`PATCH /expenses/{id}` 가 금액·귀속일·공제여부를 **그대로 바꿀 수 있었다.**
비용 금액은 순이익의 입력값이고 순이익이 곧 파트너 수익배분이다.

`update_expense()` 진입점에서 403(`APPROVAL_REQUIRED`). 섞어 보내면 전부 거부한다.

### 커밋 소유권 — 또 같은 회귀
`update_expense` 가 마지막에 커밋해서, 승인 적용 중 감사 로그가 실패해도 비용 변경만
남았다. 상품에서 겪은 것과 **똑같은 문제**다. 원자성 테스트가 다시 잡았다.
→ 승인 경로에서는 커밋하지 않고 감사 로그도 중복 기록하지 않는다.

## 3. request type — 세분화하지 않았다

| 유형 | 소유 필드 |
|---|---|
| `EXPENSE_FINANCIAL_CHANGE` | 금액 3종 · 세무 3종 · 발생일 · 카테고리 · 결제 3종 · 업무용 · 공제 2종 · 귀속파트너 · 거래처 · 내용 |
| `EXPENSE_EVIDENCE_CHANGE` | `evidence_type`, `evidence_status` |

제안받은 `EXPENSE_CREATE` / `EXPENSE_VOID` / `EXPENSE_REIMBURSEMENT` 는 **만들지 않았다.**
- `EXPENSE_CREATE`: 대상이 아직 없는 요청을 표현할 target 이 없다(승인 프레임워크가
  `target_id` 를 요구한다). 생성 승인은 별도 설계가 필요하다
- `EXPENSE_VOID`: `void_reason`·`void_effective_month` 컬럼이 스키마에 없다.
  없는 컬럼을 승인 대상으로 열면 적용 단계에서 조용히 실패한다
- `EXPENSE_REIMBURSEMENT`: 상환 상태 컬럼도 지급 장부도 없다

## 4. 월 잠금 — 승인으로도 우회되지 않는다

승인 요청은 만들 수 있지만 **적용에서 막힌다.** 승인 사실은 유효하고, 사람이
조정으로 처리할지 월을 재오픈할지 결정한다.

`get_effective_date` 가 **원래 날짜와 바뀐 날짜 중 더 이른 쪽**을 돌려준다.
7월 비용을 8월로 옮기면 7월 합계가 줄어드는데, 바뀐 날짜만 보면 통과해 버린다.

## 5. 조정 비용 모델 (M026)

```
7월 광고비가 10만원 적게 잡혔다
  → 7월 비용을 고치지 않는다
  → 8월(열린 달)에 +10만원 조정 비용을 만들고 7월 원본을 가리킨다
  → 7월 확정 순이익·배분은 그대로, 8월 순이익에 차액이 반영된다
```

| 결정 | 이유 |
|---|---|
| `expenses` self-reference 가 아니라 **별도 테이블** | nullable 5개를 더하면 일반 비용 행 대부분이 NULL 이 되고 "이게 조정인가" 를 매번 판단해야 한다 |
| 조정이 **실제 비용 행을 만든다** | 월별 집계·세무 리포트가 자동으로 포함한다. 집계 쿼리마다 "조정도 더해라" 를 넣으면 언젠가 빠뜨린다 |
| 조정도 잠긴 달에는 못 넣는다 | 넣을 수 있으면 "조정을 만들면 된다" 로 잠금이 우회된다 |
| 사유 필수, 0 원 거부 | 사유 없는 조정은 나중에 아무도 설명하지 못한다 |
| 금액은 절대값, 차액은 `amount_delta` | `expenses.gross_amount` 에 CHECK(>=0)가 있고, 음수 비용은 집계에서 "환급" 인지 "오류" 인지 구분되지 않는다 |

**월 재오픈 승인은 이번 범위가 아니다.** 잠긴 달 직접 변경 차단 + 조정 경로 +
재오픈이 필요한 상황 안내까지만 구현했다.

## 6. 손익 재계산 규칙

`period_recalc_flag` — **표시만 남긴다.**

승인 트랜잭션 안에서 전체 재집계를 하지 않는 이유:
- 느리다. 수천 건인 달을 다시 도는 동안 승인 트랜잭션이 잠금을 쥔다
- 실패하면 **승인이 통째로 롤백된다.** 집계 버그가 정당한 승인을 막는다
- 커밋 후에 돌리면 커밋과 재계산 사이에 프로세스가 죽었을 때 표시가 유실된다

| 월 상태 | 처리 |
|---|---|
| OPEN | 비용 적용 + 그 달 dirty 표시 |
| REVIEW | 비용 적용 + dirty 표시 (마감 검증 재실행 대상) |
| CONFIRMED / LOCKED | **비용 변경 자체가 막힌다.** dirty 표시도 하지 않는다 |

확정된 달을 표시하지 않는 이유: 표시해 두면 **언젠가 배치가 실행해 확정 숫자를 덮어쓴다.**
날짜 변경은 두 달 모두 표시한다 — 이전 달 합계도 줄어들기 때문이다.

## 7. 개인카드 상환 — 구조만

| 항목 | 상태 |
|---|---|
| 개인카드 비용 표시 | 구현 (`is_personal_card`) |
| 상환 필요 금액·대상자 | 구현 (`reimbursementOf`) |
| 상환 상태 6종 정의 | 구현 (타입·라벨) |
| 상환 승인 요청 | **미구현** — 상태 컬럼이 없다 |
| 실제 지급 장부 | **미구현** |

**`PAID` 를 만들지 않는다.** 지급됐다고 표시할 근거가 없는데 상태를 내면 거짓말이 된다.
현재 나오는 값은 `NOT_REQUIRED` 와 `PENDING` 뿐이다.
비용 승인과 상환 완료를 **같은 상태로 합치지 않았다** — 합치면 승인이 곧 지급이 된다.

## 8. 가격 영역 잔여 UX

| 항목 | 상태 |
|---|---|
| Listing 생성 UI | **구현** (empty state + dialog, connector 미지원 시 자동반영 차단) |
| 마진 영향 미리보기 | **구현** (기존 엔진 재사용, 권한별 redaction) |
| 옵션 부분 성공 UX | **구현** (행별 상태, 실패만 재시도, 부분 성공 warning) |
| listing 외부번호·계정 변경 | **의도적 미지원** — 기존 가격·승인·outbox 연결을 훼손한다 |

## 9. 검증

| 항목 | 결과 |
|---|---|
| 백엔드 | **344 passed, 1 xfailed** (신규 42) |
| Vitest | **109 passed** (7 파일) |
| Playwright E2E (실제 FastAPI + PostgreSQL) | **34 passed** |
| M026 up·down·up | 성공 |
| tsc / lint / build | 통과 |

## 10. 남은 미구현

| 항목 | 상태 |
|---|---|
| 비용 승인 **화면** | **미구현** — API·분류 로직·라벨은 완성, 화면 미배선 |
| 증빙 첨부 추가·삭제 흐름 | **미구현** — `expense_attachment` 테이블은 있으나 파일 스토리지 미연결 |
| EXPENSE_CREATE / VOID / REIMBURSEMENT | **미구현** (§3 근거) |
| 월 재오픈 승인 | **범위 밖** |
| 쿠팡·네이버 가격 connector | **미연결** |
| 주문 ↔ 가격 버전 연결 | 보류 (판단 근거는 이전 절) |

---

# 사업비용 화면 배선 (2026-08-07)

**출발점**: 앞 절에서 비용 승인 API·분류 로직·라벨을 다 만들었지만 **화면이 붙지 않았다.**
그 상태의 `/expenses` 는 `expenseAdapter`(mock `MOCK_EXPENSES` 배열) 위에 있었다 —
금액을 고치고 저장을 누르면 "저장했습니다" 가 뜨지만 DB 는 그대로였다.
비용은 순이익의 입력값이고 순이익이 곧 파트너 수익배분이라, 이 괴리는
**잘못된 배분**으로 이어진다.

## 1. 목록 API — 이름을 서버가 조인해서 준다

`expenses` 의 `category_id`·`payment_card_holder_id`·`attributable_partner_id`·`supplier_id`
는 전부 UUID 다. 화면이 UUID 를 사람에게 보여줄 수는 없다(D-283).

| 방법 | 문제 |
|---|---|
| 행마다 이름 조회 | 한 페이지에 N+1 |
| 파트너 목록을 통째로 받아 프론트에서 붙이기 | **삭제된 파트너**의 과거 비용이 이름 없이 뜬다 |
| **`_SELECT` 에 LEFT JOIN** (채택) | 그 행이 실제로 가리키는 값을 그대로 보여준다 |

`ExpenseOut` 에 `category_name`·`card_holder_name`·`attributable_partner_name`·`supplier_name`
4개를 **표시 전용**으로 추가했다. 저장 입력이 아니다 — 이름으로 수정 요청을 받으면
동명이인에서 갈린다.

⚠️ 조인 이후 `WHERE` 절 컬럼은 반드시 `e.` 로 한정해야 한다. `org_id` 가 조인된
세 테이블에 모두 있어 한정하지 않으면 ambiguous 다.

**필터 3종 추가** — `q`(내용·메모 부분일치), `payment_method`, `payment_card_holder_id`.
화면의 필터바가 이 파라미터들을 이미 쓰고 있었는데 서버가 안 읽어서, **필터를 걸어도
전체가 나왔다.** FastAPI 는 모르는 쿼리 파라미터를 조용히 무시한다 — 사용자는 자기가
지정한 조건이 반영됐다고 믿는다.

금액은 검색 대상에서 뺐다. `"12000"` 이 메모의 일부와도 걸려 기대와 어긋난다.

## 2. `GET /partners` — 없어서 결제자를 고를 수 없었다

`payment_card_holder_id` 는 `partner` 를 가리키는데 **파트너 목록 엔드포인트가
어디에도 없었다.** `/admin/users` 는 ADMIN 전용이라 ACCOUNTING 이 비용을 입력할 때 403 이고,
애초에 `app_user` 와 `partner` 는 다르다 — 파트너는 **수익을 배분받는 주체**고
로그인 계정이 없을 수도 있다(`linked_user_id` nullable).

**읽기 전용으로만 열었다.** 파트너 추가는 `partner_share_policy` 와 함께 다뤄야 한다 —
배분 비율 없는 파트너가 생기면 정산이 합계 1.0 을 못 채운다.

`include_inactive` 를 두었다: 그만둔 파트너가 결제한 작년 비용의 결제자 이름이
사라지면 상환 대상이 누구였는지 알 수 없다. 선택 목록에는 활성만 쓴다.

## 3. 라벨 등록 — 승인함이 대상을 가리지 않는다

승인함은 상품·가격·비용 요청을 **한 목록에 섞어** 보여준다. 그런데 라벨표를
`app/(app)/approvals/page.tsx` 가 자체 상수로 들고 있어서, 비용 요청은
`EXPENSE_FINANCIAL_CHANGE` 라는 날것으로 떴다.

라벨은 **대상별 파일이 소유하고 `lib/api/approvals` 가 합친다**로 바꿨다.

| 등록물 | 내용 |
|---|---|
| `REQUEST_TYPE_LABELS` | product 3종 + `CHANNEL_PRICE_CHANGE` + expense 2종 |
| `TARGET_TYPE_LABELS` | product / listing / expense (화면마다 들고 있던 것을 회수) |
| `FIELD_LABELS_BY_TARGET` | listing·expense 추가 |
| `VALUE_LABELS` | enum 값 한글표 — 입력 폼과 승인 diff 가 **같은 표**를 쓴다 |

`buildDiffRows` 를 세 가지로 보강했다.
- **money/date 확장** — 비용 3종 금액과 `expense_date` 를 원화·날짜로 표시
- **`kind: "ref"` + `resolve`** — 참조 UUID 를 이름으로. 못 찾으면 `"다른 항목"`,
  절대 UUID 를 그대로 노출하지 않는다
- **boolean·enum 표시** — 승인자에게 `true` / `INCLUDED` 를 보여주면 읽을 수 없다

`useExpenseRefs()` 훅이 파트너·카테고리를 한 캐시 키로 받아 목록과 승인 상세가
**같은 이름**을 보게 한다.

## 4. 화면 — 두 가지 규칙이 뼈대다

**① 금액은 직접 저장되지 않는다.** 승인 대상 필드는 서버가 알려주고
(`/approvals/field-ownership`), 화면은 저장을 누르기 **전에** 무엇이 승인으로 가는지
보여준다. 표를 못 받으면 저장을 막는다(fail-closed) — 추측으로 보내면 403 이다.

**② 잠긴 달은 편집 자체를 막는다.** 눌러서 409 를 맞은 뒤 "조정을 만드세요" 를 읽으면
이미 입력을 다 끝낸 뒤다. 열 때 `lock-state` 로 확인하고, 안내에 **다음 행동**
(정정 조정 만들기)을 같이 준다.

`saveExpenseWithApproval` 은 **승인 요청을 먼저** 만들고 직접 저장을 나중에 한다.
반대로 하면 승인 생성이 실패했을 때 메모만 바뀐 상태로 남고 사용자는 무엇이
저장됐는지 알 수 없다. 별도 파일인 이유는 순환 참조다 —
`approvals.ts` 가 `expenses-approval.ts` 의 라벨을 쓰기 때문에 저장 로직을
후자에 두면 모듈 초기화 순서에 따라 라벨이 `undefined` 가 된다.

**첨부 UI 는 걷어냈다.** 기존 것은 브라우저 메모리에만 올라가는 가짜였고,
실데이터 위에서 가짜 업로드는 "증빙을 붙였다" 는 착각을 만든다. 서버 스토리지가
붙기 전까지는 DB 에 실재하는 `evidence_type`·`evidence_status` 만 다룬다.

## 5. E2E 가 잡은 실제 결함 2건

둘 다 **화면 코드의 결함**이고, 단위 테스트로는 잡히지 않는 종류다.

### ① 열자마자 생기는 가짜 변경분

`VatInclusionToggle` 의 자동 분리 effect 가 **비용을 여는 순간** 돌아서 저장된
공급가액·부가세를 화면 기본 세율로 덮어썼다. 그 값이 그대로 변경분으로 잡혀
아무것도 안 건드렸는데 버튼이 "승인 요청" 이 됐다 — 메모만 고치려던 사람이
**금액 변경 승인을 올리게 된다.**

저장된 값의 주인은 서버다(저장 시점 정책으로 박제). 화면이 덮어쓸 근거가 없다.

- "첫 렌더면 건너뛴다" 로는 **부족했다.** 개발 모드 StrictMode 가 effect 를 두 번
  돌리는데 ref 는 살아 있어서 두 번째 실행이 곧바로 덮어썼다 →
  마운트 여부가 아니라 **직전 값과의 차이**를 본다.
- 드로어도 고쳤다. 빈 draft 로 먼저 그리고 다음 렌더에 값이 들어오면, 그 "값이 바뀜"
  을 사용자 입력으로 착각한다 → **draft 가 이 행의 값으로 채워진 뒤에만** 폼을 그린다.

### ② 열자마자 입력하면 지워지는 조정 다이얼로그

`open` 이 바뀔 때 필드를 비우는 effect 가 있어서, 빠르게 입력하면 뒤늦게 도는
초기화가 값을 지웠다(입력은 다 했는데 버튼이 계속 비활성). 초기화 effect 를 없애고
**열려 있을 때만 마운트**한다 — 매번 새 상태로 시작하니 경합 자체가 없다.

## 6. 검증

| 항목 | 결과 |
|---|---|
| 백엔드 유닛 | **54 passed, 1 xfailed** |
| Vitest | **121 passed** (8 파일, 신규 12) |
| Playwright E2E (실제 FastAPI + PostgreSQL) | **41 passed** (신규 `expense-screen.spec.ts` 7건) |
| 신규 E2E 재실행(`--repeat-each=2`) | 14 passed — 경합 재발 없음 |
| tsc / lint / build | 통과 (47 라우트) |

E2E seed 에 `partner` 1건을 추가하고 두 비용의 결제자로 걸었다. 없으면 조인 열과
결제자 선택이 **빈 채로만** 검증된다.

## 7. 남은 미구현 (갱신)

| 항목 | 상태 |
|---|---|
| ~~비용 승인 화면~~ | **완료** — 목록·드로어·승인 분류·잠금 안내·조정 다이얼로그 |
| 증빙 첨부 추가·삭제 흐름 | **미구현** — 파일 스토리지 미연결 (가짜 업로드 UI 는 제거) |
| 거래처(`supplier_id`) 선택 | **미구현** — `external_partner` 목록 API 가 없다. 이름 표시만 |
| EXPENSE_CREATE / VOID / REIMBURSEMENT | **미구현** (근거는 앞 절 §3) |
| 개인카드 상환 지급 장부 | **미구현** — 화면은 `PENDING` 까지만 말한다 |
| 월 재오픈 승인 | **범위 밖** |
| 쿠팡·네이버 가격 connector | **미연결** |

---

# 증빙 파일 실저장 + 거래처 선택 (2026-08-07 오후)

## 1. `expense_attachment` 는 껍데기였다

M001 부터 테이블은 있었지만 `storage_path`·`file_hash` 컬럼만 있고 **어디에서도
쓰이지 않았다.** 화면은 파일을 브라우저 메모리(objectURL)에만 올렸고 새로고침하면
사라졌다. 앞 절에서 그 가짜 UI 를 제거했고, 이번에 실제 저장을 붙였다.

**M027 이 더한 것**

| 컬럼 | 왜 |
|---|---|
| `org_id` | 파일은 조직 경계를 넘으면 안 되는 자산이다. 비용을 조인해야만 조직을 아는 구조면 권한 검사가 매번 조인에 의존한다 |
| `storage_key` | 저장소 내부 키. **API 응답에 싣지 않는다** |
| `status` | `PENDING_SCAN/AVAILABLE/PENDING_DELETE/DELETED/FAILED` |
| `scan_status` | 기본값 **`SCAN_UNAVAILABLE`**. 스캐너가 없다 |
| `delete_attempt_count`·`delete_next_attempt_at`·`delete_last_error` | 삭제 재시도 |

`external_partner.is_active` 도 같이 추가했다 — 거래를 끊은 거래처를 선택 목록에서
빼려면 행을 지우는 수밖에 없었고, 지우면 **과거 비용의 거래처 이름이 사라진다.**

## 2. Storage 추상화 — 경로를 바깥에 노출하지 않는다

```python
class ExpenseAttachmentStorage(ABC):
    save / open / delete / exists
```

`LocalFilesystemStorage` 하나만 구현했다. S3 로 갈 때 `get_storage()` 안에서만
분기하면 되고 호출부는 그대로다.

**키 구조**: `{org_id}/{expense_id}/{attachment_id}.{ext}` —
사용자 입력이 **한 글자도** 들어가지 않는다. 원본 파일명은 DB 컬럼에만 있고
표시에만 쓴다. 그래도 실제 파일을 만지는 마지막 지점(`_path()`)에서
root 밖 경로를 한 번 더 거부한다 — 키를 우리가 만든다는 가정은 언젠가 깨진다.

## 3. 검증 — 확장자·MIME·**내용** 셋 다 본다

확장자만 보면 `.pdf` 로 이름 붙인 실행파일이 통과하고, MIME 만 보면 클라이언트가
헤더를 조작한다. 그래서 magic byte 까지 본다.

```
evil.pdf + application/pdf + PNG 내용  →  422 CONTENT_MISMATCH
```

SHA-256 해시로 같은 비용에 같은 파일이 두 번 붙는 것을 막는다(409 + 기존 id 안내).
**살아 있는 행만** 대상이다 — 지운 뒤 다시 올리는 건 정상 작업이다.

## 4. 순서 — storage 와 DB 가 어긋나지 않게

```
업로드:  검증 → storage write → DB INSERT → evidence_status → 감사
         DB 실패 시 방금 쓴 파일 삭제(orphan 정리)
         storage 실패 시 DB 행을 만들지 않는다

삭제:    상태만 PENDING_DELETE → (커밋) → worker 가 실제 삭제 → DELETED
         storage 삭제 실패 시 PENDING_DELETE 유지 + 백오프 재시도
```

**승인 적용 트랜잭션에서 파일을 지우지 않는다.** 그 트랜잭션이 뒤에 롤백되면
파일만 사라지고 되돌릴 방법이 없다.

## 5. outbox 재사용 검토 결과 — **테이블은 재사용하지 않는다**

| | `channel_price_publish_outbox` (M024) | 첨부 삭제 |
|---|---|---|
| 대상:작업 | 1:N (한 가격이 여러 계정으로) | **1:1** (한 첨부에 삭제는 하나) |
| 작업 수명 | 대상과 별개(시도 이력이 쌓임) | 대상의 수명과 같음 |
| 실패 성격 | connector 없음 = 기다려도 안 됨 | 재시도가 의미 있음 |

1:1 관계를 별도 테이블로 쪼개면 조회마다 조인이 붙고 "행은 있는데 작업이 없는"
불일치 상태가 새로 생긴다. 그래서 **행 자체가 큐**다.
**재시도·백오프 패턴은 M024 와 같게** 유지했다(`_DELETE_BACKOFF_MINUTES`).

## 6. 삭제 승인 — 별도 target_type 인 이유

```
target_type   expense_attachment
request_type  EXPENSE_EVIDENCE_CHANGE   (증빙 상태 변경과 같은 유형)
소유 필드      status  (값은 PENDING_DELETE 만)
```

`expense` 대상에 "attachment_id" 같은 가짜 필드를 만들면, 한 비용에 첨부가
여러 개일 때 어느 것을 지우는지 요청만 봐서는 알 수 없고, 열린 요청 UNIQUE 가
**첨부 단위가 아니라 비용 단위**로 걸려 두 번째 삭제 요청이 막힌다.

스냅샷에 파일명·해시를 남긴다 — 파일이 지워진 뒤에도 승인 이력만 보고
**무엇을 지웠는지** 말할 수 있어야 한다.

## 7. 월 잠금 — 추가와 삭제를 다르게 본다

| | 잠긴 달 |
|---|---|
| 증빙 **추가** | **허용** |
| 증빙 **삭제** | 차단 |

추가를 허용하는 이유: 세무사가 마감 후 증빙을 요구하는 일이 흔하고,
증빙을 붙이는 것은 **금액을 바꾸지 않는다**. 막으면 사용자는 시스템 밖에서
파일을 주고받게 되고 증빙 관리라는 목적 자체가 사라진다.

## 8. 권한 — 목록과 원본을 나눈다

```python
PERM_VIEW_EXPENSE_ATTACHMENT = "expense.view_attachment"
```

| 역할 | 다운로드 |
|---|---|
| ADMIN · ACCOUNTING · OPERATOR | ✅ |
| EXTERNAL_ACCOUNTANT | ✅ (신고 자료를 만들려면 원본이 필요) |
| READONLY · SOURCING | ❌ |

파일명·크기는 "증빙이 붙어 있다" 를 확인하는 운영 정보지만, **내용**은
계좌번호·거래 상대가 그대로 찍힌 세금계산서일 수 있다.

다운로드는 매 요청이 인증·권한을 통과한다. **영구 public URL 을 만들지 않는다.**
`Content-Disposition` 은 RFC 5987 `filename*` 로 UTF-8 을 싣고,
`X-Content-Type-Options: nosniff` 로 이미지로 위장한 스크립트 실행을 막는다.

## 9. 거래처 — 비슷한 이름 5개를 구분했다

| 테이블 | 무엇인가 | 비용과의 관계 |
|---|---|---|
| `external_partner` | **돈을 지불하는 상대** | `expenses.supplier_id` → 여기 |
| `partner` | 수익을 배분받는 내부 파트너 | `payment_card_holder_id` 등 |
| `sale_channel` | 판매처 카탈로그 | 없음 |
| `integration_account` | 판매처의 **우리 계정** | 없음 |
| "supplier" | 테이블이 아니라 **컬럼 이름** | `external_partner` 를 가리킴 |

`GET /external-partners` 는 **읽기 전용**이다. 생성 API 를 지금 만들면 검증 없이
행이 생긴다. 화면은 "거래처 등록 기능은 아직 지원하지 않습니다" 라고 **명시**하고
가짜 버튼을 두지 않는다. `can_create: false` 를 서버가 알려준다.

`business_number` 는 **스키마에 없다.** 없는 필드를 null 로 내려주면 화면이
"값이 비어 있다" 로 오해하고 입력란을 만든다 — 키 자체를 내리지 않는다.

승인 diff 에서 삭제된 거래처는 `"삭제된 거래처"` 로 표시한다.
`"다른 항목"` 으로 뭉뚱그리면 승인자는 그게 "이름을 못 찾음" 인지
"지워진 거래처" 인지 구분할 수 없다.

## 10. E2E 가 잡은 것

| 문제 | 원인 |
|---|---|
| E2E 전체가 seed 단계에서 멈춤 | `e2e_db.py` 가 alembic 출력을 **cp949** 로 디코딩. M027 docstring 의 em dash 에서 죽었다 → `encoding="utf-8"` 명시 |
| seed 재실행 실패 | 고정 id 를 바꿨는데 `ON CONFLICT (id)` 만 있어 **이름 UNIQUE** 와 충돌 → DELETE 후 INSERT |
| 파일 단언이 남의 파일을 봄 | `reseed()` 가 DB 만 지우고 저장 디렉터리를 안 비웠다 → `wipeAttachmentStorage()` 추가(`e2e` 이름 검사 포함) |

## 11. 검증

| 항목 | 결과 |
|---|---|
| M027 up·down·up | 성공 |
| 백엔드 유닛 | **86 passed, 1 xfailed** (신규 32) |
| Vitest | **134 passed** (9 파일, 신규 13) |
| Playwright E2E (실제 FastAPI · PostgreSQL · **실제 디스크**) | **62 passed** (신규 21: evidence 15 · supplier 6) |
| tsc | 통과 |
| `npm run lint:all` (app·components·lib·hooks·e2e) | **0 errors**, 경고 1건(기존) |
| build | 통과 |

`hooks/useListQuery.ts` 의 미사용 `opts` 인자를 제거했다(호출부 0건).
기본 `next lint` 가 `hooks/`·`e2e/` 를 검사하지 않아 안 보이던 것이고,
`lint:all` 이 이제 전 소스를 본다. **대량 오류는 없었다 — baseline 문서가 필요 없다.**

## 12. 상태 (갱신)

| 항목 | 상태 |
|---|---|
| 증빙 파일 업로드·다운로드·삭제 | **live** |
| 증빙 삭제 승인 (`expense_attachment` target) | **live** |
| 삭제 worker (`POST /expense-attachments/run-delete`) | **live** (수동 실행. cron 미연결) |
| 거래처 조회·선택·승인 diff 이름 | **live** |
| 악성코드 스캐너 | **미연결** — 모든 파일 `SCAN_UNAVAILABLE` |
| S3 호환 스토리지 | **미구현** — 인터페이스만. 현재 local FS |
| 거래처 생성·수정 | **미구현** (의도적) |
| `business_number` | **스키마에 없음** |
| 개인카드 상환 지급 장부 | **미구현** (이번 범위 밖 — 화면은 `PENDING` 까지만) |
| 월 재오픈 승인 | **미구현** — 화면은 안내만 하고 버튼을 만들지 않음 |
| 쇼핑몰 정산 대사 | **설계만** (`docs/47_settlement_reconciliation_design.md`) |
