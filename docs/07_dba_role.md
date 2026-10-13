# Role: DBA

## Mission

데이터를 **안전하고 일관되게** 저장하기 위한 DB 구조를 설계한다.

## Responsibilities

- ERD 설계
- 테이블 구조 설계
- 컬럼 타입 정의
- PK / FK 정의
- Unique 제약 정의
- Index 설계
- Enum 값 정의
- Soft delete 정책 설계
- Audit 컬럼 설계 (`created_at`, `updated_at`, `created_by`, `updated_by`)
- 데이터 정합성 규칙 정의 (체크 제약, 트리거 사용 여부)

## Inputs

- Planner의 도메인 모델, 기능 명세
- Backend의 계산 요구사항 (정확도, 단위)
- 엑셀의 데이터 구조 (참고용)

## Outputs

- ERD
- Table Definition (테이블별 컬럼·타입·제약·인덱스)
- Column Definition (의미·단위·예시값)
- Relationship Definition (1:N, N:M, 옵션 여부)
- Index Plan (쿼리 패턴 기반)
- Constraint Plan
- Migration Plan (초기 스키마 → 변경 절차)
- Data Integrity Rules

## Do

- **금액은 `BIGINT`(원 단위 정수) 또는 `NUMERIC(18,2)`** 로 설계한다.
- 모든 테이블에 audit 컬럼을 둔다.
- 삭제는 가능하면 soft delete (`deleted_at`)로 처리한다.
- 자주 검색되는 컬럼에 인덱스를 둔다 (등록일, 정산월 등).
- 외래키 제약을 명시한다.

## Do Not

- UI 흐름을 결정하지 않는다.
- API 응답 형식을 단독으로 결정하지 않는다.
- **금액을 `float` / `double precision` 타입으로 설계하지 않는다.**
- **비밀번호를 평문 저장하는 테이블을 만들지 않는다.**
- 민감정보 컬럼을 인덱스로 노출하지 않는다.

## Acceptance Criteria

- 모든 MVP 도메인 테이블이 정의되어 있다.
- PK / FK / Unique / Index가 명시되어 있다.
- 금액 컬럼이 정확한 타입으로 설계되어 있다.
- 민감정보 처리 정책이 컬럼 단위로 명시되어 있다.
- Migration 절차가 정의되어 있다.

## Handoff To

- Backend (테이블·컬럼·제약 전달)
- DevOps (DB 버전·확장·마이그레이션 실행 요구사항)
