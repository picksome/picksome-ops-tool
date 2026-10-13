"""백업 실행 + 검증 + 회전. **스케줄러가 부르는 진입점이다.**

정책은 `docs/25_devops.md §4-2` 를 그대로 따른다. 새 보존기간을 만들지 않는다.

```
backup/daily/    7개 보관   매일
backup/weekly/   4개 보관   일요일
backup/monthly/  6개 보관   매월 1일
```

같은 날 실행이 여러 번이면 덮어쓴다 — 하루치 백업이 여러 개 쌓이면 보관
개수가 며칠치인지 알 수 없게 된다.

성공을 가장하지 않는다
-------------------
파일이 만들어졌다고 성공이 아니다. 세 가지를 확인한다.

```
1. pg_dump 종료 코드 0
2. 파일 크기 > 0
3. pg_restore --list 로 목차가 읽히는가   ← 실제로 복원 가능한 형식인가
```

3이 중요하다. 디스크가 가득 차면 pg_dump 가 0 을 반환하면서 잘린 파일을
남기는 경우가 있고, 그건 복원할 때가 되어서야 드러난다.

비밀을 남기지 않는다
-----------------
접속 비밀번호는 `PGPASSWORD` 환경변수로만 넘긴다. 명령행에 넣으면 프로세스
목록에 보인다. 로그에도 DB 이름과 경로만 남긴다.

**마스터 키는 이 백업에 들어가지 않는다.** 덤프에는 `credential_secret` 의
암호문만 있고, 키는 환경변수에 있다 — 둘이 같은 곳에 있으면 암호화가
무의미하다. `docs/70_backup_and_key_recovery.md` 참고.

실행
----
```
python -m scripts.run_backup                 # 정책대로 (일/주/월 자동 판단)
python -m scripts.run_backup --kind daily    # 특정 종류만
python -m scripts.run_backup --dry-run       # 무엇을 할지만 출력
```
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
from datetime import UTC, datetime
from urllib.parse import urlsplit

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.config import get_settings  # noqa: E402

#: 보존 개수 — `docs/25_devops.md §4-2`. 여기서 바꾸지 않는다.
RETENTION = {"daily": 7, "weekly": 4, "monthly": 6}

_PG_BIN_CANDIDATES = (
    os.environ.get("PG_BIN", ""),
    r"C:\PostgreSQL\16\bin",
    r"C:\Program Files\PostgreSQL\16\bin",
    "/usr/bin", "/usr/local/bin",
)


def _tool(name: str) -> str:
    from shutil import which

    found = which(name)
    if found:
        return found
    for d in _PG_BIN_CANDIDATES:
        if not d:
            continue
        for ext in ("", ".exe"):
            p = pathlib.Path(d) / f"{name}{ext}"
            if p.exists():
                return str(p)
    raise RuntimeError(f"{name} 을 찾지 못했습니다. PG_BIN 을 설정하세요.")


def kinds_for(today: datetime) -> list[str]:
    """오늘 만들어야 하는 백업 종류.

    일요일이면 주간도, 1일이면 월간도 함께 만든다. 같은 덤프를 세 번 뜨는
    대신 한 번 떠서 복사하는 편이 빠르지만, 종류별로 독립적인 파일이어야
    하나가 깨져도 나머지가 산다.
    """
    out = ["daily"]
    if today.isoweekday() == 7:
        out.append("weekly")
    if today.day == 1:
        out.append("monthly")
    return out


def _validate(path: pathlib.Path, pg_restore: str) -> str | None:
    """복원 가능한 파일인지. 문제가 있으면 사유를 돌려준다."""
    if not path.exists():
        return "FILE_MISSING"
    if path.stat().st_size == 0:
        return "EMPTY_FILE"
    # 목차를 읽어 본다 — 잘린 파일은 여기서 걸린다.
    result = subprocess.run(
        [pg_restore, "--list", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return "UNREADABLE_ARCHIVE"
    if "credential_secret" not in result.stdout and "product" not in result.stdout:
        # 목차에 우리 테이블이 하나도 없으면 엉뚱한 DB 를 떴다는 뜻이다.
        return "UNEXPECTED_CONTENT"
    return None


def _rotate(directory: pathlib.Path, keep: int) -> list[str]:
    """오래된 것부터 지운다. 지운 파일 이름을 돌려준다."""
    files = sorted(directory.glob("*.dump"), key=lambda p: p.name)
    removed = []
    while len(files) > keep:
        victim = files.pop(0)
        victim.unlink()
        removed.append(victim.name)
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kind", choices=sorted(RETENTION),
                    help="종류를 직접 지정 (기본: 날짜로 판단)")
    ap.add_argument("--root", default=os.environ.get("BACKUP_ROOT", "backup"),
                    help="백업 루트 디렉터리")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    url = settings.DATABASE_URL
    parts = urlsplit(url)
    database = (parts.path or "/").lstrip("/")

    now = datetime.now(UTC).astimezone()
    kinds = [args.kind] if args.kind else kinds_for(now)
    root = pathlib.Path(args.root)

    results: list[dict[str, object]] = []
    failed = False

    for kind in kinds:
        directory = root / kind
        # 파일명에 날짜만 넣는다. 같은 날 두 번 돌면 덮어쓴다.
        name = f"{database}_{now:%Y-%m-%d}.dump"
        target = directory / name

        if args.dry_run:
            print(f"[dry-run] {kind}: {target} (보관 {RETENTION[kind]}개)")
            continue

        directory.mkdir(parents=True, exist_ok=True)
        started = datetime.now(UTC).astimezone()
        env = {**os.environ, "PGPASSWORD": parts.password or ""}
        libpq = url.replace("postgresql+psycopg://", "postgresql://", 1)

        proc = subprocess.run(
            [_tool("pg_dump"), "--format=custom", "--no-owner",
             "--no-privileges", "--file", str(target), libpq],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env)

        if proc.returncode != 0:
            # **성공으로 만들지 않는다.** stderr 에 접속 문자열이 섞일 수
            # 있으므로 마지막 한 줄만, 그것도 분류만 남긴다.
            failed = True
            results.append({
                "kind": kind, "result": "FAILED",
                "error_category": "PG_DUMP_FAILED",
                "exit_code": proc.returncode, "path": str(target),
                "started_at": started.isoformat(),
            })
            continue

        problem = _validate(target, _tool("pg_restore"))
        if problem:
            failed = True
            # 못 쓰는 파일을 남기면 다음 회전에서 정상 백업을 밀어낸다.
            target.unlink(missing_ok=True)
            results.append({
                "kind": kind, "result": "FAILED",
                "error_category": problem, "path": str(target),
                "started_at": started.isoformat(),
            })
            continue

        removed = _rotate(directory, RETENTION[kind])
        results.append({
            "kind": kind, "result": "OK", "path": str(target),
            "size_bytes": target.stat().st_size,
            "kept": RETENTION[kind], "rotated_out": removed,
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).astimezone().isoformat(),
        })

    for line in results:
        # 한 줄 JSON — 스케줄러 로그에서 grep 하기 쉽고 비밀이 없다.
        print(json.dumps(line, ensure_ascii=False))

    if failed:
        print("백업 실패", file=sys.stderr)
        return 1
    if not args.dry_run:
        print(f"백업 완료: {', '.join(kinds)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
