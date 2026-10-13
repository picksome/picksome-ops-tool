"""재무 정책 변경 승인 (Phase A-2).

왜 정책이 승인 대상인가
--------------------
세율과 수수료는 **계산의 입력값**이다.

```
세율·수수료 → 마진 → 순이익 → 월 마감 → 수익배분
```

한 값이 바뀌면 파트너에게 실제로 나가는 금액이 바뀐다. 상품 원가·사업비용을
이미 승인 대상으로 두고 있으면서 그 둘을 곱하고 빼는 **비율**을 아무나 바꿀 수
있게 두면 승인 체계 전체가 장식이 된다.

적용은 UPDATE 가 아니라 새 version 이다
-----------------------------------
정책 행을 그 자리에서 고치면 **과거가 바뀐다.** 2026-07 확정 snapshot 이
`marketplace_fee_policy v7` 을 근거로 기록돼 있는데 v7 의 수수료율을 8%에서 10%로
고치면, 그 달을 재현할 때 다른 숫자가 나온다.

그래서 승인 적용은:

```
기존 version   effective_to = 적용일 - 1일   (닫는다)
새 version     effective_from = 적용일       (version + 1)
```

과거 월은 자기 시점에 유효했던 version 을 계속 가리킨다.

무엇을 승인 대상으로 두지 않았나
----------------------------
`memo` / `source_url` / `reliability_status` 는 금액에 영향이 없다. 근거 링크를
고치는 데 승인을 요구하면 사람들은 승인 자체를 우회할 방법을 찾는다.
**금액을 움직이는 필드만** 잠근다.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ValidationError

# 정책 종류 → (승인 대상 target_type, 요청 유형, 금액을 움직이는 필드)
#
# **여기 없는 정책 종류는 승인 대상이 아니다.** 존재하지 않는 정책을 만들어
# 넣지 않는다 — 실제 스키마(`policy_crud.ALLOWED_COLUMNS`)에 있는 것만 다룬다.
FINANCIAL_POLICIES: dict[str, tuple[str, str, frozenset[str]]] = {
    "tax": (
        "policy_tax",
        "TAX_POLICY_CHANGE",
        frozenset({
            "tax_type", "business_type", "vat_rate", "calculation_method",
            "default_vat_inclusion", "applies_to",
            "input_vat_credit_policy", "import_vat_credit_policy",
        }),
    ),
    "marketplace_fee": (
        "policy_marketplace_fee",
        "FEE_POLICY_CHANGE",
        frozenset({
            "marketplace", "category_path", "fee_type",
            "rate", "fixed_amount", "tier_json", "fee_vat_included",
        }),
    ),
    "payment_fee": (
        "policy_payment_fee",
        "FEE_POLICY_CHANGE",
        frozenset({"method", "rate"}),
    ),
    "partner_share": (
        "policy_partner_share",
        "DISTRIBUTION_POLICY_CHANGE",
        # 배분 비율은 곧 사람에게 나가는 돈이다.
        frozenset({"partner_id", "share_ratio", "applies_year_month"}),
    ),
    "settlement": (
        "policy_settlement",
        "DISTRIBUTION_POLICY_CHANGE",
        frozenset({"loss_policy", "priority_distribution_json", "close_day"}),
    ),
}

TARGET_TYPE_TO_KIND: dict[str, str] = {
    tt: kind for kind, (tt, _rt, _f) in FINANCIAL_POLICIES.items()
}


def approval_fields_for_kind(kind: str) -> frozenset[str]:
    """이 정책 종류에서 승인 없이 바꿀 수 없는 필드."""
    entry = FINANCIAL_POLICIES.get(kind)
    return entry[2] if entry else frozenset()


class PolicyAdapter:
    """정책 한 종류에 대한 승인 어댑터.

    종류마다 테이블과 컬럼이 달라 인스턴스를 따로 만든다. 로직은 같다 —
    한 클래스에 `if kind == ...` 를 늘어놓으면 새 정책이 생길 때마다 분기가 는다.
    """

    def __init__(self, kind: str) -> None:
        from app.services.policy_crud import KIND_TABLE

        target_type, request_type, fields = FINANCIAL_POLICIES[kind]
        self.kind = kind
        self.table = KIND_TABLE[kind]
        self.target_type = target_type
        self.request_type = request_type
        self.allowed_fields = fields
        self.request_types = frozenset({request_type})
        self.FIELD_OWNERSHIP = {request_type: fields}

    def fields_for(self, request_type: str) -> frozenset[str]:
        return self.FIELD_OWNERSHIP.get(request_type, frozenset())

    # ── 대상 ────────────────────────────────────────────────
    def _row(self, db: Session, org_id: UUID, target_id: UUID):
        cols = ", ".join(sorted(self.allowed_fields))
        row = db.execute(text(f"""
            SELECT id, version, effective_from, effective_to, is_active, {cols}
              FROM {self.table}
             WHERE id = :id AND org_id = :org AND deleted_at IS NULL
        """), {"id": str(target_id), "org": str(org_id)}).mappings().first()
        if row is None:
            # 조직 격리 — 다른 org 의 정책도 404 다.
            raise NotFoundError(f"{self.kind} 정책을 찾을 수 없습니다.")
        return row

    def validate_target(self, db: Session, *, org_id: UUID, target_id: UUID) -> None:
        self._row(db, org_id, target_id)

    def build_snapshot(self, db: Session, *, org_id: UUID, target_id: UUID) -> dict[str, Any]:
        row = self._row(db, org_id, target_id)
        snap = {k: _flatten(k, row[k]) for k in sorted(self.allowed_fields)}
        # version 을 스냅샷에 넣는다 — 승인 대기 중 다른 사람이 새 version 을
        # 만들면 지문이 달라져 stale 요청이 걸린다.
        snap["version"] = row["version"]
        return snap

    def get_current_version(self, db: Session, *, org_id: UUID, target_id: UUID) -> str:
        # 늦은 import — `approval_targets` 가 이 모듈의 어댑터를 등록하므로
        # 모듈 최상단에서 가져오면 순환이 된다.
        from app.services.approval_targets import version_of

        return version_of(self.build_snapshot(db, org_id=org_id, target_id=target_id))

    def get_effective_date(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], effective_from: date | None,
    ) -> date:
        """새 version 이 유효해지는 날.

        요청자가 지정하지 않으면 오늘이다. **과거 날짜를 그대로 받지 않는다** —
        이미 확정한 달의 계산 근거를 소급해서 바꾸게 된다.
        """
        _ = (db, org_id, target_id, proposed)
        return effective_from or date.today()

    # ── 검증 ────────────────────────────────────────────────
    def validate_proposed_changes(
        self, proposed: dict[str, Any], *, request_type: str | None = None
    ) -> None:
        if not proposed:
            raise ValidationError("변경할 내용이 없습니다.")
        unknown = sorted(set(proposed) - self.allowed_fields)
        if unknown:
            raise ValidationError(
                f"승인으로 변경할 수 없는 필드입니다: {unknown}",
                detail={"allowed": sorted(self.allowed_fields)},
            )
        if request_type is not None and request_type not in self.request_types:
            raise ValidationError(
                f"이 대상에서 지원하지 않는 요청 유형입니다: {request_type}",
                detail={"allowed_request_types": sorted(self.request_types)},
            )

        if "vat_rate" in proposed:
            _assert_rate(proposed["vat_rate"], "vat_rate")
        if "rate" in proposed and proposed["rate"] is not None:
            _assert_rate(proposed["rate"], "rate")
        if "share_ratio" in proposed and proposed["share_ratio"] is not None:
            _assert_rate(proposed["share_ratio"], "share_ratio")
        if proposed.get("fixed_amount") is not None and int(proposed["fixed_amount"]) < 0:
            raise ValidationError("정액 수수료는 음수가 될 수 없습니다.")
        if proposed.get("tier_json") is not None:
            validate_tiers(_parse_tiers(proposed["tier_json"]))

    # ── 적용 ────────────────────────────────────────────────
    def apply_changes(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], actor_user_id: UUID,
        request_type: str | None = None, approval_request_id: UUID | None = None,
        effective_from: date | None = None,
    ) -> dict[str, Any]:
        """**기존 행을 고치지 않는다.** 새 version 을 만들고 옛 version 을 닫는다.

        그 자리에서 UPDATE 하면 그 정책을 근거로 이미 확정된 달의 숫자를 재현할 수
        없게 된다. 확정은 "그때 그 값" 이라는 뜻이고, 근거가 바뀌면 확정이 아니다.
        """
        from app.core.audit import record_audit
        from app.services.policy_crud import JSONB_COLUMNS, _bind_value

        self.validate_proposed_changes(proposed, request_type=request_type)

        current = self._row(db, org_id, target_id)
        start = effective_from or date.today()

        if current["effective_from"] > start:
            raise ValidationError(
                "새 정책의 적용일이 기존 정책 시작일보다 빠릅니다. "
                f"기존 정책은 {current['effective_from']} 부터 유효합니다.",
                detail={"existing_effective_from": current["effective_from"].isoformat()},
            )

        next_version = db.execute(text(f"""
            SELECT COALESCE(MAX(version), 0) + 1 FROM {self.table} WHERE org_id = :org
        """), {"org": str(org_id)}).scalar_one()

        merged = {k: current[k] for k in sorted(self.allowed_fields)}
        merged.update(proposed)
        if merged.get("tier_json") is not None:
            # 요청은 평면 스칼라만 담을 수 있어 JSON 문자열로 온다(domain/approval).
            # DB 에는 JSONB 로 넣어야 하므로 여기서 되돌린다.
            merged["tier_json"] = _parse_tiers(merged["tier_json"])

        cols = sorted(self.allowed_fields)
        placeholders = [
            f"CAST(:{c} AS JSONB)" if c in JSONB_COLUMNS else f":{c}" for c in cols
        ]
        params: dict[str, Any] = {
            "org": str(org_id), "v": next_version, "from": start,
            "actor": str(actor_user_id),
            "memo": f"승인 적용 (요청 {approval_request_id})" if approval_request_id else "승인 적용",
        }
        for c in cols:
            params[c] = _bind_value(c, merged[c])

        new_id = db.execute(text(f"""
            INSERT INTO {self.table}
                   (org_id, version, effective_from, effective_to, is_active,
                    source_type, reliability_status, memo,
                    created_by, updated_by, {', '.join(cols)})
            VALUES (:org, :v, :from, NULL, TRUE,
                    'MANUAL', 'CONFIRMED', :memo,
                    :actor, :actor, {', '.join(placeholders)})
            RETURNING id
        """), params).scalar_one()

        # 옛 version 은 지우지 않는다 — 과거 계산의 근거로 남아야 한다.
        # 적용일 **전날**까지 유효하다(effective_to 는 포함 경계다).
        db.execute(text(f"""
            UPDATE {self.table}
               SET effective_to = :to, is_active = FALSE,
                   updated_at = now(), updated_by = :actor
             WHERE id = :id AND org_id = :org
        """), {"id": str(target_id), "org": str(org_id),
               "to": start - timedelta(days=1), "actor": str(actor_user_id)})

        record_audit(
            db, org_id=org_id, domain_table=self.table,
            domain_row_id=UUID(str(new_id)), action="CREATE",
            actor_user_id=actor_user_id,
            before={"superseded_policy_id": str(target_id), "version": current["version"]},
            after={"version": next_version, "effective_from": start.isoformat(),
                   **{k: str(v) for k, v in proposed.items()}},
            reason="재무 정책 변경 승인 적용",
        )

        return self.build_snapshot(db, org_id=org_id, target_id=UUID(str(new_id)))


def _flatten(col: str, value: Any) -> Any:
    """스냅샷 값을 평면 스칼라로.

    `tier_json` 은 DB 에서 리스트로 나오는데, 승인 요청의 제안 값은 평면 스칼라만
    담을 수 있다(중첩을 허용하면 어떤 컬럼이 바뀌는지 정적으로 알 수 없다 —
    `domain/approval.ProposedChanges`). 양쪽을 같은 모양으로 맞춰야 변경 전후
    비교가 의미를 갖는다.
    """
    from app.services.policy_crud import JSONB_COLUMNS

    if col in JSONB_COLUMNS and value is not None and not isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return value


def _parse_tiers(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"구간 정보를 읽을 수 없습니다: {exc.msg}") from exc
    return value


def _assert_rate(value: Any, field: str) -> None:
    """비율은 0~1 이다.

    10.8% 를 `10.8` 로 넣는 실수가 흔하다. 그대로 통과시키면 수수료가 1080% 가 되고,
    화면에는 마진이 크게 음수로 나온다 — 사용자는 원가가 잘못됐다고 생각한다.
    """
    v = float(value)
    if v < 0:
        raise ValidationError(f"{field} 는 음수가 될 수 없습니다.")
    if v > 1:
        raise ValidationError(
            f"{field} 는 0~1 사이 비율입니다. 10.8% 는 0.108 입니다 (입력값 {v}).",
            detail={"field": field, "value": v},
        )


def validate_tiers(tiers: Any) -> None:
    """구간별 수수료 검증 (Phase A-3).

    구간이 겹치거나 비면 같은 판매가에 대해 수수료가 두 개거나 하나도 없게 된다.
    **프론트에서만 막으면 API 를 직접 부르는 순간 뚫린다** — 여기서 막는다.
    """
    if not isinstance(tiers, list) or not tiers:
        raise ValidationError("구간을 하나 이상 입력하세요.")

    parsed: list[tuple[int, int | None, float]] = []
    for i, t in enumerate(tiers, start=1):
        if not isinstance(t, dict):
            raise ValidationError(f"{i}번째 구간의 형식이 올바르지 않습니다.")
        if "min" not in t or "rate" not in t:
            raise ValidationError(f"{i}번째 구간에 최소 금액과 수수료율이 필요합니다.")
        lo = int(t["min"])
        hi = None if t.get("max") in (None, "") else int(t["max"])
        _assert_rate(t["rate"], f"{i}번째 구간 수수료율")
        if lo < 0:
            raise ValidationError(f"{i}번째 구간의 최소 금액이 음수입니다.")
        if hi is not None and hi <= lo:
            raise ValidationError(
                f"{i}번째 구간의 최대 금액이 최소 금액보다 작거나 같습니다."
            )
        parsed.append((lo, hi, float(t["rate"])))

    parsed.sort(key=lambda x: x[0])

    if parsed[0][0] != 0:
        raise ValidationError(
            f"첫 구간은 0원부터 시작해야 합니다 (현재 {parsed[0][0]:,}원). "
            "그 아래 금액의 수수료를 알 수 없습니다."
        )

    for (lo, hi, _), (next_lo, _, _) in zip(parsed, parsed[1:], strict=False):
        if hi is None:
            raise ValidationError(
                "상한 없는 구간은 마지막에 하나만 올 수 있습니다."
            )
        # 포함 경계다: [min, max]. 다음 구간은 max + 1 에서 시작해야 한다.
        if next_lo <= hi:
            raise ValidationError(
                f"구간이 겹칩니다: {lo:,}~{hi:,} 와 {next_lo:,} 부터가 겹칩니다."
            )
        if next_lo > hi + 1:
            raise ValidationError(
                f"구간 사이가 비었습니다: {hi:,}원과 {next_lo:,}원 사이의 "
                "수수료를 알 수 없습니다."
            )

    if parsed[-1][1] is not None:
        raise ValidationError(
            f"마지막 구간은 상한이 없어야 합니다 (현재 {parsed[-1][1]:,}원까지). "
            "그보다 큰 금액의 수수료를 알 수 없습니다."
        )


def build_adapters() -> dict[str, PolicyAdapter]:
    return {
        target_type: PolicyAdapter(kind)
        for kind, (target_type, _rt, _f) in FINANCIAL_POLICIES.items()
    }
