"""M022 판매처별 가격 스키마 — DB 제약이 실제로 막는지 검증.

여기서 확인하는 건 "서비스가 검사한다" 가 아니라 **"DB 가 거부한다"** 이다.
서비스 검증만 있으면 동시 요청 두 개가 각자 통과해 둘 다 들어간다. 그러면
"지금 이 상품 가격이 얼마냐" 에 답이 둘이 되고, 마진 계산이 어느 쪽을 쓰는지는
쿼리 순서에 달리게 된다.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

ORG = uuid.UUID("00000000-0000-0000-0000-0000000000ff")
OTHER_ORG = uuid.UUID("00000000-0000-0000-0000-0000000000fe")


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


def _channel(db: Session, code: str) -> uuid.UUID:
    """판매처. 시스템 기본 채널은 `org_id IS NULL` 로 전 조직이 공유한다."""
    row = db.execute(
        text("SELECT id FROM sale_channel "
             " WHERE code = :c AND (org_id = :o OR org_id IS NULL) "
             " ORDER BY org_id NULLS LAST LIMIT 1"),
        {"o": str(ORG), "c": code},
    ).first()
    assert row is not None, f"시스템 판매처 {code} 가 seed 에 없다"
    return uuid.UUID(str(row.id))


def _account(db: Session, label: str, org: uuid.UUID = ORG) -> uuid.UUID:
    """판매자 계정. **같은 판매처에 계정이 여럿**인 상황을 만들기 위해 라벨로 구분한다."""
    aid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO integration_account
               (id, org_id, provider, account_label, status, sale_channel_id, auth_kind)
        VALUES (:i, :o, 'COUPANG', :l, 'ACTIVE', :ch, 'API_KEY')
    """), {"i": str(aid), "o": str(org), "l": label, "ch": str(_channel(db, "COUPANG"))})
    db.commit()
    return aid


def _product(db: Session, org: uuid.UUID = ORG) -> uuid.UUID:
    pid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product (id, org_id, business_key, name_ko, margin_type, status)
        VALUES (:i, :o, :bk, '가격 테스트 상품', 'GENERAL_RESELL', 'ACTIVE')
    """), {"i": str(pid), "o": str(org), "bk": f"PROD-{pid.hex}"})
    db.commit()
    return pid


def _listing(db: Session, account_id: uuid.UUID, product_id: uuid.UUID,
             external: str | None = None, org: uuid.UUID = ORG) -> uuid.UUID:
    lid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product_channel_listing
               (id, org_id, integration_account_id, product_id, external_product_id, status)
        VALUES (:i, :o, :a, :p, :e, 'ACTIVE')
    """), {"i": str(lid), "o": str(org), "a": str(account_id),
           "p": str(product_id), "e": external})
    db.commit()
    return lid


def _price(db: Session, listing_id: uuid.UUID, *, variant_id: uuid.UUID | None = None,
           regular: int | None = 19900, sale: int | None = None,
           sale_start: str | None = None, sale_end: str | None = None,
           frm: str = "2026-01-01", to: str | None = None,
           status: str = "ACTIVE", org: uuid.UUID = ORG) -> uuid.UUID:
    pid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product_channel_price
               (id, org_id, listing_id, product_variant_id, regular_price, sale_price,
                sale_start_at, sale_end_at, effective_from, effective_to, status)
        VALUES (:i, :o, :l, :v, :r, :s, :ss, :se, :f, :t, :st)
    """), {"i": str(pid), "o": str(org), "l": str(listing_id),
           "v": str(variant_id) if variant_id else None,
           "r": regular, "s": sale, "ss": sale_start, "se": sale_end,
           "f": frm, "t": to, "st": status})
    db.commit()
    return pid


@pytest.fixture
def ctx(db: Session):
    yield {"db": db, "product": _product(db)}


# ──────────────────────────────────────────────────────────────
# 1. 복수 스토어
# ──────────────────────────────────────────────────────────────

def test_same_product_can_be_listed_on_two_accounts(ctx):
    """한 판매처에 스토어가 둘이면 리스팅도 둘이다.

    listing 을 marketplace **문자열**에 붙였다면 여기서 UNIQUE 충돌이 났을 것이다.
    """
    db, product = ctx["db"], ctx["product"]
    a1, a2 = _account(db, "본점"), _account(db, "2호점")

    l1 = _listing(db, a1, product, external="COUPANG-1")
    l2 = _listing(db, a2, product, external="COUPANG-2")
    assert l1 != l2

    # 가격도 계정별로 따로 매길 수 있다.
    _price(db, l1, regular=19900)
    _price(db, l2, regular=21900)

    prices = db.execute(text("""
        SELECT regular_price FROM product_channel_price
         WHERE listing_id IN (:a, :b) ORDER BY regular_price
    """), {"a": str(l1), "b": str(l2)}).scalars().all()
    assert prices == [19900, 21900]


def test_same_external_id_allowed_on_different_accounts(ctx):
    """계정이 다르면 같은 외부 상품번호가 있을 수 있다.

    판매처가 발급하는 번호는 **그 스토어 안에서만** 유일하다.
    org 전체로 UNIQUE 를 걸면 다른 스토어의 같은 번호를 넣지 못한다.
    """
    db = ctx["db"]
    a1, a2 = _account(db, "스토어A"), _account(db, "스토어B")
    _listing(db, a1, _product(db), external="SAME-NO-1")
    _listing(db, a2, _product(db), external="SAME-NO-1")  # 통과해야 한다


def test_same_external_id_rejected_within_one_account(ctx):
    db = ctx["db"]
    account = _account(db, "중복확인")
    _listing(db, account, _product(db), external="DUP-1")
    with pytest.raises(Exception) as exc:
        _listing(db, account, _product(db), external="DUP-1")
    assert "ux_pcl_account_external" in str(exc.value)
    db.rollback()


def test_one_product_listed_once_per_account(ctx):
    """같은 스토어에 같은 상품을 두 번 걸 수 없다 — 가격이 갈라진다."""
    db, product = ctx["db"], ctx["product"]
    account = _account(db, "중복상품")
    _listing(db, account, product, external="A-1")
    with pytest.raises(Exception) as exc:
        _listing(db, account, product, external="A-2")
    assert "ux_pcl_account_product" in str(exc.value)
    db.rollback()


def test_external_id_may_be_null_before_registration(ctx):
    """판매처에 올리기 전에는 상품번호가 없다. NULL 이 여럿이어도 된다."""
    db = ctx["db"]
    a1, a2 = _account(db, "미등록1"), _account(db, "미등록2")
    _listing(db, a1, _product(db), external=None)
    _listing(db, a2, _product(db), external=None)


# ──────────────────────────────────────────────────────────────
# 2. 가격 값 유효성 — 미입력은 0 이 아니라 NULL
# ──────────────────────────────────────────────────────────────

def test_null_price_is_allowed(ctx):
    """아직 안 정한 가격은 NULL 이다."""
    db = ctx["db"]
    listing = _listing(db, _account(db, "널가격"), ctx["product"])
    _price(db, listing, regular=None)

    got = db.execute(text("SELECT regular_price FROM product_channel_price WHERE listing_id = :l"),
                     {"l": str(listing)}).scalar()
    assert got is None


def test_zero_price_is_rejected(ctx):
    """0 원은 '공짜로 판다' 지 '미입력' 이 아니다.

    0 을 허용하면 마진 계산이 판매가 0 으로 돌아가 손실이 원가 전액으로 잡힌다.
    """
    db = ctx["db"]
    listing = _listing(db, _account(db, "제로가격"), ctx["product"])
    with pytest.raises(Exception) as exc:
        _price(db, listing, regular=0)
    assert "ck_pcp_regular_positive" in str(exc.value)
    db.rollback()


def test_negative_price_is_rejected(ctx):
    db = ctx["db"]
    listing = _listing(db, _account(db, "음수가격"), ctx["product"])
    with pytest.raises(Exception):
        _price(db, listing, regular=-100)
    db.rollback()


def test_sale_price_above_regular_is_rejected(ctx):
    """할인가가 정상가보다 비쌀 수 없다 — 입력 실수다."""
    db = ctx["db"]
    listing = _listing(db, _account(db, "역전할인"), ctx["product"])
    with pytest.raises(Exception) as exc:
        _price(db, listing, regular=10000, sale=12000, sale_start="2026-01-01T00:00:00Z")
    assert "ck_pcp_sale_not_above_regular" in str(exc.value)
    db.rollback()


def test_sale_price_equal_to_regular_is_allowed(ctx):
    """같은 값은 허용한다 — 할인을 막 끝낸 상태를 표현할 수 있어야 한다."""
    db = ctx["db"]
    listing = _listing(db, _account(db, "동일할인"), ctx["product"])
    _price(db, listing, regular=10000, sale=10000, sale_start="2026-01-01T00:00:00Z")


def test_sale_price_requires_start(ctx):
    """언제부터인지 모르는 할인은 관리할 수 없다."""
    db = ctx["db"]
    listing = _listing(db, _account(db, "기간없음"), ctx["product"])
    with pytest.raises(Exception) as exc:
        _price(db, listing, regular=10000, sale=9000, sale_start=None)
    assert "ck_pcp_sale_window" in str(exc.value)
    db.rollback()


def test_sale_window_without_sale_price_is_rejected(ctx):
    """할인가가 없는데 할인 기간만 있으면 무슨 뜻인지 알 수 없다."""
    db = ctx["db"]
    listing = _listing(db, _account(db, "가격없음"), ctx["product"])
    with pytest.raises(Exception) as exc:
        _price(db, listing, regular=10000, sale=None, sale_start="2026-01-01T00:00:00Z")
    assert "ck_pcp_sale_window" in str(exc.value)
    db.rollback()


def test_sale_end_before_start_is_rejected(ctx):
    db = ctx["db"]
    listing = _listing(db, _account(db, "역순기간"), ctx["product"])
    with pytest.raises(Exception) as exc:
        _price(db, listing, regular=10000, sale=9000,
               sale_start="2026-02-01T00:00:00Z", sale_end="2026-01-01T00:00:00Z")
    assert "ck_pcp_sale_range" in str(exc.value)
    db.rollback()


def test_open_ended_sale_is_allowed(ctx):
    """종료일 없는 상시 할인은 정상이다."""
    db = ctx["db"]
    listing = _listing(db, _account(db, "무기한할인"), ctx["product"])
    _price(db, listing, regular=10000, sale=9000,
           sale_start="2026-01-01T00:00:00Z", sale_end=None)


def test_effective_to_before_from_is_rejected(ctx):
    db = ctx["db"]
    listing = _listing(db, _account(db, "유효기간역순"), ctx["product"])
    with pytest.raises(Exception) as exc:
        _price(db, listing, frm="2026-03-01", to="2026-02-01")
    assert "ck_pcp_effective_range" in str(exc.value)
    db.rollback()


def test_currency_must_be_iso_code(ctx):
    db = ctx["db"]
    listing = _listing(db, _account(db, "통화"), ctx["product"])
    with pytest.raises(Exception) as exc:
        db.execute(text("""
            INSERT INTO product_channel_price
                   (id, org_id, listing_id, currency, regular_price, effective_from)
            VALUES (gen_random_uuid(), :o, :l, 'won', 10000, '2026-01-01')
        """), {"o": str(ORG), "l": str(listing)})
        db.commit()
    assert "ck_pcp_currency" in str(exc.value)
    db.rollback()


# ──────────────────────────────────────────────────────────────
# 3. 기간 중복 — DB 가 막는다
# ──────────────────────────────────────────────────────────────

def test_overlapping_periods_rejected(ctx):
    db = ctx["db"]
    listing = _listing(db, _account(db, "기간중복"), ctx["product"])
    _price(db, listing, frm="2026-01-01", to="2026-03-01")
    with pytest.raises(Exception) as exc:
        _price(db, listing, frm="2026-02-01", to="2026-04-01")
    assert "ex_pcp_no_overlap" in str(exc.value)
    db.rollback()


def test_adjacent_periods_allowed(ctx):
    """`[from, to)` 반개구간이라 끝과 시작이 같은 날이면 겹치지 않는다.

    닫힌 구간으로 만들면 3/1 에 끝나고 3/1 에 시작하는 정상적인 가격 교체가 막힌다.
    """
    db = ctx["db"]
    listing = _listing(db, _account(db, "인접기간"), ctx["product"])
    _price(db, listing, frm="2026-01-01", to="2026-03-01")
    _price(db, listing, frm="2026-03-01", to="2026-05-01")


def test_two_open_ended_periods_rejected(ctx):
    """종료일 없는 가격이 둘이면 '지금 가격' 이 둘이다."""
    db = ctx["db"]
    listing = _listing(db, _account(db, "무한중복"), ctx["product"])
    _price(db, listing, frm="2026-01-01", to=None)
    with pytest.raises(Exception) as exc:
        _price(db, listing, frm="2026-06-01", to=None)
    assert "ex_pcp_no_overlap" in str(exc.value)
    db.rollback()


def test_null_variant_default_price_overlap_rejected(ctx):
    """**핵심.** 상품 기본 가격(variant NULL)끼리도 겹치면 안 된다.

    SQL 에서 `NULL = NULL` 은 참이 아니라, 생성 컬럼으로 접어 넣지 않았다면
    이 케이스는 그냥 통과한다 — 기본 가격이 두 개가 되어도 아무도 못 막는다.
    """
    db = ctx["db"]
    listing = _listing(db, _account(db, "기본가중복"), ctx["product"])
    _price(db, listing, variant_id=None, frm="2026-01-01", to=None)
    with pytest.raises(Exception) as exc:
        _price(db, listing, variant_id=None, frm="2026-02-01", to=None)
    assert "ex_pcp_no_overlap" in str(exc.value)
    db.rollback()


def test_variant_price_does_not_conflict_with_default(ctx):
    """variant 가격과 기본 가격은 서로 다른 범위라 겹쳐도 된다.

    겹치는 게 정상이다 — 기본 가격이 있고 특정 옵션만 다른 가격을 갖는 구조다.
    """
    db = ctx["db"]
    product = ctx["product"]
    listing = _listing(db, _account(db, "옵션가격"), product)

    vid = uuid.uuid4()
    db.execute(text("""
        INSERT INTO product_variant (id, org_id, product_id, variant_key, option_values_json)
        VALUES (:i, :o, :p, '색상=네이비', '{"색상":"네이비"}'::jsonb)
    """), {"i": str(vid), "o": str(ORG), "p": str(product)})
    db.commit()

    _price(db, listing, variant_id=None, frm="2026-01-01", to=None, regular=19900)
    _price(db, listing, variant_id=vid, frm="2026-01-01", to=None, regular=22900)

    rows = db.execute(text("""
        SELECT product_variant_id, regular_price FROM product_channel_price
         WHERE listing_id = :l ORDER BY regular_price
    """), {"l": str(listing)}).all()
    assert len(rows) == 2


def test_superseded_price_does_not_block_new_one(ctx):
    """지난 가격은 충돌 검사 대상이 아니다 — 과거 기록은 지우지 않고 남긴다."""
    db = ctx["db"]
    listing = _listing(db, _account(db, "지난가격"), ctx["product"])
    old = _price(db, listing, frm="2026-01-01", to=None, regular=10000)

    db.execute(text("UPDATE product_channel_price SET status = 'SUPERSEDED' WHERE id = :i"),
               {"i": str(old)})
    db.commit()

    _price(db, listing, frm="2026-01-01", to=None, regular=12000)

    # 과거 가격은 **그대로 남아 있다.** 덮어쓰면 과거 주문 손익을 재현할 수 없다.
    kept = db.execute(text("SELECT regular_price FROM product_channel_price WHERE id = :i"),
                      {"i": str(old)}).scalar()
    assert kept == 10000


def test_different_listings_do_not_conflict(ctx):
    """판매처가 다르면 같은 기간에 다른 가격이 정상이다."""
    db, product = ctx["db"], ctx["product"]
    l1 = _listing(db, _account(db, "채널1"), product)
    l2 = _listing(db, _account(db, "채널2"), _product(db))
    _price(db, l1, frm="2026-01-01", to=None, regular=19900)
    _price(db, l2, frm="2026-01-01", to=None, regular=21900)


# ──────────────────────────────────────────────────────────────
# 4. 조직 격리 / 반영 상태
# ──────────────────────────────────────────────────────────────

def test_publish_status_defaults_to_not_requested(ctx):
    """가격을 만들었다고 판매처에 나가지 않는다. **내부 확정과 외부 반영은 분리된다.**"""
    db = ctx["db"]
    listing = _listing(db, _account(db, "반영기본값"), ctx["product"])
    pid = _price(db, listing)

    row = db.execute(text("""
        SELECT publish_status, published_at, publish_attempt_count
          FROM product_channel_price WHERE id = :i
    """), {"i": str(pid)}).first()
    assert row.publish_status == "NOT_REQUESTED"
    assert row.published_at is None
    assert row.publish_attempt_count == 0


def test_auto_publish_defaults_to_false(ctx):
    """자동 반영이 기본이면 승인 실수가 곧바로 외부에 나간다."""
    db = ctx["db"]
    listing = _listing(db, _account(db, "자동반영기본값"), ctx["product"])
    auto = db.execute(
        text("SELECT auto_publish_price FROM product_channel_listing WHERE id = :i"),
        {"i": str(listing)},
    ).scalar()
    assert auto is False


def test_invalid_publish_status_rejected(ctx):
    db = ctx["db"]
    listing = _listing(db, _account(db, "잘못된반영상태"), ctx["product"])
    pid = _price(db, listing)
    with pytest.raises(Exception) as exc:
        db.execute(text("UPDATE product_channel_price SET publish_status = 'SENT' WHERE id = :i"),
                   {"i": str(pid)})
        db.commit()
    assert "ck_pcp_publish_status" in str(exc.value)
    db.rollback()


def test_updated_at_trigger_applies(ctx):
    """M021 의 clock_timestamp() 트리거가 새 테이블에도 붙어 있다."""
    db = ctx["db"]
    listing = _listing(db, _account(db, "트리거"), ctx["product"])
    pid = _price(db, listing)

    db.execute(text("UPDATE product_channel_price SET regular_price = 11111 WHERE id = :i"),
               {"i": str(pid)})
    first = db.execute(text("SELECT updated_at FROM product_channel_price WHERE id = :i"),
                       {"i": str(pid)}).scalar()
    db.execute(text("UPDATE product_channel_price SET regular_price = 22222 WHERE id = :i"),
               {"i": str(pid)})
    second = db.execute(text("SELECT updated_at FROM product_channel_price WHERE id = :i"),
                        {"i": str(pid)}).scalar()
    db.commit()
    assert second > first
