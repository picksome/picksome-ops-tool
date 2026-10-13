# 24. Frontend Spec — PICKSOME OPS TOOL

> 작성: Frontend
> 작성일: 2026-06-17
> 입력: `20_prd.md`, `22_api_spec.md`, `23_design.md`
> 다음 역할: DevOps (배포·환경)

---

## 0. Reframed Request

PRD/디자인/API 를 입력으로,
**Next.js (App Router) + React + TypeScript + Tailwind** 기반의
**디렉토리 구조, 라우팅, 컴포넌트 트리, API 클라이언트, 상태관리, 인증/RBAC 처리, i18n 골격**을 정의한다.
코드는 작성하지 않는다.

**2026-06-18 갱신** (Open Items 7건 Confirmed):
- ID 처리: 라우트 파라미터는 UUID, 화면 표시는 `business_key`. URL 에 순차 ID 노출 금지 (D-283)
- Header: 우상단 **벨 아이콘 + 미확인 카운트 배지** (`/notifications/unread-count` 폴링 30s) + 클릭 시 IN_APP 수신함 드롭다운 (D-281)
- 상품 상세 라우트: `/products/[id]/options`, `/products/[id]/variants` 신설 — variants 그리드 컴포넌트 (옵션값 헤더 + cost/sale_price/stock 인라인 편집) (D-286)
- 비용 입력 폼: `VatInclusionToggle` (라디오: 포함/별도/면세) + 선택에 따라 supply/vat 필드 입력 모드 전환 (D-284)
- 정산 REOPEN 모달: `useReopenInfo(year_month)` 훅으로 `reopen_count / reopen_limit / remaining` 표시, remaining=0 시 버튼 disabled + 사유 textarea 필수 (D-285)
- Route guard: `RequireRole` 에 `requireFeatureFlag` 옵션 추가 — EXTERNAL_ACCOUNTANT 라우트 진입 시 `FEATURE_EXTERNAL_ACCOUNTANT=false` → 403 페이지 (D-282)

---

## 1. 디렉토리 구조

```
frontend/
├── app/                              # Next.js App Router
│   ├── (auth)/
│   │   ├── login/page.tsx
│   │   ├── invite/[token]/page.tsx
│   ├── (app)/                        # 인증된 영역
│   │   ├── layout.tsx                # AppShell (Header+Sidebar)
│   │   ├── dashboard/page.tsx
│   │   ├── products/
│   │   │   ├── page.tsx
│   │   │   ├── sourcing/page.tsx
│   │   │   └── [id]/page.tsx
│   │   ├── margin/calculator/page.tsx
│   │   ├── sales/page.tsx
│   │   ├── expenses/page.tsx
│   │   ├── settlements/
│   │   │   ├── marketplace/page.tsx
│   │   │   ├── monthly/page.tsx
│   │   │   └── monthly/[ym]/page.tsx
│   │   ├── reports/
│   │   │   └── page.tsx
│   │   ├── policies/
│   │   │   ├── page.tsx
│   │   │   ├── [kind]/page.tsx
│   │   │   └── history/page.tsx
│   │   ├── partners-external/page.tsx
│   │   ├── admin/
│   │   │   ├── users/page.tsx
│   │   │   ├── audit-logs/page.tsx
│   │   │   ├── vault/page.tsx
│   │   │   └── notification-prefs/page.tsx
│   │   └── me/page.tsx
│   ├── error.tsx                     # 전역 에러 바운더리
│   ├── not-found.tsx
│   └── api/health/route.ts           # 헬스체크 (BE 프록시 안 함)
├── components/
│   ├── shell/                        # AppShell, Header, Sidebar, Breadcrumb
│   ├── ui/                           # 디자인 시스템 (Button, Input, Modal, Drawer, Toast, Badge)
│   ├── table/                        # DataTable, InlineCell, BulkActionBar, ColumnHeader
│   ├── form/                         # PolicyAutoField (자동값), MoneyInput, RateInput, DatePicker
│   ├── policy/                       # PolicyBadge, PolicySourcePopover, PolicyNewVersionModal
│   ├── settlement/                   # SettlementStatusBadge, ConfirmLockDialog, ReopenDialog
│   ├── margin/                       # MarginCalculator, ExpectedActualToggle, MarginCompareTable
│   ├── expense/                      # ExpenseDrawer, EvidenceStatusBadge, EvidenceCounts
│   ├── chart/                        # KpiCard, TrendLine, CategoryBar
│   └── shortcut/                     # GlobalCommandPalette (Cmd+K)
├── lib/
│   ├── api/                          # OpenAPI 생성 타입 + fetcher
│   │   ├── client.ts
│   │   ├── auth.ts
│   │   ├── products.ts
│   │   ├── margin.ts
│   │   ├── sales.ts
│   │   ├── expenses.ts
│   │   ├── settlements.ts
│   │   ├── policies.ts
│   │   ├── reports.ts
│   │   └── types.gen.ts              # openapi-typescript 자동 생성
│   ├── auth/                         # JWT 저장(httpOnly cookie) / 새로고침
│   ├── rbac/                         # role-based UI 가드
│   ├── money.ts                      # 천단위·KRW 포맷
│   ├── rate.ts                       # NUMERIC(7,4) ↔ % 변환
│   ├── datetime.ts                   # KST 변환·표시
│   └── error.ts                      # 표준 에러 핸들러
├── hooks/
│   ├── useCurrentUser.ts
│   ├── useTable.ts                   # 정렬·페이지·필터 상태
│   ├── useInlineEdit.ts
│   ├── useBulkSelect.ts
│   ├── usePolicy.ts                  # 정책 active 조회 (cache)
│   ├── useNotifications.ts
│   └── useToast.ts
├── stores/                           # Zustand
│   ├── ui.ts                         # 사이드바 collapsed, 모달 stack
│   ├── filters.ts                    # 페이지별 필터 영속 (URL ↔ store)
│   └── auth.ts                       # currentUser, role
├── styles/
│   ├── tokens.css                    # CSS 변수 (디자인 토큰)
│   └── globals.css                   # Tailwind base + 토큰 매핑
├── public/
├── tailwind.config.ts
├── next.config.mjs
├── tsconfig.json
└── package.json
```

---

## 2. 라우팅 (App Router)

### 2-1. 그룹화
- `(auth)` 그룹: 비로그인 페이지
- `(app)` 그룹: 로그인 필수. `layout.tsx` 에서 currentUser fetch + redirect

### 2-2. 보호된 라우팅
```ts
// app/(app)/layout.tsx
// 1) cookie 의 JWT 검증 → API GET /me
// 2) 실패 시 redirect '/login?next=...'
// 3) 성공 시 AuthProvider + AppShell 렌더
```

### 2-3. RBAC 가드
- 라우트 진입 시 role 확인 (서버 컴포넌트)
- 권한 없으면 403 페이지로 redirect
- UI 컴포넌트 단위: `<RequireRole role={['ADMIN']}>...`

### 2-4. 페이지별 데이터 패치 패턴
- 서버 컴포넌트: 초기 데이터 (목록 첫 페이지) prefetch
- 클라이언트 컴포넌트 + React Query (TanStack): 인터랙티브 갱신
- mutation 후 `queryClient.invalidateQueries`

---

## 3. 컴포넌트 트리 (주요 화면)

### 3-1. AppShell

```
<AppShell>
  <Header>
    <Logo />
    <OrgSwitcher />          # MVP: 픽썸 1개. 컴포넌트만 미리 둠
    <GlobalCommandPalette /> # Cmd+K
    <NotificationBell />
    <UserMenu />
  </Header>
  <Sidebar>
    <NavItem icon="📊" path="/dashboard" />
    <NavItem icon="📦" path="/products" />
    ... 11개
  </Sidebar>
  <main>{children}</main>
  <Toaster />
  <ModalRoot />
  <DrawerRoot />
</AppShell>
```

### 3-2. 마진계산기

```
<MarginCalculatorPage>
  <PageHeader title="마진계산기" actions={[LoadFromProduct, Reset]} />
  <Card>
    <MarginInputForm
      values={values}
      autoFields={resolvedPolicies}   # 자동 적용된 정책 매핑
      onOverride={(field, value, reason) => ...}
    />
  </Card>
  <Card>
    <MarginResult result={result} warnings={warnings} />
    <Actions>
      <Button onClick={save}>예상 마진 저장</Button>
      <Button variant="outline" onClick={openSimModal}>등록가 시뮬레이션</Button>
    </Actions>
  </Card>
  <PriceSimulationModal />
</MarginCalculatorPage>
```

### 3-3. DataTable (가장 중요)

```
<DataTable<Row>
  columns={columns}
  rows={rows}
  rowKey="id"
  inlineEdit={true}
  bulkSelect={true}
  onInlineSave={(row, patch) => mutate.patch(row.id, patch)}
  onBulkAction={(rows, action) => ...}
  stickyHeader
  pagination={{cursor, limit, onChange}}
/>
```

`columns` 항목 예:
```ts
{
  key: 'sale_price',
  header: '판매가',
  align: 'right',
  type: 'money',         // money/rate/text/date/select/status
  editable: true,
  policySource: undefined,
  width: 120,
  sortable: true,
}
```

자동값 컬럼이면:
```ts
{
  key: 'marketplace_fee_rate',
  type: 'rate',
  policySource: { table: 'marketplace_fee_policy', version: 3, ... },
  // 셀 렌더: PolicyAutoField
}
```

### 3-4. 사업비용 페이지

```
<ExpensesPage>
  <PageHeader />
  <EvidenceCounts {NONE, NEEDS_CHECK, PRESENT, SUBMITTED} />
  <Filters />
  <BulkActionBar visibleWhen={selectedCount > 0} actions={[
    '카테고리 일괄변경', '증빙상태 일괄변경', '결제자 일괄변경', '삭제'
  ]} />
  <DataTable ... onRowClick={openDrawer} />
  <ExpenseDrawer expenseId={...} tabs={['기본정보','코멘트','감사로그']} />
</ExpensesPage>
```

### 3-5. 월별 정산 상세

```
<MonthlySettlementPage ym="2026-06">
  <PageHeader title="2026-06 정산" badge={<SettlementStatusBadge />}
    actions={[Recalc, Confirm, Lock, Reopen]} />
  <SummaryCards revenue/directCost/commonExpense/netProfit />
  <CarryoverCard previous/applied/remaining />
  <ReconciliationCard coupang/sales/diff />
  <DistributionTable partners=[...] editable={status==='DRAFT'} />
  <Actions>
    <Button>리포트 묶음 다운로드 (ZIP)</Button>
    <Button>세무사에 전달</Button>
  </Actions>
  <ConfirmLockDialog />
  <ReopenDialog />
</MonthlySettlementPage>
```

### 3-6. 정책 자동값 셀

```
<PolicyAutoField
  value={0.108}
  formatter="rate"
  source={{
    table: 'marketplace_fee_policy',
    rowId: 12,
    version: 3,
    effectiveFrom: '2026-04-01',
    reliabilityStatus: 'CONFIRMED',
    sourceType: 'MANUAL',
    sourceCheckedAt: '2026-06-14T...'
  }}
  onOverride={(newValue, reason) => ...}
  override={override?  {value, reason}  :  undefined}
/>
```

상태:
- 일반: muted 배경 + ⓘ (호버 시 PolicySourcePopover)
- override: warn 배경 + "수동 수정됨" 배지 + sub-line "사유: ..."

---

## 4. API 클라이언트

### 4-1. 생성 전략
- BE 가 OpenAPI 스키마 export → `openapi-typescript` 로 `types.gen.ts` 생성
- 빌드 단계에서 자동 재생성 (CI)

### 4-2. fetch wrapper
- 공통 헤더 (Authorization)
- 401 → refresh 토큰 시도 → 실패 시 로그아웃 + redirect
- 표준 에러 wrapper 파싱 → `ApiError` 던지기
- 응답 wrapper `{data, meta}` → `data` 만 반환

```ts
type ApiError = {
  code: string;
  message: string;
  details?: any[];
  status: number;
}
```

### 4-3. mutation 패턴
- React Query `useMutation`
- 낙관적 업데이트 (인라인 편집)
- 실패 시 롤백 + 토스트
- 성공 시 invalidate + 토스트 (선택)

### 4-4. 인라인 편집 미러
```ts
const { mutate } = useMutation({
  mutationFn: ({id, patch}) => api.expenses.patch(id, patch),
  onMutate: async (variables) => {
    await queryClient.cancelQueries(...)
    const prev = queryClient.getQueryData(...)
    queryClient.setQueryData(..., applyPatch)
    return { prev }
  },
  onError: (err, vars, ctx) => {
    queryClient.setQueryData(..., ctx.prev)
    toast.error(err.message)
  },
  onSettled: () => queryClient.invalidateQueries(...)
})
```

---

## 5. 상태관리

| 영역 | 도구 | 비고 |
|---|---|---|
| 서버 상태 (목록·상세·검색) | **TanStack Query** | staleTime 30s 기본 |
| 인증/사용자 | Zustand (`auth.ts`) | rehydrate from cookie + /me |
| UI (모달 stack, 사이드바 collapsed) | Zustand (`ui.ts`) | |
| 페이지별 필터 | URL params + 가벼운 Zustand 미러 | back/forward 동작 보존 |
| 폼 | react-hook-form + zod | resolver 로 zod 사용 |

---

## 6. 인증/RBAC

### 6-1. 인증
- httpOnly Secure cookie 에 JWT 저장 (BE 발급)
- 새로고침 시 `/me` 호출로 currentUser 복구
- refresh 토큰은 `/auth/refresh` 자동 호출 (401 인터셉터)

### 6-2. RBAC UI 가드
```tsx
<RequireRole roles={['ADMIN','ACCOUNTING']}>
  <Button>월별 정산 확정</Button>
</RequireRole>
```
- 권한 없으면 컴포넌트 렌더 안 함 (또는 disabled + 사유 툴팁)

### 6-3. 라우트 가드
- `(app)/layout.tsx` 에서 user.role 체크
- 라우트별 요구 role 메타 정의 (lib/rbac/route-meta.ts)

---

## 7. i18n / 통화 / 시간

- MVP: 한국어 고정. 단, 모든 사용자 텍스트는 `t(key)` 패턴 (next-intl 또는 자체 dictionary)
- 통화: KRW 정수. `formatMoney(123456)` → `"123,456"`. 우측 정렬 + tabular-nums
- 시간: BE = UTC ISO. FE 에서 `formatKst(iso, 'YYYY-MM-DD HH:mm')`

---

## 8. 모바일 반응형 (조회 한정)

- Tailwind breakpoints: sm 640 / md 768 / lg 1024 / xl 1280
- 4개 페이지 (`/dashboard`, `/products`, `/sales`, `/settlements/monthly/[ym]`) 만 모바일 레이아웃 적용
- 모바일에서 인라인 편집·일괄 액션·정책 화면·관리 화면 진입 차단 → "데스크톱에서 사용" 안내

---

## 9. 성능

- 코드 분할 (Next.js 자동) + dynamic import 로 큰 컴포넌트 (차트, 마진계산기) 지연 로드
- React Query 캐싱 + cursor 페이지네이션
- 표 가상 스크롤 (1000+ rows): `@tanstack/react-virtual`
- 디바운스: 검색 300ms, 인라인 편집 즉시 저장 (낙관)

---

## 10. 에러 / 빈 상태 / 로딩

- 페이지 단위 ErrorBoundary
- 표는 skeleton 5행
- 빈 상태 컴포넌트 표준화 (`<EmptyState icon title description action />`)
- API 401 → 자동 refresh, 실패 시 토스트 + redirect
- API 409 (LOCKED_SETTLEMENT 등) → 토스트로 사유 설명 + 액션 비활성

---

## 11. 테스트

- 단위: Vitest + React Testing Library (포맷터·hook·DataTable)
- E2E: Playwright (로그인 → 마진계산 저장 → 정산 확정 시나리오)
- 시각적 회귀: Chromatic (Post-MVP)

---

## 12. 빌드 / 환경

- `.env.local`:
  - `NEXT_PUBLIC_API_BASE` = `http://localhost:8000/api/v1`
  - `NEXT_PUBLIC_SENTRY_DSN` = (선택)
- `next.config.mjs`: rewrites 로 `/api/proxy/*` → BE (dev only)
- 빌드: `next build` → standalone output → Docker
- OpenAPI 타입 생성: `pnpm gen:api` (BE schema URL 또는 파일)

---

## 13. Open Items 영향 (Frontend 관점)

| Open Item | 영향 |
|---|---|
| O-002 알림 채널 | NotificationPrefs 화면 토글 항목 추가 |
| O-003 세무사 합류 | EXTERNAL_ACCOUNTANT role 의 UI 라우트 가드·메뉴 가시성 |
| O-005 부가세 자동분리 | ExpenseDrawer 의 입력 동작 — 토글로 자동분리 ON/OFF |

---

## 14. Handoff

### Summary
Next.js App Router 그룹 (auth/app), 11개 1차 메뉴 라우팅, 디자인 토큰·컴포넌트 시스템 매핑, API 클라이언트(OpenAPI 자동생성), 상태관리(TanStack Query + Zustand), 인증/RBAC 가드, 모바일 4 페이지 한정.

### Decisions (Frontend 단독 결정)
- Next.js App Router + 서버컴포넌트 prefetch + 클라이언트 컴포넌트 갱신
- TanStack Query staleTime 30s
- 인라인 편집 = 낙관적 업데이트 + 즉시 PATCH
- 정책 자동값 셀 컴포넌트 단일화 (`PolicyAutoField`)
- 모바일 진입 차단 라우트 정의 (관리/정책/임의 입력 화면)

### Open Questions
- OpenAPI 자동 생성 빈도 — **현재 안: BE PR 마다 CI 에서 재생성**
- 모바일 PWA / 오프라인 — Post-MVP
- Chart 라이브러리 — **현재 안: Recharts** (간단·SSR 친화)

### Risks
- 인라인 편집 동시성 (두 사용자가 같은 셀) — last-write-wins 가정, 차후 ETag 도입
- 정책 자동값 fetch N+1 — 컬럼별 prefetch + 페이지 단위 정책 묶음 endpoint 검토 (BE 와 협의)
- 모바일 차단이 사용자 경험에 잘 전달되는지 — 명확한 안내 + 차단 시 데스크톱 URL 복사 버튼

### Required Inputs for Next Role
- **DevOps**:
  - Node 20 LTS, pnpm 9
  - `NEXT_PUBLIC_API_BASE` env
  - 정적 자산 캐시 (next standalone)
  - 헬스체크 path: `/api/health`
  - SSR 시 BE 호출에 필요한 내부 host (`API_INTERNAL_BASE`)
- **User/세무**: 실 데이터 베이스 화면 검증

### Handoff Target
**DevOps** → User/세무 검증 → PM 최종 통합

---

## 15. Action Checklist

- [x] Next.js App Router 디렉토리
- [x] 라우팅 그룹 (auth/app) + RBAC 가드
- [x] 컴포넌트 트리 (AppShell, DataTable, MarginCalculator, ExpensesPage, MonthlySettlementPage, PolicyAutoField)
- [x] API 클라이언트 (OpenAPI 생성 + fetch wrapper + mutation)
- [x] 상태관리 (TanStack Query + Zustand + URL params)
- [x] 인증/RBAC UI 가드
- [x] 모바일 4 페이지 한정 + 차단 정책
- [x] i18n/통화/시간 골격
- [x] 성능·테스트·빌드
- [x] Handoff
- [ ] (Next) DevOps: `docs/25_devops.md`
