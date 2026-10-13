# 50. 세션 최종 보고 — 월 마감 오케스트레이션 ~ 지급 장부

> 2026-08-08 · 시작 커밋 `808d008` → 종료 커밋 `391ad73` 이후
> 상세 설계 판단은 `48_month_close_orchestration_worklog.md`,
> 운영 절차는 `49_worker_operations_runbook.md`

---

## A. 이번 세션에 완료한 Phase

| Phase | 내용 | 분석 | 구현 | 테스트 | commit |
|---|---|---|---|---|---|
| 26~28 | 월 마감 오케스트레이션 · 확정 snapshot · 잠금 | ✅ | ✅ | 17건 | `12d9ec5` |
| 29 | 월 재오픈 승인 | ✅ | ✅ | 9건 | `c864657` |
| 30~31 | 순이익 단계 표기 · 확정 수익배분 | ✅ | ✅ | 20건 | `05c61da` |
| 32~34 | 지급 장부 · 부분 지급 · 개인카드 상환 | ✅ | ✅ | (위 20건에 포함) | `05c61da` |
| 35~36 | 운영 이슈 센터 + UI | ✅ | ✅ | 18건 | `04b0fe8` |
| 37~38 | worker CLI · 관측 | ✅ | ✅ | (위 18건에 포함) | `04b0fe8` |
| 9 | 프론트엔드 배선 (D2 수정) | ✅ | ✅ | Vitest 11건 | `9d45fef` |
| 57 | 실제 backend E2E | ✅ | ✅ | 11건 | `3def289` |
| 47 | 보안 회귀 | ✅ | ✅ | 16건 | `391ad73` |
| 53~54 | 배포 점검 · 백업 복구 런북 | ✅ | 문서 | — | `391ad73` |

---

## B. 새 migration

| revision | down_revision | 목적 | up/down/up |
|---|---|---|---|
| `M028` | `M027` | `monthly_close_check_run` / `_check_item` / `monthly_close_snapshot` + 불변성 트리거 + `monthly_settlement` 3컬럼 | ✅ |
| `M029` | `M028` | `distribution_run` / `distribution_result` / `payout_batch` / `payout_item` / `payout_payment` + `paid_amount` 트리거 | ✅ |
| `M030` | `M029` | `operation_issue` / `worker_run` | ✅ |

3개 연속 `downgrade M027` → `upgrade head` 성공. head = `M030`.

---

## C. Live 기능 (실제 DB/API/디스크 사용)

| 기능 | 근거 |
|---|---|
| 마감 검증 16종 + run 이력 | pytest 17건 + E2E |
| 확정 snapshot 박제 · UPDATE/DELETE 차단 | DB 트리거 + pytest |
| drift 탐지 (확정 후 데이터 변경) | E2E 에서 실제 409 확인 |
| 적자 이월 원장 | pytest (D4 회귀 포함) |
| 재오픈 승인 워크플로 | E2E 에서 요청→승인→적용→OPEN 확인 |
| 확정 수익배분 (snapshot 기준) | pytest |
| 지급 장부 · 부분 지급 · 과지급 차단 | E2E 에서 600k→400k→PAID 확인 |
| 개인카드 상환 (새 비용 생성 안 함) | E2E 에서 expenses 합계 불변 확인 |
| 운영 이슈 센터 + 스캐너 | E2E 에서 무시→재스캔→유지 확인 |
| worker CLI 5종 | dev DB 에서 `--once` 실제 실행, `failed_workers=0` |
| 월 마감 · 이슈 · 지급 화면 | E2E 에서 화면 렌더 확인 |

---

## D. External Blocker

```text
BLOCKED_EXTERNAL_DATA:
실제 쇼핑몰 정산 파일이 없어 판매처별 parser와 최종 monetary mapping을 확정할 수 없음
```

확인한 사실(추측 아님):

- 저장소·작업폴더 전체 검색 결과 정산서 파일 없음
- 엑셀 `쿠팡 정산 기록` 시트는 **날짜/정산금 2컬럼**짜리 입금 메모다
- `marketplace_settlement_record` 0건, `integration_account` 0건

| Blocker | 영향 |
|---|---|
| 실제 쿠팡 정산 파일 없음 | 정산 parser · 매칭 · 대사 전부 |
| 쿠팡/네이버 API credential 없음 | 주문·정산 자동 수집, 가격 반영(`CONNECTOR_UNAVAILABLE`) |
| Slack/Discord webhook 없음 | 알림 실제 전송 (Phase 39~40) |
| 악성코드 스캐너 없음 | 증빙 전부 `SCAN_UNAVAILABLE` (기존) |
| S3 호환 스토리지 없음 | 인터페이스만. 현재 local FS (기존) |

---

## E. 미구현 (코드 자체가 없음)

| 항목 | Phase | 비고 |
|---|---|---|
| 알림 실제 전송 어댑터 | 39~40 | credential 없이도 구조는 만들 수 있으나 이번 범위에서 미착수 |
| `sales_order.integration_account_id` | 41~42 | 주문↔listing 추적 |
| 현금흐름 ledger + 화면 | 43~44 | 손익과 현금흐름 분리 |
| 데이터 품질 scanner (독립) | 46 | 일부는 이슈 스캐너가 이미 커버 |
| 성능·index 검토 (EXPLAIN) | 49 | |
| 데이터 export 확장 | 51 | 기존 세무 리포트 7종은 있음 |
| 운영자 시스템 상태 화면 | 52 | API(`/operations/workers`)는 있고 화면이 없음 |
| 권한별 E2E 6역할 | 58 | 일부 역할만 확인 |

---

## F. 발견한 기존 결함 5건

### D1 — 레거시 잠금 경로가 월을 실제로 잠그지 않았다 ⚠️ 심각

| | |
|---|---|
| 재현 | `settlement_crud.lock_settlement` → `status='LOCKED'`, `close_state='OPEN'` |
| 원인 | 월 잠금 가드는 `close_state` 를 읽는데 이 경로는 `status` 만 썼다. M017 이 새 컬럼을 도입하며 레거시 경로를 안 고쳤다 |
| 영향 | **잠근 달의 판매·비용이 그대로 수정 가능.** 마감 검증도 안 거쳤다 |
| 수정 | `confirm_settlement`/`lock_settlement` 를 `monthly_close` 로 위임. 상태 기계 하나로 |
| 회귀 테스트 | `test_legacy_lock_path_actually_locks_the_month` |

### D2 — 월정산 프론트엔드가 존재하지 않는 API 를 호출

| | |
|---|---|
| 재현 | `lib/api/settlements.ts` 전부 `/settlements/monthly/…`. 그런 라우터가 없다 |
| 영향 | 월정산 화면의 **모든 요청이 404**. tsc·lint·build 가 전부 통과했다 — 경로는 문자열이라 |
| 수정 | 실제 경로로 교체 + 화면 재구현 |
| 회귀 테스트 | `lib/api/settlements.test.ts` (Vitest 11건, 경로를 계약으로 고정) |

### D3 — 미매칭 정산이 마감을 차단했다

| | |
|---|---|
| 원인 | `docs/47` §4 에서 "경고" 로 결론냈는데 M017 구현이 `blocking=True` |
| 영향 | 판매처 정산서 지연이 마감을 막는다 — 마감이 판매처 일정에 인질로 잡힌다 |
| 수정 | WARNING 으로. 확인 없이 넘어갈 수는 없다 |
| 회귀 테스트 | `test_unmatched_settlement_is_warning_not_blocking` |

### D4 — 적자월을 잠그면 무조건 500

| | |
|---|---|
| 재현 | `SELECT 'ACCRUE'::enum_carryover_event;` → `invalid input value` |
| 원인 | 유효 라벨은 `LOSS_ADDED/OFFSET_APPLIED/MANUAL_ADJUST` |
| 영향 | 적자월·이월차감월 잠금 불가. 흑자이고 이월 0인 달만 우연히 통과 |
| 수정 | `services/carryover.py` 로 분리 + 올바른 라벨 + 재잠금 멱등 |
| 회귀 테스트 | `test_locking_a_loss_month_records_carryover`, `test_relock_does_not_double_count_carryover` |

### D5 — 이슈 스캔이 API 경로에서만 500 (E2E 가 잡았다)

| | |
|---|---|
| 원인 | `record_audit(domain_row_id=None)` — `audit_log.domain_row_id` 는 NOT NULL |
| 왜 못 잡았나 | 서비스를 `actor` 없이 부르는 단위 테스트만 있었다. actor 가 있을 때만 감사 코드를 탄다 |
| 수정 | 스캔 요약 감사 제거. `worker_run` 이 이미 기록한다 |
| 회귀 테스트 | `test_scan_with_actor_does_not_crash` |

---

## G. Tests

| 항목 | 결과 |
|---|---|
| backend pytest | **456 passed, 1 xfailed** (세션 시작 376 → 신규 80) |
| pytest 반복 실행 | 2회 모두 동일 (440→456, fixture 충돌 없음) |
| migration up/down/up | M028·M029·M030 3개 연속 성공 |
| Vitest | **145 passed** (신규 11) |
| tsc | 통과 |
| `npm run lint:all` | 0 errors, 경고 1건(기존 `products/page.tsx`) |
| production build | 통과 |
| Playwright E2E (실제 FastAPI + PostgreSQL) | **73 passed** — **2회 연속 동일**, flaky 0 |
| ruff (신규·수정 모듈) | 통과 |

**flaky test 없음.** E2E 를 두 번 연속 돌려 73/73 동일하게 통과했다.
중간에 실패한 3건은 전부 원인을 규명하고 고쳤다(세부는 §H-2).

---

## H. 남은 것과 다음 우선순위

### H-1. 외부 blocker 해소 시 재개 지점

**실제 쿠팡 정산서가 확보되면**:

1. `docs/47` §7 표의 선행조건 1·2·4·5 순서대로
   (매칭 상태 컬럼 → UNIQUE 재설계 → `settlement_adjustment` → 승인 어댑터)
2. `services/close_checks._settlement_checks` 가 확장 지점이다.
   현재 "매칭 상태 컬럼이 없어 UNMATCHED 만 본다" 는 사실이 코드 주석에 있다
3. `close_snapshot` 의 `settlement_connected` / `profit_basis` 는 이미
   대사 완료를 표현할 수 있다 — 매칭이 붙으면 자동으로 `SETTLEMENT_RECONCILED` 가 된다

**Slack/Discord webhook 이 확보되면**: Phase 39~40.
현재 `notification_channel` 메타와 UI 는 있고 실제 전송 어댑터가 없다.

### H-2. 이번에 고친 E2E 실패 3건 (원인 규명 완료)

| 실패 | 원인 | 조치 |
|---|---|---|
| `product-save` 월 잠금 | seed 가 심은 열린 달이 "KST 이번 달" 과 UNIQUE 충돌 | seed 를 고정 과거 달(2026-06)로 |
| `month-close` 잠금 403 | 잠금은 ADMIN 전용인데 테스트가 ACCOUNTING 사용 | **동작이 맞다.** 테스트를 고치고 권한 경계를 단언으로 고정 |
| `month-close` 이슈 스캔 500 | D5 | 코드 수정 + 회귀 테스트 |

### H-3. 다음 세션 최우선 (외부 의존 없음)

우선순위 순:

1. **대시보드를 실제 데이터로 배선 (Phase 45)** — `lib/adapters/dashboard.ts` 가
   전부 `MOCK_*` 다. 확정월은 snapshot, 진행월은 live 로 나눠야 한다.
   이번에 만든 `monthly_close_snapshot` 이 그 근거를 이미 제공한다
2. **수익배분 화면 배선 (Phase 31 잔여)** — `/settlements/distribution` 이
   `distributionAdapter`(mock)를 쓴다. 백엔드 `/api/v1/distributions/*` 는 live 다
3. **운영자 시스템 상태 화면 (Phase 52)** — `/operations/workers` API 는 있고
   화면이 없다. worker 가 안 도는 것을 알아차릴 방법이 관리자에게 없다
4. **알림 실제 전송 기반 (Phase 39)** — IN_APP 부터. credential 불필요
5. **현금흐름 ledger (Phase 43~44)**

---

## I. Mock 감사 (§61)

### 이번 세션 신규 코드

production mock **0건**. 확인 방법: `MOCK_`·`mockData`·`fake`·`TODO`·`FIXME`
전수 검색 — 신규 backend 모듈 7개와 신규 화면 3개에서 매치 없음
(입력 placeholder 는 UI 힌트이지 데이터 mock 이 아니다).

### 기존 코드에 남아 있는 production mock (이번 범위 밖)

| 위치 | 성격 | 위험도 |
|---|---|---|
| `lib/adapters/dashboard.ts` | 대시보드 전체가 `MOCK_PRODUCTS`/`MOCK_EXPENSES` 집계 | **높음** — 실제 금액처럼 보이는 sample |
| `lib/adapters` `distributionAdapter` | 배분 규칙 CRUD 가 메모리 배열 | **높음** — 저장 성공 mock |
| `MOCK_MARKETPLACES` | 판매처 코드→라벨 룩업 상수 | 낮음 — 데이터가 아니라 표시 라벨 |

앞의 둘은 §61 의 "금지" 항목(실제 금액 대신 sample, 저장 성공 mock)에 해당한다.
**이번 세션에서 건드리지 않았고**, H-3 의 1·2번으로 올려 두었다.

---

## J. 최종 상태 표 (§60)

| Domain | Backend | Frontend | Test | External dep | Status |
|---|---|---|---|---|---|
| dashboard | 일부 | mock | ✗ | — | **MOCK_DEV_ONLY** |
| products | live | live | ✓ | — | LIVE |
| options | live | live | ✓ | — | LIVE |
| channel listing | live | live | ✓ | — | LIVE |
| channel price | live | live | ✓ | connector | PARTIAL |
| approvals | live | live | ✓ | — | LIVE |
| sales | live | live | ✓ | — | LIVE |
| expenses | live | live | ✓ | — | LIVE |
| attachments | live | live | ✓ | 스캐너·S3 | PARTIAL |
| external partners | 조회만 | live | ✓ | — | PARTIAL |
| **settlement (대사)** | 스키마만 | 목록만 | ✗ | **정산 파일** | **BLOCKED_EXTERNAL_DATA** |
| **monthly close** | **live** | **live** | **✓** | — | **LIVE** |
| **month snapshot** | **live** | **live** | **✓** | — | **LIVE** |
| **month reopen 승인** | **live** | **live** | **✓** | — | **LIVE** |
| **distribution (확정)** | **live** | ✗ | **✓** | — | **PARTIAL** (화면 미배선) |
| distribution (규칙 편집) | ✗ | mock | ✗ | — | **MOCK_DEV_ONLY** |
| **payout** | **live** | **live** | **✓** | — | **LIVE** |
| **issues** | **live** | **live** | **✓** | — | **LIVE** |
| **workers** | **live** | ✗ | **✓** | 외부 스케줄러 | **PARTIAL** (화면 미배선) |
| notifications | 메타만 | live | 일부 | webhook | PARTIAL |
| integrations | 메타만 | live | 일부 | credential | BLOCKED_CREDENTIAL |
| audit | live | live | ✓ | — | LIVE |
| settings | live | 일부 mock | 일부 | — | PARTIAL |

> "화면이 존재한다" 와 "기능이 live 다" 를 구분했다.
> `distribution(확정)` 과 `workers` 는 **backend 는 live 인데 화면이 없다** —
> API 를 직접 호출하면 동작한다.
