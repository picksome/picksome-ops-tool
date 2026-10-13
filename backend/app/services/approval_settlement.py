"""정산 레코드 수동 매칭 승인 어댑터 (docs/47 §3).

왜 승인 대상인가
--------------
정산 건을 주문에 강제로 연결하는 것은 **손익 귀속을 바꾸는 행위**다. 그
정산금이 어느 상품·어느 달의 이익이 되는지가 달라지고, 그 이익이 파트너
수익배분이 된다. 비용 금액 변경과 같은 무게다.

무엇이 승인이고 무엇이 아닌가
--------------------------
```
주문 연결 · 연결 해제      승인 필요   손익 귀속이 바뀐다
EXCLUDED 표시             승인 불요   금액에 영향 없고, 되돌리기 쉽고, 매일 나온다
```

두 번째를 승인으로 만들면 광고비 정산행 하나 제외하는 데 결재가 필요해지고,
사용자는 곧 대사 자체를 안 하게 된다. (`settlement_matching.mark_excluded`)

payload 가 `matching_status` 를 직접 지정하지 않는다
------------------------------------------------
요청이 상태 문자열을 들고 오면 `MATCHED_EXACT` 를 꽂을 수 있다 — 규칙이
찾은 것처럼 위장한 수동 연결이 되고, 재판정이 그걸 건드리지 않는다
(`settlement_matching.HUMAN_DECIDED` 는 `MATCHED_MANUAL` 만 보호한다).

그래서 payload 는 **무엇에 붙일지**만 말하고, 상태는 어댑터가 정한다.
`MonthReopenAdapter` 가 `close_state` 대신 `reopen_to_state` 를 쓰는 것과
같은 이유다.

```
matched_sales_order_id = <주문>   →  MATCHED_MANUAL
matched_sales_order_id = null     →  UNMATCHED  (연결 해제)
```

판매처가 다른 주문에는 붙이지 못한다
--------------------------------
두 행 모두 판매처를 스스로 선언한다. 그게 다르면 둘 중 하나가 틀린 것이고,
마감 검증도 같은 상황을 BLOCKING 으로 잡는다
(`SETTLEMENT_ACCOUNT_MARKETPLACE_MISMATCH`). 사람의 판단을 의심하는 게
아니라, 승인해도 곧바로 마감이 막히는 연결을 만들지 않는 것이다.

**주문번호가 같은지는 보지 않는다.** 번호가 안 맞아서 수동 매칭을 하는 것이다.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ValidationError

MIN_REASON_LENGTH = 5


class SettlementMatchAdapter:
    """`settlement_record` 대상 — 주문 연결/해제."""

    target_type = "settlement_record"

    FIELD_OWNERSHIP: dict[str, frozenset[str]] = {
        "SETTLEMENT_MANUAL_MATCH": frozenset({
            "matched_sales_order_id", "match_reason",
        }),
    }
    allowed_fields = frozenset().union(*FIELD_OWNERSHIP.values())
    request_types = frozenset(FIELD_OWNERSHIP)

    def fields_for(self, request_type: str) -> frozenset[str]:
        return self.FIELD_OWNERSHIP.get(request_type, frozenset())

    _SELECT = """
        SELECT id, marketplace, marketplace_order_no, settled_at,
               settlement_period_start, settlement_period_end,
               gross_revenue, net_settlement,
               matching_status, matched_sales_order_id, match_reason,
               confirmed_at
          FROM marketplace_settlement_record
         WHERE id = :id AND org_id = :org AND deleted_at IS NULL
    """

    def _row(self, db: Session, org_id: UUID, target_id: UUID):
        row = db.execute(
            text(self._SELECT), {"id": str(target_id), "org": str(org_id)}
        ).mappings().first()
        if row is None:
            # 조직 격리 — 다른 org 의 레코드도 404 다.
            raise NotFoundError("정산 레코드를 찾을 수 없습니다.")
        return row

    def validate_target(self, db: Session, *, org_id: UUID, target_id: UUID) -> None:
        row = self._row(db, org_id, target_id)
        if row["confirmed_at"] is not None:
            # 확정한 정산 건의 귀속을 바꾸는 것은 이 요청이 아니라 조정
            # (`settlement_adjustment`)의 일이다. 여기서 열어 주면 확정이
            # 아무 뜻도 갖지 않는다.
            raise ValidationError(
                "이미 확정된 정산 건입니다. 정정은 정산 조정으로 처리하세요.",
                detail={"confirmed_at": row["confirmed_at"]},
            )

    def build_snapshot(self, db: Session, *, org_id: UUID, target_id: UUID) -> dict[str, Any]:
        row = self._row(db, org_id, target_id)
        # 승인자가 **무엇을 어디에 붙이는지** 보고 판단해야 한다. 허용 필드만
        # 담으면 금액이 안 보여서 판단할 근거가 없다.
        return {
            "matched_sales_order_id": row["matched_sales_order_id"],
            "match_reason": row["match_reason"],
            "matching_status": row["matching_status"],
            "marketplace": row["marketplace"],
            "marketplace_order_no": row["marketplace_order_no"],
            "net_settlement": row["net_settlement"],
            "settlement_period": f"{row['settlement_period_start']}"
                                 f" ~ {row['settlement_period_end']}",
        }

    def get_current_version(self, db: Session, *, org_id: UUID, target_id: UUID) -> str:
        # 지연 import — `approval_targets` 가 이 모듈을 등록하므로 최상단에서
        # 부르면 순환이 된다(`approval_policy` 도 같은 이유로 이렇게 한다).
        from app.services.approval_targets import version_of

        return version_of(self.build_snapshot(db, org_id=org_id, target_id=target_id))

    def get_effective_date(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], effective_from: date | None,
    ) -> date:
        """회계 귀속일 = **정산일**. 없으면 정산 기간의 끝.

        요청을 낸 날이 아니다. 8월에 7월 정산서를 연결하는 것은 흔한 일이고,
        그건 7월이 잠겼는지로 판정해야 맞다.

        기간 끝을 쓰는 이유: 쿠팡 주정산처럼 기간이 월 경계를 넘을 때
        (7/29~8/4) 시작일을 쓰면 8월에 들어온 정산금이 7월 잠금에 걸린다.
        정산금이 실제로 확정되는 쪽은 기간의 끝이다.
        """
        _ = (proposed, effective_from)
        row = self._row(db, org_id, target_id)
        return row["settled_at"] or row["settlement_period_end"]

    def validate_proposed_changes(
        self, proposed: dict[str, Any], *, request_type: str | None = None
    ) -> None:
        if not proposed:
            raise ValidationError("변경할 내용이 없습니다.")
        unknown = set(proposed) - self.allowed_fields
        if unknown:
            raise ValidationError(
                f"승인으로 변경할 수 없는 필드입니다: {sorted(unknown)}",
                detail={"allowed": sorted(self.allowed_fields)},
            )
        if request_type is not None and request_type not in self.request_types:
            raise ValidationError(
                f"{request_type} 요청으로는 정산 매칭을 바꿀 수 없습니다.",
                detail={"allowed_request_types": sorted(self.request_types)},
            )

        # 연결이든 해제든 **사유가 필수**다. 해제 쪽을 빼면 "왜 풀었는지"
        # 가 안 남고, 다음 판정이 다시 붙여도 아무도 이상하게 여기지 않는다.
        if "match_reason" not in proposed:
            raise ValidationError(
                "매칭 사유는 필수입니다.", detail={"field": "match_reason"})
        reason = (proposed.get("match_reason") or "").strip()
        if len(reason) < MIN_REASON_LENGTH:
            raise ValidationError(
                f"매칭 사유를 {MIN_REASON_LENGTH}자 이상 입력하세요.",
                detail={"field": "match_reason"},
            )

        # 키가 아예 없으면 무엇을 하려는지 알 수 없다. 해제는 **명시적으로**
        # null 을 보내야 한다 — 빠뜨린 것과 구분되지 않으면 사유만 바꾸려던
        # 요청이 연결을 조용히 푼다.
        if "matched_sales_order_id" not in proposed:
            raise ValidationError(
                "연결할 주문을 지정하세요. 연결을 해제하려면 null 을 보내세요.",
                detail={"field": "matched_sales_order_id"},
            )
        raw = proposed.get("matched_sales_order_id")
        if raw is not None:
            try:
                UUID(str(raw))
            except (ValueError, AttributeError, TypeError) as exc:
                raise ValidationError(
                    "주문 식별자 형식이 올바르지 않습니다.",
                    detail={"field": "matched_sales_order_id"},
                ) from exc

    def _validate_order(
        self, db: Session, *, org_id: UUID, record_row: Any, order_id: UUID
    ) -> None:
        order = db.execute(text("""
            SELECT id, marketplace, marketplace_order_no, business_key
              FROM sales_order
             WHERE id = :id AND org_id = :org AND deleted_at IS NULL
        """), {"id": str(order_id), "org": str(org_id)}).mappings().first()
        if order is None:
            raise NotFoundError("연결할 주문을 찾을 수 없습니다.")
        if str(order["marketplace"]).upper() != str(record_row["marketplace"]).upper():
            raise ValidationError(
                "판매처가 다른 주문에는 연결할 수 없습니다 "
                f"(정산 {record_row['marketplace']} · 주문 {order['marketplace']}).",
                detail={"settlement_marketplace": record_row["marketplace"],
                        "order_marketplace": order["marketplace"]},
            )

    def apply_changes(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], actor_user_id: UUID,
        request_type: str | None = None, approval_request_id: UUID | None = None,
    ) -> dict[str, Any]:
        # 적용 시점에도 같은 검증을 한다 — DB 를 직접 건드려 심어도 막힌다.
        self.validate_proposed_changes(proposed, request_type=request_type)
        row = self._row(db, org_id, target_id)
        if row["confirmed_at"] is not None:
            raise ValidationError(
                "이미 확정된 정산 건입니다. 정정은 정산 조정으로 처리하세요.")

        raw = proposed.get("matched_sales_order_id")
        order_id = UUID(str(raw)) if raw is not None else None
        if order_id is not None:
            self._validate_order(db, org_id=org_id, record_row=row, order_id=order_id)

        from app.services.settlement_matching import MATCHED_MANUAL, UNMATCHED

        status = MATCHED_MANUAL if order_id else UNMATCHED
        reason = proposed["match_reason"].strip()

        db.execute(text("""
            UPDATE marketplace_settlement_record
               SET matching_status = :st,
                   matched_sales_order_id = CAST(:soid AS UUID),
                   match_reason = :reason,
                   matched_at = CASE WHEN CAST(:soid AS UUID) IS NULL
                                     THEN NULL ELSE now() END,
                   matched_by = CASE WHEN CAST(:soid AS UUID) IS NULL
                                     THEN NULL ELSE CAST(:actor AS UUID) END,
                   match_approval_request_id = CAST(:req AS UUID),
                   updated_at = now(),
                   updated_by = CAST(:actor AS UUID)
             WHERE id = :id AND org_id = :org AND deleted_at IS NULL
        """), {
            "st": status,
            "soid": str(order_id) if order_id else None,
            # 해제해도 사유는 남긴다. CHECK 가 `UNMATCHED` 에는 사유를
            # 요구하지 않지만, 왜 풀었는지가 사라지면 다음 사람이 못 읽는다.
            "reason": reason,
            "req": str(approval_request_id) if approval_request_id else None,
            "actor": str(actor_user_id),
            "id": str(target_id), "org": str(org_id),
        })

        # 감사 로그는 `approval.apply()` 가 승인 흐름 전체로 남긴다. 여기서
        # 또 남기면 같은 사건이 두 줄이 되고, 어느 쪽이 진짜인지 모호해진다.
        return self.build_snapshot(db, org_id=org_id, target_id=target_id)


def build_adapters() -> dict[str, Any]:
    return {SettlementMatchAdapter.target_type: SettlementMatchAdapter()}
