"""정산 매칭 판정 · 수동 매칭 승인 · 정산 조정 (M055 · docs/47).

이 파일이 고정하는 것
------------------
1. 판정 규칙은 exact 와 ambiguous 뿐이다 — **추측 매칭이 없다는 사실을 테스트가 막는다**
2. `AMBIGUOUS` 와 `UNMATCHED` 가 갈린다 (M054 에서 겪은 "한 칸에 두 사실" 재발 방지)
3. 재판정이 사람의 결정(`MATCHED_MANUAL`·`EXCLUDED`·확정)을 덮지 않는다
4. 수동 매칭은 승인을 거치고, 제외는 거치지 않는다
5. DB CHECK 가 상태와 연결의 어긋남을 막는다
6. 마감 검증이 둘을 다른 문구로 보고하고, **아무것도 막지 않는다**

가장 중요한 것은 1번이다. 추측 매칭은 "구현하지 않았다" 로 남겨 두면 다음
사람이 편의를 위해 넣는다. 넣으면 테스트가 깨지게 만든다.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ValidationError
from app.services import close_checks
from app.services import settlement_matching as sm
from app.services.approval_settlement import SettlementMatchAdapter
from app.tests.fixtures.factories import (
    make_app_user,
    make_marketplace_settlement_record,
    make_organization,
    make_product,
    make_sales_order,
)

YM = "2026-04"
PERIOD = date(2026, 4, 10)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


def _order(db: Session, org: uuid.UUID, order_no: str, *,
           price: int = 30000, marketplace: str = "COUPANG") -> uuid.UUID:
    product = make_product(db, org_id=org, cost_amount=10000)
    return make_sales_order(
        db, org_id=org, product_id=product, sale_date=PERIOD,
        sale_price=price, marketplace=marketplace, marketplace_order_no=order_no,
    )


def _record(db: Session, org: uuid.UUID, order_no: str | None, **kw) -> uuid.UUID:
    return make_marketplace_settlement_record(
        db, org_id=org, gross_revenue=kw.pop("gross_revenue", 30000),
        marketplace=kw.pop("marketplace", "COUPANG"),
        settlement_period_start=PERIOD, settlement_period_end=PERIOD,
        marketplace_order_no=order_no, **kw,
    )


def _status(db: Session, record_id: uuid.UUID) -> tuple[str, uuid.UUID | None]:
    row = db.execute(text("""
        SELECT matching_status, matched_sales_order_id
          FROM marketplace_settlement_record WHERE id = :i
    """), {"i": str(record_id)}).first()
    return row[0], row[1]


# ──────────────────────────────────────────────────────────────────────
# 1. 판정 규칙
# ──────────────────────────────────────────────────────────────────────

def test_single_line_order_matches_exactly(db: Session, org, actor):
    """라인이 1개면 (판매처, 주문번호)로 유일하게 붙는다. 실데이터의 362/369 경우."""
    order = _order(db, org, "ORD-EXACT")
    record = _record(db, org, "ORD-EXACT")

    decision = sm.decide(db, org_id=org, record_id=record)
    assert decision.status == sm.MATCHED_EXACT
    assert decision.sales_order_id == order
    assert decision.candidate_count == 1

    assert sm.apply_decision(db, org_id=org, decision=decision, actor_user_id=actor)
    assert _status(db, record) == (sm.MATCHED_EXACT, order)


def test_multi_line_order_stops_at_ambiguous(db: Session, org, actor):
    """라인이 여럿이면 **고르지 않는다.**

    실데이터에 7건 있는 경우다. 정산 레코드에는 `line_no` 도 상품 식별자도
    없어서 어느 라인의 정산금인지 알 방법이 없다.
    """
    first = _order(db, org, "ORD-MULTI", price=30000)
    second = _order(db, org, "ORD-MULTI", price=20000)
    record = _record(db, org, "ORD-MULTI")

    decision = sm.decide(db, org_id=org, record_id=record)
    assert decision.status == sm.AMBIGUOUS
    assert decision.candidate_count == 2
    # **첫 번째 라인에 붙이지 않는다.**
    assert decision.sales_order_id is None

    sm.apply_decision(db, org_id=org, decision=decision, actor_user_id=actor)
    status, matched = _status(db, record)
    assert status == sm.AMBIGUOUS
    assert matched is None
    # 후보는 사람이 고를 수 있게 돌려준다 — 순위는 매기지 않는다.
    candidates = sm.candidate_orders(db, org_id=org, record_id=record)
    assert {c["id"] for c in candidates} == {first, second}


def test_no_candidate_is_unmatched_not_ambiguous(db: Session, org, actor):
    """후보가 없는 것과 고를 수 없는 것은 다른 사실이다."""
    record = _record(db, org, "ORD-NOBODY")
    decision = sm.decide(db, org_id=org, record_id=record)
    assert decision.status == sm.UNMATCHED
    assert decision.candidate_count == 0
    assert not decision.no_order_no


def test_missing_order_no_is_not_auto_excluded(db: Session, org, actor):
    """주문번호가 없어도 **자동으로 EXCLUDED 를 찍지 않는다.**

    "주문과 무관하다" 는 판단을 규칙이 대신 내리면, 판매처가 주문 관련 행에서
    주문번호를 빠뜨렸을 때 그 행이 조용히 대사 대상에서 빠진다.
    """
    record = _record(db, org, None)
    decision = sm.decide(db, org_id=org, record_id=record)
    assert decision.status == sm.UNMATCHED
    assert decision.no_order_no is True

    sm.apply_decision(db, org_id=org, decision=decision, actor_user_id=actor)
    assert _status(db, record)[0] == sm.UNMATCHED


def test_no_guessing_by_amount_or_date(db: Session, org, actor):
    """**추측 매칭이 없다는 것**을 고정한다.

    금액이 정확히 같고 날짜도 같지만 주문번호가 다르다. 어떤 근사 규칙이
    들어와도 여기서 깨진다.
    """
    _order(db, org, "ORD-A", price=30000)
    record = _record(db, org, "ORD-B", gross_revenue=30000)

    decision = sm.decide(db, org_id=org, record_id=record)
    assert decision.status == sm.UNMATCHED
    assert decision.sales_order_id is None


def test_different_marketplace_is_not_a_candidate(db: Session, org, actor):
    """주문번호가 같아도 판매처가 다르면 후보가 아니다."""
    _order(db, org, "ORD-SAME", marketplace="SMARTSTORE")
    record = _record(db, org, "ORD-SAME", marketplace="COUPANG")
    assert sm.decide(db, org_id=org, record_id=record).status == sm.UNMATCHED


def test_marketplace_comparison_ignores_case(db: Session, org, actor):
    """수집 경로마다 표기가 달라도(`Coupang`/`COUPANG`) 같은 판매처로 본다."""
    order = _order(db, org, "ORD-CASE", marketplace="COUPANG")
    record = _record(db, org, "ORD-CASE", marketplace="coupang")
    decision = sm.decide(db, org_id=org, record_id=record)
    assert decision.status == sm.MATCHED_EXACT
    assert decision.sales_order_id == order


def test_other_org_record_is_not_found(db: Session, org, actor):
    """조직 격리 — 다른 org 의 레코드는 404 다."""
    other = make_organization(db)
    record = _record(db, other, "ORD-OTHER")
    with pytest.raises(NotFoundError):
        sm.decide(db, org_id=org, record_id=record)


# ──────────────────────────────────────────────────────────────────────
# 2. 재판정이 사람의 결정을 덮지 않는다
# ──────────────────────────────────────────────────────────────────────

def test_resolve_pending_preserves_manual_match(db: Session, org, actor):
    """사람이 붙인 연결을 재실행이 다시 판정하지 않는다.

    규칙대로면 다른 주문에 붙을 상황을 만들어 둔다 — 재판정이 돌면 연결이
    바뀌므로 보존 여부가 실제로 검증된다.
    """
    rule_target = _order(db, org, "ORD-KEEP")
    manual_target = _order(db, org, "ORD-ELSEWHERE")
    record = _record(db, org, "ORD-KEEP")

    db.execute(text("""
        UPDATE marketplace_settlement_record
           SET matching_status = 'MATCHED_MANUAL',
               matched_sales_order_id = :so, match_reason = '판매처 확인함'
         WHERE id = :i
    """), {"so": str(manual_target), "i": str(record)})

    summary = sm.resolve_pending(db, org_id=org, actor_user_id=actor)
    assert summary["scanned"] == 0  # 조회 단계에서 이미 제외된다

    status, matched = _status(db, record)
    assert status == sm.MATCHED_MANUAL
    assert matched == manual_target
    assert matched != rule_target


def test_resolve_pending_preserves_excluded(db: Session, org, actor):
    _order(db, org, "ORD-AD")
    record = _record(db, org, "ORD-AD")
    sm.mark_excluded(db, org_id=org, record_id=record,
                     reason="광고비 정산행", actor_user_id=actor)

    sm.resolve_pending(db, org_id=org, actor_user_id=actor)
    assert _status(db, record) == (sm.EXCLUDED, None)


def test_resolve_pending_preserves_confirmed_record(db: Session, org, actor):
    """확정한 정산 건은 재판정 대상이 아니다."""
    _order(db, org, "ORD-CONF")
    record = _record(db, org, "ORD-CONF")
    db.execute(text("""
        UPDATE marketplace_settlement_record
           SET confirmed_at = now(), matching_status = 'MATCHED_EXACT',
               matched_sales_order_id = (
                 SELECT id FROM sales_order
                  WHERE org_id = :o AND marketplace_order_no = 'ORD-CONF')
         WHERE id = :i
    """), {"o": str(org), "i": str(record)})

    summary = sm.resolve_pending(db, org_id=org, actor_user_id=actor)
    assert summary["scanned"] == 0


def test_resolve_pending_is_idempotent(db: Session, org, actor):
    """두 번 돌려야 검증이다. 2회차에 바뀌는 것이 없어야 한다."""
    _order(db, org, "ORD-1")
    _order(db, org, "ORD-2")
    _order(db, org, "ORD-DUP")
    _order(db, org, "ORD-DUP")
    for no in ("ORD-1", "ORD-2", "ORD-DUP", "ORD-NONE", None):
        _record(db, org, no)

    first = sm.resolve_pending(db, org_id=org, actor_user_id=actor)
    second = sm.resolve_pending(db, org_id=org, actor_user_id=actor)

    assert first["scanned"] == second["scanned"] == 5
    assert first["matched_exact"] == 2
    assert first["ambiguous"] == 1
    assert first["unmatched"] == 2
    assert first["no_order_no"] == 1
    assert first["changed"] == 3       # exact 2 + ambiguous 1
    assert second["changed"] == 0      # **2회차는 아무것도 안 바꾼다**


def test_resolve_pending_unlinks_when_order_disappears(db: Session, org, actor):
    """붙어 있던 주문이 사라지면 연결이 풀린다.

    `MATCHED_EXACT` 를 재판정 대상에 두는 이유다 — 규칙이 붙인 것은 규칙이
    거둬야 하고, 그대로 두면 없는 주문을 가리키는 연결이 남는다.
    """
    order = _order(db, org, "ORD-GONE")
    record = _record(db, org, "ORD-GONE")
    sm.resolve_pending(db, org_id=org, actor_user_id=actor)
    assert _status(db, record) == (sm.MATCHED_EXACT, order)

    db.execute(text("UPDATE sales_order SET deleted_at = now() WHERE id = :i"),
               {"i": str(order)})
    sm.resolve_pending(db, org_id=org, actor_user_id=actor)
    assert _status(db, record) == (sm.UNMATCHED, None)


# ──────────────────────────────────────────────────────────────────────
# 3. 제외 (승인 불요)
# ──────────────────────────────────────────────────────────────────────

def test_exclude_requires_reason(db: Session, org, actor):
    record = _record(db, org, None)
    with pytest.raises(ValidationError):
        sm.mark_excluded(db, org_id=org, record_id=record,
                         reason="   ", actor_user_id=actor)


def test_exclude_refuses_when_already_linked(db: Session, org, actor):
    """연결을 푸는 것은 승인 대상이다. 제외가 그 우회로가 되면 안 된다."""
    _order(db, org, "ORD-LINKED")
    record = _record(db, org, "ORD-LINKED")
    sm.resolve_pending(db, org_id=org, actor_user_id=actor)

    with pytest.raises(ValidationError):
        sm.mark_excluded(db, org_id=org, record_id=record,
                         reason="광고비다", actor_user_id=actor)


def test_exclusion_round_trip(db: Session, org, actor):
    record = _record(db, org, None)
    sm.mark_excluded(db, org_id=org, record_id=record,
                     reason="보증금 정산행", actor_user_id=actor)
    assert _status(db, record)[0] == sm.EXCLUDED

    sm.clear_exclusion(db, org_id=org, record_id=record, actor_user_id=actor)
    status, _ = _status(db, record)
    assert status == sm.UNMATCHED
    reason = db.execute(
        text("SELECT match_reason FROM marketplace_settlement_record WHERE id = :i"),
        {"i": str(record)}).scalar()
    assert reason is None


# ──────────────────────────────────────────────────────────────────────
# 4. DB CHECK — 상태와 연결이 어긋나지 못한다
# ──────────────────────────────────────────────────────────────────────

def test_matched_status_requires_order(db: Session, org):
    record = _record(db, org, "ORD-CK1")
    with pytest.raises(IntegrityError):
        db.execute(text("""
            UPDATE marketplace_settlement_record
               SET matching_status = 'MATCHED_EXACT'
             WHERE id = :i
        """), {"i": str(record)})
    db.rollback()


def test_unmatched_status_cannot_hold_order(db: Session, org):
    order = _order(db, org, "ORD-CK2")
    record = _record(db, org, "ORD-CK2")
    with pytest.raises(IntegrityError):
        db.execute(text("""
            UPDATE marketplace_settlement_record
               SET matching_status = 'UNMATCHED', matched_sales_order_id = :so
             WHERE id = :i
        """), {"so": str(order), "i": str(record)})
    db.rollback()


def test_manual_match_requires_reason_at_db_level(db: Session, org):
    order = _order(db, org, "ORD-CK3")
    record = _record(db, org, "ORD-CK3")
    with pytest.raises(IntegrityError):
        db.execute(text("""
            UPDATE marketplace_settlement_record
               SET matching_status = 'MATCHED_MANUAL', matched_sales_order_id = :so
             WHERE id = :i
        """), {"so": str(order), "i": str(record)})
    db.rollback()


def test_confirm_requires_resolution(db: Session, org):
    """대사가 안 끝난 건은 확정할 수 없다."""
    record = _record(db, org, "ORD-CK4")
    with pytest.raises(IntegrityError):
        db.execute(text("""
            UPDATE marketplace_settlement_record SET confirmed_at = now()
             WHERE id = :i
        """), {"i": str(record)})
    db.rollback()


def test_import_identity_is_unique_per_org(db: Session, org):
    """같은 원본 행을 두 번 넣을 수 없다. 기간이 아니라 **행 자체**가 식별자다."""
    _record(db, org, "ORD-IDEM", import_idempotency_key="NE-ROW-1")
    with pytest.raises(IntegrityError):
        _record(db, org, "ORD-IDEM-OTHER", import_idempotency_key="NE-ROW-1")
    db.rollback()


def test_null_order_no_rows_no_longer_collide(db: Session, org):
    """기존 UNIQUE 키의 결함 — 주문번호가 NULL 인 행 둘이 같은 기간에 들어간다.

    광고비 정산과 보증금 정산이 같은 주에 있으면 예전 키로는 두 번째가
    UNIQUE 위반이었다.
    """
    _record(db, org, None, import_idempotency_key="AD-FEE")
    _record(db, org, None, import_idempotency_key="DEPOSIT")
    n = db.execute(text("""
        SELECT COUNT(*) FROM marketplace_settlement_record
         WHERE org_id = :o AND marketplace_order_no IS NULL
    """), {"o": str(org)}).scalar()
    assert n == 2


# ──────────────────────────────────────────────────────────────────────
# 5. 수동 매칭 승인 어댑터
# ──────────────────────────────────────────────────────────────────────

def test_adapter_is_registered():
    from app.services import approval_targets

    assert "settlement_record" in approval_targets.supported_target_types()
    assert approval_targets.supported_request_types("settlement_record") == [
        "SETTLEMENT_MANUAL_MATCH"]


def test_adapter_rejects_status_field():
    """payload 가 `matching_status` 를 직접 지정할 수 없다.

    허용하면 `MATCHED_EXACT` 를 꽂아 규칙이 찾은 것처럼 위장할 수 있고,
    그러면 재판정이 그 행을 보호하지 않는다.
    """
    adapter = SettlementMatchAdapter()
    with pytest.raises(ValidationError):
        adapter.validate_proposed_changes(
            {"matching_status": "MATCHED_EXACT", "match_reason": "위장 시도"},
            request_type="SETTLEMENT_MANUAL_MATCH")


def test_adapter_requires_explicit_unlink(db: Session, org):
    """해제는 명시적으로 null 이어야 한다. 키를 빠뜨린 것과 구분되어야 한다."""
    adapter = SettlementMatchAdapter()
    with pytest.raises(ValidationError):
        adapter.validate_proposed_changes(
            {"match_reason": "사유만 고치려 했다"},
            request_type="SETTLEMENT_MANUAL_MATCH")


def test_adapter_requires_reason(db: Session, org):
    adapter = SettlementMatchAdapter()
    with pytest.raises(ValidationError):
        adapter.validate_proposed_changes(
            {"matched_sales_order_id": str(uuid.uuid4()), "match_reason": "짧"},
            request_type="SETTLEMENT_MANUAL_MATCH")


def test_adapter_applies_manual_match(db: Session, org, actor):
    """다건 주문에서 사람이 라인을 고른다 — 이 어댑터의 주 용도다."""
    first = _order(db, org, "ORD-PICK", price=30000)
    _order(db, org, "ORD-PICK", price=20000)
    record = _record(db, org, "ORD-PICK")
    sm.resolve_pending(db, org_id=org, actor_user_id=actor)
    assert _status(db, record)[0] == sm.AMBIGUOUS

    adapter = SettlementMatchAdapter()
    request_id = uuid.uuid4()
    adapter.apply_changes(
        db, org_id=org, target_id=record,
        proposed={"matched_sales_order_id": str(first),
                  "match_reason": "판매처 상세내역에서 1번 라인 확인"},
        actor_user_id=actor, request_type="SETTLEMENT_MANUAL_MATCH",
        approval_request_id=request_id,
    )

    row = db.execute(text("""
        SELECT matching_status, matched_sales_order_id, match_reason,
               matched_by, match_approval_request_id
          FROM marketplace_settlement_record WHERE id = :i
    """), {"i": str(record)}).mappings().first()
    assert row["matching_status"] == sm.MATCHED_MANUAL
    assert row["matched_sales_order_id"] == first
    assert row["matched_by"] == actor
    # **어느 승인이 허가했는지 행에서 바로 답한다.**
    assert row["match_approval_request_id"] == request_id

    # 그리고 재판정이 이것을 덮지 않는다.
    sm.resolve_pending(db, org_id=org, actor_user_id=actor)
    assert _status(db, record) == (sm.MATCHED_MANUAL, first)


def test_adapter_refuses_cross_marketplace_link(db: Session, org, actor):
    other_market = _order(db, org, "ORD-NAVER", marketplace="SMARTSTORE")
    record = _record(db, org, "ORD-CX", marketplace="COUPANG")
    adapter = SettlementMatchAdapter()
    with pytest.raises(ValidationError):
        adapter.apply_changes(
            db, org_id=org, target_id=record,
            proposed={"matched_sales_order_id": str(other_market),
                      "match_reason": "판매처가 다른 주문"},
            actor_user_id=actor, request_type="SETTLEMENT_MANUAL_MATCH")


def test_adapter_refuses_other_org_order(db: Session, org, actor):
    other_org = make_organization(db)
    foreign = _order(db, other_org, "ORD-FOREIGN")
    record = _record(db, org, "ORD-FOREIGN")
    adapter = SettlementMatchAdapter()
    with pytest.raises(NotFoundError):
        adapter.apply_changes(
            db, org_id=org, target_id=record,
            proposed={"matched_sales_order_id": str(foreign),
                      "match_reason": "다른 조직 주문"},
            actor_user_id=actor, request_type="SETTLEMENT_MANUAL_MATCH")


def test_adapter_refuses_confirmed_record(db: Session, org, actor):
    order = _order(db, org, "ORD-DONE")
    record = _record(db, org, "ORD-DONE")
    sm.resolve_pending(db, org_id=org, actor_user_id=actor)
    db.execute(text("""
        UPDATE marketplace_settlement_record SET confirmed_at = now() WHERE id = :i
    """), {"i": str(record)})

    adapter = SettlementMatchAdapter()
    with pytest.raises(ValidationError):
        adapter.validate_target(db, org_id=org, target_id=record)
    with pytest.raises(ValidationError):
        adapter.apply_changes(
            db, org_id=org, target_id=record,
            proposed={"matched_sales_order_id": str(order),
                      "match_reason": "확정 후 재연결 시도"},
            actor_user_id=actor, request_type="SETTLEMENT_MANUAL_MATCH")


def test_adapter_effective_date_uses_settlement_period_end(db: Session, org):
    """기간이 월 경계를 넘을 때(7/29~8/4) 귀속은 **끝**이다.

    시작일로 잡으면 8월에 확정되는 정산금이 7월 잠금에 걸린다.
    """
    record = make_marketplace_settlement_record(
        db, org_id=org, gross_revenue=10000,
        settlement_period_start=date(2026, 7, 29),
        settlement_period_end=date(2026, 8, 4),
        marketplace_order_no="ORD-CROSS",
    )
    adapter = SettlementMatchAdapter()
    assert adapter.get_effective_date(
        db, org_id=org, target_id=record, proposed={}, effective_from=None,
    ) == date(2026, 8, 4)


def test_adapter_effective_date_prefers_settled_at(db: Session, org):
    record = make_marketplace_settlement_record(
        db, org_id=org, gross_revenue=10000,
        settlement_period_start=date(2026, 7, 29),
        settlement_period_end=date(2026, 8, 4),
        marketplace_order_no="ORD-SETTLED",
    )
    db.execute(text("""
        UPDATE marketplace_settlement_record SET settled_at = DATE '2026-08-10'
         WHERE id = :i
    """), {"i": str(record)})
    adapter = SettlementMatchAdapter()
    assert adapter.get_effective_date(
        db, org_id=org, target_id=record, proposed={}, effective_from=None,
    ) == date(2026, 8, 10)


# ──────────────────────────────────────────────────────────────────────
# 6. 마감 검증
# ──────────────────────────────────────────────────────────────────────

def _codes(db: Session, org: uuid.UUID) -> dict[str, close_checks.ValidationItem]:
    items = close_checks.run_checks(db, org_id=org, year_month=YM)
    return {i.code: i for i in items}


def test_close_reports_ambiguous_separately(db: Session, org, actor):
    """`AMBIGUOUS` 와 `UNMATCHED` 가 **다른 항목**으로 나온다."""
    _order(db, org, "ORD-M1")
    _order(db, org, "ORD-M1")
    _record(db, org, "ORD-M1")
    _record(db, org, "ORD-M2")
    sm.resolve_pending(db, org_id=org, actor_user_id=actor)

    codes = _codes(db, org)
    assert codes["AMBIGUOUS_SETTLEMENT"].count == 1
    assert codes["UNMATCHED_SETTLEMENT"].count == 1
    # 안내 문구도 달라야 한다 — 같으면 나눈 의미가 없다.
    assert codes["AMBIGUOUS_SETTLEMENT"].hint != codes["UNMATCHED_SETTLEMENT"].hint


def test_close_never_blocks_on_settlement(db: Session, org, actor):
    """정산 대사는 **마감을 막지 않는다.**

    판매처가 정산서를 늦게 주는 일이 흔하고, 차단으로 만들면 마감이 판매처
    일정에 인질로 잡힌다(docs/47 §4).
    """
    _order(db, org, "ORD-B1")
    _order(db, org, "ORD-B1")
    _record(db, org, "ORD-B1")
    _record(db, org, "ORD-B2")
    _record(db, org, None)
    sm.resolve_pending(db, org_id=org, actor_user_id=actor)

    settlement_items = [
        i for i in close_checks.run_checks(db, org_id=org, year_month=YM)
        if i.domain == "SETTLEMENT"
    ]
    assert settlement_items, "정산 항목이 하나도 안 나오면 검사 자체가 안 돈 것이다"
    assert [i.code for i in settlement_items if i.blocking] == []


def test_excluded_records_drop_out_of_unmatched_count(db: Session, org, actor):
    """제외한 행은 "연결 안 됨" 으로 세지 않는다.

    광고비 정산행을 계속 세면 영원히 줄지 않는 경고가 쌓이고, 그때부터
    사람은 이 항목을 안 본다.
    """
    ad_row = _record(db, org, None)
    _record(db, org, "ORD-STILL-MISSING")
    sm.resolve_pending(db, org_id=org, actor_user_id=actor)
    assert _codes(db, org)["UNMATCHED_SETTLEMENT"].count == 2

    sm.mark_excluded(db, org_id=org, record_id=ad_row,
                     reason="광고비 정산행", actor_user_id=actor)
    assert _codes(db, org)["UNMATCHED_SETTLEMENT"].count == 1


def test_settlement_adjustment_is_surfaced_but_does_not_block(db: Session, org, actor):
    """정산 정정이 확정 숫자에 안 들어 있다는 것을 **보여 주되 막지 않는다.**

    `sales_adjustment`·`expense_adjustment` 와 같은 상태다(docs/80 §12-1).
    """
    _order(db, org, "ORD-ADJ")
    record = _record(db, org, "ORD-ADJ")
    sm.resolve_pending(db, org_id=org, actor_user_id=actor)

    db.execute(text("""
        INSERT INTO settlement_adjustment
          (org_id, original_record_id, adjustment_type, adjustment_reason,
           original_period, adjustment_period,
           return_cost_delta, net_settlement_delta)
        VALUES (:o, :r, 'LATE_ENTRY', '반품 차감 누락', :ym, :ym, 50000, -50000)
    """), {"o": str(org), "r": str(record), "ym": YM})

    item = _codes(db, org)["SETTLEMENT_ADJUSTMENT_NOT_IN_SNAPSHOT"]
    assert item.grade == close_checks.WARNING
    assert not item.blocking
    assert "-50,000" in item.message


def test_settlement_adjustment_rejects_all_zero(db: Session, org):
    """전부 0 인 조정은 조정이 아니다."""
    record = _record(db, org, "ORD-ZERO")
    with pytest.raises(IntegrityError):
        db.execute(text("""
            INSERT INTO settlement_adjustment
              (org_id, original_record_id, adjustment_type, adjustment_reason,
               adjustment_period, net_settlement_delta)
            VALUES (:o, :r, 'CORRECTION', '아무것도 안 바뀜', :ym, 0)
        """), {"o": str(org), "r": str(record), "ym": YM})
    db.rollback()


def test_settlement_adjustment_requires_reason(db: Session, org):
    record = _record(db, org, "ORD-NOREASON")
    with pytest.raises(IntegrityError):
        db.execute(text("""
            INSERT INTO settlement_adjustment
              (org_id, original_record_id, adjustment_type, adjustment_reason,
               adjustment_period, net_settlement_delta)
            VALUES (:o, :r, 'CORRECTION', '   ', :ym, -1000)
        """), {"o": str(org), "r": str(record), "ym": YM})
    db.rollback()
