"""백업 → 복원 실검증. **개발 DB 에 덮어쓰지 않는다.**

```
pg_dump 개발 DB
  ↓
일회용 복원 DB 생성 (picksome_restore_test_<난수>)
  ↓
restore
  ↓
revision · 행수 · 무결성 확인
  ↓
복원 DB 삭제
```

왜 필요한가
---------
`docs/25_devops.md` 는 `pg_dump` cron 과 "주 1회 복원 검증" 을 적어 뒀지만
**한 번도 실제로 돌려본 적이 없다.** 백업은 복원해 보기 전까지 백업이 아니다.

안전장치
------
복원 대상 이름이 `_restore_test_` 를 포함하지 않으면 **거부한다.** 개발·운영
DB 이름을 실수로 넣어도 덮어쓰지 않는다. 마이그레이션 검증기와 같은 철학이다.

비밀
----
덤프 파일에는 `credential_secret.ciphertext` 가 들어간다. 그건 암호문이고
**마스터 키는 환경변수에 있어 덤프에 없다** — 이 스크립트가 그 사실을
확인한다. 어떤 비밀 값도 출력하지 않는다.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import re
import secrets
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit

import psycopg

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.config import get_settings  # noqa: E402

#: 복원해도 되는 DB 이름. 마이그레이션 검증기의 `_migcheck` 와 같은 장치다.
_RESTORE_NAME = re.compile(r"_restore_test_[a-z0-9]+$")

#: pg_dump/pg_restore 위치. PATH 에 없으면 여기서 찾는다.
_PG_BIN_CANDIDATES = (
    os.environ.get("PG_BIN", ""),
    r"C:\PostgreSQL\16\bin",
    r"C:\Program Files\PostgreSQL\16\bin",
    "/usr/bin", "/usr/local/bin",
)

#: 복원 후 대조할 테이블. 돈·재고·감사에 직접 닿는 것만 고른다.
#:
#: `sales_claim` / `sales_adjustment` 가 여기 있는 이유(M053·M054): 유효 매출은
#: `sales_order` 만으로 나오지 않는다. 조정이 빠진 채 복원되면 매출이 조정액
#: 만큼 커진 상태로 되살아나고, **행수는 전부 ok 로 보인다.**
#:
#: `marketplace_settlement_record` / `settlement_adjustment` 도 같은 이유다
#: (M055). 정산 대사 결과와 정정이 빠진 채 복원되면 매칭을 처음부터 다시 해야
#: 하고, **사람이 승인을 거쳐 붙인 연결이 조용히 사라진다.**
_INTEGRITY_TABLES = (
    "product", "sales_order", "product_margin_actual", "expenses",
    "sales_claim", "sales_adjustment",
    "marketplace_settlement_record", "settlement_adjustment",
    "inventory_transaction", "purchase_order", "purchase_order_line",
    "approval_request", "audit_log", "credential_secret",
    "monthly_settlement", "payout_item",
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
    raise RuntimeError(
        f"{name} 을 찾지 못했습니다. PG_BIN 환경변수로 경로를 알려 주세요.")


def _swap_db(url: str, database: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{database}"))


def _libpq(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _counts(dsn: str) -> dict[str, int]:
    out: dict[str, int] = {}
    with psycopg.connect(dsn) as conn:
        for table in _INTEGRITY_TABLES:
            try:
                out[table] = conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                conn.rollback()
                out[table] = -1        # 테이블 없음 — 복원 실패 신호
    return out


def _revision(dsn: str) -> str | None:
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
    return row[0] if row else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep", action="store_true",
                    help="복원 DB 를 지우지 않는다(디버깅)")
    args = ap.parse_args()

    settings = get_settings()
    source_url = settings.DATABASE_URL
    source_db = urlsplit(source_url).path.lstrip("/")

    # DB 생성·삭제는 전용 역할이 한다. 운영 역할에 CREATEDB 를 주지 않는다.
    role = os.environ.get("RESTORE_ROLE", "picksome_test")
    password = os.environ.get("RESTORE_PASSWORD", "picksome_test")
    parts = urlsplit(source_url)
    host = parts.hostname or "localhost"
    port = f":{parts.port}" if parts.port else ""
    admin_url = urlunsplit(parts._replace(
        netloc=f"{role}:{password}@{host}{port}", path="/postgres"))

    restore_db = f"picksome_restore_test_{secrets.token_hex(4)}"
    if not _RESTORE_NAME.search(restore_db):
        print("[중단] 복원 대상 이름이 안전 패턴이 아닙니다.")
        return 2
    if restore_db == source_db:
        print("[중단] 복원 대상이 원본과 같습니다.")
        return 2

    restore_url = urlunsplit(parts._replace(
        netloc=f"{role}:{password}@{host}{port}", path=f"/{restore_db}"))

    print(f"[원본]   {source_db}")
    print(f"[복원지] {restore_db}  (일회용)")

    pg_dump = _tool("pg_dump")
    pg_restore = _tool("pg_restore")

    created = False
    tmpdir = tempfile.mkdtemp(prefix="picksome_backup_")
    dump_path = pathlib.Path(tmpdir) / "backup.dump"
    try:
        # ── 백업 ───────────────────────────────────────────────
        env = {**os.environ, "PGPASSWORD": parts.password or ""}
        result = subprocess.run(
            [pg_dump, "--format=custom", "--no-owner", "--no-privileges",
             "--file", str(dump_path), _libpq(source_url)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env)
        if result.returncode != 0:
            print("[실패] pg_dump:", (result.stderr or "")[-300:])
            return 1
        size = dump_path.stat().st_size
        print(f"[백업]   {size:,} bytes")
        if size == 0:
            print("[중단] 백업 파일이 비었습니다.")
            return 1

        # ── 복원 DB 생성 ───────────────────────────────────────
        with psycopg.connect(_libpq(admin_url), autocommit=True) as conn:
            conn.execute(f'CREATE DATABASE "{restore_db}"')
        created = True
        with psycopg.connect(_libpq(admin_url), autocommit=True) as conn:
            ok = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (restore_db,)).fetchone()
        if not ok:
            print("[중단] 복원 DB 생성이 확인되지 않습니다.")
            return 2
        print("[생성]   확인됨")

        # ── 복원 ───────────────────────────────────────────────
        env2 = {**os.environ, "PGPASSWORD": password}
        result = subprocess.run(
            [pg_restore, "--no-owner", "--no-privileges", "--exit-on-error",
             "--dbname", _libpq(restore_url), str(dump_path)],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", env=env2)
        if result.returncode != 0:
            print("[실패] pg_restore:", (result.stderr or "")[-400:])
            return 1
        print("[복원]   완료")

        # ── 무결성 ─────────────────────────────────────────────
        src_rev = _revision(_libpq(source_url))
        dst_rev = _revision(_libpq(restore_url))
        print(f"[revision] 원본={src_rev} 복원={dst_rev} "
              f"{'ok' if src_rev == dst_rev else '← 불일치'}")

        src = _counts(_libpq(source_url))
        dst = _counts(_libpq(restore_url))
        mismatched = []
        print("[행수]")
        for table in _INTEGRITY_TABLES:
            same = src[table] == dst[table] and dst[table] >= 0
            if not same:
                mismatched.append(table)
            print(f"  {table:28} 원본={src[table]:>7}  복원={dst[table]:>7}  "
                  f"{'ok' if same else '← 불일치'}")

        # ── 비밀 분리 ──────────────────────────────────────────
        #
        # 덤프에는 암호문이 들어간다. **마스터 키가 들어가면 안 된다** —
        # 둘이 같은 곳에 있으면 암호화가 무의미해진다.
        master = str(getattr(settings, "CREDENTIAL_MASTER_KEY", "") or "")
        raw = dump_path.read_bytes()
        key_in_dump = bool(master) and master.encode() in raw
        print(f"[마스터키] 덤프에 포함={'YES ← 위험' if key_in_dump else 'false'}")

        with psycopg.connect(_libpq(restore_url)) as conn:
            plain = conn.execute("""
                SELECT COUNT(*) FROM credential_secret
                 WHERE ciphertext IS NULL AND deleted_at IS NULL
            """).fetchone()[0]
        print(f"[자격증명] 암호문 없는 행={plain} (0 이어야 정상)")

        # ── 애플리케이션 스모크 ────────────────────────────────
        #
        # 행수만 맞고 실제로 못 읽으면 소용없다. 조인이 걸린 실 쿼리를 돌린다.
        with psycopg.connect(_libpq(restore_url)) as conn:
            smoke = conn.execute("""
                SELECT COUNT(*) FROM sales_order s
                  JOIN product p ON p.id = s.product_id
                 WHERE s.deleted_at IS NULL
            """).fetchone()[0]
        print(f"[스모크]  판매·상품 조인 {smoke}행 조회 ok")

        failed = bool(mismatched) or src_rev != dst_rev or key_in_dump or plain
        print("\n" + ("복원 검증 실패" if failed else "복원 검증 통과"))
        return 1 if failed else 0

    finally:
        if created and not args.keep:
            with psycopg.connect(_libpq(admin_url), autocommit=True) as conn:
                conn.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                    " WHERE datname = %s AND pid <> pg_backend_pid()",
                    (restore_db,))
                conn.execute(f'DROP DATABASE IF EXISTS "{restore_db}"')
            print(f"[정리]   {restore_db} 삭제")
        # 덤프에는 암호문과 사업 데이터가 들어 있다. 남기지 않는다.
        dump_path.unlink(missing_ok=True)
        pathlib.Path(tmpdir).rmdir()


if __name__ == "__main__":
    raise SystemExit(main())
