"""Golden Sample — 사람이 승인한 기준값으로 계산 회귀를 잡는다.

왜 필요한가
----------
자동 대사는 "원천과 파생이 맞는가" 를 본다. 그런데 **원천 해석 자체가
틀렸다면** 대사는 여전히 통과한다. 예를 들어 배송비 매출을 매출에 넣을지
말지는 대사로 판정할 수 없다 — 업무 판단이다.

그래서 사람이 소수의 주문을 직접 확인해 "이 값이 맞다" 고 못박아 두고,
코드가 바뀐 뒤 그 값이 그대로인지 본다.

절대 하지 않는 것
--------------
```
현재 Seller OS 계산 결과 → expected 로 저장
```

이걸 하면 회귀 검사가 **항상 통과한다.** 계산이 어떻게 망가지든 기준값도
같이 망가지기 때문이다. 이 모듈에는 계산 결과를 expected 로 복사하는 경로가
아예 없다 — 승인은 호출자가 값을 명시적으로 넘겨야만 이루어진다.

후보를 고르는 것과 승인하는 것은 다르다
---------------------------------
후보 선정은 자동으로 한다(어떤 주문이 확인할 가치가 있는지는 데이터로 안다).
**승인은 사람만 한다.** 후보는 기준값 없이 `CANDIDATE` 로 만들어진다.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import ConflictError, NotFoundError, ValidationError

CANDIDATE, APPROVED, RETIRED = "CANDIDATE", "APPROVED", "RETIRED"
#: 사람이 보고 "지금은 못 정한다" 고 한 것. **실패가 아니다** (M060).
DEFERRED = "DEFERRED"

#: 보류 사유 — 사용자가 고르는 값. 화면이 문구를 붙인다.
DEFER_REASONS = (
    "SOURCE_MISSING",       # 원천자료가 부족하다
    "COST_UNCERTAIN",       # 원가를 더 확인해야 한다
    "FEE_ESTIMATED",        # 수수료가 추정이라 기준으로 삼기 이르다
    "CLAIM_IMPACT",         # 취소·반품 영향을 더 봐야 한다
    "OTHER",
)

#: 회귀 비교 결과.
PASS, FAIL, NOT_COMPARABLE = "PASS", "FAIL", "NOT_COMPARABLE"

#: 사람이 확인할 수 있는 기준값 항목. **전부 채울 필요 없다.**
#:
#: 정산서를 보면 매출·배송비매출·쿠폰·수수료를 알 수 있다. 원가는 매입
#: 기록을 봐야 하고, 마진은 그 둘의 결과다. 확인할 수 없는 항목은 넣지
#: 않는 것이 맞다 — 모르는 값을 0 으로 넣으면 그게 기준이 되어 버린다.
EXPECTED_FIELDS = (
    "product_revenue",
    "delivery_revenue",
    "seller_discount",
    "net_revenue",
    "marketplace_fee",
    "cost",
    "margin_amount",
)

#: 저장된 마진 컬럼 중 어느 것과 비교하는가.
_COMPARE_TO = {
    "net_revenue": "actual_revenue",
    "marketplace_fee": "actual_marketplace_fee",
    "cost": "actual_cost",
    "margin_amount": "actual_margin_amount",
}


# ══════════════════════════════════════════════════════════════════════
# 후보 선정 — 자동. 승인은 아니다.
# ══════════════════════════════════════════════════════════════════════

def propose_candidates(
    db: Session, *, org_id: UUID, limit: int = 20,
) -> list[dict[str, Any]]:
    """사람이 확인할 만한 주문을 골라 온다. **저장하지 않는다.**

    무엇을 고르나
    -----------
    서로 다른 상황을 대표하는 것들이다. 같은 모양 20건을 확인해 봐야 한 가지
    경우만 검증된다.

    ```
    SETTLEMENT_EXACT      정산이 정확히 붙은 주문        실측 수수료가 맞는가
    SETTLEMENT_WITH_SHIP  배송비 매출이 있는 주문        D1 이 맞는가
    SETTLEMENT_COUPON     판매자 쿠폰이 있는 주문        D2 가 맞는가
    ESTIMATED_ONLY        정산이 없어 정책 추정인 주문    추정 경로가 맞는가
    MULTI_LINE            한 주문에 라인이 여럿인 주문    합산이 맞는가
    CLAIM_LINKED          취소·반품이 붙은 주문          조정이 맞는가
    ```

    **실제로 있는 경우만 돌려준다.** 환불 정산 표본이 없으므로 환불 후보를
    만들지 않는다 — 확인할 원천이 없는 것을 확인하라고 할 수 없다.
    """
    picks: list[dict[str, Any]] = []
    seen: set[str] = set()

    def take(case_type: str, reason: str, sql: str, want: int) -> None:
        rows = db.execute(text(sql), {"o": str(org_id), "n": want}).mappings().all()
        for r in rows:
            sid = str(r["sales_order_id"])
            if sid in seen:
                continue
            seen.add(sid)
            picks.append({
                "sales_order_id": sid,
                "business_key": r.get("business_key"),
                "marketplace": r.get("marketplace"),
                "sale_date": r.get("sale_date"),
                "case_type": case_type,
                "selection_reason": reason,
                # 사람이 무엇을 열어 봐야 하는지.
                "check_source": r.get("check_source"),
                "check_amounts": r.get("check_amounts"),
                # 실측 근거가 있는가 — 최소 세트를 고를 때 이걸 우선한다.
                "has_settlement": case_type.startswith("SETTLEMENT_"),
            })

    matched = """
        SELECT so.id AS sales_order_id, so.business_key, so.marketplace, so.sale_date,
               '판매처 정산서' AS check_source,
               '상품매출 · 배송비매출 · 판매수수료' AS check_amounts
          FROM sales_order so
         WHERE so.org_id = :o AND so.deleted_at IS NULL
           AND EXISTS (SELECT 1 FROM marketplace_settlement_record m
                        WHERE m.matched_sales_order_id = so.id AND m.deleted_at IS NULL
                          AND m.matching_status = 'MATCHED_EXACT' {extra})
         ORDER BY so.sale_date DESC LIMIT :n
    """

    take("SETTLEMENT_EXACT",
         "정산서가 정확히 붙은 주문 — 실측 수수료가 마진에 제대로 반영됐는지 확인",
         matched.format(extra=""), 5)
    take("SETTLEMENT_WITH_SHIPPING",
         "배송비 매출이 있는 주문 — 배송비를 매출에 포함하는 결정(D1)이 맞는지 확인",
         matched.format(
             extra="AND m.row_type <> 'PRODUCT_SALE' AND m.gross_revenue > 0"), 3)
    take("SETTLEMENT_WITH_COUPON",
         "판매자 부담 쿠폰이 있는 주문 — 쿠폰을 매출 차감으로 보는 결정(D2)이 맞는지 확인",
         matched.format(extra="AND m.other_deductions > 0"), 3)

    take("ESTIMATED_ONLY",
         "정산서가 아직 없어 정책 요율로 추정한 주문 — 추정 경로가 맞는지 확인",
         """
        SELECT so.id AS sales_order_id, so.business_key, so.marketplace, so.sale_date,
               '판매 장부 + 마켓 수수료 정책' AS check_source,
               '판매금액 · 추정 수수료 · 원가' AS check_amounts
          FROM sales_order so
          JOIN product_margin_actual pma
            ON pma.sales_order_id = so.id AND pma.deleted_at IS NULL
         WHERE so.org_id = :o AND so.deleted_at IS NULL
           AND COALESCE(pma.calculation_snapshot_json->>'basis','') <> 'SETTLEMENT_ACTUAL'
           AND pma.actual_revenue > 0
         ORDER BY so.sale_date DESC LIMIT :n
    """, 4)

    take("MULTI_LINE",
         "한 주문번호에 라인이 여럿인 주문 — 라인별 합산이 맞는지 확인",
         """
        SELECT so.id AS sales_order_id, so.business_key, so.marketplace, so.sale_date,
               '판매 장부' AS check_source,
               '라인별 판매금액 합계' AS check_amounts
          FROM sales_order so
         WHERE so.org_id = :o AND so.deleted_at IS NULL
           AND EXISTS (SELECT 1 FROM sales_order o2
                        WHERE o2.org_id = so.org_id AND o2.deleted_at IS NULL
                          AND o2.marketplace_order_no = so.marketplace_order_no
                          AND o2.id <> so.id)
         ORDER BY so.sale_date DESC LIMIT :n
    """, 3)

    take("CLAIM_LINKED",
         "취소·반품이 붙은 주문 — 매출 조정이 맞는지 확인 (환불 정산 근거는 아직 없다)",
         """
        SELECT so.id AS sales_order_id, so.business_key, so.marketplace, so.sale_date,
               '판매처 클레임 화면' AS check_source,
               '취소·반품 수량과 매출 조정액' AS check_amounts
          FROM sales_order so
          JOIN sales_claim c ON c.sales_order_id = so.id AND c.deleted_at IS NULL
         WHERE so.org_id = :o AND so.deleted_at IS NULL
         ORDER BY so.sale_date DESC LIMIT :n
    """, 2)

    return picks[:limit]


def register_candidates(
    db: Session, *, org_id: UUID, actor_user_id: UUID,
    candidates: list[dict[str, Any]],
) -> dict[str, int]:
    """후보를 `CANDIDATE` 로 저장한다. **기준값은 넣지 않는다.**

    이 함수에는 `expected_json` 을 쓰는 경로가 없다. 승인은 사람이 값을
    들고 와야만 이루어진다.
    """
    created = skipped = 0
    for c in candidates:
        exists = db.execute(text("""
            SELECT 1 FROM verification_golden_case
             WHERE org_id = :o AND sales_order_id = :s AND deleted_at IS NULL
        """), {"o": str(org_id), "s": str(c["sales_order_id"])}).first()
        if exists:
            skipped += 1
            continue
        db.execute(text("""
            INSERT INTO verification_golden_case
                   (org_id, sales_order_id, case_type, selection_reason,
                    status, created_by, updated_by)
            VALUES (:o, :s, :t, :r, 'CANDIDATE', :a, :a)
        """), {"o": str(org_id), "s": str(c["sales_order_id"]),
               "t": c["case_type"], "r": c["selection_reason"],
               "a": str(actor_user_id)})
        created += 1
    db.commit()
    return {"created": created, "skipped_existing": skipped}


# ══════════════════════════════════════════════════════════════════════
# 승인 — 사람만
# ══════════════════════════════════════════════════════════════════════

def approve(
    db: Session, *, org_id: UUID, case_id: UUID, actor_user_id: UUID,
    expected: dict[str, Any], source_evidence: str,
    change_reason: str | None = None,
    evidence_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """사람이 확인한 기준값을 넣는다.

    호출자가 값을 반드시 준다
    ---------------------
    비어 있으면 거부한다. "현재 계산값으로 채워 줘" 같은 편의를 제공하지
    않는다 — 그 편의가 이 장치를 무의미하게 만드는 유일한 방법이다.

    이미 승인된 것을 고치면 이유가 필요하다
    ---------------------------------
    기준값을 조용히 덮으면 계산이 틀렸을 때 기준을 고쳐 통과시킬 수 있다.
    `revision` 을 올리고 사유를 남긴다.

    근거를 함께 박제한다 (M060)
    ------------------------
    `evidence_snapshot` 은 **승인 순간에 사용자가 본 것**이다. 원천이 나중에
    바뀌어도 그때의 사실이 남아 있어야, 회귀가 실패했을 때 "기준이 틀렸나
    계산이 바뀌었나" 를 가릴 수 있다.

    이 값은 회귀 비교에 쓰지 않는다 — 기준은 `expected_json` 하나뿐이다.
    """
    row = db.execute(text("""
        SELECT id, status, revision, expected_json
          FROM verification_golden_case
         WHERE id = :i AND org_id = :o AND deleted_at IS NULL
    """), {"i": str(case_id), "o": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("검증 기준 사례를 찾을 수 없습니다.")
    if row["status"] == RETIRED:
        raise ConflictError("보관 처리된 사례입니다. 다시 쓰려면 후보로 되돌리세요.")

    cleaned = _validate_expected(expected)
    if not source_evidence or not source_evidence.strip():
        raise ValidationError("무엇을 보고 확인했는지 적어야 합니다 (예: 정산서 파일명).")

    revision = int(row["revision"]) + 1
    if revision > 1 and not (change_reason or "").strip():
        raise ValidationError(
            "이미 승인된 기준값을 바꾸려면 이유가 필요합니다. "
            "기준을 조용히 바꾸면 회귀 검사가 무의미해집니다.")

    db.execute(text("""
        UPDATE verification_golden_case
           SET status = 'APPROVED',
               expected_json = CAST(:e AS JSONB),
               approved_evidence_json = CAST(:ev AS JSONB),
               source_evidence = :src,
               approved_at = now(), approved_by = :a,
               revision = :rev, change_reason = :why,
               -- 승인하면 보류 흔적은 지운다. 두 상태를 동시에 들 수 없다.
               defer_reason = NULL, deferred_at = NULL, deferred_by = NULL,
               updated_at = now(), updated_by = :a
         WHERE id = :i
    """), {"i": str(case_id), "e": json.dumps(cleaned, ensure_ascii=False),
           "ev": json.dumps(evidence_snapshot, ensure_ascii=False, default=str)
                 if evidence_snapshot else None,
           "src": source_evidence.strip(), "a": str(actor_user_id),
           "rev": revision, "why": (change_reason or "").strip() or None})

    # 기준값이 언제 왜 바뀌었는지 남는다. **이전 값도 함께** — 그래야 나중에
    # "언제부터 기준이 달라졌나" 를 되짚을 수 있다.
    record_audit(
        db, org_id=org_id, domain_table="verification_golden_case",
        domain_row_id=case_id, action="UPDATE", actor_user_id=actor_user_id,
        before={"revision": int(row["revision"]), "expected": row["expected_json"]},
        after={"revision": revision, "expected": cleaned},
        reason=change_reason or "기준값 최초 승인",
    )
    db.commit()
    return {"case_id": str(case_id), "status": APPROVED, "revision": revision}


def defer(
    db: Session, *, org_id: UUID, case_id: UUID, actor_user_id: UUID,
    reason: str, note: str | None = None,
) -> dict[str, Any]:
    """"봤고, 지금은 못 정한다" (§20·§21).

    **실패가 아니다.** 계산이 틀렸다는 뜻이 아니라 판단할 근거가 아직
    부족하다는 뜻이다. 회귀 검사는 승인된 것만 본다.

    사유를 받는 이유: 이유 없는 보류가 쌓이면 나중에 왜 미뤘는지 아무도
    모르고, 결국 전부 다시 확인하게 된다.
    """
    row = db.execute(text("""
        SELECT id, status FROM verification_golden_case
         WHERE id = :i AND org_id = :o AND deleted_at IS NULL
    """), {"i": str(case_id), "o": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("검증 기준 사례를 찾을 수 없습니다.")
    if row["status"] == APPROVED:
        # 승인된 기준을 보류로 되돌리면 회귀가 조용히 약해진다.
        raise ConflictError(
            "이미 승인된 기준입니다. 기준을 바꾸려면 사유와 함께 다시 승인하세요.")
    if reason not in DEFER_REASONS:
        raise ValidationError(
            f"알 수 없는 보류 사유입니다. 가능: {', '.join(DEFER_REASONS)}")

    db.execute(text("""
        UPDATE verification_golden_case
           SET status = 'DEFERRED', defer_reason = :r,
               deferred_at = now(), deferred_by = :a,
               note = :note, updated_at = now(), updated_by = :a
         WHERE id = :i
    """), {"i": str(case_id), "r": reason, "a": str(actor_user_id),
           "note": (note or "").strip() or None})

    record_audit(
        db, org_id=org_id, domain_table="verification_golden_case",
        domain_row_id=case_id, action="STATUS_CHANGE", actor_user_id=actor_user_id,
        before={"status": row["status"]},
        after={"status": DEFERRED, "reason": reason},
        reason=note or reason,
    )
    db.commit()
    return {"case_id": str(case_id), "status": DEFERRED, "reason": reason}


#: 최소 커버리지를 이루는 케이스 — **계산 경로가 서로 다른 것들**이다.
#:
#: 같은 모양을 여러 건 확인해 봐야 한 가지 경로만 검증된다. 아래 각각은
#: 지나는 코드가 다르다.
MINIMUM_COVERAGE = (
    ("SETTLEMENT_EXACT", "실측 수수료로 계산되는 기본 경로"),
    ("SETTLEMENT_WITH_SHIPPING", "배송비 매출을 매출에 넣는 결정(D1)"),
    ("SETTLEMENT_WITH_COUPON", "판매자 쿠폰을 매출에서 빼는 결정(D2)"),
    ("ESTIMATED_ONLY", "정산서 없이 정책 요율로 추정하는 경로"),
    ("MULTI_LINE", "한 주문번호가 여러 라인으로 나뉜 경우의 합산"),
    ("CLAIM_LINKED", "취소·반품이 붙은 주문 (환불 손익은 미검증)"),
)


def recommend_minimum_set(
    db: Session, *, org_id: UUID, candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """"이것만 먼저 확인하면 주요 계산 경로를 덮습니다" (§32).

    16건을 전부 보라고 하면 아무도 시작하지 않는다. 경로가 서로 다른 것을
    **유형당 하나씩** 고른다.

    고르는 기준
    ---------
    같은 유형 안에서는 **실측 근거가 있는 것**을 먼저 고른다. 추정으로
    기준을 세우면 나중에 정산서가 오면서 기준 자체가 흔들린다.
    """
    by_type: dict[str, list[dict[str, Any]]] = {}
    for c in candidates:
        by_type.setdefault(c["case_type"], []).append(c)

    picked: list[dict[str, Any]] = []
    missing: list[dict[str, str]] = []
    for case_type, why in MINIMUM_COVERAGE:
        pool = by_type.get(case_type) or []
        if not pool:
            missing.append({"case_type": case_type, "reason": "이 경우가 실제 데이터에 없다"})
            continue
        # 실측이 있는 것 우선. 없으면 그냥 첫 번째.
        pool = sorted(pool, key=lambda c: 0 if c.get("has_settlement") else 1)
        chosen = dict(pool[0])
        chosen["covers"] = why
        picked.append(chosen)

    return {
        "recommended": picked,
        "recommended_count": len(picked),
        "total_candidates": len(candidates),
        "uncovered": missing,
        "note": (
            "이 목록은 서로 다른 계산 경로를 하나씩 덮는다. 나머지는 같은 "
            "경로를 반복하므로 나중에 확인해도 된다."
        ),
    }


def _validate_expected(expected: dict[str, Any]) -> dict[str, Any]:
    """사람이 넣은 값만 남긴다. 모르는 항목은 넣지 않는 것이 맞다."""
    if not isinstance(expected, dict) or not expected:
        raise ValidationError("확인한 기준값을 하나 이상 넣어야 합니다.")
    unknown_fields = set(expected) - set(EXPECTED_FIELDS)
    if unknown_fields:
        raise ValidationError(
            f"알 수 없는 항목입니다: {', '.join(sorted(unknown_fields))}. "
            f"가능: {', '.join(EXPECTED_FIELDS)}")
    cleaned: dict[str, Any] = {}
    for key, value in expected.items():
        if value is None:
            # 모른다는 뜻. 0 으로 바꾸지 않는다 — 그러면 0 이 기준이 된다.
            cleaned[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(
                f"{key} 는 원 단위 정수여야 합니다. 모르면 비워 두세요.")
        cleaned[key] = value
    if all(v is None for v in cleaned.values()):
        raise ValidationError("확인한 값이 하나도 없습니다.")
    return cleaned


# ══════════════════════════════════════════════════════════════════════
# 회귀 — 승인된 기준값 vs 지금 계산 결과
# ══════════════════════════════════════════════════════════════════════

def regression(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """승인된 기준값과 현재 저장된 계산 결과를 비교한다.

    **원 단위로 정확히** 본다. 허용 오차를 두면 그 오차 안에서 계산이 조용히
    망가진다.

    비교할 수 없으면 실패가 아니다
    -------------------------
    마진 행이 아직 없거나, 기준값에 그 항목이 없으면 `NOT_COMPARABLE` 이다.
    PASS 로 세지 않는다.
    """
    rows = db.execute(text("""
        SELECT g.id, g.sales_order_id, g.case_type, g.expected_json, g.revision,
               g.approved_at, so.business_key, so.marketplace,
               pma.actual_revenue, pma.actual_marketplace_fee,
               pma.actual_cost, pma.actual_margin_amount
          FROM verification_golden_case g
          JOIN sales_order so ON so.id = g.sales_order_id
          LEFT JOIN product_margin_actual pma
                 ON pma.sales_order_id = g.sales_order_id AND pma.deleted_at IS NULL
         WHERE g.org_id = :o AND g.deleted_at IS NULL AND g.status = 'APPROVED'
         ORDER BY g.case_type, so.sale_date
    """), {"o": str(org_id)}).mappings().all()

    cases: list[dict[str, Any]] = []
    counts = {PASS: 0, FAIL: 0, NOT_COMPARABLE: 0}
    for r in rows:
        expected = r["expected_json"] or {}
        checks: list[dict[str, Any]] = []
        comparable = False
        failed = False

        if r["actual_revenue"] is None:
            verdict = NOT_COMPARABLE
            reason = "이 주문의 실 마진이 아직 계산되지 않았다"
        else:
            for field, column in _COMPARE_TO.items():
                want = expected.get(field)
                if want is None:
                    continue  # 사람이 확인하지 않은 항목 — 비교하지 않는다
                got = int(r[column])
                comparable = True
                ok = got == want
                failed = failed or not ok
                checks.append({
                    "field": field, "expected": want, "actual": got,
                    "difference": got - want, "verdict": PASS if ok else FAIL,
                })
            if not comparable:
                verdict, reason = NOT_COMPARABLE, "기준값에 비교 가능한 항목이 없다"
            else:
                verdict, reason = (FAIL, None) if failed else (PASS, None)

        counts[verdict] += 1
        cases.append({
            "case_id": str(r["id"]),
            "sales_order_id": str(r["sales_order_id"]),
            "business_key": r["business_key"],
            "marketplace": r["marketplace"],
            "case_type": r["case_type"],
            "revision": r["revision"],
            "approved_at": r["approved_at"],
            "verdict": verdict,
            "reason": reason,
            "checks": checks,
        })

    return {
        "approved_cases": len(rows),
        "counts": counts,
        "cases": cases,
        # 승인된 사례가 없으면 회귀 검사가 아무것도 지키지 못한다.
        "status": (
            FAIL if counts[FAIL] else
            (PASS if counts[PASS] else NOT_COMPARABLE)
        ),
    }


def list_cases(db: Session, *, org_id: UUID) -> list[dict[str, Any]]:
    """등록된 사례 목록. 승인 전이면 기준값은 비어 있다."""
    rows = db.execute(text("""
        SELECT g.id, g.sales_order_id, g.case_type, g.selection_reason, g.status,
               g.expected_json, g.source_evidence, g.approved_at, g.revision,
               g.change_reason, g.defer_reason, g.deferred_at, g.note,
               g.approved_evidence_json IS NOT NULL AS has_evidence_snapshot,
               so.business_key, so.marketplace, so.sale_date
          FROM verification_golden_case g
          JOIN sales_order so ON so.id = g.sales_order_id
         WHERE g.org_id = :o AND g.deleted_at IS NULL
         ORDER BY g.status, g.case_type, so.sale_date DESC
    """), {"o": str(org_id)}).mappings().all()
    return [
        {
            "case_id": str(r["id"]),
            "sales_order_id": str(r["sales_order_id"]),
            "business_key": r["business_key"],
            "marketplace": r["marketplace"],
            "sale_date": r["sale_date"],
            "case_type": r["case_type"],
            "selection_reason": r["selection_reason"],
            "status": r["status"],
            "expected": r["expected_json"],
            "source_evidence": r["source_evidence"],
            "approved_at": r["approved_at"],
            "revision": r["revision"],
            "change_reason": r["change_reason"],
            "defer_reason": r["defer_reason"],
            "deferred_at": r["deferred_at"],
            "note": r["note"],
            # 승인 당시 근거가 박제돼 있나 (M060 이전 승인은 없다).
            "has_evidence_snapshot": bool(r["has_evidence_snapshot"]),
        }
        for r in rows
    ]


def coverage(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """유형별로 몇 건이 승인됐나 (§31).

    "6/16 승인" 만 보면 무엇이 아직 안 덮였는지 모른다. 경로별로 본다.
    """
    rows = db.execute(text("""
        SELECT case_type, status, count(*) AS n
          FROM verification_golden_case
         WHERE org_id = :o AND deleted_at IS NULL
         GROUP BY 1, 2
    """), {"o": str(org_id)}).mappings().all()

    by_type: dict[str, dict[str, int]] = {}
    for r in rows:
        bucket = by_type.setdefault(r["case_type"], {})
        bucket[r["status"]] = int(r["n"])

    out = []
    for case_type, why in MINIMUM_COVERAGE:
        b = by_type.get(case_type, {})
        out.append({
            "case_type": case_type,
            "covers": why,
            "approved": b.get(APPROVED, 0),
            "deferred": b.get(DEFERRED, 0),
            "candidate": b.get(CANDIDATE, 0),
            "covered": b.get(APPROVED, 0) > 0,
        })
    return {
        "by_case_type": out,
        "covered": sum(1 for x in out if x["covered"]),
        "of": len(out),
    }
