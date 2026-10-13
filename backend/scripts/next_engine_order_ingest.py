"""주문 동기화 **수동 실행** — 진단 · 장애 복구 · 백필.

자동과 같은 길을 쓴다
------------------
```
Manual (이 파일) ─┐
                  ├─► order_sync.sync_account() ─► ingest_orders() ─► sales_order
Worker            ─┘
```

수동만 다른 코드를 쓰면, 사람이 손으로 확인한 동작이 자동 실행에서 재현되지
않는다. 그 차이는 새벽에 드러난다.

수동을 없애지 않는 이유
-------------------
```
자동   정상 운영. 최근 며칠만 겹쳐 읽는다
수동   지금 당장 확인해야 할 때 · 자동이 죽어 있을 때 · 과거 백필
```

잠금은 자동과 공유한다
------------------
같은 계정에 대해 자동 실행이 돌고 있으면 이 스크립트는 `ALREADY_RUNNING` 으로
끝난다. 성공한 척하지 않는다.

사용법
-----
```bash
# 정책에 설정된 대로 (겹쳐 읽기)
python -m scripts.next_engine_order_ingest --org <UUID>

# 과거 백필. 벤더 한도(10일)에 맞춰 알아서 잘라 읽는다
python -m scripts.next_engine_order_ingest --org <UUID> --days-back 60
```
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description="주문 동기화 (수동)")
    ap.add_argument("--org", required=True)
    ap.add_argument("--account", default=None,
                    help="외부 계정 UUID. 없으면 정책에 설정된 전부.")
    ap.add_argument("--days-back", type=int, default=None,
                    help="백필용. 정책의 겹쳐 읽기 폭 대신 이만큼 거슬러 읽는다. "
                         "cutover 아래로는 내려가지 않는다.")
    args = ap.parse_args()

    from app.connectors.bootstrap import register_builtin
    from app.db.session import SessionLocal
    from app.services import automation as auto
    from app.services import order_sync as osync
    from app.services.automation_runners import automation_actor

    register_builtin()
    org_id = UUID(args.org)
    db = SessionLocal()
    try:
        policy = next(
            (p for p in auto.list_policies(db, org_id=org_id)
             if p["automation_type"] == osync.AUTOMATION_TYPE and p["id"]),
            None)
        if policy is None:
            print("ORDER_SYNC 자동화 정책이 없습니다. 설정 화면에서 먼저 만들어 주세요.")
            return 2

        configs = osync.read_config(policy)
        if not configs:
            print("겹쳐 읽기 일수와 대상 계정·시작일이 설정되지 않았습니다.")
            return 2

        if args.account:
            wanted = UUID(args.account)
            configs = {k: v for k, v in configs.items() if k == wanted}
            if not configs:
                print("그 계정은 주문 동기화 대상으로 설정돼 있지 않습니다.")
                return 2

        # 백필은 **폭만** 넓힌다. 시작일(cutover)은 그대로다 — 과거 보호는
        # 수동이라고 풀리지 않는다.
        if args.days_back:
            configs = {
                k: replace(v, overlap_days=args.days_back)
                for k, v in configs.items()
            }

        actor = automation_actor(db, org_id=org_id)
        failures = 0
        for account_id, config in configs.items():
            try:
                out = osync.sync_account(
                    db, org_id=org_id, external_account_id=account_id,
                    config=config, actor_user_id=actor, trigger="MANUAL")
            except osync.OrderSyncAlreadyRunningError:
                print(json.dumps(
                    {"external_account_id": str(account_id),
                     "status": "SKIPPED", "reason": osync.ALREADY_RUNNING},
                    ensure_ascii=False, indent=2))
                failures += 1
                continue
            print(json.dumps(out.as_dict(), ensure_ascii=False, indent=2))
            if out.status != osync.SUCCESS:
                failures += 1

        return 1 if failures else 0
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
