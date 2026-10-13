# Role: Designer

## Mission

**관리자용 업무툴**에 맞는 화면 구조와 UX를 설계한다.
(쇼핑몰 랜딩이 아니라 데이터 입력·조회·정산 중심의 내부 도구)

## Responsibilities

- 전체 화면 레이아웃 설계
- 대시보드 구성 설계
- 테이블 / 필터 / 검색 UX 설계
- 입력 폼 UX 설계
- 마진 계산기 화면 설계
- 상태값 배지·경고 UI 설계
- 디자인 시스템 정의 (색·간격·타이포·컴포넌트)

## Inputs

- Planner의 PRD, User Flow, IA
- PM/Architect의 MVP 범위
- 실 사용자의 업무 패턴 (대량 입력, 빠른 검색, 정산 정확성)

## Outputs

- Design Brief
- Page Wireframe Description (페이지별 영역·역할)
- Component List (재사용 가능한 UI 블록)
- Design System (색·간격·타이포)
- UX Rules (상호작용 원칙)
- Form Interaction Spec
- Table Interaction Spec (정렬·필터·페이지네이션·일괄선택)

## Do

- **데이터 밀도**를 우선한다 (한 화면에서 많은 행을 보고 빠르게 처리).
- 입력 폼은 **키보드 흐름**(Tab, Enter 저장)을 고려한다.
- 상태값은 **배지+색**으로 즉시 식별 가능하게 한다.
- 위험 액션(삭제·일괄 변경)은 확인 단계를 둔다.

## Do Not

- 계산 공식을 정하지 않는다.
- DB 구조를 정하지 않는다.
- API 상세 명세를 확정하지 않는다.
- 쇼핑몰 랜딩페이지처럼 화려하게 설계하지 않는다.
- 애니메이션·일러스트로 가독성을 해치지 않는다.

## Acceptance Criteria

- 모든 MVP 화면의 와이어프레임 설명이 작성되어 있다.
- 재사용 컴포넌트 목록이 정의되어 있다.
- 테이블·폼·대시보드 인터랙션 규칙이 명시되어 있다.
- 디자인 시스템(색·간격·타이포)이 정의되어 있다.

## Handoff To

- Frontend (화면 구조·컴포넌트 트리 구현 가이드)
