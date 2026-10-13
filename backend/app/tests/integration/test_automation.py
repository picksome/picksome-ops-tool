"""자동화 엔진 (M045) + MCP 선택성.

이 파일이 고정하는 것
------------------
1. 꺼진 정책은 **아무것도 하지 않는다**
2. `REPORT_ONLY` 는 쓰지 않는다 — 승인 요청도 만들지 않는다
3. `PROPOSE` 는 승인 요청까지만 — 원가는 그대로다
4. 한도가 없는 `AUTO_EXECUTE` 는 제안으로 떨어진다
5. 한도에 걸린 행동이 **기록된다** (안 한 것과 구분된다)
6. 고위험 행동은 자동화 경로가 아예 없다
7. **MCP 를 지워도 자동화가 돈다**

7 이 이 파일의 핵심이다
--------------------
AI 접근이 Core 의 필수 의존성이 되면, AI 를 안 쓰는 고객에게 AI 장애가
전파된다.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.services import automation as auto
from app.services import automation_runners as runners
from app.tests.fixtures.factories import (
    make_app_user,
    make_integration_account,
    make_organization,
    make_product,
)


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


def _listed(db, org, product_id, *, regular_price: int) -> None:
    """판매 등록 + 적용 가격.

    실제 스키마는 세 겹이다. 하나로 뭉뚱그리면 멀티 스토어를 구분할 수 없다.

    ```
    integration_account        어느 판매자 계정인가 (본점 / 2호점)
    product_channel_listing    그 계정에 이 상품을 올렸다
    product_channel_price      그 등록의 가격 (유효기간이 여기 붙는다)
    ```
    """
    account_id = make_integration_account(db, org_id=org)
    listing_id = db.execute(text("""
        INSERT INTO product_channel_listing
               (org_id, product_id, integration_account_id, status)
        VALUES (:o, :p, :a, 'ACTIVE') RETURNING id
    """), {"o": str(org), "p": str(product_id), "a": str(account_id)}).scalar_one()
    # `sale_price` 는 **할인가**다(기간 필수). 평소 판매가는 `regular_price` 다.
    db.execute(text("""
        INSERT INTO product_channel_price
               (org_id, listing_id, regular_price, effective_from)
        VALUES (:o, :l, :s, CURRENT_DATE)
    """), {"o": str(org), "l": str(listing_id), "s": regular_price})
    db.execute(text("""
        UPDATE product SET status = 'ACTIVE' WHERE id = :p
    """), {"p": str(product_id)})
    db.commit()


# ── 정책 ────────────────────────────────────────────────────────

def test_default_mode_is_report_only(db, org, actor):
    """자동 실행을 기본값으로 두지 않는다."""
    policy = auto.upsert_policy(db, org_id=org, actor_user_id=actor,
                                automation_type="MARGIN_MONITOR")
    assert policy["mode"] == "REPORT_ONLY"
    assert policy["enabled"] is False


def test_unsupported_mode_is_rejected(db, org, actor):
    """지원하지 않는 모드를 저장하면 실행 시점에 조용히 무시된다."""
    with pytest.raises(ValidationError) as err:
        auto.upsert_policy(db, org_id=org, actor_user_id=actor,
                           automation_type="MONTH_CLOSE_PRECHECK",
                           mode="AUTO_EXECUTE")
    assert "지원하지 않습니다" in str(err.value)


def test_unknown_guardrail_is_rejected(db, org, actor):
    """오타 하나로 한도가 조용히 사라지면 사용자는 한도를 걸었다고 믿는다."""
    with pytest.raises(ValidationError):
        auto.upsert_policy(db, org_id=org, actor_user_id=actor,
                           automation_type="MARGIN_MONITOR",
                           guardrails={"max_action_per_run": 5})


def test_guardrail_not_allowed_for_this_type_is_rejected(db, org, actor):
    with pytest.raises(ValidationError):
        auto.upsert_policy(db, org_id=org, actor_user_id=actor,
                           automation_type="MARGIN_MONITOR",
                           guardrails={"min_diff_rate": "0.1"})


def test_interval_floor(db, org, actor):
    """외부 API rate limit 을 모르는 상태다. 촘촘하게 두지 않는다."""
    with pytest.raises(ValidationError):
        auto.upsert_policy(db, org_id=org, actor_user_id=actor,
                           automation_type="MARGIN_MONITOR", interval_minutes=1)


def test_policy_list_includes_types_never_configured(db, org, actor):
    """목록에 없는 자동화는 사용자에게 존재하지 않는 것과 같다."""
    rows = auto.list_policies(db, org_id=org)
    types = {r["automation_type"] for r in rows}
    assert types == set(auto.AUTOMATION_TYPES)
    assert all(r["enabled"] is False for r in rows)


def test_enabling_automation_is_audited(db, org, actor):
    """"언제부터 자동으로 돌았나" 를 물을 수 있어야 한다."""
    auto.upsert_policy(db, org_id=org, actor_user_id=actor,
                       automation_type="MARGIN_MONITOR", enabled=True)
    rows = db.execute(text("""
        SELECT after_json::text FROM audit_log
         WHERE org_id = :o AND domain_table = 'automation_policy'
    """), {"o": str(org)}).scalars().all()
    assert any("true" in r.lower() for r in rows)


# ── 실행 ────────────────────────────────────────────────────────

def test_disabled_policy_does_nothing(db, org, actor):
    product = make_product(db, org_id=org, cost_amount=9_000)
    _listed(db, org, product, regular_price=10_000)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="MARGIN_MONITOR",
        enabled=False, guardrails={"min_margin_rate": "0.5"},
    )

    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    assert result.status == "SKIPPED"
    assert result.actions == []
    # 돌지 않았다는 사실 자체는 남는다 — "왜 안 돌았지" 에 답해야 한다.
    assert db.execute(text("""
        SELECT status FROM automation_run WHERE id = :i
    """), {"i": result.run_id}).scalar() == "SKIPPED"


def test_report_only_writes_nothing(db, org, actor):
    """관측만 한다. 승인 요청도 만들지 않는다."""
    product = make_product(db, org_id=org, cost_amount=9_500)
    _listed(db, org, product, regular_price=10_000)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="MARGIN_MONITOR",
        enabled=True, mode="REPORT_ONLY", guardrails={"min_margin_rate": "0.2"},
    )

    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    assert result.status == "SUCCESS"
    assert result.counts() == {"REPORTED": 1}
    assert db.execute(text("""
        SELECT COUNT(*) FROM approval_request WHERE org_id = :o
    """), {"o": str(org)}).scalar() == 0


def test_margin_monitor_does_nothing_without_a_threshold(db, org, actor):
    """"몇 %가 위험한가" 는 사업 판단이다. 기본값을 지어내지 않는다."""
    product = make_product(db, org_id=org, cost_amount=9_900)
    _listed(db, org, product, regular_price=10_000)
    policy = auto.upsert_policy(db, org_id=org, actor_user_id=actor,
                                automation_type="MARGIN_MONITOR", enabled=True)

    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    assert result.evaluated == 0
    assert result.actions[0].reason.startswith("최소 마진율 기준이 설정되지")


def test_healthy_margin_is_not_flagged(db, org, actor):
    product = make_product(db, org_id=org, cost_amount=3_000)
    _listed(db, org, product, regular_price=10_000)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="MARGIN_MONITOR",
        enabled=True, guardrails={"min_margin_rate": "0.2"},
    )

    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    assert result.evaluated == 1
    assert result.actions == []


def test_action_budget_blocks_and_records_it(db, org, actor):
    """한도에 걸린 행동을 기록하지 않으면 "아무 일도 없었다" 와 구분되지 않는다."""
    for _ in range(4):
        pid = make_product(db, org_id=org, cost_amount=9_800)
        _listed(db, org, pid, regular_price=10_000)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="MARGIN_MONITOR",
        enabled=True,
        guardrails={"min_margin_rate": "0.5", "max_actions_per_run": 2},
    )

    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    assert result.status == "PARTIAL"
    counts = result.counts()
    assert counts["REPORTED"] == 2
    assert counts["BLOCKED"] == 1

    blocked = db.execute(text("""
        SELECT blocked_by FROM automation_action
         WHERE run_id = :r AND outcome = 'BLOCKED'
    """), {"r": result.run_id}).scalar()
    assert blocked == "max_actions_per_run"


def test_auto_execute_without_guardrails_falls_back_to_propose(db, org, actor):
    """무제한 자동 실행을 허용하지 않는다."""
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="ORDER_SYNC",
        enabled=True, mode="AUTO_EXECUTE",
    )
    assert auto.effective_mode(policy) == "PROPOSE"

    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    assert result.mode == "PROPOSE"


def _order_sync_account(db, org) -> str:
    system_id = db.execute(text("""
        INSERT INTO external_system (code, name, system_type, supports_api_key)
        VALUES ('AUTO_TEST_TOOL', '테스트', 'SELLER_TOOL', TRUE)
        ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name RETURNING id
    """)).scalar()
    account_id = db.execute(text("""
        INSERT INTO external_account
               (org_id, external_system_id, account_code, display_name)
        VALUES (:o, :s, 'A1', '본사') RETURNING id
    """), {"o": str(org), "s": str(system_id)}).scalar()
    db.commit()
    return str(account_id)


def test_order_sync_without_configuration_does_not_run(db, org, actor):
    """겹쳐 읽기 폭과 시작일이 없으면 **외부를 부르지 않는다.**

    기본값으로 채우면 사용자가 정하지 않은 범위로 남의 API 를 두들기게 된다.
    """
    _order_sync_account(db, org)

    policy = auto.upsert_policy(db, org_id=org, actor_user_id=actor,
                                automation_type="ORDER_SYNC", enabled=True)
    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)

    assert result.counts() == {"BLOCKED": 1}
    assert result.actions[0].blocked_by == "NOT_CONFIGURED"


def test_order_sync_reports_why_it_cannot_run(db, org, actor):
    """설정은 됐지만 커넥터가 없으면 성공으로도 실패로도 만들지 않는다."""
    account_id = _order_sync_account(db, org)

    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="ORDER_SYNC",
        enabled=True, mode="AUTO_EXECUTE",
        guardrails={"overlap_days": 3},
        scope={"accounts": [
            {"external_account_id": account_id, "initial_sync_from": "2026-07-26"},
        ]},
    )
    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)

    assert result.counts() == {"BLOCKED": 1}
    assert result.actions[0].blocked_by == "CONNECTOR_UNAVAILABLE"


def test_order_sync_report_only_never_calls_the_vendor(db, org, actor):
    """`REPORT_ONLY` 는 읽기라도 외부를 부르지 않는다.

    꺼 두었다고 믿는 사용자의 계정으로 요청이 나가면 안 된다.
    """
    account_id = _order_sync_account(db, org)

    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="ORDER_SYNC",
        enabled=True, mode="REPORT_ONLY",
        guardrails={"overlap_days": 3},
        scope={"accounts": [
            {"external_account_id": account_id, "initial_sync_from": "2026-07-26"},
        ]},
    )
    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)

    assert result.counts() == {"REPORTED": 1}
    # 실행 기록이 남지 않았다 = 벤더를 부르지 않았다.
    assert db.execute(text("""
        SELECT count(*) FROM external_sync_run WHERE org_id = :o
    """), {"o": str(org)}).scalar() == 0


def test_high_risk_actions_have_no_automation_path(db, org, actor):
    """돈이 나가거나 확정된 과거를 되돌리는 일은 경로 자체를 만들지 않는다."""
    from app.core.exceptions import ConflictError

    for action in auto.NEVER_AUTOMATED:
        with pytest.raises(ConflictError):
            auto.assert_not_high_risk(action)

    # 자동화 유형 목록에도 없다.
    assert not (set(auto.NEVER_AUTOMATED) & set(auto.AUTOMATION_TYPES))


def test_one_failing_policy_does_not_stop_the_others(db, org, actor):
    """마진 감시가 터졌다고 마감 사전점검이 멈추면 전체를 신뢰할 수 없다."""
    auto.upsert_policy(db, org_id=org, actor_user_id=actor,
                       automation_type="MARGIN_MONITOR", enabled=True)
    auto.upsert_policy(db, org_id=org, actor_user_id=actor,
                       automation_type="DAILY_REPORT", enabled=True)

    results = runners.run_all_enabled(db, org_id=org, actor_user_id=actor)
    assert len(results) == 2
    assert {r["automation_type"] for r in results} == {
        "MARGIN_MONITOR", "DAILY_REPORT",
    }


def test_policy_without_interval_is_not_scheduled(db, org, actor):
    """임의 주기를 만들지 않는다. 사람이 수동 실행은 할 수 있다."""
    auto.upsert_policy(db, org_id=org, actor_user_id=actor,
                       automation_type="MARGIN_MONITOR", enabled=True)
    assert runners.due_policies(db, org_id=org) == []

    auto.upsert_policy(db, org_id=org, actor_user_id=actor,
                       automation_type="MARGIN_MONITOR", enabled=True,
                       interval_minutes=60)
    due = runners.due_policies(db, org_id=org)
    assert [p["automation_type"] for p in due] == ["MARGIN_MONITOR"]


# ── MCP 선택성 ───────────────────────────────────────────────────

def test_core_never_imports_the_mcp_sdk():
    """MCP SDK 가 Core 의존성이 되면 AI 를 안 쓰는 고객도 그 장애를 받는다."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    offenders = []
    for path in root.rglob("*.py"):
        if "tests" in path.parts or path.parts[-2:] == ("app", "mcp"):
            continue
        text_ = path.read_text(encoding="utf-8", errors="ignore")
        for line in text_.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import mcp", "from mcp ", "from mcp.")):
                offenders.append(f"{path.name}: {stripped}")
    assert not offenders, f"Core 가 MCP SDK 를 import 한다: {offenders}"


def test_automation_runs_with_mcp_disabled(db, org, actor, monkeypatch):
    """MCP OFF 에서 자동화·리포트가 그대로 돈다."""
    from app.config import get_settings
    from app.services import daily_report

    monkeypatch.setattr(get_settings(), "MCP_ENABLED", False, raising=False)

    product = make_product(db, org_id=org, cost_amount=9_500)
    _listed(db, org, product, regular_price=10_000)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="MARGIN_MONITOR",
        enabled=True, guardrails={"min_margin_rate": "0.2"},
    )

    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    assert result.status == "SUCCESS"
    assert result.counts() == {"REPORTED": 1}

    report = daily_report.build(db, org_id=org)
    assert report["sections"]["margin"]["below_threshold"] == 1


# ── 일일 리포트 ──────────────────────────────────────────────────

def test_report_marks_unknown_instead_of_zero(db, org, actor):
    """0 으로 채우면 "오늘 매출 0원" 과 "아직 집계 안 됨" 이 같아 보인다."""
    from app.services import daily_report

    report = daily_report.build(db, org_id=org)

    # 현금 기준 잔액이 없으면 잔액은 **모른다**.
    assert report["sections"]["cashflow"]["available"] is False
    assert report["sections"]["cashflow"]["value"] is None
    # 클레임 연동이 없으면 0 이 아니라 null 이다.
    assert report["sections"]["orders"]["claims"] is None


def test_report_puts_action_items_first(db, org, actor):
    """숫자를 먼저 보여주면 사용자는 결국 리포트를 안 보게 된다."""
    from app.services import daily_report

    product = make_product(db, org_id=org, cost_amount=5_000)
    db.commit()
    from app.services import approval

    approval.create_request(
        db, org_id=org, actor_user_id=actor, target_type="product",
        target_id=product, request_type="COST_CHANGE",
        proposed_changes={"cost_amount": 6_000}, reason="테스트", submit=True,
    )
    db.commit()

    report = daily_report.build(db, org_id=org)
    kinds = [n["kind"] for n in report["needs_attention"]]
    assert "APPROVAL_PENDING" in kinds


def test_one_broken_section_does_not_kill_the_report(db, org, actor, monkeypatch):
    """한 구획의 실패가 나머지를 전부 죽이면 리포트를 쓸 수 없다.

    Postgres 는 실패한 트랜잭션에서 이후 쿼리를 전부 거부한다 — 스모크
    테스트에서 실제로 그랬다.
    """
    from app.services import daily_report

    def boom(db_, *, org_id, day):
        db_.execute(text("SELECT * FROM table_that_does_not_exist"))

    monkeypatch.setattr(daily_report, "_cashflow", boom)
    report = daily_report.build(db, org_id=org)

    assert report["sections"]["cashflow"]["available"] is False
    # 뒤에 오는 구획이 살아 있다.
    assert report["sections"]["issues"]["available"] is True
    assert report["sections"]["automation"]["available"] is True


def test_expired_discount_is_not_treated_as_the_selling_price(db, org, actor):
    """할인 기간이 끝났으면 정상가로 판단한다.

    할인가를 그냥 판매가로 읽으면 할인이 끝난 뒤에도 낮은 값으로 계산해 마진을
    실제보다 나쁘게 본다 — 거짓 경고가 쌓이면 사용자가 감시를 꺼 버린다.
    """
    from datetime import datetime, timedelta

    product = make_product(db, org_id=org, cost_amount=6_000)
    account_id = make_integration_account(db, org_id=org)
    listing_id = db.execute(text("""
        INSERT INTO product_channel_listing
               (org_id, product_id, integration_account_id, status)
        VALUES (:o, :p, :a, 'ACTIVE') RETURNING id
    """), {"o": str(org), "p": str(product), "a": str(account_id)}).scalar_one()

    past = datetime.now(UTC) - timedelta(days=10)
    ended = datetime.now(UTC) - timedelta(days=1)
    db.execute(text("""
        INSERT INTO product_channel_price
               (org_id, listing_id, regular_price, sale_price,
                sale_start_at, sale_end_at, effective_from)
        VALUES (:o, :l, 10000, 6500, :s, :e, CURRENT_DATE)
    """), {"o": str(org), "l": str(listing_id), "s": past, "e": ended})
    db.execute(text("UPDATE product SET status = 'ACTIVE' WHERE id = :p"),
               {"p": str(product)})
    db.commit()

    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="MARGIN_MONITOR",
        enabled=True, guardrails={"min_margin_rate": "0.3"},
    )
    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)

    # 정상가 10,000 · 원가 6,000 → 40% 로 기준(30%)을 넘는다.
    # 끝난 할인가 6,500 으로 봤다면 7.7% 라 경고가 떴을 것이다.
    assert result.evaluated == 1
    assert result.actions == []


def test_active_discount_is_used_as_the_selling_price(db, org, actor):
    """진행 중인 할인은 실제 판매가다."""
    from datetime import datetime, timedelta

    product = make_product(db, org_id=org, cost_amount=6_000)
    account_id = make_integration_account(db, org_id=org)
    listing_id = db.execute(text("""
        INSERT INTO product_channel_listing
               (org_id, product_id, integration_account_id, status)
        VALUES (:o, :p, :a, 'ACTIVE') RETURNING id
    """), {"o": str(org), "p": str(product), "a": str(account_id)}).scalar_one()

    started = datetime.now(UTC) - timedelta(days=1)
    ends = datetime.now(UTC) + timedelta(days=5)
    db.execute(text("""
        INSERT INTO product_channel_price
               (org_id, listing_id, regular_price, sale_price,
                sale_start_at, sale_end_at, effective_from)
        VALUES (:o, :l, 10000, 6500, :s, :e, CURRENT_DATE)
    """), {"o": str(org), "l": str(listing_id), "s": started, "e": ends})
    db.execute(text("UPDATE product SET status = 'ACTIVE' WHERE id = :p"),
               {"p": str(product)})
    db.commit()

    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="MARGIN_MONITOR",
        enabled=True, guardrails={"min_margin_rate": "0.3"},
    )
    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)

    # 6,500 에 원가 6,000 → 7.7% 로 기준 미달이다.
    assert result.counts() == {"REPORTED": 1}


# ── PROPOSE 실제 동작 ────────────────────────────────────────────

def _sourced_product(db, org, actor, *, applied_cost: int, source_cost: int):
    """적용 원가와 소싱 원가가 다른 상품 하나."""
    from app.services import sourcing_core as sc

    product = make_product(db, org_id=org, cost_amount=applied_cost)
    supplier = sc.create_supplier(db, org_id=org, actor_user_id=actor, name="공급처")
    src = sc.add_product_source(db, org_id=org, actor_user_id=actor,
                                product_id=product,
                                supplier_site_id=uuid.UUID(supplier["id"]),
                                currency="KRW", is_primary=True)
    sc.add_cost_version(db, org_id=org, actor_user_id=actor,
                        product_source_id=uuid.UUID(src["id"]),
                        purchase_price=source_cost, currency="KRW",
                        valid_from=date(2026, 1, 1))
    db.execute(text("UPDATE product SET status = 'ACTIVE' WHERE id = :p"),
               {"p": str(product)})
    db.commit()
    return product


def test_propose_creates_approval_without_touching_the_value(db, org, actor):
    """제안은 승인 요청까지다. 원가는 그대로다."""
    product = _sourced_product(db, org, actor, applied_cost=5_000, source_cost=8_000)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor,
        automation_type="SOURCE_COST_MONITOR", enabled=True, mode="PROPOSE",
        guardrails={"min_diff_rate": "0.1"},
    )

    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    assert result.counts() == {"PROPOSED": 1}

    row = db.execute(text("""
        SELECT status, request_type FROM approval_request
         WHERE org_id = :o AND target_id = :p AND deleted_at IS NULL
    """), {"o": str(org), "p": str(product)}).mappings().one()
    assert row["status"] == "PENDING"
    assert row["request_type"] == "COST_CHANGE"
    # 실제 값은 변하지 않았다.
    assert db.execute(text("SELECT cost_amount FROM product WHERE id = :p"),
                      {"p": str(product)}).scalar() == 5_000


def test_scheduled_run_proposes_without_a_human(db, org, actor):
    """무인 실행이 아무것도 안 만들면 자동화의 존재 이유가 없다.

    사람 이름을 빌리지도 않는다 — 승인함에 "김대표가 새벽 3시에 요청함" 으로
    뜨면 승인자가 틀린 전제 위에서 판단한다.
    """
    product = _sourced_product(db, org, actor, applied_cost=5_000, source_cost=8_000)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor,
        automation_type="SOURCE_COST_MONITOR", enabled=True, mode="PROPOSE",
        guardrails={"min_diff_rate": "0.1"},
    )

    # actor_user_id 없이 = 스케줄러가 도는 것과 같다.
    result = runners.run_policy(db, org_id=org, policy=policy, trigger="SCHEDULED")
    assert result.counts() == {"PROPOSED": 1}

    row = db.execute(text("""
        SELECT requested_by, requested_by_type,
               requested_by_service_account_id IS NOT NULL AS has_sa
          FROM approval_request WHERE org_id = :o AND target_id = :p
    """), {"o": str(org), "p": str(product)}).mappings().one()
    assert row["requested_by"] is None
    assert row["requested_by_type"] == "SERVICE_ACCOUNT"
    assert row["has_sa"] is True

    # 그 주체는 토큰이 없다 — 이름일 뿐 접속 수단이 아니다.
    tokens = db.execute(text("""
        SELECT COUNT(*) FROM api_token t
          JOIN service_account s ON s.id = t.service_account_id
         WHERE s.org_id = :o AND s.name = :n
    """), {"o": str(org), "n": runners.AUTOMATION_ACTOR_NAME}).scalar()
    assert tokens == 0


def test_second_scan_does_not_duplicate_the_proposal(db, org, actor):
    """매 실행마다 같은 제안이 쌓이면 승인함을 아무도 보지 않게 된다."""
    product = _sourced_product(db, org, actor, applied_cost=5_000, source_cost=8_000)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor,
        automation_type="SOURCE_COST_MONITOR", enabled=True, mode="PROPOSE",
        guardrails={"min_diff_rate": "0.1"},
    )

    runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    second = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)

    assert second.counts() == {"BLOCKED": 1}
    assert second.actions[0].blocked_by == "PENDING_PROPOSAL_EXISTS"
    assert db.execute(text("""
        SELECT COUNT(*) FROM approval_request
         WHERE org_id = :o AND target_id = :p AND deleted_at IS NULL
    """), {"o": str(org), "p": str(product)}).scalar() == 1


def test_rejected_proposal_is_not_repeated(db, org, actor):
    """거절은 "이 값은 아니다" 라는 답이다. 같은 답을 매일 다시 묻지 않는다."""
    product = _sourced_product(db, org, actor, applied_cost=5_000, source_cost=8_000)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor,
        automation_type="SOURCE_COST_MONITOR", enabled=True, mode="PROPOSE",
        guardrails={"min_diff_rate": "0.1"},
    )
    runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)

    # 사람이 거절했다.
    db.execute(text("""
        UPDATE approval_request SET status = 'REJECTED'
         WHERE org_id = :o AND target_id = :p
    """), {"o": str(org), "p": str(product)})
    db.commit()

    again = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    assert again.counts() == {"BLOCKED": 1}
    assert again.actions[0].blocked_by == "ALREADY_REJECTED"


def test_new_evidence_after_rejection_is_proposed_again(db, org, actor):
    """원가가 또 바뀌었으면 새로운 근거다 — 그때는 다시 제안한다."""
    from app.services import sourcing_core as sc

    product = _sourced_product(db, org, actor, applied_cost=5_000, source_cost=8_000)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor,
        automation_type="SOURCE_COST_MONITOR", enabled=True, mode="PROPOSE",
        guardrails={"min_diff_rate": "0.1"},
    )
    runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    db.execute(text("""
        UPDATE approval_request SET status = 'REJECTED'
         WHERE org_id = :o AND target_id = :p
    """), {"o": str(org), "p": str(product)})
    db.commit()

    # 공급처가 원가를 또 올렸다.
    src = db.execute(text("""
        SELECT id FROM product_source WHERE product_id = :p AND is_primary
    """), {"p": str(product)}).scalar()
    sc.add_cost_version(db, org_id=org, actor_user_id=actor,
                        product_source_id=uuid.UUID(str(src)),
                        purchase_price=9_500, currency="KRW",
                        valid_from=date(2026, 6, 1))
    db.commit()

    again = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    assert again.counts() == {"PROPOSED": 1}


def test_no_threshold_means_no_proposal(db, org, actor):
    """기준이 없으면 제안하지 않는다. 임의 기준을 만들지 않는다."""
    _sourced_product(db, org, actor, applied_cost=5_000, source_cost=8_000)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor,
        automation_type="SOURCE_COST_MONITOR", enabled=True, mode="PROPOSE",
    )

    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    assert result.counts() == {"REPORTED": 1}
    assert db.execute(text("""
        SELECT COUNT(*) FROM approval_request WHERE org_id = :o
    """), {"o": str(org)}).scalar() == 0


# ── MARGIN_MONITOR → PRICE_CHANGE 제안 ───────────────────────────

def test_margin_monitor_proposes_price_change(db, org, actor):
    """마진 미달이면 가격 변경을 **제안**한다. 가격은 그대로다."""
    product = make_product(db, org_id=org, cost_amount=9_000)
    _listed(db, org, product, regular_price=10_000)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="MARGIN_MONITOR",
        enabled=True, mode="PROPOSE", guardrails={"min_margin_rate": "0.3"},
    )

    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    assert result.counts() == {"PROPOSED": 1}

    row = db.execute(text("""
        SELECT status, request_type, proposed_changes_json::text AS changes
          FROM approval_request
         WHERE org_id = :o AND target_type = 'product_channel_listing'
           AND deleted_at IS NULL
    """), {"o": str(org)}).mappings().one()
    assert row["status"] == "PENDING"
    assert row["request_type"] == "CHANNEL_PRICE_CHANGE"
    # 원가 9,000 · 목표 30% → 9000/(1-0.3) = 12,858 (올림)
    assert "12858" in row["changes"]

    # 실제 가격은 바뀌지 않았다.
    assert db.execute(text("""
        SELECT regular_price FROM product_channel_price WHERE org_id = :o
    """), {"o": str(org)}).scalar() == 10_000


def test_price_proposal_is_not_duplicated(db, org, actor):
    product = make_product(db, org_id=org, cost_amount=9_000)
    _listed(db, org, product, regular_price=10_000)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="MARGIN_MONITOR",
        enabled=True, mode="PROPOSE", guardrails={"min_margin_rate": "0.3"},
    )
    runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    second = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)

    assert second.counts() == {"BLOCKED": 1}
    assert second.actions[0].blocked_by == "PENDING_PROPOSAL_EXISTS"
    assert db.execute(text("""
        SELECT COUNT(*) FROM approval_request
         WHERE org_id = :o AND target_type = 'product_channel_listing'
           AND deleted_at IS NULL
    """), {"o": str(org)}).scalar() == 1


def test_rejected_price_proposal_is_not_repeated(db, org, actor):
    product = make_product(db, org_id=org, cost_amount=9_000)
    _listed(db, org, product, regular_price=10_000)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="MARGIN_MONITOR",
        enabled=True, mode="PROPOSE", guardrails={"min_margin_rate": "0.3"},
    )
    runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    db.execute(text("""
        UPDATE approval_request SET status = 'REJECTED'
         WHERE org_id = :o AND target_type = 'product_channel_listing'
    """), {"o": str(org)})
    db.commit()

    again = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    assert again.actions[0].blocked_by == "ALREADY_REJECTED"


def test_no_price_proposal_when_target_is_already_met(db, org, actor):
    """올릴 이유가 없으면 제안하지 않는다."""
    product = make_product(db, org_id=org, cost_amount=1_000)
    _listed(db, org, product, regular_price=10_000)
    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="MARGIN_MONITOR",
        enabled=True, mode="PROPOSE", guardrails={"min_margin_rate": "0.3"},
    )
    result = runners.run_policy(db, org_id=org, policy=policy, actor_user_id=actor)
    assert result.actions == []
    assert db.execute(text("SELECT COUNT(*) FROM approval_request WHERE org_id = :o"),
                      {"o": str(org)}).scalar() == 0


def test_a_policy_that_already_ran_is_scheduled_again(db, org, actor):
    """두 번째 주기부터 스케줄이 죽지 않는지.

    `due_policies` 의 주기 계산은 **한 번도 실행된 적이 없었다** — 첫 실행은
    `last_run_at IS NULL` 분기가 전부 흡수하기 때문이다. 그래서 어떤 자동화든
    한 번 돌고 나면 다음부터 조용히 안 도는 상태였다(구문 오류).
    """
    from app.services.automation_runners import due_policies

    policy = auto.upsert_policy(
        db, org_id=org, actor_user_id=actor, automation_type="DAILY_REPORT",
        enabled=True, interval_minutes=5)

    # 첫 실행 전에는 무조건 대상이다.
    assert [p["automation_type"] for p in due_policies(db, org_id=org)] \
        == ["DAILY_REPORT"]

    # 방금 돈 것으로 만든다 → 아직 주기가 안 됐다.
    db.execute(text("""
        UPDATE automation_policy SET last_run_at = now() WHERE id = :i
    """), {"i": policy["id"]})
    db.commit()
    assert due_policies(db, org_id=org) == []

    # 주기가 지났다 → 다시 대상이다.
    db.execute(text("""
        UPDATE automation_policy SET last_run_at = now() - INTERVAL '10 minutes'
         WHERE id = :i
    """), {"i": policy["id"]})
    db.commit()
    assert [p["automation_type"] for p in due_policies(db, org_id=org)] \
        == ["DAILY_REPORT"]
