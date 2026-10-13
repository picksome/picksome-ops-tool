"""운영 상태 조회 — **읽기 전용.** 아무것도 실행하지 않는다.

왜 따로 있나
-----------
기동 스크립트가 스스로 판단하려면 사실이 필요하다. 그 사실을 PowerShell 이
SQL 로 직접 캐면, 같은 판정이 두 곳(파이썬 서비스와 셸 스크립트)에 생기고
언젠가 갈라진다. 판정은 여기서 한 번만 한다.

그리고 이 정보는 Windows 와 무관하다. 나중에 서버로 옮겨도 이 스크립트는
그대로고 launcher 만 바뀐다.

무엇을 답하나
------------
```
DB            붙는가 · 어느 DB 인가 · 스키마 리비전이 코드와 맞는가
주문 · 클레임   마지막 성공 / 마지막 시도 (두 개를 따로)
일일리포트      오늘 것이 있나
백업           마지막 성공이 언제인가 · 밀렸나
예외           지금 몇 건이고 그중 차단은 몇 건인가
worker         마지막 실행 · 상태
```

무엇을 하지 않나
--------------
```
실행 · 수정 · 마이그레이션 · 백업 · 외부 API 호출   전부 안 한다
비밀값 · 접속 비밀번호 출력                        안 한다 (DB 이름까지만)
개인정보 출력                                     안 한다 (건수만)
```

"마지막 성공" 과 "마지막 시도" 를 합치지 않는다
------------------------------------------
합치면 지금 몇 시간째 못 읽고 있는 상태가 성공 시각 뒤에 숨는다.
앱이 켜졌다는 사실과 데이터가 최신이라는 사실은 다른 것이다.

종료 코드
--------
```
0  전부 정상
1  degraded — 서비스는 살아 있으나 확인할 것이 있다 (동기화 실패·백업 밀림 등)
2  DB 에 붙지 못했다
```

실행
----
```
python -m app.scripts.ops_status
python -m app.scripts.ops_status --json
```
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import text

#: 백업이 이 시간을 넘으면 밀린 것으로 본다.
#:
#: `docs/25_devops.md §4-2` 의 **매일 1회** 정책에서 나온 값이다. 여기서
#: 새 주기를 정하는 것이 아니라, 그 정책이 지켜졌는지 판정할 뿐이다.
#: 하루 정확히로 자르면 실행 시각이 몇 분만 밀려도 매번 경고가 뜬다.
BACKUP_DUE_HOURS = 36


def _database_name(url: str) -> str:
    """접속 문자열에서 **DB 이름만.** 사용자·비밀번호·호스트는 버린다."""
    return (urlsplit(url).path or "/").lstrip("/") or "?"


def _code_head() -> str | None:
    """코드가 가진 마이그레이션 head 리비전."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        backend_root = pathlib.Path(__file__).resolve().parents[2]
        cfg = Config(str(backend_root / "alembic.ini"))
        cfg.set_main_option("script_location", str(backend_root / "app/db/migrations"))
        return ScriptDirectory.from_config(cfg).get_current_head()
    except Exception:  # noqa: BLE001 — 알 수 없으면 모른다고 답한다
        return None


def _schema_state(db) -> dict[str, Any]:
    """DB 리비전과 코드 head 를 비교한다. **고치지 않는다.**

    세 가지를 구분한다. 셋의 대응이 전부 다르기 때문이다.

    ```
    CURRENT   같다                    그냥 쓰면 된다
    BEHIND    DB 가 뒤처졌다           upgrade 하면 된다
    AHEAD     DB 가 코드보다 앞섰다     **기동을 막아야 한다**
    ```

    `AHEAD` 는 코드를 되돌린 채 최신 DB 에 붙은 상태다. 그대로 쓰면 없는
    컬럼을 읽거나 새 제약을 모르고 쓴다. downgrade 로 맞추면 데이터를
    잃는다(docs/55 §5) — 그래서 여기서는 사실만 보고한다.
    """
    db_rev = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    head = _code_head()
    if head is None or db_rev is None:
        state = "UNKNOWN"
    elif db_rev == head:
        state = "CURRENT"
    else:
        # 리비전 이름은 M001, M002 … 로 단조 증가한다. 이 저장소의 규칙이고
        # `alembic history` 로 확인할 수도 있지만, 이름 비교면 충분하다.
        state = "BEHIND" if str(db_rev) < str(head) else "AHEAD"
    return {"db_revision": db_rev, "code_head": head, "state": state}


def _sync_freshness(db) -> dict[str, Any]:
    """주문·클레임의 마지막 성공/시도. **조직 전체를 합치지 않는다.**

    계정마다 자격증명도 상태도 다르다. 하나가 죽어 있는데 다른 하나가
    최근에 성공했다고 "정상" 으로 묶으면 죽은 쪽이 숨는다.
    """
    rows = db.execute(text("""
        SELECT r.sync_type,
               a.display_name,
               max(r.started_at)  FILTER (WHERE TRUE)              AS last_attempt_at,
               max(r.finished_at) FILTER (WHERE r.status = 'SUCCESS') AS last_success_at,
               (array_agg(r.status ORDER BY r.started_at DESC))[1]     AS last_status,
               (array_agg(r.error_code ORDER BY r.started_at DESC))[1] AS last_error_code
          FROM external_sync_run r
          JOIN external_account a ON a.id = r.external_account_id
         WHERE r.sync_type IN ('ORDERS', 'CLAIMS')
           AND a.deleted_at IS NULL
         GROUP BY r.sync_type, a.id, a.display_name
         ORDER BY r.sync_type, a.display_name
    """)).mappings().all()

    out: dict[str, list[dict[str, Any]]] = {"ORDERS": [], "CLAIMS": []}
    for r in rows:
        out[r["sync_type"]].append({
            "account": r["display_name"],
            "last_attempt_at": r["last_attempt_at"],
            "last_success_at": r["last_success_at"],
            "last_status": r["last_status"],
            "last_error_code": r["last_error_code"],
        })
    return out


def _daily_report(db) -> dict[str, Any]:
    """오늘(KST) 리포트가 성공적으로 만들어졌나."""
    from app.services import daily_report

    report_date = daily_report.report_date_now()
    built = db.execute(text("""
        SELECT count(*)
          FROM automation_run r
          JOIN automation_policy p ON p.id = r.policy_id
          JOIN automation_action a ON a.run_id = r.id
         WHERE p.automation_type = 'DAILY_REPORT'
           AND a.action_type = 'DAILY_REPORT_BUILT'
           AND a.outcome = 'REPORTED'
           AND (r.started_at AT TIME ZONE 'Asia/Seoul')::date = :d
    """), {"d": report_date}).scalar() or 0
    return {"report_date": report_date.isoformat(),
            "built_today": int(built), "duplicate": max(0, int(built) - 1)}


def _backup(root: str) -> dict[str, Any]:
    """마지막 백업 파일. **파일을 만들지도 지우지도 않는다.**

    `run_backup.py` 가 검증까지 끝난 것만 남기므로, 파일이 있다는 것은
    그 시점에 복원 가능한 덤프가 만들어졌다는 뜻이다. 여기서 다시 검증하지
    않는다 — `pg_restore --list` 는 싸지 않고, 상태 조회가 느리면 아무도 안 본다.
    """
    directory = pathlib.Path(root) / "daily"
    if not directory.exists():
        return {"root": str(directory), "last_success_at": None,
                "age_hours": None, "state": "NEVER_RUN"}
    dumps = sorted(directory.glob("*.dump"), key=lambda p: p.stat().st_mtime)
    if not dumps:
        return {"root": str(directory), "last_success_at": None,
                "age_hours": None, "state": "NEVER_RUN"}
    newest = dumps[-1]
    when = datetime.fromtimestamp(newest.stat().st_mtime, tz=UTC).astimezone()
    age = (datetime.now(UTC).astimezone() - when).total_seconds() / 3600
    return {
        "root": str(directory),
        "last_success_at": when.isoformat(),
        "age_hours": round(age, 1),
        "count": len(dumps),
        "state": "CURRENT" if age <= BACKUP_DUE_HOURS else "DUE",
    }


def _exceptions(db) -> dict[str, Any]:
    """열려 있는 운영 이슈. **건수만** — 내용에는 주문번호가 들어 있다."""
    rows = db.execute(text("""
        SELECT severity, count(*) AS n
          FROM operation_issue
         WHERE status IN ('OPEN', 'ACK')
         GROUP BY severity
    """)).mappings().all()
    by_severity = {r["severity"]: int(r["n"]) for r in rows}
    return {
        "open": sum(by_severity.values()),
        "blocking": by_severity.get("BLOCKING", 0),
        "by_severity": by_severity,
    }


def collect(db, *, backup_root: str, org_id: UUID | None = None) -> dict[str, Any]:
    """상태 한 벌. 어느 하나가 실패해도 나머지는 답한다."""
    from app.config import get_settings
    from app.services import worker_runtime as rt

    settings = get_settings()
    schema = _schema_state(db)
    sync = _sync_freshness(db)
    backup = _backup(backup_root)
    exceptions = _exceptions(db)
    report = _daily_report(db)

    degraded: list[str] = []
    if schema["state"] in ("BEHIND", "AHEAD", "UNKNOWN"):
        degraded.append(f"SCHEMA_{schema['state']}")
    for kind, accounts in sync.items():
        if any(a["last_status"] not in (None, "SUCCESS") for a in accounts):
            degraded.append(f"{kind}_SYNC_NOT_SUCCEEDED")
    if backup["state"] != "CURRENT":
        degraded.append(f"BACKUP_{backup['state']}")
    if exceptions["blocking"]:
        degraded.append("BLOCKING_ISSUES")
    if report["duplicate"]:
        degraded.append("DUPLICATE_DAILY_REPORT")

    return {
        "checked_at": datetime.now(UTC).astimezone().isoformat(),
        # **DB 이름까지만.** 사용자·비밀번호·호스트는 넣지 않는다.
        "database": _database_name(settings.DATABASE_URL),
        "app_env": settings.APP_ENV,
        "schema": schema,
        "sync": sync,
        "daily_report": report,
        "backup": backup,
        "exceptions": exceptions,
        "workers": rt.worker_health(db, org_id=org_id),
        "degraded": degraded,
        "healthy": not degraded,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M")
    return str(value)


def _print_human(state: dict[str, Any]) -> None:
    print("")
    print(f"  DB           {state['database']} ({state['app_env']})")
    schema = state["schema"]
    print(f"  스키마        {schema['state']}  "
          f"DB {_fmt(schema['db_revision'])} / 코드 {_fmt(schema['code_head'])}")
    for kind, label in (("ORDERS", "주문 동기화"), ("CLAIMS", "클레임 수집")):
        accounts = state["sync"][kind]
        if not accounts:
            print(f"  {label}   실행된 적 없음")
            continue
        for a in accounts:
            # 성공은 **완료** 시각, 시도는 **시작** 시각이다. 마지막 시도가
            # 곧 성공한 실행이면 성공이 더 뒤로 찍힌다 — 정상이다.
            print(f"  {label}   {a['account']}  "
                  f"마지막 성공(완료) {_fmt(a['last_success_at'])} / "
                  f"마지막 시도(시작) {_fmt(a['last_attempt_at'])} "
                  f"[{_fmt(a['last_status'])}]")
    rep = state["daily_report"]
    print(f"  일일리포트    {rep['report_date']} "
          + ("생성됨" if rep["built_today"] else "없음"))
    bak = state["backup"]
    print(f"  백업          {bak['state']}  마지막 {_fmt(bak['last_success_at'])}")
    exc = state["exceptions"]
    print(f"  예외          {exc['open']}건 (차단 {exc['blocking']}건)")
    print("")
    if state["healthy"]:
        print("  상태  정상")
    else:
        print("  상태  확인 필요 — " + ", ".join(state["degraded"]))
    print("")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ops_status", description="운영 상태 조회 (읽기 전용)")
    parser.add_argument("--org", help="조직 UUID (worker 상태를 좁힌다)")
    parser.add_argument("--json", action="store_true", help="JSON 만 출력")
    parser.add_argument("--backup-root",
                        default=os.environ.get("BACKUP_ROOT", "backup"),
                        help="백업 루트 (기본: backup)")
    args = parser.parse_args(argv)

    from app.db.session import SessionLocal

    try:
        db = SessionLocal()
        state = collect(db, backup_root=args.backup_root,
                        org_id=UUID(args.org) if args.org else None)
    except Exception as exc:  # noqa: BLE001
        # 접속 문자열이 예외 메시지에 섞여 나오는 경우가 있다. 종류만 남긴다.
        print(json.dumps({"error": type(exc).__name__, "healthy": False},
                         ensure_ascii=False))
        return 2
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass

    print(json.dumps(state, ensure_ascii=False, default=str, indent=2))
    if not args.json:
        _print_human(state)
    return 0 if state["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())
