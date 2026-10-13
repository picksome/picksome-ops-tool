"""자동 적용 실행기 CLI.

    python -m app.scripts_run_due            # 전 조직, KST 오늘 기준
    python -m app.scripts_run_due --at 2026-09-01 --batch-size 100

cron 이나 워커 플랫폼이 없어도 이 명령을 주기 실행하면 된다.
여러 개가 동시에 돌아도 `FOR UPDATE SKIP LOCKED` 로 한 번만 적용된다.
"""

from __future__ import annotations

import argparse
import json
from datetime import date

from app.db.session import SessionLocal
from app.services.approval_executor import run_due_approvals


def main() -> int:
    parser = argparse.ArgumentParser(description="승인 요청 자동 적용")
    parser.add_argument("--at", type=str, default=None, help="기준일 YYYY-MM-DD (기본: KST 오늘)")
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()

    at = date.fromisoformat(args.at) if args.at else None
    db = SessionLocal()
    try:
        result = run_due_approvals(db, at=at, batch_size=args.batch_size)
    finally:
        db.close()

    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2, default=str))
    # 실패가 있으면 비정상 종료 — 스케줄러가 알림을 띄울 수 있게
    return 1 if (result.other_failed or result.validation_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
