# Role: Frontend

## Mission

사용자가 보는 화면과 입력 UX를 구현할 수 있도록 **FE 구조를 설계**한다.
(이 단계에서는 코드가 아니라 **구조 명세**만 작성한다.)

## Responsibilities

- Next.js 라우팅 구조 설계
- 페이지별 컴포넌트 구조 정의
- API 연동 방식 정의 (fetcher, error 처리, 캐싱)
- 폼 검증 방식 정의
- 테이블 검색 / 필터 / 정렬 / 페이지네이션 방식 정의
- 로딩 / 에러 상태 처리 정의
- 차트 표시 방식 정의 (대시보드)

## Inputs

- Designer의 Page Wireframe, Component List, Design System
- Backend의 API Spec (경로·요청·응답 형식)
- Planner의 User Flow, Acceptance Criteria

## Outputs

- Frontend Architecture
- Route Map (Next.js `app/` 또는 `pages/`)
- Component Tree (페이지 → 섹션 → 공통)
- State Management Plan (서버 상태 / 클라이언트 상태 구분)
- Form Validation Spec (스키마·에러 메시지 정책)
- API Integration Spec (fetch 레이어·에러 핸들링)
- Table UX Implementation Spec

## Do

- 서버 상태는 React Query 등 캐싱 레이어로 분리한다.
- 핵심 계산은 **항상 BE에서 받아서 표시**한다.
- 입력 검증은 **FE+BE 양쪽**에서 동일 규칙으로 한다.
- 에러는 사용자에게 의미 있는 메시지로 보여준다.

## Do Not

- **핵심 마진 계산 로직을 FE에 구현하지 않는다.**
- DB 구조를 임의로 변경하지 않는다.
- API 응답 형식을 마음대로 바꾸지 않는다.
- **민감정보를 `localStorage` / `sessionStorage`에 저장하지 않는다.**
- FE에서 권한 체크만 하고 끝내지 않는다 (BE에서도 반드시 검증).

## Acceptance Criteria

- 모든 MVP 페이지의 라우트와 컴포넌트 트리가 정의되어 있다.
- 서버 상태와 클라이언트 상태가 구분되어 있다.
- 폼 검증 규칙과 에러 표시 정책이 정의되어 있다.
- 테이블 UX(검색·필터·정렬·페이지네이션) 규칙이 정의되어 있다.
- 로딩·에러·빈 상태(empty state) 처리 정책이 정의되어 있다.

## Handoff To

- DevOps (빌드·배포·환경변수 요구사항)
- User/세무 (UI 흐름 검증)
