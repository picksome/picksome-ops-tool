# PICKSOME OPS TOOL — Claude Working Rules

## Project Purpose

PICKSOME OPS TOOL은 기존 `마진계산기&장부.xlsx`를 기반으로
상품 소싱, 마진 계산, 등록가 시뮬레이션, 판매 장부, 사업비용,
쿠팡 정산 기록, 월별 정산, 파트너별 수익배분, 세무사 제출용 리포트,
운영 Task를 관리하는 **셀러 운영관리 SaaS**다.

**1차 목표**: 픽썸 내부 운영툴.
**최종 목표**: 다른 셀러도 사용 가능한 **범용 셀러 운영 자동화 툴**.

## Core Principle

엑셀 화면을 그대로 복사하지 않는다.
엑셀에 들어 있는 **업무 프로세스와 계산 로직**을 분석해서
프로그램 구조로 재설계한다.

## Generalization Principle (범용화 원칙)

**모든 비즈니스 값은 하드코딩하지 않는다.** 다음은 전부 정책 테이블로 관리한다.

- 사용자명·파트너명 → `partner` 테이블
- 마켓별 수수료율 → `marketplace_fee_policy`
- 결제 수수료 → `payment_fee_policy`
- 세금률·과세 유형 → `tax_policy`
- 로켓그로스 입고/출고/보관비 → `fulfillment_fee_policy`
- 반품 위험비율 → `return_risk_policy`
- 목표 마진율 → `target_margin_policy`
- 정산·수익 배분 비율 → `settlement_policy`, `partner_share_policy`
- 비용 카테고리 → `expense_category_policy`
- 증빙 유형 → `evidence_type_policy`

**정책은 effective_from / effective_to + version**을 가져
과거 시점의 계산을 재현 가능해야 한다.

## Tech Stack

- Frontend: Next.js, React, TypeScript
- Backend: FastAPI, Python
- Database: PostgreSQL
- DevOps: Docker Compose
- Timezone: Asia/Seoul
- Currency: KRW

## Security Rules

- 비밀번호, 계좌정보, 인증서 정보, 카드/보안카드 정보 등 **민감정보를 평문 저장하지 않는다**.
- 프로그램에는 **실제 비밀번호를 저장하지 않고 보관 위치만** 저장한다 (예: "Bitwarden - 쿠팡 Wing").
- 민감정보를 **로그에 출력하지 않는다**.
- DB seed/샘플 데이터에 실제 계정정보를 넣지 않는다.
- 주민등록번호 등 고위험 개인정보는 저장하지 않는다.

## Calculation Rules

- 금액 계산에 `float`를 사용하지 않는다. **`int`(원 단위) 또는 `Decimal`** 만 사용한다.
- 핵심 계산 로직(마진·정산·수익배분)은 FE가 아닌 **BE service layer**에서 처리한다.
- 엑셀 수식을 그대로 복사하지 않고 **명명된 Python 함수**로 재작성한다.
- 계산 결과에는 **사용된 정책 버전(snapshot)** 을 기록해 과거 재현이 가능해야 한다.

## Settlement Model (정산 / 수익배분 모델)

**기본 원칙**: 수익배분은 매출이 아닌 **모든 비용을 차감한 순이익** 기준이다.

```
순이익 = 매출
       - 상품 직접비 (원가 + 마켓수수료 + 광고비 + 반품비 + 풀필먼트비 + 배송비)
       - 사업 공통비 (expenses 테이블의 월별 합계)
```

- **분배 대상**: 위 순이익. 매출이나 상품별 마진 단독이 아니다.
- **분배 방식**: N명 파트너, 비율 합 = 1.0. 기본 1:1, 월별 override 가능.
- **우선 분배**(priority_distribution): 비율 계산 전 특정 파트너에게 정액을 먼저 배정.
- **적자 정책**: 기본 `carryover` — 적자월은 분배 0, 손실은 누적되어 다음 흑자월에서 우선 차감.


## Tax Report (세무 리포트) — MVP 필수

**이 프로그램은 세무 신고를 자동 대체하지 않는다.**
세무사가 검토·신고에 사용할 수 있는 수준의 **구조화된 자료**를 제공한다.

MVP 필수 리포트:
- 월별 매출 / 매입 / 사업비용 리포트
- 판매처별 매출 리포트
- 상품별 손익 리포트
- 쿠팡 정산 대사 리포트
- 부가세 참고 리포트
- 파트너별 수익배분 리포트
- 증빙 누락 리포트

모든 리포트는 **Excel/CSV 다운로드**, 컬럼명은 **한국어**.
파일명 규칙 예: `2026-06_월별매출리포트.xlsx`.

## Expense + Evidence (사업비용·증빙) — MVP 필수

- `expenses` 테이블은 단순 메모가 아닌 정산·수익배분의 입력이다.
- 각 비용 건은 **증빙 유형 + 증빙 상태**를 가진다.
- MVP에서 파일 첨부까지 못 가도, **증빙 유형과 상태는 반드시** DB에 저장한다.
- 파일 첨부는 MVP 이후 확장.

## RBAC (권한 모델)

사용자 수는 가변. 최소 5개 역할 구조로 설계:
- 관리자 / 운영 담당자 / 상품 소싱 / 정산·세무 / 읽기 전용

MVP에서 권한 화면을 완성하지 않더라도 **DB·코드는 role 기반 접근제어가 가능한 구조**.

## External Integration Roadmap

MVP에서는 **수동 입력 + 엑셀 import/export** 만 구현.
이후 확장: 쿠팡/스마트스토어 API, 도매꾹/1688 크롤링, 야간 배치, 가격·재고 변동 알림.
구조적으로는 처음부터 확장 가능하게 설계:
`integration_account`, `integration_job`, `batch_run`, `batch_run_log`,
`external_product_mapping`, `supplier_price_snapshot`,
`marketplace_order_import`, `marketplace_settlement_import`.

## Working Rule

Claude는 한 번에 모든 역할을 섞어서 작업하지 않는다.
**항상 현재 역할을 명시**하고, 해당 역할의 책임 범위 안에서만 산출물을 작성한다.

**사용자가 승인하기 전까지 코드를 작성하지 않는다.**

## Skill Rule

`.claude/skills/structured-thinking` 스킬을 **항상 적용**한다.

- 요청을 받으면 먼저 reframed request로 다시 정리한다.
- 모르는 정보는 [Confirmed] / [Inferred] / [Uncertain] 라벨로 구분한다.
- 분석만으로 끝내지 않고 항상 **구체적 산출물**을 만든다.
- 마지막에는 **action checklist**로 닫는다.

## Roles (8개)

| # | Role | File |
|---|------|------|
| 0 | PM / Architect | `docs/01_pm_architect_role.md` |
| 1 | Planner (기획자) | `docs/02_planner_role.md` |
| 2 | Designer | `docs/03_designer_role.md` |
| 3 | Frontend | `docs/04_frontend_role.md` |
| 4 | Backend | `docs/05_backend_role.md` |
| 5 | DevOps / SE | `docs/06_devops_role.md` |
| 6 | DBA | `docs/07_dba_role.md` |
| 7 | User / 세무 담당자 | `docs/08_user_accounting_role.md` |

## Work Order

1. CLAUDE.md 작성 ✅
2. Role Spec MD 작성 (docs/01~08) ✅
3. Handoff Rule 작성 (docs/09) ✅
4. PM / Architect 산출물 작성 (docs/00_project_overview.md) ✅
5. Planner PRD 작성 (docs/20_prd.md) ✅
6. DBA ERD 작성 (docs/21_erd.md) ✅
7. Backend API/계산 로직 작성 (docs/22_api_spec.md) ✅
8. Designer 화면 설계 (docs/23_design.md) ✅
9. Frontend 화면 구조 작성 (docs/24_frontend.md) ✅
10. DevOps 실행 구조 작성 (docs/25_devops.md) ✅
11. User/세무 검증 (docs/26_user_verification.md) ✅
12. PM 최종 통합 (docs/10_decision_log.md) ✅
12-1. **Open Items 7건 Confirmed + 8역할 합의 + ERD/API/FE/DevOps 패치 (2026-06-18)** ✅
13. **실제 코드 생성** ← 진입 시작 (사용자 승인 완료)

## Resume Point (다음 세션 진입점)

**설계 + Open Items 7건 모두 완료 (2026-06-18). 코드 생성 단계 진입.**

확정된 핵심 결정 (D-280~D-286):
- D-283 모든 PK/FK = UUID, 사용자 노출 ID 는 `business_key` 별도
- D-280 organization.business_type (SOLE/JOINT/CORP/TEAM) + N명 파트너 분배
- D-281 MVP 알림 = EMAIL + IN_APP (notification_channel 메타 + 채널 어댑터)
- D-282 세무사 export 7종 MVP, EXTERNAL_ACCOUNTANT 라우트는 Post-MVP
- D-284 tax_policy 확장 + expenses.vat_inclusion_type + tax_policy_id snapshot
- D-285 monthly_settlement.reopen_count, MVP 기본 3회 (org 단위 override)
- D-286 product 옵션 정규화 (option_group/value/variant 3테이블)

**다음 세션 시작 시 (코드 생성 권장 순서)**:
1. DB 초기 마이그레이션 (Alembic M001 — extension pgcrypto, enum 25종, 약 42 테이블, UUID PK 전체)
2. seed (M002 — organization/partner/notification_channel/정책 9건)
3. Backend 핵심 계산 함수 6종 (`resolve_active_policies`, `calculate_expected_margin`, `calculate_actual_margin`, `calculate_monthly_settlement`, `simulate_recommended_prices`, `generate_tax_report`) + 단위테스트
4. Backend API endpoint (정책/마진/사업비용/정산/리포트/옵션 순)
5. Frontend AppShell + DataTable + 마진계산기 + 사업비용 + 월정산 + 헤더 벨/옵션 그리드
6. DevOps Docker Compose + .env.example (SMTP_*, FEATURE_* 플래그)

모든 코드 작성 시 **structured-thinking 스킬 + 8역할 분리 원칙 + UUID 노출 금지 + 정책 snapshot 박제** 유지.

## Handoff Rule

모든 역할은 작업을 마칠 때 `docs/09_handoff_rule.md`에 정의된
공통 Handoff 템플릿을 따라 다음 역할에게 인수인계한다.

## Reference Docs

- `docs/_excel_analysis.md` — 엑셀 원본 자동 분석 결과
- `docs/10_decision_log.md` — 확정 결정·미확정 사항 누적 기록
