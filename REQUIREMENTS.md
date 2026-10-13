# REQUIREMENTS

이 저장소를 새 컴퓨터에서 실행하기 전에 설치되어 있어야 하는 것들입니다.
설치 후 실행 절차는 [`docs/34_local_dev_setup_no_docker.md`](docs/34_local_dev_setup_no_docker.md) (Docker 없이) 또는
[`docs/25_devops.md`](docs/25_devops.md) (Docker Compose)를 따르세요.

마지막 확인: 2026-07-02 (Windows 10 Home 개발 환경 기준)

---

## 시스템 요구사항

| 프로그램 | 필요 버전 | 확인된 버전 | 확인 명령 |
|---|---|---|---|
| **Python** | 3.12 이상 | 3.12.3 | `python --version` |
| **Node.js** | 18 이상 | v24.14.0 | `node --version` |
| **npm** | (Node에 포함) | 11.9.0 | `npm --version` |
| **PostgreSQL** | 16 | 16.9 | `psql --version` |
| **Git** | 아무 최신 버전 | — | `git --version` |

Docker Desktop이 되는 환경(Windows 11 Pro, macOS, Linux)이면 위 중 Python/Node/PostgreSQL을
직접 설치할 필요 없이 **Docker + Docker Compose**만 있으면 됩니다 (`docs/25_devops.md` 참고).
Windows 10 Home처럼 Docker Desktop이 불가한 환경만 위 표대로 네이티브 설치가 필요합니다.

---

## Backend 의존성 (Python)

`backend/pyproject.toml`에 선언되어 있고, 아래 한 번이면 전부 설치됩니다.

```powershell
cd backend
pip install -e .
```

| 패키지 | 버전 | 역할 |
|---|---|---|
| fastapi | >=0.115 | API 프레임워크 |
| uvicorn[standard] | >=0.32 | ASGI 서버 |
| pydantic / pydantic-settings | >=2.9 / >=2.6 | 스키마·설정 |
| sqlalchemy | >=2.0.36 | ORM |
| alembic | >=1.14 | 마이그레이션 |
| psycopg[binary] | >=3.2 | PostgreSQL 드라이버 |
| argon2-cffi | >=23.1 | 비밀번호 해시 |
| python-jose[cryptography] | >=3.3 | JWT |
| uuid7 | >=0.1.0 | UUID v7 생성 |
| openpyxl / xlsxwriter | >=3.1 / >=3.2 | Excel 리포트 export |
| email-validator | >=2.2 | 이메일 형식 검증 |
| apscheduler | >=3.10 | 야간 배치(정산·부가세·마진 알림) |

개발 전용(`pip install -e ".[dev]"`): `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`.

전체 목록/버전 고정은 항상 `backend/pyproject.toml`이 source of truth입니다 — 이 표가 그것과
어긋나면 이 표가 아니라 `pyproject.toml`을 믿으세요.

---

## Frontend 의존성 (Node)

`frontend/package.json`에 선언되어 있고, 아래 한 번이면 전부 설치됩니다.

```powershell
cd frontend
npm install
```

| 패키지 | 버전 | 역할 |
|---|---|---|
| next | 15.0.3 | 프레임워크 (App Router) |
| react / react-dom | 19.0.0-rc | UI 런타임 |
| typescript | ^5.6.3 | 타입 |
| @tanstack/react-query | ^5.59 | 서버 상태 |
| @tanstack/react-table | ^8.20 | 테이블 |
| zustand | ^5.0.1 | 클라이언트 상태 |
| react-hook-form + zod | ^7.53 / ^3.23 | 폼 검증 |
| tailwindcss | ^4.0.0-beta.4 | 스타일 |
| radix-ui 계열 | — | 헤드리스 UI 컴포넌트 |

전체 목록/버전 고정은 항상 `frontend/package.json`이 source of truth입니다.

---

## DB 확장 (PostgreSQL)

마이그레이션(`M001_initial_schema.py`)이 자동으로 활성화하므로 수동 설치는 필요 없지만, 참고용:

| 확장 | 용도 |
|---|---|
| `pgcrypto` | UUID v4 (`gen_random_uuid()`) 등 |

---

## 체크리스트 (새 컴퓨터에서 처음 셋업할 때)

- [ ] `python --version` → 3.12 이상
- [ ] `node --version` → 18 이상
- [ ] `psql --version` → 16
- [ ] `git clone <이 저장소 주소>`
- [ ] `docs/34_local_dev_setup_no_docker.md` (또는 Docker 가능하면 `docs/25_devops.md`) 순서대로 진행
