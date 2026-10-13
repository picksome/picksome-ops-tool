"""자동화 유형별 판단 (M045).

`automation.py` 는 **틀**이고 여기는 **판단**이다. 나눈 이유는 판단마다 데이터
출처도 실패 성격도 다르기 때문이다 — 하나의 generic 처리기로 묶으면 각자의
판단이 뭉개진다(`worker_runtime` 이 업무 로직을 합치지 않은 것과 같은 이유).

여기서 새 계산을 만들지 않는다
--------------------------
마진은 `sourcing_recommendation` 과 `margin` 이 계산한다. 원가 차이는
`compare_to_product_cost` 가 계산한다. 마감 차단은 `close_checks` 가 판단한다.
자동화는 **그 결과를 보고 무엇을 할지만** 정한다.

같은 숫자를 두 곳에서 계산하면 어느 쪽이 맞는지 아무도 모르게 된다.

임계값을 지어내지 않는다
--------------------
"마진 15% 미만이면 위험" 같은 기준은 업무 판단이다. 정책의 `guardrail_json` 에
사람이 넣은 값만 쓰고, 없으면 **아무것도 하지 않는다.** 기본값을 정해 두면 그
숫자가 근거처럼 굳는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import automation as auto
from app.services.automation import ActionRecord, AutomationBlockedError, RunResult

#: 자동화가 올린 제안의 요청자 이름. 사람 계정과 구분하기 위한 것이다.
AUTOMATION_ACTOR_NAME = "Seller OS 자동화"


def run_policy(
    db: Session, *, org_id: UUID, policy: dict[str, Any],
    actor_user_id: UUID | None = None, trigger: str = "SCHEDULED",
    worker_run_id: UUID | None = None,
) -> RunResult:
    """정책 하나를 실행한다.

    꺼져 있으면 아무것도 하지 않고 `SKIPPED` 로 남긴다 — "왜 안 돌았지" 를
    물었을 때 기록이 있어야 한다.
    """
    result = auto.start_run(db, org_id=org_id, policy=policy, trigger=trigger,
                            worker_run_id=worker_run_id)
    if not policy["enabled"]:
        auto.finish_run(db, result=result, status="SKIPPED")
        return result

    runner = _RUNNERS.get(policy["automation_type"])
    if runner is None:
        result.error_code = "NO_RUNNER"
        auto.finish_run(db, result=result, status="FAILED")
        return result

    try:
        runner(db, org_id=org_id, policy=policy, result=result,
               actor_user_id=actor_user_id)
    except AutomationBlockedError as blocked:
        # 한도에 걸린 것은 실패가 아니다. 남은 대상은 다음 실행이 본다.
        result.actions.append(ActionRecord(
            action_type="BUDGET_EXHAUSTED", outcome="BLOCKED",
            blocked_by=blocked.guardrail, reason=str(blocked),
        ))
        auto.finish_run(db, result=result, status="PARTIAL")
        return result
    except Exception as exc:  # noqa: BLE001
        result.error_code = type(exc).__name__
        auto.finish_run(db, result=result, status="FAILED")
        raise

    auto.finish_run(db, result=result, status="SUCCESS")
    return result


# ──────────────────────────────────────────────────────────────────────────
# 마진 감시
# ──────────────────────────────────────────────────────────────────────────

def _margin_monitor(
    db: Session, *, org_id: UUID, policy: dict[str, Any], result: RunResult,
    actor_user_id: UUID | None,
) -> None:
    """예상 마진이 기준 아래로 떨어진 상품을 찾는다.

    기준이 없으면 아무것도 하지 않는다 — "몇 %가 위험한가" 는 사업 판단이다.
    """
    threshold = auto.guardrail(policy, "min_margin_rate")
    if threshold is None:
        result.skipped += 1
        result.actions.append(ActionRecord(
            action_type="MARGIN_CHECK", outcome="REPORTED",
            reason="최소 마진율 기준이 설정되지 않아 판단하지 않았습니다.",
        ))
        return

    # 판매 중이고 원가·판매가를 둘 다 아는 상품만. 모르는 값을 0 으로 채우면
    # "마진 100%" 나 "마진 -100%" 가 나오고 둘 다 거짓이다.
    #
    # 가격은 `product_channel_listing` 이 아니라 **`product_channel_price`** 에
    # 있다. 처음에 listing 에 가격이 있다고 가정했다가 테스트가 잡았다 —
    # 실제 스키마는 세 겹이다.
    #
    # ```
    # integration_account       어느 판매자 계정인가 (본점 / 2호점)
    # product_channel_listing   그 계정에 이 상품을 올렸다
    # product_channel_price     그 등록의 가격 (유효기간이 여기 붙는다)
    # ```
    #
    # **`sale_price` 는 할인가다.** 정상가는 `regular_price` 이고, 할인은
    # `sale_start_at`~`sale_end_at` 창 안에서만 유효하다(`ck_pcp_sale_window`).
    # 할인가를 그냥 판매가로 읽으면 할인 기간이 끝난 뒤에도 낮은 값으로 계산해
    # 마진을 실제보다 나쁘게 본다 — 거짓 경고가 쌓이면 감시를 끄게 된다.
    #
    # `effective_to IS NULL` 이 지금 적용 중인 가격 행이다.
    rows = db.execute(text("""
        SELECT p.id, p.name_ko, p.cost_amount, l.id AS listing_id,
               cp.regular_price, cp.currency,
               CASE
                 WHEN cp.sale_price IS NOT NULL
                  AND cp.sale_start_at <= now()
                  AND (cp.sale_end_at IS NULL OR cp.sale_end_at > now())
                 THEN cp.sale_price
                 ELSE cp.regular_price
               END AS effective_price,
               COALESCE(sc.display_name, ia.account_label, '판매처') AS channel
          FROM product p
          JOIN product_channel_listing l
            ON l.product_id = p.id AND l.deleted_at IS NULL
          JOIN product_channel_price cp
            ON cp.listing_id = l.id AND cp.deleted_at IS NULL
           AND cp.effective_to IS NULL
          LEFT JOIN integration_account ia ON ia.id = l.integration_account_id
          LEFT JOIN sale_channel sc ON sc.id = ia.sale_channel_id
         WHERE p.org_id = :o AND p.deleted_at IS NULL
           AND p.status = 'ACTIVE'
           AND p.cost_amount IS NOT NULL AND p.cost_amount > 0
         ORDER BY p.name_ko
    """), {"o": str(org_id)}).mappings().all()

    for row in rows:
        price = row["effective_price"]
        if price is None or price <= 0:
            # 가격을 모르면 마진도 모른다. 0 으로 채우면 "마진 -100%" 가 나온다.
            result.skipped += 1
            continue

        result.evaluated += 1
        # 수수료·세금을 뺀 정확한 마진은 정책이 필요하다. 여기서는 **원가 대비
        # 총이익률**만 본다. 이 값보다 실제 마진은 항상 낮으므로, 여기서
        # 걸리면 실제로도 걸린다 — 거짓 음성은 있어도 거짓 양성은 없다.
        gross = Decimal(price) - Decimal(row["cost_amount"])
        rate = gross / Decimal(price)
        if rate >= threshold:
            continue

        auto.check_action_budget(policy, result)
        pct = f"{rate * 100:.1f}%"
        limit_pct = f"{threshold * 100:.1f}%"
        reason = (f"{row['name_ko']} 총이익률 {pct} (기준 {limit_pct}, "
                  f"{row['channel']})")

        if result.mode != "PROPOSE":
            # 관찰만. 가격을 자동으로 바꾸지 않는다.
            result.actions.append(ActionRecord(
                action_type="MARGIN_BELOW_THRESHOLD", outcome="REPORTED",
                target_table="product", target_id=UUID(str(row["id"])),
                reason=reason,
            ))
            continue

        proposal = _propose_price_change(
            db, org_id=org_id, actor_user_id=actor_user_id,
            listing_id=UUID(str(row["listing_id"])),
            current_price=int(price), cost_amount=int(row["cost_amount"]),
            target_rate=threshold, currency=row["currency"] or "KRW",
            reason=reason,
        )
        result.actions.append(ActionRecord(
            action_type="PRICE_CHANGE_PROPOSED", outcome=proposal.outcome,
            target_table="product_channel_listing",
            target_id=UUID(str(row["listing_id"])),
            reason=proposal.reason or reason,
            blocked_by=proposal.blocked_by,
            approval_request_id=proposal.approval_id,
        ))


def _price_for_target_margin(cost_amount: int, target_rate: Decimal) -> int | None:
    """목표 총이익률을 만드는 판매가.

    ```
    (price - cost) / price = target      →   price = cost / (1 - target)
    ```

    **여기서 임계값을 만들지 않는다.** `target` 은 정책에 사람이 넣은 값이다.
    수수료·세금까지 반영한 정확한 권장가는 `price_sim` 이 계산하지만, 그건
    판매처 정책이 있어야 한다. 정책이 없어도 제안할 수 있는 최소 근거로
    **원가 대비 총이익률**만 쓴다 — 이 값보다 실제 마진은 항상 낮으므로,
    이 가격에서도 목표에 못 미칠 수는 있어도 넘치지는 않는다.
    """
    if target_rate >= 1:
        # 100% 이상은 수학적으로 불가능하다. 정책 검증이 막지만 여기서도 막는다.
        return None
    price = Decimal(cost_amount) / (Decimal(1) - target_rate)
    return int(price.to_integral_value(rounding="ROUND_CEILING"))


def _propose_price_change(
    db: Session, *, org_id: UUID, actor_user_id: UUID | None, listing_id: UUID,
    current_price: int, cost_amount: int, target_rate: Decimal, currency: str,
    reason: str,
) -> ProposalOutcome:
    """가격 변경 **제안**.

    가격을 직접 바꾸지 않는다. 기존 `CHANNEL_PRICE_CHANGE` 승인 요청을 만들고
    끝난다 — 가격은 경쟁·재고·시즌이 함께 정하는 것이라 숫자 하나로 정할 수
    없고, 그래서 사람이 승인해야 한다.

    원가 감시와 같은 중복 방지를 쓴다: 대기 중이면 안 만들고, 같은 값으로
    거절된 적이 있으면 다시 만들지 않는다.
    """
    from app.services import approval

    target_price = _price_for_target_margin(cost_amount, target_rate)
    if target_price is None or target_price <= current_price:
        # 올릴 이유가 없다. 기준을 이미 넘겼거나 계산이 성립하지 않는다.
        return ProposalOutcome(
            outcome="REPORTED",
            reason="목표 마진을 만족하는 가격이 현재가보다 높지 않습니다.",
        )

    pending = db.execute(text("""
        SELECT id FROM approval_request
         WHERE org_id = :o AND target_type = 'product_channel_listing'
           AND target_id = :l AND request_type = 'CHANNEL_PRICE_CHANGE'
           AND status = 'PENDING' AND deleted_at IS NULL
    """), {"o": str(org_id), "l": str(listing_id)}).scalar()
    if pending is not None:
        return ProposalOutcome(
            outcome="BLOCKED", blocked_by="PENDING_PROPOSAL_EXISTS",
            approval_id=UUID(str(pending)),
            reason="이미 대기 중인 가격 변경 요청이 있습니다.",
        )

    # `effective_from` 은 필수다. 오늘(KST)로 둔다 — 승인이 나면 그날부터
    # 적용한다는 뜻이고, 미래 날짜를 지어내면 사람이 승인해도 값이 안 바뀐 것처럼
    # 보인다. 승인자가 날짜를 바꾸고 싶으면 요청을 수정할 수 있다.
    from app.core.timezone import now_local

    changes = {
        "regular_price": target_price,
        "currency": currency,
        "effective_from": now_local().date().isoformat(),
    }
    rejected = db.execute(text("""
        SELECT id FROM approval_request
         WHERE org_id = :o AND target_type = 'product_channel_listing'
           AND target_id = :l AND request_type = 'CHANNEL_PRICE_CHANGE'
           AND status = 'REJECTED' AND deleted_at IS NULL
           AND proposed_changes_json = CAST(:changes AS JSONB)
         LIMIT 1
    """), {"o": str(org_id), "l": str(listing_id),
           "changes": json.dumps(changes)}).scalar()
    if rejected is not None:
        return ProposalOutcome(
            outcome="BLOCKED", blocked_by="ALREADY_REJECTED",
            reason=f"같은 가격({target_price:,}원)으로 올린 요청이 거절된 적이 "
                   "있습니다. 원가나 기준이 바뀌면 새로 제안합니다.",
        )

    created = approval.create_request(
        db, org_id=org_id, actor_user_id=actor_user_id,
        service_account_id=(
            None if actor_user_id else automation_actor(db, org_id=org_id)
        ),
        target_type="product_channel_listing", target_id=listing_id,
        request_type="CHANNEL_PRICE_CHANGE",
        proposed_changes=changes,
        reason=f"[자동 제안] {reason} → {target_price:,}원 제안",
        submit=True,
    )
    return ProposalOutcome(outcome="PROPOSED",
                           approval_id=UUID(str(created["id"])))


# ──────────────────────────────────────────────────────────────────────────
# 소싱 원가 감시
# ──────────────────────────────────────────────────────────────────────────

def _source_cost_monitor(
    db: Session, *, org_id: UUID, policy: dict[str, Any], result: RunResult,
    actor_user_id: UUID | None,
) -> None:
    """공급처 원가와 적용 원가의 차이를 본다.

    **원가를 자동으로 바꾸지 않는다.** `product.cost_amount` 는 `COST_CHANGE`
    승인으로만 바뀐다 — 자동화가 그 규칙을 우회하면 승인 체계가 장식이 된다.
    PROPOSE 모드에서는 승인 **요청**까지만 만든다.
    """
    threshold = auto.guardrail(policy, "min_diff_rate")
    if threshold is None:
        result.skipped += 1
        result.actions.append(ActionRecord(
            action_type="SOURCE_COST_CHECK", outcome="REPORTED",
            reason="최소 차이율 기준이 설정되지 않아 판단하지 않았습니다.",
        ))
        return

    from app.services import sourcing_recommendation as rec

    product_ids = db.execute(text("""
        SELECT DISTINCT ps.product_id
          FROM product_source ps
          JOIN product p ON p.id = ps.product_id AND p.deleted_at IS NULL
         WHERE ps.org_id = :o AND ps.deleted_at IS NULL AND ps.is_primary
           AND p.status = 'ACTIVE'
    """), {"o": str(org_id)}).scalars().all()

    for pid in product_ids:
        result.evaluated += 1
        comparison = rec.compare_sources(db, org_id=org_id, product_id=UUID(str(pid)))
        primary = next((s for s in comparison["sources"] if s["is_primary"]), None)
        if primary is None or primary["difference_rate"] is None:
            # 계산할 수 없는 것은 건너뛴다. 0 으로 보지 않는다.
            result.skipped += 1
            continue

        diff_rate = abs(Decimal(str(primary["difference_rate"])))
        if diff_rate < threshold:
            continue

        auto.check_action_budget(policy, result)
        landed = primary["landed_cost"]["total_krw"]
        current = comparison["current_cost_amount"]
        reason = (f"{comparison['product_name']}: 적용 원가 {current:,}원 → "
                  f"공급처 예상 {landed:,}원 ({diff_rate * 100:.1f}% 차이)")

        if result.mode == "PROPOSE":
            # **사람이 없어도 제안한다.** 예전에는 `actor_user_id` 가 있을 때만
            # 제안해서, 스케줄로 도는 무인 실행은 아무것도 만들지 않았다 —
            # 자동화의 존재 이유가 무인 실행인데.
            proposal = _propose_cost_change(
                db, org_id=org_id, actor_user_id=actor_user_id,
                product_id=UUID(str(pid)), landed_cost=landed, reason=reason,
            )
            result.actions.append(ActionRecord(
                action_type="COST_CHANGE_PROPOSED",
                outcome=proposal.outcome,
                target_table="product", target_id=UUID(str(pid)),
                reason=proposal.reason or reason,
                blocked_by=proposal.blocked_by,
                approval_request_id=proposal.approval_id,
            ))
        else:
            result.actions.append(ActionRecord(
                action_type="SOURCE_COST_DIVERGED", outcome="REPORTED",
                target_table="product", target_id=UUID(str(pid)), reason=reason,
            ))


@dataclass
class ProposalOutcome:
    """제안 시도의 결과. **왜 안 만들었는지**가 남아야 한다."""

    outcome: str
    approval_id: UUID | None = None
    blocked_by: str | None = None
    reason: str | None = None


def automation_actor(db: Session, *, org_id: UUID) -> UUID:
    """자동화가 자기 이름으로 요청을 올리기 위한 주체.

    왜 사람 계정을 빌리지 않나
    ----------------------
    스케줄 실행에는 사람이 없다. 마지막에 화면을 눌렀던 사람 이름으로 올리면
    승인함에 "김대표가 새벽 3시에 원가 변경을 요청함" 으로 뜬다 — 승인자가
    틀린 전제 위에서 판단한다.

    왜 서비스 계정인가
    ---------------
    M046 이 이미 "사람 아닌 요청자" 를 표현한다. 새 종류를 만들면 승인 화면·
    감사·알림이 모두 세 번째 경우를 알아야 한다.

    **토큰을 발급하지 않는다.** 이 계정은 로그인도 API 접근도 하지 않고,
    오직 "누가 이 제안을 올렸나" 를 가리키는 이름이다.
    """
    row = db.execute(text("""
        SELECT id FROM service_account
         WHERE org_id = :o AND name = :n AND deleted_at IS NULL
    """), {"o": str(org_id), "n": AUTOMATION_ACTOR_NAME}).scalar()
    if row is not None:
        return UUID(str(row))

    created = db.execute(text("""
        INSERT INTO service_account (org_id, name, description)
        VALUES (:o, :n, :d) RETURNING id
    """), {"o": str(org_id), "n": AUTOMATION_ACTOR_NAME,
           "d": "자동화가 올린 제안의 요청자입니다. 토큰이 없어 외부에서 "
                "접속할 수 없고, 감사에서 사람과 구분하기 위한 이름입니다."}).scalar_one()
    return UUID(str(created))


def _propose_cost_change(
    db: Session, *, org_id: UUID, actor_user_id: UUID | None, product_id: UUID,
    landed_cost: int, reason: str,
) -> ProposalOutcome:
    """기존 승인 경로를 그대로 쓴다. 자동화용 우회로를 만들지 않는다.

    두 가지를 막는다
    -------------
    1. **대기 중 중복** — 같은 상품에 PENDING 이 있으면 만들지 않는다.
       매 실행마다 같은 제안이 쌓이면 승인함을 아무도 보지 않게 된다.
    2. **거절 후 재제안** — 사람이 거절한 값을 다시 올리지 않는다. 거절은
       "이 값은 아니다" 라는 답이고, 같은 답을 매일 다시 묻는 것은 자동화가
       아니라 잔소리다.

    2 는 **같은 값일 때만** 막는다. 원가가 또 달라졌으면 새로운 근거이므로
    다시 제안한다.
    """
    from app.services import approval

    pending = db.execute(text("""
        SELECT id FROM approval_request
         WHERE org_id = :o AND target_type = 'product' AND target_id = :p
           AND request_type = 'COST_CHANGE' AND status = 'PENDING'
           AND deleted_at IS NULL
    """), {"o": str(org_id), "p": str(product_id)}).scalar()
    if pending is not None:
        return ProposalOutcome(
            outcome="BLOCKED", blocked_by="PENDING_PROPOSAL_EXISTS",
            approval_id=UUID(str(pending)),
            reason="이미 대기 중인 원가 변경 요청이 있습니다.",
        )

    # 같은 값으로 거절당한 적이 있는가. `proposed_changes_json` 을 그대로 비교한다 —
    # 근거가 달라지면(원가가 또 바뀌면) 다른 JSON 이 되어 다시 제안된다.
    rejected = db.execute(text("""
        SELECT id FROM approval_request
         WHERE org_id = :o AND target_type = 'product' AND target_id = :p
           AND request_type = 'COST_CHANGE' AND status = 'REJECTED'
           AND deleted_at IS NULL
           AND proposed_changes_json = CAST(:changes AS JSONB)
         LIMIT 1
    """), {"o": str(org_id), "p": str(product_id),
           "changes": json.dumps({"cost_amount": int(landed_cost)})}).scalar()
    if rejected is not None:
        return ProposalOutcome(
            outcome="BLOCKED", blocked_by="ALREADY_REJECTED",
            reason=f"같은 값({landed_cost:,}원)으로 올린 요청이 거절된 적이 있습니다. "
                   "원가가 다시 바뀌면 새로 제안합니다.",
        )

    created = approval.create_request(
        db, org_id=org_id,
        # 스케줄 실행에는 사람이 없다. 그때는 자동화가 자기 이름으로 올린다.
        actor_user_id=actor_user_id,
        service_account_id=(
            None if actor_user_id else automation_actor(db, org_id=org_id)
        ),
        target_type="product", target_id=product_id,
        request_type="COST_CHANGE",
        proposed_changes={"cost_amount": int(landed_cost)},
        reason=f"[자동 제안] {reason}", submit=True,
    )
    return ProposalOutcome(outcome="PROPOSED",
                           approval_id=UUID(str(created["id"])))


# ──────────────────────────────────────────────────────────────────────────
# 마감 사전점검
# ──────────────────────────────────────────────────────────────────────────

def _month_close_precheck(
    db: Session, *, org_id: UUID, policy: dict[str, Any], result: RunResult,
    actor_user_id: UUID | None,
) -> None:
    """마감을 막을 항목을 **미리** 찾는다.

    마감일에 처음 알면 그날 하루가 사라진다. 기존 `close_checks` 를 그대로
    돌리고 결과만 전한다 — 여기서 새 판단을 만들지 않는다.
    """
    from app.services import close_checks

    ym = db.execute(text("""
        SELECT year_month FROM monthly_settlement
         WHERE org_id = :o AND status NOT IN ('LOCKED')
         ORDER BY year_month DESC LIMIT 1
    """), {"o": str(org_id)}).scalar()
    if ym is None:
        result.skipped += 1
        return

    items = close_checks.run_checks(db, org_id=org_id, year_month=ym)
    result.evaluated = len(items)
    blocking = [i for i in items if i.severity == "BLOCKING"]

    if not blocking:
        result.actions.append(ActionRecord(
            action_type="MONTH_CLOSE_READY", outcome="REPORTED",
            reason=f"{ym} 마감을 막는 항목이 없습니다.",
        ))
        return

    for item in blocking:
        auto.check_action_budget(policy, result)
        result.actions.append(ActionRecord(
            action_type="MONTH_CLOSE_BLOCKED", outcome="REPORTED",
            reason=f"{ym} {item.title}",
        ))
    _ = actor_user_id


# ──────────────────────────────────────────────────────────────────────────
# 일일 리포트
# ──────────────────────────────────────────────────────────────────────────

def _daily_report(
    db: Session, *, org_id: UUID, policy: dict[str, Any], result: RunResult,
    actor_user_id: UUID | None,
) -> None:
    """리포트를 만들고 **알림으로 전달한다.**

    계산은 `daily_report` 가 한다. 여기서 숫자를 다시 세지 않는다.

    ### 하루에 한 번만

    worker 가 두 번 돌아도 같은 날 리포트가 두 번 만들어지지 않는다. 판단
    기준은 **성공한 실행이 오늘 이미 있었나** 다.

    실패한 실행은 세지 않는다 — 정산 조회가 터져서 실패한 뒤 다시 못 돌면
    그날은 리포트가 영영 없다.

    ### 날짜는 KST

    `daily_report._today()` 가 `now_local()` 을 쓴다. UTC 로 날짜를 세면
    한국 시간 오전 9시 이전에 돈 실행이 **전날** 리포트를 만든다.
    """
    from app.services import daily_report, notification_events

    report_date = daily_report.report_date_now()

    if _daily_report_already_done(db, org_id=org_id, report_date=report_date):
        result.evaluated = 1
        result.actions.append(ActionRecord(
            action_type="DAILY_REPORT_BUILT", outcome="BLOCKED",
            blocked_by="ALREADY_GENERATED",
            reason=f"{report_date} 리포트가 이미 생성됐습니다.",
        ))
        return

    report = daily_report.build(db, org_id=org_id, report_date=report_date)
    attention = len(report["needs_attention"])
    result.evaluated = 1
    result.actions.append(ActionRecord(
        action_type="DAILY_REPORT_BUILT", outcome="REPORTED",
        reason=f"{report['report_date']} 리포트 — 확인 필요 {attention}건",
    ))

    # 만들기만 하고 안 알리면 사용자가 화면을 열어야 하고, 그러면 자동 생성의
    # 의미가 없다. 전달까지가 이 자동화의 일이다.
    delivery = notification_events.daily_report_ready(
        db, org_id=org_id, report_date=report_date, attention_count=attention)
    if delivery.created:
        result.actions.append(ActionRecord(
            action_type="DAILY_REPORT_DELIVERED", outcome="EXECUTED",
            reason=f"앱 알림 {delivery.created}명에게 전달했습니다.",
        ))
    elif delivery.deduped:
        # 같은 날 이미 보냈다. 실패가 아니다.
        result.actions.append(ActionRecord(
            action_type="DAILY_REPORT_DELIVERED", outcome="BLOCKED",
            blocked_by="ALREADY_DELIVERED",
            reason="오늘 리포트 알림은 이미 전달됐습니다.",
        ))
    else:
        # 받을 사람이 없거나 채널이 실패했다. **성공으로 만들지 않는다.**
        result.actions.append(ActionRecord(
            action_type="DAILY_REPORT_DELIVERED", outcome="BLOCKED",
            blocked_by="NO_RECIPIENT",
            reason="리포트를 받을 사용자가 없습니다.",
        ))
    _ = (policy, actor_user_id)


def _daily_report_already_done(
    db: Session, *, org_id: UUID, report_date: date,
) -> bool:
    """오늘 리포트가 **성공적으로** 만들어진 적 있나.

    `automation_run` 을 본다. 리포트 전용 테이블을 만들지 않는 이유: 실행
    기록은 이미 여기 있고, 두 군데 두면 어긋난다.

    KST 기준으로 자른다. `started_at` 은 timestamptz 라 그냥 날짜로 자르면
    UTC 로 잘려서 오전 9시 경계가 어긋난다.
    """
    # 유형은 `automation_run` 이 아니라 정책에 있다. 실행은 정책을 가리킨다.
    return db.execute(text("""
        SELECT 1
          FROM automation_run r
          JOIN automation_policy p ON p.id = r.policy_id
          JOIN automation_action a ON a.run_id = r.id
         WHERE r.org_id = :o
           AND p.automation_type = 'DAILY_REPORT'
           AND a.action_type = 'DAILY_REPORT_BUILT'
           AND a.outcome = 'REPORTED'
           AND (r.started_at AT TIME ZONE 'Asia/Seoul')::date = :d
         LIMIT 1
    """), {"o": str(org_id), "d": report_date}).scalar() is not None


# ──────────────────────────────────────────────────────────────────────────
# 주문 자동 수집
# ──────────────────────────────────────────────────────────────────────────

def _order_sync(
    db: Session, *, org_id: UUID, policy: dict[str, Any], result: RunResult,
    actor_user_id: UUID | None,
) -> None:
    """판매처에서 주문을 가져와 판매장부에 넣는다.

    **여기서 수집 로직을 구현하지 않는다.** `order_sync.sync_org` 를 부른다 —
    사람이 화면에서 누르는 수동 동기화도 같은 함수를 지난다. 둘이 갈라지면
    사람이 손으로 확인한 동작이 자동 실행에서 재현되지 않는다.

    `REPORT_ONLY` 는 외부를 부르지 않는다
    ---------------------------------
    이 모드는 "무엇을 할 수 있는지만 보여 달라" 는 뜻이다. 읽기라도 외부를
    부르면, 꺼 두었다고 믿는 사용자의 계정으로 요청이 나간다.
    """
    from app.services import order_sync as osync

    mode = auto.effective_mode(policy)

    configs = osync.read_config(policy)
    if not configs:
        result.actions.append(ActionRecord(
            action_type="ORDER_SYNC", outcome="BLOCKED",
            blocked_by=osync.NOT_CONFIGURED,
            reason="겹쳐 읽기 일수와 대상 계정·시작일을 설정해야 동작합니다.",
        ))
        return

    if mode != "AUTO_EXECUTE":
        for account_id in configs:
            result.evaluated += 1
            result.actions.append(ActionRecord(
                action_type="ORDER_SYNC", outcome="REPORTED",
                target_table="external_account", target_id=account_id,
                reason="자동 실행이 꺼져 있어 외부를 호출하지 않았습니다.",
            ))
        return

    actor = actor_user_id or automation_actor(db, org_id=org_id)
    # worker_run 연결은 `automation_run` 이 이미 갖고 있다. 같은 링크를
    # `external_sync_run` 에도 복제하지 않는다 — 한쪽만 갱신되면 갈라진다.
    # 실행 주체를 그대로 넘긴다. 못박아 두면 기동 직후 따라잡기가 주기
    # 실행으로 기록되고, "PC 가 꺼져 있었다" 가 "스케줄러가 멈췄다" 로
    # 보인다(M058).
    outcomes = osync.sync_org(
        db, org_id=org_id, policy=policy, actor_user_id=actor,
        trigger=result.trigger)

    for out in outcomes:
        result.evaluated += 1
        if out.status == osync.SUCCESS:
            result.actions.append(ActionRecord(
                action_type="ORDER_SYNC", outcome="EXECUTED",
                target_table="external_account", target_id=out.external_account_id,
                reason=(f"주문 {out.fetched}건 조회, 신규 {out.created}건, "
                        f"갱신 {out.updated}건, 되살림 {out.replayed}건."),
            ))
            continue
        if out.status == osync.SKIPPED:
            result.actions.append(ActionRecord(
                action_type="ORDER_SYNC", outcome="BLOCKED",
                blocked_by=out.reason,
                target_table="external_account", target_id=out.external_account_id,
                reason="이번 실행에서는 동기화하지 않았습니다.",
            ))
            continue
        # PARTIAL·FAILED. 성공 건수가 있어도 성공으로 세지 않는다.
        result.actions.append(ActionRecord(
            action_type="ORDER_SYNC", outcome="FAILED",
            blocked_by=out.reason,
            target_table="external_account", target_id=out.external_account_id,
            reason=(f"동기화가 {out.status} 로 끝났습니다. "
                    f"신규 {out.created}건은 유지됩니다."),
        ))


def _reorder_monitor(
    db: Session, *, org_id: UUID, policy: dict[str, Any], result: RunResult,
    actor_user_id: UUID | None,
) -> None:
    """재고가 떨어질 상품을 찾아 **발주 초안까지만** 만든다.

    발주를 자동으로 내보내지 않는 이유
    -------------------------------
    판매속도는 추정치다. 시즌·광고·경쟁사 품절로 크게 흔들리고, 지난 30일이
    다음 30일을 보장하지 않는다. 그 추정치로 돈을 쓰면 안 된다.

    그래서 `PROPOSE` 는 `DRAFT` 발주까지만 만든다. `DRAFT → ORDERED` 는
    사람이 누르고, 그 권한은 ADMIN/OPERATOR 다.

    파라미터가 없으면 돌지 않는다
    -------------------------
    `velocity_days` / `coverage_days` 가 정책에 없으면 `BLOCKED_MISSING_GUARDRAIL`
    로 끝낸다. 여기서 "보통 30일" 을 채우면 사용자가 자기 회전율을 한 번도
    정하지 않은 채 그 숫자를 믿게 된다.
    """
    from app.services import purchasing, reorder

    velocity_days = auto.guardrail(policy, "velocity_days")
    coverage_days = auto.guardrail(policy, "coverage_days")
    missing = [
        name for name, value in (
            ("velocity_days", velocity_days), ("coverage_days", coverage_days),
        ) if value is None
    ]
    if missing:
        result.actions.append(ActionRecord(
            action_type="REORDER_SCAN", outcome="BLOCKED",
            blocked_by="MISSING_GUARDRAIL",
            reason=f"판매속도 관찰 기간과 목표 커버 일수를 먼저 정하세요"
                   f" (누락: {', '.join(missing)}).",
        ))
        return

    scan = reorder.calculate(
        db, org_id=org_id, velocity_days=int(velocity_days),
        coverage_days=int(coverage_days),
    )
    result.evaluated = len(scan["candidates"]) + len(scan["blocked"])

    # 추천할 수 없는 것들을 **한 줄로 요약해서** 보고한다. 상품마다 한 줄씩
    # 남기면 실제 발주 후보가 그 속에 묻힌다.
    if scan["blocked"]:
        by_reason: dict[str, int] = {}
        for b in scan["blocked"]:
            by_reason[b["block_reason"]] = by_reason.get(b["block_reason"], 0) + 1
        summary = ", ".join(
            f"{reorder.BLOCK_REASONS[k]} {v}건" for k, v in sorted(by_reason.items())
        )
        result.actions.append(ActionRecord(
            action_type="REORDER_DATA_MISSING", outcome="REPORTED",
            reason=f"재주문을 계산할 수 없는 상품이 있습니다 — {summary}",
        ))

    # 발주 기한이 지났거나 오늘인 것만 다룬다. 아직 여유가 있는 것까지 초안을
    # 만들면 초안이 쌓여 아무도 보지 않게 된다.
    urgent = [c for c in scan["candidates"] if c["overdue"]]
    if not urgent:
        result.actions.append(ActionRecord(
            action_type="REORDER_SCAN", outcome="REPORTED",
            reason=f"발주 기한이 임박한 상품이 없습니다 "
                   f"(후보 {len(scan['candidates'])}건은 아직 여유 있음).",
        ))
        return

    if result.mode != "PROPOSE":
        # 관찰만. 발주 초안도 만들지 않는다.
        for c in urgent[:20]:
            auto.check_action_budget(policy, result)
            result.actions.append(ActionRecord(
                action_type="REORDER_NEEDED", outcome="REPORTED",
                target_table="product", target_id=UUID(c["product_id"]),
                reason=f"{c['product_name']} 재고 {c['on_hand']}개, "
                       f"{c['stockout_date']} 소진 예상 "
                       f"(발주 기한 {c['reorder_by_date']})",
            ))
        return

    # 공급처별로 묶는다. 발주는 공급처 단위로 나가기 때문이다 — 상품마다 초안을
    # 하나씩 만들면 사용자가 손으로 합쳐야 한다.
    for group in reorder.group_by_supplier(urgent):
        auto.check_action_budget(policy, result)
        site_id = UUID(group["supplier_site_id"])

        if _has_open_draft(db, org_id=org_id, supplier_site_id=site_id):
            # 아직 처리 안 된 초안이 있는데 또 만들면 중복 발주가 된다.
            result.actions.append(ActionRecord(
                action_type="PURCHASE_ORDER_PROPOSED", outcome="BLOCKED",
                blocked_by="PENDING_DRAFT_EXISTS",
                target_table="supplier_site", target_id=site_id,
                reason=f"{group['supplier_name']}: 처리되지 않은 발주 초안이 "
                       "이미 있습니다.",
            ))
            continue

        order = purchasing.create_order(
            db, org_id=org_id, actor_user_id=None, supplier_site_id=site_id,
            lines=[{
                "product_id": c["product_id"],
                "product_variant_id": c["product_variant_id"],
                "quantity": c["recommended_quantity"],
                "unit_price": c["unit_price"],
                "product_source_id": c["product_source_id"],
            } for c in group["lines"]],
            currency=group["currency"],
            memo=f"[자동 제안] 재고 소진 임박 {len(group['lines'])}건. "
                 f"관찰 {int(velocity_days)}일 / 목표 커버 {int(coverage_days)}일 기준.",
            automation_run_id=UUID(result.run_id),
        )
        result.actions.append(ActionRecord(
            action_type="PURCHASE_ORDER_PROPOSED", outcome="PROPOSED",
            target_table="purchase_order", target_id=UUID(order["id"]),
            reason=f"{group['supplier_name']} 발주 초안 {order['business_key']} "
                   f"— {len(group['lines'])}품목 {order['total_amount']:,}원. "
                   "확정은 사람이 합니다.",
        ))
    _ = actor_user_id


def _has_open_draft(db: Session, *, org_id: UUID, supplier_site_id: UUID) -> bool:
    """이 공급처에 아직 처리 안 된 자동 초안이 있나.

    사람이 만든 초안은 세지 않는다 — 사람이 별도로 준비 중인 발주를 이유로
    자동화가 침묵하면 안 된다.
    """
    return db.execute(text("""
        SELECT 1 FROM purchase_order
         WHERE org_id = :o AND supplier_site_id = :s AND status = 'DRAFT'
           AND automation_run_id IS NOT NULL AND deleted_at IS NULL
         LIMIT 1
    """), {"o": str(org_id), "s": str(supplier_site_id)}).scalar() is not None


def _claim_sync(
    db: Session, *, org_id: UUID, policy: dict[str, Any], result: RunResult,
    actor_user_id: UUID | None,
) -> None:
    """판매처에서 취소·반품 사실을 가져와 매출 조정을 만든다.

    **반품 업무를 대신하지 않는다.** 승인·회수·배송은 판매처가 한다. 여기서
    하는 일은 "그 일이 일어났다" 는 사실을 읽어 장부를 맞추는 것뿐이다.

    `REPORT_ONLY` 는 외부를 부르지 않는다 — 주문 동기화와 같은 규칙이다.
    """
    from app.services import claim_sync as csync

    mode = auto.effective_mode(policy)
    configs = csync.read_config(policy)
    if not configs:
        result.actions.append(ActionRecord(
            action_type="CLAIM_SYNC", outcome="BLOCKED",
            blocked_by=csync.NOT_CONFIGURED,
            reason="클레임 조회 기간과 대상 계정·시작일을 설정해야 동작합니다.",
        ))
        return

    if mode != "AUTO_EXECUTE":
        for account_id in configs:
            result.evaluated += 1
            result.actions.append(ActionRecord(
                action_type="CLAIM_SYNC", outcome="REPORTED",
                target_table="external_account", target_id=account_id,
                reason="자동 실행이 꺼져 있어 외부를 호출하지 않았습니다.",
            ))
        return

    actor = actor_user_id or automation_actor(db, org_id=org_id)
    # 주문 동기화와 같은 이유로 실행 주체를 그대로 넘긴다 (M058).
    outcomes = csync.sync_org(
        db, org_id=org_id, policy=policy, actor_user_id=actor,
        trigger=result.trigger)

    for out in outcomes:
        result.evaluated += 1
        ing = out.ingest or {}
        fin = ing.get("financial") or {}
        if out.status == csync.SUCCESS:
            result.actions.append(ActionRecord(
                action_type="CLAIM_SYNC", outcome="EXECUTED",
                target_table="external_account", target_id=out.external_account_id,
                reason=(f"클레임 {ing.get('fetched', 0)}건 조회, "
                        f"신규 {ing.get('created', 0)}건, "
                        f"매출조정 {ing.get('adjustments_created', 0)}건, "
                        f"확인필요 {fin.get('unresolved', 0)}건."),
            ))
            continue
        if out.status == csync.SKIPPED:
            result.actions.append(ActionRecord(
                action_type="CLAIM_SYNC", outcome="BLOCKED",
                blocked_by=out.reason,
                target_table="external_account", target_id=out.external_account_id,
                reason="이번 실행에서는 동기화하지 않았습니다.",
            ))
            continue
        result.actions.append(ActionRecord(
            action_type="CLAIM_SYNC", outcome="FAILED",
            blocked_by=out.reason,
            target_table="external_account", target_id=out.external_account_id,
            reason=f"클레임 동기화가 {out.status} 로 끝났습니다.",
        ))


_RUNNERS = {
    "MARGIN_MONITOR": _margin_monitor,
    "SOURCE_COST_MONITOR": _source_cost_monitor,
    "MONTH_CLOSE_PRECHECK": _month_close_precheck,
    "DAILY_REPORT": _daily_report,
    "ORDER_SYNC": _order_sync,
    "CLAIM_SYNC": _claim_sync,
    "REORDER_MONITOR": _reorder_monitor,
}


def run_all_enabled(
    db: Session, *, org_id: UUID, actor_user_id: UUID | None = None,
    trigger: str = "SCHEDULED", worker_run_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """켜진 정책을 전부 실행한다.

    **하나가 실패해도 나머지는 돈다.** 마진 감시가 터졌다고 마감 사전점검이
    멈추면 자동화 전체를 신뢰할 수 없게 된다.
    """
    out: list[dict[str, Any]] = []
    for policy in auto.list_policies(db, org_id=org_id):
        if policy["id"] is None or not policy["enabled"]:
            continue
        try:
            result = run_policy(
                db, org_id=org_id, policy=policy, actor_user_id=actor_user_id,
                trigger=trigger, worker_run_id=worker_run_id,
            )
            out.append(result.as_dict())
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            out.append({
                "policy_id": policy["id"],
                "automation_type": policy["automation_type"],
                "status": "FAILED", "error_code": type(exc).__name__,
            })
    return out


def due_policies(db: Session, *, org_id: UUID) -> list[dict[str, Any]]:
    """지금 돌 차례인 정책.

    주기가 설정되지 않은 정책은 스케줄로 돌지 않는다 — 임의 주기를 만들지
    않는다. 사람이 화면에서 직접 실행할 수는 있다.
    """
    out = []
    for policy in auto.list_policies(db, org_id=org_id):
        if policy["id"] is None or not policy["enabled"]:
            continue
        if policy["interval_minutes"] is None:
            continue
        last = policy["last_run_at"]
        if last is None:
            out.append(policy)
            continue
        # `CAST(... AS ...)` 를 쓴다. `:last::timestamptz` 는 SQLAlchemy 가
        # 바인딩하지 못하고 콜론을 그대로 넘겨 구문 오류가 난다.
        #
        # 이 줄은 **한 번도 실행된 적이 없었다** — 위의 `last is None` 분기가
        # 첫 실행을 모두 흡수하기 때문이다. 그래서 어떤 자동화든 두 번째
        # 주기부터 스케줄이 통째로 죽는 상태였다.
        due = db.execute(text("""
            SELECT (CAST(:last AS timestamptz) + make_interval(mins => :m)) <= now()
        """), {"last": last, "m": policy["interval_minutes"]}).scalar()
        if due:
            out.append(policy)
    return out


