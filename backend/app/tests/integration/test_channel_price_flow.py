"""판매처 가격 운영 흐름 — listing API · 가격 해결 · 승인 · outbox.

여기서 지키는 것
--------------
· 과거 가격은 **절대** 덮어써지지 않는다 (금액 UPDATE 금지, 기간만 닫는다)
· 승인 전에는 가격이 바뀌지 않는다
· 승인 적용과 outbox 적재는 **한 트랜잭션**이다
· connector 가 없으면 성공을 만들지 않는다
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import (
    ChannelPriceImmutableError,
    ConnectorUnavailableError,
    NotFoundError,
    ValidationError,
)
from app.domain.channel_price import ChannelListingCreate, ChannelListingUpdate, ChannelPriceIn
from app.services import approval as approval_svc
from app.services import channel_connector, channel_price, channel_publish

ORG = uuid.UUID("00000000-0000-0000-0000-0000000000ff")
KST = timezone(timedelta(hours=9))


@pytest.fixture
def db(_test_db_ready: str) -> Iterator[Session]:
    eng = create_engine(_test_db_ready, future=True)
    s = sessionmaker(bind=eng, future=True, expire_on_commit=False)()
    try:
        yield s
    finally:
        s.rollback()
        s.close()
        eng.dispose()


def _user(db: Session, email: str, role: str) -> uuid.UUID:
    row = db.execute(text("SELECT id FROM app_user WHERE org_id = :o AND email = :e"),
                     {"o": str(ORG), "e": email}).first()
    if row:
        return uuid.UUID(str(row.id))
    uid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO app_user (id, org_id, email, password_hash, display_name, role, is_active)
        VALUES (:i, :o, :e, 'x', :n, :r, TRUE)
    """), {"i": str(uid), "o": str(ORG), "e": email, "n": email.split("@")[0], "r": role})
    db.commit()
    return uid


def _account(db: Session, label: str, status: str = "ACTIVE") -> uuid.UUID:
    ch = db.execute(text(
        "SELECT id FROM sale_channel WHERE code = 'COUPANG' "
        " AND (org_id = :o OR org_id IS NULL) ORDER BY org_id NULLS LAST LIMIT 1"
    ), {"o": str(ORG)}).scalar()
    aid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO integration_account
               (id, org_id, provider, account_label, status, sale_channel_id, auth_kind)
        VALUES (:i, :o, 'COUPANG', :l, :st, :ch, 'API_KEY')
    """), {"i": str(aid), "o": str(ORG), "l": label, "st": status, "ch": str(ch)})
    db.commit()
    return aid


def _product(db: Session) -> uuid.UUID:
    pid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product (id, org_id, business_key, name_ko, margin_type, status, cost_amount)
        VALUES (:i, :o, :bk, '가격흐름 상품', 'GENERAL_RESELL', 'ACTIVE', 10000)
    """), {"i": str(pid), "o": str(ORG), "bk": f"PROD-{pid.hex}"})
    db.commit()
    return pid


def _variant(db: Session, product_id: uuid.UUID, value: str) -> uuid.UUID:
    vid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product_variant (id, org_id, product_id, variant_key, option_values_json)
        VALUES (:i, :o, :p, :k, CAST(:j AS jsonb))
    """), {"i": str(vid), "o": str(ORG), "p": str(product_id),
           "k": f"색상={value}", "j": f'{{"색상":"{value}"}}'})
    db.commit()
    return vid


@pytest.fixture
def ctx(db: Session):
    product = _product(db)
    actor = _user(db, "price-op@picksome.local", "OPERATOR")
    approver = _user(db, "price-admin@picksome.local", "ADMIN")
    listing = channel_price.create_listing(
        db, org_id=ORG, actor=actor, product_id=product,
        body=ChannelListingCreate(integration_account_id=_account(db, "가격흐름 스토어"),
                                  external_product_id=f"EXT-{uuid.uuid4().hex}",
                                  status="ACTIVE"),
    )
    yield {"db": db, "product": product, "actor": actor,
           "approver": approver, "listing": listing.id}

    # 자기 데이터를 치운다. 남기면 다른 테스트의 정리(`DELETE FROM approval_request`)와
    # 얽히고, 테스트 DB 에 행이 쌓여 조회가 느려진다.
    db.rollback()
    db.execute(text("DELETE FROM channel_price_publish_outbox WHERE org_id = :o"),
               {"o": str(ORG)})
    db.execute(text("DELETE FROM product_channel_price WHERE org_id = :o"), {"o": str(ORG)})
    db.execute(text("DELETE FROM product_channel_listing WHERE org_id = :o"), {"o": str(ORG)})
    db.commit()


def _price_in(**kw) -> ChannelPriceIn:
    kw.setdefault("effective_from", date(2026, 1, 1))
    kw.setdefault("regular_price", 19900)
    return ChannelPriceIn(**kw)


# ──────────────────────────────────────────────────────────────
# 1. listing API
# ──────────────────────────────────────────────────────────────

def test_listing_created_with_account_metadata(ctx):
    """화면이 계정 UUID 를 보여줄 수는 없다 — 라벨과 판매처를 함께 준다."""
    out = channel_price.get_listing(ctx["db"], org_id=ORG, listing_id=ctx["listing"])
    assert out.account_label == "가격흐름 스토어"
    assert out.marketplace == "COUPANG"
    # connector 가 없으므로 미지원이라고 정직하게 답한다.
    assert out.price_publish_supported is False
    assert out.price_publish_unsupported_reason


def test_inactive_account_cannot_be_linked(ctx):
    db = ctx["db"]
    inactive = _account(db, "정지된 스토어", status="DISABLED")
    with pytest.raises(ValidationError) as exc:
        channel_price.create_listing(
            db, org_id=ORG, actor=ctx["actor"], product_id=_product(db),
            body=ChannelListingCreate(integration_account_id=inactive),
        )
    assert "비활성" in str(exc.value)


def test_other_org_account_is_404(ctx):
    """다른 조직 계정은 403 이 아니라 404 — 403 이면 존재 여부가 새어 나간다."""
    db = ctx["db"]
    with pytest.raises(NotFoundError):
        channel_price.create_listing(
            db, org_id=ORG, actor=ctx["actor"], product_id=_product(db),
            body=ChannelListingCreate(integration_account_id=uuid.uuid4()),
        )


def test_auto_publish_blocked_without_connector(ctx):
    """connector 가 없는데 자동 반영을 켜면 승인할 때마다 실패 작업이 쌓인다."""
    db = ctx["db"]
    with pytest.raises(ConnectorUnavailableError):
        channel_price.create_listing(
            db, org_id=ORG, actor=ctx["actor"], product_id=_product(db),
            body=ChannelListingCreate(integration_account_id=_account(db, "자동반영 시도"),
                                      auto_publish_price=True),
        )


def test_auto_publish_cannot_be_enabled_by_update(ctx):
    """생성에서 막고 수정에서 열어두면 우회된다."""
    with pytest.raises(ConnectorUnavailableError):
        channel_price.update_listing(
            ctx["db"], org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
            body=ChannelListingUpdate(auto_publish_price=True),
        )


def test_listing_deactivate_keeps_row(ctx):
    """비활성화는 삭제가 아니다 — 과거 가격과 주문이 이 리스팅을 가리킨다."""
    out = channel_price.deactivate_listing(
        ctx["db"], org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"])
    assert out.status == "ENDED"
    assert channel_price.get_listing(
        ctx["db"], org_id=ORG, listing_id=ctx["listing"]).id == ctx["listing"]


def test_listing_org_isolation(ctx):
    other = uuid.UUID("00000000-0000-0000-0000-0000000000fe")
    with pytest.raises(NotFoundError):
        channel_price.get_listing(ctx["db"], org_id=other, listing_id=ctx["listing"])


# ──────────────────────────────────────────────────────────────
# 2. 가격 해결 (resolve)
# ──────────────────────────────────────────────────────────────

def test_resolve_returns_none_when_no_price(ctx):
    """가격이 없으면 **0 이 아니라 None** 이다. 0 이면 손실이 원가 전액으로 잡힌다."""
    r = channel_price.resolve_price(ctx["db"], org_id=ORG, listing_id=ctx["listing"])
    assert r.resolved_price is None
    assert r.source_price_id is None


def test_resolve_uses_regular_price_outside_sale(ctx):
    db = ctx["db"]
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=19900))
    r = channel_price.resolve_price(db, org_id=ORG, listing_id=ctx["listing"],
                                    at=date(2026, 6, 1))
    assert r.resolved_price == 19900
    assert r.on_sale is False


def test_resolve_uses_sale_price_inside_window(ctx):
    db = ctx["db"]
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=19900, sale_price=15900,
                          sale_start_at=datetime(2026, 5, 1, tzinfo=KST),
                          sale_end_at=datetime(2026, 7, 1, tzinfo=KST)))
    inside = channel_price.resolve_price(db, org_id=ORG, listing_id=ctx["listing"],
                                         at=date(2026, 6, 1))
    assert inside.resolved_price == 15900
    assert inside.on_sale is True

    after = channel_price.resolve_price(db, org_id=ORG, listing_id=ctx["listing"],
                                        at=date(2026, 8, 1))
    assert after.resolved_price == 19900
    assert after.on_sale is False


def test_resolve_open_ended_sale(ctx):
    """종료일 없는 상시 할인."""
    db = ctx["db"]
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=19900, sale_price=17900,
                          sale_start_at=datetime(2026, 1, 1, tzinfo=KST)))
    r = channel_price.resolve_price(db, org_id=ORG, listing_id=ctx["listing"],
                                    at=date(2030, 1, 1))
    assert r.resolved_price == 17900
    assert r.on_sale is True


def test_variant_price_wins_over_default(ctx):
    """variant 가격은 **완전 판매가**다 — 기본가 + 추가금이 아니다."""
    db = ctx["db"]
    vid = _variant(db, ctx["product"], "네이비")
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=19900))
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=22900, product_variant_id=vid))

    r = channel_price.resolve_price(db, org_id=ORG, listing_id=ctx["listing"],
                                    variant_id=vid, at=date(2026, 6, 1))
    assert r.resolved_price == 22900
    assert r.variant_fallback is False


def test_variant_falls_back_to_default_price(ctx):
    """variant 가격이 없으면 상품 기본 가격을 쓴다. **fallback 이었음을 표시한다.**"""
    db = ctx["db"]
    vid = _variant(db, ctx["product"], "그레이")
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=19900))

    r = channel_price.resolve_price(db, org_id=ORG, listing_id=ctx["listing"],
                                    variant_id=vid, at=date(2026, 6, 1))
    assert r.resolved_price == 19900
    assert r.variant_fallback is True


def test_future_price_not_applied_yet(ctx):
    db = ctx["db"]
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=19900, effective_from=date(2026, 1, 1)))
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=24900, effective_from=date(2026, 9, 1)))

    before = channel_price.resolve_price(db, org_id=ORG, listing_id=ctx["listing"],
                                         at=date(2026, 8, 31))
    after = channel_price.resolve_price(db, org_id=ORG, listing_id=ctx["listing"],
                                        at=date(2026, 9, 1))
    assert before.resolved_price == 19900
    assert after.resolved_price == 24900


# ──────────────────────────────────────────────────────────────
# 3. 가격 버전 — 과거 불변
# ──────────────────────────────────────────────────────────────

def test_new_version_closes_previous_without_changing_amount(ctx):
    """**핵심.** 새 가격은 기존 가격의 기간만 닫는다. 금액은 그대로 남는다."""
    db = ctx["db"]
    old = channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=19900, effective_from=date(2026, 1, 1)))
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=24900, effective_from=date(2026, 9, 1)))

    row = db.execute(text("""
        SELECT regular_price, effective_to, status FROM product_channel_price WHERE id = :i
    """), {"i": str(old.id)}).first()
    assert row.regular_price == 19900, "과거 가격 금액이 덮어써졌다"
    assert row.effective_to == date(2026, 9, 1)
    # **status 는 ACTIVE 로 남는다.** 종료일이 생겼다고 무효해진 게 아니라,
    # 새 가격이 시작하기 전까지는 여전히 지금 팔리는 가격이다.
    assert row.status == "ACTIVE"

    # 새 가격 시작 전에는 옛 가격이, 시작 후에는 새 가격이 나온다.
    assert channel_price.resolve_price(
        db, org_id=ORG, listing_id=ctx["listing"], at=date(2026, 8, 31)
    ).resolved_price == 19900


def test_cannot_create_price_starting_before_existing(ctx):
    """과거를 덮어쓰는 시작일은 막는다. 정정은 더 뒤 날짜의 새 버전이다."""
    db = ctx["db"]
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(effective_from=date(2026, 6, 1)))
    with pytest.raises(ChannelPriceImmutableError):
        channel_price.create_price_version(
            db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
            payload=_price_in(effective_from=date(2026, 3, 1)))


def test_same_start_date_is_rejected(ctx):
    db = ctx["db"]
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(effective_from=date(2026, 6, 1)))
    with pytest.raises(ChannelPriceImmutableError):
        channel_price.create_price_version(
            db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
            payload=_price_in(effective_from=date(2026, 6, 1)))


def test_new_price_inside_existing_period_closes_it_early(ctx):
    """기존 기간 **안에서** 새 가격이 시작하면 앞 가격을 그 날짜에 끊는다.

    기간 중 가격 인상은 정상적인 운영이다. 이걸 오류로 막으면 사용자가
    기존 가격을 먼저 지워야 하고, 그러면 과거 이력이 사라진다.
    """
    db = ctx["db"]
    old = channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=19900,
                          effective_from=date(2026, 1, 1), effective_to=date(2026, 3, 1)))
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=21900,
                          effective_from=date(2026, 2, 1), effective_to=date(2026, 4, 1)))

    row = db.execute(text("""
        SELECT regular_price, effective_to FROM product_channel_price WHERE id = :i
    """), {"i": str(old.id)}).first()
    assert row.regular_price == 19900, "앞 가격 금액이 바뀌었다"
    assert row.effective_to == date(2026, 2, 1)

    # 경계 전후로 각각 다른 가격이 나온다.
    assert channel_price.resolve_price(
        db, org_id=ORG, listing_id=ctx["listing"], at=date(2026, 1, 15)).resolved_price == 19900
    assert channel_price.resolve_price(
        db, org_id=ORG, listing_id=ctx["listing"], at=date(2026, 2, 15)).resolved_price == 21900


def test_direct_overlapping_insert_is_blocked_by_db(ctx):
    """서비스를 우회해 직접 넣어도 DB exclusion 이 막는다.

    서비스 검증만으로는 동시 요청 두 개가 각자 통과해 둘 다 들어간다.
    """
    db = ctx["db"]
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(effective_from=date(2026, 1, 1), effective_to=date(2026, 3, 1)))

    with pytest.raises(Exception) as exc:
        db.execute(text("""
            INSERT INTO product_channel_price
                   (id, org_id, listing_id, regular_price, effective_from, effective_to, status)
            VALUES (gen_random_uuid(), :o, :l, 21900, '2026-02-01', '2026-04-01', 'ACTIVE')
        """), {"o": str(ORG), "l": str(ctx["listing"])})
        db.commit()
    assert "ex_pcp_no_overlap" in str(exc.value)
    db.rollback()


def test_variant_from_another_product_rejected(ctx):
    db = ctx["db"]
    stranger = _variant(db, _product(db), "남의옵션")
    with pytest.raises(ValidationError):
        channel_price.create_price_version(
            db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
            payload=_price_in(product_variant_id=stranger))


def test_zero_price_rejected_by_domain(ctx):
    """미입력은 NULL 이고 0 은 '공짜로 판다' 다."""
    with pytest.raises(Exception):
        _price_in(regular_price=0)


def test_sale_above_regular_rejected_by_domain(ctx):
    with pytest.raises(Exception) as exc:
        _price_in(regular_price=10000, sale_price=12000,
                  sale_start_at=datetime(2026, 1, 1, tzinfo=KST))
    assert "할인가" in str(exc.value)


def test_sale_window_outside_effective_period_rejected(ctx):
    """할인 기간이 가격 유효기간을 벗어나면 어느 버전의 할인인지 알 수 없다."""
    with pytest.raises(Exception) as exc:
        _price_in(effective_from=date(2026, 6, 1), effective_to=date(2026, 8, 1),
                  regular_price=19900, sale_price=15900,
                  sale_start_at=datetime(2026, 5, 1, tzinfo=KST))
    assert "할인 시작일" in str(exc.value)


# ──────────────────────────────────────────────────────────────
# 4. CHANNEL_PRICE_CHANGE 승인
# ──────────────────────────────────────────────────────────────

def _create_request(ctx, **payload):
    return approval_svc.create_request(
        ctx["db"], org_id=ORG, actor_user_id=ctx["actor"],
        target_type="product_channel_listing", target_id=ctx["listing"],
        request_type="CHANNEL_PRICE_CHANGE",
        proposed_changes=payload, reason="E2E 가격 변경", submit=True,
    )


def test_price_unchanged_until_approval_applied(ctx):
    """**승인 전에는 가격이 바뀌지 않는다.** 이게 승인 워크플로의 존재 이유다."""
    db = ctx["db"]
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=19900))

    _create_request(ctx, regular_price=24900, effective_from="2026-09-01")

    still = channel_price.resolve_price(db, org_id=ORG, listing_id=ctx["listing"],
                                        at=date(2026, 9, 1))
    assert still.resolved_price == 19900


def test_approval_apply_creates_new_price_version(ctx):
    db = ctx["db"]
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=19900))

    req = _create_request(ctx, regular_price=24900, effective_from="2026-09-01")
    rid = uuid.UUID(str(req["id"]))
    approval_svc.approve(db, org_id=ORG, request_id=rid,
                         actor_user_id=ctx["approver"], actor_role="ADMIN")
    approval_svc.apply(db, org_id=ORG, request_id=rid,
                       actor_user_id=ctx["approver"], actor_role="ADMIN")

    applied = channel_price.resolve_price(db, org_id=ORG, listing_id=ctx["listing"],
                                          at=date(2026, 9, 1))
    assert applied.resolved_price == 24900

    # 승인 요청과 가격 버전이 연결돼 있어야 이력에서 "왜 바뀌었나" 를 알 수 있다.
    linked = db.execute(text("""
        SELECT approval_request_id FROM product_channel_price WHERE id = :i
    """), {"i": str(applied.source_price_id)}).scalar()
    assert str(linked) == str(rid)


def test_past_price_survives_approval_apply(ctx):
    db = ctx["db"]
    old = channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=19900))

    req = _create_request(ctx, regular_price=24900, effective_from="2026-09-01")
    rid = uuid.UUID(str(req["id"]))
    approval_svc.approve(db, org_id=ORG, request_id=rid,
                         actor_user_id=ctx["approver"], actor_role="ADMIN")
    approval_svc.apply(db, org_id=ORG, request_id=rid,
                       actor_user_id=ctx["approver"], actor_role="ADMIN")

    kept = db.execute(text("SELECT regular_price FROM product_channel_price WHERE id = :i"),
                      {"i": str(old.id)}).scalar()
    assert kept == 19900


def test_invalid_price_payload_rejected_at_request_time(ctx):
    """형식 오류를 **승인 전에** 잡는다. 적용 시점에 터지면 승인이 이미 끝난 뒤다."""
    with pytest.raises(ValidationError):
        _create_request(ctx, regular_price=0, effective_from="2026-09-01")


def test_field_not_owned_by_request_type_rejected(ctx):
    with pytest.raises(ValidationError):
        _create_request(ctx, cost_amount=5000, effective_from="2026-09-01")


def test_price_fields_are_sensitive(ctx):
    """가격은 원가와 함께 마진을 결정한다 — 가리지 않으면 역산된다."""
    from app.core.sensitive import build_redacted_diff, is_sensitive

    assert is_sensitive("product_channel_listing", "regular_price")
    assert is_sensitive("product_channel_listing", "sale_price")

    rows = build_redacted_diff(
        "product_channel_listing",
        {"regular_price": 19900}, {"regular_price": 24900}, allowed=False)
    row = next(r for r in rows if r["field"] == "regular_price")
    assert row["redacted"] is True
    assert row["before"] is None and row["after"] is None
    # 키는 남는다 — 지우면 "변경 없음" 과 "볼 수 없음" 이 구분되지 않는다.
    assert row["label"] == "정상가"


def test_effective_date_comes_from_price_payload(ctx):
    """월 잠금 판정은 **가격이 적용되는 달**로 해야 맞다."""
    from app.services.approval_targets import get_adapter

    adapter = get_adapter("product_channel_listing")
    got = adapter.get_effective_date(
        ctx["db"], org_id=ORG, target_id=ctx["listing"],
        proposed={"effective_from": "2026-09-01"}, effective_from=date(2026, 8, 1),
    )
    assert got == date(2026, 9, 1)


# ──────────────────────────────────────────────────────────────
# 5. outbox
# ──────────────────────────────────────────────────────────────

def _jobs(db: Session, price_id: uuid.UUID):
    return db.execute(text("""
        SELECT status, attempt_count, last_error_code, dedupe_key
          FROM channel_price_publish_outbox WHERE channel_price_id = :i
    """), {"i": str(price_id)}).mappings().all()


def test_auto_publish_false_creates_no_job(ctx):
    """기본은 수동이다. 가격을 만들었다고 판매처에 나가지 않는다."""
    db = ctx["db"]
    req = _create_request(ctx, regular_price=24900, effective_from="2026-09-01")
    rid = uuid.UUID(str(req["id"]))
    approval_svc.approve(db, org_id=ORG, request_id=rid,
                         actor_user_id=ctx["approver"], actor_role="ADMIN")
    approval_svc.apply(db, org_id=ORG, request_id=rid,
                       actor_user_id=ctx["approver"], actor_role="ADMIN")

    resolved = channel_price.resolve_price(db, org_id=ORG, listing_id=ctx["listing"],
                                           at=date(2026, 9, 1))
    assert _jobs(db, resolved.source_price_id) == []
    status = db.execute(text("SELECT publish_status FROM product_channel_price WHERE id = :i"),
                        {"i": str(resolved.source_price_id)}).scalar()
    assert status == "NOT_REQUESTED"


def test_manual_publish_blocked_without_connector(ctx):
    """connector 가 없으면 **큐에 넣지 않고** 거절한다. 넣으면 '처리 중' 처럼 보인다."""
    db = ctx["db"]
    price = channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"], payload=_price_in())

    with pytest.raises(ConnectorUnavailableError):
        channel_publish.request_publish(db, org_id=ORG, price_id=price.id, actor=ctx["actor"])
    assert _jobs(db, price.id) == []


def test_publish_enqueue_and_runner_reports_unavailable(ctx, monkeypatch):
    """connector 가 지원한다고 가정하면 큐에 들어가고, runner 가 실제 상태를 남긴다.

    **성공을 만들지 않는다.** 전송 구현이 없으므로 runner 는 정직한 실패 상태로 끝낸다.
    """
    db = ctx["db"]
    price = channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"], payload=_price_in())

    # 지원한다고 가정 → 큐에 들어간다
    monkeypatch.setattr(channel_connector, "assert_publish_supported", lambda m: None)
    job_id = channel_publish.request_publish(
        db, org_id=ORG, price_id=price.id, actor=ctx["actor"])
    assert job_id is not None
    assert _jobs(db, price.id)[0]["status"] == "QUEUED"
    assert db.execute(text("SELECT publish_status FROM product_channel_price WHERE id = :i"),
                      {"i": str(price.id)}).scalar() == "QUEUED"

    # runner 는 실제 레지스트리를 본다 → 미연결로 끝난다
    monkeypatch.undo()
    result = channel_publish.run_publish_batch(db, org_id=ORG, batch_size=10)
    assert result.claimed >= 1
    assert result.connector_unavailable >= 1
    assert result.published == 0, "connector 가 없는데 성공이 나왔다"

    job = _jobs(db, price.id)[0]
    assert job["status"] == "CONNECTOR_UNAVAILABLE"
    # 미연결은 재시도해도 소용없다 — 시도 횟수를 올리지 않는다.
    assert job["attempt_count"] == 0


def test_duplicate_publish_job_blocked(ctx, monkeypatch):
    """같은 가격 버전의 활성 작업은 하나만."""
    db = ctx["db"]
    price = channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"], payload=_price_in())
    monkeypatch.setattr(channel_connector, "assert_publish_supported", lambda m: None)

    first = channel_publish.request_publish(db, org_id=ORG, price_id=price.id,
                                            actor=ctx["actor"])
    second = channel_publish.request_publish(db, org_id=ORG, price_id=price.id,
                                             actor=ctx["actor"])
    assert first is not None
    # 중복은 조용히 무시한다 — 오류로 만들면 버튼 두 번 눌렀다고 실패가 된다.
    assert second is None
    assert len(_jobs(db, price.id)) == 1


def test_stale_lease_is_reclaimed(ctx, monkeypatch):
    """워커가 죽으면 lease 만료 후 다른 워커가 회수한다."""
    db = ctx["db"]
    price = channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"], payload=_price_in())
    monkeypatch.setattr(channel_connector, "assert_publish_supported", lambda m: None)
    channel_publish.request_publish(db, org_id=ORG, price_id=price.id, actor=ctx["actor"])

    # 죽은 워커가 잡은 상태를 만든다.
    db.execute(text("""
        UPDATE channel_price_publish_outbox
           SET status = 'PROCESSING', locked_at = now() - interval '1 hour',
               lease_expires_at = now() - interval '30 minutes', locked_by = 'dead-worker'
         WHERE channel_price_id = :i
    """), {"i": str(price.id)})
    db.commit()

    monkeypatch.undo()
    result = channel_publish.run_publish_batch(db, org_id=ORG, batch_size=10)
    assert result.reclaimed_stale >= 1


def test_publish_job_org_isolation(ctx, monkeypatch):
    db = ctx["db"]
    price = channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"], payload=_price_in())
    monkeypatch.setattr(channel_connector, "assert_publish_supported", lambda m: None)
    channel_publish.request_publish(db, org_id=ORG, price_id=price.id, actor=ctx["actor"])
    monkeypatch.undo()

    other = uuid.UUID("00000000-0000-0000-0000-0000000000fe")
    result = channel_publish.run_publish_batch(db, org_id=other, batch_size=10)
    assert result.claimed == 0


def test_connector_registry_reports_all_unsupported(ctx):
    """지금은 **전부 미연결**이다. 하나라도 True 면 성공 mock 이 섞인 것이다."""
    for entry in channel_connector.all_support():
        assert entry["price_publish_supported"] is False
        assert entry["reason"]


def test_unknown_marketplace_defaults_to_unsupported(ctx):
    """모르는 판매처를 '지원함' 으로 두면 조용히 전송을 시도하고 실패가 쌓인다."""
    assert channel_connector.support_for("NEW_MARKET").price_publish_supported is False
    assert channel_connector.support_for(None).price_publish_supported is False


# ──────────────────────────────────────────────────────────────
# 6. 마진 영향 미리보기
# ──────────────────────────────────────────────────────────────

def _preview(ctx, role="OPERATOR", **payload):
    from app.services.channel_price_preview import preview_price_change

    payload.setdefault("effective_from", date(2026, 9, 1))
    return preview_price_change(
        ctx["db"], org_id=ORG, listing_id=ctx["listing"],
        payload=ChannelPriceIn(**payload), role=role, at=date(2026, 9, 1),
    )


def test_preview_compares_before_and_after(ctx):
    """현재가와 제안가를 **같은 엔진**으로 계산해 비교한다."""
    db = ctx["db"]
    channel_price.create_price_version(
        db, org_id=ORG, actor=ctx["actor"], listing_id=ctx["listing"],
        payload=_price_in(regular_price=19900, effective_from=date(2026, 1, 1)))

    out = _preview(ctx, regular_price=24900)
    assert out["before"]["sale_price"] == 19900
    assert out["after"]["sale_price"] == 24900
    # 가격이 오르면 순이익도 오른다 — 엔진이 실제로 돌았다는 증거.
    assert out["after"]["expected_net_profit"] > out["before"]["expected_net_profit"]


def test_preview_without_current_price_has_no_before(ctx):
    """현재 가격이 없으면 before 는 None 이다. **0 으로 만들지 않는다.**"""
    out = _preview(ctx, regular_price=24900)
    assert out["before"] is None
    assert out["after"] is not None


def test_preview_uses_sale_price_when_in_window(ctx):
    """할인 기간이 기준일을 포함하면 할인가로 계산한다."""
    out = _preview(
        ctx, regular_price=24900, sale_price=19900,
        sale_start_at=datetime(2026, 9, 1, tzinfo=KST),
        sale_end_at=datetime(2026, 10, 1, tzinfo=KST),
    )
    assert out["after"]["sale_price"] == 19900


def test_preview_without_cost_says_so(ctx):
    """원가를 모르면 **0 으로 가정하지 않고** 계산 불가를 알린다.

    0 으로 두면 마진이 부풀어 사용자가 실제보다 좋은 숫자를 보고 가격을 정한다.
    """
    db = ctx["db"]
    db.execute(text("UPDATE product SET cost_amount = NULL WHERE id = :i"),
               {"i": str(ctx["product"])})
    db.commit()

    out = _preview(ctx, regular_price=24900)
    assert out["after"]["unavailable_reason"]
    assert "원가" in out["after"]["unavailable_reason"]


def test_preview_redacts_for_unauthorized_role(ctx):
    """권한이 없으면 **원가와 역산 가능한 값을 전부** 지운다.

    마진율만 남겨도 판매가를 알면 원가가 나온다.
    """
    out = _preview(ctx, role="READONLY", regular_price=24900)
    assert out["sensitive_visible"] is False
    after = out["after"]
    for key in ("cost_amount", "expected_net_profit", "margin_rate",
                "marketplace_fee", "vat"):
        assert after[key] is None, f"{key} 가 남아 있다"
    # 판매가 자체는 사용자가 입력한 값이라 가리지 않는다.
    assert after["sale_price"] == 24900
    assert out["margin_gap_pp"] is None
    assert out["redacted_message"]


def test_preview_shows_values_for_authorized_role(ctx):
    out = _preview(ctx, role="ADMIN", regular_price=24900)
    assert out["sensitive_visible"] is True
    assert out["after"]["cost_amount"] is not None
    assert out["after"]["margin_rate"] is not None


def test_preview_reports_policy_verification(ctx):
    """정책이 검증되지 않았으면 화면이 '예상값' 이라고 말할 근거를 준다."""
    out = _preview(ctx, regular_price=24900)
    assert "policies_verified" in out
    assert isinstance(out["policy_warnings"], list)


def test_preview_includes_computed_at(ctx):
    """승인 snapshot 에 저장한다면 계산 시점이 함께 가야 재현된다."""
    out = _preview(ctx, regular_price=24900)
    assert out["computed_at"] == "2026-09-01"
    assert out["marketplace"] == "COUPANG"
