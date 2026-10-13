# Decision Log

이 문서는 PICKSOME OPS TOOL 설계 과정에서 내려진 모든 의사결정과 미확정 사항을 누적 기록한다.
각 항목은 **Confirmed / Inferred / Uncertain** 라벨과 결정 시점, 영향 범위를 함께 남긴다.

- 최초 작성: 2026-06-17
- 작성자(역할): PM / Architect
- 참조: `CLAUDE.md`, `docs/00_project_overview.md`, `docs/_planning_session.md`, `docs/_schema_design.md`, `docs/_excel_analysis.md`

---

## 1. Confirmed Decisions (확정 사항)

### 1-1. 제품 비전 및 단계

| # | 결정 | 라벨 | 일자 | 영향 |
|---|------|------|------|------|
| D-001 | 1차 = 픽썸 내부 운영툴, 최종 = 범용 셀러 운영 SaaS | Confirmed | 2026-06-17 | 모든 역할 |
| D-002 | 8개 역할 (PM/Architect, Planner, Designer, FE, BE, DevOps, DBA, User/세무) 분리 진행 | Confirmed | 2026-06-17 | 워크플로우 |
| D-003 | 코드 생성은 사용자 승인 전까지 금지. 문서·설계만 진행 | Confirmed | 2026-06-17 | 작업 순서 |
| D-004 | `.claude/skills/structured-thinking` 항상 적용 | Confirmed | 2026-06-17 | 모든 산출물 |

### 1-2. 기술 스택

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-010 | Frontend: Next.js (App Router) + React + TypeScript + Tailwind | Confirmed | FE |
| D-011 | Backend: FastAPI + Python 3.12+ + SQLAlchemy 2 + Pydantic v2 + Alembic | Confirmed | BE |
| D-012 | Database: PostgreSQL 16 | Confirmed | DBA |
| D-013 | Container: Docker Compose (MVP), Kubernetes는 Post-MVP | Confirmed | DevOps |
| D-014 | Timezone Asia/Seoul, Currency KRW 고정 (MVP) | Confirmed | 전 영역 |
| D-015 | 금액은 `int (원 단위)` 또는 `Decimal`, **`float` 금지** | Confirmed | BE/DBA |

### 1-3. 범용화 / 정책 테이블

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-020 | 모든 비즈니스 값은 정책 테이블화. 하드코딩 금지 | Confirmed | 전 영역 |
| D-021 | 12개 정책 테이블 채택: `marketplace_fee_policy`, `payment_fee_policy`, `tax_policy`, `fulfillment_fee_policy`, `return_risk_policy`, `ad_cost_policy`, `target_margin_policy`, `settlement_policy`, `partner_share_policy`, `expense_category_policy`, `evidence_type_policy`, `policy_change_history` | Confirmed | DBA/BE |
| D-022 | 공통 정책 컬럼: `version, effective_from, effective_to, source_type, source_url, source_checked_at, approved_by, approved_at, memo, is_active` | Confirmed | DBA |
| D-023 | 정책 변경은 새 row 삽입 + 이전 row의 `effective_to` 갱신 (불변 이력) | Confirmed | BE |
| D-024 | 정책 신뢰도 상태(`source_type`): MANUAL / CRAWLED / API / IMPORTED — UI 배지로 표시 | Confirmed | FE/Design |

### 1-4. 계산 스냅샷 패턴

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-030 | 마진·정산 계산 결과에는 **사용된 정책 버전 snapshot** 을 기록 | Confirmed | BE |
| D-031 | 스냅샷 저장 방식: **하이브리드 (FK + JSONB)**. `policy_snapshot_refs` (FK 리스트) + `policy_snapshot_values` (계산 시점의 실제 값들 JSON) | Confirmed | DBA/BE |
| D-032 | 과거 정책으로 다시 계산해도 동일 결과가 나와야 함 (재현성) | Confirmed | BE |

### 1-5. 마진 모델

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-040 | **예상 마진 vs 실 마진 분리**: `product_margin_expected` / `product_margin_actual` 테이블 별도 운영 | Confirmed | DBA/BE |
| D-041 | 예상 마진: 정책 snapshot 기반. 실 마진: 정산 import 데이터 기반 | Confirmed | BE |
| D-042 | 마진 타입 enum: `PURCHASE_AGENCY`, `ROCKET_GROWTH`, `OVERSEAS_DIRECT`, `GENERAL_RESELL` | Confirmed | BE |
| D-043 | 예상/실제 차이(delta) 자동 계산 후 ±5% 초과 시 경고 배지 | Confirmed | FE/Design |
| D-044 | UI 토글 [예상 \| 실제] + 비교 뷰 제공 | Confirmed | Design/FE |

### 1-6. 정산 / 수익배분 모델

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-050 | **수익배분 기준 = 모든 비용을 차감한 순이익** (매출이나 단일 상품 마진 단독 아님) | Confirmed | BE |
| D-051 | 순이익 = 매출 - 상품 직접비 (원가 + 마켓수수료 + 광고비 + 반품비 + 풀필먼트비 + 배송비) - 사업 공통비 (`expenses` 월별 합계) | Confirmed | BE |
| D-052 | 분배 방식: N명 파트너, 비율 합 = 1.0, 월별 override 가능 | Confirmed | DBA/BE |
| D-053 | **우선 분배** (priority_distribution) 지원: 비율 계산 전 특정 파트너에게 정액을 먼저 배정 | Confirmed | BE |
| D-054 | 픽썸 기본값 (참고): 파트너A 우선 100만 → 잔여 6:4 (파트너A:파트너B). **단, 이는 한 사례일 뿐 정책 테이블로 관리** | Confirmed | BE/DBA |
| D-055 | **적자월 정책 = `carryover` (A안)**: 분배 0, 손실은 `loss_carryover_ledger` 에 누적, 다음 흑자월에서 우선 차감 | Confirmed | BE |
| D-056 | 정산 상태 워크플로우: `DRAFT` → `CONFIRMED` → `LOCKED`, 정정은 별도 `REOPENED` 이벤트로만 (감사 로그 필수) | Confirmed | BE/Design |

### 1-7. 세무 리포트 (MVP Must)

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-060 | **세무사 제출용 리포트는 MVP 필수**. 자동 신고 대체는 하지 않음 | Confirmed | 전 영역 |
| D-061 | MVP 리포트: 월별 매출/매입/사업비용, 판매처별 매출, 상품별 손익, 쿠팡 정산 대사, 부가세 참고, 파트너별 수익배분, 증빙 누락, **분기별 부가세 참고** | Confirmed | BE |
| D-062 | 출력 포맷: Excel/CSV, 컬럼명 한국어. 파일명 규칙 `YYYY-MM_리포트명.xlsx` | Confirmed | BE |
| D-063 | 부가세 신고 일정 (1·4·7·10월 25일) 알림 + 분기별 리포트 자동 생성 | Confirmed | BE/알림 |

### 1-8. 사업비용 / 증빙 (MVP Must)

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-070 | `expenses` 테이블은 정산·수익배분의 **입력**. 단순 메모 금지 | Confirmed | BE/DBA |
| D-071 | 각 비용 건은 **증빙 유형 + 증빙 상태** 필수 보유 | Confirmed | DBA |
| D-072 | 증빙 상태 워크플로우: `증빙없음` → `확인필요` → `증빙있음` → `세무사전달완료` | Confirmed | BE/Design |
| D-073 | 컬럼 추가: `payment_card_holder`, `is_personal_card`, `is_business_use`, `is_tax_deductible`, `is_settlement_deductible`, `attributable_partner_id` | Confirmed | DBA |
| D-074 | 파일 첨부는 MVP 이후 확장. MVP는 유형/상태만 저장 | Confirmed | BE/FE |

### 1-9. RBAC / 권한

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-080 | 6개 역할: `ADMIN`, `OPERATOR`, `SOURCING`, `ACCOUNTING`, `READONLY`, `EXTERNAL_ACCOUNTANT` | Confirmed | BE/DBA |
| D-081 | MVP에서 권한 화면 완성하지 않더라도 DB·코드는 role 기반 접근제어 가능 구조 | Confirmed | BE |
| D-082 | **세무사 초대 계정** (읽기 + 코멘트 권한) MVP 포함 | Confirmed | BE/FE |

### 1-10. 보안

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-090 | 비밀번호·계좌·인증서·카드·보안카드 평문 저장 금지 | Confirmed | 전 영역 |
| D-091 | 프로그램에는 보관 위치만 저장 (예: "Bitwarden - 쿠팡 Wing"). `password_vault_entry` 테이블 사용 | Confirmed | DBA/BE |
| D-092 | 주민등록번호 등 고위험 PII는 저장하지 않음 | Confirmed | DBA |
| D-093 | 로그에 민감정보 출력 금지. seed/sample 에 실제 계정 금지 | Confirmed | DevOps |

### 1-11. 감사 로그 / 정산 잠금

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-100 | `audit_log` 테이블: 모든 도메인 변경 (before/after JSONB) 기록 | Confirmed | DBA/BE |
| D-101 | 확정된 월별 정산은 변경 불가. 정정은 다음달 "정정 정산" 또는 REOPENED 이벤트로 처리 | Confirmed | BE |

### 1-12. UI/UX

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-110 | 관리자 업무툴 UX: 데이터 밀도 우선, 키보드 입력 흐름, 상태 배지로 즉시 인지 | Confirmed | Design |
| D-111 | 정책 자동값 시각화: 회색 배경 + i 아이콘, 수동 수정 시 노란 배경 + 배지 | Confirmed | Design/FE |
| D-112 | 표 셀 인라인 편집 (Enter=저장, Esc=취소, Tab=다음 셀), 일괄 선택+일괄 수정 | Confirmed | FE/Design |
| D-113 | 모바일 반응형은 **조회 화면 한정**. 대량 입력은 데스크톱 전용 | Confirmed | FE/Design |
| D-114 | 다크 테마 / 색약 모드 / 다국어 / 다통화는 Post-MVP | Confirmed | Design |

### 1-13. 외부 연동 로드맵

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-120 | MVP는 수동 입력 + 엑셀 import/export 만 | Confirmed | BE/FE |
| D-121 | 쿠팡/스마트스토어 API 연동, 도매꾹/1688 크롤링, 야간 배치는 Post-MVP | Confirmed | BE/DevOps |
| D-122 | 단, DB·코드 구조는 처음부터 확장 가능하게 (`integration_account`, `integration_job`, `batch_run`, `external_product_mapping`, `supplier_price_snapshot`, `marketplace_order_import`, `marketplace_settlement_import`) | Confirmed | DBA |

### 1-14. 엑셀 분석 (참고)

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-130 | 엑셀 14개 시트 분석 완료, `docs/_excel_analysis.md` 에 정리 (비밀번호 모두 REDACTED) | Confirmed | 참조 |
| D-131 | 엑셀 화면을 그대로 복사하지 않음. 업무 프로세스와 계산 로직만 재설계 | Confirmed | Planner/Design |
| D-132 | Sheet12 등 사적 메모성 시트는 마이그레이션 대상에서 제외 | Confirmed | DBA |

---

## 2. Roundtable 합의 (Planner + Designer + User/세무) — 16개 MVP 추가

`docs/_planning_session.md` 표 그대로. 요약:

✅ **MVP Must** (12): 감사 로그, 정산 확정 잠금, 세무사 초대, 증빙 워크플로우, 매입-매출 대사, 카테고리 목표마진, 대시보드 KPI, 인라인 편집+일괄수정, 분기별 부가세 리포트, 카드결제자·사적/사업 비용 구분, 적자 carryover, 세무사 코멘트/잠금/전달완료

⚠️ **MVP Should** (2): 알림 시스템 (이메일 우선), 모바일 반응형 (조회 한정)

⏳ **Post-MVP** (6): 온보딩 마법사, 상품 옵션(사이즈/색상), 다국어/다통화, 다크/색약, 자동 크롤링, 쿠팡/스마트스토어 API

---

## 3. Open Items (사용자 결정 — 2026-06-18 모두 Confirmed)

> 7건 모두 사용자 결정 완료. 각 항목은 §1-15 ~ §1-21 의 D-280 ~ D-286 으로 이동되었다.
> 일부 기존 결정은 본 결정에 의해 **SUPERSEDED** 처리된다 (§1-22 참조).

| # | 항목 | 결정 요약 | 새 결정 번호 |
|---|------|----------|------------|
| O-001 | 사업 형태 | **하드코딩 금지**. organization 구조 + business_type enum (SOLE/JOINT/CORP/TEAM) + N명 파트너 분배 | D-280 |
| O-002 | 알림 채널 | MVP = **이메일 + 앱 내부 알림** 두 채널. 카톡/슬랙/텔레그램/웹훅 = Post-MVP. 채널 확장형 구조 | D-281 |
| O-003 | 세무사 합류 | MVP = **세무사 export 리포트** (월별 매출/비용/정산/증빙누락/부가세참고/종합소득세참고). 세무사 초대 계정 = Post-MVP | D-282 |
| O-004 | ID 타입 | **UUID (v7 권장)** 전 테이블 PK/FK. 사용자 노출 ID 는 별도 `business_key` 컬럼 | D-283 |
| O-005 | 부가세 자동분리 | **세무 참고용 자동 분리**. `vat_inclusion_type` (INCLUDED/EXCLUDED/EXEMPT) + 수동 override + 세율 정책 snapshot | D-284 |
| O-006 | REOPEN 횟수 제한 | **MVP 기본 3회**, organization 설정값으로 override 가능. ADMIN 전용 + 사유 필수 + 감사 로그 | D-285 |
| O-007 | 옵션 슬롯 | **정규화** — `product_options` / `product_option_values` / `product_variants` 별도 테이블. 4컬럼 슬롯 폐기 | D-286 |

---

## 1-15. O-001 결정 (사업 형태) — D-280

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-280-1 | 특정 사업 형태를 하드코딩하지 않는다. organization 단위로 운영 | Confirmed | 전 영역 |
| D-280-2 | `organization.business_type` enum 추가: `SOLE_PROPRIETOR`, `JOINT_BUSINESS`, `CORPORATION`, `TEAM_OPERATION` | Confirmed | DBA |
| D-280-3 | 하나의 organization 에 N명 사용자/파트너 소속. 파트너 분배는 정책 테이블 (`partner_share_policy`) 로 관리 — N명 분배 지원 | Confirmed | DBA/BE |
| D-280-4 | 픽썸 기본 seed = 공동사업자 + 2인 (파트너A/파트너B) 6:4. **단, 이는 한 사례일 뿐 다른 조직은 자유롭게 변경 가능** | Confirmed | DBA seed |
| D-280-5 | 정산·세무 리포트는 organization 기준으로 생성. 필요 시 파트너별 분배 리포트 추가 생성 | Confirmed | BE/리포트 |

## 1-16. O-002 결정 (알림 채널) — D-281

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-281-1 | MVP 알림 채널 = `EMAIL` + `IN_APP` 두 가지. 카톡/슬랙/텔레그램/웹훅 = Post-MVP | Confirmed | BE/FE |
| D-281-2 | 알림 채널을 enum 으로 하드코딩하지 않고 `notification_channel` 메타 테이블 + 채널 어댑터 인터페이스 구조 | Confirmed | BE/DBA |
| D-281-3 | 알림 이력 (`notification`) 영구 저장. 사용자별 채널/종류별 ON/OFF (`notification_pref`) | Confirmed | DBA |
| D-281-4 | 알림 트리거 이벤트 (MVP): 마진율 기준 이하, 예상 적자, 공급가 상승, 품절 위험, 정산 import 완료, 배치 실패, 증빙 누락, 세무 리포트 생성 완료 | Confirmed | BE |
| D-281-5 | 앱 내부 알림 = 헤더 벨 아이콘 + 수신함. 미확인 카운트 배지 | Confirmed | FE/Design |

## 1-17. O-003 결정 (세무사 협업) — D-282

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-282-1 | MVP 에서 **세무사 계정 초대 기능은 필수가 아님**. EXTERNAL_ACCOUNTANT 라우트는 비활성 토글 | Confirmed | BE/FE |
| D-282-2 | MVP 필수 = 세무사에게 바로 전달할 수 있는 **Excel/CSV export 리포트** | Confirmed | BE |
| D-282-3 | MVP 리포트 (세무사 제출용): 월별 매출/비용/정산, 증빙 누락, 부가세 참고, **종합소득세/사업소득 참고자료** | Confirmed | BE |
| D-282-4 | Post-MVP: 세무사 전용 읽기 권한 계정, 초대 기능, 코멘트, 검토 상태값, 자동 리포트 발송 | Confirmed | 로드맵 |
| D-282-5 | 시스템은 세무 신고를 자동 대체하지 않는다 — 구조화된 자료 제공 + Disclaimer | Confirmed | UX 문구 |

## 1-18. O-004 결정 (ID 타입) — D-283

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-283-1 | 모든 주요 도메인 테이블 PK = `UUID PRIMARY KEY` (v7 권장, fallback `gen_random_uuid()`) | Confirmed | DBA/BE |
| D-283-2 | 모든 FK = `UUID` | Confirmed | DBA |
| D-283-3 | 사용자 노출 ID (상품코드, 주문번호, 정산번호) 는 별도 `business_key` 컬럼 (TEXT, organization 내 UNIQUE) | Confirmed | DBA/BE |
| D-283-4 | PostgreSQL extension: `pgcrypto` (gen_random_uuid). v7 은 app 측에서 생성 (uuid7 라이브러리) — DB 함수 미존재 시 대비 | Confirmed | DevOps |
| D-283-5 | 외부 노출 URL/API 응답에 UUID 사용. 순차 ID 노출 금지 | Confirmed | BE/FE |

## 1-19. O-005 결정 (부가세 자동분리) — D-284

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-284-1 | 부가세 자동분리 = **세무 참고용** (확정 세무 계산이 아님). Disclaimer UI 명시 | Confirmed | FE/Design |
| D-284-2 | 금액 입력 시 `vat_inclusion_type` 선택: `INCLUDED` (부가세 포함) / `EXCLUDED` (부가세 별도) / `EXEMPT` (면세·제외) | Confirmed | DBA/BE/FE |
| D-284-3 | `tax_policy` 확장: `tax_type`, `business_type`, `vat_rate`, `calculation_method`, `is_vat_included`, `effective_from`, `effective_to`, `is_active`. 세율은 하드코딩 금지 — 정책 row 참조 | Confirmed | DBA |
| D-284-4 | `expenses` / 매출 테이블 컬럼: `gross_amount`, `supply_amount`, `vat_amount`, `tax_type`, `vat_inclusion_type`, `tax_policy_id`, `is_manual_tax_override` | Confirmed | DBA |
| D-284-5 | 계산 시점의 `tax_policy_id` 박제. 과거 리포트는 정책 변경 후에도 동일 결과 재현 (snapshot 패턴 유지) | Confirmed | BE |
| D-284-6 | 일반과세 기본 부가세율 10% 는 **seed row** 로만 존재. 간이/면세/영세 처리 분기는 정책 row 로 관리 | Confirmed | DBA seed |

## 1-20. O-006 결정 (REOPEN 횟수 제한) — D-285

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-285-1 | 월별 정산 상태 = `DRAFT` → `CONFIRMED` → `LOCKED` → (필요 시) `REOPENED` | Confirmed | BE |
| D-285-2 | MVP 기본 REOPEN 제한 = **3회**. organization 설정값 (`reopen_limit`) 으로 override 가능 | Confirmed | DBA/BE |
| D-285-3 | REOPEN 권한 = ADMIN 만. 사유 (`reopen_reason`) 필수 입력 | Confirmed | BE/FE |
| D-285-4 | `monthly_settlement.reopen_count` 카운터 + `last_reopened_at`, `reopened_by`. 제한 초과 시 `REOPEN_LIMIT_EXCEEDED` (409) | Confirmed | BE |
| D-285-5 | REOPEN 이력 `settlement_reopen_audit` 에 영구 저장 (감사 로그 별도 +1) | Confirmed | DBA |

## 1-21. O-007 결정 (옵션 정규화) — D-286

| # | 결정 | 라벨 | 영향 |
|---|------|------|------|
| D-286-1 | `product.option_*` 4컬럼 슬롯 **폐기**. 별도 테이블로 정규화 | Confirmed | DBA |
| D-286-2 | 신규 테이블: `product_option_group` (옵션 그룹: 색상/사이즈), `product_option_value` (옵션 값), `product_variant` (조합 변형) | Confirmed | DBA |
| D-286-3 | MVP 지원: 옵션명·옵션값·옵션별 원가/판매가/재고. 복잡한 조합 생성기는 Post-MVP | Confirmed | BE/FE |
| D-286-4 | `sales_order` 와 `product_margin_*` 는 `product_variant_id` (NULL 허용) 추가. 옵션 없는 상품은 NULL | Confirmed | DBA/BE |
| D-286-5 | 시트12(키링 옵션 관리) 는 MVP 제외. 일반화된 옵션·재고 관리 기능으로 통합 | Confirmed | 범위 |

## 1-22. Supersede (기존 결정 정정)

| 기존 # | 변경 | 사유 | 신규 # |
|--------|------|------|--------|
| ~~D-082~~ 세무사 초대 계정 MVP 포함 | **Post-MVP 로 이동**. MVP 는 export 리포트만 | O-003 답변 | D-282 |
| ~~D-211~~ PK = BIGSERIAL (Inferred) | **UUID 로 전환** (v7 권장) | O-004 답변 | D-283 |
| ~~D-217~~ product.option_* 4컬럼 사전 준비 (Inferred) | **컬럼 폐기, 별도 테이블 정규화** | O-007 답변 | D-286 |
| ~~D-273~~ R-V-4 (부가세 자동분리 Inferred) | **확정**: vat_inclusion_type + 정책 snapshot | O-005 답변 | D-284 |
| ~~D-276~~ R-V-7 (세무사 코멘트 알림 일원화 보류) | **MVP 알림 채널 EMAIL + IN_APP 두 가지 확정** | O-002 답변 | D-281 |

기존 결정은 본 문서에서 **취소선 + supersede 링크** 로 남기고 hard-delete 하지 않는다 (이력 추적용).

---

## 1-23. 8역할 합의 (Roundtable on Open Items 7건) — 2026-06-18

| 역할 | 합의 사항 |
|------|----------|
| PM/Architect | 7건 결정은 모두 **범용 SaaS 전환을 강화**하는 방향. 마이그레이션 비용은 ERD 단계에서 흡수 (코드 생성 전) — 합리적. |
| Planner | PRD §Persona에 EXTERNAL_ACCOUNTANT 활성 시점을 "Post-MVP" 로 표기. 세무사 export 리포트 세트(D-282-3) 를 US-502 에 보강. |
| Designer | UUID 노출 안함 → `business_key` 라벨(상품코드/정산번호) UI 표시. 옵션 입력은 variants 그리드 컴포넌트. 알림 벨 + 수신함 디자인 토큰 보강. |
| Frontend | UUID 화면 표시 금지, 항상 `business_key`. Route guard 에 EXTERNAL_ACCOUNTANT 비활성 토글. 옵션·variants 화면은 MVP 단순 폼. |
| Backend | UUID v7 라이브러리 (`uuid7`) app-side 생성 — DB 펑션 의존 회피. tax_policy_id snapshot 박제는 기존 policy_snapshot 패턴 재사용. REOPEN_LIMIT_EXCEEDED 에러코드 추가. |
| DBA | `pgcrypto` extension + UUID 컬럼. expenses 의 generated `total_amount` 컬럼 폐기 (vat_inclusion_type 분기로 조건부 계산). 옵션 정규화 3테이블 추가. notification_channel 메타 테이블 도입. |
| DevOps | env 에 SMTP_* 만 필요. Kakao/Slack 시크릿은 Post-MVP. UUID 생성은 app-side라 별도 DB 함수 불필요. |
| User/세무 | 세무사 export 가 MVP 에 포함되므로 검증 체크리스트 통과. EXTERNAL_ACCOUNTANT UI 미구현은 수용 (export 가 더 현실적). 부가세 분리 disclaimer 명시 요청 — 수용. |

**합의 결론**: 위 7건 결정에 따라 ERD/API/FE/DevOps 문서를 일괄 패치 후 코드 생성으로 진입.

---

## 4. 역할별 산출물 통합 결정 (Planner / DBA / BE / Designer / FE / DevOps / User)

### 4-1. Planner (`20_prd.md`)
| # | 결정 | 라벨 |
|---|------|------|
| D-200 | 5 Persona, 8 Epic, 25+ User Story, 5 User Flow 확정 | Confirmed |
| D-201 | IA 11개 1차 메뉴 + 하위 구조 | Confirmed |
| D-202 | 모바일 반응형 = 조회 4 화면 한정 (대시보드/상품목록/판매장부/월정산상세) | Confirmed |
| D-203 | 마진 타입 4종을 단일 마진계산기에 통합 (탭/셀렉트) | Confirmed |
| D-204 | 세무 리포트 = MVP 7종 + 분기 부가세 (총 8종) | Confirmed |
| D-205 | 인라인 편집 키 바인딩 Enter/Esc/Tab 표준 | Confirmed |
| D-206 | 마진 격차 경고 임계값 = ±5% (Inferred — 카테고리별 조정 가능성 있음) | Inferred |

### 4-2. DBA (`21_erd.md`)
| # | 결정 | 라벨 |
|---|------|------|
| D-210 | 약 38개 테이블 + 21개 enum 확정 | Confirmed |
| D-211 | PK = BIGSERIAL (O-004 미결 시까지) | Inferred |
| D-212 | 정책 snapshot = `policy_snapshot` + `policy_snapshot_ref`* + `policy_snapshot_value` (하이브리드) | Confirmed |
| D-213 | `expenses.total_amount` = GENERATED 컬럼 (supply + vat) | Confirmed |
| D-214 | `audit_log` 월별 파티셔닝은 Post-MVP. MVP 단일 테이블 + 인덱스 + 2년 보관 | Confirmed |
| D-215 | 초기 seed 9개 항목 (organization/partner/표준 카테고리/증빙유형/세금/파트너분배/정산정책/목표마진/마켓수수료 예시) | Confirmed |
| D-216 | 정책 row hard delete 금지, DEPRECATED 로 대체 | Confirmed |
| D-217 | `product.option_*` 4컬럼 사전 준비 (O-007 미결 시까지) | Inferred |

### 4-3. Backend (`22_api_spec.md`)
| # | 결정 | 라벨 |
|---|------|------|
| D-220 | 디렉토리 = api/services/repositories/domain/db/io 계층 분리 | Confirmed |
| D-221 | 응답 wrapper `{data, meta}`, 에러 wrapper `{error{code,message,details}}` + HTTP 상태 | Confirmed |
| D-222 | 모든 mutation 트랜잭션 안에 audit_log + (해당 시) snapshot 박제 — **DB 트리거 아닌 서비스에서 명시 호출** | Confirmed |
| D-223 | Decimal 사용, float 금지, 금액 BIGINT(원) 유지 | Confirmed |
| D-224 | 14개 라우터 그룹, 약 80+ endpoint | Confirmed |
| D-225 | 6종 핵심 계산 함수 시그니처 확정 (`resolve_active_policies`, `calculate_expected_margin`, `calculate_actual_margin`, `calculate_monthly_settlement`, `simulate_recommended_prices`, `generate_tax_report`) | Confirmed |
| D-226 | MVP = sync SQLAlchemy (psycopg). async 전환은 Post-MVP | Confirmed |
| D-227 | 리포트 = 작은 기간 동기, 큰 기간 백그라운드 (Celery/RQ 는 Post-MVP) | Confirmed |

### 4-4. Designer (`23_design.md`)
| # | 결정 | 라벨 |
|---|------|------|
| D-230 | 디자인 토큰 (color/typo/space/radius/shadow/grid) 확정 | Confirmed |
| D-231 | 사이드바 240px / 헤더 72px / max-width 1440 | Confirmed |
| D-232 | 자동값 = muted 배경 + ⓘ / 수동값 = warn 배경 + 배지 + 사유 (선택) | Confirmed |
| D-233 | 상태 배지 11종 정의 (정책 6 + 정산 4 + 증빙 4 — 일부 공유) | Confirmed |
| D-234 | Table 컴포넌트 = sticky 헤더 + 인라인 편집 + 일괄 액션바 + 우상 더보기 | Confirmed |
| D-235 | 글로벌 Cmd/Ctrl+K 검색 (MVP: 상품/판매/비용 3개) | Confirmed |
| D-236 | 색약/다크 모드 = Post-MVP. 단, 컬러 토큰은 시맨틱화 유지 | Confirmed |

### 4-5. Frontend (`24_frontend.md`)
| # | 결정 | 라벨 |
|---|------|------|
| D-240 | Next.js App Router + 그룹 (auth/app) 구조 | Confirmed |
| D-241 | 서버 컴포넌트 prefetch + TanStack Query (staleTime 30s) | Confirmed |
| D-242 | 인라인 편집 = 낙관적 업데이트 + 즉시 PATCH | Confirmed |
| D-243 | 상태관리 = TanStack Query (서버) + Zustand (UI/auth) + URL params (필터) | Confirmed |
| D-244 | API 타입 = OpenAPI → openapi-typescript 자동 생성 (CI) | Confirmed |
| D-245 | RBAC = 라우트 가드 + 컴포넌트 `<RequireRole>` 이중 적용 | Confirmed |
| D-246 | Chart = Recharts (Inferred — 추후 변경 가능) | Inferred |
| D-247 | 모바일 진입 차단 라우트 정의 (관리/정책/입력 화면) | Confirmed |

### 4-6. DevOps (`25_devops.md`)
| # | 결정 | 라벨 |
|---|------|------|
| D-250 | Docker Compose 4 서비스 (postgres / backend / frontend / proxy) | Confirmed |
| D-251 | Reverse proxy = Caddy (자동 HTTPS 옵션) | Confirmed |
| D-252 | 스케줄러 = APScheduler 내장 (MVP). Celery/RQ Post-MVP | Confirmed |
| D-253 | 백업 = 일 1회 pg_dump + 7/4/6 회전 + 주 1회 검증 restore | Confirmed |
| D-254 | 로그 = stdout JSON + 민감정보 자동 마스킹. Loki Post-MVP | Confirmed |
| D-255 | TZ=Asia/Seoul, DB 저장 UTC, 표시 KST | Confirmed |
| D-256 | MVP 사이징 = 2 vCPU / 4 GiB / 50 GiB | Confirmed |
| D-257 | 비밀번호 hash = argon2 권장 (bcrypt 도 허용) | Confirmed |

### 4-7. User / 세무 검증 (`26_user_verification.md`)
| # | 결정 | 라벨 |
|---|------|------|
| D-260 | 합격 조건 = 검증 체크리스트 핵심 80%+ 통과 + SC-1 시나리오 ±1원 일치 + 세무사 의견 반영 (또는 미반영 사유 명문화) | Confirmed |
| D-261 | 세무 공제 가능 판단 = 시스템은 분류만 제공, 판단은 세무사 책임 (Disclaimer 명시) | Confirmed |
| D-262 | 사적 비용 = 정산공제 자동 OFF 기본 | Confirmed |
| D-263 | 직전 값 복사 단축키 Ctrl+D 추가 권고 (수용 — Should) | Confirmed |

### 4-8. PM 통합 — 잔여 위험 처리
| # | 위험 | 처리 |
|---|------|------|
| D-270 | R-V-1 환불/환출 음수 비용 | **수용** — DBA 가 `expenses.supply_amount/vat_amount/total_amount` CHECK 를 음수 허용으로 완화 (별도 추가 결정) |
| D-271 | R-V-2 광고비 정책 전수 입력 부담 | seed 단계에서 "전체" 카테고리 1행 기본값 추가 (수용) |
| D-272 | R-V-3 세무사 합류 미확정 | O-003 답변 전까지 EXTERNAL_ACCOUNTANT 라우트·UI 는 비활성 토글 가능하게 (수용) |
| D-273 | R-V-4 부가세 자동분리 | O-005 답변 전까지 total 입력 시 1/11 자동분할 + 사용자 override (수용, Inferred) |
| D-274 | R-V-5 정책 변경 후 인지 부족 | 정책 신규 등록 시 ADMIN/ACCOUNTING 에 이메일 알림 추가 (수용 — BE 보강) |
| D-275 | R-V-6 LOCKED 정산 누적 부담 | 분기 LOCKED 정산 요약 리포트 별도 자동 생성 (Should — 시간 허용 시) |
| D-276 | R-V-7 세무사 코멘트 알림 누락 | O-002 답변 후 일원화. MVP 는 이메일 디폴트 유지 |

---

## 5. 작업 순서 (Work Order) 진행 상태

| # | 단계 | 산출물 | 상태 |
|---|------|--------|------|
| 1 | CLAUDE.md | `CLAUDE.md` | ✅ 완료 |
| 2 | Role Specs | `docs/01~08_*.md` | ✅ 완료 |
| 3 | Handoff Rule | `docs/09_handoff_rule.md` | ✅ 완료 |
| 4 | PM/Architect 통합 | `docs/00_project_overview.md` + `_planning_session.md` + `_schema_design.md` + `_excel_analysis.md` + 본 문서 | ✅ 완료 |
| 5 | Planner PRD | `docs/20_prd.md` | ✅ 완료 |
| 6 | DBA ERD | `docs/21_erd.md` | ✅ 완료 |
| 7 | Backend API/계산 | `docs/22_api_spec.md` | ✅ 완료 |
| 8 | Designer 화면 | `docs/23_design.md` | ✅ 완료 |
| 9 | Frontend 구조 | `docs/24_frontend.md` | ✅ 완료 |
| 10 | DevOps | `docs/25_devops.md` | ✅ 완료 |
| 11 | User/세무 검증 | `docs/26_user_verification.md` | ✅ 완료 |
| 12 | **PM 최종 통합** | `docs/10_decision_log.md` (본 문서) | ✅ 완료 |
| 12-1 | **Open Items 7건 Confirmed + 8역할 합의** | 본 문서 §1-15 ~ §1-23 + ERD/API/FE/DevOps 패치 | ✅ 완료 (2026-06-18) |
| 13 | 실제 코드 생성 | 모든 패키지 | 🏗 **진행 시작 (사용자 승인 완료 — 2026-06-18)** |

---

## 6. 변경 이력

| 일자 | 변경자 | 내용 |
|------|--------|------|
| 2026-06-17 | PM / Architect | 최초 작성. 라운드테이블·기술세션·엑셀분석 결과 종합. Open Items 7건 식별. |
| 2026-06-17 | PM / Architect (최종 통합) | Planner/DBA/BE/Designer/FE/DevOps/User 산출물의 결정 71건(D-200~D-276) 추가. 잔여 위험 7건 처리 방향 명문화. Work Order 12단계까지 완료 표시. |
| 2026-06-18 | 사용자 + 8역할 합의 | Open Items 7건 모두 Confirmed (D-280~D-286). UUID 전환·organization business_type·tax_policy 확장·옵션 정규화·notification_channel·REOPEN 3회·세무사 export MVP 확정. D-082/D-211/D-217/D-273/D-276 supersede. Work Order 12-1 완료, 13단계 코드 생성 진입. |

---

## 7. 다음 갱신 트리거

- 사용자가 Open Items 에 답변할 때마다 → 해당 항목 Confirmed 로 이동, 영향 받는 D-xxx 항목 보강
- Planner / DBA / BE / Designer 가 각 산출물에서 새로운 결정을 내릴 때마다 → 본 문서에 누적
- 결정 번복 시 → 기존 항목에 취소선, 신규 D-xxx 항목 추가, 변경 이력에 사유 기록
