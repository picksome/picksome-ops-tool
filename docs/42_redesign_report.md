# 42. OPS 콘솔 개편 — 완료 보고서

> **브랜치**: `feat/ops-console-redesign` (커밋 4개)
> **작업 기간**: 2026-08-05 ~ 08-06
> **상태**: 프론트엔드 개편 15단계 전부 완료. `next build` 통과 (46 라우트)
> **관련 문서**: 기획 `40_ops_improvement_plan.md` / 상세 작업 로그 `41_redesign_worklog.md`

---

## 1. 실행 및 테스트 방법

```powershell
# 백엔드 (선택 — 없어도 mock 으로 전 화면 동작)
cd D:\picksomeprod\picksome-ops-tool\backend
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 프론트엔드
cd D:\picksomeprod\picksome-ops-tool\frontend
npm run dev            # http://localhost:3000

# 검증
npx tsc --noEmit       # 타입
npx next lint          # 린트
npx next build         # 프로덕션 빌드 (46 라우트)
```

**확인 순서 (10분 코스)**

1. `/dashboard` — 기간 프리셋을 바꿔 KPI·차트가 같이 움직이는지. 경고 항목 클릭 → 필터 걸린 목록으로 이동
2. `/products` — 검색 `쿨링 스카프`(띄어쓰기 포함) → 상품명 클릭 → **오른쪽 drawer 가 불투명하게** 열리는지
3. drawer `가격·마진` 탭 — 판매가를 바꾸면 마진·역산가·계산 근거가 즉시 갱신되는지
4. 목록 `옵션` 열의 숫자 클릭 → 옵션 모달 → 그룹 추가 → `조합 생성` → 일괄 수정
5. `/tools/calculator` — `다른 과세유형과 나란히 비교` 체크 → 일반/간이 세금 차이 확인
6. `/settlements/distribution` → 시뮬레이션 탭 → 가정 순이익 입력 → 참여자 유형을 직원↔공동사업자로 바꿔 결과 비교
7. `/expenses` — 비용 등록 → 파일 첨부(10MB 초과 파일로 거부 메시지도 확인)
8. 브라우저 폭을 768px 아래로 → 사이드바가 헤더 메뉴로 바뀌는지

---

## 2. 변경한 파일

### 수정 (기존 자산 유지, 확장만)

| 파일 | 변경 |
|---|---|
| `app/globals.css` | `--color-popover` 토큰 추가(팝업 투명 원인), 레이아웃/간격/손익 색 토큰, `.ops-*` 공통 클래스, reduced-motion |
| `app/(app)/layout.tsx` | 바깥 패딩 제거(sticky 필터), skip link |
| `components/shell/sidebar.tsx` | 전면 재작성 — 16항목 6그룹, `MobileNav` 추가 |
| `components/shell/header.tsx` | 재작성 — 64px 고정, 브랜드 이동, 검색 진입점 상시 노출, 모바일 메뉴 |
| `components/ui/dialog.tsx` / `sheet.tsx` | 오버레이 `/10`→`/40`+blur, Sheet 폭을 `[--sheet-w]` 변수화 |
| `components/ui/empty-state.tsx` | `action` 이 ReactNode 도 허용 |
| `components/table/data-table.tsx` | **기존 props 전부 유지** + 열 표시/순서, 외부 정렬, 오류 상태, density, `meta.align` |
| `lib/format.ts` | 기존 4함수 유지 + 축약/부호/상대시간/공급처 라벨 등 8종 |

### 신규 — 라이브러리

| 파일 | 역할 |
|---|---|
| `lib/money.ts` | 원 단위 정수 산술, bp 기반 비율 합, round-half-up, VAT 분리, 잔수 분배 |
| `lib/date-range.ts` | KST 기준 프리셋 6종, 직전 동일 길이 기간, 버킷 자동 선택 |
| `lib/calc/types.ts` | `TaxRule` / `MarketplaceFeePolicy` / 계산 입출력 타입 |
| `lib/calc/engine.ts` | `calculateMargin` / `resolveFeePolicy` / `resolveTaxRule` / `compareMarketplaces` |
| `lib/calc/distribution.ts` | 배분 waterfall, 참여자·지급방식 라벨, 기간 합산 |
| `lib/adapters/types.ts` | 개편 도메인 타입 전체 |
| `lib/adapters/index.ts` | product/sales/settlement/expense/settings/distribution/notification/integration 어댑터 |
| `lib/adapters/mock-data.ts` | 현실적 한국어 mock (이상 케이스 포함) |
| `lib/adapters/dashboard.ts` | 대시보드 집계 |
| `hooks/useListQuery.ts` | 목록 상태 ↔ URL 동기화 |

### 신규 — 컴포넌트

`components/filters/{date-range-filter,filter-bar}.tsx` ·
`components/table/table-toolbar.tsx`(열 설정/저장된 보기/CSV) ·
`components/shell/page-shell.tsx`(PageShell/PageTabs/Section/Field) ·
`components/ui/money-input.tsx`(MoneyInput/PercentInput/TextInput) ·
`components/charts/primitives.tsx` · `components/dashboard/kpi-tile.tsx` ·
`components/calc/calc-breakdown.tsx`(근거표/요약/역산/도움말 drawer) ·
`components/products/{product-drawer,option-modal}.tsx` ·
`components/notifications/channels-panel.tsx`

### 신규 — 화면

`/dashboard`(재작성) · `/products`(재작성) · `/sales`(재작성) · `/tools/calculator` ·
`/settlements/marketplace`(재작성) · `/settlements/distribution` · `/expenses`(재작성) ·
`/notifications`(재작성) · `/integrations/{messaging,marketplaces}` ·
`/settings/{profile,members,tax,expense-categories,fee-policies}`

### 삭제 또는 통합한 메뉴 (26 → 16)

| 기존 메뉴 | 처리 | 리다이렉트 |
|---|---|---|
| 아이템리스트 | 상품 관리 `소싱검토` 탭으로 흡수 | `/products?status=CANDIDATE` |
| 옵션/Variant | 상품 목록 `옵션 수` 클릭 → 모달 | `/products` |
| 등록가 시뮬레이션 | 마진 계산기와 통합(모드 전환) | `/tools/calculator?mode=price` |
| 마진 계산기 | 보조 도구로 위상 조정 | `/tools/calculator?mode=margin` |
| 실마진 분석 | 판매장부에 흡수 | `/sales?view=margin` |
| 증빙 관리 | 사업비용 `증빙 없음` 탭 | `/expenses?tab=evidence` |
| 정책 5화면 | 설정 2화면으로 압축 | `/settings/{tax,fee-policies}` |
| 관리자 5화면 | 설정으로 통합 | `/settings/members` 등 |
| 거래처 / 봇·API 연동 | 설정 / 쇼핑몰 API 연결 | — |

**기존 라우트는 삭제하지 않고 `redirect()` 스텁 21개로 남겼다.** 북마크·외부 링크가 살아 있고,
`typedRoutes` 가 남은 참조를 빌드 시점에 잡아준다.

---

## 3. 계산 로직

### 마진 (`lib/calc/engine.ts`)

```
예상 정산액 = 판매가 + 구매자부담배송비
            − 판매수수료 − 수수료부가세 − 결제수수료 − 판매자쿠폰 − 기타차감
상품 공헌이익 = 예상 정산액 − 총원가 − 광고비 − 예상 반품비용
예상 순이익   = 공헌이익 − 고정비 배분액 − 예상 세금
마진율        = 순이익 ÷ 판매가
목표 판매가   = (고정 원가 + 목표 이익) ÷ (1 − 변동비율 합 − 목표마진율)
```

판단이 필요했던 지점:

- **수입 부가세는 공제 가능하면 원가가 아니다.** 나중에 돌려받는 돈이라 원가에 넣으면 마진이 과소계상된다.
- **반품 비용 = (반품 1건당 비용 + 총원가) × 반품률.** 반품되면 회수비만 드는 게 아니라 원가도 날아간다.
- **역산 분모에는 판매가에 비례하는 항목만.** 반품충당은 원가 기반이라 비례하지 않아 고정 항목으로 뺐다.
- 분모가 0 이하면 "달성 불가"로 표시한다. 변동비 합계가 이미 판매가를 다 먹는다는 뜻이다.

### 부가세

| 과세유형 | 매출세액 | 매입세액 |
|---|---|---|
| 일반과세 (`INCLUDED_IN_PRICE`) | (판매가+배송비) × r/(1+r) | 적격증빙 매입의 부가세 전액 |
| 일반과세 (`ADDED_TO_PRICE`) | (판매가+배송비) × r | 동일 |
| 간이과세 (`DEEMED_VALUE_ADDED`) | 공급대가 × 부가가치율 × r | 매입액 × 고정 공제율 |
| 면세 / 영세율 | 0 | 면세는 없음, 영세율은 공제 |

**기존 백엔드는 `tax_policy.calculation_method` 를 읽어오면서도 계산에서 한 번도 분기하지 않아
간이과세를 골라도 일반과세와 똑같은 값이 나왔다.** 새 엔진은 정책으로 갈라진다.

### 금액 산술 (`lib/money.ts`)

JS 는 정수 타입이 없고 모든 number 가 double 이다. 규칙을 강제했다.

- 금액은 **원 단위 정수**로만 들고 다닌다
- 비율 곱셈 결과는 즉시 반올림해 정수로 되돌린다
- **비율끼리의 덧셈은 basis point 정수로 환산해서 더한다** — 역산 분모에서 요율 6개를 더하는데
  실수로 더하면 마지막 자리가 흔들려 권장가가 1원씩 튄다
- 반올림은 round-half-up. JS `Math.round` 는 음수에서 half-toward-+∞ 라 손실 금액이 어긋난다

### 수익배분 (`lib/calc/distribution.ts`)

```
① 총매출 → ② −판매처 차감·변동비 → ③ −사업비용 → ④ −세금·반품충당
→ ⑤ −고정 지급액 → ⑥ −투자자 우선 회수 → ⑦ −기준초과분·성과급
→ ⑧ 잔여 이익을 비율 배분
```

**직원 급여는 비용(⑤에서 차감), 동업자 배분은 이익 처분(⑧에서 분배).**
직원을 비율 배분 표에 넣으면 급여가 비용으로 안 잡혀 순이익이 과대계상되고 세무 리포트가 어긋난다.

---

## 4. mock 처리된 API — 실제 백엔드 연동이 필요한 부분

| 어댑터 | 상태 | 필요한 백엔드 작업 |
|---|---|---|
| `productAdapter.list` | ② BE 우선, 실패 시 mock | 없음 (동작) |
| `productAdapter.detail/save` | ③ mock | 소싱 원가 세부(통화·환율·관세·수입VAT·포장비), 판매처별 상품번호, 원가/판매가 적용일, 변경 이력 API |
| `productAdapter.options` | ③ mock | 옵션 그룹/값/Variant CRUD + 옵션명 커스텀 필드, 옵션 SKU, 소스 옵션 URL |
| `salesAdapter.list` | ② BE 우선 | 순이익·마진율 계산 결과를 목록에 포함 |
| `settlementAdapter` | ③ mock | `marketplace_settlement_record` 에 정산 예정일/완료일/실입금/차이/매칭상태 컬럼. 대사 API |
| `expenseAdapter` | ③ mock | 첨부 업로드·다운로드(스토리지), 결제자·관련상품·승인상태 필드, 키워드 검색(`q`) |
| `settingsAdapter.categories` | ③ mock | `/policies/expense_category` 재연결 + `costType`/`includeInProductCost`/`monthlyBudget` 컬럼 |
| `settingsAdapter.taxRules` | ③ mock | `tax_policy` 확장 — `deemed_value_added_rate`, `input_credit_flat_rate`, `verified_at` |
| `settingsAdapter.feePolicies` | ③ mock | `marketplace_fee_policy.fee_vat_included` 컬럼 추가 |
| `settingsAdapter.members` | ③ mock | `app_user` 확장 — `job_title`, `department`, `phone`, `joined_at`, 담당 판매처/카테고리 |
| `distributionAdapter` | ③ mock | 신규 `distribution_rule` 테이블 (participantType/calculationBase/threshold/cap/minimum/priority) |
| `notificationAdapter` | ③ mock | 알림 규칙 CRUD, 채널 설정 테이블(암호화 저장), Discord enum 추가, Slack/Discord 어댑터 |
| `integrationAdapter` | ③ mock | 쿠팡 Wing / 네이버 커머스 커넥터, `integration_account` 암호화 |
| `dashboardAdapter` | ③ mock | 기간 파라미터를 받는 집계 API (현재 `/reports/dashboard-summary` 는 `date.today()` 고정) |

**교체 방법**: `lib/adapters/index.ts` 의 해당 함수 본문만 실제 API 호출로 바꾼다.
화면 코드는 한 줄도 건드리지 않는다.

**보안**: `maskSecret()` 는 mock 에서도 원문을 저장하지 않는다. 실제 구현으로 갈아끼울 때
평문 저장 코드가 남지 않게 하려는 장치다. Webhook URL·API 키는 서버에서 암호화 저장하고
조회 응답에는 마스킹된 값만 내려야 한다.

---

## 5. 추가로 검증해야 할 세금 및 정산 규칙

> 아래 값은 **구조 검증용 기본값**이다. `설정 > 사업자·세금 설정` 에서 `verifiedAt` 이 비어 있으면
> 계산 화면에 경고가 표시된다. 확인 후 "확인 완료로 표시"를 눌러야 경고가 사라진다.

| # | 항목 | 현재 기본값 | 확인 필요 |
|---|---|---|---|
| 1 | 간이과세 업종별 부가가치율 | 15% (소매업 가정) | **업종 판정**과 최신 비율. 전자상거래 소매업이 어느 업종으로 분류되는지 |
| 2 | 간이과세 매입세액 공제율 | 0.5% | 최신 개정 여부 |
| 3 | 간이과세 적용 기준금액 | 미설정 | 직전연도 공급대가 한도 (최근 몇 년 사이 여러 번 상향됨) |
| 4 | 간이과세 납부의무 면제 기준 | 미설정 | 과세기간 공급대가 기준금액 |
| 5 | 소득세 실효세율 | 일반 15% / 간이 10% | 순이익 추정용 참고값. 실제 신고세액과 다름 |
| 6 | **마켓별 수수료 VAT 포함 여부** | 쿠팡 별도 / 스마트스토어 포함 | 마켓 공지·계약서로 확인. **틀리면 수수료가 10% 어긋난다** |
| 7 | 오늘의집 수수료율 | 15% (추정) | 실제 계약 요율. 현재 근거 없음 |
| 8 | 매입세액 공제 대상 범위 | 국내배송·포장·수수료·결제수수료·광고비 | 세금계산서/카드전표 수취 여부에 따라 달라짐 |
| 9 | 픽썸의 현재 과세 유형 | 미확정 | 일반과세자인지 간이과세자인지 — 기본 정책이 갈린다 |
| 10 | 정산 불일치 허용 오차 | 50,000원 | 알림 임계값. 실무 기준 확인 |

**세무 전문가에게 6번을 먼저 물어보시길 권합니다.** 1~5번은 화면 표시 차이지만
6번은 모든 상품의 마진이 10% 어긋나는 문제입니다.

---

## 6. 남은 작업 (프론트 범위 밖)

- [ ] 백엔드 마이그레이션 — §4 표의 컬럼·테이블 추가
- [ ] 파일 스토리지 연동 (현재 첨부는 브라우저 메모리, 새로고침 시 소멸)
- [ ] Slack/Discord webhook 어댑터 + `enum_notification_channel_code` 에 `DISCORD` 추가
- [ ] 쿠팡 Wing API 키 발급 → 실제 커넥터 구현
- [ ] 저장된 보기를 서버 저장으로 전환 (현재 localStorage)
- [ ] 기존 화면 중 미개편: `/reports`, `/settlements/monthly`, `/sales/import`,
      `/settlements/marketplace/import`, `/products/[id]` — 동작에는 문제 없으나 새 디자인 미적용

---

## 7. 접근성 · 반응형 체크

- 색만으로 상태를 구분하지 않는다 — 증감은 화살표, 저마진은 ⚠, 활성 탭은 하단 인디케이터
- 모든 인터랙티브 요소에 `ops-focusable` 포커스 링, 아이콘 버튼에 `aria-label`
- 테이블은 `scope="col"/"row"`, 정렬은 `aria-sort`, 결과 건수는 `aria-live`
- 파괴적 동작(삭제·미저장 닫기)은 AlertDialog 확인
- md 미만: 사이드바 → 헤더 드로어. 넓은 테이블은 `ops-scroll-x` 로 가로 스크롤(억지 축소 안 함)
- `prefers-reduced-motion` 존중
- "본문으로 건너뛰기" skip link
