"""기동 직후 따라잡기 (Local Operation Mode).

무엇을 해결하나
--------------
서버가 없는 운영에서는 PC 를 끄면 자동화도 멈춘다. 그건 장애가 아니라 정상
이다 — 꺼진 컴퓨터는 원래 아무것도 안 한다. **문제는 다시 켰을 때다.**
그동안 결제된 주문과 발생한 클레임은 아무도 읽지 않았고, 주기 스케줄만으로는
영영 안 읽힌다.

이 스크립트는 그 구멍만 메운다.

```
PC 를 켠다
  ↓
서비스 기동
  ↓
startup_catchup      ← 여기
  ↓
주문 · 클레임 · 오늘 리포트가 최신이 된다
```

무엇을 새로 만들지 않았나
-----------------------
수집 로직·창 계산·멱등 적재·잠금·실패 분류는 **전부 이미 있다.**
`order_sync` / `claim_sync` 가 마지막 성공 시각을 기준으로 창을 넓히고
(M058 주석 참고), 커넥터가 벤더 한도(10일)로 창을 나눈다. 한 달을 꺼 뒀어도
같은 코드가 같은 답을 낸다.

그래서 여기서 하는 일은 셋뿐이다.

```
1. 켜진 ORDER_SYNC / CLAIM_SYNC 정책을 주기와 무관하게 한 번 돌린다
2. 오늘 리포트가 없으면 만든다 (중복은 기존 dedupe 가 막는다)
3. 나머지 worker 를 평소 한 바퀴 돌린다
```

왜 주기를 무시하나
----------------
주기(`due_policies`)는 "마지막 실행 + 간격 <= 지금" 을 본다. 어제 저녁까지
PC 가 켜져 있었고 아침에 다시 켰다면 간격이 아직 안 됐을 수 있는데, 그
사이에 밤새 주문이 들어왔다. 기동은 주기와 다른 사건이므로 따로 센다.

두 번 눌러도 안전하다 — 적재가 멱등이고 계정마다 advisory lock 이 있다.

무엇을 하지 않나
--------------
```
외부 WRITE                    0. 읽기만 한다
과거 날짜 리포트 소급 생성      안 한다. 오늘 것만
환불·현금 posting             안 한다 (증거 게이트 그대로)
마이그레이션 · 백업 · seed      안 한다. 각자 자기 스크립트가 있다
```

종료 코드
--------
```
0  따라잡기 성공 (돌릴 것이 없었던 경우 포함)
1  일부 실패 — 서비스는 쓸 수 있지만 **데이터가 최신이 아니다**
2  실행 자체가 불가능 (DB 접속 실패 등)
```

`1` 을 `0` 으로 뭉개지 않는 것이 이 스크립트의 존재 이유 절반이다. 앱이
열렸다는 사실과 데이터가 최신이라는 사실은 다른 것이고, 둘을 합치면
사용자는 틀린 숫자를 믿는다.

실행
----
```
python -m app.scripts.startup_catchup
python -m app.scripts.startup_catchup --org <UUID>
python -m app.scripts.startup_catchup --json      # 사람용 요약 없이 JSON 만
```
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db.session import SessionLocal

_log = logging.getLogger("startup_catchup")

#: 기동 때 주기와 무관하게 돌리는 정책. **순서에 의미가 있다.**
#:
#: 주문 → 클레임 → 리포트. 리포트는 방금 들어온 주문·클레임을 봐야 한다.
#: 먼저 만들면 어제 숫자로 오늘 리포트를 쓰게 된다.
FORCED_TYPES = ("ORDER_SYNC", "CLAIM_SYNC", "DAILY_REPORT")

#: `external_sync_run.trigger` / `worker_run.trigger` 에 남길 값 (M058).
TRIGGER = "STARTUP"


def _orgs(db, org_id: UUID | None) -> list[UUID]:
    """따라잡기 대상 조직.

    자동화 정책이 하나라도 있는 조직만 본다. 정책이 없으면 돌릴 것이 없고,
    빈 조직까지 훑으면 조직이 늘수록 기동이 느려진다.
    """
    if org_id is not None:
        return [org_id]
    return [UUID(str(r[0])) for r in db.execute(text("""
        SELECT DISTINCT org_id FROM automation_policy
         WHERE enabled AND deleted_at IS NULL
    """))]


def _forced_runs(db, *, org_id: UUID) -> list[dict[str, Any]]:
    """켜진 ORDER_SYNC / CLAIM_SYNC / DAILY_REPORT 를 한 번씩.

    한 정책의 실패가 다음을 막지 않는다. 주문 수집이 인증 오류로 죽어도
    클레임과 리포트는 돌아야 한다 — 하나 때문에 전부 멈추면 따라잡기가
    "전부 아니면 전무" 가 되고, 그러면 아무것도 안 따라잡힌다.
    """
    from app.services import automation as auto
    from app.services import automation_runners as runners

    by_type = {
        p["automation_type"]: p
        for p in auto.list_policies(db, org_id=org_id)
        if p["id"] is not None and p["enabled"]
    }

    out: list[dict[str, Any]] = []
    for automation_type in FORCED_TYPES:
        policy = by_type.get(automation_type)
        if policy is None:
            # 꺼져 있거나 아예 없다. **실패가 아니다** — 사용자가 안 켠 것이다.
            out.append({"automation_type": automation_type, "status": "NOT_ENABLED"})
            continue
        try:
            result = runners.run_policy(
                db, org_id=org_id, policy=policy, trigger=TRIGGER)
            out.append(result.as_dict())
        except Exception as exc:  # noqa: BLE001 — 정책 단위 격리
            db.rollback()
            _log.exception("%s 따라잡기 실패", automation_type)
            out.append({
                "automation_type": automation_type, "status": "FAILED",
                "error_code": type(exc).__name__,
            })
    return out


def _sync_summary(db, *, since: datetime, org_id: UUID | None) -> dict[str, Any]:
    """이번 기동에서 실제로 무엇을 읽었나.

    자동화 실행 요약이 아니라 **외부 동기화 실행 기록**을 센다. 자동화
    요약은 "정책이 돌았다" 까지고, 몇 건을 읽어 몇 건이 새로 생겼는지는
    `external_sync_run` 이 갖는다.

    개인정보를 꺼내지 않는다 — 건수와 상태 코드뿐이다.
    """
    # 조직 필터를 SQL 안에서 `:org IS NULL OR ...` 로 쓰지 않는다. 바인딩된
    # NULL 은 타입이 없어서 PostgreSQL 이 `AmbiguousParameter` 로 거부한다.
    # 조건 자체를 파이썬에서 붙이면 그 함정이 아예 없다.
    params: dict[str, Any] = {"tr": TRIGGER, "since": since}
    org_filter = ""
    if org_id is not None:
        org_filter = "AND org_id = CAST(:org AS uuid)"
        params["org"] = str(org_id)

    rows = db.execute(text(f"""
        SELECT sync_type, status,
               count(*)                        AS runs,
               coalesce(sum(read_count), 0)    AS fetched,
               coalesce(sum(created_count), 0) AS created,
               coalesce(sum(updated_count), 0) AS updated,
               coalesce(sum(skipped_count), 0) AS unchanged,
               coalesce(sum(conflict_count), 0) AS conflict,
               coalesce(sum(error_count), 0)   AS rejected
          FROM external_sync_run
         WHERE trigger = :tr AND started_at >= :since {org_filter}
         GROUP BY sync_type, status
         ORDER BY sync_type, status
    """), params).mappings().all()

    out: dict[str, Any] = {}
    for r in rows:
        bucket = out.setdefault(r["sync_type"], {
            "runs": 0, "fetched": 0, "created": 0, "updated": 0,
            "unchanged": 0, "conflict": 0, "rejected": 0,
            "failed_runs": 0, "statuses": {},
        })
        bucket["runs"] += r["runs"]
        for key in ("fetched", "created", "updated", "unchanged",
                    "conflict", "rejected"):
            bucket[key] += int(r[key])
        bucket["statuses"][r["status"]] = r["runs"]
        # RUNNING 도 성공이 아니다 — 프로세스가 중간에 죽으면 그대로 남는다.
        if r["status"] != "SUCCESS":
            bucket["failed_runs"] += r["runs"]
    return out


def _daily_report_state(db, *, org_id: UUID | None) -> dict[str, Any]:
    """오늘(KST) 리포트가 만들어졌나. **중복 개수도 함께 센다.**

    "만들어졌다" 만 보면 중복 생성을 못 잡는다. 하루에 두 개가 만들어지면
    알림도 두 번 가고, 어느 쪽이 맞는지 사용자가 판단해야 한다.
    """
    from app.services import daily_report

    report_date = daily_report.report_date_now()
    params: dict[str, Any] = {"d": report_date}
    org_filter = ""
    if org_id is not None:  # 바인딩된 NULL 비교를 쓰지 않는다 (`_sync_summary` 주석)
        org_filter = "AND r.org_id = CAST(:org AS uuid)"
        params["org"] = str(org_id)
    built = db.execute(text(f"""
        SELECT count(*)
          FROM automation_run r
          JOIN automation_policy p ON p.id = r.policy_id
          JOIN automation_action a ON a.run_id = r.id
         WHERE p.automation_type = 'DAILY_REPORT'
           AND a.action_type = 'DAILY_REPORT_BUILT'
           AND a.outcome = 'REPORTED'
           AND (r.started_at AT TIME ZONE 'Asia/Seoul')::date = :d {org_filter}
    """), params).scalar() or 0
    return {
        "report_date": report_date.isoformat(),
        "built_today": int(built),
        "duplicate": max(0, int(built) - 1),
    }


def run(db, *, org_id: UUID | None = None, skip_workers: bool = False) -> dict[str, Any]:
    """따라잡기 한 번. 요약을 돌려준다."""
    from app.scripts.run_workers import run_all
    from app.services import order_sync as osync
    from app.services import worker_runtime as rt

    # 시작 시각을 먼저 잡는다. 이 뒤에 만들어진 실행 기록만 이번 기동의
    # 것이다 — 앞선 실행의 숫자를 이번 요약에 섞으면 "따라잡았다" 가
    # 거짓이 된다.
    since = db.execute(text("SELECT now()")).scalar()

    # 지난번에 동기화 도중 전원이 꺼졌을 수 있다. 그때 남은 RUNNING 기록을
    # 먼저 정리한다 — 놔두면 화면이 "지금 조회 중" 이라고 계속 말한다.
    reclaimed = rt.reclaim_stale_runs(db)
    reclaimed_syncs = osync.reclaim_stale_sync_runs(db)

    forced: dict[str, list[dict[str, Any]]] = {}
    for oid in _orgs(db, org_id):
        forced[str(oid)] = _forced_runs(db, org_id=oid)

    workers: dict[str, Any] | None = None
    if not skip_workers:
        # 나머지 worker 는 평소 한 바퀴. 자동화 스캔도 여기 들어 있지만,
        # 방금 강제 실행한 세 정책은 `last_run_at` 이 막 갱신돼 주기가
        # 안 됐으므로 다시 돌지 않는다.
        workers = run_all(db, org_id=org_id, trigger=TRIGGER)

    sync = _sync_summary(db, since=since, org_id=org_id)
    failed_syncs = sum(int(v["failed_runs"]) for v in sync.values())
    failed_forced = sum(
        1 for runs in forced.values() for r in runs
        if r.get("status") in ("FAILED", "PARTIAL")
    )
    failed_workers = int(workers["failed_workers"]) if workers else 0

    return {
        "started_at": since.isoformat() if since else None,
        "reclaimed_stale_runs": reclaimed,
        "reclaimed_stale_sync_runs": reclaimed_syncs,
        "forced_policies": forced,
        "sync": sync,
        "daily_report": _daily_report_state(db, org_id=org_id),
        "workers": workers,
        "failed_forced_policies": failed_forced,
        "failed_sync_runs": failed_syncs,
        "failed_workers": failed_workers,
        # 서비스 기동과 별개의 판정이다. 이 값이 False 여도 앱은 쓸 수 있다.
        "data_is_current": (
            failed_syncs == 0 and failed_forced == 0 and failed_workers == 0),
    }


def _pad(label: str, width: int = 12) -> str:
    """한글은 터미널에서 두 칸을 차지한다. 글자 수로 맞추면 열이 어긋난다."""
    used = sum(2 if ord(ch) > 0x2E7F else 1 for ch in label)
    return label + " " * max(1, width - used)


def _forced_status(summary: dict[str, Any], automation_type: str) -> str | None:
    """이 유형의 정책이 어떤 상태로 끝났나. 없으면 None."""
    for runs in summary["forced_policies"].values():
        for run in runs:
            if run.get("automation_type") == automation_type:
                return str(run.get("status"))
    return None


def _print_human(summary: dict[str, Any]) -> None:
    """사람이 읽는 요약. **개인정보·비밀값 없음.**"""
    print("")
    print("  기동 따라잡기 결과")
    print("  " + "-" * 44)
    sync = summary["sync"]
    for name, label, policy_type in (
        ("ORDERS", "주문", "ORDER_SYNC"),
        ("CLAIMS", "클레임", "CLAIM_SYNC"),
    ):
        bucket = sync.get(name)
        if bucket is None:
            # 실행 기록이 없다. 꺼져 있는 것과 실패한 것을 섞지 않는다 —
            # 사용자가 할 일이 다르다("켜세요" vs "원인을 보세요").
            state = _forced_status(summary, policy_type)
            if state == "NOT_ENABLED":
                print(f"  {_pad(label)}—     자동 수집이 꺼져 있습니다")
            else:
                print(f"  {_pad(label)}—     설정이 완료되지 않았습니다")
            continue
        mark = "OK  " if bucket["failed_runs"] == 0 else "실패"
        print(f"  {_pad(label)}{mark}  조회 {bucket['fetched']} · "
              f"신규 {bucket['created']} · 갱신 {bucket['updated']} · "
              f"변화없음 {bucket['unchanged']}")
        if bucket["failed_runs"]:
            print(f"  {_pad('')}      실패한 실행 {bucket['failed_runs']}건 "
                  f"({', '.join(f'{k} {v}' for k, v in bucket['statuses'].items())})")

    rep = summary["daily_report"]
    if rep["built_today"]:
        note = f"{rep['report_date']} 생성됨"
        if rep["duplicate"]:
            note += f" (중복 {rep['duplicate']}건)"
    elif _forced_status(summary, "DAILY_REPORT") == "NOT_ENABLED":
        note = "자동 생성이 꺼져 있습니다"
    else:
        note = f"{rep['report_date']} 생성되지 않았습니다"
    print(f"  {_pad('일일리포트')}{note}")

    print("  " + "-" * 44)
    if summary["data_is_current"]:
        print("  데이터 최신성  OK")
    else:
        print("  데이터 최신성  확인 필요 — 예외 센터에서 원인을 보세요")
    print("")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="startup_catchup",
        description="PC 를 켠 뒤 못 돈 기간을 따라잡는다 (외부 READ only)")
    parser.add_argument("--org", help="조직 UUID (없으면 전체)")
    parser.add_argument("--json", action="store_true",
                        help="JSON 만 출력 (사람용 요약 생략)")
    parser.add_argument("--skip-workers", action="store_true",
                        help="자동화 정책만 돌리고 나머지 worker 는 건너뛴다")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    # worker 프로세스와 같은 이유로 커넥터를 여기서 등록한다. 빠뜨리면
    # 화면에서는 되는 동기화가 기동 따라잡기에서만 "연동 미구현" 이 된다.
    from app.connectors.bootstrap import register_builtin

    register_builtin()

    try:
        db = SessionLocal()
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"error": f"DB 접속 실패: {type(exc).__name__}"},
                         ensure_ascii=False))
        return 2

    try:
        summary = run(db, org_id=UUID(args.org) if args.org else None,
                      skip_workers=args.skip_workers)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        _log.exception("따라잡기 실행 실패")
        print(json.dumps({"error": type(exc).__name__}, ensure_ascii=False))
        return 2
    finally:
        db.close()

    print(json.dumps(summary, ensure_ascii=False, default=str, indent=2))
    if not args.json:
        _print_human(summary)
    return 0 if summary["data_is_current"] else 1


if __name__ == "__main__":
    sys.exit(main())
