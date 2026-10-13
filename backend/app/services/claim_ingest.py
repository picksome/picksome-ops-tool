"""클레임 사실 적재와 매출 조정 (Phase 3).

이 파일이 지키는 것
----------------
```
원 주문을 덮어쓰지 않는다        조정 행을 쌓는다
모르는 금액을 0 으로 만들지 않는다  UNRESOLVED 로 남긴다
반품이라고 재고를 늘리지 않는다     실물 입고 증거가 없다
반품이라고 원가를 되돌리지 않는다   회수·파손·폐기를 구분할 수 없다
잠긴 달을 다시 계산하지 않는다      열린 달에 조정을 단다
```

왜 매출과 손익을 나누나
-------------------
반품이 확정되면 **매출이 얼마나 줄었는지는 알 수 있다.** 하지만 그 상품이
실제로 창고에 돌아왔는지, 파손됐는지, 마켓 수수료가 얼마나 환급되는지는
클레임 API 가 말해 주지 않는다.

```
유효 매출    계산할 수 있다
유효 순이익  아직 모른다
```

이걸 뭉개서 "마진 100%" 같은 숫자를 만들지 않는다. 모른다고 적는 것이
정확한 상태다.

금액을 어디서 얻는가
-----------------
실측에서 `취소완료` 인데 `cancel_price` 가 `0` 으로 왔다 — 3만원 주문을 전량
취소한 건이다. **벤더 금액을 그대로 믿을 수 없다.**

```
벤더 금액 > 0                        VENDOR_CLAIM
벤더 금액이 없거나 0 인데 전량 취소     DERIVED_FROM_ORIGINAL_ORDER
                                     (그 라인 매출 전부를 되돌린다)
부분 취소인데 벤더 금액이 없다          UNRESOLVED — 사람이 본다
```

부분에서 파생하지 않는 이유는 쿠폰·옵션가·마켓부담 할인 때문이다. 단가를
안다고 부분 금액을 알 수 있는 게 아니다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

_log = logging.getLogger("claim_ingest")

#: 적재 출처. 주문과 같은 어휘를 쓴다.
INGEST_SOURCE = "NEXTENGINE_API"

# ── 처리 결과 ────────────────────────────────────────────────────────────

LINKED = "LINKED"
UNMATCHED_ORDER = "UNMATCHED_CLAIM_ORDER"
UNMATCHED_LINE = "UNMATCHED_CLAIM_LINE"
FINANCIAL_UNRESOLVED = "CLAIM_FINANCIAL_UNRESOLVED"
STATUS_UNKNOWN = "CLAIM_STATUS_UNKNOWN"
INVALID = "INVALID_CLAIM"

#: 재무 효과. `sales_claim.financial_effect` 와 같은 어휘다.
NONE = "NONE"
PENDING = "PENDING"
EFFECTIVE = "EFFECTIVE"
REVERSED = "REVERSED"
UNRESOLVED = "UNRESOLVED"
#: 확정된 취소·반품인데 **원 주문이 장부에 없다.** 되돌릴 매출이 애초에
#: 없으므로 순매출은 정확하다 — 확인은 필요하지만 마감을 막지 않는다.
#: `UNRESOLVED` 와 섞으면 출고 전 취소가 마감을 막는다(실측에서 14건).
NO_LEDGER_IMPACT = "NO_LEDGER_IMPACT"

VENDOR_CLAIM = "VENDOR_CLAIM"
DERIVED = "DERIVED_FROM_ORIGINAL_ORDER"


@dataclass
class ClaimIngestResult:
    """적재 집계. 모든 클레임은 **정확히 하나**의 결과로 설명된다."""

    fetched: int = 0
    invalid: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    linked: int = 0
    unmatched_order: int = 0
    unmatched_line: int = 0
    financial_effective: int = 0
    financial_pending: int = 0
    financial_none: int = 0
    financial_unresolved: int = 0
    financial_reversed: int = 0
    financial_no_ledger_impact: int = 0
    adjustments_created: int = 0
    reversals_created: int = 0
    #: 원 주문이 뒤늦게 생겨 되살아난 클레임 수.
    recovered: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def note(self, key: str) -> None:
        self.reasons[key] = self.reasons.get(key, 0) + 1

    @property
    def persisted_total(self) -> int:
        return self.created + self.updated + self.unchanged

    @property
    def reconciles(self) -> bool:
        """읽은 것 = 버린 것 + 남긴 것. 어긋나면 성공이 아니다."""
        return self.fetched == self.invalid + self.persisted_total

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched, "invalid": self.invalid,
            "created": self.created, "updated": self.updated,
            "unchanged": self.unchanged,
            "linked": self.linked,
            "unmatched_order": self.unmatched_order,
            "unmatched_line": self.unmatched_line,
            "financial": {
                "effective": self.financial_effective,
                "pending": self.financial_pending,
                "none": self.financial_none,
                "unresolved": self.financial_unresolved,
                "reversed": self.financial_reversed,
                "no_ledger_impact": self.financial_no_ledger_impact,
            },
            "adjustments_created": self.adjustments_created,
            "reversals_created": self.reversals_created,
            "recovered": self.recovered,
            "reasons": dict(self.reasons),
            "reconciles": self.reconciles,
        }


# ── 원 주문 찾기 ─────────────────────────────────────────────────────────

def resolve_marketplace_account(
    db: Session, *, org_id: UUID, external_account_id: UUID, mall_cd: str | None,
) -> UUID | None:
    """`mall_cd` → 판매처 계정. 주문 커넥터와 **같은 매핑**을 쓴다."""
    if not mall_cd:
        return None
    row = db.execute(text("""
        SELECT m.internal_entity_id FROM external_entity_mapping m
         WHERE m.org_id = :org AND m.external_account_id = :acc
           AND m.external_entity_type = 'MARKETPLACE_ACCOUNT'
           AND m.external_entity_id = :cd
           AND m.mapping_status = 'ACTIVE' AND m.deleted_at IS NULL
    """), {"org": str(org_id), "acc": str(external_account_id),
           "cd": mall_cd}).scalar()
    return UUID(str(row)) if row else None


def find_original_order(
    db: Session, *, org_id: UUID, ne_order_no: str | None,
    mall_order_no: str | None, marketplace: str | None,
) -> dict[str, Any] | None:
    """클레임의 원 주문. **정확 일치만 본다.**

    두 경로가 있고 둘 다 식별자 기반이다.

    ```
    business_key = NE:<ne_order_no>      API 로 들어온 주문
    (마켓, 마켓주문번호)                   Excel 로 들어온 초기 장부
    ```

    두 번째가 중요하다. 초기 구축 데이터라고 클레임을 무시하면, 7월 주문의
    8월 반품이 장부에 영원히 안 잡힌다. 들어온 경로가 다를 뿐 같은 판매다.

    이름·금액으로 찾지 않는다. 비슷한 주문에 붙은 조정은 아무도 발견하지 못한다.
    """
    if ne_order_no:
        row = db.execute(text("""
            SELECT id, sale_date, sale_price, quantity, marketplace, product_id,
                   import_source, line_no
              FROM sales_order
             WHERE org_id = :o AND business_key = :bk AND deleted_at IS NULL
        """), {"o": str(org_id), "bk": f"NE:{ne_order_no}"}).mappings().first()
        if row:
            return dict(row)

    if mall_order_no and marketplace:
        rows = db.execute(text("""
            SELECT id, sale_date, sale_price, quantity, marketplace, product_id,
                   import_source, line_no
              FROM sales_order
             WHERE org_id = :o AND marketplace = :mk
               AND marketplace_order_no = :mo AND deleted_at IS NULL
        """), {"o": str(org_id), "mk": marketplace,
               "mo": mall_order_no}).mappings().all()
        if len(rows) == 1:
            return dict(rows[0])
        if len(rows) > 1:
            # 한 주문번호에 라인이 여럿이다. 어느 라인의 클레임인지 모르면
            # 임의로 나눠 붙이지 않는다 — 틀린 라인의 마진이 조용히 망가진다.
            return {"__ambiguous__": True, "count": len(rows)}
    return None


# ── 재무 판단 ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FinancialVerdict:
    """이 클레임이 매출에 무엇을 하는가."""

    effect: str
    amount: int | None = None
    amount_source: str | None = None
    quantity: int | None = None
    reason: str | None = None


def decide_financial_effect(claim: Any, order: dict[str, Any] | None) -> FinancialVerdict:
    """상태와 근거를 보고 매출 영향을 정한다. **추측하지 않는다.**

    상태가 곧 재무효과가 아니다
    ------------------------
    ```
    반품요청     아직 환불이 아니다        PENDING
    취소완료     매출이 줄었다             EFFECTIVE (금액을 알면)
    취소철회     되돌아갔다                REVERSED / NONE
    교환완료     매출이 바뀌지 않는다       NONE
    모르는 코드   판단하지 않는다            UNRESOLVED
    원 주문 없음  되돌릴 매출이 없다         NO_LEDGER_IMPACT
    ```

    마지막 둘을 구분하는 것이 중요하다
    -----------------------------
    ```
    UNRESOLVED        확정됐는데 금액을 못 정했다   순매출이 부풀어 있다  → 마감 차단
    NO_LEDGER_IMPACT  원 주문이 장부에 없다        순매출은 정확하다    → 경고만
    ```

    한 칸에 두면 출고 전 취소가 마감을 막는다. 실측에서 그 상태로 5·6·7월이
    전부 BLOCKING 이었다 — 순매출은 한 원도 틀리지 않았는데도.
    """
    ctype, cstatus = claim.claim_type, claim.claim_status

    if ctype == "UNKNOWN" or cstatus == "UNKNOWN":
        # 벤더가 새 코드를 냈다. 잘못 해석해 매출을 건드리는 것보다 멈춘다.
        return FinancialVerdict(UNRESOLVED, reason=STATUS_UNKNOWN)

    if cstatus in ("REQUESTED", "IN_PROGRESS"):
        # 아직 확정이 아니다. 철회될 수 있다.
        return FinancialVerdict(PENDING, reason="NOT_YET_CONFIRMED")

    if cstatus == "WITHDRAWN":
        # 되돌아갔다. 이미 반영한 조정이 있으면 상위에서 상쇄한다.
        return FinancialVerdict(REVERSED, reason="CLAIM_WITHDRAWN")

    if ctype == "EXCHANGE":
        # 교환은 같은 값으로 물건이 바뀌는 것이고, 환불/추가결제 사실을 이
        # API 가 주지 않는다. 매출을 0 으로 만들거나 되돌리지 않는다.
        return FinancialVerdict(NONE, reason="EXCHANGE_NO_REVENUE_EVIDENCE")

    # 여기부터 CANCEL/RETURN 의 COMPLETED — 매출이 실제로 줄어든 경우다.
    if order is None:
        # 원 주문이 장부에 없다. **되돌릴 매출이 애초에 없다.**
        #
        # 출고 전 취소가 대부분이다 — 결제만 되고 발송 전에 취소돼서 판매로
        # 잡힌 적이 없다. 실측 16건 중 14건이 여기였고, 전수 확인에서 장부에
        # 있는데 못 붙은 건은 0건이었다(docs/80 §10).
        #
        # 이걸 `UNRESOLVED` 로 두면 "확정됐는데 금액을 못 정했다"(= 순매출이
        # 그만큼 부풀어 있다) 와 같은 칸에 들어가고, 마감 검증이 BLOCKING 을
        # 낸다. 실제로는 순매출이 정확한데 5·6·7월 마감이 전부 막혔다.
        #
        # 그렇다고 조용히 넘기지도 않는다 — 주문 동기화가 밀려서 아직 안
        # 들어온 것일 수도 있다. `UNMATCHED_CLAIM_ORDER` 이슈로 남기고
        # 마감 검증은 `CLAIM_WITHOUT_ORDER` WARNING 으로 보여 준다. 주문이
        # 뒤늦게 들어오면 다음 동기화가 잇고 그때 조정이 생긴다.
        return FinancialVerdict(NO_LEDGER_IMPACT, reason=UNMATCHED_ORDER)

    vendor = claim.vendor_amount
    if vendor is not None and vendor > 0:
        return FinancialVerdict(
            EFFECTIVE, amount=-vendor, amount_source=VENDOR_CLAIM,
            quantity=claim.quantity, reason="VENDOR_AMOUNT")

    # 벤더 금액이 없거나 0 이다. 실측에서 전량 취소인데 0 으로 오는 것을 봤다.
    order_qty = order.get("quantity")
    claim_qty = claim.quantity
    booked = order.get("sale_price")

    full_by_quantity = (
        claim_qty is not None and order_qty is not None
        and claim_qty > 0 and claim_qty == order_qty)
    # 수량이 비어 있어도 원 주문이 1개짜리면 전량으로 볼 수 있다 — 부분이
    # 성립할 수 없는 구조다.
    full_by_single_line = (
        (claim_qty is None or claim_qty == 0) and order_qty == 1)

    if booked is not None and (full_by_quantity or full_by_single_line):
        # 그 라인 매출 **전부**를 되돌린다. 전량이라 할인 배분을 풀 필요가 없다.
        return FinancialVerdict(
            EFFECTIVE, amount=-int(booked), amount_source=DERIVED,
            quantity=order_qty, reason="FULL_LINE_REVERSAL")

    # 부분인데 금액이 없다. 단가를 안다고 부분 금액을 알 수 있는 게 아니다 —
    # 쿠폰·옵션가·마켓부담 할인이 어디에 붙었는지 모른다.
    return FinancialVerdict(
        UNRESOLVED, quantity=claim_qty, reason="PARTIAL_AMOUNT_UNKNOWN")


def booking_period(
    db: Session, *, org_id: UUID, sale_date: date | None, now: datetime,
) -> tuple[str | None, str]:
    """조정을 어느 달에 다는가.

    ```
    원 주문 달이 열려 있다   그 달에 단다
    닫혀 있다              지금 열린 달에 단다  (원본은 그대로)
    ```

    두 번째가 회계에서 하는 방식이고, 이 저장소가 비용 쪽에서 이미 쓰는
    방식이다(`expense_adjustment`). 잠긴 달을 다시 계산하면 이미 확정된
    순이익과 파트너 배분이 소급해서 틀린 값이 된다.
    """
    from app.core.month_lock import get_close_state, year_month_of

    original = year_month_of(sale_date) if sale_date else None
    if original is not None:
        state = get_close_state(db, org_id=org_id, year_month=original)
        if state == "OPEN":
            return original, original

    # 지금 열린 달을 찾는다. 이번 달이 이미 닫혀 있으면 앞으로 나아간다 —
    # 닫힌 달에 다는 일은 없다.
    cursor = date(now.year, now.month, 1)
    for _ in range(24):
        ym = f"{cursor.year:04d}-{cursor.month:02d}"
        if get_close_state(db, org_id=org_id, year_month=ym) == "OPEN":
            return original, ym
        cursor = date(cursor.year + (cursor.month // 12),
                      (cursor.month % 12) + 1, 1)
    # 24개월이 전부 닫혀 있는 상황은 정상이 아니다. 지어내지 않고 이번 달로
    # 두면 잠금 검증이 막아 준다.
    return original, f"{now.year:04d}-{now.month:02d}"


# ── 적재 ────────────────────────────────────────────────────────────────

def ingest_claims(
    db: Session, *, org_id: UUID, actor_user_id: UUID, external_account_id: UUID,
    claims: list[Any], now: datetime | None = None,
) -> ClaimIngestResult:
    """클레임을 적재하고, 확정된 것만 매출 조정을 만든다. **멱등하다.**

    각 클레임은 정확히 하나의 결과로 끝난다
    ---------------------------------
    ```
    식별자가 없다        invalid
    원 주문을 못 찾았다   NO_LEDGER_IMPACT → 되돌릴 매출이 없다, 나중에 되살린다
    라인이 애매하다      UNRESOLVED — 임의로 나누지 않는다
    아직 확정 전         PENDING
    확정 + 금액 확정      EFFECTIVE → 조정 생성
    확정인데 금액 미상     UNRESOLVED → 사람이 본다
    철회                REVERSED → 상쇄 조정
    ```

    두 번 돌려도 하나다
    ----------------
    `(external_account_id, external_claim_id)` 가 유일하고, 조정은
    `(sales_claim_id, adjustment_type)` 이 유일하다. 같은 클레임을 100번
    읽어도 매출이 한 번만 깎인다.
    """
    now = now or datetime.now()
    result = ClaimIngestResult(fetched=len(claims))

    for claim in claims:
        if not claim.ne_claim_no:
            # 식별자가 없으면 멱등성을 보장할 수 없다. 넣으면 매번 늘어난다.
            result.invalid += 1
            result.note(INVALID)
            continue

        account_id = resolve_marketplace_account(
            db, org_id=org_id, external_account_id=external_account_id,
            mall_cd=claim.mall_cd)
        marketplace = None
        if account_id is not None:
            marketplace = db.execute(text("""
                SELECT provider FROM integration_account
                 WHERE id = :i AND org_id = :o AND deleted_at IS NULL
            """), {"i": str(account_id), "o": str(org_id)}).scalar()

        found = find_original_order(
            db, org_id=org_id, ne_order_no=claim.ne_order_no,
            mall_order_no=claim.mall_order_no, marketplace=marketplace)

        ambiguous = bool(found and found.get("__ambiguous__"))
        order = None if ambiguous else found

        verdict = decide_financial_effect(claim, order)
        if ambiguous:
            # 주문은 있는데 어느 라인인지 모른다. 금액을 나눠 붙이지 않는다.
            verdict = FinancialVerdict(UNRESOLVED, reason=UNMATCHED_LINE)

        claim_id, changed = _upsert_claim(
            db, org_id=org_id, actor_user_id=actor_user_id,
            external_account_id=external_account_id, claim=claim,
            order=order, integration_account_id=account_id,
            verdict=verdict, now=now, result=result)

        if changed == "created":
            result.created += 1
        elif changed == "updated":
            result.updated += 1
        else:
            result.unchanged += 1

        if order is not None:
            result.linked += 1
        elif ambiguous:
            result.unmatched_line += 1
            result.note(UNMATCHED_LINE)
        else:
            result.unmatched_order += 1
            result.note(UNMATCHED_ORDER)

        _apply_financial(
            db, org_id=org_id, actor_user_id=actor_user_id, claim_id=claim_id,
            claim=claim, order=order, verdict=verdict, now=now, result=result)

        _sync_issues(
            db, org_id=org_id, external_account_id=external_account_id,
            claim=claim, order=order, ambiguous=ambiguous, verdict=verdict,
            claim_id=claim_id, result=result)

    return result


#: 클레임 하나가 만들 수 있는 이슈 전부. 해소 판정에 같은 목록을 쓴다.
_CLAIM_ISSUE_KINDS = (
    "UNMATCHED_CLAIM_ORDER", "UNMATCHED_CLAIM_LINE",
    "CLAIM_FINANCIAL_UNRESOLVED", "CLAIM_STATUS_UNKNOWN",
)


def _sync_issues(
    db: Session, *, org_id: UUID, external_account_id: UUID, claim: Any,
    order: dict[str, Any] | None, ambiguous: bool, verdict: FinancialVerdict,
    claim_id: UUID, result: ClaimIngestResult,
) -> None:
    """보류된 것을 사람이 보는 곳에 남기고, 실제로 풀린 것만 닫는다.

    닫는 조건이 핵심이다
    -----------------
    원 주문이 생겼다는 것만으로 닫지 않는다. **연결됐고 매출 반영까지
    끝났을 때** 닫는다. 그 전에 닫으면 목록은 깨끗해지고 매출은 비어 있다.
    """
    claim_no = claim.ne_claim_no
    sale_date = order["sale_date"] if order else claim.sale_date

    if order is None and not ambiguous:
        record_claim_issue(
            db, org_id=org_id, external_account_id=external_account_id,
            external_claim_id=claim_no, kind=ISSUE_UNMATCHED_ORDER,
            sale_date=sale_date)
        if verdict.reason == STATUS_UNKNOWN:
            # 원 주문도 없고 상태 코드도 모른다. "주문을 못 찾았다" 만
            # 남기면 운영자가 주문만 찾다가 끝난다 — 코드 자체를 확인해야
            # 이 클레임이 무엇인지 알 수 있다.
            record_claim_issue(
                db, org_id=org_id, external_account_id=external_account_id,
                external_claim_id=claim_no, kind=ISSUE_STATUS_UNKNOWN,
                sale_date=sale_date)
        return
    if ambiguous:
        record_claim_issue(
            db, org_id=org_id, external_account_id=external_account_id,
            external_claim_id=claim_no, kind=ISSUE_UNMATCHED_LINE,
            sale_date=sale_date)
        return

    if verdict.effect == UNRESOLVED:
        kind = (ISSUE_STATUS_UNKNOWN if verdict.reason == STATUS_UNKNOWN
                else ISSUE_FINANCIAL_UNRESOLVED)
        record_claim_issue(
            db, org_id=org_id, external_account_id=external_account_id,
            external_claim_id=claim_no, kind=kind, sale_date=sale_date)
        return

    # 여기까지 왔으면 원 주문에 붙었고 매출 판단도 끝났다. 이 클레임 때문에
    # 열려 있던 것들을 닫는다 — 되살리기가 실제로 완료된 시점이다.
    result.recovered += resolve_claim_issues(
        db, org_id=org_id, external_account_id=external_account_id,
        external_claim_id=claim_no, kinds=_CLAIM_ISSUE_KINDS)


def _upsert_claim(
    db: Session, *, org_id: UUID, actor_user_id: UUID, external_account_id: UUID,
    claim: Any, order: dict[str, Any] | None, integration_account_id: UUID | None,
    verdict: FinancialVerdict, now: datetime, result: ClaimIngestResult,
) -> tuple[UUID, str]:
    """클레임 사실 한 행. 상태가 바뀌면 **같은 행을 갱신한다.**

    상태 전이마다 새 행을 만들면 한 클레임이 여러 건으로 보이고, 취소 건수가
    실제보다 부풀어 오른다.
    """
    existing = db.execute(text("""
        SELECT id, claim_status, financial_effect, claim_amount, claim_quantity,
               sales_order_id
          FROM sales_claim
         WHERE external_account_id = :acc AND external_claim_id = :cid
           AND deleted_at IS NULL
    """), {"acc": str(external_account_id),
           "cid": claim.ne_claim_no}).mappings().first()

    payload = {
        "org": str(org_id), "acc": str(external_account_id),
        "cid": claim.ne_claim_no,
        "eoid": claim.ne_order_no, "mono": claim.mall_order_no,
        "soid": str(order["id"]) if order else None,
        "iaid": str(integration_account_id) if integration_account_id else None,
        "ctype": claim.claim_type, "cstatus": claim.claim_status,
        "vcode": claim.claim_divi, "vtext": claim.claim_status_txt,
        "eff": verdict.effect,
        "qty": verdict.quantity if verdict.quantity is not None else claim.quantity,
        "amt": abs(verdict.amount) if verdict.amount is not None else None,
        "ship": claim.vendor_shipping_amount,
        "src": verdict.amount_source,
        "req": claim.requested_at, "chg": claim.status_changed_at,
        "reg": claim.claim_reg_date,
        "sd": order["sale_date"] if order else claim.sale_date,
        "actor": str(actor_user_id),
    }

    if existing is None:
        row = db.execute(text("""
            INSERT INTO sales_claim (
              org_id, external_account_id, external_claim_id,
              external_order_id, marketplace_order_no,
              sales_order_id, integration_account_id,
              claim_type, claim_status, vendor_status_code, vendor_status_text,
              financial_effect, claim_quantity, claim_amount, shipping_amount,
              amount_source, requested_at, status_changed_at,
              claim_registered_at, original_sale_date, created_by, updated_by)
            VALUES (
              :org, :acc, :cid, :eoid, :mono, :soid, :iaid,
              :ctype, :cstatus, :vcode, :vtext, :eff, :qty, :amt, :ship,
              :src, :req, :chg, :reg, :sd, :actor, :actor)
            RETURNING id
        """), payload).scalar_one()
        return UUID(str(row)), "created"

    same = (
        existing["claim_status"] == claim.claim_status
        and existing["financial_effect"] == verdict.effect
        and existing["claim_amount"] == payload["amt"]
        and existing["claim_quantity"] == payload["qty"]
        and (str(existing["sales_order_id"]) if existing["sales_order_id"] else None)
            == payload["soid"]
    )
    if same:
        db.execute(text("""
            UPDATE sales_claim SET last_seen_at = :t WHERE id = :i
        """), {"t": now, "i": str(existing["id"])})
        return UUID(str(existing["id"])), "unchanged"

    db.execute(text("""
        UPDATE sales_claim
           SET sales_order_id = COALESCE(CAST(:soid AS UUID), sales_order_id),
               integration_account_id = COALESCE(CAST(:iaid AS UUID),
                                                 integration_account_id),
               claim_type = :ctype, claim_status = :cstatus,
               vendor_status_code = :vcode, vendor_status_text = :vtext,
               financial_effect = :eff,
               claim_quantity = :qty, claim_amount = :amt,
               shipping_amount = :ship, amount_source = :src,
               requested_at = COALESCE(:req, requested_at),
               status_changed_at = COALESCE(:chg, status_changed_at),
               original_sale_date = COALESCE(CAST(:sd AS DATE), original_sale_date),
               last_seen_at = :t, updated_at = now(), updated_by = :actor
         WHERE id = :i
    """), dict(payload, t=now, i=str(existing["id"])))
    return UUID(str(existing["id"])), "updated"


# ── 매출 조정 ────────────────────────────────────────────────────────────

def _apply_financial(
    db: Session, *, org_id: UUID, actor_user_id: UUID, claim_id: UUID,
    claim: Any, order: dict[str, Any] | None, verdict: FinancialVerdict,
    now: datetime, result: ClaimIngestResult,
) -> None:
    """확정된 것만 조정을 만든다. **원 주문은 건드리지 않는다.**

    되돌림은 지우지 않고 상쇄한다
    ------------------------
    ```
    취소완료   REV -10,000
    취소철회   REV +10,000   ← 앞 행을 지우지 않는다
    합계             0
    ```

    지우면 "취소가 있었다가 철회됐다" 는 사실이 사라지고, 그 달 매출이 왜
    두 번 움직였는지 아무도 설명하지 못한다.
    """
    if verdict.effect == EFFECTIVE:
        result.financial_effective += 1
    elif verdict.effect == PENDING:
        result.financial_pending += 1
        return
    elif verdict.effect == NONE:
        result.financial_none += 1
        return
    elif verdict.effect == UNRESOLVED:
        result.financial_unresolved += 1
        result.note(verdict.reason or FINANCIAL_UNRESOLVED)
        return
    elif verdict.effect == NO_LEDGER_IMPACT:
        # 되돌릴 매출이 장부에 없다. 조정을 만들 대상이 없으므로 아무것도
        # 만들지 않는다 — 이건 보류가 아니라 **완결된 판단**이다.
        result.financial_no_ledger_impact += 1
        result.note(verdict.reason or UNMATCHED_ORDER)
        return
    elif verdict.effect == REVERSED:
        result.financial_reversed += 1
        _reverse_if_applied(
            db, org_id=org_id, actor_user_id=actor_user_id, claim_id=claim_id,
            now=now, result=result)
        return

    if verdict.amount is None or verdict.amount_source is None:
        # 여기 오면 판단 로직이 어긋난 것이다. 금액 없는 조정을 만들지 않는다.
        result.financial_effective -= 1
        result.financial_unresolved += 1
        result.note(FINANCIAL_UNRESOLVED)
        return

    # 이미 이 클레임의 조정이 있으면 다시 만들지 않는다. 두 번 만들면 매출이
    # 두 번 깎이고, 그건 되돌리기 어렵다.
    exists = db.execute(text("""
        SELECT 1 FROM sales_adjustment
         WHERE sales_claim_id = :c AND adjustment_type = 'CLAIM_REVENUE'
           AND deleted_at IS NULL
    """), {"c": str(claim_id)}).first()
    if exists:
        return

    sale_date = order["sale_date"] if order else claim.sale_date
    original_period, adjustment_period = booking_period(
        db, org_id=org_id, sale_date=sale_date, now=now)

    db.execute(text("""
        INSERT INTO sales_adjustment (
          org_id, sales_claim_id, sales_order_id, adjustment_type,
          adjustment_reason, original_period, adjustment_period,
          amount_delta, quantity_delta, amount_source, created_by, updated_by)
        VALUES (
          :org, :c, :so, 'CLAIM_REVENUE', :reason, :op, :ap,
          :amt, :qty, :src, :actor, :actor)
    """), {
        "org": str(org_id), "c": str(claim_id),
        "so": str(order["id"]) if order else None,
        "reason": f"{claim.claim_type} {claim.claim_status}"
                  + (f" — {claim.reason}" if claim.reason else ""),
        "op": original_period, "ap": adjustment_period,
        "amt": verdict.amount,
        "qty": -verdict.quantity if verdict.quantity else None,
        "src": verdict.amount_source, "actor": str(actor_user_id),
    })
    result.adjustments_created += 1
    if original_period and original_period != adjustment_period:
        # 잠긴 달의 클레임을 열린 달에 달았다. 나중에 "왜 8월 매출에 7월
        # 주문이 있나" 를 물으면 이 사실이 답이다.
        result.note("LATE_CLAIM_BOOKED_TO_OPEN_PERIOD")


def _reverse_if_applied(
    db: Session, *, org_id: UUID, actor_user_id: UUID, claim_id: UUID,
    now: datetime, result: ClaimIngestResult,
) -> None:
    """이미 반영한 조정이 있으면 상쇄 조정을 만든다.

    반영한 적이 없으면 아무 일도 하지 않는다 — 없던 것을 되돌릴 수는 없다.
    """
    applied = db.execute(text("""
        SELECT id, amount_delta, quantity_delta, original_period, sales_order_id
          FROM sales_adjustment
         WHERE sales_claim_id = :c AND adjustment_type = 'CLAIM_REVENUE'
           AND deleted_at IS NULL
    """), {"c": str(claim_id)}).mappings().first()
    if applied is None:
        return

    already = db.execute(text("""
        SELECT 1 FROM sales_adjustment
         WHERE sales_claim_id = :c AND adjustment_type = 'CLAIM_REVERSAL'
           AND deleted_at IS NULL
    """), {"c": str(claim_id)}).first()
    if already:
        return

    _, adjustment_period = booking_period(
        db, org_id=org_id, sale_date=None, now=now)
    db.execute(text("""
        INSERT INTO sales_adjustment (
          org_id, sales_claim_id, sales_order_id, adjustment_type,
          adjustment_reason, original_period, adjustment_period,
          amount_delta, quantity_delta, amount_source,
          reverses_adjustment_id, created_by, updated_by)
        VALUES (
          :org, :c, :so, 'CLAIM_REVERSAL', :reason, :op, :ap,
          :amt, :qty, 'VENDOR_CLAIM', :rev, :actor, :actor)
    """), {
        "org": str(org_id), "c": str(claim_id),
        "so": str(applied["sales_order_id"]) if applied["sales_order_id"] else None,
        "reason": "클레임이 철회되어 앞서 반영한 매출 조정을 상쇄합니다.",
        "op": applied["original_period"], "ap": adjustment_period,
        "amt": -applied["amount_delta"],
        "qty": -applied["quantity_delta"] if applied["quantity_delta"] else None,
        "rev": str(applied["id"]), "actor": str(actor_user_id),
    })
    result.reversals_created += 1


# ── 읽기 모델 ────────────────────────────────────────────────────────────

def effective_sales(
    db: Session, *, org_id: UUID, year_month: str,
) -> dict[str, Any]:
    """그 달의 총매출·클레임 조정·유효 매출.

    손익은 여기서 말하지 않는다
    ------------------------
    매출은 계산할 수 있지만 순이익은 아직 모른다 — 반품된 물건이 실제로
    돌아왔는지, 마켓 수수료가 얼마나 환급되는지를 클레임 API 가 주지 않는다.
    한 칸에 뭉치면 "마진 100%" 같은 숫자가 나온다.
    """
    gross = db.execute(text("""
        SELECT COALESCE(SUM(sale_price), 0) FROM sales_order
         WHERE org_id = :o AND deleted_at IS NULL
           AND to_char(sale_date, 'YYYY-MM') = :ym
    """), {"o": str(org_id), "ym": year_month}).scalar_one()

    adjustment = db.execute(text("""
        SELECT COALESCE(SUM(amount_delta), 0) FROM sales_adjustment
         WHERE org_id = :o AND deleted_at IS NULL AND adjustment_period = :ym
    """), {"o": str(org_id), "ym": year_month}).scalar_one()

    # **두 숫자를 한 칸에 두지 않는다.**
    #
    #   unresolved        확정됐는데 금액을 못 정했다 → 위 매출이 부풀어 있다
    #   no_ledger_impact  원 주문이 장부에 없다      → 위 매출은 정확하다
    #
    # 그리고 **그 달 것만 센다.** 7월 주문의 미확정 클레임이 8월 유효 매출을
    # 못 믿게 만들 이유가 없다. 판단 기준은 마감 검증과 같다 — 원 주문의
    # 판매월. 판매일을 모르는 것은 어느 달에도 넣을 수 없으므로 함께 보여
    # 준다(마감 검증도 같은 규칙이다).
    counts = db.execute(text("""
        SELECT COUNT(*) FILTER (WHERE financial_effect = 'UNRESOLVED')
                 AS unresolved,
               COUNT(*) FILTER (WHERE financial_effect = 'NO_LEDGER_IMPACT')
                 AS no_ledger_impact
          FROM sales_claim
         WHERE org_id = :o AND deleted_at IS NULL
           AND (original_sale_date IS NULL
                OR to_char(original_sale_date, 'YYYY-MM') = :ym)
    """), {"o": str(org_id), "ym": year_month}).mappings().one()

    return {
        "year_month": year_month,
        "gross_sales": int(gross),
        "claim_adjustment": int(adjustment),
        "effective_sales": int(gross) + int(adjustment),
        # 반품 원가·수수료 영향을 모르므로 순이익은 확정할 수 없다.
        "profit_complete": False,
        "profit_incomplete_reason": "반품 원가·마켓 수수료 환급은 정산서로만 확인됩니다.",
        # 이 숫자가 0 이 아니면 그만큼 매출이 아직 안 줄었다.
        "unresolved_claims": int(counts["unresolved"]),
        # 이 숫자는 매출과 무관하다 — 원 주문이 장부에 없는 취소·반품이다.
        "no_ledger_impact_claims": int(counts["no_ledger_impact"]),
    }


# ── 보류된 클레임을 사람이 보는 곳에 ────────────────────────────────────

#: 운영 이슈 종류. 예외 센터와 같은 토큰을 쓴다.
ISSUE_UNMATCHED_ORDER = "UNMATCHED_CLAIM_ORDER"
ISSUE_UNMATCHED_LINE = "UNMATCHED_CLAIM_LINE"
ISSUE_FINANCIAL_UNRESOLVED = "CLAIM_FINANCIAL_UNRESOLVED"
ISSUE_STATUS_UNKNOWN = "CLAIM_STATUS_UNKNOWN"

_ISSUE_TITLE = {
    ISSUE_UNMATCHED_ORDER: "원 주문을 찾지 못한 클레임",
    ISSUE_UNMATCHED_LINE: "어느 주문 라인인지 정할 수 없는 클레임",
    ISSUE_FINANCIAL_UNRESOLVED: "매출 영향을 확정하지 못한 클레임",
    ISSUE_STATUS_UNKNOWN: "해석할 수 없는 클레임 상태",
}

_ISSUE_DESCRIPTION = {
    ISSUE_UNMATCHED_ORDER:
        "이 클레임의 원 주문이 Seller OS 에 없습니다. 되돌릴 매출이 없으므로 "
        "장부 금액은 정확하며 마감을 막지 않습니다 — 출고 전에 취소돼 매출로 "
        "잡힌 적이 없는 건이면 정상입니다. 주문이 뒤늦게 들어오면 다음 "
        "동기화가 자동으로 이어 붙이고 그때 매출 조정을 만듭니다.",
    ISSUE_UNMATCHED_LINE:
        "한 주문번호에 라인이 여럿이라 어느 라인의 클레임인지 알 수 없습니다. "
        "임의로 나눠 붙이지 않았습니다.",
    ISSUE_FINANCIAL_UNRESOLVED:
        "취소·반품이 확정됐지만 금액을 확정할 수 없습니다. 매출 조정을 만들지 "
        "않았습니다 — 추측한 금액으로 장부를 바꾸지 않습니다.",
    ISSUE_STATUS_UNKNOWN:
        "판매처가 모르는 상태 코드를 보냈습니다. 잘못 해석해 매출을 건드리는 "
        "것보다 멈췄습니다.",
}


def _issue_key(external_account_id: UUID, external_claim_id: str, kind: str) -> str:
    return f"CLAIM:{kind}:{external_account_id}:{external_claim_id}"


def record_claim_issue(
    db: Session, *, org_id: UUID, external_account_id: UUID,
    external_claim_id: str, kind: str, sale_date: date | None,
) -> None:
    """보류 사실을 `operation_issue` 에 남긴다.

    적재 결과 dict 는 그 실행에서만 살아 있다. 화면이 그걸 못 보면, 매출이
    빠진 채로 달이 마감된다 — 주문 쪽에서 이미 겪은 문제다.

    **되살릴 좌표를 함께 남긴다.** `year_month` 에 원 주문 월을 적어 두면
    나중에 주문이 들어왔을 때 어느 구간을 다시 읽어야 하는지가 정해진다.

    개인정보를 싣지 않는다 — 계정·클레임번호·월뿐이다.
    """
    from app.services.operation_issue import CLOSED_STATUSES, OPEN_STATUSES

    key = _issue_key(external_account_id, external_claim_id, kind)
    year_month = sale_date.strftime("%Y-%m") if sale_date else None

    existing = db.execute(text("""
        SELECT id, status FROM operation_issue
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
              blocks_month_close, auto_resolve, created_by_scanner)
            VALUES (
              :org, :itype, 'WARNING', 'external_account', :acc,
              :ym, :k, :title, :desc, 1, FALSE, FALSE, FALSE)
        """), {
            "org": str(org_id), "itype": kind,
            "acc": str(external_account_id), "ym": year_month, "k": key,
            "title": f"{_ISSUE_TITLE.get(kind, kind)} {external_claim_id}",
            "desc": _ISSUE_DESCRIPTION.get(kind, ""),
        })
        return

    # 사람이 닫은 것을 다시 열지 않는다 — 주문 쪽과 같은 규칙이다.
    if existing["status"] in CLOSED_STATUSES:
        return
    db.execute(text("""
        UPDATE operation_issue
           SET last_detected_at = now(),
               year_month = LEAST(COALESCE(year_month, :ym),
                                  COALESCE(:ym, year_month))
         WHERE id = :i
    """), {"i": str(existing["id"]), "ym": year_month})


def resolve_claim_issues(
    db: Session, *, org_id: UUID, external_account_id: UUID,
    external_claim_id: str, kinds: tuple[str, ...],
) -> int:
    """조건이 실제로 해소된 이슈만 닫는다.

    **매핑이 생겼다는 것으로 닫지 않는다.** 클레임이 원 주문에 붙고 필요한
    조정까지 만들어진 뒤에야 닫는다 — 주문 Phase 에서 배운 것과 같다.
    """
    from app.services.operation_issue import OPEN_STATUSES

    keys = [_issue_key(external_account_id, external_claim_id, k) for k in kinds]
    closed = db.execute(text("""
        UPDATE operation_issue
           SET status = 'RESOLVED', resolved_at = now(),
               resolution_note = COALESCE(resolution_note,
                                          '클레임이 원 주문에 연결되고 매출 반영이 끝났습니다')
         WHERE org_id = :org AND deleted_at IS NULL
           AND dedupe_key = ANY(:keys)
           AND status = ANY(:open_st)
        RETURNING id
    """), {"org": str(org_id), "keys": keys,
           "open_st": list(OPEN_STATUSES)}).scalars().all()
    return len(closed)
