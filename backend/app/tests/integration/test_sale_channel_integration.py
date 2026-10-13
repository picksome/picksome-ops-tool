"""
주의: 커스텀 채널 코드로 OHOUSE 를 쓰지 않는다.
M015(2026-08-05)가 오늘의집을 **시스템 채널**로 시드하면서 org 커스텀 채널과 코드가 충돌한다.
이 테스트가 검증하려는 건 "org 전용 커스텀 채널" 이므로 시스템 카탈로그에 없는 코드를 쓴다.
services/sale_channel.py 통합 테스트 (M007, docs/27 D-287).

ENUM -> TEXT 전환 이후 앱 레벨 검증이 실제로 막고/허용하는지 확인.
"""

import pytest

from app.core.exceptions import ValidationError
from app.domain.sales import SaleOrderCreate
from app.services.sale_channel import create_custom_channel, is_valid_channel_code, list_channels
from app.services.sales import create_sale
from app.tests.fixtures import factories as f


def test_system_channel_valid_for_any_new_org(db):
    org_id = f.make_organization(db)
    assert is_valid_channel_code(db, org_id=org_id, code="COUPANG")
    assert is_valid_channel_code(db, org_id=org_id, code="TEMU")
    assert not is_valid_channel_code(db, org_id=org_id, code="NOT_A_REAL_MARKET")


def test_create_custom_channel_and_use_in_sales_order(db):
    org_id = f.make_organization(db)
    actor = f.make_app_user(db, org_id=org_id)
    product_id = f.make_product(db, org_id=org_id, cost_amount=5_000)

    create_custom_channel(db, org_id=org_id, actor=actor, code="MYSHOP", display_name="자체몰")
    assert is_valid_channel_code(db, org_id=org_id, code="MYSHOP")

    out = create_sale(
        db, org_id=org_id, actor=actor,
        body=SaleOrderCreate(
            sale_date="2026-07-29", marketplace="MYSHOP", marketplace_order_no="MYSHOP-1",
            product_id=product_id, quantity=1, sale_price=10_000,
        ),
    )
    assert out.marketplace == "MYSHOP"


def test_custom_channel_is_org_scoped(db):
    org_a = f.make_organization(db)
    org_b = f.make_organization(db)
    actor_a = f.make_app_user(db, org_id=org_a)

    create_custom_channel(db, org_id=org_a, actor=actor_a, code="MYSHOP", display_name="자체몰")

    assert is_valid_channel_code(db, org_id=org_a, code="MYSHOP")
    assert not is_valid_channel_code(db, org_id=org_b, code="MYSHOP")


def test_create_sale_rejects_unknown_marketplace(db):
    org_id = f.make_organization(db)
    actor = f.make_app_user(db, org_id=org_id)
    product_id = f.make_product(db, org_id=org_id, cost_amount=5_000)

    with pytest.raises(ValidationError):
        create_sale(
            db, org_id=org_id, actor=actor,
            body=SaleOrderCreate(
                sale_date="2026-07-29", marketplace="NOT_A_REAL_MARKET", marketplace_order_no="BAD-1",
                product_id=product_id, quantity=1, sale_price=10_000,
            ),
        )


def test_duplicate_custom_channel_code_rejected(db):
    org_id = f.make_organization(db)
    actor = f.make_app_user(db, org_id=org_id)

    create_custom_channel(db, org_id=org_id, actor=actor, code="MYSHOP", display_name="자체몰")
    with pytest.raises(ValidationError):
        create_custom_channel(db, org_id=org_id, actor=actor, code="MYSHOP", display_name="오늘의집2")


def test_list_channels_includes_system_and_custom(db):
    org_id = f.make_organization(db)
    actor = f.make_app_user(db, org_id=org_id)
    create_custom_channel(db, org_id=org_id, actor=actor, code="MYSHOP", display_name="자체몰")

    codes = {c["code"] for c in list_channels(db, org_id=org_id)}
    assert {"COUPANG", "TEMU", "TOSS", "MYSHOP"} <= codes
