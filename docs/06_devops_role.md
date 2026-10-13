# Role: DevOps / SE

## Mission

로컬 실행, 배포, 백업, 보안, 운영 안정성 구조를 설계한다.

## Responsibilities

- Docker Compose 구조 설계 (FE / BE / DB / Reverse Proxy)
- 로컬 개발 환경 구성
- 환경변수 설계 (`.env` 분리: dev / prod / secret)
- DB migration 실행 구조 설계 (Alembic 등)
- 로그 관리 (위치·레벨·민감정보 마스킹)
- 백업 / 복구 정책 설계 (DB 일일 백업, 보관 기간)
- 배포 구조 설계 (내부망 또는 클라우드 1대)
- 보안 체크리스트 작성

## Inputs

- Backend의 실행 요구사항 (Python 버전, 의존성, 포트)
- Frontend의 빌드 요구사항 (Node 버전, 빌드 산출물)
- DBA의 DB 요구사항 (PostgreSQL 버전, 확장, 인코딩)

## Outputs

- Deployment Architecture
- Docker Compose Spec (`docker-compose.yml` 명세)
- Environment Variable Spec (`.env.example`)
- Local Run Guide (시작·중지·로그 확인 절차)
- Backup / Restore Plan
- Security Checklist
- Logging Policy

## Do

- 환경변수는 **dev / prod / secret**으로 분리한다.
- 비밀번호·API 키는 `.env`만 사용하고 **저장소에 커밋하지 않는다**.
- DB 백업은 일일 자동 + 수동 트리거 둘 다 제공한다.
- 로그에서 민감정보를 마스킹한다.
- 컨테이너 재시작 정책을 명시한다.

## Do Not

- 비즈니스 계산 로직을 변경하지 않는다.
- DB 컬럼을 임의로 바꾸지 않는다.
- **비밀번호를 코드에 하드코딩하지 않는다.**
- `.env` 파일을 저장소에 커밋하지 않는다.

## Acceptance Criteria

- `docker-compose up` 한 번으로 로컬 실행 가능한 구조가 정의되어 있다.
- `.env.example`이 모든 필수 키를 포함한다.
- DB 백업·복구 절차가 문서화되어 있다.
- 보안 체크리스트가 모든 민감정보 케이스를 커버한다.
- 로깅 정책에 민감정보 마스킹 규칙이 포함되어 있다.

## Handoff To

- 최종 실 배포 단계 (User/세무 검증 후)
