# 00. Project Overview — PICKSOME OPS TOOL

> 작성: PM / Architect
> 작성일: 2026-06-17
> 입력: `_excel_analysis.md`, `_planning_session.md`, `_schema_design.md`, 사용자 결정 6건 + 추가 요구사항 2건

---

## 1. Vision & Goal

### 1차 목표 (MVP)
**픽썸 내부 운영 SaaS**:
- 엑셀 `마진계산기&장부.xlsx` 의 업무 프로세스를 프로그램화
- 마진계산·판매장부·사업비용·월별정산·세무사 제출용 리포트 자동화

### 최종 목표 (Post-MVP)
**범용 셀러 운영 자동화 SaaS**:
- 다른 셀러도 사용 가능
- 마켓 정산·공급처 가격 자동 수집
- 정책값 자동 감지 + 관리자 승인
- 세무사 협업 모드

### 비목표 (Out of Scope)
- 세무 신고 자동 대체
- 판매 사이트 자체 운영
- 결제 모듈

---

## 2. MVP Scope

### Must (반드시 포함)

#### A. 도메인
1. **상품 관리** (`product`) + 소싱 후보 (`product_sourcing_candidate`)
2. **마진 계산기** — `PURCHASE_AGENCY` / `ROCKET_GROWTH` / `OVERSEAS_DIRECT` / `GENERAL_RESELL`
3. **예상 마진** (`product_margin_expected`) — 정책 자동값 + 수동 override
4. **실마진** (`product_margin_actual`) — 정산 데이터 import 후 계산 (MVP는 수동 import)
5. **등록가 시뮬레이션** — 목표마진 기반 추천가
6. **판매 장부** (`sales_order`)
7. **사업비용** (`expenses`) — 카테고리/공급가액/부가세/결제자/사적·사업 구분/증빙
8. **쿠팡 정산 기록** (`marketplace_settlement_record`) — MVP는 수동 입력
9. **월별 정산** (`monthly_settlement` + `partner_settlement`) — 순이익 기준, N명 파트너, 적자 carryover
10. **세무사 제출용 리포트** (Excel 다운로드)
11. **대시보드** — 월 매출/순이익/마진율 추세, 상위 상품, 격차 경고

#### B. 횡단 기능
12. **정책 관리** — 12종 정책 테이블의 등록/변경/이력 화면
13. **RBAC** — 5개 역할(관리자/운영/소싱/회계세무/읽기) + 세무사 초대
14. **감사 로그** — 모든 도메인 변경 기록
15. **정산 확정 잠금** — DRAFT → CONFIRMED → LOCKED, REOPENED은 audit 동반
16. **인라인 편집 + 일괄 수정 테이블**
17. **모바일 반응형 (조회 한정)**
18. **엑셀 import / export**
19. **분기별 부가세 참고 리포트** (분기말 자동 생성)
20. **알림** (이메일 한정): 정산 마감 / 신고 일정 / 마진 하락 / 품절 위험

### Should (가능하면 포함)
- 카카오톡/슬랙 알림
- 매입-매출 대사 리포트(상세)
- 대량 일괄 처리 (CSV 일괄 등록 등)

### Could (시간 남으면)
- task 관리 (운영 체크리스트)
- 다크 테마

### Won't (Post-MVP)
- 자동 크롤링(공급처/마켓 수수료)
- 쿠팡/스마트스토어 API 자동 import
- 영수증 파일 첨부 (DB·UI 슬롯은 준비)
- 다국어/다통화 UI
- 상품 옵션 (사이즈/색상) — DB 슬롯만 준비
- 온보딩 마법사 (SaaS화)
- 세무사 통합 대시보드
- AI OCR 비용 자동 분류
- 자금 흐름 시뮬레이션

---

## 3. Tech Stack (확정)

- Frontend: **Next.js (App Router) + React + TypeScript + Tailwind**
- Backend: **FastAPI + Python 3.12+ + SQLAlchemy 2 + Pydantic v2 + Alembic**
- Database: **PostgreSQL 16**
- Excel: **openpyxl** (분석) + **xlsxwriter** (export, 한국어 포맷)
- DevOps: **Docker Compose** (FE / BE / DB / Reverse Proxy)
- 인증: 자체 (이메일+비번 + JWT)
- 시각: TIMESTAMPTZ (UTC 저장 / Asia/Seoul 표시)
- 통화: KRW (정수)

---

## 4. 핵심 모델 요약 (상세는 `_schema_design.md`)

### 4-1. 정책 테이블 패밀리 (12종)
| 테이블 | 용도 |
|---|---|
| `marketplace_fee_policy` | 마켓·카테고리별 판매 수수료 (RATE/FIXED/TIERED) |
| `payment_fee_policy` | 결제수단 수수료 |
| `tax_policy` | 세금률·과세유형(GENERAL/SIMPLE/EXEMPT) |
| `fulfillment_fee_policy` | 풀필먼트 입고/출고/보관/반품처리 |
| `return_risk_policy` | 반품 위험률 (마켓·카테고리별) |
| `ad_cost_policy` | 광고비 기본값·ROAS |
| `target_margin_policy` | 카테고리별 목표 마진율 + 경고 임계값 |
| `settlement_policy` | 사업비/우선분배/적자 정책 |
| `partner_share_policy` | 월별·파트너별 분배 비율 |
| `expense_category_policy` | 비용 카테고리 (세무공제 기본/정산공제 기본) |
| `evidence_type_policy` | 증빙 유형 (공식/비공식) |
| `policy_change_history` | 모든 정책 변경 통합 로그 |

**공통 컬럼**: `version`, `effective_from`, `effective_to`, `source_type`, `approved_by/at`, audit.
**신뢰도 상태**: `CONFIRMED | AUTO_DETECTED | NEEDS_REVIEW | EXPIRED | MANUAL_OVERRIDE | DEPRECATED`.

### 4-2. Snapshot 패턴
모든 계산 결과는 **정책 snapshot** 을 박제한다.
- FK + version (정상 조회용)
- JSONB 박제 (스키마 변경 방어)

### 4-3. 정산 모델
```
순이익 = 매출 - 상품 직접비(원가/수수료/광고/반품/풀필먼트/배송) - 공통 사업비(expenses 합)
분배 = 우선분배(priority) 후 → 비율 분배 (N명)
적자 = carryover (다음 흑자월 우선 차감)
확정 = DRAFT → CONFIRMED → LOCKED (REOPENED 시 audit 동반)
```

### 4-4. 세무 리포트 (MVP 필수)
- 월별 매출 / 매입 / 사업비용 / 판매처별 매출 / 상품별 손익
- 쿠팡 정산 대사 / 부가세 참고(분기) / 파트너별 수익배분 / 증빙 누락
- Excel/CSV 다운로드, **한국어 컬럼명**
- 파일명 규칙: `YYYY-MM_{리포트명}.xlsx`

---

## 5. RBAC

| 역할 | 권한 요약 |
|---|---|
| **ADMIN** | 전체. 사용자 관리, 정책 승인 |
| **OPERATOR** | 상품·판매장부·마진계산 입력/수정 |
| **SOURCING** | 소싱 후보·상품 등록 |
| **ACCOUNTING** | 사업비용·정산·세무 리포트 |
| **READONLY** | 대시보드·장부·정산 조회 |
| **EXTERNAL_ACCOUNTANT** | 읽기 + 코멘트 + 증빙상태 변경 (세무사) |

MVP에서 화면을 다 안 만들어도 **DB·서비스 계층은 role 기반 접근제어 동작** 해야 한다.

---

## 6. 외부 연동 로드맵

| 단계 | 대상 |
|---|---|
| **MVP** | 수동 입력 + 엑셀 import/export |
| **Phase 2** | 쿠팡 정산 import (CSV 업로드 → 자동 매칭) |
| **Phase 3** | 쿠팡/스마트스토어 API 직접 연동 |
| **Phase 4** | 도매꾹/1688 가격·재고 크롤링 (야간 배치) |
| **Phase 5** | 정책값 변경 감지 (관리자 승인 워크플로우) |
| **Phase 6** | 카카오/슬랙 알림, 자동 정산 워크플로우 |

DB·BE 인터페이스는 처음부터 다음을 갖춘다:
`integration_account`, `integration_job`, `batch_run`, `batch_run_log`,
`external_product_mapping`, `supplier_price_snapshot`,
`marketplace_order_import`, `marketplace_settlement_import`.

---

## 7. 보안 핵심

- **비밀번호·계좌비번·카드비번·인증서 비번·주민번호 저장 절대 금지**
- `password_vault_entry` 는 **보관 위치만** (예: "Bitwarden - 쿠팡 Wing")
- DB CHECK 제약으로 평문 비밀번호 패턴 거부 (트리거)
- 로그·에러 메시지에서 민감정보 마스킹
- DB seed/샘플은 가짜 데이터만
- 기존 엑셀의 비밀번호는 **이관 금지** + 사용자 별도 안내(`_excel_analysis.md` 참조)

---

## 8. 계산 규칙 (재확인)

- 금액: `BIGINT`(원) 또는 `Decimal`. `float` 금지.
- 비율: `Decimal(7,4)`
- 마진/정산 핵심 로직: **BE service layer**
- 모든 계산 결과에 **정책 snapshot 박제**
- 엑셀 수식은 **명명된 Python 함수**로 재작성

---

## 9. 역할 작업 순서 (확정)

```
PM/Architect (✅ 본 문서)
    ↓
Planner (PRD 작성) ← Next
    ↓
DBA (ERD + DDL 확정)  ──┐
Backend (API + 계산 함수) ─┤  병렬 가능
    ↓
Designer (와이어프레임 + 디자인 시스템)
    ↓
Frontend (라우팅 + 컴포넌트 트리 + API 연동 명세)
    ↓
DevOps (Docker Compose + .env + 백업 + 배포)
    ↓
User/세무 검증 (실 데이터 + 리포트 검증)
    ↓
PM/Architect 최종 통합 + Decision Log 갱신
    ↓
실제 코드 생성 (사용자 승인 후)
```

---

## 10. 리스크 & 완화

| 리스크 | 영향 | 완화 |
|---|---|---|
| MVP 범위 팽창 (세무 리포트·정책 12종·RBAC·감사로그) | 일정 지연 | Should/Could 단계 미루기, 정책 seed 미리 준비 |
| 정책 12종 초기 설정 부담 | 사용성 저하 | 마이그레이션 마법사 + 기본 정책 seed |
| 사업 형태 미확정 | 세무 처리·정산 표현 | **사용자 즉답 필요** (공동/단독) |
| 적자 정책의 세무적 합법성 | 분쟁 가능 | 세무사 검토 의무화 + 정책 변경 이력 |
| Audit log 증가 | DB 크기 | 월별 파티셔닝 + 보관 정책(예: 2년) |
| snapshot JSONB 비대 | 행 크기 | TOAST 압축, 큰 snapshot 분리 저장 |
| 정책 row hard delete 시 snapshot 깨짐 | 데이터 무결성 | hard delete 금지 + soft delete + DEPRECATED |

---

## 11. 미확정 / 사용자 결정 필요 (Open Items)

다음은 PM이 단독 결정할 수 없고, 사용자 결정이 필요한 항목 (`docs/10_decision_log.md` 참조):

1. **사업 형태**: 공동사업자(공동대표) vs 단독+직원/외주 — RBAC/세무/정산 표현에 영향
2. **알림 채널 우선순위**: 이메일만(MVP) vs 카카오톡/슬랙 동시
3. **세무사 실 합류 여부**: 세무사 계정 초대 기능을 실제로 사용할 것인가
4. **ID 타입**: BIGSERIAL (간단) vs UUID (SaaS 확장 친화)
5. **공급가액/부가세 자동 분리 정책**: total만 입력 시 자동 분할 적용?
6. **정산 REOPENED 횟수 제한**: 무제한 vs N회 제한
7. **product 옵션 슬롯 사전 준비 여부**: Post-MVP라도 DB에 컬럼 미리 둘까

---

## 12. 다음 단계 (Immediate Next)

1. 위 Open Items 답변 → `docs/10_decision_log.md` 에 반영
2. **Planner 역할로 진행** → `docs/20_prd.md` (PRD: 사용자 시나리오, 화면별 사용자 스토리, 완료 기준)
3. Planner 끝나면 DBA + Backend 병렬 가능 (`docs/21_erd.md`, `docs/22_api_spec.md`)
4. Designer → Frontend → DevOps → User/세무 검증
5. 최종 통합 후 코드 생성

---

## 참고 문서

- `_excel_analysis.md` — 엑셀 원본 분석 (마이그레이션 매핑·매직넘버 표 포함)
- `_planning_session.md` — Planner+Designer+User/세무 라운드테이블 결과
- `_schema_design.md` — DBA+BE 합동 기술 세션 결과 (정책 테이블 상세 DDL, 계산 함수 시그니처, 의사코드)
- `09_handoff_rule.md` — 역할 간 인수인계 템플릿
- `10_decision_log.md` — 결정·미결정 누적 로그
- `01_pm_architect_role.md` ~ `08_user_accounting_role.md` — 역할 스펙
