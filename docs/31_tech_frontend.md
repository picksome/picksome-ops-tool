# 31. 프런트엔드 기술 스택 (FE)

> PICKSOME OPS TOOL 프런트엔드 기술과 채택 이유.
> 작성일 2026-06-22 / 작성자 Frontend Role.
> ⚠️ FE 코드는 13-6 단계에서 작성 시작. 본 문서는 사전 계획.

---

## 1. 핵심 스택

| 분류 | 기술 | 버전 | 채택 이유 |
|------|------|------|-----------|
| 언어 | **TypeScript** | 5.x | 도메인 객체가 많고 마진/정산 등 숫자 정확성이 중요 → 타입 강제 |
| 프레임워크 | **Next.js** | 15.x (App Router) | SSR/RSC 로 초기 로딩 빠름. 파일 기반 라우팅이 라우터 12종 매핑에 적합 |
| UI 런타임 | **React** | 19.x | Next.js 15 의 권장 버전. Server Components 도입 |
| 패키지 매니저 | pnpm | 9.x | workspace + monorepo 친화, 디스크 효율 |
| 빌드 | Next 내장 (Turbopack) | — | 별도 설정 없음 |

---

## 2. 상태 관리 / 데이터 페칭

| 분류 | 기술 | 채택 이유 |
|------|------|-----------|
| 서버 상태 | **TanStack Query** v5 | 캐시 + revalidate + 낙관적 업데이트. 폼/리스트 양쪽 적합 |
| 클라이언트 상태 | React `useState` + `useReducer` | Redux 미사용 — 도메인이 페이지 단위로 분리되어 글로벌 store 가 적음 |
| 폼 | **react-hook-form** + zod | 비제어 컴포넌트로 리렌더 최소화. zod 로 BE pydantic 스키마와 1:1 |
| URL 상태 | `nuqs` | 검색/필터를 query string 에 두면 새로고침 안전 |

---

## 3. UI 컴포넌트

| 분류 | 기술 | 채택 이유 |
|------|------|-----------|
| 컴포넌트 시스템 | **shadcn/ui** (Radix UI 기반) | headless 친화. 디자인 자유도 + 접근성 (a11y) |
| 스타일 | **Tailwind CSS** v4 | utility-first. shadcn/ui 와 권장 조합 |
| 아이콘 | lucide-react | shadcn 권장. 트리쉐이킹 좋음 |
| 다크 모드 | next-themes | SSR-safe, prefers-color-scheme 호환 |

---

## 4. 데이터 그리드 / 테이블

운영툴 특성상 마진 계산 그리드, 사업비용 리스트, 정산 분배 표 등 **데이터 테이블 비중이 매우 크다**.

| 라이브러리 | 용도 | 채택 이유 |
|-----------|------|----------|
| **TanStack Table** v8 | 일반 리스트 (사용자/정책/제품) | headless, 정렬/필터/페이지네이션 표준 |
| **AG Grid Community** | 옵션 매트릭스 (D-286), 마진 시뮬 | 셀 단위 편집 + 키보드 내비. Excel 사용자 친화 |

---

## 5. 차트

| 도구 | 용도 |
|------|------|
| **Recharts** | 월별 매출/순이익 추이, 정산 분배 |
| 또는 visx | 복잡한 인포그래픽 필요 시 (Post-MVP) |

---

## 6. 파일 입출력

| 용도 | 라이브러리 | 이유 |
|------|-----------|------|
| Excel 다운로드 (BE 응답) | 브라우저 native | BE 가 xlsxwriter 로 만든 blob 을 Content-Disposition 으로 받음 |
| Excel 업로드 (import) | shadcn `<Input type="file">` | 파일은 BE 에 그대로 multipart 전송, FE 에서 파싱 안 함 |

---

## 7. 인증 / 권한

| 분류 | 방식 |
|------|------|
| 토큰 저장 | **httpOnly cookie** 또는 메모리 + refresh token 회전 | XSS 안전 |
| 권한 가드 | App Router middleware + page-level RBAC | 로그인/역할 미달 → /login 또는 403 |
| API 호출 | fetch wrapper (`lib/api.ts`) — 401 시 refresh 자동 |

---

## 8. 알림 (D-281)

- 헤더 벨 아이콘 — `GET /notifications/unread-count` 30초 polling
- 알림 드롭다운 — `GET /notifications?only_unread=true`
- 읽음 처리 → 캐시 invalidate (TanStack Query)
- Post-MVP: Server-Sent Events (SSE) 또는 WebSocket 으로 push

---

## 9. i18n / 통화 / 시간대

| 분류 | 처리 |
|------|------|
| 언어 | 한국어 단일 (i18n 라이브러리 미도입) |
| 통화 | KRW. `Intl.NumberFormat('ko-KR')` 표시. **클라이언트 측 계산 금지** (BE 책임) |
| 시간대 | Asia/Seoul. BE 가 UTC 로 반환 → FE 에서 `Intl.DateTimeFormat` |

---

## 10. 개발 도구

| 도구 | 용도 |
|------|------|
| ESLint | next/core-web-vitals + 자체 룰 |
| Prettier | 포맷 |
| Playwright | E2E 테스트 (Post-MVP) |
| Vitest | 컴포넌트/유닛 테스트 |

---

## 11. 채택하지 않은 기술 (의도적)

| 선택지 | 채택 안 함 이유 |
|--------|---------------|
| Vue / Svelte | React/Next 생태계가 운영툴 컴포넌트 풍부도에서 우위 |
| Redux / Zustand | 페이지 단위 상태로 충분. 글로벌 store 가 필요한 시점에 도입 |
| Material UI | shadcn/Tailwind 가 디자인 자유도 우위. MUI 는 디자인 종속성 큼 |
| Chakra UI | shadcn 등장 후 maintainable headless 가 표준화 |
| Remix | Next.js 15 RSC 로 충분. 팀/생태계 두꺼움 |

---

## 12. 디렉토리 구조 (계획)

```
frontend/
├── app/                       # Next App Router
│   ├── (auth)/login/page.tsx
│   ├── (app)/
│   │   ├── layout.tsx         # AppShell (nav + header + bell)
│   │   ├── dashboard/page.tsx
│   │   ├── products/page.tsx
│   │   ├── margin/page.tsx    # 마진계산기
│   │   ├── expenses/page.tsx
│   │   ├── settlement/page.tsx
│   │   ├── policies/page.tsx
│   │   └── reports/page.tsx
│   └── api/health/route.ts
├── components/
│   ├── ui/                    # shadcn 컴포넌트
│   ├── shell/                 # nav/header/bell
│   └── tables/                # DataTable, OptionGrid
├── lib/
│   ├── api.ts                 # fetch wrapper
│   ├── auth.ts
│   └── format.ts              # KRW / date 포맷
└── styles/globals.css
```

---

## 13. 작업 우선순위 (13-6 단계)

1. AppShell (좌측 nav + 헤더 사용자/벨 + 컨텐츠 영역)
2. DataTable 공용 (TanStack Table)
3. 마진계산기 (POST /margin/calculate)
4. 사업비용 (vat_inclusion_type UI 분기 — D-284)
5. 월정산 (calc preview → 생성 → confirm → lock + REOPEN 모달)
6. 헤더 벨 (unread-count polling)
7. 옵션 그리드 (D-286 3테이블 CRUD, AG Grid)
