"""외부 시스템 연동 기반 (M038/M039).

이 파일이 고정하는 것
------------------
1. 외부 계정은 조직별이고 한 시스템에 여러 개 가질 수 있다
2. **비밀은 어떤 응답에도 나가지 않는다** — 설정 여부만
3. 한 외부 항목은 내부 하나에만 붙는다 (DB 가 막는다)
4. 한 내부 항목에 여러 외부 매핑은 허용
5. 동기화는 **멱등**하다 — 같은 상품을 다섯 번 가져와도 하나다
6. **사람이 고친 값을 외부가 덮지 않는다** — 충돌로 남는다
7. 외부 매입가는 `product.cost_amount` 를 바꾸지 않는다
8. 커넥터가 없으면 **미지원**이지 성공이 아니다

6~7 이 이 기반의 핵심이다
----------------------
덮어쓰면 사용자는 같은 수정을 매번 반복하다 동기화를 꺼 버린다.
원가를 자동 채택하면 승인 체계가 장식이 된다.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.services import connector as conn
from app.services import external_sync as ex
from app.services import sourcing_core as sc
from app.tests.fixtures.factories import (
    make_app_user,
    make_organization,
    make_product,
)
from app.tests.fixtures.fake_connector import (
    TEST_SYSTEM_CODE,
    FakeSellerToolConnector,
    use_fake_connector,
)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


@pytest.fixture
def system_id(db: Session) -> uuid.UUID:
    """테스트 전용 외부 시스템 행."""
    row = db.execute(text("""
        INSERT INTO external_system (code, name, system_type, supports_api_key)
        VALUES (:c, '테스트 셀러툴', 'SELLER_TOOL', TRUE)
        ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
    """), {"c": TEST_SYSTEM_CODE}).mappings().one()
    db.commit()
    return uuid.UUID(str(row["id"]))


@pytest.fixture
def account(db: Session, org, actor, system_id) -> dict:
    """자격증명이 **설정된** 계정.

    M042 부터 동기화는 자격증명 없이는 거부된다(무인 실행이 조용히 401 을 받는
    상태를 만들지 않기 위해서다). 그래서 sync 를 검증하는 테스트는 설정된
    계정에서 출발한다 — 미설정 상태의 거부는 별도 테스트가 본다.

    값은 가짜다. 실제 credential 을 fixture 에 넣지 않는다.
    """
    acc = ex.create_account(
        db, org_id=org, actor_user_id=actor, external_system_id=system_id,
        display_name="테스트 계정", account_code="ACC-1",
    )
    return ex.set_credential_secret(
        db, org_id=org, actor_user_id=actor, account_id=uuid.UUID(acc["id"]),
        credential_kind="API_KEY", payload={"api_key": "TEST_SECRET"},
    )


@pytest.fixture
def account_without_credential(db: Session, org, actor, system_id) -> dict:
    return ex.create_account(
        db, org_id=org, actor_user_id=actor, external_system_id=system_id,
        display_name="자격증명 없는 계정", account_code="ACC-NOCRED",
    )


# ──────────────────────────────────────────────────────────────────────────
# 계정
# ──────────────────────────────────────────────────────────────────────────

def test_one_org_can_have_several_accounts_of_one_system(db, org, actor, system_id):
    """법인을 나눠 쓰면 같은 셀러툴 계정이 둘 이상이다."""
    a = ex.create_account(db, org_id=org, actor_user_id=actor,
                          external_system_id=system_id,
                          display_name="본사", account_code="ACC-1")
    b = ex.create_account(db, org_id=org, actor_user_id=actor,
                          external_system_id=system_id,
                          display_name="지사", account_code="ACC-2")
    assert a["id"] != b["id"]
    assert len(ex.list_accounts(db, org_id=org)) == 2


def test_duplicate_account_code_is_rejected(db, org, actor, system_id, account):
    with pytest.raises(ConflictError):
        ex.create_account(db, org_id=org, actor_user_id=actor,
                          external_system_id=system_id,
                          display_name="중복", account_code="ACC-1")


def test_account_never_exposes_a_secret(db, org, actor, account):
    """설정 여부만 나간다. 값도, 암호문도, 참조도 나가지 않는다."""
    out = ex.get_account(db, org_id=org, account_id=uuid.UUID(account["id"]))

    assert out["credential_configured"] is True
    # 참조를 알면 무엇을 노려야 하는지 알게 된다.
    assert "credential_secret_ref" not in out
    assert "credentials_vault_entry_id" not in out
    for banned in ("secret", "password", "access_token", "refresh_token", "api_key"):
        assert not any(banned in k for k in out), f"{banned} 가 응답에 있다"
    assert "TEST_SECRET" not in repr(out)


def test_configured_means_the_machine_can_authenticate(
    db, org, actor, account_without_credential,
):
    """vault 쪽지만 있는 상태를 "설정됨" 이라고 말하지 않는다.

    쪽지에는 값이 없어서 새벽 배치가 열 수 없다. 하나로 합치면 사용자는
    동기화가 왜 계속 실패하는지 알 수 없다.
    """
    account_id = uuid.UUID(account_without_credential["id"])
    entry = db.execute(text("""
        INSERT INTO password_vault_entry
               (org_id, service_name, vault_provider, vault_item_name)
        VALUES (:o, '셀러툴', 'Bitwarden', '셀러툴 항목') RETURNING id
    """), {"o": str(org)}).scalar_one()
    db.commit()

    out = ex.link_vault_note(db, org_id=org, actor_user_id=actor,
                             account_id=account_id, vault_entry_id=entry)
    assert out["vault_note_linked"] is True
    assert out["credential_configured"] is False

    out = ex.set_credential_secret(
        db, org_id=org, actor_user_id=actor, account_id=account_id,
        credential_kind="API_KEY", payload={"api_key": "TEST_SECRET"},
    )
    assert out["credential_configured"] is True
    # 쪽지는 그대로 남는다 — 담당자가 바뀔 때의 단서다.
    assert out["vault_note_linked"] is True


def test_accounts_do_not_leak_across_orgs(db, org, actor, account):
    other = make_organization(db)
    assert ex.list_accounts(db, org_id=other) == []
    with pytest.raises(NotFoundError):
        ex.get_account(db, org_id=other, account_id=uuid.UUID(account["id"]))


def test_disabling_keeps_history(db, org, actor, account):
    """비활성화는 삭제가 아니다 — 과거 매핑과 동기화 기록이 남아야 한다."""
    out = ex.set_account_status(db, org_id=org, actor_user_id=actor,
                                account_id=uuid.UUID(account["id"]), status="DISABLED")
    assert out["status"] == "DISABLED"
    # 목록에서 사라지지 않는다 — 사라지면 왜 동기화가 멈췄는지 알 수 없다.
    assert len(ex.list_accounts(db, org_id=org)) == 1


# ──────────────────────────────────────────────────────────────────────────
# 매핑
# ──────────────────────────────────────────────────────────────────────────

def test_one_external_entity_maps_to_one_internal(db, org, actor, account):
    """뚫리면 같은 외부 상품의 재고·원가가 두 상품으로 갈라져 들어온다."""
    p1 = make_product(db, org_id=org)
    p2 = make_product(db, org_id=org)
    db.commit()

    ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                      external_account_id=uuid.UUID(account["id"]),
                      external_entity_type="PRODUCT", external_entity_id="123",
                      internal_entity_type="PRODUCT", internal_entity_id=p1)

    with pytest.raises(ConflictError) as err:
        ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                          external_account_id=uuid.UUID(account["id"]),
                          external_entity_type="PRODUCT", external_entity_id="123",
                          internal_entity_type="PRODUCT", internal_entity_id=p2)
    assert "재연결" in str(err.value)


def test_database_blocks_duplicate_external_identity(db, org, actor, account):
    """서비스를 우회해도 막힌다 — 동시 sync 두 개가 각각 붙일 수 있다."""
    p1 = make_product(db, org_id=org)
    p2 = make_product(db, org_id=org)
    db.commit()
    ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                      external_account_id=uuid.UUID(account["id"]),
                      external_entity_type="PRODUCT", external_entity_id="123",
                      internal_entity_type="PRODUCT", internal_entity_id=p1)

    with pytest.raises(IntegrityError):
        db.execute(text("""
            INSERT INTO external_entity_mapping
                   (org_id, external_account_id, external_entity_type,
                    external_entity_id, internal_entity_type, internal_entity_id)
            VALUES (:o, :a, 'PRODUCT', '123', 'PRODUCT', :p)
        """), {"o": str(org), "a": account["id"], "p": str(p2)})
        db.flush()
    db.rollback()


def test_one_internal_can_have_many_external_mappings(db, org, actor, system_id):
    """셀러툴을 둘 쓰는 조직이 있다."""
    a1 = ex.create_account(db, org_id=org, actor_user_id=actor,
                           external_system_id=system_id,
                           display_name="툴A", account_code="A")
    a2 = ex.create_account(db, org_id=org, actor_user_id=actor,
                           external_system_id=system_id,
                           display_name="툴B", account_code="B")
    product = make_product(db, org_id=org)
    db.commit()

    for acc, ext in ((a1, "123"), (a2, "ABC")):
        ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                          external_account_id=uuid.UUID(acc["id"]),
                          external_entity_type="PRODUCT", external_entity_id=ext,
                          internal_entity_type="PRODUCT", internal_entity_id=product)

    assert len(ex.list_mappings(db, org_id=org, internal_entity_id=product)) == 2


def test_mapping_is_idempotent(db, org, actor, account):
    product = make_product(db, org_id=org)
    db.commit()
    args = dict(external_account_id=uuid.UUID(account["id"]),
                external_entity_type="PRODUCT", external_entity_id="123",
                internal_entity_type="PRODUCT", internal_entity_id=product)

    first = ex.upsert_mapping(db, org_id=org, actor_user_id=actor, **args)
    second = ex.upsert_mapping(db, org_id=org, actor_user_id=actor, **args)

    assert first["created"] is True
    assert second["created"] is False
    assert first["id"] == second["id"]


def test_remap_requires_a_reason_and_is_audited(db, org, actor, account):
    """연결을 옮기면 그 뒤의 재고·원가가 다른 상품으로 들어간다."""
    p1 = make_product(db, org_id=org)
    p2 = make_product(db, org_id=org)
    db.commit()
    m = ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                          external_account_id=uuid.UUID(account["id"]),
                          external_entity_type="PRODUCT", external_entity_id="123",
                          internal_entity_type="PRODUCT", internal_entity_id=p1)

    from app.core.exceptions import ValidationError

    with pytest.raises(ValidationError):
        ex.remap_mapping(db, org_id=org, actor_user_id=actor,
                         mapping_id=uuid.UUID(m["id"]), internal_entity_id=p2, reason="  ")

    ex.remap_mapping(db, org_id=org, actor_user_id=actor,
                     mapping_id=uuid.UUID(m["id"]), internal_entity_id=p2,
                     reason="상품 통합")

    rows = db.execute(text("""
        SELECT reason FROM audit_log
         WHERE domain_table = 'external_entity_mapping' AND action = 'UPDATE'
           AND org_id = :o
    """), {"o": str(org)}).scalars().all()
    assert "상품 통합" in rows


def test_mapping_to_another_orgs_product_is_rejected(db, org, actor, account):
    other = make_organization(db)
    foreign = make_product(db, org_id=other)
    db.commit()

    with pytest.raises(NotFoundError):
        ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                          external_account_id=uuid.UUID(account["id"]),
                          external_entity_type="PRODUCT", external_entity_id="X",
                          internal_entity_type="PRODUCT", internal_entity_id=foreign)


# ──────────────────────────────────────────────────────────────────────────
# 커넥터
# ──────────────────────────────────────────────────────────────────────────

def test_unimplemented_connector_reports_all_capabilities_false():
    """구현이 없으면 전부 False 다 — 그 자체가 정확한 답이다."""
    caps = conn.capabilities_of("NO_SUCH_SELLER_TOOL")
    assert set(caps) == set(conn.CAPABILITIES)
    assert not any(caps.values())
    assert conn.has_connector("NO_SUCH_SELLER_TOOL") is False


def test_next_engine_declares_only_what_it_can_actually_do():
    """되는 것보다 넓게 선언하면 화면이 거짓말을 한다.

    주문 읽기는 실데이터로 검증됐다(docs/77·78). 클레임은 READ 만 되고 재무
    반영이 없으므로 **선언하지 않는다** — `CLAIM_IMPORT = True` 로 두면 일일
    리포트가 취소·반품까지 보고 있다고 말하게 된다.
    """
    from app.connectors.bootstrap import register_builtin

    register_builtin()
    caps = conn.capabilities_of("NEXT_ENGINE")

    assert caps["ORDER_IMPORT"] is True
    assert caps["CLAIM_IMPORT"] is False
    assert caps["PRODUCT_IMPORT"] is False
    # 외부 쓰기는 영원히 없다.
    assert not any(v for k, v in caps.items() if k.endswith("_EXPORT"))


def test_next_engine_refuses_product_sync_instead_of_returning_empty():
    """빈 목록을 주면 "상품 0건" 으로 보이고, 그건 확인해 봤다는 뜻이다."""
    from app.connectors.next_engine.connector import NextEngineConnector

    with pytest.raises(conn.UnsupportedCapabilityError):
        NextEngineConnector().fetch_products(credentials={"api_key": "x"})


def test_sync_without_a_connector_is_unsupported_not_success(db, org, actor, account):
    """가짜 성공을 만들지 않는다."""
    with pytest.raises(conn.ConnectorUnavailableError) as err:
        ex.sync_products(db, org_id=org, actor_user_id=actor,
                         account_id=uuid.UUID(account["id"]))
    assert err.value.http_status == 501

    # 실행 기록은 남는다 — 시도했다는 사실 자체가 운영 정보다.
    runs = ex.list_runs(db, org_id=org)
    assert runs[0]["status"] == "UNSUPPORTED"
    assert runs[0]["error_code"] == "CONNECTOR_UNAVAILABLE"


def test_capability_false_blocks_that_sync_type(db, org, actor, account):
    fake = FakeSellerToolConnector(supported=frozenset({"ORDER_IMPORT"}))
    with use_fake_connector(fake), pytest.raises(conn.UnsupportedCapabilityError):
        ex.sync_products(db, org_id=org, actor_user_id=actor,
                         account_id=uuid.UUID(account["id"]))


def test_test_connector_cannot_register_in_production(monkeypatch):
    """운영에서 가짜 커넥터가 성공을 돌려주면 사용자는 동기화를 믿는다."""
    monkeypatch.setattr(conn, "_is_production", lambda: True)
    with pytest.raises(RuntimeError):
        conn.register(FakeSellerToolConnector(), allow_in_production=False)


# ──────────────────────────────────────────────────────────────────────────
# 동기화
# ──────────────────────────────────────────────────────────────────────────

def test_sync_does_not_create_products(db, org, actor, account):
    """셀러툴은 master 가 아니다. 우리에게 없는 상품을 만들지 않는다."""
    before = db.execute(text("SELECT COUNT(*) FROM product WHERE org_id = :o"),
                        {"o": str(org)}).scalar()
    fake = FakeSellerToolConnector([conn.ExternalProduct(external_id="999", name="외부만 있는 상품")])

    with use_fake_connector(fake):
        result = ex.sync_products(db, org_id=org, actor_user_id=actor,
                                  account_id=uuid.UUID(account["id"]))

    assert result.skipped == 1
    assert result.created == 0
    after = db.execute(text("SELECT COUNT(*) FROM product WHERE org_id = :o"),
                       {"o": str(org)}).scalar()
    assert after == before


def test_sync_is_idempotent(db, org, actor, account):
    """같은 상품을 다섯 번 가져와도 내부 상품이 다섯 개 생기면 안 된다."""
    product = make_product(db, org_id=org)
    db.commit()
    ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                      external_account_id=uuid.UUID(account["id"]),
                      external_entity_type="PRODUCT", external_entity_id="123",
                      internal_entity_type="PRODUCT", internal_entity_id=product)

    fake = FakeSellerToolConnector([conn.ExternalProduct(external_id="123", name="같은 이름")])
    with use_fake_connector(fake):
        for _ in range(5):
            ex.sync_products(db, org_id=org, actor_user_id=actor,
                             account_id=uuid.UUID(account["id"]))

    assert len(ex.list_mappings(db, org_id=org)) == 1
    assert db.execute(text("SELECT COUNT(*) FROM product WHERE org_id = :o"),
                      {"o": str(org)}).scalar() == 1


def test_external_value_updates_when_no_one_claimed_the_field(db, org, actor, account):
    product = make_product(db, org_id=org)
    db.commit()
    ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                      external_account_id=uuid.UUID(account["id"]),
                      external_entity_type="PRODUCT", external_entity_id="123",
                      internal_entity_type="PRODUCT", internal_entity_id=product)

    fake = FakeSellerToolConnector([conn.ExternalProduct(external_id="123", name="외부 이름")])
    with use_fake_connector(fake):
        result = ex.sync_products(db, org_id=org, actor_user_id=actor,
                                  account_id=uuid.UUID(account["id"]))

    assert result.updated == 1
    assert db.execute(text("SELECT name_ko FROM product WHERE id = :i"),
                      {"i": str(product)}).scalar() == "외부 이름"


def test_manual_override_is_never_overwritten(db, org, actor, account):
    """사용자가 고친 값을 다음 sync 가 되돌리면 동기화를 꺼 버리게 된다."""
    product = make_product(db, org_id=org)
    db.commit()
    ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                      external_account_id=uuid.UUID(account["id"]),
                      external_entity_type="PRODUCT", external_entity_id="123",
                      internal_entity_type="PRODUCT", internal_entity_id=product)

    db.execute(text("UPDATE product SET name_ko = '사람이 정한 이름' WHERE id = :i"),
               {"i": str(product)})
    ex.mark_manual_override(db, org_id=org, actor_user_id=actor,
                            internal_entity_type="PRODUCT", internal_entity_id=product,
                            field_name="name_ko")
    db.commit()

    fake = FakeSellerToolConnector([conn.ExternalProduct(external_id="123", name="외부 이름")])
    with use_fake_connector(fake):
        result = ex.sync_products(db, org_id=org, actor_user_id=actor,
                                  account_id=uuid.UUID(account["id"]))

    assert db.execute(text("SELECT name_ko FROM product WHERE id = :i"),
                      {"i": str(product)}).scalar() == "사람이 정한 이름"
    assert result.conflicts == 1

    # 조용히 무시하지 않는다 — 무엇이 달랐는지 남긴다.
    conflicts = ex.list_conflicts(db, org_id=org)
    assert conflicts[0]["field_name"] == "name_ko"
    assert conflicts[0]["external_value"] == "외부 이름"
    assert conflicts[0]["internal_value"] == "사람이 정한 이름"

    # 어떤 상품인지 없으면 목록만 보고는 아무 판단도 할 수 없다.
    assert conflicts[0]["internal_entity_name"] == "사람이 정한 이름"
    assert conflicts[0]["external_entity_id"] == "123"
    assert conflicts[0]["account_name"]
    # 상품명은 원가가 아니다 — 가리지 않는다.
    assert conflicts[0]["values_masked"] is False


def test_conflict_list_masks_cost_values_for_roles_without_permission(
    db, org, actor, account,
):
    """충돌 행에는 **양쪽 값이 다 들어 있다.**

    원가 필드면 이 목록이 원가 조회 권한을 우회하는 통로가 된다. 프론트에서
    숨기는 것으로는 부족하다 — API 를 직접 부르면 뚫린다.
    """
    product = make_product(db, org_id=org)
    db.commit()
    mapping = ex.upsert_mapping(
        db, org_id=org, actor_user_id=actor,
        external_account_id=uuid.UUID(account["id"]),
        external_entity_type="PRODUCT", external_entity_id="123",
        internal_entity_type="PRODUCT", internal_entity_id=product,
    )
    run_id = db.execute(text("""
        INSERT INTO external_sync_run
               (org_id, external_account_id, sync_type, trigger, status)
        VALUES (:o, :a, 'PRODUCTS', 'MANUAL', 'SUCCESS') RETURNING id
    """), {"o": str(org), "a": account["id"]}).scalar_one()
    db.execute(text("""
        INSERT INTO external_sync_conflict
               (org_id, sync_run_id, mapping_id, field_name,
                external_value, internal_value)
        VALUES (:o, :r, :m, 'cost_amount', '3900', '4200')
    """), {"o": str(org), "r": str(run_id), "m": mapping["id"]})
    db.commit()

    allowed = ex.list_conflicts(db, org_id=org, viewer_role="SOURCING")
    assert allowed[0]["external_value"] == "3900"
    assert allowed[0]["values_masked"] is False

    denied = ex.list_conflicts(db, org_id=org, viewer_role="READONLY")
    # 값은 지우되 **무엇이 충돌했는지는 남긴다** — 목록이 비어 보이면
    # 사용자는 이유를 알 수 없다.
    assert denied[0]["external_value"] is None
    assert denied[0]["internal_value"] is None
    assert denied[0]["field_name"] == "cost_amount"
    assert denied[0]["values_masked"] is True


def test_external_source_cost_never_touches_product_cost(db, org, actor, account):
    """셀러툴이 매입가를 줘도 그것은 관측된 사실일 뿐이다.

    `product.cost_amount` 는 `COST_CHANGE` 승인으로만 바뀐다.
    """
    product = make_product(db, org_id=org)
    supplier = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="1688")
    sc.add_product_source(db, org_id=org, actor_user_id=actor, product_id=product,
                          supplier_site_id=uuid.UUID(supplier["id"]), currency="CNY")
    db.commit()
    ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                      external_account_id=uuid.UUID(account["id"]),
                      external_entity_type="PRODUCT", external_entity_id="123",
                      internal_entity_type="PRODUCT", internal_entity_id=product)

    cost_before = db.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                             {"i": str(product)}).scalar()

    fake = FakeSellerToolConnector([
        conn.ExternalProduct(external_id="123", source_cost=Decimal("100"),
                             source_cost_currency="CNY", observed_at=date(2026, 8, 1)),
    ])
    with use_fake_connector(fake):
        ex.sync_products(db, org_id=org, actor_user_id=actor,
                         account_id=uuid.UUID(account["id"]))

    # 상품 원가는 그대로.
    assert db.execute(text("SELECT cost_amount FROM product WHERE id = :i"),
                      {"i": str(product)}).scalar() == cost_before

    # 소싱 원가 version 으로는 쌓인다.
    primary = sc.primary_source_of(db, org_id=org, product_id=product)
    history = sc.cost_history(db, org_id=org,
                              product_source_id=uuid.UUID(primary["id"]))
    assert len(history) == 1
    assert history[0]["purchase_price"] == 100
    assert history[0]["source_type"] == "SELLER_TOOL"


def test_source_cost_sync_is_skipped_without_a_primary_source(db, org, actor, account):
    """공급처를 추측해서 만들지 않는다."""
    product = make_product(db, org_id=org)
    db.commit()
    ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                      external_account_id=uuid.UUID(account["id"]),
                      external_entity_type="PRODUCT", external_entity_id="123",
                      internal_entity_type="PRODUCT", internal_entity_id=product)

    fake = FakeSellerToolConnector([
        conn.ExternalProduct(external_id="123", source_cost=Decimal("100"),
                             source_cost_currency="CNY"),
    ])
    with use_fake_connector(fake):
        ex.sync_products(db, org_id=org, actor_user_id=actor,
                         account_id=uuid.UUID(account["id"]))

    assert db.execute(text("SELECT COUNT(*) FROM supplier_site WHERE org_id = :o"),
                      {"o": str(org)}).scalar() == 0


def test_one_bad_row_does_not_roll_back_the_rest(db, org, actor, account):
    """상품 1,000건 중 1건 때문에 999건을 되돌리면 운영이 안 된다."""
    good = make_product(db, org_id=org)
    db.commit()
    ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                      external_account_id=uuid.UUID(account["id"]),
                      external_entity_type="PRODUCT", external_entity_id="ok",
                      internal_entity_type="PRODUCT", internal_entity_id=good)
    # 매핑이 가리키는 상품을 지워 고아로 만든다 → 그 행만 실패해야 한다.
    orphan = make_product(db, org_id=org)
    db.commit()
    ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                      external_account_id=uuid.UUID(account["id"]),
                      external_entity_type="PRODUCT", external_entity_id="bad",
                      internal_entity_type="PRODUCT", internal_entity_id=orphan)
    db.execute(text("UPDATE product SET deleted_at = now() WHERE id = :i"),
               {"i": str(orphan)})
    db.commit()

    fake = FakeSellerToolConnector([
        conn.ExternalProduct(external_id="ok", name="정상 반영"),
        conn.ExternalProduct(external_id="bad", source_cost=Decimal("1")),
    ])
    with use_fake_connector(fake):
        result = ex.sync_products(db, org_id=org, actor_user_id=actor,
                                  account_id=uuid.UUID(account["id"]))

    assert result.read == 2
    # 정상 건은 반영됐다.
    assert db.execute(text("SELECT name_ko FROM product WHERE id = :i"),
                      {"i": str(good)}).scalar() == "정상 반영"


def test_fetch_failure_marks_the_run_failed(db, org, actor, account):
    """실패를 성공으로 처리하지 않는다."""
    fake = FakeSellerToolConnector(fail_with=RuntimeError("외부 API 응답 없음"))
    with use_fake_connector(fake), pytest.raises(RuntimeError):
        ex.sync_products(db, org_id=org, actor_user_id=actor,
                         account_id=uuid.UUID(account["id"]))

    run = ex.list_runs(db, org_id=org)[0]
    assert run["status"] == "FAILED"
    assert run["error_code"] == "FETCH_FAILED"


def test_sync_run_records_counts(db, org, actor, account):
    product = make_product(db, org_id=org)
    db.commit()
    ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                      external_account_id=uuid.UUID(account["id"]),
                      external_entity_type="PRODUCT", external_entity_id="123",
                      internal_entity_type="PRODUCT", internal_entity_id=product)

    fake = FakeSellerToolConnector([
        conn.ExternalProduct(external_id="123", name="새 이름"),
        conn.ExternalProduct(external_id="없는것", name="무시됨"),
    ])
    with use_fake_connector(fake):
        ex.sync_products(db, org_id=org, actor_user_id=actor,
                         account_id=uuid.UUID(account["id"]))

    run = ex.list_runs(db, org_id=org)[0]
    assert run["status"] == "SUCCESS"
    assert run["read_count"] == 2
    assert run["updated_count"] == 1
    assert run["skipped_count"] == 1


def test_every_external_write_is_audited(db, org, actor, account):
    product = make_product(db, org_id=org)
    db.commit()
    ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                      external_account_id=uuid.UUID(account["id"]),
                      external_entity_type="PRODUCT", external_entity_id="123",
                      internal_entity_type="PRODUCT", internal_entity_id=product)
    ex.set_account_status(db, org_id=org, actor_user_id=actor,
                          account_id=uuid.UUID(account["id"]), status="DISABLED")

    tables = {r[0] for r in db.execute(text("""
        SELECT DISTINCT domain_table FROM audit_log WHERE org_id = :o
    """), {"o": str(org)})}
    assert {"external_account", "external_entity_mapping"} <= tables


# ──────────────────────────────────────────────────────────────────────────
# 자격증명이 없으면 동기화하지 않는다 (M042)
# ──────────────────────────────────────────────────────────────────────────

def test_sync_without_credential_is_refused_before_calling_out(
    db, org, actor, account_without_credential,
):
    """빈 자격증명으로 호출하면 실패 사유가 "외부 서버 오류" 로 기록된다.

    정작 고쳐야 할 것은 우리 설정이다. 나가기 전에 막는다.
    """
    from app.services.credential_resolver import CredentialNotConfiguredError

    account_id = uuid.UUID(account_without_credential["id"])
    fake = FakeSellerToolConnector([conn.ExternalProduct(external_id="1")])
    with use_fake_connector(fake), pytest.raises(CredentialNotConfiguredError):
        ex.sync_products(db, org_id=org, actor_user_id=actor, account_id=account_id)

    row = db.execute(text("""
        SELECT status, error_code FROM external_sync_run
         WHERE external_account_id = :a ORDER BY started_at DESC LIMIT 1
    """), {"a": str(account_id)}).mappings().one()
    # 성공도 미지원도 아니다 — 우리 설정이 빠진 실패다.
    assert row["status"] == "FAILED"
    assert row["error_code"] == "CREDENTIAL_NOT_CONFIGURED"


def test_connector_receives_the_decrypted_credential(db, org, actor, account):
    """커넥터는 저장소도 세션도 모른다. 쓸 값이 담긴 dict 하나만 받는다."""
    seen: list[dict] = []

    class Capturing(FakeSellerToolConnector):
        def fetch_products(self, *, credentials, since=None):
            seen.append(dict(credentials))
            return []

    with use_fake_connector(Capturing()):
        ex.sync_products(db, org_id=org, actor_user_id=actor,
                         account_id=uuid.UUID(account["id"]))

    assert seen == [{"api_key": "TEST_SECRET"}]


def test_decrypted_credential_is_not_written_anywhere(db, org, actor, account):
    """평문은 프로세스 메모리에만 산다."""
    with use_fake_connector(FakeSellerToolConnector([])):
        ex.sync_products(db, org_id=org, actor_user_id=actor,
                         account_id=uuid.UUID(account["id"]))

    for table in ("external_sync_run", "external_account", "audit_log"):
        dumped = db.execute(text(
            f"SELECT string_agg(t::text, ' ') FROM {table} t WHERE org_id = :o"
        ), {"o": str(org)}).scalar() or ""
        assert "TEST_SECRET" not in dumped, f"{table} 에 평문이 남았다"


def test_fetch_failure_does_not_record_the_external_message(db, org, actor, account):
    """외부 예외 메시지에 인증 헤더나 쿼리 문자열이 섞여 오는 일이 있다."""
    boom = RuntimeError("GET https://api.example.com/v1?api_key=TEST_SECRET 401")
    with use_fake_connector(FakeSellerToolConnector(fail_with=boom)):
        with pytest.raises(RuntimeError):
            ex.sync_products(db, org_id=org, actor_user_id=actor,
                             account_id=uuid.UUID(account["id"]))

    message = db.execute(text("""
        SELECT error_message FROM external_sync_run
         WHERE org_id = :o ORDER BY started_at DESC LIMIT 1
    """), {"o": str(org)}).scalar()
    assert "TEST_SECRET" not in (message or "")
    assert message == "RuntimeError"


def test_broken_secret_reference_is_not_reported_as_unconfigured(
    db, org, actor, account, secret_store,
):
    """계정은 "설정됨" 인데 저장소에 없다 — 운영 사고지 미설정이 아니다.

    미설정으로 뭉개면 사용자가 다시 설정하고, 원인(키 교체 실수·DB 복원)은
    그대로 남는다.
    """
    ref = db.execute(text("""
        SELECT credential_secret_ref FROM external_account WHERE id = :i
    """), {"i": account["id"]}).scalar()
    secret_store.delete(org_id=org, secret_ref=ref)

    with use_fake_connector(FakeSellerToolConnector([])):
        result = ex.test_connection(db, org_id=org, actor_user_id=actor,
                                    account_id=uuid.UUID(account["id"]))
    assert result["status"] == "CREDENTIAL_BROKEN"
    # 여전히 "설정됨" 이라고 말한다 — 참조는 살아 있다. 그게 사고의 내용이다.
    assert result["account"]["credential_configured"] is True
