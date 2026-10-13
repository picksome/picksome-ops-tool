"""정산 레코드의 판매자 계정 귀속 (M033).

왜 필요한가
----------
M032 로 주문은 계정을 갖게 됐는데 정산서는 `marketplace` 문자열뿐이었다.
정산 대사는 "이 주문의 정산금이 얼마인가" 를 맞추는 일이라, 한쪽만 계정을 알면
본점 주문에 2호점 정산금이 붙어도 알아챌 방법이 없다.

**실제 정산 파일이 없으므로**(`BLOCKED_EXTERNAL_DATA`) 여기서는 fixture 로
invariant 만 검증한다. 가짜 정산 데이터를 production 에 만들지 않는다.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.services.integration_account import resolve_account
from app.services.marketplace_settlement import (
    get_marketplace_settlement,
    list_marketplace_settlements,
)
from app.tests.fixtures.factories import (
    make_integration_account,
    make_marketplace_settlement_record,
    make_organization,
)

PERIOD_START = date(2026, 3, 1)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def coupang_accounts(db: Session, org: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    main = make_integration_account(db, org_id=org, provider="COUPANG", account_label="본점")
    second = make_integration_account(db, org_id=org, provider="COUPANG", account_label="2호점")
    return main, second


def _settlement(
    db: Session, org: uuid.UUID, *, order_no: str, account_id: uuid.UUID | None = None,
    marketplace: str = "COUPANG", net: int = 100_000,
) -> uuid.UUID:
    return make_marketplace_settlement_record(
        db, org_id=org, marketplace=marketplace,
        settlement_period_start=PERIOD_START,
        settlement_period_end=PERIOD_START,
        gross_revenue=net,
        marketplace_order_no=order_no,
        integration_account_id=account_id,
    )


# ──────────────────────────────────────────────────────────────────────────
# 조회
# ──────────────────────────────────────────────────────────────────────────

def test_settlement_exposes_its_account(
    db: Session, org: uuid.UUID, coupang_accounts
):
    main, _ = coupang_accounts
    rid = _settlement(db, org, order_no="ST-1", account_id=main)

    out = get_marketplace_settlement(db, org_id=org, record_id=rid)
    assert out.integration_account_id == main
    assert out.integration_account_label == "본점"


def test_legacy_settlement_without_an_account_is_still_listed(
    db: Session, org: uuid.UUID
):
    """NULL 은 '모른다' 이지 '없는 레코드' 가 아니다.

    조회에서 조용히 빠지면 정산 금액이 줄어 보인다 — `_SELECT` 가 LEFT JOIN 인 이유다.
    """
    _settlement(db, org, order_no="ST-LEGACY")

    result = list_marketplace_settlements(db, org_id=org)
    assert result["total"] == 1
    assert result["items"][0].integration_account_id is None
    assert result["items"][0].integration_account_label is None


def test_account_filter_separates_stores_on_the_same_channel(
    db: Session, org: uuid.UUID, coupang_accounts
):
    """이 컬럼이 존재하는 이유 — 본점과 2호점 정산서가 섞이지 않는다."""
    main, second = coupang_accounts
    _settlement(db, org, order_no="ST-A", account_id=main, net=300_000)
    _settlement(db, org, order_no="ST-B", account_id=second, net=100_000)
    db.commit()

    from_main = list_marketplace_settlements(db, org_id=org, integration_account_id=main)
    from_second = list_marketplace_settlements(db, org_id=org, integration_account_id=second)

    assert from_main["total"] == 1
    assert from_second["total"] == 1
    assert from_main["items"][0].marketplace_order_no == "ST-A"
    # 채널 필터로는 둘 다 나온다.
    assert list_marketplace_settlements(db, org_id=org, marketplace="COUPANG")["total"] == 2


def test_settlement_list_does_not_leak_across_orgs(db: Session, org: uuid.UUID):
    other = make_organization(db)
    other_account = make_integration_account(db, org_id=other, provider="COUPANG")
    _settlement(db, other, order_no="ST-OTHER", account_id=other_account, net=9_999_000)
    db.commit()

    assert list_marketplace_settlements(db, org_id=org)["total"] == 0


# ──────────────────────────────────────────────────────────────────────────
# invariant — 주문과 같은 규칙을 쓴다
# ──────────────────────────────────────────────────────────────────────────

def test_cross_org_account_is_rejected_by_the_shared_resolver(
    db: Session, org: uuid.UUID
):
    """정산도 주문과 **같은 resolver** 를 쓴다 — 규칙을 두 벌 만들지 않는다."""
    other = make_organization(db)
    foreign = make_integration_account(db, org_id=other, provider="COUPANG")

    with pytest.raises(ValidationError):
        resolve_account(db, org_id=org, account_id=foreign, marketplace="COUPANG")


def test_marketplace_mismatch_is_rejected_by_the_shared_resolver(
    db: Session, org: uuid.UUID
):
    """쿠팡 정산서를 네이버 계정에 붙일 수 없다 (D12 와 같은 규칙)."""
    naver = make_integration_account(
        db, org_id=org, provider="SMARTSTORE", account_label="네이버",
    )
    with pytest.raises(ValidationError) as err:
        resolve_account(db, org_id=org, account_id=naver, marketplace="COUPANG")
    assert "맞지 않습니다" in str(err.value)


def test_account_without_a_declared_channel_is_not_guessed(
    db: Session, org: uuid.UUID
):
    """`provider` 를 판매처로 취급하지 않는다 (M032 D12 의 교훈).

    `BOT_GENERIC` 같은 값은 판매처가 아니다. 채널을 선언하지 않은 계정은
    비교 대상이 없으므로 통과시킨다 — 없는 근거로 거절하지 않는다.
    """
    generic = make_integration_account(
        db, org_id=org, provider="BOT_GENERIC", account_label="봇",
    )
    # 선언된 채널이 없으므로 어떤 판매처와도 충돌하지 않는다.
    assert resolve_account(
        db, org_id=org, account_id=generic, marketplace="COUPANG"
    ) == generic


def test_db_rejects_a_settlement_pointing_at_a_missing_account(
    db: Session, org: uuid.UUID
):
    """서비스를 우회해도 FK 가 막는다."""
    rid = _settlement(db, org, order_no="ST-FK")
    with pytest.raises(IntegrityError):
        db.execute(text("""
            UPDATE marketplace_settlement_record
               SET integration_account_id = :a WHERE id = :i
        """), {"a": str(uuid.uuid4()), "i": str(rid)})
        db.flush()
    db.rollback()


def test_deleting_a_referenced_account_is_blocked(
    db: Session, org: uuid.UUID, coupang_accounts
):
    """계정 정리 작업에 정산 기록이 딸려 사라지면 안 된다."""
    main, _ = coupang_accounts
    _settlement(db, org, order_no="ST-KEEP", account_id=main)
    db.commit()

    with pytest.raises(IntegrityError):
        db.execute(text("DELETE FROM integration_account WHERE id = :i"), {"i": str(main)})
        db.flush()
    db.rollback()


# ──────────────────────────────────────────────────────────────────────────
# 데이터 품질 검사 (Phase 12)
#
# 계정 미상 자체는 오류가 아니다 — 실제 정산 파일이 없어 모든 레코드가 그 상태다.
# 등급을 올리면 의미 없는 경고만 쌓인다.
# ──────────────────────────────────────────────────────────────────────────

def _codes(db: Session, org: uuid.UUID) -> dict[str, str]:
    from app.services import close_checks

    items = close_checks.run_checks(db, org_id=org, year_month="2026-03")
    return {i.code: i.grade for i in items}


def test_settlement_without_an_account_raises_no_issue(db: Session, org: uuid.UUID):
    """계정 미상은 이슈가 아니다. 실제 정산 파일이 없는 지금은 전부 그 상태다."""
    _settlement(db, org, order_no="Q-NULL")
    codes = _codes(db, org)

    assert "SETTLEMENT_ACCOUNT_ORG_MISMATCH" not in codes
    assert "SETTLEMENT_ACCOUNT_MARKETPLACE_MISMATCH" not in codes


def test_settlement_marketplace_mismatch_is_blocking(db: Session, org: uuid.UUID):
    """이 상태로 확정하면 틀린 대사 결과가 snapshot 에 박제된다."""
    naver = make_integration_account(
        db, org_id=org, provider="SMARTSTORE", account_label="네이버",
    )
    _settlement(db, org, order_no="Q-BAD", account_id=naver, marketplace="COUPANG")

    assert _codes(db, org).get("SETTLEMENT_ACCOUNT_MARKETPLACE_MISMATCH") == "BLOCKING"


def test_settlement_cross_org_account_is_blocking(db: Session, org: uuid.UUID):
    other = make_organization(db)
    foreign = make_integration_account(db, org_id=other, provider="COUPANG")
    _settlement(db, org, order_no="Q-XORG", account_id=foreign)

    assert _codes(db, org).get("SETTLEMENT_ACCOUNT_ORG_MISMATCH") == "BLOCKING"


def test_correctly_bound_settlement_raises_no_issue(
    db: Session, org: uuid.UUID, coupang_accounts
):
    """정상 귀속이 잘못 울리면 사용자가 검증 전체를 무시하게 된다."""
    main, _ = coupang_accounts
    _settlement(db, org, order_no="Q-OK", account_id=main)
    codes = _codes(db, org)

    assert "SETTLEMENT_ACCOUNT_ORG_MISMATCH" not in codes
    assert "SETTLEMENT_ACCOUNT_MARKETPLACE_MISMATCH" not in codes
