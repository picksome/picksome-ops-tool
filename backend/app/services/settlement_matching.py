"""정산 레코드 ↔ 주문 매칭 **판정** (docs/47 §2).

이 파일이 하지 않는 것부터
------------------------
```
금액 근사치 매칭 (|정산액 - 판매액| < 1000)      ✗
날짜 근접 매칭 (정산일 ±N일 안의 주문)            ✗
상품명 문자열 유사도                              ✗
라인이 여럿일 때 "첫 번째 라인" 에 붙이기          ✗
금액 비례 배분                                    ✗
```

전부 의도적으로 없다. **틀린 매칭은 틀린 손익이 되고, 그 손익이 파트너
수익배분이 된다.** 그리고 잘못 붙은 매칭은 나중에 사람이 봐도 "원래 그런 줄"
알기 때문에 발견되지 않는다 — 매칭이 없는 것은 눈에 보이지만, 잘못된 매칭은
보이지 않는다.

exact key 가 없다는 사실
---------------------
실제 `sales_order` 378건으로 확인했다.

```
라인이 1개인 주문번호   362건   (marketplace, order_no) 로 유일하다
라인이 2개 이상          7건    어느 라인의 정산인지 알 방법이 없다
```

정산 레코드에는 `line_no` 도 상품 식별자도 없다. 그래서 다건 주문에서는
**고르지 않고 멈춘다**(`AMBIGUOUS`).

`AMBIGUOUS` 와 `UNMATCHED` 를 구분하는 것이 핵심이다
------------------------------------------------
```
AMBIGUOUS   후보는 있는데 고를 수 없다   사람이 고르면 끝나는 일
UNMATCHED   후보가 없다                 데이터가 없는 일
```

같은 화면에 같은 문구로 보이면 사용자는 둘 다 방치한다. M054 에서 겪은 것과
같은 결함이다 — 한 칸에 성격이 다른 사실을 담으면 게이트도 화면도 틀린다.

사람이 내린 결정을 판정이 덮지 않는다
--------------------------------
```
MATCHED_MANUAL   승인을 거쳐 사람이 연결했다    건드리지 않는다
EXCLUDED         사람이 대사 대상에서 뺐다      건드리지 않는다
confirmed_at     정산 건이 확정됐다            건드리지 않는다
MATCHED_EXACT    규칙이 붙였다                 다시 판정한다 (결정론적이라 같은 결과)
```

앞의 셋을 다시 판정하면 **재실행이 사람의 판단을 지우는 도구**가 된다.
docs/47 §6-3 의 보존 규칙과 같은 이유다.

주문번호가 없는 행은 자동으로 `EXCLUDED` 로 만들지 않는다
--------------------------------------------------
광고비 정산·보증금처럼 주문과 무관한 행은 주문번호가 없다. 그렇다고 판정이
`EXCLUDED` 를 찍으면, **"주문과 무관하다" 는 판단을 규칙이 대신 내리는 것**이다.
판매처가 주문 관련 행에서 주문번호를 빠뜨렸을 수도 있고, 그 경우 조용히
대사 대상에서 빠진다.

그래서 `UNMATCHED` 로 두고 요약에서 `no_order_no` 로 따로 센다 — 사람이
사유와 함께 `EXCLUDED` 를 찍는다(DB CHECK 가 사유를 강제한다).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import NotFoundError, ValidationError

# ── 매칭 상태 (M055 CHECK 와 같은 어휘) ──────────────────────────────
UNMATCHED = "UNMATCHED"
AMBIGUOUS = "AMBIGUOUS"
MATCHED_EXACT = "MATCHED_EXACT"
MATCHED_MANUAL = "MATCHED_MANUAL"
EXCLUDED = "EXCLUDED"

#: 판정이 **건드리지 않는** 상태. 사람이 내린 결정이다.
HUMAN_DECIDED = frozenset({MATCHED_MANUAL, EXCLUDED})

#: 판정이 자동으로 붙일 수 있는 상태. `MATCHED_MANUAL` 은 여기 없다 —
#: 승인 어댑터만 그 값을 쓴다.
AUTO_ASSIGNABLE = frozenset({UNMATCHED, AMBIGUOUS, MATCHED_EXACT})


@dataclass(frozen=True, slots=True)
class MatchDecision:
    """정산 레코드 1건에 대한 판정 결과."""

    record_id: UUID
    status: str
    sales_order_id: UUID | None
    candidate_count: int
    #: 주문번호가 없어 판정 자체가 불가능한 행. `EXCLUDED` 후보다.
    no_order_no: bool = False
    #: 사람이 이미 결정해서 판정을 건너뛴 경우.
    skipped: bool = False
    skip_reason: str | None = None


# 후보 조회. **규칙이 쓰는 축만 쓴다** — 여기에 조건을 하나 더 넣는 것은
# 곧 새로운 추측을 하나 더 하는 것이다.
#
# 판매처 비교는 대소문자를 무시한다. 두 컬럼은 M007 이후 자유 텍스트라
# 수집 경로마다 표기가 다를 수 있다(`Coupang` / `COUPANG`).
#
# 계정이 **양쪽 다 있고 서로 다르면** 후보에서 뺀다. 본점 주문에 2호점
# 정산금을 붙이면 스토어별 손익이 통째로 틀어진다. 한쪽이라도 NULL 이면
# 비교하지 않는다 — 지금 정산 레코드는 계정이 전부 NULL 이고, 비교를
# 강제하면 매칭이 전부 0건이 된다.
_CANDIDATES = """
    SELECT so.id
      FROM sales_order so
     WHERE so.org_id = :org
       AND so.deleted_at IS NULL
       AND UPPER(so.marketplace) = UPPER(:mkt)
       AND so.marketplace_order_no = :ono
       AND (so.integration_account_id IS NULL
            OR CAST(:account AS UUID) IS NULL
            OR so.integration_account_id = CAST(:account AS UUID))
     ORDER BY so.line_no, so.id
"""

# 판정에 필요한 컬럼. 한 곳에 두는 이유는 단건 조회와 일괄 판정이 **같은
# 행 모양**을 봐야 하기 때문이다 — 한쪽에만 컬럼이 빠지면 일괄 판정만
# 조용히 다른 결론을 낸다.
_RECORD_COLS = """
    id, marketplace, marketplace_order_no, integration_account_id,
    matching_status, matched_sales_order_id, confirmed_at,
    settled_at, settlement_period_end
"""

_RECORD = f"""
    SELECT {_RECORD_COLS}
      FROM marketplace_settlement_record
     WHERE id = :id AND org_id = :org AND deleted_at IS NULL
"""


def _record(db: Session, org_id: UUID, record_id: UUID) -> Any:
    row = db.execute(
        text(_RECORD), {"id": str(record_id), "org": str(org_id)}
    ).mappings().first()
    if row is None:
        # 조직 격리 — 다른 org 의 레코드도 404 다.
        raise NotFoundError("정산 레코드를 찾을 수 없습니다.")
    return row


def decide(db: Session, *, org_id: UUID, record_id: UUID) -> MatchDecision:
    """판정만 한다. **쓰지 않는다.**

    쓰기와 나눈 이유: 화면이 "이렇게 붙일 예정" 을 미리 보여줄 수 있어야 하고,
    테스트가 규칙 자체를 저장 없이 고정할 수 있어야 한다.
    """
    row = _record(db, org_id, record_id)
    return _decide_row(db, org_id, row)


def _decide_row(db: Session, org_id: UUID, row: Any) -> MatchDecision:
    record_id = row["id"]

    if row["confirmed_at"] is not None:
        return MatchDecision(
            record_id, row["matching_status"], row["matched_sales_order_id"], 0,
            skipped=True, skip_reason="이미 확정된 정산 건입니다.",
        )
    if row["matching_status"] in HUMAN_DECIDED:
        return MatchDecision(
            record_id, row["matching_status"], row["matched_sales_order_id"], 0,
            skipped=True, skip_reason="사람이 결정한 매칭입니다.",
        )

    order_no = (row["marketplace_order_no"] or "").strip()
    if not order_no:
        # 판정할 축이 없다. `EXCLUDED` 로 단정하지 않는다(모듈 docstring).
        return MatchDecision(record_id, UNMATCHED, None, 0, no_order_no=True)

    candidates = db.execute(text(_CANDIDATES), {
        "org": str(org_id),
        "mkt": row["marketplace"],
        "ono": order_no,
        "account": str(row["integration_account_id"])
                   if row["integration_account_id"] else None,
    }).scalars().all()
    return classify(record_id, candidates)


def classify(record_id: UUID, candidates: list[UUID] | tuple[UUID, ...]) -> MatchDecision:
    """후보 목록 → 판정. **규칙은 여기 한 곳에만 있다.**

    단건 경로와 일괄 경로가 각자 판정하면 언젠가 갈라진다 — 그때 둘 중
    어느 쪽이 맞는지 아무도 모른다. 후보를 어떻게 모으는지는 달라도
    (한 건씩 조회 vs 한 번에 조회) 결론은 이 함수가 낸다.
    """
    if not candidates:
        return MatchDecision(record_id, UNMATCHED, None, 0)
    if len(candidates) == 1:
        return MatchDecision(record_id, MATCHED_EXACT, candidates[0], 1)
    # 여기서 멈춘다. 첫 번째를 고르지 않는다.
    return MatchDecision(record_id, AMBIGUOUS, None, len(candidates))


def load_candidate_index(
    db: Session, *, org_id: UUID, marketplace: str, order_nos: list[str],
    integration_account_id: UUID | None,
) -> dict[str, list[UUID]]:
    """주문번호 → 후보 라인. **한 번의 조회로 전부 가져온다.**

    정산서 한 달치를 적재할 때 행마다 주문을 조회하면 수백~수천 번이 된다.
    조건은 `_CANDIDATES` 와 **같아야 한다** — 다르면 일괄 적재와 개별
    재판정이 다른 결과를 낸다.
    """
    if not order_nos:
        return {}
    rows = db.execute(text("""
        SELECT so.marketplace_order_no AS ono, so.id
          FROM sales_order so
         WHERE so.org_id = :org
           AND so.deleted_at IS NULL
           AND UPPER(so.marketplace) = UPPER(:mkt)
           AND so.marketplace_order_no = ANY(:onos)
           AND (so.integration_account_id IS NULL
                OR CAST(:account AS UUID) IS NULL
                OR so.integration_account_id = CAST(:account AS UUID))
         ORDER BY so.marketplace_order_no, so.line_no, so.id
    """), {
        "org": str(org_id), "mkt": marketplace,
        "onos": sorted(set(order_nos)),
        "account": str(integration_account_id) if integration_account_id else None,
    }).all()
    index: dict[str, list[UUID]] = {}
    for ono, sid in rows:
        index.setdefault(ono, []).append(sid)
    return index


def apply_decision(
    db: Session, *, org_id: UUID, decision: MatchDecision,
    actor_user_id: UUID | None,
) -> bool:
    """판정을 반영한다. 바뀐 것이 있으면 True. **커밋하지 않는다.**

    커밋하지 않는 이유: 일괄 판정은 수백 건을 한 트랜잭션으로 처리해야
    중간에 끊겨도 절반만 붙은 상태가 남지 않는다.
    """
    if decision.skipped:
        return False
    if decision.status not in AUTO_ASSIGNABLE:
        # 방어. `MATCHED_MANUAL` 은 승인 어댑터만 쓴다.
        raise ValueError(f"판정이 지정할 수 없는 상태입니다: {decision.status}")

    before = db.execute(text("""
        SELECT matching_status, matched_sales_order_id
          FROM marketplace_settlement_record
         WHERE id = :id AND org_id = :org AND deleted_at IS NULL
    """), {"id": str(decision.record_id), "org": str(org_id)}).mappings().first()
    if before is None:
        raise NotFoundError("정산 레코드를 찾을 수 없습니다.")

    if (before["matching_status"] == decision.status
            and before["matched_sales_order_id"] == decision.sales_order_id):
        # 같은 결과다. UPDATE 하면 `updated_at` 만 흔들리고, 그 흔들림이
        # "뭔가 바뀌었다" 로 읽힌다.
        return False

    db.execute(text("""
        UPDATE marketplace_settlement_record
           SET matching_status = :st,
               matched_sales_order_id = CAST(:soid AS UUID),
               -- 규칙이 붙인 것에는 `matched_by` 를 남기지 않는다. 사람이
               -- 붙인 것과 구분되어야 한다 — 규칙이면 `MATCHED_EXACT` 라는
               -- 상태 자체가 근거다.
               matched_at = CASE WHEN CAST(:soid AS UUID) IS NULL
                                 THEN NULL ELSE now() END,
               matched_by = NULL,
               match_approval_request_id = NULL,
               updated_at = now(),
               updated_by = CAST(:actor AS UUID)
         WHERE id = :id AND org_id = :org AND deleted_at IS NULL
    """), {
        "st": decision.status,
        "soid": str(decision.sales_order_id) if decision.sales_order_id else None,
        "id": str(decision.record_id), "org": str(org_id),
        "actor": str(actor_user_id) if actor_user_id else None,
    })

    record_audit(
        db, org_id=org_id,
        domain_table="marketplace_settlement_record",
        domain_row_id=decision.record_id,
        action="UPDATE", actor_user_id=actor_user_id,
        before={"matching_status": before["matching_status"],
                "matched_sales_order_id": before["matched_sales_order_id"]},
        after={"matching_status": decision.status,
               "matched_sales_order_id": decision.sales_order_id,
               "candidate_count": decision.candidate_count},
        reason="정산 매칭 자동 판정",
    )
    return True


def resolve_pending(
    db: Session, *, org_id: UUID, actor_user_id: UUID | None = None,
    marketplace: str | None = None, limit: int | None = None,
) -> dict[str, int]:
    """아직 사람이 결정하지 않은 레코드를 전부 다시 판정한다.

    `MATCHED_EXACT` 도 대상이다 — 규칙이 결정론적이라 같은 결과가 나오고,
    주문이 뒤늦게 삭제되면 붙어 있던 연결이 풀려야 한다.

    **커밋하지 않는다.** 호출자가 정한다.
    """
    where = ["org_id = :org", "deleted_at IS NULL",
             "confirmed_at IS NULL",
             "matching_status NOT IN ('MATCHED_MANUAL', 'EXCLUDED')"]
    params: dict[str, Any] = {"org": str(org_id)}
    if marketplace:
        where.append("UPPER(marketplace) = UPPER(:mkt)")
        params["mkt"] = marketplace

    sql = (f"SELECT {_RECORD_COLS} FROM marketplace_settlement_record"
           f" WHERE {' AND '.join(where)} ORDER BY created_at")
    if limit is not None:
        sql += " LIMIT :lim"
        params["lim"] = limit

    summary = {
        "scanned": 0, "changed": 0,
        MATCHED_EXACT.lower(): 0, AMBIGUOUS.lower(): 0, UNMATCHED.lower(): 0,
        "no_order_no": 0,
    }
    for row in db.execute(text(sql), params).mappings().all():
        summary["scanned"] += 1
        decision = _decide_row(db, org_id, row)
        if decision.skipped:
            continue
        summary[decision.status.lower()] += 1
        if decision.no_order_no:
            summary["no_order_no"] += 1
        if apply_decision(db, org_id=org_id, decision=decision,
                          actor_user_id=actor_user_id):
            summary["changed"] += 1
    return summary


def mark_excluded(
    db: Session, *, org_id: UUID, record_id: UUID, reason: str,
    actor_user_id: UUID | None,
) -> None:
    """대사 대상에서 뺀다. **승인을 거치지 않는다** (docs/47 §3).

    광고비 정산·보증금처럼 주문과 무관한 행이다. 금액에 영향이 없고, 되돌리기
    쉽고, 매일 나오는 작업이다 — 승인으로 만들면 사용자가 대사 자체를 안 한다.

    이미 붙어 있는 연결은 여기서 풀지 않는다. 연결을 푸는 것은 손익 귀속을
    바꾸는 일이고 그건 승인 대상이다(`SettlementMatchAdapter`). 여기서 같이
    처리하면 제외가 승인 우회 경로가 된다.

    **커밋하지 않는다.**
    """
    text_reason = (reason or "").strip()
    if not text_reason:
        # DB CHECK 도 막지만, 여기서 막아야 사용자에게 읽을 수 있는 문장이 간다.
        raise ValidationError("제외 사유를 입력하세요.", detail={"field": "reason"})

    row = _record(db, org_id, record_id)
    if row["matched_sales_order_id"] is not None:
        raise ValidationError(
            "주문에 연결된 정산 건입니다. 먼저 연결 해제 승인을 거치세요.",
            detail={"matching_status": row["matching_status"]},
        )
    if row["confirmed_at"] is not None:
        raise ValidationError("이미 확정된 정산 건입니다.")

    db.execute(text("""
        UPDATE marketplace_settlement_record
           SET matching_status = 'EXCLUDED',
               match_reason = :reason,
               updated_at = now(),
               updated_by = CAST(:actor AS UUID)
         WHERE id = :id AND org_id = :org AND deleted_at IS NULL
    """), {"reason": text_reason, "id": str(record_id), "org": str(org_id),
           "actor": str(actor_user_id) if actor_user_id else None})

    record_audit(
        db, org_id=org_id,
        domain_table="marketplace_settlement_record",
        domain_row_id=record_id, action="UPDATE", actor_user_id=actor_user_id,
        before={"matching_status": row["matching_status"]},
        after={"matching_status": EXCLUDED},
        reason=f"대사 대상 제외: {text_reason}",
    )


def clear_exclusion(
    db: Session, *, org_id: UUID, record_id: UUID, actor_user_id: UUID | None,
) -> None:
    """제외를 되돌려 다시 판정 대상으로 만든다. 승인 불요 — 제외와 대칭이다.

    되돌린 뒤 상태는 `UNMATCHED` 다. 여기서 바로 판정하지 않는다 —
    되돌리는 것과 판정하는 것은 다른 결정이고, 호출자가 `resolve_pending`
    을 부를지 정한다.
    """
    row = _record(db, org_id, record_id)
    if row["matching_status"] != EXCLUDED:
        raise ValidationError(
            "제외 상태가 아닙니다.", detail={"matching_status": row["matching_status"]})

    db.execute(text("""
        UPDATE marketplace_settlement_record
           SET matching_status = 'UNMATCHED',
               match_reason = NULL,
               updated_at = now(),
               updated_by = CAST(:actor AS UUID)
         WHERE id = :id AND org_id = :org AND deleted_at IS NULL
    """), {"id": str(record_id), "org": str(org_id),
           "actor": str(actor_user_id) if actor_user_id else None})

    record_audit(
        db, org_id=org_id,
        domain_table="marketplace_settlement_record",
        domain_row_id=record_id, action="UPDATE", actor_user_id=actor_user_id,
        before={"matching_status": EXCLUDED},
        after={"matching_status": UNMATCHED},
        reason="대사 대상 제외 해제",
    )


def counts_by_status(db: Session, *, org_id: UUID) -> dict[str, int]:
    """상태별 건수. 화면 배지와 마감 검증이 같은 값을 보게 한다."""
    rows = db.execute(text("""
        SELECT matching_status, COUNT(*) AS n
          FROM marketplace_settlement_record
         WHERE org_id = :org AND deleted_at IS NULL
         GROUP BY matching_status
    """), {"org": str(org_id)}).all()
    out = {s: 0 for s in
           (UNMATCHED, AMBIGUOUS, MATCHED_EXACT, MATCHED_MANUAL, EXCLUDED)}
    for status, n in rows:
        out[status] = int(n)
    return out


def candidate_orders(
    db: Session, *, org_id: UUID, record_id: UUID
) -> list[dict[str, Any]]:
    """`AMBIGUOUS` 를 사람이 고를 수 있게 후보를 돌려준다.

    **순위를 매기지 않는다.** 금액이 가까운 순으로 정렬하면 그건 화면이
    하는 추측 매칭이고, 사용자는 맨 위를 고른다.
    """
    row = _record(db, org_id, record_id)
    order_no = (row["marketplace_order_no"] or "").strip()
    if not order_no:
        return []
    return [dict(r) for r in db.execute(text("""
        SELECT so.id, so.business_key, so.line_no, so.sale_date::text AS sale_date,
               so.product_snapshot_name, so.quantity, so.sale_price,
               so.marketplace_order_no
          FROM sales_order so
         WHERE so.org_id = :org
           AND so.deleted_at IS NULL
           AND UPPER(so.marketplace) = UPPER(:mkt)
           AND so.marketplace_order_no = :ono
         ORDER BY so.line_no, so.id
    """), {"org": str(org_id), "mkt": row["marketplace"], "ono": order_no}
    ).mappings().all()]
