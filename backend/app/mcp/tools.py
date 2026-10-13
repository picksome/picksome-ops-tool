"""MCP tool 정의 — 읽기 + 제안.

세 단계로 나눈다
-------------
```
READ      조회만. 아무것도 바꾸지 않는다
PROPOSE   승인 요청을 만든다. 사람이 승인해야 반영된다
EXECUTE   직접 실행한다                      ← **여기 없다**
```

`EXECUTE` tool 을 만들지 않았다. 자동 실행이 필요한 일은 `automation_policy`
가 한도와 함께 정하고 그 경로로만 간다. tool 에 실행 권한을 주면 정책 밖에서
실행되는 길이 하나 더 생긴다.

권한은 서버가 판정한다
------------------
클라이언트가 tool 목록을 필터하는 것은 **보안 경계가 아니다.** 목록에서 숨겨도
이름만 알면 부를 수 있다. 그래서 모든 tool 이 실행 시점에 scope 를 확인한다.

원가는 따로 막는다
---------------
`cost:read` 가 없으면 원가·landed cost·마진을 지운다. 원가를 가리면서 마진을
열어 두면 역산이 되므로 함께 묶는다(`core/sensitive.py` 와 같은 판단).

tool 설명에 비밀을 쓰지 않는다
--------------------------
설명은 그대로 AI 프롬프트에 들어간다. 내부 경로·환경변수 이름·자격증명 위치를
쓰면 그게 모델 컨텍스트로 흘러간다.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.service_account import TokenPrincipal

#: 한 번에 돌려주는 최대 건수. AI 에게 10만 건을 넘기면 컨텍스트가 터지고,
#: 비용도 그만큼 든다. 더 필요하면 조건을 좁혀서 다시 부른다.
MAX_LIMIT = 100
DEFAULT_LIMIT = 20


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    #: 이 tool 을 부르는 데 필요한 권한. 서버가 실행 시점에 확인한다.
    scope: str
    #: READ / PROPOSE. EXECUTE 는 없다.
    kind: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]


def _clamp(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(value, MAX_LIMIT))


def _strip_cost(row: dict[str, Any], principal: TokenPrincipal) -> dict[str, Any]:
    """원가 권한이 없으면 원가와 **파생값**을 함께 지운다.

    마진만 남겨 두면 판매가와 함께 원가가 역산된다.
    """
    if principal.has("cost:read"):
        return row
    hidden = {
        "cost_amount", "purchase_cost", "landed_cost", "total_krw",
        "margin_amount", "margin_rate", "expected_margin", "difference",
        "difference_rate", "current_cost_amount",
    }
    return {k: ("(권한 없음)" if k in hidden else v) for k, v in row.items()}


# ──────────────────────────────────────────────────────────────────────────
# READ
# ──────────────────────────────────────────────────────────────────────────

def _search_products(
    db: Session, principal: TokenPrincipal, *, query: str | None = None,
    limit: Any = DEFAULT_LIMIT, **_: Any,
) -> dict[str, Any]:
    from app.services import product as product_service

    rows, total = product_service.list_products(
        db, org_id=principal.org_id, q=query, limit=_clamp(limit), offset=0,
    )
    items = [_strip_cost(r.model_dump(), principal) for r in rows]
    return {"items": items, "total": total,
            "note": f"최대 {MAX_LIMIT}건까지 돌려줍니다. 더 필요하면 조건을 좁히세요."}


def _get_product(
    db: Session, principal: TokenPrincipal, *, product_id: str, **_: Any,
) -> dict[str, Any]:
    from app.services import product as product_service

    row = product_service.get_product(
        db, org_id=principal.org_id, product_id=UUID(product_id),
    )
    return _strip_cost(row.model_dump(), principal)


def _compare_product_sources(
    db: Session, principal: TokenPrincipal, *, product_id: str, **_: Any,
) -> dict[str, Any]:
    """대안 소싱처 비교.

    **순위를 매기지 않는다.** 서버가 점수도 추천도 만들지 않으므로 AI 도
    "가장 싼 곳" 을 정답처럼 말할 근거가 없다 — MOQ·리드타임·품질은 숫자에
    담기지 않는다.
    """
    principal.require("cost:read")
    from app.services import sourcing_recommendation as rec

    return rec.compare_sources(db, org_id=principal.org_id,
                               product_id=UUID(product_id))


def _get_sales_summary(
    db: Session, principal: TokenPrincipal, *, year_month: str | None = None, **_: Any,
) -> dict[str, Any]:
    from app.services import dashboard

    ym = year_month or dashboard.current_year_month()
    figures = dashboard.month_figures(db, org_id=principal.org_id, year_month=ym)
    return _strip_cost({"year_month": ym, **figures}, principal)


def _get_cashflow(
    db: Session, principal: TokenPrincipal, **_: Any,
) -> dict[str, Any]:
    from app.services import cashflow

    return cashflow.build_summary(db, org_id=principal.org_id)


def _list_operation_issues(
    db: Session, principal: TokenPrincipal, *, severity: str | None = None,
    limit: Any = DEFAULT_LIMIT, **_: Any,
) -> dict[str, Any]:
    from app.services import operation_issue

    items, total = operation_issue.list_issues(
        db, org_id=principal.org_id,
        severity=[severity] if severity else None,
        limit=_clamp(limit), offset=0,
    )
    return {"items": items, "total": total}


def _get_daily_report(
    db: Session, principal: TokenPrincipal, *, report_date: str | None = None, **_: Any,
) -> dict[str, Any]:
    """일일 운영 리포트.

    AI 가 재무 숫자를 새로 계산하지 않도록 **이미 계산된 데이터셋**을 준다.
    요약은 AI 가 하고, 숫자는 여기서 온다.
    """
    from app.services import daily_report

    parsed = date.fromisoformat(report_date) if report_date else None
    report = daily_report.build(db, org_id=principal.org_id, report_date=parsed)
    if not principal.has("cost:read"):
        report["sections"].pop("margin", None)
        report["sections"].pop("sourcing", None)
    return report


# ──────────────────────────────────────────────────────────────────────────
# 재고 · 발주
#
# **여기서 재고를 다시 계산하지 않는다.** 서비스가 계산한 것을 그대로 넘긴다 —
# 두 곳에서 계산하면 화면과 AI 가 다른 숫자를 말하게 되고, 사용자는 어느 쪽도
# 믿지 않게 된다.
# ──────────────────────────────────────────────────────────────────────────

def _get_inventory_status(
    db: Session, principal: TokenPrincipal, *, search: str | None = None,
    only_negative: Any = False, limit: Any = DEFAULT_LIMIT, **_: Any,
) -> dict[str, Any]:
    """재고 현황.

    `reserved_status` 를 그대로 넘긴다. AI 가 "예약 0개" 로 요약하지 않도록
    **모른다는 사실**이 데이터에 남아 있어야 한다.
    """
    from app.services import inventory

    return inventory.list_balances(
        db, org_id=principal.org_id, search=search,
        only_negative=bool(only_negative), limit=_clamp(limit),
    )


def _list_inventory_movements(
    db: Session, principal: TokenPrincipal, *, product_id: str,
    limit: Any = DEFAULT_LIMIT, **_: Any,
) -> dict[str, Any]:
    """한 상품의 재고 움직임 이력."""
    from app.services import inventory

    rows = inventory.list_transactions(
        db, org_id=principal.org_id, product_id=UUID(product_id),
        limit=_clamp(limit),
    )
    if not principal.has("cost:read"):
        # 취득 단가는 원가다. 원가 권한이 없으면 지운다.
        for r in rows:
            r.pop("unit_cost", None)
    return {"items": rows}


def _list_reorder_candidates(
    db: Session, principal: TokenPrincipal, *, velocity_days: Any = None,
    coverage_days: Any = None, **_: Any,
) -> dict[str, Any]:
    """재주문 후보.

    **기본값이 없다.** 두 값을 안 주면 계산하지 않고 왜 못 하는지 알린다 —
    AI 가 "보통 30일" 을 채워 넣고 그 결과를 사람이 믿는 일을 막는다.
    """
    from app.services import reorder

    if velocity_days is None or coverage_days is None:
        return {
            "status": "NOT_CONFIGURED",
            "reason": "velocity_days 와 coverage_days 가 필요합니다. "
                      "적정 재고 수준은 사업마다 달라 기본값을 두지 않습니다.",
            "candidates": [], "blocked": [],
        }

    result = reorder.calculate(
        db, org_id=principal.org_id,
        velocity_days=int(velocity_days), coverage_days=int(coverage_days),
    )
    if not principal.has("cost:read"):
        for c in result["candidates"]:
            c.pop("unit_price", None)
            c.pop("estimated_amount", None)
            c.pop("price_basis", None)
        result["summary"].pop("estimated_total", None)
    return result


def _list_order_exceptions(
    db: Session, principal: TokenPrincipal, *, category: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """사람이 손대야 하는 것들.

    화면(`/operations/exceptions`)과 **같은 읽기 모델**이다.
    """
    from app.services import exception_center

    data = exception_center.build(db, org_id=principal.org_id)
    if category:
        data["items"] = [i for i in data["items"] if i["category"] == category]
    return data


def _get_purchase_order(
    db: Session, principal: TokenPrincipal, *, order_id: str, **_: Any,
) -> dict[str, Any]:
    from app.services import purchasing

    order = purchasing.get_order(
        db, org_id=principal.org_id, order_id=UUID(order_id))
    if not principal.has("cost:read"):
        for ln in order["lines"]:
            ln.pop("unit_price", None)
            ln.pop("line_total", None)
        order.pop("total_amount", None)
    return order


def _propose_purchase_order(
    db: Session, principal: TokenPrincipal, *, supplier_site_id: str,
    lines: Any, reason: str, expected_at: str | None = None, **_: Any,
) -> dict[str, Any]:
    """발주 **초안** 제안.

    `DRAFT` 까지만 만든다. 초안은 아무 데도 나가지 않고, `ORDERED` 로 옮기는
    것은 사람이 화면에서 하는 별도 행동이다.

    공급처에 실제로 주문을 넣는 tool 은 없다. 만들지 않는다 — 발주 근거인
    판매속도는 추정치이고, 추정치로 돈을 쓰는 경로를 AI 에게 열어 주면
    승인 체계가 장식이 된다.
    """
    principal.require("purchasing:propose")
    from app.services import purchasing, service_account

    if not reason or not reason.strip():
        raise ValueError("발주 사유가 필요합니다.")
    if not lines:
        raise ValueError("발주 품목이 없습니다.")

    # 단가를 지어내지 않는다. 모르면 사람이 채운다.
    for ln in lines:
        if ln.get("unit_price") is None:
            raise ValueError("발주 단가가 없습니다. 소싱 원가를 확인하세요.")

    order = purchasing.create_order(
        db, org_id=principal.org_id,
        # **사람으로 위장하지 않는다.** 서비스 계정이 만든 것으로 남는다.
        actor_user_id=None,
        service_account_id=principal.service_account_id,
        supplier_site_id=UUID(supplier_site_id),
        lines=[{
            "product_id": ln["product_id"],
            "product_variant_id": ln.get("product_variant_id"),
            "quantity": int(ln["quantity"]),
            "unit_price": int(ln["unit_price"]),
            "product_source_id": ln.get("product_source_id"),
        } for ln in lines],
        expected_at=date.fromisoformat(expected_at) if expected_at else None,
        memo=f"[AI 제안] {reason.strip()}",
    )
    service_account.record_agent_audit(
        db, principal=principal,
        domain_table="purchase_order", domain_row_id=UUID(order["id"]),
        action="CREATE", after={"status": "DRAFT", "lines": len(lines)},
        reason=f"[AI 제안] {reason.strip()}",
    )
    db.commit()
    return {
        "purchase_order_id": order["id"],
        "business_key": order["business_key"],
        "status": order["status"],
        "note": "초안입니다. 사람이 확정해야 발주가 나갑니다.",
    }


# ──────────────────────────────────────────────────────────────────────────
# PROPOSE
# ──────────────────────────────────────────────────────────────────────────

def _propose_cost_change(
    db: Session, principal: TokenPrincipal, *, product_id: str,
    cost_amount: int, reason: str, **_: Any,
) -> dict[str, Any]:
    """원가 변경 **제안**.

    `product.cost_amount` 를 직접 바꾸지 않는다. 기존 `COST_CHANGE` 승인
    요청을 만들고 거기서 끝난다 — 사람이 승인해야 반영된다.

    자동화용 우회 경로를 만들지 않는다. AI 가 승인 체계를 지나칠 수 있으면
    승인 체계가 장식이 된다.
    """
    principal.require("approvals:create")
    from app.services import approval, service_account

    if not reason or not reason.strip():
        # 사유 없는 제안은 승인자가 판단할 수 없다.
        raise ValueError("변경 사유가 필요합니다.")

    created = approval.create_request(
        db, org_id=principal.org_id,
        # 승인 요청의 작성자는 사람이 아니다. 서비스 계정이 만든 것으로 남는다 —
        # 사람 이름으로 남기면 승인자가 틀린 전제 위에서 판단한다(M046).
        actor_user_id=None,
        service_account_id=principal.service_account_id,
        target_type="product", target_id=UUID(product_id),
        request_type="COST_CHANGE",
        proposed_changes={"cost_amount": int(cost_amount)},
        reason=f"[{principal.service_account_name}] {reason.strip()}",
        submit=True,
    )
    service_account.record_agent_audit(
        db, principal=principal, domain_table="approval_request",
        domain_row_id=UUID(str(created["id"])), action="CREATE",
        after={"request_type": "COST_CHANGE", "cost_amount": int(cost_amount)},
        reason="MCP_PROPOSE_COST_CHANGE",
    )
    db.commit()
    return {
        "approval_request_id": str(created["id"]),
        "status": created["status"],
        "note": "승인 요청을 만들었습니다. 사람이 승인해야 원가가 바뀝니다.",
    }


# ──────────────────────────────────────────────────────────────────────────
# 등록
# ──────────────────────────────────────────────────────────────────────────

_STR = {"type": "string"}
_INT = {"type": "integer"}

TOOLS: tuple[Tool, ...] = (
    Tool(
        name="search_products",
        description="상품을 이름·SKU 로 찾는다. 최대 100건.",
        scope="products:read", kind="READ",
        input_schema={"type": "object", "properties": {
            "query": _STR, "limit": _INT}},
        handler=_search_products,
    ),
    Tool(
        name="get_product",
        description="상품 하나의 상세를 읽는다.",
        scope="products:read", kind="READ",
        input_schema={"type": "object", "properties": {"product_id": _STR},
                      "required": ["product_id"]},
        handler=_get_product,
    ),
    Tool(
        name="compare_product_sources",
        description=(
            "한 상품의 소싱처를 나란히 비교한다. 예상 원가·MOQ·리드타임을 준다. "
            "순위나 추천은 제공하지 않는다 — 가장 싼 곳이 정답이 아니다."
        ),
        scope="sourcing:read", kind="READ",
        input_schema={"type": "object", "properties": {"product_id": _STR},
                      "required": ["product_id"]},
        handler=_compare_product_sources,
    ),
    Tool(
        name="get_sales_summary",
        description="특정 달의 매출·비용·이익 요약. 형식은 YYYY-MM.",
        scope="sales:read", kind="READ",
        input_schema={"type": "object", "properties": {"year_month": _STR}},
        handler=_get_sales_summary,
    ),
    Tool(
        name="get_cashflow",
        description="현금 흐름 요약. 기준 잔액이 없으면 '확인 불가' 로 온다.",
        scope="cashflow:read", kind="READ",
        input_schema={"type": "object", "properties": {}},
        handler=_get_cashflow,
    ),
    Tool(
        name="list_operation_issues",
        description="운영 이슈 목록. severity 는 BLOCKING/WARNING/INFO.",
        scope="issues:read", kind="READ",
        input_schema={"type": "object", "properties": {
            "severity": _STR, "limit": _INT}},
        handler=_list_operation_issues,
    ),
    Tool(
        name="get_daily_report",
        description=(
            "하루치 운영 리포트. 확인이 필요한 일이 맨 위에 온다. "
            "숫자는 이미 계산된 값이니 다시 계산하지 말고 그대로 인용한다."
        ),
        scope="reports:read", kind="READ",
        input_schema={"type": "object", "properties": {"report_date": _STR}},
        handler=_get_daily_report,
    ),
    Tool(
        name="get_inventory_status",
        description=(
            "재고 현황. reserved_status 가 NOT_CONNECTED 이면 예약 수량을 알 수 "
            "없다는 뜻이다 — 0개로 해석하지 말 것."
        ),
        scope="inventory:read", kind="READ",
        input_schema={"type": "object", "properties": {
            "search": _STR, "only_negative": {"type": "boolean"}, "limit": _INT}},
        handler=_get_inventory_status,
    ),
    Tool(
        name="list_inventory_movements",
        description="한 상품의 재고 움직임 이력. 재고가 왜 지금 숫자인지 설명한다.",
        scope="inventory:read", kind="READ",
        input_schema={"type": "object", "properties": {
            "product_id": _STR, "limit": _INT}, "required": ["product_id"]},
        handler=_list_inventory_movements,
    ),
    Tool(
        name="list_reorder_candidates",
        description=(
            "재주문 후보. velocity_days 와 coverage_days 가 **필수**다 — "
            "기본값이 없으므로 임의로 정하지 말고 사용자에게 물을 것. "
            "blocked 목록은 데이터가 없어 계산 못 한 상품이며 '여유 있음' 이 아니다."
        ),
        scope="inventory:read", kind="READ",
        input_schema={"type": "object", "properties": {
            "velocity_days": _INT, "coverage_days": _INT}},
        handler=_list_reorder_candidates,
    ),
    Tool(
        name="list_order_exceptions",
        description=(
            "사람이 손대야 하는 항목. 카테고리 status 가 NOT_CONNECTED / "
            "NOT_CONFIGURED 이면 건수 0 이 '이상 없음' 을 뜻하지 않는다."
        ),
        scope="issues:read", kind="READ",
        input_schema={"type": "object", "properties": {"category": _STR}},
        handler=_list_order_exceptions,
    ),
    Tool(
        name="get_purchase_order",
        description="발주 한 건의 상세와 입고 현황.",
        scope="purchasing:read", kind="READ",
        input_schema={"type": "object", "properties": {"order_id": _STR},
                      "required": ["order_id"]},
        handler=_get_purchase_order,
    ),
    Tool(
        name="propose_purchase_order",
        description=(
            "발주 **초안**을 만든다. DRAFT 까지만이고 사람이 확정해야 나간다. "
            "공급처에 실제로 주문하는 기능은 제공하지 않는다."
        ),
        scope="purchasing:propose", kind="PROPOSE",
        input_schema={"type": "object", "properties": {
            "supplier_site_id": _STR,
            "lines": {"type": "array", "items": {"type": "object"}},
            "reason": _STR, "expected_at": _STR},
            "required": ["supplier_site_id", "lines", "reason"]},
        handler=_propose_purchase_order,
    ),
    Tool(
        name="propose_cost_change",
        description=(
            "상품 원가 변경을 **제안**한다. 원가가 바로 바뀌지 않고 승인 요청이 "
            "만들어진다. 사람이 승인해야 반영된다."
        ),
        scope="approvals:create", kind="PROPOSE",
        input_schema={"type": "object", "properties": {
            "product_id": _STR, "cost_amount": _INT, "reason": _STR},
            "required": ["product_id", "cost_amount", "reason"]},
        handler=_propose_cost_change,
    ),
)

TOOLS_BY_NAME = {t.name: t for t in TOOLS}


def visible_tools(principal: TokenPrincipal) -> list[Tool]:
    """이 토큰이 쓸 수 있는 tool.

    목록을 좁히는 것은 **편의**다. 보안은 `call()` 의 scope 확인이 한다 —
    목록에 없어도 이름만 알면 부를 수 있기 때문이다.
    """
    return [t for t in TOOLS if principal.has(t.scope)]


def call(
    db: Session, principal: TokenPrincipal, *, name: str,
    arguments: dict[str, Any] | None = None,
) -> Any:
    """tool 실행. **여기가 보안 경계다.**"""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise KeyError(f"알 수 없는 tool 입니다: {name}")

    # 클라이언트가 목록을 어떻게 보여줬든 서버가 다시 판정한다.
    principal.require(tool.scope)

    # 인자를 그대로 SQL·경로·URL 로 쓰지 않는다. handler 는 기존 service 를
    # 부르고, 그 service 들이 이미 검증한다.
    return tool.handler(db, principal, **(arguments or {}))
