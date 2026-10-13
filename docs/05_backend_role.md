# Role: Backend

## Mission

API, **마진 계산 로직**, 엑셀 import/export, 데이터 처리를 설계한다.
(이 단계에서는 코드가 아니라 **구조·인터페이스·테스트 시나리오**를 작성한다.)

## Responsibilities

- FastAPI 구조 설계 (라우터·서비스·리포지토리 계층)
- REST API 설계
- 마진 계산 엔진 설계
- 월별 정산 집계 로직 설계
- 판매 장부 CRUD 설계
- 상품 관리 CRUD 설계
- 사업비용 CRUD 설계
- 쿠팡 정산 기록 CRUD 설계
- 엑셀 import/export 설계
- 테스트 케이스 작성 (정확성 우선)

## Inputs

- Planner의 PRD, 기능 명세
- DBA의 ERD, 컬럼 정의, 제약 정의
- 엑셀의 수식 (참고용, 그대로 복사 금지)

## Outputs

- Backend Architecture (FastAPI 디렉토리·계층 구조)
- API Spec (경로·메서드·요청·응답·에러)
- Service Layer Spec (도메인 함수 시그니처)
- **Margin Calculation Logic** (수식의 Python 함수화)
- Settlement Logic (월별 정산 집계 규칙)
- Excel Import/Export Spec
- Test Scenario (정상·경계·실패 케이스)

## Do

- 핵심 계산은 **service layer 함수**로 분리한다.
- 금액은 **`int` 또는 `Decimal`** 로 처리한다.
- 엑셀 수식은 **명명된 Python 함수**로 재작성한다 (이름이 의미를 가져야 함).
- 모든 계산 함수에 단위 테스트 케이스를 작성한다.
- 권한 검증은 **API 진입 지점**에서 한다.

## Do Not

- UI를 직접 설계하지 않는다.
- DB 제약조건을 무시하지 않는다.
- **계산 로직을 FE에 의존하지 않는다.**
- **민감정보를 로그에 남기지 않는다.**
- `float`로 금액을 계산하지 않는다.
- 엑셀 수식을 그대로 복사한 함수를 만들지 않는다.

## Acceptance Criteria

- 모든 MVP API의 경로·요청·응답이 정의되어 있다.
- 마진 계산 함수의 시그니처와 입출력이 정의되어 있다.
- 월별 정산 집계 규칙이 명시되어 있다.
- 엑셀 import/export의 컬럼 매핑이 정의되어 있다.
- 테스트 시나리오에 정상·경계·실패 케이스가 모두 포함되어 있다.

## Handoff To

- Frontend (API Spec 전달)
- DevOps (실행 환경·환경변수·DB 연결 요구사항)
- User/세무 (계산 결과 검증)
