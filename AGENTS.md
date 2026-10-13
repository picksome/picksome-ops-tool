# AGENTS.md

이 저장소에서 작업하는 AI 에이전트(Codex, Claude Code 등)를 위한 진입점입니다.

## 로컬 개발 환경을 처음 띄워야 한다면

**Docker를 설치하려 하지 마세요.** 이 프로젝트는 Windows 10 Home 환경(Docker Desktop 미지원)에서
개발되며, Docker 없이 PostgreSQL 네이티브 설치 + 직접 `uvicorn`/`npm run dev` 실행으로 로컬 개발을
진행합니다.

→ **`docs/34_local_dev_setup_no_docker.md`** 를 그대로 따라 실행하면 됩니다. 실제로 한 번 겪은
문제(끄집어낸 `.env` 경로 이슈, 파일 인코딩 이슈 등)와 해결법이 순서대로 정리되어 있습니다.
설치해야 할 프로그램/버전은 **`REQUIREMENTS.md`** 에 정리되어 있으니 먼저 확인하세요.

Docker가 사용 가능한 서버/CI 환경이라면 `docs/25_devops.md` (Docker Compose 기반 정식 배포 구성)를
대신 참고하세요.

## 코드 작업 규칙

이 저장소의 작업 규칙(정책 하드코딩 금지, 금액은 int/Decimal만 사용, 순이익 기준 정산 모델,
사용자 승인 전 코드 작성 금지 등)은 `CLAUDE.md`에 정의되어 있습니다. Codex를 포함해 어떤 에이전트든
작업 전에 반드시 읽으세요.
