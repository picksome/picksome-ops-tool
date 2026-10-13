"""NEXT ENGINE 주문 Shadow 대사 — **읽기 전용, 수동 실행 전용**.

무엇을 하나
---------
실제 주문을 읽어 우리 개념으로 해석해 보고, 기존 장부와 맞춰 본다.
**아무것도 쓰지 않는다** — `sales_order` 도, 재고도, 원가도.

```
NEXT ENGINE 주문  →  해석(계정·상품·옵션)  →  기존 장부와 대사  →  cutover 후보
```

왜 ingest 전에 이걸 하나
--------------------
기존 장부에 Excel 로 들어온 주문이 이미 있다. 겹치는 구간을 모른 채 API 를
켜면 매출이 두 번 잡히거나, 반대로 잘라낸 구간이 비어 버린다. 그 둘 다
정산이 끝난 뒤에야 드러난다.

⚠️ 개인정보를 출력하지 않는다
------------------------
주문 응답에는 구매자·수취인 정보가 있다. 정규화 단계에서 버려지므로 이
스크립트가 볼 수 있는 것은 이미 개인정보가 아니다. 그래도 주문번호까지
찍지 않는다 — 집계만 낸다.

⚠️ 일반 테스트에 넣지 않는다
------------------------
실제 외부 API 를 부른다.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description="NEXT ENGINE 주문 Shadow 대사 (읽기 전용)")
    ap.add_argument("--org", required=True, help="조직 UUID")
    ap.add_argument("--days-back", type=int, default=10,
                    help="오늘로부터 며칠 전까지 볼지 (10일 창으로 잘라 조회)")
    ap.add_argument("--window-end-offset", type=int, default=0,
                    help="끝 지점을 오늘에서 며칠 앞으로 당길지")
    ap.add_argument("--size", type=int, default=30)
    ap.add_argument("--timeout", type=float, default=30.0)
    args = ap.parse_args()

    from sqlalchemy import text

    from app.connectors.next_engine import NextEngineOrderClient
    from app.connectors.next_engine.client import split_windows
    from app.db.session import SessionLocal
    from app.services import next_engine_order_sync as nes
    from app.services.credential_resolver import resolve_for_account

    org_id = UUID(args.org)
    db = SessionLocal()
    try:
        account_id = db.execute(text("""
            SELECT a.id FROM external_account a
              JOIN external_system s ON s.id = a.external_system_id
             WHERE a.org_id = :o AND s.code = 'NEXT_ENGINE' AND a.deleted_at IS NULL
             ORDER BY a.created_at LIMIT 1
        """), {"o": str(org_id)}).scalar()
        if account_id is None:
            print("NEXT_ENGINE 외부 계정이 없습니다. credential_bootstrap 을 먼저 실행하세요.")
            return 2
        account_id = UUID(str(account_id))
        api_key = resolve_for_account(
            db, org_id=org_id, account_id=account_id).as_dict()["api_key"]

        end = datetime.now() - timedelta(days=args.window_end_offset)
        start = end - timedelta(days=args.days_back)
        windows = split_windows(start, end)

        client = NextEngineOrderClient(timeout=args.timeout)
        lines = []
        for w_start, w_end in windows:
            lines += client.fetch_orders(
                api_key=api_key, start=w_start, end=w_end, size=args.size)

        # 창을 여러 개 돌면 같은 라인을 다시 볼 수 있다. 정확 식별자로 접는다.
        unique = {ln.identity(): ln for ln in lines}
        lines = list(unique.values())

        resolved, summary = nes.resolve_lines(
            db, org_id=org_id, external_account_id=account_id, lines=lines,
            # 대사 단계에서는 매핑을 만들지 않는다. 무엇이 이어질 수 있는지만 본다.
            allow_product_bootstrap=False)
        shadow = nes.shadow_compare(
            db, org_id=org_id, lines=lines,
            account_by_mall=nes.account_lookup(
                db, org_id=org_id, external_account_id=account_id))
        cutover = nes.cutover_candidate(shadow)

        # 해석이 매핑을 만들지 않았는지 확인하고 되돌린다 — 이 스크립트는 읽기다.
        db.rollback()

        print(f"windows          = {len(windows)} (각 ≤10일)")
        print(f"range            = {start:%Y-%m-%d} ~ {end:%Y-%m-%d}")
        print("\n--- 해석 ---")
        print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
        print("\n--- Shadow 대사 ---")
        print(json.dumps(shadow.as_dict(), ensure_ascii=False, indent=2))
        proposals = nes.propose_product_mappings(db, org_id=org_id, lines=lines)
        print("\n--- 상품 연결 후보 (적용하지 않음) ---")
        print(json.dumps(proposals, ensure_ascii=False, indent=2, default=str))

        print("\n--- Cutover 후보 ---")
        print(json.dumps(cutover, ensure_ascii=False, indent=2))

        # 건수 대사: 해석 결과의 합이 fetched 와 같아야 한다.
        total = sum(summary.counts.values())
        print(f"\nreconciliation   = {total} / fetched {summary.fetched} "
              f"-> {'OK' if total == summary.fetched else 'MISMATCH'}")
        return 0 if total == summary.fetched else 1
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    sys.exit(main())
