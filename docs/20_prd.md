# 20. PRD — PICKSOME OPS TOOL

> 작성: Planner (기획자)
> 작성일: 2026-06-17
> 입력: `00_project_overview.md`, `_planning_session.md`, `_excel_analysis.md`, `10_decision_log.md`
> 다음 역할: DBA (ERD) + Backend (API/계산 함수) — **병렬 가능**

---

## 0. Reframed Request

엑셀 `마진계산기&장부.xlsx` 의 셀 단위 작업을 **업무 단위 기능**으로 재정의하고,
픽썸 내부 사용자(운영자·소싱·회계세무·외부 세무사) 가 실제로 따라갈 수 있는
**MVP 화면 IA + 사용자 시나리오 + 사용자 스토리 + 완료 기준**을 작성한다.

Open Items 7건은 `[Inferred]` 가정으로 진행하고, 영향 받는 항목에는 가정 출처를 표시한다.

**2026-06-18 갱신**: Open Items 7건 모두 Confirmed (D-280~D-286). 본 PRD 의 가정·미결 표시는 해당 결정으로 대체된다 — 특히:
- P5 외부 세무사 = **Post-MVP 활성** (MVP 는 export 리포트로 대체, D-282)
- 상품 옵션 = **정규화 별도 테이블** (D-286) → US-101 에 옵션 그룹/값/variants 단순 폼 추가
- 정산 REOPEN = **MVP 기본 3회 제한** (D-285) → US-205 REOPEN 모달에 제한 카운터 표시
- 알림 = **EMAIL + IN_APP 2채널** (D-281) → 헤더 벨 아이콘 + 수신함 추가
- 사용자 노출 ID = `business_key` (UUID 노출 금지)

---

## 1. Persona

| Persona | 역할 (RBAC) | 주 업무 | 핵심 통점 (Pain Point) |
|---|---|---|---|
| **P1. 파트너A (대표/운영)** | ADMIN | 상품 소싱·등록가 결정·월별 정산 확정·파트너 분배 송금 | 엑셀 수식 깨짐, 마진계산기와 장부 동기화 안 됨, 세무사에게 매번 수기로 자료 정리 |
| **P2. 파트너B (파트너)** | OPERATOR | 판매장부 입력·반품 처리·재고 확인 | 본인 분배액 근거가 불투명, 실 마진과 예상 마진 차이를 모름 |
| **P3. 운영 보조 (가정)** [Inferred] | SOURCING | 도매꾹/1688 가격 수집·신상품 후보 등록 | 가격 변동 추적 불가, 수기 메모 의존 |
| **P4. 회계 담당 (가정 — 파트너A 겸직 가능)** [Inferred] | ACCOUNTING | 사업비용 입력·증빙 정리·세무사 자료 송부 | 카드명세 ↔ 영수증 짝맞춤이 수기, 부가세 신고 분기마다 야근 |
| **P5. 외부 세무사** [D-282: Post-MVP] | EXTERNAL_ACCOUNTANT (라우트 비활성) | MVP 에서는 픽썸이 export 한 Excel/CSV 리포트로 검토. Post-MVP 시 로그인하여 코멘트·검토상태 변경 | 픽썸이 보내는 자료 포맷이 매번 다름, 정산 확정 여부 불확실 |

---

## 2. MVP 시트 분류 (엑셀 → 신규 시스템)

`_excel_analysis.md` 의 마이그레이션 표를 PRD 관점에서 분류.

| Excel 시트 | MVP 분류 | 신규 시스템 위치 | 비고 |
|---|---|---|---|
| 마진계산 | **MVP Must** | 마진계산기 (`/margin/calculator`) | 4종 마진 타입 통합 |
| 판매장부 | **MVP Must** | 판매장부 (`/sales`) | sales_order |
| 사업비용 | **MVP Must** | 사업비용 (`/expenses`) | 컬럼 대거 확장 |
| 쿠팡정산 | **MVP Must** | 쿠팡 정산 기록 (`/settlements/marketplace`) | 수동 import |
| 월별정산 | **MVP Must** | 월별 정산 (`/settlements/monthly`) | 순이익 기준 재설계 |
| 파트너 분배 | **MVP Must** | 파트너 정산 (월별 정산에 포함) | N명 일반화 |
| 풀필먼트 / 로켓그로스 | **MVP Must** | 마진계산기의 ROCKET_GROWTH 타입 | 정책 테이블화 |
| 구매대행 | **MVP Must** | 마진계산기의 PURCHASE_AGENCY 타입 | 정책 테이블화 |
| 해외직구 | **MVP Must** | 마진계산기의 OVERSEAS_DIRECT 타입 | 정책 테이블화 |
| 일반 리셀 | **MVP Must** | 마진계산기의 GENERAL_RESELL 타입 | 정책 테이블화 |
| 비밀번호 메모 (Sheet12) | **제외** | `password_vault_entry` 로 분리 (보관위치만) | 평문 비번 절대 이관 금지 |
| 임시 메모 / 사적 노트 | **제외** | — | |
| 마진율 표 (참고) | 정책 seed | `target_margin_policy` 초기 데이터 | |
| 거래처 / 공급처 | **MVP Must (간소화)** | 거래처 (`/partners-external`) | MVP는 최소 컬럼 |

---

## 3. Information Architecture (메뉴 구조)

```
PICKSOME OPS TOOL
├── 1. 대시보드                     (/dashboard)
├── 2. 상품                         (/products)
│   ├── 2-1. 상품 목록              (/products)
│   ├── 2-2. 소싱 후보              (/products/sourcing)
│   └── 2-3. 상품 상세 (탭)         (/products/:id)
│        ├── 기본 정보
│        ├── 마진 (예상/실제 토글)
│        └── 판매 이력
├── 3. 마진계산기                   (/margin/calculator)
├── 4. 판매장부                     (/sales)
├── 5. 사업비용                     (/expenses)
├── 6. 정산                         (/settlements)
│   ├── 6-1. 쿠팡 정산 import       (/settlements/marketplace)
│   ├── 6-2. 월별 정산              (/settlements/monthly)
│   └── 6-3. 정산 상세              (/settlements/monthly/:yyyymm)
├── 7. 리포트                       (/reports)
│   ├── 7-1. 월별 리포트
│   ├── 7-2. 판매처별
│   ├── 7-3. 상품별 손익
│   ├── 7-4. 쿠팡 정산 대사
│   ├── 7-5. 부가세 참고 (분기)
│   ├── 7-6. 파트너 수익배분
│   └── 7-7. 증빙 누락
├── 8. 정책                         (/policies)
│   ├── 마켓 수수료 / 결제 / 세금 / 풀필먼트 / 반품위험 / 광고 / 목표마진
│   ├── 정산 / 파트너 분배 / 비용 카테고리 / 증빙 유형
│   └── 정책 변경 이력
├── 9. 거래처                       (/partners-external)
├── 10. 관리                        (/admin)
│   ├── 사용자 / 역할
│   ├── 세무사 초대
│   ├── 감사 로그
│   ├── 알림 설정
│   └── 비밀번호 보관 위치 (Vault)
└── 11. 내 계정                     (/me)
```

**모바일 반응형 범위 (D-113)**: 1, 2-1, 4, 6-2 의 **조회 전용**. 입력·일괄수정은 데스크톱.

---

## 4. User Flow (핵심 시나리오)

### Flow A. 신상품 등록부터 첫 판매까지

```
[SOURCING] 소싱 후보 등록 (도매꾹 URL/공급가/MOQ)
    ↓
[ADMIN/OPERATOR] 후보 → 상품 승격 (product 생성)
    ↓
마진계산기에서 4종 타입 중 선택
    ↓
정책 자동 적용 (마켓수수료, 부가세, 풀필먼트, 반품위험률, 광고)  ← 회색 배경 + i 아이콘
    ↓
원가/판매가 입력 → 예상 마진 계산 + snapshot 박제
    ↓
"등록가 시뮬레이션" 으로 목표마진(target_margin_policy) 기반 추천가 표시
    ↓
판매가 확정 → 상품 상태 ACTIVE
    ↓
[OPERATOR] 첫 판매 시 판매장부에 sales_order 입력 (또는 차후 CSV import)
```

### Flow B. 월별 정산 확정 (핵심)

```
매월 1~5일: 전월 데이터 수합
    ↓
[ACCOUNTING] 쿠팡 정산 CSV import → marketplace_settlement_record 생성
    ↓
[ACCOUNTING] 사업비용 누락 확인 → 증빙 상태 점검
    ↓
[ACCOUNTING] 월별 정산 생성 (DRAFT)
    ├── 매출 합계 (sales_order)
    ├── 상품 직접비 합계 (예상 vs 실제 토글)
    ├── 공통 사업비 합계 (expenses)
    ├── = 순이익
    ├── 적자 carryover 잔액 확인
    └── 우선분배(priority) → 비율 분배 (partner_share_policy)
    ↓
[ACCOUNTING] 매입-매출 대사 리포트로 차이 확인 → 0 이면 통과
    ↓
[ADMIN] 검토 → CONFIRMED
    ↓
[ADMIN] 송금 완료 후 LOCKED (audit_log)
    ↓
세무사에게 리포트 묶음 export (Excel)
```

### Flow C. 분기 부가세 신고 자료 준비

```
3·6·9·12월 25일 D-7 알림 (이메일)
    ↓
[ACCOUNTING] 부가세 참고 리포트 (분기) 자동 생성
    ↓
증빙 누락 리포트로 빠진 영수증 확인
    ↓
[EXTERNAL_ACCOUNTANT] 자료 수신 → 코멘트로 추가 요청 / "전달 완료" 상태 변경
    ↓
[ADMIN] 신고 완료 후 분기 정산 LOCKED
```

### Flow D. 정책값 변경 (예: 쿠팡 수수료율 변경)

```
[ADMIN] 정책 화면에서 marketplace_fee_policy 항목 선택
    ↓
"새 버전" 입력: rate, effective_from
    ↓
저장 시 이전 row 의 effective_to = (new.effective_from - 1day)
    ↓
신규 row 삽입 (version += 1, status = CONFIRMED, source_type = MANUAL)
    ↓
policy_change_history 자동 기록
    ↓
이후 신규 계산은 새 정책 적용. 기존 정산은 snapshot 으로 그대로 유지.
```

### Flow E. 예상 vs 실제 마진 격차 발견

```
[OPERATOR] 대시보드 → "예상 대비 실제 격차 큰 상품 TOP 10" 카드 클릭
    ↓
상품 상세 → 마진 탭 → [예상 | 실제] 토글 비교
    ↓
delta > ±5% 인 항목 빨간 배지
    ↓
원인 분석:
  - 마켓수수료 정책이 오래된 값인가? (NEEDS_REVIEW 배지)
  - 광고비가 예상보다 많이 나갔나?
  - 반품률이 예상보다 높은가?
    ↓
정책 갱신 또는 상품 판매가 조정
```

---

## 5. Feature List (Must / Should / Could)

### 5-1. Must

| # | Feature | 카테고리 | 우선순위 |
|---|---|---|---|
| F-001 | 상품 CRUD + 4종 마진 타입 분류 | 상품 | P1 |
| F-002 | 소싱 후보 등록·승격 | 상품 | P2 |
| F-003 | 마진계산기 (4종 타입 통합 + 정책 자동값) | 마진 | P1 |
| F-004 | 예상 마진 저장 + snapshot 박제 | 마진 | P1 |
| F-005 | 실 마진 계산 (정산 import 후) | 마진 | P1 |
| F-006 | 예상 vs 실제 비교 뷰 + delta 경고 (±5%) | 마진 | P2 |
| F-007 | 등록가 시뮬레이션 (목표마진 기반 추천가) | 마진 | P2 |
| F-008 | 판매장부 입력 + CSV import | 판매 | P1 |
| F-009 | 사업비용 등록 (공급가액/부가세/결제자/사적·사업/증빙) | 비용 | P1 |
| F-010 | 증빙 상태 워크플로우 + 단계별 카운트 | 비용 | P1 |
| F-011 | 쿠팡 정산 CSV import + 자동 매칭 | 정산 | P1 |
| F-012 | 월별 정산 생성·계산 (순이익 기준) | 정산 | P1 |
| F-013 | 적자 carryover 잔액 관리 | 정산 | P1 |
| F-014 | 우선분배 + 비율 분배 (N명) | 정산 | P1 |
| F-015 | 정산 상태 워크플로우 (DRAFT→CONFIRMED→LOCKED, REOPENED) | 정산 | P1 |
| F-016 | 매입-매출 대사 리포트 (쿠팡 정산금 vs 판매장부 합계) | 정산 | P1 |
| F-017 | 정책 12종 CRUD + effective_from/to 관리 | 정책 | P1 |
| F-018 | 정책 변경 이력 통합 화면 | 정책 | P2 |
| F-019 | 정책 신뢰도 상태 배지 (CONFIRMED 등 6종) | 정책 | P2 |
| F-020 | 세무 리포트 7종 + 분기 부가세 (Excel/CSV, 한국어 컬럼) | 리포트 | P1 |
| F-021 | 대시보드 KPI (매출/순이익/마진율/TOP/격차) | 대시보드 | P2 |
| F-022 | RBAC (6역할) + 세무사 초대 | 관리 | P2 |
| F-023 | 감사 로그 (모든 도메인 변경) | 관리 | P1 |
| F-024 | 알림 (이메일): 정산마감/신고일/마진하락/품절위험 | 알림 | P3 |
| F-025 | 거래처 (간소) | 거래처 | P3 |
| F-026 | 비밀번호 보관 위치 vault (절대 평문 금지) | 관리 | P2 |
| F-027 | 인라인 편집 + 일괄 선택·수정 | 횡단 | P2 |
| F-028 | 모바일 반응형 (조회 한정) | 횡단 | P3 |
| F-029 | 엑셀 import / export (한국어 컬럼) | 횡단 | P1 |

### 5-2. Should

| # | Feature | 비고 |
|---|---|---|
| FS-001 | 카카오톡/슬랙 알림 | O-002 답변에 따라 결정 |
| FS-002 | 매입-매출 대사 리포트 상세화 | 차이 사유 자동 분류 |
| FS-003 | 사업비용 CSV 일괄 등록 | 카드명세 paste |

### 5-3. Could

| # | Feature | 비고 |
|---|---|---|
| FC-001 | 운영 task 체크리스트 | 엑셀 task 시트 대체 |
| FC-002 | 다크 테마 | |

### 5-4. Won't (Post-MVP)

`00_project_overview.md` 2.Won't 절 참조.

---

## 6. User Stories + Acceptance Criteria

### Epic E1. 상품 & 마진

#### US-101. [SOURCING] 소싱 후보 등록
- **스토리**: "소싱 담당자로서, 도매꾹/1688 후보 상품을 URL·공급가·MOQ 와 함께 등록해, 파트너A 검토 후 정식 상품으로 승격할 수 있어야 한다."
- **AC**:
  1. 공급처 URL, 공급가, MOQ, 카테고리(자동완성), 메모 필드 입력 가능
  2. 후보 상태: `CANDIDATE` / `APPROVED` / `REJECTED`
  3. ADMIN 만 `APPROVED` 변경 가능. APPROVED 시 product 자동 생성 + 후보의 마진 시뮬레이션 결과 연결
  4. 거절 시 사유 필수 입력
  5. 모든 변경은 audit_log

#### US-102. [OPERATOR] 마진계산기 사용
- **스토리**: "운영자로서, 4종 마진 타입 중 하나를 선택하고 원가/판매가만 입력하면 정책 자동값으로 예상 마진을 즉시 보고 싶다."
- **AC**:
  1. 4종 타입: `PURCHASE_AGENCY`, `ROCKET_GROWTH`, `OVERSEAS_DIRECT`, `GENERAL_RESELL` 셀렉트
  2. 마켓 선택 시 `marketplace_fee_policy` 의 현재 effective row 자동 적용 (회색 배경 + i 아이콘)
  3. i 클릭 시 팝오버: 정책 출처, version, source_type, source_checked_at, 신뢰도 상태
  4. 사용자가 자동값을 수정하면 노란 배경 + "수동 수정됨" 배지, override 값이 snapshot 에 박제
  5. 계산 결과: 마켓수수료, 부가세, 정산금액, 마진금액, 마진율 표시
  6. 저장 시 `product_margin_expected` row + 정책 snapshot 생성
  7. 정책이 `NEEDS_REVIEW` / `EXPIRED` 상태면 상단에 경고 배너

#### US-103. [ADMIN] 등록가 시뮬레이션
- **스토리**: "대표로서, 목표 마진율을 기준으로 추천 판매가를 자동 계산해 등록가를 빠르게 결정하고 싶다."
- **AC**:
  1. 카테고리·마켓 선택 시 `target_margin_policy` 의 목표마진 자동 로드
  2. 원가·수수료·예상광고비 입력 → 추천 판매가 계산
  3. 마진율 슬라이더로 시나리오 비교 (15%/20%/25%/30%/35%)
  4. 결과는 저장하지 않아도 됨 (휘발성), 저장 시 메모 가능

#### US-104. [OPERATOR/ADMIN] 예상 vs 실제 비교
- **스토리**: "운영자로서, 한 상품의 예상 마진과 실 마진을 토글로 비교해 정책이나 운영의 어디가 틀어졌는지 확인하고 싶다."
- **AC**:
  1. 상품 상세 → 마진 탭에 [예상 | 실제] 토글
  2. 비교 뷰에서 두 값 나란히 표시 + 컬럼별 delta 계산
  3. delta 절대값 / 예상값 > 5% 시 해당 행 빨간 배지
  4. 실 마진이 없는 상품(정산 import 전)은 "정산 import 필요" 안내
  5. 대시보드에 "예상 대비 실제 격차 큰 상품 TOP 10" 카드

### Epic E2. 판매 & 정산

#### US-201. [OPERATOR] 판매장부 입력
- **스토리**: "운영자로서, 판매 데이터를 직접 입력하거나 CSV 일괄 업로드해, 마진 계산과 정산의 기초 자료를 만들고 싶다."
- **AC**:
  1. 필드: 판매일, 마켓, 주문번호, 상품, 수량, 판매가, 광고비(선택), 메모
  2. 인라인 편집 (Enter=저장, Esc=취소, Tab=다음 셀)
  3. CSV 업로드: 마켓별 템플릿 제공 (헤더 한국어), 미리보기 후 확정
  4. 중복 주문번호는 경고 + 사용자가 덮어쓰기/skip 선택
  5. 모든 입력에 audit_log

#### US-202. [ACCOUNTING] 쿠팡 정산 import
- **스토리**: "회계세무 담당으로서, 쿠팡 정산 CSV 를 업로드하면 자동으로 판매장부와 매칭되어 실 정산금이 기록되기를 원한다."
- **AC**:
  1. 쿠팡 정산 CSV 업로드 → 파싱 → 미리보기
  2. 주문번호 기준 자동 매칭 (sales_order ↔ marketplace_settlement_record)
  3. 미매칭 건은 별도 섹션에 표시. 수동 매칭 또는 무시 가능
  4. 매칭된 sales_order 는 실 정산금 컬럼이 채워지고 `product_margin_actual` 자동 생성
  5. 동일 정산 파일 재업로드 시 중복 방지 (파일 hash + 정산 기간 키)

#### US-203. [ACCOUNTING] 월별 정산 생성
- **스토리**: "회계세무 담당으로서, 매월 정산 화면에서 한 번에 매출·직접비·공통비·순이익·분배 결과를 보고 싶다."
- **AC**:
  1. 정산 월(YYYY-MM) 선택 → DRAFT 생성 (이미 있으면 불러오기)
  2. 자동 집계: 매출/직접비/공통비/순이익/carryover 잔액
  3. 분배 미리보기: 우선분배 → 비율분배, partner_share_policy 의 해당월 효력 적용
  4. 사용자가 우선분배·비율 override 가능 (사유 입력, audit_log)
  5. 매입-매출 대사: 쿠팡정산금 vs 판매장부 합계 차이 = 0 이면 통과 배지, 아니면 차이 금액 강조

#### US-204. [ADMIN] 정산 확정 잠금
- **스토리**: "대표로서, 정산을 확정한 후에는 그 누구도 수정할 수 없게 잠그고, 정정이 필요하면 별도 이벤트로 처리하고 싶다."
- **AC**:
  1. 상태: `DRAFT` → `CONFIRMED` → `LOCKED`
  2. `CONFIRMED` 이상은 ACCOUNTING 권한도 수정 불가 (UI 비활성)
  3. `REOPENED` 는 ADMIN 만 가능, 사유 입력 필수, audit_log + 알림
  4. `REOPENED` 횟수 [Inferred — O-006] 제한 없음, 단 횟수 카운터 표시
  5. LOCKED 정산은 후속 carryover 계산의 기준값으로 동결

#### US-205. [ACCOUNTING] 적자 carryover 잔액 확인
- **스토리**: "회계세무 담당으로서, 적자월에 누가 얼마를 추가 입금했고 다음 흑자월에 얼마가 차감될지 명확히 알고 싶다."
- **AC**:
  1. 적자월: 분배 0, 손실은 `loss_carryover_ledger` 에 (월, 손실금액, 잔액) 누적
  2. 다음 흑자월에서 우선 차감 (분배 전 자동)
  3. 정산 화면에 carryover 잔액 카드 상시 표시
  4. carryover 이력 별도 페이지 (월별 누적·차감 표)

### Epic E3. 비용 & 증빙

#### US-301. [ACCOUNTING] 사업비용 등록
- **스토리**: "회계세무 담당으로서, 각 사업비 건에 카테고리·증빙·결제자·사적/사업 구분을 입력해 세무사가 그대로 신뢰할 수 있는 장부를 만들고 싶다."
- **AC**:
  1. 필드 (DBA `_schema_design.md` 와 일치):
     - 카테고리 (`expense_category_policy`)
     - 공급가액, 부가세, 합계 ([Inferred — O-005] 합계 입력 시 자동 1/11 분리, 사용자 수정 가능)
     - 결제자(파트너), 사업카드/개인카드
     - 사업용/사적 구분
     - 증빙유형, 증빙상태
     - 세무공제 가능 여부, 정산공제 가능 여부
     - 귀속 파트너(선택)
  2. 카테고리 변경 시 세무공제·정산공제 기본값 자동 채움 (override 가능)
  3. 인라인 편집 + 일괄 선택 + 일괄 수정 (카테고리 일괄변경, 증빙상태 일괄변경)

#### US-302. [ACCOUNTING/EXTERNAL_ACCOUNTANT] 증빙 워크플로우
- **스토리**: "세무사로서, 증빙이 없는 비용을 한눈에 보고 픽썸에 코멘트로 다시 요청하고 싶다."
- **AC**:
  1. 증빙 상태: `증빙없음` → `확인필요` → `증빙있음` → `세무사전달완료`
  2. 상단에 단계별 카운트 배지 (예: "증빙없음 12 / 확인필요 5 / 증빙있음 134 / 전달완료 110")
  3. 각 상태별 필터링
  4. 세무사가 비용 행에 코멘트 작성 가능, 알림 발송
  5. "전달완료" 는 세무사만 변경 가능 [Inferred — O-003 미합류 시 ACCOUNTING 도 가능]

### Epic E4. 정책 관리

#### US-401. [ADMIN] 정책 새 버전 등록
- **스토리**: "대표로서, 마켓 수수료가 변경되면 새 버전을 등록해 과거 정산은 그대로 두고 향후 계산만 새 값으로 적용하고 싶다."
- **AC**:
  1. 정책 화면에서 [+ 새 버전] 클릭 → 폼 입력
  2. effective_from 미래일자/오늘 허용. 과거일자는 경고 (이미 계산된 snapshot 영향 없음 확인)
  3. 저장 시: 이전 effective row 의 effective_to = (new.effective_from - 1day), 신규 row 삽입 (version +=1)
  4. source_type 선택 (MANUAL / CRAWLED / API / IMPORTED), source_url 선택 입력
  5. `policy_change_history` 자동 기록 (변경자, 사유, before/after)
  6. 동시기간 중복 방지 제약

#### US-402. [ADMIN] 정책 신뢰도 검증
- **스토리**: "대표로서, 자동 감지된 정책이나 오래된 정책을 한눈에 확인해 검토 후 CONFIRMED 처리하고 싶다."
- **AC**:
  1. 정책 목록에 신뢰도 배지: `CONFIRMED` / `AUTO_DETECTED` / `NEEDS_REVIEW` / `EXPIRED` / `MANUAL_OVERRIDE` / `DEPRECATED`
  2. 필터: 신뢰도별
  3. `AUTO_DETECTED` → ADMIN 승인 시 `CONFIRMED`
  4. effective_from 이 N일(예: 180일) 이전이면 자동 `NEEDS_REVIEW`
  5. 정책 사용 카운트 (이 정책 snapshot 을 가진 계산 row 수)

#### US-403. [전 역할] 정책 변경 이력 통합 조회
- **스토리**: "감사·세무 검토를 위해 모든 정책 변경 이력을 한 화면에서 검색하고 싶다."
- **AC**:
  1. 통합 화면 `/policies/history`
  2. 정책 종류 / 변경자 / 기간 / source_type 필터
  3. before/after 컬럼 diff 뷰
  4. Excel export

### Epic E5. 리포트

#### US-501. [ACCOUNTING/EXTERNAL_ACCOUNTANT] 세무 리포트 다운로드
- **스토리**: "세무사로서, 분기 또는 연말 신고용 자료를 한 번의 다운로드로 받고 싶다."
- **AC**:
  1. 리포트 종류 (MVP 7종 + 분기 부가세):
     1. 월별 매출/매입/사업비용
     2. 판매처별 매출
     3. 상품별 손익
     4. 쿠팡 정산 대사
     5. 부가세 참고 (월/분기)
     6. 파트너별 수익배분
     7. 증빙 누락
     8. 분기별 부가세 참고
  2. 기간 선택 → 미리보기 → Excel/CSV 다운로드
  3. 파일명 규칙 `YYYY-MM_{리포트명}.xlsx` 또는 `YYYY-Q{n}_{리포트명}.xlsx`
  4. 컬럼명 한국어, 금액은 천 단위 콤마
  5. "일괄 다운로드" 옵션: 분기·연도 단위로 ZIP 묶음

#### US-502. [ADMIN] 리포트 묶음 세무사 전달
- **스토리**: "대표로서, 분기마다 세무사에게 리포트 묶음을 한 번에 전달하고 전달 이력을 남기고 싶다."
- **AC**:
  1. "세무사 전달" 버튼 클릭 → 분기/월 선택 → 자동 ZIP 생성
  2. 세무사 계정에 알림 + 전달 이력 row 생성 (전달자, 전달 일시, 포함된 리포트 종류)
  3. 세무사가 "수신 확인" 클릭 시 상태 업데이트

### Epic E6. 대시보드

#### US-601. [ADMIN/OPERATOR] 대시보드 KPI
- **스토리**: "운영자로서, 한 화면에서 이번 달 매출·순이익·마진율·상위 상품·격차 경고를 보고 싶다."
- **AC**:
  1. 카드:
     - 이번 달 매출 (전월 대비 ↑↓%)
     - 이번 달 순이익 (carryover 잔액 표시)
     - 이번 달 평균 마진율 (목표 대비)
     - 정산 상태 (DRAFT/CONFIRMED/LOCKED 카운트)
  2. 차트:
     - 6개월 매출·순이익 추세 (라인)
     - 카테고리별 마진율 (막대)
  3. 위젯:
     - "예상 대비 실제 격차 큰 상품 TOP 10"
     - "증빙 누락 N건" (클릭 시 사업비용 필터로 이동)
     - "마진율 하락 경고 상품 N건"
     - "다가오는 정산 마감/신고일"
  4. 데이터는 캐시 가능 (10분) — BE 와 협의

### Epic E7. 관리

#### US-701. [ADMIN] 사용자 / 역할 관리
- **스토리**: "대표로서, 사용자를 초대하고 6개 역할 중 하나를 부여하고 싶다."
- **AC**:
  1. 이메일 초대 → 가입 토큰 메일 발송 (만료 72h)
  2. 역할 부여: ADMIN/OPERATOR/SOURCING/ACCOUNTING/READONLY/EXTERNAL_ACCOUNTANT
  3. 비활성화 (soft delete) 가능
  4. 모든 변경 audit_log

#### US-702. [ADMIN] 세무사 초대
- **스토리**: "대표로서, 외부 세무사를 초대해 사업비용 코멘트·증빙 상태 변경만 허용하고 싶다."
- **AC**:
  1. 별도 초대 폼 (역할 = EXTERNAL_ACCOUNTANT 고정)
  2. 접근 범위: 사업비용/정산/리포트 읽기, 사업비용 코멘트, 증빙상태 변경
  3. 정책·상품·관리 영역 접근 불가
  4. 세무사 계정 비활성화 즉시 코멘트 잠금

#### US-703. [ADMIN] 비밀번호 보관 위치 관리
- **스토리**: "대표로서, 쿠팡 Wing 같은 외부 계정의 보관 위치(Bitwarden 항목명 등)를 기록하되 실제 비밀번호는 절대 저장하지 않게 강제하고 싶다."
- **AC**:
  1. 필드: 서비스명(예: 쿠팡 Wing), 보관처(예: Bitwarden), 보관처 항목명, 메모
  2. 비밀번호·계좌번호·인증서번호 입력 필드 없음 (UI/DB 모두)
  3. 메모 내용 평문 비밀번호 패턴 자동 거부 ([Inferred] 정규식 기반 경고)
  4. 모든 조회 audit_log

#### US-704. [ADMIN] 감사 로그 조회
- **스토리**: "대표로서, 누가·언제·무엇을 바꿨는지 검색하고 싶다."
- **AC**:
  1. 필터: 도메인, 사용자, 기간, 작업(CREATE/UPDATE/DELETE)
  2. before / after JSONB diff 뷰
  3. Excel export

#### US-705. [전 역할] 알림 수신
- **스토리**: "운영자로서, 정산 마감 / 부가세 신고 D-7 / 마진율 하락 / 품절 위험 이벤트를 이메일로 받고 싶다."
- **AC**:
  1. MVP 채널: 이메일만 [Inferred — O-002 답변 시 확장]
  2. 사용자별 알림 ON/OFF 설정
  3. 발송 이력 (사용자, 종류, 발송일, 상태) 보관

---

## 7. Non-Functional Requirements

| 분류 | 요구사항 |
|---|---|
| 성능 | 정책 자동 적용 < 200ms, 월별 정산 계산 < 2s, 리포트 export (1년 분량) < 5s |
| 보안 | 비밀번호 평문 저장 금지 (DB·로그·seed), JWT 만료 8h, RBAC 모든 endpoint 적용 |
| 가용성 | MVP는 단일 노드. 일 1회 백업 (DB dump + 첨부물). 복구 RTO 24h |
| 호환성 | 데스크톱: Chrome/Edge 최신 2버전. 모바일 조회: iOS Safari/Chrome 최신 2버전 |
| 접근성 | 키보드 전 기능 조작 가능 (인라인 편집 Tab/Enter/Esc) |
| 다국어/통화 | MVP는 한국어 + KRW 고정 (Post-MVP 확장 슬롯만) |
| Timezone | DB UTC 저장, 화면 Asia/Seoul 표시 |
| 감사 | 모든 도메인 mutation 은 audit_log row 1개 이상 |

---

## 8. Out of Scope (재확인)

- 세무 신고 자동화·전자 신고 연계
- 결제/송금 직접 처리 (은행 API 연동)
- 판매 사이트 운영 (상품 페이지 호스팅 등)
- 영수증 파일 첨부 UI (DB·UI 슬롯만 준비, 첨부 처리는 Post-MVP)
- 자동 크롤링 / 외부 API 자동 import (MVP는 수동 CSV)
- AI/OCR 비용 자동 분류
- 다국어/다통화 UI

---

## 9. Open Items 영향 정리 (Planner 관점)

| Open Item | 영향 받는 US/AC | Inferred 가정 |
|---|---|---|
| O-001 사업 형태 | US-701, US-204 | 공동사업자 가정. 단독+직원이면 RBAC 기본 매핑만 단순화 |
| O-002 알림 채널 | US-705 | 이메일만. 답변 후 카톡/슬랙 추가 |
| O-003 세무사 합류 | US-302, US-501, US-502, US-702 | 합류 가정. 미합류 시 EXTERNAL_ACCOUNTANT 활성도만 낮춤 |
| O-004 ID 타입 | (DBA 영향, PRD 무관) | — |
| O-005 부가세 자동분리 | US-301 | 자동 1/11 분리 + 사용자 override 가능 |
| O-006 REOPENED 횟수 | US-204 | 횟수 제한 없음, 카운터만 표시 |
| O-007 옵션 슬롯 사전준비 | (DBA 영향, PRD 무관) | — |

---

## 10. Handoff

### Summary
8개 Epic, 29개 Must Feature, 25+ User Story 와 AC 작성. 5개 핵심 사용자 흐름 정의.
모바일은 조회 4화면 한정. Open Items 7건은 Inferred 라벨로 가정 진행.

### Decisions (Planner 단독 결정)
- IA 11개 1차 메뉴 + 하위 구조 확정
- 모바일 반응형 4화면 한정
- 마진 타입 4종 → 단일 마진계산기에 통합 (탭 또는 셀렉트)
- 세무 리포트 8종(MVP 7 + 분기 부가세) 확정
- 인라인 편집 키 바인딩 표준화 (Enter/Esc/Tab)

### Open Questions (User 결정 필요)
- Open Items 7건 (`10_decision_log.md` 동일)
- 추가: 거래처 정보 어디까지 (사업자번호·계좌 보관 위치만? 연락처는?)
- 추가: 알림 수신 사용자 단위 vs 역할 단위 default

### Risks
- US-104 예상/실제 비교의 ±5% 임계값은 픽썸 카테고리 다양성 고려 시 조정 필요 가능 (정책 테이블화 고려)
- US-203 월별 정산 자동 집계 성능 — 1년 누적 데이터에서 < 2s 가능 여부 BE/DBA 확인
- US-301 사업비용 컬럼 수가 많음 — Designer 가 입력 흐름을 잘 설계해야 함 (기본값 자동 채움 의존도 높음)
- 정책 12종 초기 seed 부담 — 마이그레이션 마법사 또는 기본 seed 필수

### Required Inputs for Next Role
- **DBA**: 본 PRD 의 User Story 기반으로 ERD 작성. 특히 sales_order, marketplace_settlement_record, monthly_settlement, partner_settlement, loss_carryover_ledger, expenses, audit_log, policy_snapshot_*, password_vault_entry 의 컬럼·제약·인덱스 확정.
- **Backend**: User Story 의 AC 를 API endpoint 와 service 함수로 매핑. 핵심 계산 함수 시그니처는 `_schema_design.md` 기준. 정책 적용·snapshot 박제·delta 계산 로직 구현 명세.
- **Designer**: 5개 User Flow 와 IA 기준 와이어프레임. 정책 자동값 UX, [예상 | 실제] 토글, 인라인 편집 표준, 모바일 4화면 가이드라인.
- **Frontend**: IA 기반 라우팅, 페이지/컴포넌트 트리. API spec 은 BE 산출물 대기.
- **DevOps**: 알림(이메일), 백업, 환경변수, Docker Compose 4 서비스(FE/BE/DB/Reverse Proxy).

### Handoff Target
**DBA + Backend (병렬)** → Designer → Frontend → DevOps → User/세무 검증

---

## 11. Action Checklist

- [x] Persona 5명 정의
- [x] Excel 시트 분류 (MVP/제외)
- [x] IA 11개 1차 메뉴 확정
- [x] 5개 User Flow 작성
- [x] 29개 Must Feature 우선순위
- [x] 7개 Epic / 25+ User Story / 모든 AC 작성
- [x] Non-Functional 정리
- [x] Open Items Inferred 가정 표시
- [x] Handoff 작성
- [ ] (사용자) Open Items 7건 답변 → `10_decision_log.md` 갱신
- [ ] (Next) DBA: `docs/21_erd.md` 작성
- [ ] (Next) Backend: `docs/22_api_spec.md` 작성
