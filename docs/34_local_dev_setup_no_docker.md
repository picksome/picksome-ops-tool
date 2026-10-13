# 34. 로컬 개발 환경 셋업 — Docker 없이 (Windows)

> 대상 독자: Claude Code / Codex 등 AI 코딩 에이전트, 또는 이 저장소를 처음 여는 개발자
> 전제: **Windows 10 Home** (Docker Desktop이 WSL2 요구사항 문제로 설치 불가한 환경). Windows 11 Pro나 Docker가 되는 환경이면 대신 `README.md`의 `make up` 경로를 쓸 것.
> 목표: 이 문서만 따라가면 PostgreSQL → Backend(FastAPI) → Frontend(Next.js) 까지 한 번에 로컬 기동 가능.

이 문서는 실제로 한 번 막혔다가 뚫은 기록을 바탕으로 쓴 **실행형 런북**이다. "왜 이게 필요한지" 설명보다 "무엇을 실행해야 하는지"에 집중한다. 각 단계 끝에 검증 명령이 붙어 있으니, 스스로 실행해보고 실패하면 바로 아래 "알려진 이슈" 섹션에서 원인을 찾을 것.

> ⚠️ **운영하려는 것이라면 이 문서가 아니다.** 여기 절차는 **개발용**이다
> (`npm run dev` 의 hot reload, 수동 기동, worker 없음).
>
> 매일 장부를 보려고 켜는 것이라면 `docs/85_local_operation_mode.md` 를 보고
> `scripts\start_seller_os.ps1` 한 번을 쓴다 — DB·백엔드·프론트·worker 를
> 순서대로 띄우고, PC 가 꺼져 있던 동안의 주문·클레임까지 따라잡는다.
> 이 문서는 그 스크립트가 무엇을 하는지 이해하거나, 처음 환경을 만들거나,
> 기동이 실패했을 때 손으로 짚어 볼 때 쓴다.

---

## 0. 사전 확인

필요한 프로그램/버전 전체 목록은 [`../REQUIREMENTS.md`](../REQUIREMENTS.md) 참고. 요약:

```powershell
python --version    # 3.12.x 필요
node --version       # 18+ 필요
psql --version       # PostgreSQL 16 이 이미 설치돼 있는지 확인
```

PostgreSQL이 없다면 `C:\PostgreSQL\16` 경로에 PostgreSQL 16을 설치한다 (Docker 대신 네이티브 설치 — Windows 10 Home 표준 우회 경로).

---

## 1. PostgreSQL 확인 / 셋업

### 1-1. 서비스가 떠 있는지 확인

```powershell
sc query postgresql-16
```

`STATE: RUNNING` 이 아니면 시작:

```powershell
net start postgresql-16
```

> ⚠️ 서비스명은 `postgresql-x64-16`이 **아니라 `postgresql-16`** 이다 (버전에 따라 다를 수 있으니 `sc query state= all | findstr /i postgres` 로 정확한 이름을 먼저 확인할 것).

### 1-2. DB / 유저가 없다면 최초 1회 생성

```powershell
$env:PGPASSWORD = "<postgres superuser 비밀번호>"
psql -h localhost -p 5432 -U postgres -c "CREATE USER picksome WITH PASSWORD 'change-me-local';"
psql -h localhost -p 5432 -U postgres -c "CREATE DATABASE picksome_ops OWNER picksome;"
```

### 1-3. 검증

```powershell
$env:PGPASSWORD = "change-me-local"
psql -h localhost -p 5432 -U picksome -d picksome_ops -c "SELECT 1;"
```
`1`이 반환되면 통과.

---

## 2. `.env` 파일

프로젝트 **루트**(`picksome-ops-tool/.env`, `backend/` 아님)에 위치해야 한다.

```powershell
Copy-Item .env.example .env
```

최소 이 값들을 채운다 (로컬 개발 기준):

```env
POSTGRES_USER=picksome
POSTGRES_PASSWORD=change-me-local
POSTGRES_DB=picksome_ops

APP_ENV=dev
DATABASE_URL=postgresql+psycopg://picksome:change-me-local@localhost:5432/picksome_ops
JWT_SECRET=picksome-dev-jwt-secret-key-min-32-bytes-ok
```

> ✅ **해결됨 (2026-07-02)**: 과거엔 `backend/app/config.py`의 `Settings`가 `env_file=".env"`(cwd 상대경로)로 되어 있어서 `cd backend && uvicorn ...`으로 실행하면 `.env`를 못 찾고 `DATABASE_URL` 기본값으로 조용히 폴백해 DB 인증 실패가 나는 문제가 있었다. 지금은 `config.py`가 `Path(__file__).resolve().parents[2] / ".env"`로 **프로젝트 루트 절대경로**를 계산해서 읽으므로, 어느 디렉토리에서 `uvicorn`을 실행하든 환경변수를 따로 export하지 않아도 된다. 아래 3장 실행 명령에 더 이상 `$env:DATABASE_URL = ...` 같은 수동 export가 필요 없다.

---

## 3. Backend 실행

### 3-1. 의존성 확인

```powershell
cd backend
python -c "import uvicorn, fastapi, sqlalchemy, psycopg; print('OK')"
```
`ModuleNotFoundError`가 나면:
```powershell
pip install -e .
```
(`pyproject.toml` 기준 설치. venv 안 쓰고 전역 Python 3.12에 설치되어 있는 것이 현재 검증된 상태.)

### 3-2. 실행

```powershell
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000
```
루트 `.env`를 자동으로 찾아 읽는다 (2장 참고). 값을 일시적으로 오버라이드하고 싶을 때만 `$env:DATABASE_URL = "..."` 식으로 셸 환경변수를 추가로 export하면 된다 (실제 env가 `.env`보다 항상 우선).

### 3-3. 최초 1회: 마이그레이션 + 시드

```powershell
alembic upgrade head
python -m scripts.internal.picksome_init
```

시드 스크립트는 ADMIN 계정을 만들면서 **비밀번호를 콘솔에 1회만 출력**한다 (이메일: `admin@picksome.co.kr`). 그 자리에서 바로 적어둘 것 — 어디에도 저장되지 않는다. `ADMIN_SEED_PASSWORD` 환경변수를 미리 설정해두면 그 값을 그대로 쓰고, 없으면 랜덤 생성한다.

### 3-4. 검증

```powershell
curl.exe -s http://localhost:8000/api/health      # {"status":"ok","env":"dev"}
curl.exe -s http://localhost:8000/healthz          # {"status":"ok"}  ← DB ping 포함, 이게 통과해야 진짜 정상
curl.exe -s http://localhost:8000/api/docs         # Swagger UI (200)
```

`/healthz`가 503이면 2장의 `.env` 경로 문제부터 의심할 것. 에러 로그에 한글이 mojibake(깨진 유니코드 이스케이프)로 찍히는 건 Windows 콘솔 인코딩 문제일 뿐이고, 실제 원인은 보통 `psycopg.OperationalError: ... password ...` (인증 실패) 문구로 알 수 있다.

---

## 4. Frontend 실행

```powershell
cd frontend
npm install     # node_modules 없을 때만
npm run dev
```

검증:
```powershell
curl.exe -s -o NUL -w "%{http_code}`n" http://localhost:3000
```
200 또는 (로그인 리다이렉트로 인한) 307이면 정상.

---

## 5. 전체 검증 체크리스트

- [ ] `sc query postgresql-16` → RUNNING
- [ ] `psql -U picksome -d picksome_ops -c "SELECT 1;"` → 성공
- [ ] `curl http://localhost:8000/healthz` → `{"status":"ok"}`
- [ ] `curl http://localhost:8000/api/docs` → 200
- [ ] `curl http://localhost:3000` → 200/307
- [ ] 브라우저로 `http://localhost:3000` 접속 → 로그인 화면 노출

---

## 6. 알려진 이슈 (Known Issues)

### 6-1. `sales.py` / `sourcing.py`에 숨은 CR 문자 (해결됨, 재발 감시용 기록)
과거 PowerShell 정규식으로 import 줄을 편집하다가 줄 중간에 `\r`(CR, LF 없이 단독)이 섞여 들어간 적이 있었다. 텍스트 에디터·Read 도구에는 한 줄로 보이지만 Python 파서는 단독 `\r`도 줄바꿈으로 처리해서 `SyntaxError: invalid syntax`가 난다. 이미 제거했지만, 앞으로 **PowerShell로 `.py` 파일의 import 줄을 정규식 치환할 때는 특히 주의**. 재발 여부 확인:
```powershell
python -c "
import os
for root, _, files in os.walk('backend/app'):
    if '__pycache__' in root: continue
    for f in files:
        if f.endswith('.py'):
            p = os.path.join(root, f)
            data = open(p, 'rb').read()
            i = 0
            while True:
                idx = data.find(b'\r', i)
                if idx == -1: break
                if data[idx+1:idx+2] != b'\n':
                    print(p, 'stray CR at byte', idx)
                i = idx + 1
"
```

### 6-2. 시드 admin 계정으로 로그인이 안 됨 (해결됨, 2026-07-02)
과거엔 두 가지 문제로 시드 admin 계정 로그인이 구조적으로 불가능했다:
- 이메일 `admin@picksome.local` — `.local`은 예약된(reserved) TLD라 `pydantic`의 `EmailStr` 검증기가 항상 거부.
- 비밀번호 해시가 대응되는 평문이 없는 더미 값.

지금은 `picksome_init.py`가 이메일을 `admin@picksome.co.kr`로 시딩하고, 비밀번호는 `ADMIN_SEED_PASSWORD` env(없으면 랜덤 생성) 값을 해시해서 저장한 뒤 **평문 비밀번호를 콘솔에 1회만 출력**한다 (3-3 참고). 운영 진입 전 반드시 로그인 후 비밀번호를 교체할 것.

### 6-4. `enum_audit_action`에 LOGIN/LOGOUT 누락 (해결됨, 2026-07-02)
`app/services/auth.py`의 로그인/로그아웃은 `record_audit(..., action="LOGIN")` / `"LOGOUT"`을 호출하는데, `M001_initial_schema.py`가 만드는 `enum_audit_action` 타입에는 이 두 값이 없어서 **로그인 시도만 하면 무조건 500 에러**가 나는 상태였다. `CREATE TYPE enum_audit_action AS ENUM (...)`에 `'LOGIN','LOGOUT'`을 추가해서 해결. 기존에 이미 마이그레이션을 적용해둔 로컬 DB가 있다면 `ALTER TYPE enum_audit_action ADD VALUE 'LOGIN';` / `'LOGOUT';`을 한 번 실행하거나, DB를 지우고 처음부터 다시 마이그레이션하면 된다.

### 6-5. `enum_audit_action`에 EXPORT/PERMISSION_CHANGE 누락 (해결됨, 2026-07-05)
6-4와 같은 종류의 문제가 두 곳 더 있었다: `reports.py`/`admin_audit.py`의 export 로그는 `POLICY_OVERRIDE`로 임시 매핑돼 있었고(주석에 TODO로 남아있었음), `services/user.py`의 역할 변경 로그는 `action="PERMISSION_CHANGE"`를 그대로 쓰는데 DB enum에 없어서 **사용자 role을 바꿀 때마다 500 에러**가 나는 상태였다. `M003_audit_action_enum_sync.py` 마이그레이션으로 `EXPORT`/`PERMISSION_CHANGE`/`IMPORT` 3종을 추가하고, 기존에 `POLICY_OVERRIDE`로 남아있던 export 이력을 `EXPORT`로 백필했다. 이미 M002까지 마이그레이션을 적용해둔 로컬 DB는 `python -m alembic upgrade head`만 실행하면 된다.

### 6-3. Windows 콘솔 한글 mojibake
uvicorn 에러 로그에 한글 메시지가 `�` 등으로 깨져 나오는 건 psycopg가 돌려주는 서버 메시지의 인코딩과 Windows 콘솔 코드페이지 불일치 때문이며, 기능상 문제는 아니다. 에러 원인 파악은 영문 키워드(`password`, `OperationalError`, `connection failed` 등)로 판단할 것.

---

## 7. 참고

- Docker 기반 정식 배포 구성은 `docs/25_devops.md` 참고 (Windows 10 Home 환경에서는 사용 불가, Docker 가능한 서버/CI 전용).
- 환경변수 전체 목록은 `README.md`의 "Environment Variables" 표 참고.
- 이 문서는 실행형 런북이므로, 手順이 바뀌면(예: `config.py`의 env_file 경로를 고치면) **이 문서도 함께 갱신**할 것.
