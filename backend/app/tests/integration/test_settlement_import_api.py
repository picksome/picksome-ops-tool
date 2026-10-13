"""정산서 가져오기 HTTP 계층 (M056 · docs/82).

이 파일이 고정하는 것
------------------
1. 기본이 `DRY_RUN` 이다 — mode 를 안 보내면 반영되지 않는다
2. 같은 파일을 두 번 올려도 행이 두 번 생기지 않는다
3. 판매계정을 **화면이 고른다** — 서버가 첫 쿠팡 계정을 자동 선택하지 않는다
4. 정산서가 아닌 파일은 반쯤 읽지 않고 거부한다
5. 권한 · 조직 격리
"""

from __future__ import annotations

import io
import uuid
from datetime import date

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.security import create_access_token
from app.deps import get_db
from app.main import app
from app.services import settlement_import as si
from app.tests.fixtures.factories import (
    make_app_user,
    make_integration_account,
    make_organization,
    make_product,
    make_sales_order,
)
from app.tests.integration.test_settlement_file_import import HEADERS, _order_rows

URL = "/api/v1/settlements/import"


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
def account(db: Session, org) -> uuid.UUID:
    return make_integration_account(
        db, org_id=org, provider="COUPANG", account_label="쿠팡 스토어")


def _auth(user_id, org_id, role="ADMIN") -> dict:
    return {"Authorization": f"Bearer {create_access_token(
        user_id=user_id, org_id=org_id, role=role)}"}


def _xlsx(rows, headers=HEADERS) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Excel Data (1)"
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    wb.close()
    return buf.getvalue()


def _sale(db, org, order_no):
    p = make_product(db, org_id=org, cost_amount=1000)
    return make_sales_order(
        db, org_id=org, product_id=p, sale_date=date(2025, 12, 1),
        sale_price=24000, marketplace="COUPANG", marketplace_order_no=order_no)


def _post(client, headers, account, data: bytes, mode=None):
    form = {"integration_account_id": str(account)}
    if mode:
        form["mode"] = mode
    return client.post(
        URL, headers=headers, data=form,
        files={"file": ("settlement.xlsx", data,
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet")},
    )


def _count(db, org) -> int:
    return db.execute(text("""
        SELECT count(*) FROM marketplace_settlement_record
         WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org)}).scalar()


def test_default_mode_is_dry_run(client, db, org, admin, account):
    """`mode` 를 안 보내면 **반영되지 않는다.**"""
    _sale(db, org, "1001")
    data = _xlsx(_order_rows("1001", gross=24000, fee=2592, net=21408))
    r = _post(client, _auth(admin, org), account, data)
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert body["mode"] == si.DRY_RUN
    assert body["inserted"] == 3 and body["reconciles"] is True
    assert _count(db, org) == 0


def test_commit_persists_and_matches(client, db, org, admin, account):
    order = _sale(db, org, "1001")
    data = _xlsx(_order_rows("1001", gross=24000, fee=2592, net=21408))
    body = _post(client, _auth(admin, org), account, data,
                 mode=si.COMMIT).json()["data"]
    assert body["inserted"] == 3 and body["matched_exact"] == 3
    assert _count(db, org) == 3
    assert db.execute(text("""
        SELECT count(*) FROM marketplace_settlement_record
         WHERE org_id = :o AND matched_sales_order_id = :s
    """), {"o": str(org), "s": str(order)}).scalar() == 3


def test_same_upload_twice_is_idempotent(client, db, org, admin, account):
    _sale(db, org, "1001")
    data = _xlsx(_order_rows("1001", gross=24000, fee=2592, net=21408))
    h = _auth(admin, org)
    first = _post(client, h, account, data, mode=si.COMMIT).json()["data"]
    second = _post(client, h, account, data, mode=si.COMMIT).json()["data"]
    assert first["inserted"] == 3
    assert second["inserted"] == 0 and second["unchanged"] == 3
    assert _count(db, org) == 3


def test_non_settlement_file_is_rejected(client, db, org, admin, account):
    """미확정 스냅샷처럼 다른 WING 파일은 정산서가 아니다."""
    snap = ["주문번호", "상품번호", "상품명", "구매자명", "판매가", "판매금액",
            "판매배송비", "취소금액", "취소배송비", "주문일", "배송완료일", "배송상태"]
    r = _post(client, _auth(admin, org), account, _xlsx([], headers=snap))
    assert r.status_code == 422, r.text
    assert "필수 컬럼" in r.text


def test_garbage_file_is_rejected(client, db, org, admin, account):
    r = _post(client, _auth(admin, org), account, b"not an excel file")
    assert r.status_code == 422


def test_account_is_required(client, db, org, admin):
    """서버가 첫 쿠팡 계정을 자동 선택하지 않는다."""
    data = _xlsx(_order_rows("1001", gross=100, fee=0, net=100))
    r = client.post(URL, headers=_auth(admin, org),
                    files={"file": ("s.xlsx", data, "application/octet-stream")})
    assert r.status_code == 422


def test_readonly_cannot_import(client, db, org, account):
    viewer = make_app_user(db, org_id=org, role="READONLY")
    data = _xlsx(_order_rows("1001", gross=100, fee=0, net=100))
    r = _post(client, _auth(viewer, org, "READONLY"), account, data, mode=si.COMMIT)
    assert r.status_code == 403


def test_other_org_account_is_not_found(client, db, org, admin):
    other = make_organization(db)
    foreign = make_integration_account(
        db, org_id=other, provider="COUPANG", account_label="남의 스토어")
    data = _xlsx(_order_rows("1001", gross=100, fee=0, net=100))
    r = _post(client, _auth(admin, org), foreign, data)
    assert r.status_code == 404


def test_runs_history_lists_only_own_org(client, db, org, admin, account):
    _sale(db, org, "1001")
    data = _xlsx(_order_rows("1001", gross=24000, fee=2592, net=21408))
    _post(client, _auth(admin, org), account, data, mode=si.COMMIT)

    other = make_organization(db)
    other_admin = make_app_user(db, org_id=other, role="ADMIN")
    runs = client.get(f"{URL}/runs", headers=_auth(admin, org)).json()["data"]
    assert len(runs) == 1 and runs[0]["record_count"] == 3
    assert client.get(f"{URL}/runs",
                      headers=_auth(other_admin, other)).json()["data"] == []
