"""자동화 엔진 (M045).

목표
----
사용자가 매일 확인해야 하는 것을 **주문·클레임·중요 승인**으로 줄인다. 나머지는
Seller OS 가 스스로 돌아본다.

```
Event/Schedule → Automation Policy → Guardrail → Action → Audit
```

AI 가 없어도 돈다
--------------
이 모듈은 MCP 도 AI 도 import 하지 않는다. MCP 를 끄거나 지워도 마진 감시·원가
감시·마감 사전점검·일일 리포트는 그대로 동작한다. AI 는 나중에 이 결과를
**요약**할 뿐, 판단을 대신하지 않는다.

기존 구조를 재사용한다
------------------
새로 만든 것은 정책뿐이다. 나머지는 이미 있는 것을 쓴다.

```
승인이 필요한 변경   approval_request   (COST_CHANGE 등 기존 governance 그대로)
사람이 봐야 할 것    operation_issue    (dedupe·자동해소 규칙 그대로)
실행 이력           worker_run          (자동화 worker 한 번)
```

세 가지 모드
----------
```
REPORT_ONLY    관측만. 아무것도 쓰지 않는다.  ← 기본값
PROPOSE        승인 요청을 만든다. 사람이 승인해야 반영된다
AUTO_EXECUTE   한도 안에서 직접 실행한다
```

기본이 `REPORT_ONLY` 인 이유: 자동 실행은 사람이 켜는 결정이어야 한다. 기본으로
켜 두면 "언제 이게 켜졌지" 를 묻게 된다.

한도가 없으면 자동 실행하지 않는다
-----------------------------
`AUTO_EXECUTE` 인데 `guardrail_json` 이 비어 있으면 **실행하지 않고 제안으로
떨어진다.** 무제한 자동 실행을 허용하지 않는다.

고위험 행동은 자동화 대상이 아니다
-----------------------------
지급 실행·이체·월 재오픈·자격증명 변경·정산 수동매칭은 이 엔진이 하지 않는다.
돈이 나가거나 확정된 과거를 되돌리는 일이고, 되돌릴 수 없다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import ConflictError, NotFoundError, ValidationError

MODES = ("REPORT_ONLY", "PROPOSE", "AUTO_EXECUTE")

#: 지금 실제로 판단할 수 있는 것만 둔다.
#:
#: 재고 자동발주(`INVENTORY_REORDER`) 는 **없다.** 재고 데이터가 없어서
#: 동작하는 척만 하게 된다.
AUTOMATION_TYPES: dict[str, dict[str, Any]] = {
    "MARGIN_MONITOR": {
        "label": "마진 감시",
        "description": "판매 중인 상품의 예상 마진이 목표 아래로 떨어졌는지 본다.",
        # 이 자동화가 무엇을 할 수 있나. 없는 모드는 정책이 거부한다.
        "modes": ("REPORT_ONLY", "PROPOSE"),
        "guardrails": ("min_margin_rate", "max_actions_per_run"),
    },
    "SOURCE_COST_MONITOR": {
        "label": "소싱 원가 감시",
        "description": "공급처 원가와 적용 원가의 차이를 본다. 원가 채택은 승인을 거친다.",
        "modes": ("REPORT_ONLY", "PROPOSE"),
        "guardrails": ("min_diff_rate", "max_actions_per_run"),
    },
    "MONTH_CLOSE_PRECHECK": {
        "label": "마감 사전점검",
        "description": "열린 달의 마감 차단 항목을 미리 찾아 이슈로 남긴다.",
        "modes": ("REPORT_ONLY",),
        "guardrails": ("max_actions_per_run",),
    },
    "DAILY_REPORT": {
        "label": "일일 운영 리포트",
        "description": "매일의 주문·마진·정산·이슈를 한 장으로 모은다.",
        "modes": ("REPORT_ONLY",),
        "guardrails": (),
    },
    "ORDER_SYNC": {
        "label": "주문 자동 수집",
        "description": "판매처에서 주문을 가져온다. 커넥터가 있어야 동작한다.",
        "modes": ("REPORT_ONLY", "AUTO_EXECUTE"),
        # `overlap_days` 에 기본값이 없는 것이 의도다. 외부에 수정일이 없어
        # 매번 최근 구간을 통째로 다시 읽는데, 그 폭을 코드가 정하면 사용자는
        # 자기가 고르지 않은 범위로 외부 API 를 두들기게 된다.
        "guardrails": ("overlap_days", "max_actions_per_run"),
    },
    "CLAIM_SYNC": {
        "label": "취소·반품 자동 수집",
        "description": "판매처에서 취소·반품·교환 사실을 가져와 매출 조정을 만든다. "
                       "반품 처리를 대신하지는 않는다.",
        "modes": ("REPORT_ONLY", "AUTO_EXECUTE"),
        # 주문과 달리 "최근 며칠" 이 아니라 "클레임이 아직 생길 수 있는 주문
        # 기간" 이다. 조회 날짜가 클레임 날짜가 아니라 주문 날짜라서 그렇다.
        "guardrails": ("claim_lookback_days", "max_actions_per_run"),
    },
    "REORDER_MONITOR": {
        "label": "재주문 감시",
        "description": "재고 소진 속도를 보고 발주 시점이 된 상품을 찾는다. "
                       "발주는 초안까지만 만들고 확정은 사람이 한다.",
        # `AUTO_EXECUTE` 가 없다. 발주는 돈이 나가는 일이고, 판매속도는 시즌·
        # 광고·품절로 흔들리는 추정치다. 추정치로 돈을 쓰지 않는다.
        "modes": ("REPORT_ONLY", "PROPOSE"),
        # 앞의 둘은 한도가 아니라 **필수 파라미터**다. 기본값을 코드에 두지
        # 않으려고 여기에 뒀다 — 사용자가 정하지 않으면 이 자동화는 돌지 않는다.
        "guardrails": ("velocity_days", "coverage_days", "max_actions_per_run"),
    },
}

#: 이 엔진이 하지 않는 일. 목록으로 남겨 둬야 "왜 없지" 를 묻지 않는다.
NEVER_AUTOMATED = (
    "PAYOUT_EXECUTE",
    "BANK_TRANSFER",
    "MONTH_REOPEN",
    "CREDENTIAL_CHANGE",
    "MANUAL_SETTLEMENT_MATCH",
)

#: 한도 이름 → 값 검증. **모르는 키는 거부한다.**
#:
#: 검증하지 않는 설정 칸은 있으나 마나다 — 오타 하나(`max_action_per_run`)로
#: 한도가 조용히 사라지고, 사용자는 한도를 걸었다고 믿는다.
_GUARDRAIL_RULES: dict[str, tuple[Decimal, Decimal]] = {
    # 비율은 0~1. 0.15 = 15%.
    "min_margin_rate": (Decimal("0"), Decimal("1")),
    "min_diff_rate": (Decimal("0"), Decimal("1")),
    "max_change_rate": (Decimal("0"), Decimal("1")),
    # 금액·건수.
    "max_change_amount": (Decimal("0"), Decimal("100000000")),
    "max_actions_per_run": (Decimal("1"), Decimal("1000")),
    "daily_limit": (Decimal("1"), Decimal("1000")),
    # 일수. 판매속도를 며칠 치로 볼지, 발주로 며칠 치를 확보할지.
    #
    # 여기에 기본값이 없는 것이 의도다. "보통 30일" 을 코드에 넣으면 아무도
    # 자기 회전율에 맞게 정하지 않고, 나중에 그 숫자가 어디서 왔는지 모른다.
    "velocity_days": (Decimal("1"), Decimal("365")),
    "coverage_days": (Decimal("1"), Decimal("365")),
    # 주문을 몇 일 치나 겹쳐 다시 읽을지. 상한이 10 인 것은 취향이 아니라
    # 벤더가 한 요청에 받아 주는 최대 폭이다(docs/73 실측). 그보다 넓게 두면
    # 매 실행이 요청 여러 개로 쪼개져 한도만 태운다.
    "overlap_days": (Decimal("1"), Decimal("10")),
    # 클레임이 아직 생길 수 있는 주문 기간. 상한 180일은 "그보다 오래된 주문에
    # 반품이 들어오면 자동으로는 못 본다" 는 선언이다 — 그 경우는 수동
    # 백필로 처리한다. 매 실행이 상태 17개 × 창 개수만큼 요청을 내므로
    # 무제한으로 열어 두면 한도만 태운다.
    "claim_lookback_days": (Decimal("1"), Decimal("180")),
}


class AutomationBlockedError(Exception):
    """한도에 걸려 실행하지 않았다. 실패가 아니다."""

    def __init__(self, guardrail: str, message: str) -> None:
        super().__init__(message)
        self.guardrail = guardrail


def validate_guardrails(automation_type: str, guardrails: dict[str, Any]) -> dict[str, Any]:
    """모르는 키와 범위 밖 값을 거부한다."""
    spec = AUTOMATION_TYPES.get(automation_type)
    if spec is None:
        raise ValidationError(f"알 수 없는 자동화 유형입니다: {automation_type}")

    allowed = set(spec["guardrails"])
    cleaned: dict[str, Any] = {}
    for key, raw in (guardrails or {}).items():
        if key not in _GUARDRAIL_RULES:
            raise ValidationError(f"알 수 없는 한도입니다: {key}")
        if key not in allowed:
            raise ValidationError(
                f"{spec['label']} 에는 {key} 한도를 쓸 수 없습니다. "
                f"사용 가능: {', '.join(sorted(allowed)) or '없음'}",
            )
        try:
            value = Decimal(str(raw))
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"{key} 값이 숫자가 아닙니다.") from exc
        low, high = _GUARDRAIL_RULES[key]
        if not (low <= value <= high):
            raise ValidationError(f"{key} 는 {low}~{high} 사이여야 합니다.")
        cleaned[key] = str(value)
    return cleaned


# ──────────────────────────────────────────────────────────────────────────
# 정책
# ──────────────────────────────────────────────────────────────────────────

def list_types() -> list[dict[str, Any]]:
    """무엇을 자동화할 수 있는지. 화면이 목록을 하드코딩하지 않게."""
    return [
        {"automation_type": k, **v,
         "modes": list(v["modes"]), "guardrails": list(v["guardrails"])}
        for k, v in AUTOMATION_TYPES.items()
    ]


def upsert_policy(
    db: Session, *, org_id: UUID, actor_user_id: UUID, automation_type: str,
    name: str | None = None, enabled: bool = False, mode: str = "REPORT_ONLY",
    interval_minutes: int | None = None,
    guardrails: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """정책 생성·수정. 한 조직에 유형당 하나다."""
    spec = AUTOMATION_TYPES.get(automation_type)
    if spec is None:
        raise ValidationError(f"알 수 없는 자동화 유형입니다: {automation_type}")
    if mode not in MODES:
        raise ValidationError(f"알 수 없는 모드입니다: {mode}")
    if mode not in spec["modes"]:
        # 지원하지 않는 모드를 저장해 두면 실행 시점에 조용히 무시된다.
        raise ValidationError(
            f"{spec['label']} 는 {mode} 모드를 지원하지 않습니다. "
            f"가능: {', '.join(spec['modes'])}",
        )
    if interval_minutes is not None and interval_minutes < 5:
        # 외부 API rate limit 을 모르는 상태다. 5분보다 촘촘하게 두지 않는다.
        raise ValidationError("실행 주기는 5분 이상이어야 합니다.")

    clean_guardrails = validate_guardrails(automation_type, guardrails or {})

    existing = db.execute(text("""
        SELECT id, enabled, mode FROM automation_policy
         WHERE org_id = :o AND automation_type = :t AND deleted_at IS NULL
    """), {"o": str(org_id), "t": automation_type}).mappings().first()

    import json
    params = {
        "o": str(org_id), "t": automation_type,
        "n": (name or spec["label"]).strip(),
        "e": enabled, "m": mode, "iv": interval_minutes,
        "g": json.dumps(clean_guardrails, ensure_ascii=False),
        "s": json.dumps(scope or {}, ensure_ascii=False),
        # str(None) 은 'None' 문자열이 되어 UUID 컬럼에서 터진다.
        # 에러가 원인을 가리키지 않아 찾기 어렵다.
        "a": str(actor_user_id) if actor_user_id else None,
    }

    if existing is None:
        pid = db.execute(text("""
            INSERT INTO automation_policy
                   (org_id, automation_type, name, enabled, mode, interval_minutes,
                    guardrail_json, scope_json, created_by, updated_by)
            VALUES (:o, :t, :n, :e, :m, :iv, CAST(:g AS JSONB), CAST(:s AS JSONB),
                    :a, :a)
            RETURNING id
        """), params).scalar_one()
        action, before = "CREATE", None
    else:
        pid = existing["id"]
        params["i"] = str(pid)
        db.execute(text("""
            UPDATE automation_policy
               SET name = :n, enabled = :e, mode = :m, interval_minutes = :iv,
                   guardrail_json = CAST(:g AS JSONB), scope_json = CAST(:s AS JSONB),
                   updated_at = now(), updated_by = :a
             WHERE id = :i
        """), params)
        action = "UPDATE"
        before = {"enabled": existing["enabled"], "mode": existing["mode"]}

    # 자동화를 켜고 끄는 것은 감사 대상이다 — "언제부터 자동으로 돌았나" 를
    # 물을 수 있어야 한다.
    record_audit(
        db, org_id=org_id, domain_table="automation_policy",
        domain_row_id=UUID(str(pid)), action=action, actor_user_id=actor_user_id,
        before=before,
        after={"enabled": enabled, "mode": mode, "guardrails": clean_guardrails},
    )
    db.commit()
    return get_policy(db, org_id=org_id, policy_id=UUID(str(pid)))


def get_policy(db: Session, *, org_id: UUID, policy_id: UUID) -> dict[str, Any]:
    row = db.execute(text("""
        SELECT id, automation_type, name, enabled, mode, interval_minutes,
               guardrail_json, scope_json, last_run_at, created_at
          FROM automation_policy
         WHERE id = :i AND org_id = :o AND deleted_at IS NULL
    """), {"i": str(policy_id), "o": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("자동화 정책을 찾을 수 없습니다.")
    d = dict(row)
    d["id"] = str(d["id"])
    spec = AUTOMATION_TYPES.get(d["automation_type"], {})
    d["label"] = spec.get("label", d["automation_type"])
    d["description"] = spec.get("description")
    d["supported_modes"] = list(spec.get("modes", ()))
    d["available_guardrails"] = list(spec.get("guardrails", ()))
    return d


def list_policies(db: Session, *, org_id: UUID) -> list[dict[str, Any]]:
    """등록된 정책 + **아직 만들지 않은 유형**.

    목록에 없는 자동화는 사용자에게 존재하지 않는 것과 같다. 무엇을 켤 수
    있는지 전부 보여주고, 안 만든 것은 꺼진 상태로 표시한다.
    """
    ids = db.execute(text("""
        SELECT id FROM automation_policy
         WHERE org_id = :o AND deleted_at IS NULL ORDER BY automation_type
    """), {"o": str(org_id)}).scalars().all()
    existing = [get_policy(db, org_id=org_id, policy_id=UUID(str(i))) for i in ids]
    have = {p["automation_type"] for p in existing}

    for code, spec in AUTOMATION_TYPES.items():
        if code in have:
            continue
        existing.append({
            "id": None, "automation_type": code, "name": spec["label"],
            "enabled": False, "mode": "REPORT_ONLY", "interval_minutes": None,
            "guardrail_json": {}, "scope_json": {}, "last_run_at": None,
            "created_at": None, "label": spec["label"],
            "description": spec["description"],
            "supported_modes": list(spec["modes"]),
            "available_guardrails": list(spec["guardrails"]),
        })
    existing.sort(key=lambda p: p["automation_type"])
    return existing


# ──────────────────────────────────────────────────────────────────────────
# 실행
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class ActionRecord:
    action_type: str
    outcome: str
    reason: str | None = None
    target_table: str | None = None
    target_id: UUID | None = None
    blocked_by: str | None = None
    approval_request_id: UUID | None = None
    operation_issue_id: UUID | None = None


@dataclass
class RunResult:
    run_id: str
    policy_id: str
    automation_type: str
    mode: str
    #: 무엇이 이 실행을 시켰나. `automation_run.trigger` 에 적힌 값 그대로다.
    #:
    #: 실행기(runner)가 이 값을 봐야 하는 이유: 외부 동기화는 자기 실행
    #: 기록(`external_sync_run`)을 따로 남기는데, 거기에 `SCHEDULED` 를
    #: 못박아 두면 기동 직후 따라잡기가 주기 실행으로 기록된다(M058).
    trigger: str = "SCHEDULED"
    status: str = "RUNNING"
    evaluated: int = 0
    actions: list[ActionRecord] = field(default_factory=list)
    skipped: int = 0
    error_code: str | None = None

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for a in self.actions:
            out[a.outcome] = out.get(a.outcome, 0) + 1
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "policy_id": self.policy_id,
            "automation_type": self.automation_type, "mode": self.mode,
            "trigger": self.trigger,
            "status": self.status, "evaluated": self.evaluated,
            "action_count": len(self.actions), "skipped": self.skipped,
            "outcomes": self.counts(), "error_code": self.error_code,
        }


def effective_mode(policy: dict[str, Any]) -> str:
    """실제로 적용될 모드.

    `AUTO_EXECUTE` 인데 한도가 비어 있으면 **제안으로 떨어뜨린다.** 무제한
    자동 실행을 허용하지 않는다. 조용히 실행하는 것보다 조용히 제안하는 편이
    되돌리기 쉽다.
    """
    if policy["mode"] == "AUTO_EXECUTE" and not (policy.get("guardrail_json") or {}):
        return "PROPOSE"
    return policy["mode"]


def start_run(
    db: Session, *, org_id: UUID, policy: dict[str, Any],
    trigger: str = "SCHEDULED", worker_run_id: UUID | None = None,
) -> RunResult:
    mode = effective_mode(policy)
    run_id = db.execute(text("""
        INSERT INTO automation_run
               (org_id, policy_id, mode, trigger, worker_run_id)
        VALUES (:o, :p, :m, :t, :w) RETURNING id
    """), {"o": str(org_id), "p": policy["id"], "m": mode, "t": trigger,
           "w": str(worker_run_id) if worker_run_id else None}).scalar_one()
    db.commit()
    return RunResult(run_id=str(run_id), policy_id=policy["id"],
                     automation_type=policy["automation_type"], mode=mode,
                     trigger=trigger)


def finish_run(db: Session, *, result: RunResult, status: str) -> None:
    result.status = status
    for a in result.actions:
        db.execute(text("""
            INSERT INTO automation_action
                   (org_id, run_id, action_type, target_table, target_id,
                    outcome, reason, blocked_by,
                    approval_request_id, operation_issue_id)
            SELECT r.org_id, :run, :at, :tt, :ti, :oc, :why, :blocked, :ar, :oi
              FROM automation_run r WHERE r.id = :run
        """), {
            "run": result.run_id, "at": a.action_type, "tt": a.target_table,
            "ti": str(a.target_id) if a.target_id else None,
            "oc": a.outcome, "why": a.reason, "blocked": a.blocked_by,
            "ar": str(a.approval_request_id) if a.approval_request_id else None,
            "oi": str(a.operation_issue_id) if a.operation_issue_id else None,
        })

    db.execute(text("""
        UPDATE automation_run
           SET status = :s, finished_at = now(),
               evaluated_count = :e, action_count = :a, skipped_count = :k,
               error_code = :ec
         WHERE id = :i
    """), {"i": result.run_id, "s": status, "e": result.evaluated,
           "a": len(result.actions), "k": result.skipped,
           "ec": result.error_code})
    db.execute(text("""
        UPDATE automation_policy SET last_run_at = now() WHERE id = :i
    """), {"i": result.policy_id})
    db.commit()


def guardrail(policy: dict[str, Any], key: str) -> Decimal | None:
    raw = (policy.get("guardrail_json") or {}).get(key)
    return Decimal(str(raw)) if raw is not None else None


def check_action_budget(policy: dict[str, Any], result: RunResult) -> None:
    """한 번에 너무 많이 하지 않는다.

    감시가 잘못 걸리면 한 번에 수백 건의 승인 요청이 쏟아진다. 그러면 사람은
    승인함을 아예 보지 않게 되고, 자동화가 오히려 통제를 없앤다.
    """
    limit = guardrail(policy, "max_actions_per_run")
    if limit is not None and len(result.actions) >= int(limit):
        raise AutomationBlockedError(
            "max_actions_per_run",
            f"한 번에 최대 {int(limit)}건까지만 처리합니다.",
        )


def run_history(
    db: Session, *, org_id: UUID, policy_id: UUID | None = None, limit: int = 20,
) -> list[dict[str, Any]]:
    where = ["r.org_id = :o"]
    params: dict[str, Any] = {"o": str(org_id), "lim": limit}
    if policy_id:
        where.append("r.policy_id = :p")
        params["p"] = str(policy_id)

    rows = db.execute(text(f"""
        SELECT r.id, r.policy_id, p.automation_type, p.name, r.mode, r.trigger,
               r.status, r.evaluated_count, r.action_count, r.skipped_count,
               r.started_at, r.finished_at, r.error_code
          FROM automation_run r
          JOIN automation_policy p ON p.id = r.policy_id
         WHERE {" AND ".join(where)}
         ORDER BY r.started_at DESC LIMIT :lim
    """), params).mappings().all()

    out = []
    for r in rows:
        d = dict(r)
        d["id"] = str(d["id"])
        d["policy_id"] = str(d["policy_id"])
        # 행동 결과를 함께 준다 — 건수만으로는 무엇을 했는지 알 수 없다.
        d["outcomes"] = {
            k: v for k, v in db.execute(text("""
                SELECT outcome, COUNT(*) FROM automation_action
                 WHERE run_id = :r GROUP BY outcome
            """), {"r": d["id"]}).all()
        }
        out.append(d)
    return out


def run_actions(
    db: Session, *, org_id: UUID, run_id: UUID, limit: int = 200,
) -> list[dict[str, Any]]:
    rows = db.execute(text("""
        SELECT action_type, target_table, target_id, outcome, reason,
               blocked_by, approval_request_id, operation_issue_id, created_at
          FROM automation_action
         WHERE run_id = :r AND org_id = :o
         ORDER BY created_at LIMIT :lim
    """), {"r": str(run_id), "o": str(org_id), "lim": limit}).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("target_id", "approval_request_id", "operation_issue_id"):
            d[k] = str(d[k]) if d[k] else None
        out.append(d)
    return out


def assert_not_high_risk(action_type: str) -> None:
    """자동화가 손대지 않는 행동.

    돈이 나가거나 확정된 과거를 되돌리는 일은 되돌릴 수 없다. 한도를 걸어도
    한도 계산이 틀리면 끝이다 — 그래서 아예 경로를 만들지 않는다.
    """
    if action_type in NEVER_AUTOMATED:
        raise ConflictError(
            f"{action_type} 은 자동으로 실행하지 않습니다. 사람이 화면에서 처리합니다.",
            detail={"never_automated": list(NEVER_AUTOMATED)},
        )
