"""민감 변경값 조회 권한과 redaction.

원칙
----
**프론트에서 숨기지 않는다.** 값을 응답에 실어 보낸 뒤 CSS 로 가리면
개발자 도구·API 직접 호출로 그대로 보인다. **서버가 값을 지워서 보낸다.**

무엇이 민감한가
--------------
원가 구성값과 그로부터 나오는 마진·배분 금액이다.
상품명·상태·URL 은 아니다 — 그건 승인자가 판단하려면 봐야 하는 정보다.

권한을 역할 이름으로만 판정하지 않는다. `PERMISSION_MATRIX` 에 permission code 를 두고
역할을 매핑한다. 나중에 역할이 늘거나 조직별 override 가 생겨도 이 표만 고치면 된다.
"""

from __future__ import annotations

from typing import Any

# ── permission code ─────────────────────────────────────────────
PERM_VIEW_SENSITIVE_DIFF = "approval.view_sensitive_diff"

# 증빙 파일 내려받기 (M027).
#
# **목록 조회와 별개로 둔다.** 파일명·크기·업로드자는 "증빙이 붙어 있다" 를
# 확인하는 데 필요한 운영 정보지만, 파일 **내용**은 계좌번호·사업자번호·거래
# 상대방이 그대로 찍힌 세금계산서일 수 있다. 목록을 보는 것과 문서를 여는 것은
# 다른 권한이다.
PERM_VIEW_EXPENSE_ATTACHMENT = "expense.view_attachment"

# 소싱 매입가 조회 (M037).
#
# **공급처에서 얼마에 사 오는지는 사업의 핵심 정보다.** 어디서 사 오는지(공급처명·
# URL)는 운영 업무에 필요하지만 매입가는 다르다. 조회 전용 계정과 외부 세무대리인은
# 볼 이유가 없다 — 세무 신고에 필요한 것은 실제 매입 증빙이지 소싱 검토가가 아니다.
PERM_VIEW_SOURCE_COST = "sourcing.view_cost"

# 재고 **수량** 조회 (M048).
#
# 수량과 원가는 다른 축이다. "지금 몇 개 있나" 는 창고·CS·조회 전용까지
# 알아야 하는 운영 정보고, "그걸 얼마에 샀나" 는 사업의 핵심 정보다.
#
# 예전에는 재고 화면 자체를 원가 권한자에게만 열어 뒀는데, 그건 수량을 숨기려는
# 것이 아니라 원가를 숨기려다 수량까지 같이 막은 것이었다. 둘을 나눈다.
PERM_VIEW_INVENTORY_QUANTITY = "inventory.view_quantity"

# 재고 **취득원가** 조회 (M048).
#
# `PERM_VIEW_SOURCE_COST` 와 **같은 역할 집합**을 쓴다. 둘 다 "공급처에 얼마를
# 주는가" 이고, 하나만 가리면 다른 하나로 역산된다 — 소싱 매입가를 아는 사람은
# 입고 단가도 사실상 안다.
#
# 그래도 코드를 따로 두는 이유: 나중에 "소싱 검토가는 못 보지만 실제 지불액은
# 봐야 하는" 역할이 생길 수 있고, 그때 이 표만 고치면 된다.
PERM_VIEW_ACQUISITION_COST = "inventory.view_acquisition_cost"

# 역할 → permission. `core/rbac.ROLES` 6종과 대응한다.
PERMISSION_MATRIX: dict[str, frozenset[str]] = {
    # 전 권한
    "ADMIN": frozenset({PERM_VIEW_SENSITIVE_DIFF, PERM_VIEW_EXPENSE_ATTACHMENT,
                        PERM_VIEW_SOURCE_COST, PERM_VIEW_INVENTORY_QUANTITY,
                        PERM_VIEW_ACQUISITION_COST}),
    # 원가·마진이 곧 업무 대상. 증빙 원본은 세무 업무의 입력값이다
    "ACCOUNTING": frozenset({PERM_VIEW_SENSITIVE_DIFF, PERM_VIEW_EXPENSE_ATTACHMENT,
                             PERM_VIEW_SOURCE_COST, PERM_VIEW_INVENTORY_QUANTITY,
                             # 매입액은 매출원가의 입력값이다. 결산에 필요하다
                             PERM_VIEW_ACQUISITION_COST}),
    # 상품 원가를 승인하는 주체라 값을 봐야 승인이 성립한다.
    # 비용 승인도 하므로 근거 문서를 열 수 있어야 한다
    "OPERATOR": frozenset({PERM_VIEW_SENSITIVE_DIFF, PERM_VIEW_EXPENSE_ATTACHMENT,
                           # 상품 원가 승인 주체라 근거가 되는 매입가를 봐야 한다
                           PERM_VIEW_SOURCE_COST, PERM_VIEW_INVENTORY_QUANTITY,
                           PERM_VIEW_ACQUISITION_COST}),
    # 소싱은 매입가를 직접 다룬다. 사업비용 증빙은 업무 범위 밖이다
    "SOURCING": frozenset({PERM_VIEW_SENSITIVE_DIFF, PERM_VIEW_SOURCE_COST,
                           PERM_VIEW_INVENTORY_QUANTITY,
                           PERM_VIEW_ACQUISITION_COST}),
    # 조회 전용 — 금액도 증빙 원본도 보지 못한다.
    # 다만 **재고 수량은 본다.** 몇 개 남았는지는 금액이 아니다
    "READONLY": frozenset({PERM_VIEW_INVENTORY_QUANTITY}),
    # 외부 세무대리인 — 신고 자료를 만들려면 증빙 원본이 필요하다.
    # 상품 원가는 여전히 볼 이유가 없다. (라우트 자체는 D-282 feature flag 대상)
    # 재고 수량도 주지 않는다 — 신고 자료를 만드는 데 창고 현황은 필요 없다
    "EXTERNAL_ACCOUNTANT": frozenset({PERM_VIEW_EXPENSE_ATTACHMENT}),
}


def has_permission(role: str, permission: str) -> bool:
    return permission in PERMISSION_MATRIX.get(role, frozenset())


def can_view_sensitive_diff(role: str) -> bool:
    return has_permission(role, PERM_VIEW_SENSITIVE_DIFF)


def can_view_source_cost(role: str) -> bool:
    """소싱 매입가를 볼 수 있는가. **API 응답 자체를 가린다** —
    프론트에서 숨기는 것만으로는 API 를 직접 부르면 뚫린다."""
    return has_permission(role, PERM_VIEW_SOURCE_COST)


def can_download_expense_attachment(role: str) -> bool:
    """증빙 원본을 내려받을 수 있는가. 목록 조회 권한과 다르다."""
    return has_permission(role, PERM_VIEW_EXPENSE_ATTACHMENT)


# ── 민감 필드 ───────────────────────────────────────────────────
#
# 대상별로 나눈다. 필드명이 같아도 대상이 다르면 민감도가 다를 수 있다.
SENSITIVE_FIELDS: dict[str, frozenset[str]] = {
    "product": frozenset({
        # 원가 구성 — 이 값들이 곧 마진이다
        "cost_amount", "cost_amount_foreign", "cost_fx_rate", "cost_currency",
        # 설계 문서상 앞으로 들어올 원가 세부 (docs/46). 미리 등록해 두면
        # 컬럼이 생기는 순간 자동으로 보호된다 — 나중에 추가하는 걸 잊지 않도록.
        "purchase_price", "domestic_shipping_abroad", "international_shipping",
        "customs_duty", "import_vat", "domestic_shipping", "packaging_cost",
        "other_cost", "total_cost", "expected_margin",
    }),
    # 판매가는 원가와 함께 마진을 결정한다. 원가를 가리면서 판매가를 열어두면
    # 승인 화면에서 마진을 역산할 수 있어 가린 의미가 없다.
    "product_channel_listing": frozenset({
        "regular_price", "sale_price",
        # 마진 영향값이 응답에 들어오면 그것도 가려야 한다 — 미리 등록해 둔다.
        "margin_amount", "margin_rate", "expected_settlement", "expected_net_profit",
        "price_delta",
    }),
    # 사업비용. 금액은 파트너 수익배분의 입력값이고, 결제자·개인카드 여부는
    # **누가 얼마를 썼는지**라 사내 민감정보다.
    "expense": frozenset({
        "gross_amount", "supply_amount", "vat_amount",
        "payment_card_holder_id", "is_personal_card", "payment_method",
        "supplier_id", "attributable_partner_id",
        # 배분·상환 영향값. 컬럼이 생기면 자동으로 보호되도록 미리 등록한다.
        "allocation_amount", "reimbursement_amount", "settlement_impact",
    }),
}

# 화면 표시용 라벨. 값을 가려도 "무엇이 가려졌는지" 는 알려준다.
FIELD_LABELS: dict[str, str] = {
    "cost_amount": "원가",
    "cost_amount_foreign": "현지 매입가",
    "cost_fx_rate": "환율",
    "cost_currency": "매입 통화",
    "name_ko": "상품명",
    "name_en": "영문 상품명",
    "category_path": "카테고리",
    "supply_url": "소스 상품 URL",
    "supplier_id": "공급처",
    "status": "상품 상태",
    "margin_type": "마진 유형",
    # 판매처 가격
    "regular_price": "정상가",
    "sale_price": "할인가",
    "sale_start_at": "할인 시작",
    "sale_end_at": "할인 종료",
    "effective_from": "적용 시작일",
    "effective_to": "적용 종료일",
    "product_variant_id": "옵션",
    "currency": "통화",
    # 사업비용
    "gross_amount": "총액",
    "supply_amount": "공급가액",
    "vat_amount": "부가세",
    "expense_date": "비용 발생일",
    "category_id": "비용 카테고리",
    "payment_method": "결제수단",
    "payment_card_holder_id": "결제자",
    "is_personal_card": "개인카드 사용",
    "is_business_use": "업무용 여부",
    "is_tax_deductible": "세무 공제",
    "is_settlement_deductible": "정산 차감",
    "attributable_partner_id": "귀속 파트너",
    "evidence_type": "증빙 유형",
    "evidence_status": "증빙 상태",
    "description": "비용 내용",
}

REDACTED_MESSAGE = "권한이 없어 금액을 확인할 수 없습니다."


def is_sensitive(target_type: str, field: str) -> bool:
    return field in SENSITIVE_FIELDS.get(target_type, frozenset())


def redact_snapshot(
    target_type: str, snapshot: dict[str, Any] | None, *, allowed: bool
) -> dict[str, Any] | None:
    """스냅샷에서 민감 필드 **값을 제거**한다.

    키 자체는 남긴다 — 어떤 필드가 바뀌는지는 승인 판단에 필요하고,
    키를 지우면 "변경 없음" 과 "볼 수 없음" 이 구분되지 않는다.
    """
    if snapshot is None or allowed:
        return snapshot
    return {
        k: (None if is_sensitive(target_type, k) else v)
        for k, v in snapshot.items()
    }


def build_redacted_diff(
    target_type: str,
    before: dict[str, Any] | None,
    proposed: dict[str, Any] | None,
    *,
    allowed: bool,
) -> list[dict[str, Any]]:
    """필드별 비교 행 + redaction 표시.

    프론트가 이 구조를 그대로 렌더한다. 값이 없는 이유(`redacted`)를 함께 준다 —
    "값이 없음(null)" 과 "볼 권한이 없음" 은 다른 상태다.
    """
    b = before or {}
    p = proposed or {}
    rows: list[dict[str, Any]] = []
    for field in sorted(set(b) | set(p)):
        sensitive = is_sensitive(target_type, field)
        redacted = sensitive and not allowed
        before_val = b.get(field)
        after_val = p.get(field, before_val)
        rows.append({
            "field": field,
            "label": FIELD_LABELS.get(field, field),
            "sensitive": sensitive,
            "redacted": redacted,
            "before": None if redacted else before_val,
            "after": None if redacted else after_val,
            "changed": (field in p) and (after_val != before_val),
            **({"message": REDACTED_MESSAGE} if redacted else {}),
        })
    return rows


def redact_approval_row(row: dict[str, Any], *, allowed: bool) -> dict[str, Any]:
    """승인 요청 한 건의 민감값 제거 + diff 부착.

    목록·상세 API 가 **둘 다** 이 함수를 거친다. 목록만 가리고 상세를 열어두면
    상세 API 를 직접 부르는 것으로 뚫린다.
    """
    target_type = row.get("target_type", "")
    before = row.get("before_snapshot_json")
    proposed = row.get("proposed_changes_json")

    out = dict(row)
    out["before_snapshot_json"] = redact_snapshot(target_type, before, allowed=allowed)
    out["proposed_changes_json"] = redact_snapshot(target_type, proposed, allowed=allowed)
    out["diff"] = build_redacted_diff(target_type, before, proposed, allowed=allowed)
    out["sensitive_visible"] = allowed
    return out


def can_view_acquisition_cost(role: str) -> bool:
    """취득원가·발주금액을 볼 수 있나.

    라우터가 매번 `has_permission(role, PERM_...)` 를 쓰면 어느 권한을 봐야
    하는지 호출부마다 판단하게 된다. 이름 하나로 고정한다.
    """
    return has_permission(role, PERM_VIEW_ACQUISITION_COST)


#: 취득원가에서 **역산되는** 값들.
#:
#: 단가를 지웠는데 합계와 수량이 남으면 나눗셈 한 번으로 복원된다. 실제로
#: 흔한 실수라 이름을 목록으로 박아 둔다.
ACQUISITION_COST_FIELDS = (
    "unit_cost",          # 재고 원장 취득 단가
    "unit_price",         # 발주 단가
    "line_total",         # 발주 품목 금액  ← 수량으로 나누면 단가
    "total_amount",       # 발주 합계        ← 수량 합으로 나누면 평균 단가
    "estimated_amount",   # 재주문 예상 금액
    "price_basis",        # 어느 원가를 썼는지 (값 자체는 아니지만 단서다)
    "estimated_total",
)


def redact_acquisition_cost(payload: Any, role: str) -> Any:
    """권한이 없으면 금액과 **파생값**을 함께 지운다.

    합계만 지우고 단가를 남기거나 그 반대로 하면 남은 쪽으로 복원된다.
    그래서 목록 전체를 한 번에 지운다.

    dict/list 를 재귀로 훑는다 — 발주 상세처럼 중첩된 응답이 있다.
    """
    if can_view_acquisition_cost(role):
        return payload
    return _strip(payload)


def _strip(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: _strip(v) for k, v in node.items()
            if k not in ACQUISITION_COST_FIELDS
        }
    if isinstance(node, list):
        return [_strip(v) for v in node]
    return node
