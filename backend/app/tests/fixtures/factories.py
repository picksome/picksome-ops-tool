"""통합 테스트용 DB row factory (raw SQL — ORM 모델 미사용, M001 스키마 기준).

DB 필요한 서비스(policy/settlement/margin/price_sim/expense) 통합 테스트에서 공용으로 사용.
각 함수는 새 row 의 UUID 를 반환. 커밋은 호출자(conftest.db fixture)가 rollback 으로 정리.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.ids import new_id

DEFAULT_EFFECTIVE_FROM = date(2026, 1, 1)


def _jsonb(value: Any) -> str | None:
    return json.dumps(value, default=str) if value is not None else None


def make_organization(
    db: Session,
    *,
    business_type: str = "JOINT_BUSINESS",
    settlement_reopen_limit: int = 3,
) -> UUID:
    oid = new_id()
    db.execute(
        text(
            """
            INSERT INTO organization (id, business_name, business_type, settlement_reopen_limit)
            VALUES (:id, :name, CAST(:bt AS enum_business_type), :limit)
            """
        ),
        {"id": str(oid), "name": f"test-org-{oid}", "bt": business_type, "limit": settlement_reopen_limit},
    )
    return oid


def make_app_user(db: Session, *, org_id: UUID, role: str = "ADMIN", email: str | None = None) -> UUID:
    uid = new_id()
    db.execute(
        text(
            """
            INSERT INTO app_user (id, org_id, email, password_hash, display_name, role)
            VALUES (:id, :org, :email, 'argon2:test', 'Test User', CAST(:role AS enum_user_role))
            """
        ),
        {"id": str(uid), "org": str(org_id), "email": email or f"{uid}@test.local", "role": role},
    )
    return uid


def make_partner(db: Session, *, org_id: UUID, name: str) -> UUID:
    pid = new_id()
    db.execute(
        text("INSERT INTO partner (id, org_id, name) VALUES (:id, :org, :name)"),
        {"id": str(pid), "org": str(org_id), "name": name},
    )
    return pid


def make_tax_policy(
    db: Session,
    *,
    org_id: UUID,
    vat_rate: Decimal = Decimal("0.1000"),
    tax_type: str = "GENERAL",
    business_type: str | None = None,
    applies_to: str = "전사",
    effective_from: date = DEFAULT_EFFECTIVE_FROM,
    effective_to: date | None = None,
    version: int = 1,
) -> UUID:
    tid = new_id()
    db.execute(
        text(
            """
            INSERT INTO tax_policy (
              id, org_id, version, effective_from, effective_to,
              tax_type, business_type, vat_rate, applies_to
            ) VALUES (
              :id, :org, :ver, :eff_from, :eff_to,
              CAST(:tt AS enum_tax_type), CAST(:bt AS enum_business_type), :rate, :applies
            )
            """
        ),
        {
            "id": str(tid), "org": str(org_id), "ver": version,
            "eff_from": effective_from, "eff_to": effective_to,
            "tt": tax_type, "bt": business_type, "rate": vat_rate, "applies": applies_to,
        },
    )
    return tid


def make_marketplace_fee_policy(
    db: Session,
    *,
    org_id: UUID,
    marketplace: str = "COUPANG",
    fee_type: str = "RATE",
    rate: Decimal | None = Decimal("0.1080"),
    fixed_amount: int | None = None,
    tier_json: list[dict[str, Any]] | None = None,
    category_path: str | None = None,
    reliability_status: str = "CONFIRMED",
    effective_from: date = DEFAULT_EFFECTIVE_FROM,
    effective_to: date | None = None,
    version: int = 1,
) -> UUID:
    mid = new_id()
    db.execute(
        text(
            """
            INSERT INTO marketplace_fee_policy (
              id, org_id, version, effective_from, effective_to, reliability_status,
              marketplace, category_path, fee_type, rate, fixed_amount, tier_json
            ) VALUES (
              :id, :org, :ver, :eff_from, :eff_to, CAST(:rel AS enum_policy_reliability),
              :mkt, :cat, CAST(:ftype AS enum_marketplace_fee_type),
              :rate, :fixed, CAST(:tiers AS JSONB)
            )
            """
        ),
        {
            "id": str(mid), "org": str(org_id), "ver": version,
            "eff_from": effective_from, "eff_to": effective_to, "rel": reliability_status,
            "mkt": marketplace, "cat": category_path, "ftype": fee_type,
            "rate": rate, "fixed": fixed_amount, "tiers": _jsonb(tier_json),
        },
    )
    return mid


def make_payment_fee_policy(
    db: Session,
    *,
    org_id: UUID,
    method: str = "BUSINESS_CARD",
    rate: Decimal = Decimal("0.0250"),
    effective_from: date = DEFAULT_EFFECTIVE_FROM,
    effective_to: date | None = None,
    version: int = 1,
) -> UUID:
    pid = new_id()
    db.execute(
        text(
            """
            INSERT INTO payment_fee_policy (id, org_id, version, effective_from, effective_to, method, rate)
            VALUES (:id, :org, :ver, :eff_from, :eff_to, CAST(:method AS enum_payment_method), :rate)
            """
        ),
        {
            "id": str(pid), "org": str(org_id), "ver": version,
            "eff_from": effective_from, "eff_to": effective_to, "method": method, "rate": rate,
        },
    )
    return pid


def make_return_risk_policy(
    db: Session,
    *,
    org_id: UUID,
    return_rate: Decimal = Decimal("0.0300"),
    marketplace: str | None = "COUPANG",
    category_path: str | None = None,
    effective_from: date = DEFAULT_EFFECTIVE_FROM,
    effective_to: date | None = None,
    version: int = 1,
) -> UUID:
    rid = new_id()
    db.execute(
        text(
            """
            INSERT INTO return_risk_policy (
              id, org_id, version, effective_from, effective_to, marketplace, category_path, return_rate
            ) VALUES (
              :id, :org, :ver, :eff_from, :eff_to, :mkt, :cat, :rate
            )
            """
        ),
        {
            "id": str(rid), "org": str(org_id), "ver": version,
            "eff_from": effective_from, "eff_to": effective_to,
            "mkt": marketplace, "cat": category_path, "rate": return_rate,
        },
    )
    return rid


def make_settlement_policy(
    db: Session,
    *,
    org_id: UUID,
    loss_policy: str = "CARRYOVER",
    priority_distribution: list[dict[str, Any]] | None = None,
    close_day: int = 5,
    effective_from: date = DEFAULT_EFFECTIVE_FROM,
    effective_to: date | None = None,
    version: int = 1,
) -> UUID:
    sid = new_id()
    db.execute(
        text(
            """
            INSERT INTO settlement_policy (
              id, org_id, version, effective_from, effective_to,
              loss_policy, priority_distribution_json, close_day
            ) VALUES (
              :id, :org, :ver, :eff_from, :eff_to, :loss, CAST(:pri AS JSONB), :close_day
            )
            """
        ),
        {
            "id": str(sid), "org": str(org_id), "ver": version,
            "eff_from": effective_from, "eff_to": effective_to,
            "loss": loss_policy, "pri": _jsonb(priority_distribution or []), "close_day": close_day,
        },
    )
    return sid


def make_partner_share_policy(
    db: Session,
    *,
    org_id: UUID,
    partner_id: UUID,
    share_ratio: Decimal,
    applies_year_month: str | None = None,
    effective_from: date = DEFAULT_EFFECTIVE_FROM,
    effective_to: date | None = None,
    version: int = 1,
) -> UUID:
    pid = new_id()
    db.execute(
        text(
            """
            INSERT INTO partner_share_policy (
              id, org_id, version, effective_from, effective_to,
              partner_id, share_ratio, applies_year_month
            ) VALUES (
              :id, :org, :ver, :eff_from, :eff_to, :partner, :ratio, :ym
            )
            """
        ),
        {
            "id": str(pid), "org": str(org_id), "ver": version,
            "eff_from": effective_from, "eff_to": effective_to,
            "partner": str(partner_id), "ratio": share_ratio, "ym": applies_year_month,
        },
    )
    return pid


def make_expense_category_policy(
    db: Session,
    *,
    org_id: UUID,
    name: str = "원가/상품매입",
    kind: str = "COGS",
    effective_from: date = DEFAULT_EFFECTIVE_FROM,
    version: int = 1,
) -> UUID:
    cid = new_id()
    db.execute(
        text(
            """
            INSERT INTO expense_category_policy (id, org_id, version, effective_from, name, kind)
            VALUES (:id, :org, :ver, :eff_from, :name, CAST(:kind AS enum_expense_category_kind))
            """
        ),
        {"id": str(cid), "org": str(org_id), "ver": version, "eff_from": effective_from, "name": name, "kind": kind},
    )
    return cid


def make_product(
    db: Session,
    *,
    org_id: UUID,
    margin_type: str = "GENERAL_RESELL",
    name_ko: str = "테스트상품",
    cost_amount: int | None = 10_000,
    business_key: str | None = None,
) -> UUID:
    pid = new_id()
    db.execute(
        text(
            """
            INSERT INTO product (id, org_id, business_key, name_ko, margin_type, cost_amount)
            VALUES (:id, :org, :bkey, :name, CAST(:mt AS enum_margin_type), :cost)
            """
        ),
        {
            "id": str(pid), "org": str(org_id), "bkey": business_key or f"P-{pid}",
            "name": name_ko, "mt": margin_type, "cost": cost_amount,
        },
    )
    return pid


def make_integration_account(
    db: Session,
    *,
    org_id: UUID,
    provider: str = "COUPANG",
    account_label: str = "본점",
    status: str = "ACTIVE",
    sale_channel_code: str | None = None,
) -> UUID:
    """판매자 계정 1개.

    같은 `provider` 로 여러 개 만들 수 있다 — 멀티 스토어(본점/2호점)를 구분하는
    것이 이 테이블의 존재 이유다. `account_label` 을 다르게 주면 된다.

    `sale_channel_id` 를 함께 채운다 (`create_bot_account` 와 같은 방식).
    이게 있어야 "쿠팡 주문을 네이버 계정에 붙일 수 없다" 는 검증이 걸린다 —
    채널을 선언하지 않은 계정은 비교할 대상이 없어 통과한다.

    `sale_channel_code` 를 주지 않으면 `provider` 를 코드로 보고 찾는다.
    `BOT_GENERIC` 처럼 채널이 아닌 provider 는 못 찾고 NULL 로 남는다 —
    그것도 실제로 있는 상태이므로 그대로 둔다.
    """
    aid = new_id()
    channel_row = db.execute(
        text(
            """
            SELECT id FROM sale_channel
             WHERE (org_id = :org OR org_id IS NULL) AND code = :code
               AND deleted_at IS NULL
             LIMIT 1
            """
        ),
        {"org": str(org_id), "code": sale_channel_code or provider},
    ).first()
    db.execute(
        text(
            """
            INSERT INTO integration_account
                   (id, org_id, provider, account_label, status, auth_kind,
                    sale_channel_id)
            VALUES (:id, :org, :provider, :label, :status, 'API_KEY', :scid)
            """
        ),
        {
            "id": str(aid), "org": str(org_id), "provider": provider,
            "label": account_label, "status": status,
            "scid": str(channel_row.id) if channel_row else None,
        },
    )
    return aid


def make_sales_order(
    db: Session,
    *,
    org_id: UUID,
    product_id: UUID | None,
    sale_date: date,
    sale_price: int,
    quantity: int = 1,
    marketplace: str = "COUPANG",
    ad_cost: int = 0,
    business_key: str | None = None,
    marketplace_order_no: str | None = None,
    product_variant_id: UUID | None = None,
    # 기본값은 0 이 아니라 None 이어야 한다. M014 이후 0 은 "실제로 0원"이라
    # 상품 마스터 원가 fallback 을 막는다 — 기본값을 0 으로 두면 원가를 안 넘긴
    # 테스트가 조용히 cost=0 으로 계산된다.
    purchase_cost: int | None = None,
    shipping_fee: int = 0,
    # 유입 계정 (M032). 기본은 None — **legacy 주문과 같은 상태**다.
    # 계정 귀속을 검증하는 테스트만 명시적으로 넘긴다. 여기에 기본 계정을 만들어
    # 넣으면 "계정 없이도 주문이 만들어진다" 는 사실을 테스트가 못 보게 된다.
    integration_account_id: UUID | None = None,
) -> UUID:
    soid = new_id()
    db.execute(
        text(
            """
            -- line_no 는 서비스(services/sales.py::_next_line_no)와 같은 방식으로
            -- 자동 채번한다. 같은 주문번호로 여러 건 만드는 테스트가
            -- uq_sales_order_mkt_orderno 에 걸리지 않게 하기 위함 (M012).
            INSERT INTO sales_order (
              id, org_id, business_key, sale_date, marketplace, marketplace_order_no,
              line_no, product_id, product_variant_id, quantity, sale_price, ad_cost,
              purchase_cost, shipping_fee, integration_account_id
            ) VALUES (
              :id, :org, :bkey, :sdate, :mkt, :orderno,
              (SELECT COALESCE(MAX(line_no), 0) + 1 FROM sales_order
                WHERE org_id = :org AND marketplace = :mkt
                  AND marketplace_order_no = :orderno AND deleted_at IS NULL),
              :product, :variant, :qty, :price, :ad, :pcost, :ship, :account
            )
            """
        ),
        {
            "id": str(soid), "org": str(org_id), "bkey": business_key or f"SO-{soid}",
            "sdate": sale_date, "mkt": marketplace, "orderno": marketplace_order_no or f"ORD-{soid}",
            "product": str(product_id) if product_id else None,
            "variant": str(product_variant_id) if product_variant_id else None,
            "qty": quantity, "price": sale_price, "ad": ad_cost,
            "pcost": purchase_cost, "ship": shipping_fee,
            "account": str(integration_account_id) if integration_account_id else None,
        },
    )
    return soid


def make_product_margin_actual(
    db: Session,
    *,
    org_id: UUID,
    product_id: UUID,
    sales_order_id: UUID,
    actual_revenue: int,
    actual_cost: int = 0,
    actual_marketplace_fee: int = 0,
    actual_payment_fee: int = 0,
    actual_ad_cost: int = 0,
    actual_fulfillment_fee: int = 0,
    actual_return_cost: int = 0,
    actual_vat: int = 0,
) -> UUID:
    margin = (
        actual_revenue - actual_cost - actual_marketplace_fee - actual_payment_fee
        - actual_ad_cost - actual_fulfillment_fee - actual_return_cost - actual_vat
    )
    rate = Decimal(margin) / Decimal(actual_revenue) if actual_revenue else Decimal(0)
    pmaid = new_id()
    db.execute(
        text(
            """
            INSERT INTO product_margin_actual (
              id, org_id, product_id, sales_order_id,
              actual_revenue, actual_cost, actual_marketplace_fee, actual_payment_fee,
              actual_ad_cost, actual_fulfillment_fee, actual_return_cost, actual_vat,
              actual_margin_amount, actual_margin_rate
            ) VALUES (
              :id, :org, :product, :so,
              :rev, :cost, :mfee, :pfee, :ad, :fulfill, :ret, :vat,
              :margin, :rate
            )
            """
        ),
        {
            "id": str(pmaid), "org": str(org_id), "product": str(product_id), "so": str(sales_order_id),
            "rev": actual_revenue, "cost": actual_cost, "mfee": actual_marketplace_fee, "pfee": actual_payment_fee,
            "ad": actual_ad_cost, "fulfill": actual_fulfillment_fee, "ret": actual_return_cost, "vat": actual_vat,
            "margin": margin, "rate": rate.quantize(Decimal("0.0001")),
        },
    )
    return pmaid


def make_ad_cost_policy(
    db: Session,
    *,
    org_id: UUID,
    marketplace: str = "COUPANG",
    default_ad_ratio: Decimal | None = Decimal("0.0500"),
    category_path: str | None = None,
    effective_from: date = DEFAULT_EFFECTIVE_FROM,
    effective_to: date | None = None,
    version: int = 1,
) -> UUID:
    aid = new_id()
    db.execute(
        text(
            """
            INSERT INTO ad_cost_policy (
              id, org_id, version, effective_from, effective_to, marketplace, category_path, default_ad_ratio
            ) VALUES (
              :id, :org, :ver, :eff_from, :eff_to, :mkt, :cat, :ratio
            )
            """
        ),
        {
            "id": str(aid), "org": str(org_id), "ver": version,
            "eff_from": effective_from, "eff_to": effective_to,
            "mkt": marketplace, "cat": category_path, "ratio": default_ad_ratio,
        },
    )
    return aid


def make_marketplace_settlement_record(
    db: Session,
    *,
    org_id: UUID,
    gross_revenue: int,
    marketplace: str = "COUPANG",
    marketplace_fee: int = 0,
    ad_cost: int = 0,
    return_cost: int = 0,
    fulfillment_fee: int = 0,
    settlement_period_start: date = DEFAULT_EFFECTIVE_FROM,
    settlement_period_end: date = DEFAULT_EFFECTIVE_FROM,
    import_file_hash: str | None = None,
    marketplace_order_no: str | None = None,
    # 유입 계정 (M033). 기본 None — legacy 정산 레코드와 같은 상태다.
    integration_account_id: UUID | None = None,
    # 원본 행 식별자 (M055). **기본값을 DB 에 두지 않았다** — 식별자를 안 넣은
    # import 가 조용히 통과하면 재발행 정산서가 같은 행을 덮어쓴다.
    # 팩토리는 행마다 유일한 값을 만들어 그 제약을 실제로 지킨다.
    import_idempotency_key: str | None = None,
    matching_status: str = "UNMATCHED",
    # 행 종류 (M056). 기본값을 DB 에 두지 않았다 — 배송료 행을 상품 매출로
    # 세면 상품 마진에 배송비가 얹힌다. 테스트도 명시적으로 고른다.
    row_type: str = "PRODUCT_SALE",
) -> UUID:
    msr_id = new_id()
    net_settlement = gross_revenue - marketplace_fee - ad_cost - return_cost - fulfillment_fee
    db.execute(
        text(
            """
            INSERT INTO marketplace_settlement_record (
              id, org_id, marketplace, settlement_period_start, settlement_period_end,
              gross_revenue, marketplace_fee, ad_cost, return_cost, fulfillment_fee,
              net_settlement, import_file_hash, marketplace_order_no,
              integration_account_id, import_idempotency_key, matching_status,
              row_type
            ) VALUES (
              :id, :org, :mkt, :ps, :pe,
              :rev, :mfee, :ad, :ret, :fulfill,
              :net, :hash, :ono, :account, :idem, :mstatus,
              :rtype
            )
            """
        ),
        {
            "id": str(msr_id), "org": str(org_id), "mkt": marketplace,
            "ps": settlement_period_start, "pe": settlement_period_end,
            "rev": gross_revenue, "mfee": marketplace_fee, "ad": ad_cost,
            "ret": return_cost, "fulfill": fulfillment_fee,
            "net": net_settlement, "hash": import_file_hash or f"hash-{msr_id}",
            "ono": marketplace_order_no,
            "account": str(integration_account_id) if integration_account_id else None,
            "idem": import_idempotency_key or f"TEST:{msr_id}",
            "mstatus": matching_status,
            "rtype": row_type,
        },
    )
    return msr_id


def make_expense(
    db: Session,
    *,
    org_id: UUID,
    category_id: UUID,
    expense_date: date,
    gross_amount: int,
    supply_amount: int,
    vat_amount: int,
    payment_method: str = "BUSINESS_CARD",
    is_settlement_deductible: bool = True,
    is_business_use: bool = True,
    description: str = "테스트 비용",
) -> UUID:
    eid = new_id()
    db.execute(
        text(
            """
            INSERT INTO expenses (
              id, org_id, expense_date, category_id, description,
              gross_amount, supply_amount, vat_amount, payment_method,
              is_settlement_deductible, is_business_use
            ) VALUES (
              :id, :org, :edate, :cat, :desc,
              :gross, :supply, :vat, CAST(:pm AS enum_payment_method),
              :isd, :ibu
            )
            """
        ),
        {
            "id": str(eid), "org": str(org_id), "edate": expense_date, "cat": str(category_id), "desc": description,
            "gross": gross_amount, "supply": supply_amount, "vat": vat_amount, "pm": payment_method,
            "isd": is_settlement_deductible, "ibu": is_business_use,
        },
    )
    return eid


# ──────────────────────────────────────────────────────────────────────────
# 월 마감 헬퍼
# ──────────────────────────────────────────────────────────────────────────

def confirm_month(
    db: Session, *, org_id: UUID, year_month: str, actor_user_id: UUID,
) -> dict[str, Any]:
    """검증을 돌려 **나온 경고를 전부 확인 처리한 뒤** 확정한다.

    테스트가 경고 코드를 하드코딩하면 검증 항목이 하나 늘 때마다 전부 깨진다.
    확인이 필요한 목록을 그때그때 읽어서 넘긴다 — 실제 사용자도 화면에서
    같은 목록을 보고 체크한다.

    **blocking 은 확인으로 넘길 수 없다.** 여기서도 우회하지 않는다 —
    blocking 이 남아 있으면 이 헬퍼도 실패해야 테스트가 의미를 갖는다.
    """
    from app.services import monthly_close as mc

    items = mc.validate_month(db, org_id=org_id, year_month=year_month)
    warnings = [i.code for i in items if i.grade != "BLOCKING"]
    return mc.confirm(
        db, org_id=org_id, year_month=year_month, actor_user_id=actor_user_id,
        acknowledged_warnings=warnings,
    )


def lock_month(
    db: Session, *, org_id: UUID, year_month: str, actor_user_id: UUID,
) -> dict[str, Any]:
    from app.services import monthly_close as mc

    items = mc.validate_month(db, org_id=org_id, year_month=year_month)
    warnings = [i.code for i in items if i.grade != "BLOCKING"]
    return mc.lock(
        db, org_id=org_id, year_month=year_month, actor_user_id=actor_user_id,
        acknowledged_warnings=warnings,
    )


def purge_month_close(db: Session, *, org_id: UUID, year_month: str | None = None) -> None:
    """월 마감 관련 행을 전부 지운다 — **테스트 teardown 전용**.

    실제 커밋을 하는 테스트(공용 SAVEPOINT 격리를 쓰지 않는 fixture)는 남긴 행을
    직접 치워야 한다. M028 이후 `monthly_settlement` 를 참조하는 테이블이 늘어서
    삭제 순서가 중요해졌다.

    snapshot 은 DELETE 트리거로 보호된다. 여기서 유지보수 탈출구를 명시적으로 연다 —
    **애플리케이션 코드에는 이 플래그가 없다.** 여기 말고 다른 곳에서 이 문자열이
    보이면 확정 숫자를 지우는 경로가 새로 생긴 것이므로 리뷰에서 막아야 한다.
    """
    scope = "AND year_month = :ym" if year_month else ""
    p: dict[str, Any] = {"o": str(org_id)}
    if year_month:
        p["ym"] = year_month

    db.execute(text("SET LOCAL picksome.allow_snapshot_delete = 'on'"))
    db.execute(text(f"""
        UPDATE monthly_settlement
           SET close_snapshot_id = NULL, last_check_run_id = NULL
         WHERE org_id = :o {scope}
    """), p)
    db.execute(text(f"""
        DELETE FROM monthly_close_snapshot WHERE org_id = :o {scope}
    """), p)
    db.execute(text(f"""
        DELETE FROM settlement_reopen_audit
         WHERE monthly_settlement_id IN (
           SELECT id FROM monthly_settlement WHERE org_id = :o {scope}
         )
    """), p)
    db.execute(text(f"""
        DELETE FROM loss_carryover_ledger
         WHERE org_id = :o AND source_settlement_id IN (
           SELECT id FROM monthly_settlement WHERE org_id = :o {scope}
         )
    """), p)
    db.execute(text(f"""
        DELETE FROM monthly_close_check_run WHERE org_id = :o {scope}
    """), p)
    db.execute(text(f"""
        DELETE FROM monthly_settlement WHERE org_id = :o {scope}
    """), p)
