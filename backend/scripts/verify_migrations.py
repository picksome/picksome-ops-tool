"""마이그레이션 되돌리기 검증 — **임시 DB 에서만** 돈다.

```
scratch DB 생성
  ↓ base → head          처음부터 다 올라가는가
  ↓ head → 직전          되돌아가는가
  ↓ 직전 → head          다시 올라가는가
scratch DB 삭제
```

왜 스크립트로 만드나
-----------------
예전에는 이 절차를 손으로 쳤다. 그러다 `ALEMBIC_DATABASE_URL` 이 무시되는 줄
모르고 **개발 DB 를 내렸다.** 손으로 치는 절차는 언젠가 틀린다.

이 스크립트는 매 단계에서 대상을 다시 확인한다:

```
1. 이름이 scratch 인가        (app/db/migration_target.py)
2. APP_ENV 가 운영이 아닌가
3. 개발 메인 DB 가 아닌가
4. **실제로 그 DB 가 존재하는가**
```

4번이 중요하다. 예전 사고에서 `picksome_migcheck` 는 **존재하지도 않았는데**
그 사실이 드러나지 않은 채 명령이 다른 DB 로 흘러갔다. 이제는 만들고, 만들어진
것을 확인하고, 그 다음에 마이그레이션을 돌린다.

실행
----
```
python -m scripts.verify_migrations
python -m scripts.verify_migrations --keep     삭제하지 않고 남긴다(디버깅)
```
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.config import get_settings  # noqa: E402
from app.db.migration_target import (  # noqa: E402
    UnsafeMigrationTargetError,
    assert_safe_for_destructive,
    describe_target,
)

#: 임시 DB 를 만들 역할. **운영 역할(picksome)에는 CREATEDB 를 주지 않는다** —
#: 애플리케이션이 임의로 DB 를 만들 수 있으면 최소권한이 깨진다.
#: 통합테스트(conftest.py)가 쓰는 역할과 같은 것을 재사용한다.
_SCRATCH_ROLE = os.environ.get("MIGCHECK_ROLE", "picksome_test")
_SCRATCH_PASSWORD = os.environ.get("MIGCHECK_PASSWORD", "picksome_test")


def _swap_database(url: str, database: str) -> str:
    """URL 의 DB 이름만 바꾼다. 자격증명·호스트는 그대로 둔다."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{database}"))


def _as_scratch_role(url: str) -> str:
    """자격증명만 임시 DB 역할로 바꾼다. 호스트·DB 는 그대로."""
    parts = urlsplit(url)
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{_SCRATCH_ROLE}:{_SCRATCH_PASSWORD}@{host}{port}"
    return urlunsplit(parts._replace(netloc=netloc))


def _psycopg_url(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _run_alembic(url: str, *args: str) -> None:
    """alembic 을 **하위 프로세스로** 돌린다.

    같은 프로세스에서 부르면 `get_settings()` 캐시와 env.py 전역 상태가 단계
    사이에 남는다. 프로세스를 나누면 각 단계가 깨끗한 상태에서 시작한다.
    """
    env = {**os.environ, "ALEMBIC_DATABASE_URL": url,
           "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=_BACKEND_ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        # stdout/stderr 에 URL 이 섞일 수 있다. 마지막 줄만 보여준다.
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        raise RuntimeError(
            f"alembic {' '.join(args)} 실패:\n  " + "\n  ".join(tail))


def _database_exists(admin_url: str, database: str) -> bool:
    with psycopg.connect(_psycopg_url(admin_url), autocommit=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
    return row is not None


def _current_revision(url: str) -> str:
    """지금 이 DB 가 올라가 있는 revision.

    `alembic history` 출력을 파싱하지 않는다 — 형식이 바뀌면 조용히 틀린 값을
    읽고, 그러면 엉뚱한 곳으로 downgrade 한다. `current` 는 형식이 단순하다.
    """
    env = {**os.environ, "ALEMBIC_DATABASE_URL": url,
           "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=_BACKEND_ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    ).stdout
    for line in out.splitlines():
        token = line.strip().split()
        # "M048 (head)" 또는 "M048"
        if token and not token[0].startswith("["):
            return token[0]
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true",
                        help="검증 후 임시 DB 를 지우지 않는다")
    args = parser.parse_args()

    settings = get_settings()
    dev_url = settings.DATABASE_URL

    # 임시 DB 이름에 난수를 붙인다. 같은 이름이 남아 있으면 앞 실행의 잔재가
    # 결과를 오염시킨다.
    scratch_db = f"picksome_migcheck_{secrets.token_hex(4)}"
    # 임시 DB 는 처음부터 끝까지 **전용 역할**로만 다룬다. 운영 역할에
    # CREATEDB 를 주지 않기 위해서다.
    scratch_url = _as_scratch_role(_swap_database(dev_url, scratch_db))
    admin_url = _as_scratch_role(_swap_database(dev_url, "postgres"))

    target = describe_target(
        scratch_url, app_env=settings.APP_ENV, development_url=dev_url)
    print(f"[검증 대상] {target.describe()}")

    try:
        assert_safe_for_destructive(target)
    except UnsafeMigrationTargetError as exc:
        print(f"[중단] {exc}")
        return 2

    # 개발 DB 가 이 절차에 절대 등장하지 않는지 마지막으로 못 박는다.
    dev_target = describe_target(dev_url, app_env=settings.APP_ENV,
                                 development_url=dev_url)
    if target.database == dev_target.database:
        print("[중단] 임시 DB 이름이 개발 DB 와 같습니다.")
        return 2

    created = False
    try:
        with psycopg.connect(_psycopg_url(admin_url), autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{scratch_db}"')
        created = True

        # **만들었다고 믿지 않고 확인한다.** 예전 사고의 핵심이 여기였다.
        if not _database_exists(admin_url, scratch_db):
            print(f"[중단] {scratch_db} 생성이 확인되지 않습니다.")
            return 2
        print(f"[생성] {scratch_db} 확인됨")

        print("[1/3] base → head")
        _run_alembic(scratch_url, "upgrade", "head")
        head = _current_revision(scratch_url)
        if not head:
            print("[중단] 현재 revision 을 읽지 못했습니다.")
            return 2
        print(f"      head={head}")

        # `-1` 은 alembic 이 직접 계산한다. 우리가 이름을 파싱해서 넘기면
        # 잘못 읽었을 때 엉뚱한 지점까지 내려간다.
        print(f"[2/3] {head} → 직전(-1)")
        _run_alembic(scratch_url, "downgrade", "-1")
        previous = _current_revision(scratch_url)
        if previous == head:
            print("[중단] downgrade 후에도 revision 이 그대로입니다.")
            return 2
        print(f"      직전={previous or 'base'}")

        print(f"[3/3] {previous or 'base'} → head")
        _run_alembic(scratch_url, "upgrade", "head")
        if _current_revision(scratch_url) != head:
            print("[중단] 다시 올라간 revision 이 head 와 다릅니다.")
            return 2

        print("\n검증 통과")
        print(f"  target_database          = {scratch_db}")
        print("  is_scratch               = true")
        print(f"  development_db_untouched = true ({dev_target.database} 미접속)")
        return 0

    except Exception as exc:  # noqa: BLE001
        print(f"[실패] {exc}")
        return 1

    finally:
        if created and not args.keep:
            with psycopg.connect(_psycopg_url(admin_url), autocommit=True) as conn:
                # 다른 세션이 붙어 있으면 DROP 이 막힌다. 임시 DB 라 끊어도 된다.
                conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = %s AND pid <> pg_backend_pid()",
                    (scratch_db,))
                conn.execute(f'DROP DATABASE IF EXISTS "{scratch_db}"')
            print(f"[정리] {scratch_db} 삭제")


if __name__ == "__main__":
    raise SystemExit(main())
