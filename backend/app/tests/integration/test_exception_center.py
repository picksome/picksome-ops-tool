"""예외 센터.

이 파일이 고정하는 것
------------------
1. 미연결을 **0건이라고 하지 않는다**
2. 재주문 기준이 없으면 계산하지 않고 `NOT_CONFIGURED`
3. 정상 주문은 목록에 오르지 않는다
4. 한 구획이 깨져도 나머지가 살아남는다
5. 일일 리포트와 **숫자가 같다**
6. 심각도는 기존 체계만 쓴다 — 새 점수 없음

1 과 5 가 핵심이다
----------------
"클레임 0건" 은 확인했다는 뜻으로 읽힌다. 실제로는 볼 방법이 없다.
그리고 두 화면이 다른 숫자를 말하면 사용자는 둘 다 안 믿는다.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import automation as auto
from app.services import daily_report, exception_center, inventory
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org)


def _categories(result: dict) -> dict[str, dict]:
    return {c["category"]: c for c in result["categories"]}


def test_claims_are_not_connected_not_zero(db: Session, org):
    """클레임 수집이 한 번도 안 돌았으면 `NOT_CONNECTED` 다. 0건이 아니다."""
    result = exception_center.build(db, org_id=org)
    order = _categories(result)["ORDER"]
    assert order["status"] == "NOT_CONNECTED"
    assert "알 수 없" in (order["note"] or "")
    # 화면이 "이상 없음" 으로 그리지 않도록 상태가 OK 와 달라야 한다.
    assert order["status"] != "OK"


def _claim_sync_ran(db: Session, org: uuid.UUID, actor: uuid.UUID) -> None:
    """`CLAIM_SYNC` 자동화가 실제로 한 번 돌았다는 기록.

    정책만 켜 두고 한 번도 안 돈 상태와 돌아서 0건인 상태는 다르다.
    """
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="CLAIM_SYNC",
        enabled=True, mode="AUTO_EXECUTE")
    run_id = db.execute(text("""
        INSERT INTO automation_run (org_id, policy_id, trigger, mode, status)
        VALUES (:o, :p, 'SCHEDULED', 'AUTO_EXECUTE', 'SUCCESS')
        RETURNING id
    """), {"o": str(org), "p": str(policy["id"])}).scalar()
    db.execute(text("""
        INSERT INTO automation_action (org_id, run_id, action_type, outcome, reason)
        VALUES (:o, :r, 'CLAIM_SYNC', 'EXECUTED', '테스트')
    """), {"o": str(org), "r": str(run_id)})
    db.commit()


def test_once_claims_are_collected_the_screen_stops_saying_unconnected(
    db: Session, org, actor,
):
    """수집이 붙었는데 계속 "미연결" 이라고 하면 그것도 거짓이다.

    반대 방향의 같은 실수다 — 운영자가 취소·반품이 안 들어오는 줄 안다.
    """
    _claim_sync_ran(db, org, actor)

    order = _categories(exception_center.build(db, org_id=org))["ORDER"]

    assert order["status"] == "OK"
    assert "0건" in (order["note"] or "")


def test_claims_that_could_not_reduce_revenue_are_surfaced(db: Session, org, actor):
    """매출이 줄어야 하는데 안 줄었다. 조용하면 그 달이 부푼 채로 마감된다."""
    _claim_sync_ran(db, org, actor)
    _seed_claim(db, org, financial_effect="UNRESOLVED")

    result = exception_center.build(db, org_id=org)
    order = _categories(result)["ORDER"]

    assert order["status"] == "ATTENTION"
    assert any(i["kind"] == "CLAIM_FINANCIAL_UNRESOLVED"
               for i in result["items"] if i["category"] == "ORDER")


def test_a_pre_shipment_cancel_does_not_make_the_order_section_shout(
    db: Session, org, actor,
):
    """원 주문이 장부에 없는 취소는 되돌릴 매출이 없다 — 장부는 정확하다.

    여기서 경고를 띄우면 정상 상태가 매일 빨갛게 보이고, 그러면 진짜
    경고도 같이 무시된다. 개별 확인은 연동 묶음에 남아 있다.
    """
    _claim_sync_ran(db, org, actor)
    _seed_claim(db, org, financial_effect="NO_LEDGER_IMPACT")

    result = exception_center.build(db, org_id=org)
    order = _categories(result)["ORDER"]

    assert order["status"] == "OK"
    assert not [i for i in result["items"] if i["category"] == "ORDER"]


def _seed_claim(db: Session, org: uuid.UUID, *, financial_effect: str) -> None:
    sys_id = db.execute(text("""
        INSERT INTO external_system (code, name, system_type, supports_api_key)
        VALUES ('NEXT_ENGINE', 'NEXT ENGINE', 'SELLER_TOOL', TRUE)
        ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id
    """)).scalar()
    acc = db.execute(text("""
        INSERT INTO external_account (org_id, external_system_id, account_code,
               display_name) VALUES (:o, :s, 'DEFAULT', 'NE') RETURNING id
    """), {"o": str(org), "s": str(sys_id)}).scalar()
    db.execute(text("""
        INSERT INTO sales_claim (org_id, external_account_id, external_claim_id,
               claim_type, claim_status, financial_effect)
        VALUES (:o, :a, :c, 'CANCEL', 'COMPLETED', :e)
    """), {"o": str(org), "a": str(acc), "c": f"C-{uuid.uuid4().hex[:8]}",
           "e": financial_effect})
    db.commit()


def test_reorder_without_policy_is_not_configured(db: Session, org, actor):
    """기준이 없으면 계산하지 않는다. 30일을 지어내지 않는다."""
    p = make_product(db, org_id=org)
    inventory.record_transaction(
        db, org_id=org, product_id=p, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=5, actor_user_id=actor,
        unit_cost=1000)
    db.commit()

    result = exception_center.build(db, org_id=org)
    inv = _categories(result)["INVENTORY"]
    assert inv["status"] == "NOT_CONFIGURED"
    assert "기준" in (inv["note"] or "")
    # 재주문 관련 항목이 하나도 만들어지지 않았다.
    assert not [i for i in result["items"] if i["kind"].startswith("REORDER")]


def test_reorder_uses_policy_guardrails(db: Session, org, actor):
    """정책이 기준을 가지면 그걸 쓴다 — 화면 파라미터가 아니라."""
    p = make_product(db, org_id=org, cost_amount=1000)
    inventory.record_transaction(
        db, org_id=org, product_id=p, product_variant_id=None,
        transaction_type="OPENING", quantity_delta=1, actor_user_id=actor,
        unit_cost=1000)
    db.commit()
    auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="REORDER_MONITOR",
        enabled=True, mode="REPORT_ONLY",
        guardrails={"velocity_days": 30, "coverage_days": 30})

    result = exception_center.build(db, org_id=org)
    inv = _categories(result)["INVENTORY"]
    # 이제 계산은 됐다. 소싱처가 없으니 "계산 불가" 가 보고된다.
    assert inv["status"] == "ATTENTION"
    incomplete = next(
        i for i in result["items"] if i["kind"] == "REORDER_DATA_INCOMPLETE")
    # 사유가 **구체적**이어야 한다. "추천 불가" 로 뭉뚱그리면 사용자가 무엇을
    # 채워야 하는지 모른다. 어느 사유인지는 데이터에 달렸으므로(판매 이력이
    # 없으면 소싱처를 보기 전에 그것부터 걸린다) 목록에 있는지로 확인한다.
    from app.services.reorder import BLOCK_REASONS

    assert any(msg in incomplete["detail"] for msg in BLOCK_REASONS.values())
    # 기능이 안 되는 것이지 마감을 막는 게 아니다.
    assert incomplete["severity"] == "INFO"


def test_negative_inventory_is_listed(db: Session, org, actor):
    p = make_product(db, org_id=org, name_ko="음수상품")
    inventory.adjust(
        db, org_id=org, actor_user_id=actor, product_id=p,
        product_variant_id=None, quantity_delta=-4, reason="실사 차이")

    result = exception_center.build(db, org_id=org)
    row = next(i for i in result["items"] if i["kind"] == "NEGATIVE_INVENTORY")
    assert "음수상품" in row["title"]
    # 무엇을 하면 되는지 알려 준다.
    assert row["fix_link"]


def test_overdue_purchase_is_listed(db: Session, org, actor):
    """예상 입고일이 지났는데 안 온 발주."""
    from app.services import purchasing

    site = uuid.uuid4()
    db.execute(text("""
        INSERT INTO supplier_site (id, org_id, supplier_code, name, country_code)
        VALUES (:id, :o, :c, '지연공급처', 'KR')
    """), {"id": str(site), "o": str(org), "c": f"S-{site.hex[:8]}"})
    p = make_product(db, org_id=org)
    po = purchasing.create_order(
        db, org_id=org, actor_user_id=actor, supplier_site_id=site,
        lines=[{"product_id": str(p), "quantity": 5, "unit_price": 1000}],
        expected_at=date.today() - timedelta(days=3))
    purchasing.set_status(
        db, org_id=org, actor_user_id=actor, order_id=uuid.UUID(po["id"]),
        status="ORDERED")

    result = exception_center.build(db, org_id=org)
    assert any(i["kind"] == "PURCHASE_OVERDUE" for i in result["items"])


def test_normal_state_produces_empty_list(db: Session, org):
    """아무 문제가 없으면 목록이 비어 있다. 정상 주문을 올리지 않는다."""
    result = exception_center.build(db, org_id=org)
    assert result["items"] == []
    assert result["total"] == 0


def test_severity_uses_existing_vocabulary(db: Session, org, actor):
    """새 점수 체계를 만들지 않았다."""
    p = make_product(db, org_id=org)
    inventory.adjust(
        db, org_id=org, actor_user_id=actor, product_id=p,
        product_variant_id=None, quantity_delta=-1, reason="실사")

    result = exception_center.build(db, org_id=org)
    assert set(result["by_severity"]) == {"BLOCKING", "WARNING", "INFO"}
    for item in result["items"]:
        assert item["severity"] in ("BLOCKING", "WARNING", "INFO")


def test_daily_report_matches_exception_center(db: Session, org, actor):
    """두 화면의 숫자가 같아야 한다.

    서버가 같은 읽기 모델을 쓰는지 확인한다. 각자 세면 언젠가 갈라진다.
    """
    p = make_product(db, org_id=org)
    inventory.adjust(
        db, org_id=org, actor_user_id=actor, product_id=p,
        product_variant_id=None, quantity_delta=-2, reason="실사")

    center = exception_center.build(db, org_id=org)
    report = daily_report.build(db, org_id=org)

    assert report["exception_summary"]["total"] == center["total"]
    assert report["exception_summary"]["by_severity"] == center["by_severity"]


def test_one_broken_section_does_not_kill_the_rest(
    db: Session, org, actor, monkeypatch,
):
    """연동 조회가 깨져도 승인 대기는 보여야 한다."""
    def boom(*args, **kwargs):
        raise RuntimeError("연동 조회 실패")

    monkeypatch.setattr(exception_center, "_integrations", boom)

    p = make_product(db, org_id=org)
    inventory.adjust(
        db, org_id=org, actor_user_id=actor, product_id=p,
        product_variant_id=None, quantity_delta=-1, reason="실사")

    result = exception_center.build(db, org_id=org)
    assert _categories(result)["INTEGRATION"]["status"] == "ERROR"
    # 재고 예외는 살아 있다.
    assert any(i["kind"] == "NEGATIVE_INVENTORY" for i in result["items"])
