"""검증센터 HTTP 계약 — 권한 · 조직 격리 · 유출.

이 파일이 고정하는 것
------------------
```
1. 재무 데이터라 마진 화면과 같은 권한이다
2. EXTERNAL_ACCOUNTANT 는 못 본다 (마진 화면과 같은 판단)
3. 읽기 전용 역할은 기준값을 승인할 수 없다
4. 다른 조직 주문을 조회할 수 없다
5. 응답에 개인정보 · 비밀이 없다
6. 서버가 기준값을 채워 주지 않는다
```
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.deps import get_db
from app.main import app
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
    make_sales_order,
)

SALE_DATE = date(2026, 1, 15)


@pytest.fixture
def client(db: Session):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def admin(db: Session, org) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


@pytest.fixture
def order(db: Session, org) -> uuid.UUID:
    product = make_product(db, org_id=org, cost_amount=4000)
    sid = make_sales_order(db, org_id=org, product_id=product,
                           sale_date=SALE_DATE, sale_price=10000,
                           purchase_cost=4000)
    db.commit()
    return sid


def _auth(user_id, org_id, role="ADMIN") -> dict:
    return {"Authorization":
            f"Bearer {create_access_token(user_id=user_id, org_id=org_id, role=role)}"}


# ── 권한 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "role,expected",
    [("ADMIN", 200), ("OPERATOR", 200), ("ACCOUNTING", 200),
     ("SOURCING", 200), ("READONLY", 200),
     # 마진 화면이 이미 제외한다. 여기만 열면 같은 데이터가 다른 문으로 샌다.
     ("EXTERNAL_ACCOUNTANT", 403)],
)
def test_summary_uses_the_same_permission_set_as_the_margin_screen(
    client, org, admin, role, expected,
):
    res = client.get("/api/v1/verification/summary",
                     headers=_auth(admin, org, role))
    assert res.status_code == expected


@pytest.mark.parametrize("role", ["READONLY", "OPERATOR", "SOURCING"])
def test_readonly_roles_cannot_approve_a_baseline(client, org, admin, role):
    """기준값 승인은 재무 판단이다."""
    res = client.post(
        f"/api/v1/verification/golden-samples/{uuid.uuid4()}/approve",
        headers=_auth(admin, org, role),
        json={"expected": {"margin_amount": 1}, "source_evidence": "정산서"})
    assert res.status_code == 403


# ── 조직 격리 ───────────────────────────────────────────────────────────

def test_another_orgs_order_is_not_visible(client, db, org, admin, order):
    other = make_organization(db)
    other_admin = make_app_user(db, org_id=other, role="ADMIN")
    db.commit()

    res = client.get(f"/api/v1/verification/orders/{order}",
                     headers=_auth(other_admin, other))

    assert res.status_code == 404


def test_summary_of_an_empty_org_shows_nothing_from_another_org(
    client, db, org, admin, order,
):
    other = make_organization(db)
    other_admin = make_app_user(db, org_id=other, role="ADMIN")
    db.commit()

    res = client.get("/api/v1/verification/summary",
                     headers=_auth(other_admin, other))

    assert res.status_code == 200
    assert res.json()["data"]["confidence"]["orders_total"] == 0


# ── 유출 ────────────────────────────────────────────────────────────────

def test_the_trace_response_has_no_personal_information(client, org, admin, order):
    res = client.get(f"/api/v1/verification/orders/{order}",
                     headers=_auth(admin, org))

    assert res.status_code == 200
    blob = res.text.lower()
    for forbidden in ("recipient", "phone", "postcode", "수취인", "전화번호", "배송지"):
        assert forbidden not in blob


def test_the_summary_response_has_no_connection_string_or_secret(
    client, org, admin, order,
):
    res = client.get("/api/v1/verification/summary", headers=_auth(admin, org))

    blob = res.text
    assert "postgresql+psycopg://" not in blob
    assert "password" not in blob.lower()


# ── 기준값 ──────────────────────────────────────────────────────────────

def test_the_server_never_fills_in_the_expected_value(client, db, org, admin, order):
    """§25 — 값을 안 주면 거부한다. 계산 결과로 채워 주지 않는다."""
    from app.services import verification_golden as golden

    golden.register_candidates(
        db, org_id=org, actor_user_id=admin,
        candidates=[{"sales_order_id": str(order), "case_type": "ESTIMATED_ONLY",
                     "selection_reason": "테스트"}])
    case_id = db.execute(text("""
        SELECT id FROM verification_golden_case WHERE org_id = :o
    """), {"o": str(org)}).scalar()

    res = client.post(
        f"/api/v1/verification/golden-samples/{case_id}/approve",
        headers=_auth(admin, org),
        json={"expected": {}, "source_evidence": "정산서"})

    assert res.status_code == 422 or res.status_code == 400
    still_candidate = db.execute(text("""
        SELECT status, expected_json FROM verification_golden_case WHERE id = :i
    """), {"i": str(case_id)}).mappings().one()
    assert still_candidate["status"] == "CANDIDATE"
    assert still_candidate["expected_json"] is None


def test_candidates_endpoint_does_not_persist_or_approve(client, db, org, admin, order):
    res = client.get("/api/v1/verification/golden-samples/candidates",
                     headers=_auth(admin, org))

    assert res.status_code == 200
    assert db.execute(text(
        "SELECT count(*) FROM verification_golden_case WHERE org_id = :o"),
        {"o": str(org)}).scalar() == 0


# ── 요약 계약 ───────────────────────────────────────────────────────────

def test_the_evidence_endpoint_gives_everything_needed_in_one_call(
    client, org, admin, order,
):
    """§23 — 사용자가 엑셀·DB·계산기를 따로 열지 않아도 되어야 한다."""
    res = client.get(f"/api/v1/verification/golden-samples/evidence/{order}",
                     headers=_auth(admin, org))

    assert res.status_code == 200
    data = res.json()["data"]
    # 원천값 · 독립 검산 · 비교 · 확인 목록이 한 번에 온다.
    assert data["facts"]
    assert "independent" in data
    assert "comparison" in data
    assert data["checklist"]
    assert data["formula"]
    assert data["golden_confidence"] in ("ACTUAL", "ESTIMATED", "UNKNOWN")


def test_the_evidence_endpoint_is_org_scoped(client, db, org, admin, order):
    other = make_organization(db)
    other_admin = make_app_user(db, org_id=other, role="ADMIN")
    db.commit()

    res = client.get(f"/api/v1/verification/golden-samples/evidence/{order}",
                     headers=_auth(other_admin, other))

    assert res.status_code == 404


def test_the_recommended_set_is_smaller_than_the_full_candidate_list(
    client, org, admin, order,
):
    """§32 — 16건을 전부 보라고 하면 아무도 시작하지 않는다."""
    res = client.get("/api/v1/verification/golden-samples/recommended",
                     headers=_auth(admin, org))

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["recommended_count"] <= data["total_candidates"] or data["total_candidates"] == 0
    for r in data["recommended"]:
        assert r["covers"]


@pytest.mark.parametrize("role", ["READONLY", "OPERATOR", "SOURCING"])
def test_readonly_roles_cannot_defer(client, org, admin, role):
    """보류도 재무 판단이다."""
    res = client.post(
        f"/api/v1/verification/golden-samples/{uuid.uuid4()}/defer",
        headers=_auth(admin, org, role),
        json={"reason": "OTHER"})
    assert res.status_code == 403


def test_defer_is_recorded_as_deferred_not_failed(client, db, org, admin, order):
    """§21 — 보류는 실패가 아니다."""
    from app.services import verification_golden as golden

    golden.register_candidates(
        db, org_id=org, actor_user_id=admin,
        candidates=[{"sales_order_id": str(order), "case_type": "ESTIMATED_ONLY",
                     "selection_reason": "테스트"}])
    case_id = db.execute(text(
        "SELECT id FROM verification_golden_case WHERE org_id = :o"),
        {"o": str(org)}).scalar()

    res = client.post(f"/api/v1/verification/golden-samples/{case_id}/defer",
                      headers=_auth(admin, org),
                      json={"reason": "COST_UNCERTAIN", "note": "매입 기록 확인"})

    assert res.status_code == 200
    assert res.json()["data"]["status"] == "DEFERRED"
    # 회귀는 승인된 것만 본다.
    reg = client.get("/api/v1/verification/golden-samples/regression",
                     headers=_auth(admin, org)).json()["data"]
    assert reg["counts"]["FAIL"] == 0


def test_approval_stores_the_evidence_snapshot(client, db, org, admin, order):
    """§15 — 승인 순간의 근거가 남아야 나중에 원인을 가릴 수 있다."""
    from app.services import verification_golden as golden

    golden.register_candidates(
        db, org_id=org, actor_user_id=admin,
        candidates=[{"sales_order_id": str(order), "case_type": "ESTIMATED_ONLY",
                     "selection_reason": "테스트"}])
    case_id = db.execute(text(
        "SELECT id FROM verification_golden_case WHERE org_id = :o"),
        {"o": str(org)}).scalar()

    res = client.post(
        f"/api/v1/verification/golden-samples/{case_id}/approve",
        headers=_auth(admin, org),
        json={"expected": {"margin_amount": 5000},
              "source_evidence": "정산서 2026-01.xlsx"})

    assert res.status_code == 200
    snapshot = db.execute(text("""
        SELECT approved_evidence_json FROM verification_golden_case WHERE id = :i
    """), {"i": str(case_id)}).scalar()
    assert snapshot is not None
    assert "facts" in snapshot


def test_summary_keeps_the_three_states_separate(client, org, admin, order):
    res = client.get("/api/v1/verification/summary", headers=_auth(admin, org))

    data = res.json()["data"]
    assert set(data["counts"]) == {"PASS", "FAIL", "NOT_VERIFIED"}
    # 미검증이 통과에 섞이지 않았는지 — 합이 판정 수와 같아야 한다.
    assert sum(data["counts"].values()) == len(data["verdicts"])
    assert data["evidence_gates"]["refund_financial"]["state"] == "NOT_VERIFIED"


def test_exceptions_endpoint_lists_only_things_to_check(client, org, admin, order):
    res = client.get("/api/v1/verification/exceptions", headers=_auth(admin, org))

    assert res.status_code == 200
    items = res.json()["data"]["verification"]
    # 정상 건을 나열하지 않는다 — 0 건인 항목은 목록에 없다.
    assert all(item["count"] > 0 for item in items)
