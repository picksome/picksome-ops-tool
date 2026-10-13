"""NEXT ENGINE 주문 → Seller OS 해석 · Shadow 대사 (Phase 1B).

이 파일은 **쓰지 않는다**
---------------------
읽어 온 주문을 우리 개념으로 해석(resolve)하고, 기존 장부와 맞춰 보는(shadow)
데까지만 한다. `sales_order` INSERT 는 여기 없다.

이유는 판매처 계정 매핑이 아직 불가능하기 때문이다 —
`external_entity_mapping` 이 CHECK 로 `PRODUCT` 에 묶여 있어 `mall_cd` 를
`integration_account` 에 이을 수 없다(docs/74). 계정을 모른 채 주문을 넣으면
매출이 어느 스토어 것인지 모르는 채로 장부에 남는다.

해석은 세 축이다
-------------
```
판매처 계정   mall_cd  → integration_account     (지금 막혀 있다)
상품         ne_product_cd → product            (된다)
옵션         ne_stock_cd  → variant             (규칙으로 안전하게)
```

셋 다 **정확 일치만** 쓴다. 상품명·옵션명으로 찾지 않는다. 이름으로 맞추면
비슷한 이름의 다른 상품에 매출이 붙고, 그건 아무도 발견하지 못한다.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.connectors.next_engine.normalize import ExternalOrderLine

# ── 해석 결과 ────────────────────────────────────────────────────────────
#
# 각 라인이 어떻게 끝났는지. 화면·예외센터가 이 값으로 무엇을 해야 할지 안다.
RESOLVED = "RESOLVED"
UNMAPPED_MARKETPLACE_ACCOUNT = "UNMAPPED_MARKETPLACE_ACCOUNT"
UNMAPPED_PRODUCT = "UNMAPPED_PRODUCT"
AMBIGUOUS_PRODUCT = "AMBIGUOUS_PRODUCT"
UNMAPPED_OPTION = "UNMAPPED_OPTION"
INVALID_LINE = "INVALID_LINE"

#: 외부 개체 종류. `external_entity_mapping` 의 external/internal 양쪽에
#: **같은 토큰**을 쓴다 — 기존 `PRODUCT`/`PRODUCT` 와 같은 방식이고, M052 의
#: `ck_eem_type_symmetry` 가 이를 강제한다.
#:
#: 물리 테이블 이름(`integration_account`)을 어휘로 쓰지 않는다. 개념 이름은
#: 데이터에 남고, 개념→테이블 연결은 코드가 안다.
ENTITY_PRODUCT = "PRODUCT"
ENTITY_MARKETPLACE_ACCOUNT = "MARKETPLACE_ACCOUNT"


@dataclass(slots=True)
class ResolvedLine:
    """한 라인의 해석 결과. **개인정보 없음.**"""

    line: ExternalOrderLine
    status: str
    integration_account_id: UUID | None = None
    product_id: UUID | None = None
    product_variant_id: UUID | None = None
    #: 왜 그렇게 끝났는지. 사람이 읽는 짧은 사유이며 값은 담지 않는다.
    reason: str | None = None
    #: 이번에 부트스트랩으로 새로 만든 상품 매핑인가.
    product_mapping_created: bool = False

    @property
    def ok(self) -> bool:
        return self.status == RESOLVED


@dataclass
class ResolutionSummary:
    """해석 집계. 화면·리포트가 그대로 쓴다."""

    fetched: int = 0
    resolved: int = 0
    counts: Counter = field(default_factory=Counter)
    #: 매핑이 없어 막힌 스토어. 사용자가 연결해야 할 목록이다.
    unmapped_malls: dict[str, str] = field(default_factory=dict)
    #: 매핑이 없어 막힌 상품 코드.
    unmapped_products: set[str] = field(default_factory=set)
    ambiguous_products: set[str] = field(default_factory=set)
    product_mappings_created: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "resolved": self.resolved,
            "counts": dict(self.counts),
            "unmapped_marketplace_accounts": [
                {"mall_cd": k, "mall_name": v} for k, v in sorted(self.unmapped_malls.items())
            ],
            "unmapped_product_count": len(self.unmapped_products),
            "ambiguous_product_count": len(self.ambiguous_products),
            "product_mappings_created": self.product_mappings_created,
        }


# ── 판매처 계정 ──────────────────────────────────────────────────────────

def resolve_marketplace_account(
    db: Session, *, org_id: UUID, external_account_id: UUID, mall_cd: str | None,
) -> UUID | None:
    """`mall_cd` → `integration_account`. **명시적 매핑만 본다.**

    fallback 이 없다
    --------------
    `mall_name` 이 '쿠팡' 이니까 조직의 첫 쿠팡 계정에 붙이는 코드를 두지 않는다.
    계정이 하나뿐인 동안은 맞는 것처럼 보이다가, 2호점이 생기는 날 과거 주문까지
    전부 잘못된 계정에 귀속된다. 그때는 이미 정산이 끝나 있다.

    "계정이 하나뿐이니 그거겠지" 도 같은 이유로 하지 않는다.
    """
    if not mall_cd:
        return None
    row = db.execute(text("""
        SELECT internal_entity_id FROM external_entity_mapping
         WHERE org_id = :org AND external_account_id = :acc
           AND external_entity_type = :etype AND external_entity_id = :eid
           AND mapping_status = 'ACTIVE' AND deleted_at IS NULL
    """), {"org": str(org_id), "acc": str(external_account_id),
           "etype": ENTITY_MARKETPLACE_ACCOUNT, "eid": mall_cd}).scalar()
    if row is None:
        return None
    # org 를 넘지 않는지 **다시** 확인한다. 매핑 행이 잘못 만들어졌을 수 있고,
    # 그 경우 다른 조직의 계정에 매출이 붙는다.
    ok = db.execute(text("""
        SELECT 1 FROM integration_account
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(row), "org": str(org_id)}).first()
    return UUID(str(row)) if ok else None


# ── 상품 ────────────────────────────────────────────────────────────────

def _person_or_none(db: Session, actor_user_id: UUID | None) -> UUID | None:
    """`app_user` 를 가리키는 컬럼에 넣어도 안전한 값.

    자동화는 서비스 계정으로 돈다(사람이 로그인한 게 아니다). 그 id 를
    `app_user` 외래키 컬럼에 넣으면 실행이 그 자리에서 죽는다. 자동 실행의
    주체는 `automation_run`·`external_sync_run` 이 따로 기록한다.
    """
    if actor_user_id is None:
        return None
    found = db.execute(text("""
        SELECT 1 FROM app_user WHERE id = :i
    """), {"i": str(actor_user_id)}).first()
    return actor_user_id if found else None


def _lookup_product_mapping(
    db: Session, *, org_id: UUID, external_account_id: UUID, ne_product_cd: str,
) -> UUID | None:
    row = db.execute(text("""
        SELECT m.internal_entity_id FROM external_entity_mapping m
          JOIN product p ON p.id = m.internal_entity_id AND p.org_id = m.org_id
         WHERE m.org_id = :org AND m.external_account_id = :acc
           AND m.external_entity_type = :etype AND m.external_entity_id = :eid
           AND m.mapping_status = 'ACTIVE' AND m.deleted_at IS NULL
           AND p.deleted_at IS NULL
    """), {"org": str(org_id), "acc": str(external_account_id),
           "etype": ENTITY_PRODUCT, "eid": ne_product_cd}).scalar()
    return UUID(str(row)) if row else None


def bootstrap_product_candidates(
    db: Session, *, org_id: UUID, partner_product_cd: str,
) -> list[UUID]:
    """판매자상품코드로 우리 상품 후보를 찾는다. **정확 일치만.**

    `sku` 와 `business_key` 두 축을 본다. 둘 다 사람이 정한 상품 코드고,
    NEXT ENGINE 의 `ne_partner_product_cd` 도 같은 성격이다.

    이름은 보지 않는다. 후보가 2건 이상이면 **고르지 않는다** — 고르는 순간
    절반은 틀리고, 틀린 쪽은 아무도 확인하지 않는다.
    """
    rows = db.execute(text("""
        SELECT id FROM product
         WHERE org_id = :org AND deleted_at IS NULL
           AND (sku = :code OR business_key = :code)
    """), {"org": str(org_id), "code": partner_product_cd}).scalars().all()
    return [UUID(str(r)) for r in rows]


def upsert_product_mapping(
    db: Session, *, org_id: UUID, external_account_id: UUID, ne_product_cd: str,
    product_id: UUID, actor_user_id: UUID | None,
) -> None:
    """확인된 짝을 저장한다. 다음 sync 부터는 추론하지 않는다.

    매번 SKU 로 다시 추론하면, 사용자가 상품 코드를 고친 날 조용히 다른 상품에
    붙는다. 한 번 확인된 짝은 **기록이 진실**이다.

    `created_by` 는 `app_user` 를 가리킨다. 자동 실행의 주체는 서비스 계정이라
    그대로 넣으면 외래키에서 터지고, **부트스트랩 매핑이 생기는 순간 동기화
    전체가 죽는다.** 사람이 아니면 비운다.
    """
    actor_user_id = _person_or_none(db, actor_user_id)
    db.execute(text("""
        INSERT INTO external_entity_mapping
               (org_id, external_account_id, external_entity_type, external_entity_id,
                internal_entity_type, internal_entity_id, mapping_status,
                first_seen_at, last_seen_at, created_by, updated_by)
        VALUES (:org, :acc, :etype, :eid, 'PRODUCT', :pid, 'ACTIVE',
                now(), now(), :actor, :actor)
        ON CONFLICT (external_account_id, external_entity_type, external_entity_id)
          WHERE deleted_at IS NULL
        DO UPDATE SET last_seen_at = now(), updated_at = now(),
                      updated_by = EXCLUDED.updated_by
    """), {"org": str(org_id), "acc": str(external_account_id),
           "etype": ENTITY_PRODUCT, "eid": ne_product_cd, "pid": str(product_id),
           "actor": str(actor_user_id) if actor_user_id else None})


# ── 옵션 ────────────────────────────────────────────────────────────────

def _variant_count(db: Session, *, org_id: UUID, product_id: UUID) -> int:
    """옵션 개수. **비활성 옵션도 센다.**

    `is_active = false` 를 빼면 "지금은 안 파는 옵션" 이 있는 상품이 단일 옵션
    으로 보이고, 그 상품의 과거 주문이 옵션 없이 들어온다. 세는 쪽이 보수적이면
    최악의 결과가 `UNMAPPED_OPTION`(사람이 확인) 이고, 안 세면 최악이 **조용히
    틀린 옵션**이다.
    """
    return db.execute(text("""
        SELECT COUNT(*) FROM product_variant
         WHERE org_id = :org AND product_id = :p AND deleted_at IS NULL
    """), {"org": str(org_id), "p": str(product_id)}).scalar() or 0


def resolve_option(
    db: Session, *, org_id: UUID, product_id: UUID, ne_stock_cd: str | None,
) -> tuple[str, UUID | None, str | None]:
    """옵션 안전규칙. 돌려주는 것은 `(상태, variant_id, 사유)`.

    실제 주문 3건에서 `ne_stock_cd` 가 전부 비어 있었다. 그렇다고 **"이 벤더는
    옵션 코드를 안 준다" 로 일반화하지 않는다** — 셋 다 단일 상품이었을 수도
    있고, 옵션 주문이 처음 들어오는 날 규칙이 없으면 아무 옵션에나 붙는다.

    ```
    A  코드 있음                    → 그 코드로 정확 매칭
    B  코드 없음 + 우리 상품도 단일   → 상품 단위로 안전하게 처리
    C  코드 없음 + 우리 상품은 다옵션 → 고르지 않는다. UNMAPPED_OPTION
    ```

    C 가 이 함수의 전부다. 다옵션 상품에서 옵션을 모른 채 하나를 고르면 재고와
    손익이 동시에 틀어지고, 틀린 쪽이 어느 옵션인지도 남지 않는다.
    """
    if ne_stock_cd:
        # `sku_external` 이 외부 코드를 담는 자리고, `variant_key` 는 우리 키다.
        # 둘 다 정확 일치로만 본다.
        row = db.execute(text("""
            SELECT id FROM product_variant
             WHERE org_id = :org AND product_id = :p AND deleted_at IS NULL
               AND (sku_external = :code OR variant_key = :code)
        """), {"org": str(org_id), "p": str(product_id), "code": ne_stock_cd}).scalars().all()
        if len(row) == 1:
            return RESOLVED, UUID(str(row[0])), None
        if not row:
            return UNMAPPED_OPTION, None, "외부 옵션코드에 해당하는 우리 옵션이 없습니다."
        return UNMAPPED_OPTION, None, "외부 옵션코드가 여러 옵션에 걸립니다."

    variants = _variant_count(db, org_id=org_id, product_id=product_id)
    if variants <= 1:
        # 옵션이 없거나 하나뿐이면 고를 것이 없다. 상품 단위로 안전하다.
        return RESOLVED, None, None
    return (UNMAPPED_OPTION, None,
            "외부에 옵션코드가 없는데 우리 상품에는 판매옵션이 여럿입니다.")


# ── 전체 해석 ───────────────────────────────────────────────────────────

def resolve_lines(
    db: Session, *, org_id: UUID, external_account_id: UUID,
    lines: list[ExternalOrderLine], actor_user_id: UUID | None = None,
    allow_product_bootstrap: bool = True,
) -> tuple[list[ResolvedLine], ResolutionSummary]:
    """주문 라인들을 우리 개념으로 해석한다. **DB 업무 데이터는 쓰지 않는다.**

    쓰는 것은 상품 매핑(`external_entity_mapping`) 하나뿐이고, 그것도 정확
    일치가 확인된 경우에만이다. 매핑은 업무 데이터가 아니라 **번역표**다.
    """
    out: list[ResolvedLine] = []
    summary = ResolutionSummary(fetched=len(lines))

    for line in lines:
        if not line.ne_order_no:
            out.append(ResolvedLine(line, INVALID_LINE, reason="주문번호가 없습니다."))
            summary.counts[INVALID_LINE] += 1
            continue

        account_id = resolve_marketplace_account(
            db, org_id=org_id, external_account_id=external_account_id,
            mall_cd=line.mall_cd)
        if account_id is None:
            summary.unmapped_malls[line.mall_cd or "(없음)"] = line.mall_name or ""
            out.append(ResolvedLine(
                line, UNMAPPED_MARKETPLACE_ACCOUNT,
                reason="이 스토어에 연결된 판매처 계정이 없습니다."))
            summary.counts[UNMAPPED_MARKETPLACE_ACCOUNT] += 1
            continue

        if not line.ne_product_cd:
            out.append(ResolvedLine(line, UNMAPPED_PRODUCT,
                                    integration_account_id=account_id,
                                    reason="외부 상품코드가 없습니다."))
            summary.counts[UNMAPPED_PRODUCT] += 1
            continue

        product_id = _lookup_product_mapping(
            db, org_id=org_id, external_account_id=external_account_id,
            ne_product_cd=line.ne_product_cd)
        created = False

        if product_id is None and allow_product_bootstrap and line.ne_partner_product_cd:
            candidates = bootstrap_product_candidates(
                db, org_id=org_id, partner_product_cd=line.ne_partner_product_cd)
            if len(candidates) == 1:
                product_id = candidates[0]
                upsert_product_mapping(
                    db, org_id=org_id, external_account_id=external_account_id,
                    ne_product_cd=line.ne_product_cd, product_id=product_id,
                    actor_user_id=actor_user_id)
                created = True
                summary.product_mappings_created += 1
            elif len(candidates) > 1:
                summary.ambiguous_products.add(line.ne_product_cd)
                out.append(ResolvedLine(
                    line, AMBIGUOUS_PRODUCT, integration_account_id=account_id,
                    reason="판매자상품코드가 우리 상품 여러 건에 걸립니다."))
                summary.counts[AMBIGUOUS_PRODUCT] += 1
                continue

        if product_id is None:
            summary.unmapped_products.add(line.ne_product_cd)
            out.append(ResolvedLine(
                line, UNMAPPED_PRODUCT, integration_account_id=account_id,
                reason="이 외부 상품에 연결된 상품이 없습니다."))
            summary.counts[UNMAPPED_PRODUCT] += 1
            continue

        opt_status, variant_id, opt_reason = resolve_option(
            db, org_id=org_id, product_id=product_id, ne_stock_cd=line.ne_stock_cd)
        if opt_status != RESOLVED:
            out.append(ResolvedLine(
                line, UNMAPPED_OPTION, integration_account_id=account_id,
                product_id=product_id, reason=opt_reason,
                product_mapping_created=created))
            summary.counts[UNMAPPED_OPTION] += 1
            continue

        out.append(ResolvedLine(
            line, RESOLVED, integration_account_id=account_id,
            product_id=product_id, product_variant_id=variant_id,
            product_mapping_created=created))
        summary.counts[RESOLVED] += 1
        summary.resolved += 1

    return out, summary


# ── Shadow 대사 ─────────────────────────────────────────────────────────

@dataclass
class ShadowReport:
    """기존 장부와 맞춰 본 결과. **주문번호도 개인정보도 담지 않는다.**"""

    fetched: int = 0
    exact_existing_match: int = 0
    new_candidate: int = 0
    quantity_mismatch: int = 0
    amount_mismatch: int = 0
    marketplace_mismatch: int = 0
    #: 판매처를 **비교할 수 없었던** 건수.
    #:
    #: 우리는 코드(`COUPANG`)를, NEXT ENGINE 은 한글 이름(`쿠팡`)을 쓴다.
    #: 문자열을 맞대면 전부 불일치로 나오는데 그건 데이터 문제가 아니라 표기
    #: 차이다. 이름으로 맞추는 것은 금지된 fuzzy 매칭이고, 올바른 비교는
    #: `mall_cd → integration_account` 매핑이 생긴 뒤에만 가능하다.
    marketplace_not_comparable: int = 0
    not_comparable: int = 0
    #: 기존 장부에서 가장 최근 매출일. cutover 후보의 근거다.
    existing_last_sale_date: date | None = None
    #: 외부에서 본 가장 이른/늦은 매출일.
    external_first_sale_date: date | None = None
    external_last_sale_date: date | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "exact_existing_match": self.exact_existing_match,
            "new_candidate": self.new_candidate,
            "quantity_mismatch": self.quantity_mismatch,
            "amount_mismatch": self.amount_mismatch,
            "marketplace_mismatch": self.marketplace_mismatch,
            "marketplace_not_comparable": self.marketplace_not_comparable,
            "not_comparable": self.not_comparable,
            "existing_last_sale_date": str(self.existing_last_sale_date)
                if self.existing_last_sale_date else None,
            "external_first_sale_date": str(self.external_first_sale_date)
                if self.external_first_sale_date else None,
            "external_last_sale_date": str(self.external_last_sale_date)
                if self.external_last_sale_date else None,
        }


def shadow_compare(
    db: Session, *, org_id: UUID, lines: list[ExternalOrderLine],
    account_by_mall: dict[str, UUID] | None = None,
) -> ShadowReport:
    """외부 주문과 기존 `sales_order` 를 **정확 식별자로만** 맞춰 본다.

    금액·상품명·날짜가 비슷하다고 같은 주문이라고 하지 않는다. 기존 374건은
    Excel 로 들어왔고 외부 주문번호가 남아 있을 수도, 아닐 수도 있다. 없으면
    **비교 불가로 정직하게 센다** — 억지로 맞추면 cutover 를 잘못 자르고,
    그러면 매출이 중복되거나 사라진다.

    아무것도 쓰지 않는다.
    """
    report = ShadowReport(fetched=len(lines))

    report.existing_last_sale_date = db.execute(text("""
        SELECT MAX(sale_date) FROM sales_order
         WHERE org_id = :org AND deleted_at IS NULL
    """), {"org": str(org_id)}).scalar()

    dates = [d for d in (ln.sale_date for ln in lines) if d]
    if dates:
        report.external_first_sale_date = min(dates)
        report.external_last_sale_date = max(dates)

    for line in lines:
        if not line.mall_order_no:
            report.not_comparable += 1
            continue
        row = db.execute(text("""
            SELECT quantity, sale_price, integration_account_id FROM sales_order
             WHERE org_id = :org AND marketplace_order_no = :no AND deleted_at IS NULL
             LIMIT 1
        """), {"org": str(org_id), "no": line.mall_order_no}).mappings().first()
        if row is None:
            report.new_candidate += 1
            continue

        report.exact_existing_match += 1
        if line.order_cnt is not None and row["quantity"] != line.order_cnt:
            report.quantity_mismatch += 1
        if line.order_price is not None and row["sale_price"] != line.order_price:
            report.amount_mismatch += 1

        # 판매처는 **계정 UUID 로만** 비교한다.
        #
        # `marketplace` 문자열끼리 맞대면 안 된다 — 우리는 `COUPANG`, 상대는
        # `쿠팡` 이라 100% 불일치가 나오고, 그건 데이터 문제가 아니라 표기
        # 차이다. 처음에 그렇게 만들어 14건 전부를 거짓 불일치로 셌다.
        expected = (account_by_mall or {}).get(line.mall_cd or "")
        if expected is None or row["integration_account_id"] is None:
            report.marketplace_not_comparable += 1
        elif UUID(str(row["integration_account_id"])) != expected:
            report.marketplace_mismatch += 1

    return report


# ── 적재 ────────────────────────────────────────────────────────────────

#: `sales_order.import_source`. Excel 과 구분해야 어느 경로로 들어온 주문인지
#: 알 수 있고, cutover 이후 문제가 생겼을 때 되돌릴 범위를 특정할 수 있다.
INGEST_SOURCE = "NEXTENGINE_API"

#: 적재 거부 사유. 예외센터가 이 값으로 무엇을 해야 할지 안다.
REJECT_UNKNOWN_AMOUNT = "UNKNOWN_AMOUNT"
REJECT_INVALID_QUANTITY = "INVALID_QUANTITY"
REJECT_NO_SALE_DATE = "NO_SALE_DATE"
REJECT_BEFORE_CUTOVER = "BEFORE_CUTOVER"
REJECT_PERIOD_CLOSED = "PERIOD_CLOSED"
CONFLICT_OTHER_SOURCE = "EXISTING_ORDER_FROM_OTHER_SOURCE"


@dataclass
class IngestResult:
    """적재 깔때기. **주문번호도 개인정보도 담지 않는다.**

    왜 단계별로 세나
    -------------
    "적재 0건" 만 보고하면 원인을 추측하게 된다. 매핑이 없어서인지, 마감
    보호에 걸렸는지, 애초에 대상 기간에 주문이 없었는지가 전부 "0" 으로 보인다.

    각 라인은 **정확히 하나의 관문**에서 떨어지고, 마지막에 그 합이 `fetched`
    와 같은지 확인한다. 안 맞으면 어떤 라인이 조용히 사라진 것이고, 그 상태를
    SUCCESS 로 기록하면 화면과 장부가 달라진다.
    """

    fetched: int = 0

    # ── 관문 (서로 배타적, 위에서부터 순서대로) ──────────────────
    #: 주문번호나 매출일이 없어 시간축에 놓을 수 없는 라인.
    invalid_line: int = 0
    before_cutover: int = 0
    account_unmapped: int = 0
    product_unmapped: int = 0
    product_ambiguous: int = 0
    option_unmapped: int = 0
    closed_month_protected: int = 0
    #: 다른 경로(Excel)로 이미 들어온 주문. 덮지 않는다.
    existing_protected: int = 0
    #: 모든 관문을 통과한 라인 수.
    eligible: int = 0

    # ── eligible 안에서의 결과 ──────────────────────────────
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    conflict: int = 0
    rejected: int = 0

    reasons: Counter = field(default_factory=Counter)

    @property
    def after_cutover(self) -> int:
        return self.fetched - self.invalid_line - self.before_cutover

    @property
    def account_mapped(self) -> int:
        return self.after_cutover - self.account_unmapped

    @property
    def product_mapped(self) -> int:
        return self.account_mapped - self.product_unmapped - self.product_ambiguous

    @property
    def option_safe(self) -> int:
        return self.product_mapped - self.option_unmapped

    @property
    def gate_total(self) -> int:
        return (self.invalid_line + self.before_cutover + self.account_unmapped
                + self.product_unmapped + self.product_ambiguous
                + self.option_unmapped + self.closed_month_protected
                + self.existing_protected + self.eligible)

    @property
    def eligible_total(self) -> int:
        return (self.created + self.updated + self.unchanged
                + self.conflict + self.rejected)

    @property
    def reconciles(self) -> bool:
        """두 등식이 **동시에** 성립해야 한다."""
        return self.fetched == self.gate_total and self.eligible == self.eligible_total

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "funnel": {
                "invalid_line": self.invalid_line,
                "before_cutover": self.before_cutover,
                "after_cutover": self.after_cutover,
                "account_unmapped": self.account_unmapped,
                "account_mapped": self.account_mapped,
                "product_unmapped": self.product_unmapped,
                "product_ambiguous": self.product_ambiguous,
                "product_mapped": self.product_mapped,
                "option_unmapped": self.option_unmapped,
                "option_safe": self.option_safe,
                "closed_month_protected": self.closed_month_protected,
                "existing_protected": self.existing_protected,
                "eligible": self.eligible,
            },
            "created": self.created, "updated": self.updated,
            "unchanged": self.unchanged, "conflict": self.conflict,
            "rejected": self.rejected,
            "reasons": dict(self.reasons),
            "reconciles": self.reconciles,
            "gate_total": self.gate_total,
            "eligible_total": self.eligible_total,
        }

    def root_cause(self) -> str:
        """0건이면 **어느 관문 때문인지** 한 마디로 답한다.

        "데이터 문제" 로 끝내지 않기 위한 것이다.

        cutover 이전은 원인이 아니다
        ------------------------
        처음에는 가장 많이 떨어뜨린 관문을 골랐는데, 그러면 과거 주문이 많은
        첫 실행에서 항상 `CUTOVER_PROTECTION` 이 나온다. 그건 **의도한 보호**
        이지 막힌 이유가 아니다.

        답해야 할 질문은 "cutover 이후 후보들이 어디서 멈췄나" 다. 그래서
        cutover 이후 관문만 본다. 후보 자체가 없으면 그때만
        `NO_ORDERS_AFTER_CUTOVER` 다.
        """
        if self.created or self.updated:
            return "INGESTED"
        if self.fetched == 0 or self.after_cutover == 0:
            return "NO_ORDERS_AFTER_CUTOVER"
        ordered = [
            ("ACCOUNT_MAPPING_MISSING", self.account_unmapped),
            ("PRODUCT_MAPPING_MISSING", self.product_unmapped),
            ("PRODUCT_MAPPING_AMBIGUOUS", self.product_ambiguous),
            ("OPTION_MAPPING_MISSING", self.option_unmapped),
            ("CLOSED_MONTH_PROTECTION", self.closed_month_protected),
            ("ALL_ALREADY_EXIST", self.existing_protected + self.unchanged),
            ("BUSINESS_RULE_REJECTION", self.rejected),
        ]
        best = max(ordered, key=lambda kv: kv[1])
        return best[0] if best[1] else "OTHER"


def _line_numbers(lines: list[ExternalOrderLine]) -> dict[str, int]:
    """쇼핑몰 주문 안에서 라인 번호를 **결정적으로** 매긴다.

    받은 순서를 쓰면 안 된다. 페이지 경계나 상태별 조회 순서가 달라지면 같은
    라인이 다음 실행에 다른 번호를 받고, 그러면
    `uq_sales_order_mkt_orderno (org, marketplace, marketplace_order_no, line_no)`
    기준으로 **다른 행이 되어 중복이 생긴다.**

    무엇으로 묶고 무엇으로 정렬하나
    --------------------------
    묶는 것은 `mall_order_no` 다 — 유일 인덱스가 그걸 기준으로 걸려 있다.
    정렬은 `ne_order_no` 로 한다.

    상품코드로 정렬하면 안 된다. 실제로 한 쇼핑몰 주문이 **같은 상품** 두
    라인으로 오는 경우가 있었고(각각 다른 `ne_order_no`), 그때 두 라인이
    똑같이 1번을 받아 유일 인덱스를 위반한다.
    """
    by_order: dict[str, list[str]] = {}
    for line in lines:
        key = line.mall_order_no or line.ne_order_no
        by_order.setdefault(key, []).append(line.ne_order_no)
    out: dict[str, int] = {}
    for order_nos in by_order.values():
        for idx, ne_order_no in enumerate(sorted(set(order_nos)), start=1):
            out[ne_order_no] = idx
    return out


def ingest_orders(
    db: Session, *, org_id: UUID, actor_user_id: UUID, external_account_id: UUID,
    lines: list[ExternalOrderLine], cutover_after: date | None = None,
) -> IngestResult:
    """해석된 주문을 `sales_order` 에 넣는다. **멱등하다.**

    관문을 순서대로 통과한다
    -------------------
    ```
    시간축에 놓을 수 있는가   →  invalid_line
    cutover 이후인가         →  before_cutover
    판매처 계정이 붙는가       →  account_unmapped
    상품이 붙는가            →  product_unmapped / product_ambiguous
    옵션이 안전한가          →  option_unmapped
    마감된 달이 아닌가        →  closed_month_protected
    다른 경로 주문이 아닌가    →  existing_protected
                            →  eligible
    ```

    각 라인은 **정확히 하나**의 관문에서 떨어진다. 그래야 "왜 0건인가" 에
    추측 없이 답할 수 있다 — "데이터 문제" 로 끝내지 않기 위한 구조다.

    두 번 돌려도 하나다
    ----------------
    식별 기준은 `business_key = NE:<ne_order_no>` 다. 받은 순서·페이지·상태와
    무관하게 같으므로, 같은 범위를 다시 동기화하면 전부 `unchanged` 로 떨어진다.

    과거를 건드리지 않는다
    ------------------
    cutover 이전, 마감된 달, 다른 경로로 들어온 주문은 **읽기만** 한다.
    Excel 로 들어온 주문을 API 가 덮으면 사람이 확인해 고쳐 둔 값이 사라지고
    그 사실이 어디에도 남지 않는다.

    모르는 금액은 넣지 않는다
    --------------------
    `order_price` 나 `delivery_price` 가 없으면 **거부**한다. 0 으로 채우면
    "0원 주문" 이 되어 순이익이 조용히 부풀려진다.
    """
    from app.services.monthly_close import get_close_state_of

    result = IngestResult(fetched=len(lines))

    # ── 관문 1·2: 시간축과 cutover ───────────────────────────────
    survivors: list[ExternalOrderLine] = []
    for line in lines:
        if not line.ne_order_no or line.sale_date is None:
            result.invalid_line += 1
            result.reasons[
                REJECT_NO_SALE_DATE if line.ne_order_no else INVALID_LINE] += 1
            continue
        if cutover_after is not None and line.sale_date <= cutover_after:
            result.before_cutover += 1
            result.reasons[REJECT_BEFORE_CUTOVER] += 1
            continue
        survivors.append(line)

    # ── 관문 3·4·5: 계정 · 상품 · 옵션 ──────────────────────────
    resolved, resolution = resolve_lines(
        db, org_id=org_id, external_account_id=external_account_id,
        lines=survivors, actor_user_id=actor_user_id)

    ready: list[ResolvedLine] = []
    for item in resolved:
        if item.status == UNMAPPED_MARKETPLACE_ACCOUNT:
            result.account_unmapped += 1
        elif item.status == UNMAPPED_PRODUCT:
            result.product_unmapped += 1
        elif item.status == AMBIGUOUS_PRODUCT:
            result.product_ambiguous += 1
        elif item.status == UNMAPPED_OPTION:
            result.option_unmapped += 1
        elif item.status == INVALID_LINE:
            result.invalid_line += 1
        else:
            ready.append(item)
            continue
        result.reasons[item.status] += 1

    numbering = _line_numbers([r.line for r in ready])
    closed_cache: dict[str, str] = {}

    for item in ready:
        line = item.line
        sale_date = line.sale_date
        if sale_date is None:      # 관문 1 이 걸러 냈다. 방어적으로 둔다.
            result.invalid_line += 1
            continue

        # ── 관문 6: 마감된 달 ──────────────────────────────────
        ym = sale_date.strftime("%Y-%m")
        if ym not in closed_cache:
            closed_cache[ym] = get_close_state_of(db, org_id=org_id, year_month=ym)
        if closed_cache[ym] in ("CONFIRMED", "LOCKED"):
            result.closed_month_protected += 1
            result.reasons[REJECT_PERIOD_CLOSED] += 1
            continue

        provider = db.execute(text("""
            SELECT provider FROM integration_account
             WHERE id = :i AND org_id = :org AND deleted_at IS NULL
        """), {"i": str(item.integration_account_id), "org": str(org_id)}).scalar()
        if provider is None:
            result.account_unmapped += 1
            result.reasons[UNMAPPED_MARKETPLACE_ACCOUNT] += 1
            continue

        business_key = f"NE:{line.ne_order_no}"
        line_no = numbering.get(line.ne_order_no, 1)
        mall_order_no = line.mall_order_no or line.ne_order_no

        existing = db.execute(text("""
            SELECT id, quantity, sale_price, shipping_fee, sale_date, import_source
              FROM sales_order
             WHERE org_id = :org AND business_key = :bk AND deleted_at IS NULL
        """), {"org": str(org_id), "bk": business_key}).mappings().first()

        # ── 관문 7: 다른 경로로 이미 들어온 주문 ────────────────
        if existing is None:
            clash = db.execute(text("""
                SELECT import_source FROM sales_order
                 WHERE org_id = :org AND marketplace = :mk
                   AND marketplace_order_no = :mo AND line_no = :ln
                   AND deleted_at IS NULL
            """), {"org": str(org_id), "mk": provider,
                   "mo": mall_order_no, "ln": line_no}).scalar()
            if clash is not None:
                result.existing_protected += 1
                result.reasons[CONFLICT_OTHER_SOURCE] += 1
                continue
        elif existing["import_source"] != INGEST_SOURCE:
            result.existing_protected += 1
            result.reasons[CONFLICT_OTHER_SOURCE] += 1
            continue

        # ── 여기부터 eligible ────────────────────────────────
        result.eligible += 1

        if not line.order_cnt or line.order_cnt <= 0:
            result.rejected += 1
            result.reasons[REJECT_INVALID_QUANTITY] += 1
            continue
        if line.order_price is None or line.delivery_price is None:
            result.rejected += 1
            result.reasons[REJECT_UNKNOWN_AMOUNT] += 1
            continue

        payload = {
            "org": str(org_id), "bk": business_key, "sd": sale_date,
            "mk": provider, "mo": mall_order_no,
            "pid": str(item.product_id) if item.product_id else None,
            "vid": str(item.product_variant_id) if item.product_variant_id else None,
            "snap": line.product_name, "qty": line.order_cnt,
            "price": line.order_price, "ship": line.delivery_price,
            "ln": line_no, "ia": str(item.integration_account_id),
            "src": INGEST_SOURCE, "actor": str(actor_user_id),
        }

        if existing is None:
            db.execute(text("""
                INSERT INTO sales_order
                       (org_id, business_key, sale_date, marketplace,
                        marketplace_order_no, product_id, product_variant_id,
                        product_snapshot_name, quantity, sale_price, ad_cost,
                        shipping_fee, vat_inclusion_type, import_source, line_no,
                        profit_status, integration_account_id, created_by, updated_by)
                VALUES (:org, :bk, :sd, :mk, :mo, :pid, :vid, :snap, :qty, :price,
                        0, :ship, 'INCLUDED', :src, :ln, 'EXPECTED', :ia,
                        :actor, :actor)
            """), payload)
            # `ad_cost = 0` 은 "모르는 값을 0 으로" 가 아니다. 주문 시점에는
            # 광고비가 배분되지 않았고, 배분은 `ad_cost_allocation` 이 따로 한다.
            #
            # `purchase_cost` 는 넣지 않는다 → NULL 이다. 원가 미상을 0 으로
            # 계산하지 않는다는 기존 불변식 그대로다.
            result.created += 1
            continue

        changed = (
            existing["quantity"] != line.order_cnt
            or existing["sale_price"] != line.order_price
            or existing["shipping_fee"] != line.delivery_price
            or existing["sale_date"] != sale_date
        )
        if not changed:
            result.unchanged += 1
            continue

        db.execute(text("""
            UPDATE sales_order
               SET quantity = :qty, sale_price = :price, shipping_fee = :ship,
                   sale_date = :sd, product_snapshot_name = :snap,
                   updated_at = now(), updated_by = :actor
             WHERE id = :id AND org_id = :org
        """), dict(payload, id=str(existing["id"])))
        result.updated += 1

    # 미매핑 상품은 **실패가 아니라 운영 대기 항목**이다. 사람이 볼 곳에 남긴다.
    #
    # 코드마다 **가장 이른 판매일**을 함께 넘긴다. 나중에 매핑이 생겼을 때
    # 어디까지 거슬러 읽어야 그 주문을 되찾을 수 있는지가 이 값 하나로 정해진다
    # (`order_sync.pending_replays`). 이게 없으면 자동 동기화의 좁은 창 밖으로
    # 밀려난 주문은 영원히 장부에 안 들어온다.
    earliest: dict[str, date] = {}
    for item in resolved:
        if item.status != UNMAPPED_PRODUCT:
            continue
        code = item.line.ne_product_cd
        sale_date = item.line.sale_date
        if not code or sale_date is None:
            continue
        if code not in earliest or sale_date < earliest[code]:
            earliest[code] = sale_date

    sync_unmapped_product_issues(
        db, org_id=org_id, external_account_id=external_account_id,
        unmapped_codes=resolution.unmapped_products, first_sale_dates=earliest)

    return result


# ── 미매핑 상품 = 운영 예외 ──────────────────────────────────────────────

#: 운영 이슈 종류. 예외 센터와 운영 이슈 목록이 **같은 토큰**을 쓴다.
ISSUE_UNMAPPED_PRODUCT = "UNMAPPED_EXTERNAL_PRODUCT"


def _unmapped_dedupe_key(external_account_id: UUID, code: str) -> str:
    return f"NE_UNMAPPED_PRODUCT:{external_account_id}:{code}"


def sync_unmapped_product_issues(
    db: Session, *, org_id: UUID, external_account_id: UUID,
    unmapped_codes: set[str],
    first_sale_dates: dict[str, date] | None = None,
) -> dict[str, int]:
    """매핑이 없어 막힌 외부 상품코드를 `operation_issue` 에 남긴다.

    왜 이슈로 남기나
    -------------
    미매핑은 **커넥터 결함이 아니라 운영 데이터 선행조건**이다. 자동으로
    엉뚱한 상품에 붙이는 것보다 멈추는 쪽이 안전하지만, 멈춘 사실이 아무 데도
    안 보이면 매출이 조용히 빠진 채로 달이 마감된다.

    적재 결과(dict)는 그 실행에서만 살아 있고 화면은 그걸 못 본다. 그래서
    사람이 보는 곳에 남긴다.

    새 테이블을 만들지 않는다
    --------------------
    `operation_issue` 가 이미 "운영 확인 필요" 의 저장소다. 예외 센터도 이걸
    읽는다(`exception_center._integrations`).

    스캐너가 닫지 못하게 한다
    --------------------
    `operation_issue.scan()` 은 자기가 만든 `auto_resolve` 이슈 중 후보에
    없는 것을 자동 종료한다. 이 이슈의 조건은 외부 API 를 읽어야만 알 수
    있어 스캐너의 후보 목록에 오를 수 없다 — 그래서
    `created_by_scanner = FALSE`, `auto_resolve = FALSE` 로 넣는다.

    **매핑이 생겼다고 여기서 닫지 않는다.**
    -----------------------------------
    매핑 저장과 "빠졌던 주문이 장부에 들어왔다" 는 다른 사실이다. 저장 시점에
    닫으면 목록은 깨끗해지는데 매출은 비어 있고, 그 차이는 아무도 발견하지
    못한다. 종료는 되살리기가 실제로 성공한 뒤 `order_sync` 가 한다.

    복구 기간을 남긴다
    ---------------
    `year_month` 에 **가장 이른 판매 월**을 적는다. 자동 동기화는 최근 며칠만
    겹쳐 읽으므로, 나중에 매핑이 생겼을 때 어디까지 거슬러 읽어야 하는지가
    이 값으로 정해진다. 개인정보가 아니고, 새 컬럼도 필요 없다.

    값은 코드만 남긴다
    ---------------
    상품명·주문번호·구매자 정보를 제목에 넣지 않는다. 이슈 목록은 권한이 더
    넓은 화면이고, 여기 개인정보가 흘러들면 회수할 수 없다.
    """
    # 상태 어휘는 운영 이슈 모듈의 것을 그대로 쓴다. 여기서 다시 나열하면
    # 한쪽에 상태가 추가될 때 조용히 갈라진다.
    from app.services.operation_issue import CLOSED_STATUSES, OPEN_STATUSES

    codes = {c for c in (unmapped_codes or set()) if c}
    dates = first_sale_dates or {}
    opened = 0

    for code in sorted(codes):
        key = _unmapped_dedupe_key(external_account_id, code)
        seen = dates.get(code)
        year_month = seen.strftime("%Y-%m") if seen else None
        existing = db.execute(text("""
            SELECT id, status, year_month FROM operation_issue
             WHERE org_id = :org AND dedupe_key = :k AND deleted_at IS NULL
             ORDER BY CASE WHEN status = ANY(:open_st) THEN 0 ELSE 1 END,
                      last_detected_at DESC
             LIMIT 1
        """), {"org": str(org_id), "k": key,
               "open_st": list(OPEN_STATUSES)}).mappings().first()

        if existing is None:
            db.execute(text("""
                INSERT INTO operation_issue (
                  org_id, issue_type, severity, entity_type, entity_id,
                  year_month, dedupe_key, title, description, affected_count,
                  blocks_month_close, auto_resolve, created_by_scanner
                ) VALUES (
                  :org, :itype, 'WARNING', 'external_account', :acc,
                  :ym, :k, :title, :desc, 1, FALSE, FALSE, FALSE
                )
            """), {
                "org": str(org_id), "itype": ISSUE_UNMAPPED_PRODUCT,
                "acc": str(external_account_id), "k": key, "ym": year_month,
                "title": f"NEXT ENGINE 상품 {code} 미매핑",
                "desc": ("이 외부 상품코드에 연결된 Seller OS 상품이 없어 해당 "
                         "주문을 적재하지 않았습니다. 상품을 연결하면 다음 "
                         "동기화에서 보류된 주문까지 되살아납니다."),
            })
            opened += 1
            continue

        # 사람이 닫은 것을 다시 열지 않는다 — 운영 이슈 스캐너와 같은 규칙이다.
        if existing["status"] in CLOSED_STATUSES:
            continue

        # 관측 시각만 갱신한다. **월은 앞으로 당기기만** 한다 — 되살릴 때
        # 거슬러 올라갈 지점이므로 뒤로 밀면 그 사이 주문을 놓친다.
        db.execute(text("""
            UPDATE operation_issue
               SET last_detected_at = now(),
                   year_month = LEAST(
                     COALESCE(year_month, :ym), COALESCE(:ym, year_month))
             WHERE id = :id
        """), {"id": str(existing["id"]), "ym": year_month})

    return {"opened": opened, "resolved": 0}


# ── 판매처 계정 매핑 관리 ────────────────────────────────────────────────

def list_store_mappings(
    db: Session, *, org_id: UUID, external_account_id: UUID,
) -> list[dict[str, Any]]:
    """이 셀러툴 계정에 등록된 스토어 매핑. 화면이 그대로 그린다."""
    rows = db.execute(text("""
        SELECT m.external_entity_id AS mall_cd, m.internal_entity_id,
               m.mapping_status, m.first_seen_at, m.last_seen_at,
               a.account_label, a.provider
          FROM external_entity_mapping m
          JOIN integration_account a
            ON a.id = m.internal_entity_id AND a.org_id = m.org_id
         WHERE m.org_id = :org AND m.external_account_id = :acc
           AND m.external_entity_type = :etype AND m.deleted_at IS NULL
           AND a.deleted_at IS NULL
         ORDER BY m.external_entity_id
    """), {"org": str(org_id), "acc": str(external_account_id),
           "etype": ENTITY_MARKETPLACE_ACCOUNT}).mappings().all()
    return [{
        "mall_cd": r["mall_cd"],
        "integration_account_id": str(r["internal_entity_id"]),
        "account_label": r["account_label"],
        "provider": r["provider"],
        "mapping_status": r["mapping_status"],
        "first_seen_at": r["first_seen_at"],
        "last_seen_at": r["last_seen_at"],
    } for r in rows]


def map_store_to_account(
    db: Session, *, org_id: UUID, actor_user_id: UUID, external_account_id: UUID,
    mall_cd: str, integration_account_id: UUID,
) -> dict[str, Any]:
    """스토어 코드를 판매처 계정에 **사람이 명시적으로** 잇는다.

    이 함수는 `mall_name` 을 받지 않는다
    --------------------------------
    이름을 인자로 받으면 언젠가 그 이름으로 계정을 찾는 코드가 붙는다.
    받는 것은 사용자가 화면에서 **고른 계정의 id** 뿐이다.

    막는 것
    ------
    ```
    남의 조직 계정          org_id 대조 (FK 를 걸 수 없다 — 유형마다 대상이 다르다)
    같은 mall_cd 두 계정    uq_eem_external_identity 가 DB 에서 막는다
    ```
    """
    from app.core.audit import record_audit
    from app.core.exceptions import ConflictError, NotFoundError, ValidationError

    code = (mall_cd or "").strip()
    if not code:
        raise ValidationError("스토어 코드가 필요합니다.")

    # 셀러툴 계정이 이 조직 것인가.
    if db.execute(text("""
        SELECT 1 FROM external_account
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(external_account_id), "org": str(org_id)}).first() is None:
        raise NotFoundError("외부 계정을 찾을 수 없습니다.")

    # 판매처 계정이 **이 조직 것인가.** 여기가 cross-org 를 막는 자리다.
    # 다른 조직 계정을 가리키는 매핑이 한 번 생기면 매출이 남의 장부로 간다.
    if db.execute(text("""
        SELECT 1 FROM integration_account
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(integration_account_id), "org": str(org_id)}).first() is None:
        raise NotFoundError("판매처 계정을 찾을 수 없습니다.")

    existing = db.execute(text("""
        SELECT internal_entity_id FROM external_entity_mapping
         WHERE external_account_id = :acc AND external_entity_type = :etype
           AND external_entity_id = :eid AND deleted_at IS NULL
    """), {"acc": str(external_account_id), "etype": ENTITY_MARKETPLACE_ACCOUNT,
           "eid": code}).scalar()

    if existing is not None and UUID(str(existing)) != integration_account_id:
        # 조용히 갈아끼우지 않는다. 이미 그 스토어로 들어온 주문이 있으면
        # 과거 매출의 귀속이 통째로 바뀐다.
        raise ConflictError(
            f"{code} 는 이미 다른 판매처 계정에 연결돼 있습니다. "
            "먼저 연결을 해제해 주세요.",
        )

    db.execute(text("""
        INSERT INTO external_entity_mapping
               (org_id, external_account_id, external_entity_type, external_entity_id,
                internal_entity_type, internal_entity_id, mapping_status,
                first_seen_at, last_seen_at, created_by, updated_by)
        VALUES (:org, :acc, :etype, :eid, :etype, :iid, 'ACTIVE',
                now(), now(), :actor, :actor)
        ON CONFLICT (external_account_id, external_entity_type, external_entity_id)
          WHERE deleted_at IS NULL
        DO UPDATE SET last_seen_at = now(), updated_at = now(),
                      updated_by = EXCLUDED.updated_by
    """), {"org": str(org_id), "acc": str(external_account_id),
           "etype": ENTITY_MARKETPLACE_ACCOUNT, "eid": code,
           "iid": str(integration_account_id), "actor": str(actor_user_id)})

    record_audit(
        db, org_id=org_id, domain_table="external_entity_mapping",
        domain_row_id=integration_account_id, action="UPDATE",
        actor_user_id=actor_user_id,
        after={"mall_cd": code, "entity_type": ENTITY_MARKETPLACE_ACCOUNT},
        reason="MARKETPLACE_ACCOUNT_MAPPED",
    )
    db.commit()
    return {"mall_cd": code, "integration_account_id": str(integration_account_id)}


def unmap_store(
    db: Session, *, org_id: UUID, actor_user_id: UUID, external_account_id: UUID,
    mall_cd: str,
) -> None:
    """연결을 끊는다. **soft delete** 다 — 과거에 무엇에 붙어 있었는지가 남는다."""
    from app.core.audit import record_audit

    db.execute(text("""
        UPDATE external_entity_mapping
           SET deleted_at = now(), deleted_by = :actor
         WHERE org_id = :org AND external_account_id = :acc
           AND external_entity_type = :etype AND external_entity_id = :eid
           AND deleted_at IS NULL
    """), {"org": str(org_id), "acc": str(external_account_id),
           "etype": ENTITY_MARKETPLACE_ACCOUNT, "eid": mall_cd,
           "actor": str(actor_user_id)})
    record_audit(
        db, org_id=org_id, domain_table="external_entity_mapping",
        domain_row_id=external_account_id, action="DELETE",
        actor_user_id=actor_user_id, before={"mall_cd": mall_cd},
        reason="MARKETPLACE_ACCOUNT_UNMAPPED",
    )
    db.commit()


def map_product_to_internal(
    db: Session, *, org_id: UUID, actor_user_id: UUID, external_account_id: UUID,
    ne_product_cd: str, product_id: UUID,
) -> dict[str, Any]:
    """외부 상품코드를 우리 상품에 **사람이 명시적으로** 잇는다.

    왜 이 경로가 꼭 필요한가
    -------------------
    부트스트랩(판매자상품코드 정확 일치)은 두 카탈로그가 같은 코드 체계를 쓸
    때만 작동한다. 실제로는 우리가 `PICK0785`, NEXT ENGINE 이 10자리 숫자를
    쓰고 있어 **겹칠 수가 없다.** 그러면 자동 매칭은 영원히 0건이고, 사람이
    이어 주는 길이 없으면 주문은 계속 예외로만 쌓인다.

    이름으로 잇지 않는다. 상품명이 같아도 다른 상품일 수 있고, 그렇게 붙은
    매출은 아무도 발견하지 못한다.
    """
    from app.core.audit import record_audit
    from app.core.exceptions import ConflictError, NotFoundError, ValidationError

    code = (ne_product_cd or "").strip()
    if not code:
        raise ValidationError("외부 상품코드가 필요합니다.")

    if db.execute(text("""
        SELECT 1 FROM external_account
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(external_account_id), "org": str(org_id)}).first() is None:
        raise NotFoundError("외부 계정을 찾을 수 없습니다.")

    if db.execute(text("""
        SELECT 1 FROM product
         WHERE id = :i AND org_id = :org AND deleted_at IS NULL
    """), {"i": str(product_id), "org": str(org_id)}).first() is None:
        raise NotFoundError("상품을 찾을 수 없습니다.")

    existing = db.execute(text("""
        SELECT internal_entity_id FROM external_entity_mapping
         WHERE external_account_id = :acc AND external_entity_type = :etype
           AND external_entity_id = :eid AND deleted_at IS NULL
    """), {"acc": str(external_account_id), "etype": ENTITY_PRODUCT,
           "eid": code}).scalar()
    if existing is not None and UUID(str(existing)) != product_id:
        raise ConflictError(
            f"{code} 는 이미 다른 상품에 연결돼 있습니다. 먼저 연결을 해제해 주세요.")

    upsert_product_mapping(
        db, org_id=org_id, external_account_id=external_account_id,
        ne_product_cd=code, product_id=product_id, actor_user_id=actor_user_id)
    record_audit(
        db, org_id=org_id, domain_table="external_entity_mapping",
        domain_row_id=product_id, action="UPDATE", actor_user_id=actor_user_id,
        after={"external_product_cd": code, "entity_type": ENTITY_PRODUCT},
        reason="PRODUCT_MAPPED",
    )
    # **여기서 이슈를 닫지 않는다.**
    #
    # 매핑을 저장한 것과 그 상품 때문에 빠졌던 주문이 장부에 들어온 것은 다른
    # 사실이다. 저장 시점에 닫으면 목록은 깨끗해지고 매출은 비어 있다 —
    # 자동 동기화는 최근 며칠만 겹쳐 읽으므로, 이미 창 밖으로 밀려난 주문은
    # 아무도 다시 읽어 주지 않는다.
    #
    # 이슈는 `order_sync` 가 그 구간을 겨냥해 다시 읽고 **실제로 적재한 뒤**
    # 닫는다. 이슈가 남아 있는 동안이 곧 "아직 복구 안 됨" 이다.
    db.commit()
    return {"ne_product_cd": code, "product_id": str(product_id)}


def unmap_product(
    db: Session, *, org_id: UUID, actor_user_id: UUID, external_account_id: UUID,
    ne_product_cd: str,
) -> None:
    """상품 연결을 끊는다. **soft delete** 다.

    왜 이게 있어야 하나
    ---------------
    연결이 틀렸다는 것은 나중에야 드러난다(엉뚱한 상품의 마진이 이상해질 때).
    되돌릴 길이 없으면 그때 할 수 있는 일이 DB 를 직접 고치는 것뿐이고, 그건
    감사에도 남지 않는다.

    끊긴 기록은 남긴다 — 과거에 무엇에 붙어 있었는지가 사고 조사에 필요하다.
    이미 적재된 `sales_order` 는 건드리지 않는다. 연결을 끊는 것과 과거 매출을
    고치는 것은 다른 결정이다.
    """
    from app.core.audit import record_audit

    db.execute(text("""
        UPDATE external_entity_mapping
           SET deleted_at = now(), deleted_by = :actor
         WHERE org_id = :org AND external_account_id = :acc
           AND external_entity_type = :etype AND external_entity_id = :eid
           AND deleted_at IS NULL
    """), {"org": str(org_id), "acc": str(external_account_id),
           "etype": ENTITY_PRODUCT, "eid": ne_product_cd,
           "actor": str(actor_user_id)})
    record_audit(
        db, org_id=org_id, domain_table="external_entity_mapping",
        domain_row_id=external_account_id, action="DELETE",
        actor_user_id=actor_user_id, before={"external_product_cd": ne_product_cd},
        reason="PRODUCT_UNMAPPED",
    )
    db.commit()


def account_lookup(
    db: Session, *, org_id: UUID, external_account_id: UUID,
) -> dict[str, UUID]:
    """`mall_cd → integration_account` 표. Shadow 대사가 판매처를 비교할 때 쓴다."""
    rows = db.execute(text("""
        SELECT external_entity_id, internal_entity_id
          FROM external_entity_mapping
         WHERE org_id = :org AND external_account_id = :acc
           AND external_entity_type = :etype AND mapping_status = 'ACTIVE'
           AND deleted_at IS NULL
    """), {"org": str(org_id), "acc": str(external_account_id),
           "etype": ENTITY_MARKETPLACE_ACCOUNT}).all()
    return {str(r[0]): UUID(str(r[1])) for r in rows}


def propose_product_mappings(
    db: Session, *, org_id: UUID, lines: list[ExternalOrderLine],
) -> list[dict[str, Any]]:
    """상품 연결 **후보**를 근거와 함께 낸다. **적용하지 않는다.**

    왜 필요한가
    ---------
    부트스트랩은 두 카탈로그가 같은 코드 체계를 쓸 때만 작동한다. 실제로는
    우리가 `PICK0785`, NEXT ENGINE 이 10자리 숫자라 정확 일치가 영원히 0건이다.
    그러면 사용자는 11개 상품을 아무 단서 없이 손으로 찾아야 한다.

    근거는 **같은 쇼핑몰 주문번호**다
    ---------------------------
    기존 장부에 이미 상품이 붙어 있는 주문이 있고, 그 주문번호가 외부 주문과
    정확히 일치하면, 두 상품은 같은 상품일 **가능성이 높다.** 이름을 비교하지
    않는다 — 식별자만 본다.

    그래도 자동으로 적용하지 않는 이유
    ---------------------------
    Excel 의 상품 연결이 틀렸을 수 있고, 한 주문에 상품이 여럿이면 어느 쪽인지
    알 수 없다. 그래서 **주문번호 하나에 상품 하나만 걸린 경우**만 후보로 내고,
    확정은 사람이 한다(`PUT /external/accounts/{id}/product-mappings`).
    """
    by_code: dict[str, set[str]] = {}
    for line in lines:
        if not (line.ne_product_cd and line.mall_order_no):
            continue
        rows = db.execute(text("""
            SELECT DISTINCT product_id FROM sales_order
             WHERE org_id = :org AND marketplace_order_no = :no
               AND product_id IS NOT NULL AND deleted_at IS NULL
        """), {"org": str(org_id), "no": line.mall_order_no}).scalars().all()
        if len(rows) != 1:
            # 0건이면 근거가 없고, 2건 이상이면 어느 상품인지 알 수 없다.
            continue
        by_code.setdefault(line.ne_product_cd, set()).add(str(rows[0]))

    out: list[dict[str, Any]] = []
    for code, product_ids in sorted(by_code.items()):
        if len(product_ids) != 1:
            # 같은 외부 상품이 서로 다른 우리 상품에 걸렸다 — 후보로 내지 않는다.
            out.append({"ne_product_cd": code, "proposed_product_id": None,
                        "evidence": "동일 외부 상품이 서로 다른 상품에 연결돼 있습니다.",
                        "confidence": "CONFLICTING"})
            continue
        pid = next(iter(product_ids))
        name = db.execute(text("""
            SELECT name_ko FROM product WHERE id = :i AND org_id = :org
        """), {"i": pid, "org": str(org_id)}).scalar()
        out.append({
            "ne_product_cd": code, "proposed_product_id": pid,
            "product_name_ko": name,
            "evidence": "동일 쇼핑몰 주문번호의 기존 주문이 이 상품에 연결돼 있습니다.",
            "confidence": "PROPOSED",
        })
    return out


def cutover_candidate(report: ShadowReport) -> dict[str, Any]:
    """언제부터 API 를 진실로 삼을지 **후보**를 계산한다. 확정하지 않는다.

    근거는 두 가지다. 기존 장부의 마지막 매출일과, 외부에서 기존 장부에
    없는 주문이 나타나기 시작하는 지점.

    사용자에게 날짜를 먼저 물어보지 않는 이유: 사용자도 모른다. 엑셀을 마지막에
    언제 넣었는지는 데이터가 알고 있다.
    """
    if report.exact_existing_match == 0 and report.fetched > 0:
        basis = ("기존 장부에 외부 주문번호가 하나도 없습니다. Excel 주문에 "
                 "쇼핑몰 주문번호가 보존되지 않았을 수 있어 겹침을 판정할 수 "
                 "없습니다.")
        confidence = "LOW"
    elif report.exact_existing_match > 0:
        basis = "기존 장부에서 같은 쇼핑몰 주문번호를 찾았습니다. 겹침 구간이 있습니다."
        confidence = "HIGH"
    else:
        basis = "비교할 외부 주문이 없습니다."
        confidence = "NONE"

    return {
        "proposed_cutover_after": str(report.existing_last_sale_date)
            if report.existing_last_sale_date else None,
        "confidence": confidence,
        "basis": basis,
        "existing_last_sale_date": str(report.existing_last_sale_date)
            if report.existing_last_sale_date else None,
        "external_range": [
            str(report.external_first_sale_date) if report.external_first_sale_date else None,
            str(report.external_last_sale_date) if report.external_last_sale_date else None,
        ],
    }
