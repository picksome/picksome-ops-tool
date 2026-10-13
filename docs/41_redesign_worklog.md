# 41. OPS 콘솔 전면 개편 — 작업 로그

> **브랜치**: `feat/ops-console-redesign`
> **시작**: 2026-08-05
> **목표**: 셀러가 상품 등록 → 판매 → 정산 → 비용 → 순이익까지 하나의 데이터 흐름으로 관리하는
> B2B 운영 콘솔 (Shopify Admin / Stripe Dashboard 계열 정보 밀도)
> **이 문서의 목적**: 세션이 중단돼도 다음 세션이 여기서 바로 이어받는다.

---

## 진행 상태 요약

| # | 단계 | 상태 | 산출물 |
|---|---|---|---|
| 1 | 현재 코드 구조 분석 | ✅ 완료 | 아래 §1 |
| 2 | 중복 메뉴·컴포넌트 파악 | ✅ 완료 | 아래 §2 |
| 3 | 새 정보구조·라우팅 정의 | ✅ 완료 | 아래 §3 |
| 4 | 디자인 토큰·레이아웃 | ✅ 완료 | globals.css, sidebar, header, layout |
| 5 | 공통 컴포넌트 (필터/테이블/포맷) | ✅ 완료 | money/format/date-range/useListQuery/filter-bar/table-toolbar/page-shell |
| 6 | Adapter 레이어 + mock | ✅ 완료 | lib/adapters/{types,mock-data,index} |
| 7 | 계산 엔진 | ✅ 완료 | lib/calc/{types,engine,distribution} |
| 8 | 대시보드 | ✅ 완료 | KPI 8 + 차트 5 + 경고영역 |
| 9 | 상품 관리 + drawer | ✅ 완료 | 18열/6탭 drawer |
| 10 | 옵션 modal | ✅ 완료 | 그룹→조합 생성, 일괄수정 |
| 11 | 판매장부 + 계산 근거 | ✅ 완료 | 검색+건별 계산근거 drawer |
| 12 | 쇼핑몰 정산 / 사업비용 / 수익배분 | ✅ 완료 | 4탭 / 첨부·결제자 / waterfall 3탭 |
| 13 | 알림·연동 / 계정·권한 | ✅ 완료 | 규칙·채널 / 커넥터 / 프로필·권한매트릭스 |
| 14 | 반응형·접근성 | ✅ 완료 | MobileNav, skip link, aria, reduced-motion |
| 15 | 빌드·테스트 | ✅ 완료 | tsc + next build 46 라우트 |

---

## §1. 현재 코드 구조 분석 (완료)

### 기술 스택 — **전부 유지한다**

| 영역 | 스택 | 비고 |
|---|---|---|
| 프레임워크 | Next.js 15.0.3 App Router, React 19 RC | `app/(app)` / `app/(auth)` route group |
| 언어 | TypeScript (strict), `typedRoutes` 활성 | 존재하지 않는 route 는 빌드 실패 |
| 스타일 | **Tailwind v4** (`@theme` 블록), CSS 변수 토큰 | v3 문법 아님 — `--color-*` 네임스페이스만 유틸리티 생성 |
| UI 프리미티브 | radix-ui 1.6 + shadcn 계열 22개 (`components/ui/`) | dialog/sheet/popover/select/command/tabs 등 보유 |
| 서버 상태 | TanStack Query v5 | |
| 클라 상태 | Zustand (`stores/auth.ts`, `stores/ui.ts`) | |
| 테이블 | TanStack Table v8 (`components/table/data-table.tsx`) | |
| 폼 | react-hook-form + zod + `@hookform/resolvers` | |
| 차트 | **recharts 2.15** | 신규 라이브러리 도입 없이 이걸로 간다 |
| 아이콘 | lucide-react | |
| 토스트 | sonner | |
| 백엔드 | FastAPI + PostgreSQL, raw SQL (ORM 모델 없음) | `app/services/*.py` 에 로직 |

### API 호출 구조

- `lib/api.ts` — fetch 래퍼. envelope(`{data, meta}`) 자동 unwrap, 401 → refresh 재시도.
  - 목록 응답은 `{ items, total, ...meta }` 로 정규화되어 내려온다.
  - `page/page_size` 를 보내면 `limit/offset` 도 자동으로 함께 실어 보낸다 (BE 가 두 방식 혼용).
- `lib/api/*.ts` — 도메인별 얇은 클라이언트 16개.
- **API base**: `NEXT_PUBLIC_API_BASE ?? "/api/v1"`. BE 는 `/api` 접두어 필수.

### 백엔드 현황 (라우터 18종)

`auth, admin_users, admin_audit, admin_vault, products, sales, expenses, policies, margin,
settlement, marketplace_settlements, reports, notifications, integration, sourcing,
sale_channel, exchange_rate, search`

- 계산: `services/calculation.py` (순수 함수) + `services/margin.py` (DB 연동)
- 정책 11종이 `/policies/{kind}` 하나로 CRUD (`marketplace_fee, payment_fee, tax, fulfillment_fee,
  return_risk, ad_cost, target_margin, settlement, partner_share, expense_category, evidence_type`)
- DB 53테이블, 전 PK UUID + `business_key` 노출용 별도

### 개편에 직접 영향 있는 현황 이슈 (개편 과정에서 자연 해소)

| ID | 내용 | 개편 시 처리 |
|---|---|---|
| I-1 | `--popover` 가 `--color-` 네임스페이스 밖이라 `bg-popover` 유틸리티가 생성 안 됨 → **모든 dialog/sheet/popover/dropdown/select/command 가 투명** | §4 디자인 토큰에서 해소 |
| I-2 | 대시보드 3개 위젯이 BE 응답 필드명과 불일치 (`month/margin`, `target_rate/actual_rate/gap`, `items` vs `alerts`) | §8 대시보드 재작성으로 해소 |
| I-3 | 옵션 목록이 `name` 을 읽는데 API 는 `name_ko` | §10 옵션 재작성으로 해소 |
| I-4 | 비용 카테고리 FE 가 존재하지 않는 `/expense-categories` 호출. `code`/`parent_id` 컬럼도 DB에 없음 | §12 에서 adapter + 정책 API 로 재연결 |
| I-5 | 판매장부 `q` 는 BE 지원하나 FE 입력창 없음. 매칭도 구식 `%q%` | §11 에서 해소 |
| I-6 | `tax_policy.calculation_method` 를 읽지만 계산에서 분기하지 않음 → 간이/일반 동일 결과 | §7 계산 엔진에서 해소 |

### 재사용 가능한 자산 (버리지 않는다)

- `components/ui/*` 22개 — 토큰만 고치면 그대로 사용
- `components/table/data-table.tsx`, `table-pagination.tsx`, `bulk-action-bar.tsx`
- `components/ui/finance.tsx` — `KpiCard`, `MoneyText`, `MarginRateBadge`, `ProfitDelta`
- `lib/api.ts` 전체, `lib/format.ts`
- `hooks/useSaleChannels.ts` (동적 판매채널 카탈로그), `useCurrentUser.ts`
- BE 계산·정책·정산 로직 전부

---

## §2. 중복 메뉴·컴포넌트 (완료)

| 중복 | 현재 | 결정 |
|---|---|---|
| 아이템리스트 ↔ 상품 관리 | `/products/sourcing` 과 `/products` 가 같은 "상품 후보" 개념. `product.status` 에도 CANDIDATE/APPROVED 존재 | **아이템리스트 메뉴 제거.** 상품 관리 상태 탭 + drawer `소싱` 탭으로 흡수. 데이터(`product_sourcing_candidate`)는 검토 이력으로 보존 |
| 등록가 시뮬레이션 ↔ 마진 계산기 | `/margin/simulator`(원가→가격), `/margin/calculator`(가격→마진). 같은 식의 방향 차이 | **계산 엔진 1개**로 통합. 상품 drawer `가격·마진` 탭이 주 사용처, `/tools/calculator` 는 보조 도구(탭 전환) |
| 옵션/Variant 메뉴 ↔ 상품 옵션 | `/products/options` 목록 → `/products/[id]/options` 페이지 이동 | 상품 목록의 **옵션 수 클릭 → modal**. `/products/[id]/options` 는 딥링크로 유지 |
| 정책 화면 4개 분산 | `/policies`, `/policies/fees`, `/policies/tax`, `/policies/exchange-rates`, `/policies/history` | 설정 그룹으로 재편: `사업자·세금 설정`, `수수료 정책` 2개로 압축 (환율/이력은 하위 탭) |
| 관리자 화면 5개 | `/admin/users, settings, audit, vault, notifications` | `구성원 및 권한` 으로 통합, 나머지는 하위 탭 |
| 증빙 관리 ↔ 사업비용 | `/expenses/evidence` 가 사업비용의 필터된 뷰 | 사업비용 내 탭으로 흡수 |

**메뉴 수: 26 → 16**

---

## §3. 새 정보구조 · 라우팅 (완료)

```
1. 대시보드                     /dashboard
2. 상품 관리                    /products              (+ ?status= 탭, drawer, 옵션 modal)
3. 판매 관리
   └ 판매장부                   /sales
4. 정산·재무
   ├ 쇼핑몰 정산                /settlements/marketplace   (탭: 예정/완료/불일치/연동)
   ├ 사업비용                   /expenses                  (탭: 전체/증빙)
   └ 수익배분                   /settlements/distribution  (탭: 결과/규칙/시뮬레이션)
5. 알림·연동
   ├ 알림센터                   /notifications             (탭: 목록/규칙/채널)
   ├ 메신저 연결                /integrations/messaging
   └ 쇼핑몰 API 연결            /integrations/marketplaces
6. 설정
   ├ 내 프로필                  /settings/profile
   ├ 구성원 및 권한             /settings/members
   ├ 사업자·세금 설정           /settings/tax
   ├ 비용 카테고리              /settings/expense-categories
   └ 수수료 정책                /settings/fee-policies

보조: /tools/calculator (빠른 마진계산기 — 사이드바 하단 별도 배치)
```

### 하위 호환 (기존 URL → 신규)

| 기존 | 신규 | 방식 |
|---|---|---|
| `/products/sourcing` | `/products?status=CANDIDATE` | redirect |
| `/products/options` | `/products?hasVariants=1` | redirect |
| `/margin/simulator` | `/tools/calculator?mode=price` | redirect |
| `/margin/calculator` | `/tools/calculator?mode=margin` | redirect |
| `/margin/actual` | `/sales?view=margin` | redirect |
| `/settlements/partner` | `/settlements/distribution` | redirect |
| `/expenses/evidence` | `/expenses?tab=evidence` | redirect |
| `/expenses/categories` | `/settings/expense-categories` | redirect |
| `/policies*`, `/admin/*` | `/settings/*` | redirect |
| `/me` | `/settings/profile` | redirect |
| `/partners-external` | `/settings/members?tab=partners` | redirect |
| `/automation/integrations` | `/integrations/marketplaces` | redirect |

> **원칙**: 기존 라우트는 삭제하지 않고 `redirect()` 스텁으로 남긴다.
> `typedRoutes` 가 켜져 있어 링크가 깨지면 빌드가 실패하므로 안전장치가 된다.

---

## §4. 디자인 토큰 · 레이아웃 (완료)

### `app/globals.css`
- **`--color-popover` / `--color-popover-foreground` 추가** — Tailwind v4 는 `--color-*`
  네임스페이스에서만 유틸리티를 만든다. `--popover` 만 있어서 `bg-popover` 클래스가
  아예 생성되지 않았고, dialog·sheet·popover·dropdown·select·command·tooltip 이
  전부 배경 없이 렌더됐다(=투명). 개편의 drawer/modal 이 전부 이 토큰에 의존하므로 선행 조건.
- 레이아웃 치수 토큰: `--layout-sidebar-width: 240px`, `--layout-header-height: 64px`,
  `--layout-drawer-width: 680px`, `--layout-sidebar-collapsed-width: 64px`
- 8px 간격 토큰 `--space-1..8`
- 손익 전용 색 `--color-profit / --color-loss / --color-neutral-value`
- 공통 클래스: `.ops-panel`, `.ops-sticky-head`, `.ops-scroll-x`, `.ops-focusable`,
  `.ops-num`, `.sr-only-focusable`, `prefers-reduced-motion` 대응

### `components/ui/dialog.tsx` / `sheet.tsx`
- 오버레이 `bg-black/10` → `bg-black/40` + `backdrop-blur-sm` (밝은 배경에서 안 보였다)
- Sheet 폭을 `[--sheet-w]` 변수로 바꿔 호출자가 `[--sheet-w:680px]` 로 재정의 가능
  (기존 `sm:max-w-sm` 하드코딩은 tailwind-merge 로 덮어쓸 수 없었다)

### `components/shell/sidebar.tsx` (전면 재작성)
- 26항목/8그룹 → **16항목/6그룹**. 단일 항목 그룹은 헤더 없이 노출.
- `MobileNav` 추가 — md 미만에서 사이드바를 숨기고 헤더의 드로어로 대체
- active 표시에 색 + 좌측 바 인디케이터 병행 (색만으로 구분하지 않음)

### `components/shell/header.tsx` (재작성)
- 높이 64px 고정, 브랜드를 헤더 좌측으로 이동(사이드바가 접혀도 정체성 유지)
- 검색 진입점을 "상품·주문·비용 검색 ⌘K" 로 상시 노출 — 단축키만 있으면 존재를 모른다
- 모바일 메뉴 토글 + 프로필/설정/로그아웃

### `app/(app)/layout.tsx`
- 바깥 패딩 제거(sticky 필터가 배경에 비치는 문제) → 여백은 `PageShell` 책임
- "본문으로 건너뛰기" skip link 추가

## §5. 공통 유틸 · 컴포넌트 (완료)

| 파일 | 역할 | 핵심 결정 |
|---|---|---|
| `lib/money.ts` | 금액·비율 산술 | **금액은 원 단위 정수.** 비율 덧셈은 basis point 정수로 — 실수 누적 오차로 권장가가 1원씩 튀는 것 방지. `roundHalfUp`(JS `Math.round` 는 음수에서 half-up 이 아님), `splitVatIncluded`, `distributeByRatio`(잔수 분배), `parseAmountInput`, `parsePercentInput`("11"→11%) |
| `lib/format.ts` | 표시 포맷 | 기존 4함수 시그니처 유지 + `formatCompactKRW`(125만), `formatSigned*`, `formatYearMonth`, `formatRelative`, `supplierLabelFromUrl`. `formatPercent` 기본 자릿수 2→1 |
| `lib/date-range.ts` | 기간 프리셋 | **KST 기준 연/월/일 직접 계산** — 서버 UTC 환경에서 로컬 날짜를 쓰면 새벽에 하루 밀린다. `previousRange`(직전 동일 길이), `suggestBucket`(92일 초과 시 월별) |
| `hooks/useListQuery.ts` | 목록 상태 ↔ URL | 기본값과 같으면 URL 에 안 쓴다. 필터 변경 시 page 자동 리셋. `router.replace` 로 히스토리 미적재 |
| `components/filters/date-range-filter.tsx` | 기간 필터 | 프리셋 6종 칩 + 네이티브 date input. from>to 검증 |
| `components/filters/filter-bar.tsx` | 필터 바 | sticky. `SearchInput`(디바운스 내장, ESC 초기화), `SelectFilter`, `RangeFilter`, 결과 건수 + 필터 초기화 |
| `components/table/data-table.tsx` | 목록 테이블 | **기존 props 전부 유지** + 열 표시/순서, 외부 정렬, 오류 상태, density, `meta.align` 우측정렬 |
| `components/table/table-toolbar.tsx` | 툴바 | `ColumnSettings`(표시/순서/밀도), `SavedViews`(localStorage), `toCsv`(**UTF-8 BOM** — 없으면 엑셀 한글 깨짐), `useColumnPreferences` |
| `components/shell/page-shell.tsx` | 화면 골격 | `PageShell`(list/detail), `PageTabs`(URL 연동), `Section`, `Field` |
| `components/ui/empty-state.tsx` | 빈 상태 | action 이 `{label,onClick}` + ReactNode 둘 다 허용 |

### 신규 라우트 생성 (typedRoutes 통과용 스텁 → 이후 단계에서 실제 구현으로 교체)
`/settlements/distribution`, `/integrations/messaging`, `/integrations/marketplaces`,
`/settings/{profile,members,tax,expense-categories,fee-policies}`, `/tools/calculator`

**검증**: `npx tsc --noEmit` 통과 ✅

---

## 작업 로그

| 시각 | 내용 | 결과 |
|---|---|---|
| 2026-08-05 | 분석·IA 확정, 기획서 `40_ops_improvement_plan.md` 작성 | ✅ |
| 2026-08-05 | §4 디자인 토큰·레이아웃 | ✅ tsc 통과 |
| 2026-08-05 | §5 공통 유틸·컴포넌트 | ✅ tsc 통과 |

## §6. Adapter 레이어 + mock (완료)

### 설계

화면은 `lib/adapters/index.ts` 의 인터페이스만 안다. 뒤에 실제 BE 가 있는지 mock 이 있는지 모른다.
세 가지 상태를 함수 주석에 표시했다.

| 상태 | 의미 | 해당 |
|---|---|---|
| ① live | BE 그대로 사용 | — |
| ② fallback | BE 를 먼저 부르고 실패 시 mock | `productAdapter.list`, `salesAdapter.list` |
| ③ mock | BE 엔드포인트 없음 | 나머지 전부 |

`withFallback` 은 **401 은 그대로 던진다.** 미인증까지 mock 으로 가리면 로그인 문제를 못 본다.
404/500/네트워크 오류만 mock 으로 떨어진다.

### mock 데이터 원칙

"화면이 비어 보이지 않게"가 아니라 **화면 설계를 검증**하려고 만들었다.
그래서 일부러 이상 케이스를 섞었다 — 적자 상품(차량용 청소기, 반품률 12%),
소스 URL 없는 상품(빨래건조대), 원가 적용일이 1년 넘은 상품(메쉬 이불),
정산 불일치 건(쿠팡 6월 −184,620원), 저마진 상품(무선 마우스 2.2%).
전부 정상 데이터만 넣으면 경고 영역과 빈 상태를 검증할 수 없다.

### 보안

`maskSecret()` — webhook URL / API 키는 mock 에서도 **원문을 저장하지 않는다.**
실제 구현으로 갈아끼울 때 평문 저장 코드가 남지 않게 하기 위해서다.

## §7. 계산 엔진 (완료)

### `lib/calc/types.ts` — 세금 규칙을 데이터로

간이/일반과세를 코드에 하드코딩하지 않는다. `TaxRule` 모델:
`taxProfileType / effectiveFrom / effectiveTo / outputVatPolicy / inputVatCreditPolicy /
importVatCreditPolicy / taxRateParameters / description / verifiedAt / ruleVersion`

- `outputVatPolicy`: `INCLUDED_IN_PRICE`(판매가가 VAT 포함 → `× r/(1+r)`) /
  `ADDED_TO_PRICE` / `DEEMED_VALUE_ADDED`(간이과세 부가가치율) / `NONE` / `ZERO_RATED`
- `inputVatCreditPolicy`: `FULL_CREDIT` / `FLAT_RATE_CREDIT`(간이) / `NO_CREDIT`
- `verifiedAt` 이 null 이면 화면에 "세무 확인 필요" 경고가 뜬다

`MarketplaceFeePolicy` 에 **`feeVatIncluded`** 를 넣었다. 마켓마다 수수료 표기가
VAT 포함/별도로 갈리는데 이걸 구분하지 않으면 수수료가 10% 어긋난다.

### `lib/calc/engine.ts` — 단일 계산 함수

`calculateMargin()` 하나를 등록가 시뮬레이션·마진계산기·상품 인라인 계산기가 공유한다.

```
예상 정산액 = 판매가 + 구매자부담배송비
            − 판매수수료 − 수수료부가세 − 결제수수료 − 쿠폰 − 기타차감
상품 공헌이익 = 예상 정산액 − 총원가 − 광고비 − 예상 반품비용
예상 순이익   = 공헌이익 − 고정비 배분액 − 예상 세금
목표 판매가   = (고정 원가 + 목표 이익) / (1 − 변동비율합 − 목표마진율)
```

판단이 필요했던 지점:
- **수입 부가세는 공제 가능하면 원가가 아니다.** 나중에 돌려받는 돈이라 원가에 넣으면
  마진이 과소계상된다. `importVatCreditPolicy` 로 분기한다.
- **반품 비용 = (반품 1건당 비용 + 총원가) × 반품률.** 반품되면 원가도 함께 손실된다.
- **역산의 분모**에는 판매가에 비례하는 항목만 넣는다. 반품충당은 원가 기반이라
  비례하지 않으므로 고정 항목으로 뺐다.
- `compareMarketplaces()` — 같은 원가·판매가로 판매처별 순이익을 비교해 정렬한다.

모든 결과에 `steps: CalcStep[]`(항목/산식/근거/금액)과 `appliedRules`(규칙 버전 박제)가 붙는다.

### `lib/calc/distribution.ts` — 배분 waterfall

**직원 급여는 비용(순이익 계산 전), 동업자 배분은 이익 처분(순이익 계산 후).**
이걸 안 나누면 급여가 비용으로 안 잡혀 순이익이 과대계상되고 세무 리포트가 어긋난다.

지급 방식 7종: 고정 / 순이익 비율 / 매출 비율 / 상품별 이익 비율 /
기준금액 초과분 / 단계별 성과급 / 투자 원금 우선 회수. 각각 상한·하한·우선순위·유효기간.

비율 배분의 잔수는 `distributeByRatio` 로 1원 단위까지 정확히 맞춘다.
비율 합이 100% 가 아니면 경고를 띄우고 정규화해서 배분한다(잔액이 붕 뜨는 것 방지).

**검증**: `npx tsc --noEmit` 통과 ✅

| 시각 | 내용 | 결과 |
|---|---|---|
| 2026-08-05 | §6 adapter + mock 데이터 | ✅ tsc 통과 |
| 2026-08-05 | §7 계산 엔진 (마진 + 배분) | ✅ tsc 통과 |


## §8. 대시보드 (완료)

`lib/adapters/dashboard.ts` + `components/charts/primitives.tsx` +
`components/dashboard/kpi-tile.tsx` + `app/(app)/dashboard/page.tsx`

- **전역 필터**: 기간(프리셋 6종) · 판매처 · 담당자. URL 동기화.
- **KPI 8종**: 총매출 / 예상 정산액 / 상품 공헌이익 / 예상 순이익 / 평균 마진율 /
  사업비용 / 판매 상품 수 / 정산 불일치 금액. 각각 이전 **동일 길이** 기간 대비 증감액+증감률.
  - 증감 색은 `higherIsBetter` 로 뒤집힌다 — 사업비용이 는 걸 초록으로 칠하면 안 된다.
  - 색만으로 구분하지 않도록 ↑↓− 아이콘을 항상 함께 쓴다.
- **차트 5종**
  1. 매출·순이익 추이 (복합: 막대 2 + 마진율 선, 우측 축). 92일 초과면 자동으로 월별 집계.
  2. **워터폴** — recharts 에 전용 차트가 없어 투명 받침 막대 + 값 막대로 만들었다.
     "매출은 느는데 왜 안 남지"에 답하는 그래프.
  3. 판매처별 매출·마진 (가로 막대) — 도넛은 5개 넘으면 판독이 안 돼 막대로.
  4. 비용 카테고리 구성 (가로 막대)
  5. 상품별 매출×마진율 산점도 + **손익분기선**. 버블 크기는 순이익.
- **경고 영역**: 저마진 / 적자 / 정산 불일치 / API 동기화 실패 / 원가 적용일 경과 /
  소스 URL 누락. 각 항목이 **해당 필터가 걸린 목록 화면으로 딥링크**된다.
- 툴팁은 금액을 축약하지 않는다(정확한 값을 확인하는 자리). 축 라벨만 "125만" 축약.

mock 이지만 상품·정산·비용 mock 에서 실제로 집계한다 — 대시보드 숫자와 목록 숫자가
어긋나면 화면 간 정합성 문제를 여기서 먼저 발견할 수 있다.

## §9. 상품 관리 + drawer (완료)

### 목록 (`app/(app)/products/page.tsx`)
- 상태 탭 6개로 **아이템리스트(소싱 후보)를 흡수**
- 통합 검색: 상품명·SKU·상품번호·URL·공급처·메모
- **날짜 기준 선택형 기간 필터** — 등록일 / 원가 적용일 / 판매가 적용일.
  기준 없는 날짜 필터는 "무슨 날짜인지" 항상 혼란을 만든다.
- 판매처·담당자·마진율 범위 필터, 필터 초기화, 결과 건수
- 18개 열 정의 + 표시/순서/밀도 설정(localStorage) — **기본 13개만 표시**.
  18개를 다 켜면 가로 스크롤이 화면 세 배가 된다.
- 저장된 보기, CSV 내보내기(UTF-8 BOM)
- 적자 행은 배경 강조, 저마진·소스URL 누락은 배지로 표시(색만으로 구분하지 않음)

### Drawer (`components/products/product-drawer.tsx`)
- 우측 slide-over, 폭 680px(`--layout-drawer-width`), 모바일은 전체 폭
- **클릭으로만 열린다** (hover 로 열리지 않음)
- 탭 6개: 기본 정보 / 소싱 / 가격·마진 / 판매처 정보 / 옵션 / 변경 기록
- 헤더 고정(상품명·상태·새 창 열기·닫기), **푸터 고정**(취소·저장)
- 저장 안 한 변경사항이 있으면 닫을 때 AlertDialog 확인
- **가격·마진 탭에 계산기 내장** — 입력 즉시 `calculateMargin` 이 돌아
  요약 4종 + 역산 3종(손익분기/목표이익/목표마진) + 단계별 계산 근거가 갱신된다
- 소스 URL 은 http/https 검증 + 공급처 배지(1688/도매꾹/타오바오…) + 새 창 열기

### 옵션 modal (`components/products/option-modal.tsx`)
- 목록의 "옵션 수" 클릭 → 페이지 이동 없이 넓은 모달(max-w-6xl)
- 좌: 옵션 그룹/값 편집 → **조합 생성**(데카르트 곱), 자동 생성 옵션명 미리보기
- 우: Variant 테이블 — 옵션명(자동/직접) / SKU / 원가 / 추가판매가 / 재고 / 판매상태 /
  소스 옵션 URL / 판매처 옵션번호
- 행 단위 추가·복제·삭제, 체크 선택 후 **추가 판매가 일괄 수정**
- **조합에서 빠진 기존 옵션은 삭제하지 않고 비활성으로 남긴다** — 판매 이력이 붙어 있을 수 있다

### 기존 URL 보존
21개 legacy 라우트를 `redirect()` 스텁으로 남겼다. 북마크·외부 링크가 살아 있고,
typedRoutes 가 남은 참조를 빌드 시점에 알려준다.

**검증**: `npx tsc --noEmit` 통과 ✅ / `npx next build` 통과 ✅ (전체 42 라우트)

| 시각 | 내용 | 결과 |
|---|---|---|
| 2026-08-06 | §8 대시보드 | ✅ tsc |
| 2026-08-06 | §9 상품 관리 + drawer, §10 옵션 modal, legacy 리다이렉트 21건 | ✅ tsc + next build |


## §11~13. 나머지 화면 (완료)

### 통합 마진 계산기 `/tools/calculator`
등록가 시뮬레이션과 마진계산기를 **한 화면의 모드 전환**으로 합쳤다(같은 식의 방향 차이일 뿐).
- 입력 22종(매입가·환율·중국내배송·해외배송·관세·수입부가세·국내배송·포장·기타·판매처·
  카테고리·수수료율·수수료VAT·결제수수료·쿠폰·광고비·반품률·반품비·고정비·목표이익·목표마진·과세유형)
- 출력 10종(총원가·수수료·정산액·공헌이익·세금·순이익·마진율·손익분기가·목표이익가·목표마진가)
- **과세유형 나란히 비교** — 같은 조건에 일반/간이/면세를 동시에 계산해 세금과 순이익 차이를 보여준다
- **판매처별 비교표** — 같은 원가·판매가로 9개 마켓의 수수료·정산액·순이익을 정렬. 1위에 왕관
- 계산식 도움말 drawer (`CalcHelpDrawer`) — 정산액/공헌이익/순이익/일반과세/간이과세/수입부가세/역산 7개 절

### 판매장부 `/sales`
- 통합 검색(주문번호·상품명·SKU·판매처 상품번호·구매자·메모) + 기간·판매처·주문상태·담당자 필터
- 행 클릭 → 우측 drawer 에 **건별 계산 근거**. **판매 시점 정책**으로 계산한다(지금 요율이 아니라)
- 상품 연결이 없으면 원가 0으로 계산했다는 경고를 명시

### 쇼핑몰 정산 `/settlements/marketplace`
- 탭 4개: 정산 예정 / 완료 / **불일치** / 연동 관리 (탭마다 건수 배지)
- 14개 항목(매출·수수료·배송비차감·광고쿠폰차감·기타차감·예상정산액·실입금·차이·매칭상태·등록경로)
- 불일치 탭에 원인 안내. **자동으로 맞추지 않는다** — 잘못 매칭되면 정산이 통째로 틀어진다
- 등록 경로 3종(API 자동 / 파일 업로드 / 수동)을 동등하게 취급

### 사업비용 `/expenses`
- 탭: 전체 / 증빙 없음. 필터 6종(기간·키워드·카테고리·결제자·결제수단·승인상태)
- **첨부파일** — jpg/png/heic/pdf, 10MB, 최대 5개. 미리보기·다운로드·삭제. 초과 시 이유를 명시해 거부
- **결제자** + 개인카드 배지(정산 시 돌려줘야 하는 돈이라 목록에서 바로 보인다)
- 관련 판매처·상품·프로젝트 연결, 승인 상태
- 삭제는 확인 dialog

### 수익배분 `/settlements/distribution`
- 탭 3개: 배분 결과 / 배분 규칙 / 시뮬레이션
- 참여자 5종 × 지급방식 7종, 상한·하한·우선순위·유효기간
- 계산 과정을 단계별로 표시(총매출→차감→영업이익→고정지급→투자회수→성과급→배분가능이익→비율배분)
- **시뮬레이션은 저장하지 않는다.** 월 범위 + 가정 순이익으로 what-if 를 돌린다
- 비율 합 ≠ 100% 경고

### 알림센터 `/notifications` + 메신저 연결 `/integrations/messaging`
- 탭 3개: 알림 목록 / 알림 규칙 / 전달 채널
- 규칙 9종의 **발동 조건을 평문 한국어로** 표시 + 임계값 편집 + 채널 토글
  (기존 알림센터가 "기능이 뭔지 모르겠다"는 소리를 들은 이유는 수신함만 있고 발송 규칙이 안 보였기 때문)
- 채널 5종(앱/이메일/Slack/Discord/카카오). 연결·테스트 전송·해제·마지막 테스트 결과
- 카카오는 사전심사가 필요해 `SETUP_REQUIRED` 로 두고 사유를 명시
- `ChannelsPanel` 은 `components/notifications/channels-panel.tsx` 에 두고 두 화면이 공유
  (Next.js page 파일은 default export 외 named export 를 허용하지 않는다)

### 쇼핑몰 API 연결 `/integrations/marketplaces`
- 커넥터 인터페이스(connect/disconnect/syncProducts/syncOrders/syncSettlements/getSyncStatus/getLastSyncResult)
- 마켓별 API 지원 수준을 3단계로 표시(API 지원 / 제휴 필요 / API 없음)
- 11번가는 인증 실패 상태로 두어 오류 UI 를 검증
- API 키는 마스킹 표시. 평문을 클라이언트에 두지 않는다

### 설정 5화면
- **내 프로필** — 프로필/보안/알림 3탭. **권한(role)과 직급(jobTitle)을 분리**했다.
  섞으면 "부장인데 읽기전용" 조합을 표현할 수 없다. 담당 판매처·카테고리 지정
- **구성원 및 권한** — 역할 6종 × 권한 12종 매트릭스. 권한을 개별로 켜고 끄지 않는다
  (조합이 폭발하고 누가 뭘 볼 수 있는지 아무도 모르게 된다).
  원가·마진은 조회 전용에게도 닫는다 — 외부 세무대리인이 원가를 볼 이유가 없다
- **사업자·세금 설정** — TaxRule 편집. `verifiedAt` 이 비면 계산 화면에 경고가 뜬다
- **비용 카테고리** — 상품 원가 포함 / 순이익 계산 포함 / 월 예산 / 고정·변동.
  사용 중인 카테고리는 삭제 불가, 비활성만 가능
- **수수료 정책** — 판매처·카테고리별 정률/정액/구간, **feeVatIncluded**, 유효기간, 근거 링크

**검증**: `npx tsc --noEmit` 통과 / `npx next build` 통과 (46 라우트, 정적 46/46)

| 시각 | 내용 | 결과 |
|---|---|---|
| 2026-08-06 | §11 계산기 통합 + 판매장부 | ✅ |
| 2026-08-06 | §12 정산·비용·수익배분 | ✅ |
| 2026-08-06 | §13 알림·연동·설정 5화면 | ✅ next build 46 라우트 |
