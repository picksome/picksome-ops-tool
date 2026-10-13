"""클레임 자동 동기화 오케스트레이션 (Phase 3).

주문 동기화를 복사하지 않았다
------------------------
겉모습은 닮았지만 **창을 잡는 방법이 다르다.** 그 차이가 이 파일의 이유다.

```
주문 조회   조회 날짜 = 주문 날짜   최근 며칠만 보면 새 주문을 다 본다
클레임 조회 조회 날짜 = 주문 날짜   최근 며칠만 보면 옛 주문의 클레임을 못 본다
```

둘 다 주문 날짜다. 주문에서는 그게 맞다 — 새 주문은 항상 최근이다. 클레임은
아니다. **오늘 발생한 반품이라도 원 주문이 6월이면 6월 창으로만 찾을 수 있다.**
실측으로 확인했다(docs/80): 6/18 주문의 취소가 8/05~8/15 창에서는 0건이다.

그래서 클레임 창은 "최근 며칠" 이 아니라 **"클레임이 아직 생길 수 있는 주문
기간"** 이다. 반품 가능 기간이 얼마인지는 사업이 정하므로 정책 값으로 둔다.

`claim_divi` 가 필수다
------------------
"전체 클레임" 을 한 번에 받는 방법이 없다. 상태 17개를 각각 부른다 —
창 하나당 요청이 17번이다. 창을 넓게 잡으면 요청이 곱으로 늘어난다.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError

_log = logging.getLogger("claim_sync")

AUTOMATION_TYPE = "CLAIM_SYNC"
#: `external_sync_run.sync_type`. **주문과 다른 값이다** (M057).
#:
#: 예전에는 `ORDERS` 를 썼다 — "무엇을 읽었는지는 자동화 실행 기록이 구분한다"
#: 는 이유였는데, `external_sync_run.worker_run_id` 가 비어 있어서 실제로는
#: 구분되지 않았다. 그래서 주문 상태 화면이 클레임 실행의 숫자를 주문 숫자로
#: 보여주고, 클레임 실패가 "주문 동기화 실패" 로 뜨고 있었다.
SYNC_TYPE = "CLAIMS"

#: advisory lock 이름공간. **주문과 다른 값이다** — 주문 동기화와 클레임
#: 동기화는 다른 일이고, 하나가 다른 하나를 막으면 안 된다.
_LOCK_NAMESPACE = 0x434C4D53  # 'CLMS'

#: 클레임이 아직 생길 수 있는 주문 기간(일). 기본값을 코드에 두지 않는다 —
#: 반품 가능 기간은 마켓·상품·사업마다 다르다.
LOOKBACK_GUARDRAIL = "claim_lookback_days"

SCOPE_ACCOUNTS = "accounts"
SCOPE_ACCOUNT_ID = "external_account_id"
SCOPE_INITIAL_FROM = "initial_sync_from"

SUCCESS = "SUCCESS"
PARTIAL = "PARTIAL"
FAILED = "FAILED"
SKIPPED = "SKIPPED"

NOT_CONFIGURED = "NOT_CONFIGURED"
ALREADY_RUNNING = "ALREADY_RUNNING"
CONNECTOR_UNAVAILABLE = "CONNECTOR_UNAVAILABLE"
CREDENTIAL_NOT_CONFIGURED = "CREDENTIAL_NOT_CONFIGURED"
RECONCILIATION_FAILED = "RECONCILIATION_FAILED"


@dataclass
class ClaimSyncOutcome:
    """계정 하나의 클레임 동기화 결과. **개인정보 없음.**"""

    external_account_id: UUID
    status: str
    reason: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    sync_run_id: UUID | None = None
    failed_windows: int = 0
    #: 벤더 한도로 나눈 창의 총 개수. 오래 멈췄다가 돌면 커진다.
    windows: int = 0
    ingest: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "external_account_id": str(self.external_account_id),
            "status": self.status, "reason": self.reason,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "sync_run_id": str(self.sync_run_id) if self.sync_run_id else None,
            "failed_windows": self.failed_windows, "windows": self.windows,
            "ingest": self.ingest,
        }


class ClaimSyncAlreadyRunningError(Exception):
    """같은 계정에 대해 클레임 동기화가 이미 돌고 있다."""


# ── single-flight ───────────────────────────────────────────────────────

def _lock_key(external_account_id: UUID) -> int:
    folded = external_account_id.int % (2**31 - 1)
    return folded or 1


@contextmanager
def single_flight(db: Session, external_account_id: UUID) -> Iterator[None]:
    """계정 하나에 대해 클레임 동기화는 하나만.

    **주문 동기화와는 겹칠 수 있다.** 이름공간이 달라 서로 막지 않는다 —
    둘은 다른 일이고, 하나가 느리다고 다른 하나를 멈추면 주문이 밀린다.
    같은 `sales_order` 를 건드리지 않기 때문에 안전하다: 클레임은 조정 행만
    만들고 주문 행은 **읽기만** 한다.
    """
    from app.services.order_sync import _new_connection

    conn = _new_connection(db)
    key = _lock_key(external_account_id)
    try:
        acquired = bool(conn.execute(
            text("SELECT pg_try_advisory_lock(:ns, :k)"),
            {"ns": _LOCK_NAMESPACE, "k": key},
        ).scalar())
        if not acquired:
            raise ClaimSyncAlreadyRunningError(str(external_account_id))
        yield
    finally:
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:ns, :k)"),
                         {"ns": _LOCK_NAMESPACE, "k": key})
        except Exception:  # noqa: BLE001
            pass
        conn.close()


# ── 설정 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClaimAccountConfig:
    external_account_id: UUID
    initial_sync_from: date
    lookback_days: int


def read_config(policy: dict[str, Any]) -> dict[UUID, ClaimAccountConfig]:
    """정책에서 계정별 설정을 읽는다. **빠진 값을 채우지 않는다.**"""
    guardrails = policy.get("guardrail_json") or {}
    raw = guardrails.get(LOOKBACK_GUARDRAIL)
    if raw is None:
        return {}
    lookback = int(float(raw))
    if lookback < 1:
        raise ValidationError("클레임 조회 기간은 1일 이상이어야 합니다.")

    entries = (policy.get("scope_json") or {}).get(SCOPE_ACCOUNTS) or []
    if not isinstance(entries, list):
        raise ValidationError("클레임 동기화 대상 계정 설정이 목록이 아닙니다.")

    out: dict[UUID, ClaimAccountConfig] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValidationError("클레임 동기화 계정 설정이 올바르지 않습니다.")
        raw_id, raw_from = entry.get(SCOPE_ACCOUNT_ID), entry.get(SCOPE_INITIAL_FROM)
        if not raw_id or not raw_from:
            raise ValidationError(
                "클레임 동기화 계정 설정에는 계정과 시작일이 모두 필요합니다.")
        try:
            account_id = UUID(str(raw_id))
            initial_from = date.fromisoformat(str(raw_from))
        except ValueError as exc:
            raise ValidationError("클레임 동기화 계정 설정 형식이 올바르지 않습니다.") from exc
        out[account_id] = ClaimAccountConfig(
            external_account_id=account_id, initial_sync_from=initial_from,
            lookback_days=lookback)
    return out


def compute_window(
    *, now: datetime, config: ClaimAccountConfig,
    last_success_at: datetime | None = None,
) -> tuple[datetime, datetime]:
    """조회할 **주문 기간**.

    ```
    anchor       = min(now, 마지막 성공)          ← 없으면 now
    window_start = max(initial_sync_from, anchor - claim_lookback_days)
    window_end   = now
    ```

    `lookback` 이 주문 동기화의 `overlap` 보다 훨씬 길어야 하는 이유가 여기
    있다. 이 창은 "언제 클레임이 생겼나" 가 아니라 **"어느 시기 주문의
    클레임을 볼 것인가"** 다. 30일로 두면 31일 전 주문의 오늘 반품을 못 본다.

    앞을 자르는 것은 주문과 같다 — cutover 이전으로는 내려가지 않는다.

    `anchor` 도 주문과 같다. `lookback` 이 충분히 길면 며칠 멈춰도 창이 이미
    그 기간을 덮지만, **그건 사용자가 넣은 숫자에 기대는 것이다.** 20일을
    넣어 두고 한 달을 꺼 두면 그 사이 주문의 클레임을 못 본다. 창이 짧아지는
    방향으로는 움직이지 않으므로 넣어 둔 `lookback` 은 그대로 보장된다.
    """
    from app.services.order_sync import align_tz

    anchor = now
    if last_success_at is not None:
        anchor = min(align_tz(last_success_at, now), now)
    lookback_start = anchor - timedelta(days=config.lookback_days)
    cutover = datetime.combine(config.initial_sync_from, datetime.min.time())
    cutover = align_tz(cutover, lookback_start)
    start = max(lookback_start, cutover)
    if start > now:
        start = now
    return start, now


def last_success_at(
    db: Session, *, org_id: UUID, external_account_id: UUID,
) -> datetime | None:
    """이 계정에서 클레임을 마지막으로 **성공적으로** 읽은 시각.

    주문과 달리 `external_account.last_successful_sync_at` 을 쓰지 않는다.
    그 칸은 주문 동기화가 소유한다 — 클레임이 같이 쓰면 한쪽만 돌아도 둘 다
    최신인 것처럼 보이고, 그 거짓이 바로 M057 이 고친 문제다.

    대신 이 계정의 `CLAIMS` 실행 기록에서 마지막 성공을 읽는다. `PARTIAL` 은
    성공이 아니다 — 못 읽은 창이 다음 실행의 창에 다시 들어와야 한다.
    """
    return db.execute(text("""
        SELECT max(finished_at) FROM external_sync_run
         WHERE org_id = :o AND external_account_id = :a
           AND sync_type = :t AND status = 'SUCCESS'
    """), {"o": str(org_id), "a": str(external_account_id),
           "t": SYNC_TYPE}).scalar()


# ── 실행 기록 ────────────────────────────────────────────────────────────

def _start_run(
    db: Session, *, org_id: UUID, account_id: UUID, trigger: str,
    actor_user_id: UUID | None,
) -> UUID:
    """실행 기록 시작. 주문 동기화와 같은 규칙을 쓴다.

    `created_by` 는 실제로 `app_user` 에 있을 때만 채운다 — 자동화는 서비스
    계정으로 돌고, 그 id 를 넣으면 외래키에서 죽는다.
    """
    if actor_user_id is not None:
        found = db.execute(text("""
            SELECT 1 FROM app_user WHERE id = :i AND org_id = :o
        """), {"i": str(actor_user_id), "o": str(org_id)}).first()
        if not found:
            actor_user_id = None

    row = db.execute(text("""
        INSERT INTO external_sync_run
               (org_id, external_account_id, sync_type, trigger, status,
                started_at, created_by)
        VALUES (:org, :a, :t, :tr, 'RUNNING', clock_timestamp(), :actor)
        RETURNING id
    """), {"org": str(org_id), "a": str(account_id), "t": SYNC_TYPE,
           "tr": trigger,
           "actor": str(actor_user_id) if actor_user_id else None}).scalar_one()
    db.commit()
    return UUID(str(row))


def _finish_run(db: Session, *, run_id: UUID, outcome: ClaimSyncOutcome) -> None:
    """마감. **벤더 응답을 남기지 않는다** — 클레임 응답에는 개인정보가 있다."""
    ing = outcome.ingest or {}
    fin = ing.get("financial") or {}
    db.execute(text("""
        UPDATE external_sync_run
           SET status = :s, finished_at = now(),
               read_count = :read, created_count = :created,
               updated_count = :updated, skipped_count = :skipped,
               conflict_count = :conflicts, error_count = :errors,
               error_code = :ec, error_message = :em
         WHERE id = :i
    """), {
        "i": str(run_id), "s": outcome.status,
        "read": ing.get("fetched"), "created": ing.get("created"),
        "updated": ing.get("updated"), "skipped": ing.get("unchanged"),
        # 사람이 봐야 하는 것 = 원 주문/라인을 못 찾았거나 금액을 못 정한 것.
        #
        # `unresolved` 에는 원 주문을 못 찾은 건이 **더는 들어 있지 않다**
        # (M054 로 `NO_LEDGER_IMPACT` 로 분리). 그 전에는 같은 클레임이
        # `unmatched_order` 와 `unresolved` 양쪽에 잡혀 실측 14건이 28로
        # 세어졌다.
        "conflicts": (ing.get("unmatched_order", 0) + ing.get("unmatched_line", 0)
                      + fin.get("unresolved", 0)),
        "errors": ing.get("invalid"),
        "ec": outcome.reason,
        "em": _REASON_MESSAGE.get(outcome.reason or ""),
    })
    db.commit()


_REASON_MESSAGE = {
    "AUTH_FAILED": "NEXT ENGINE 이 자격증명을 거부했습니다.",
    "RATE_LIMITED": "NEXT ENGINE 요청 한도에 걸렸습니다.",
    "TIMEOUT": "NEXT ENGINE 응답이 시간 안에 오지 않았습니다.",
    "SERVER_ERROR": "NEXT ENGINE 서버가 오류를 반환했습니다.",
    "SCHEMA_CHANGED": "NEXT ENGINE 클레임 응답 형식이 예상과 다릅니다.",
    "PAGINATION_STALLED": "NEXT ENGINE 클레임 페이지가 넘어가지 않습니다.",
    RECONCILIATION_FAILED: "건수 대사가 맞지 않아 성공으로 기록하지 않았습니다.",
    CREDENTIAL_NOT_CONFIGURED: "자격증명이 설정되지 않았습니다.",
    CONNECTOR_UNAVAILABLE: "이 외부 시스템의 연동이 아직 없습니다.",
    NOT_CONFIGURED: "클레임 동기화 설정이 완료되지 않았습니다.",
}

#: 다시 시도해도 결과가 같은 범주. 남은 창을 더 부르지 않는다.
_NO_IMMEDIATE_RETRY = frozenset({
    "AUTH_FAILED", "RATE_LIMITED", "SCHEMA_CHANGED", "PAGINATION_STALLED",
})


# ── 동기화 ──────────────────────────────────────────────────────────────

def sync_account(
    db: Session, *, org_id: UUID, external_account_id: UUID,
    config: ClaimAccountConfig, actor_user_id: UUID,
    trigger: str = "SCHEDULED", now: datetime | None = None,
    client: Any = None,
) -> ClaimSyncOutcome:
    """계정 하나의 클레임을 읽어 적재한다. **수동·자동이 같은 입구를 쓴다.**"""
    from app.connectors.next_engine.claim_client import split_claim_windows
    from app.services import claim_ingest as ci
    from app.services.credential_resolver import resolve_for_account
    from app.services.order_sync import classify_error

    now = now or datetime.now()
    outcome = ClaimSyncOutcome(external_account_id=external_account_id, status=FAILED)

    with single_flight(db, external_account_id):
        # 못 돈 기간을 창에 포함시킨다 (`compute_window` 참고). 잠금을 잡은
        # 뒤에 읽어야 앞선 실행이 남긴 성공을 본다.
        start, end = compute_window(
            now=now, config=config,
            last_success_at=last_success_at(
                db, org_id=org_id, external_account_id=external_account_id))
        outcome.window_start, outcome.window_end = start, end
        run_id = _start_run(db, org_id=org_id, account_id=external_account_id,
                            trigger=trigger, actor_user_id=actor_user_id)
        outcome.sync_run_id = run_id

        try:
            api_key = resolve_for_account(
                db, org_id=org_id, account_id=external_account_id,
            ).as_dict()["api_key"]
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            outcome.status = FAILED
            outcome.reason = CREDENTIAL_NOT_CONFIGURED
            _log.warning("클레임 동기화 자격증명 실패: %s", type(exc).__name__)
            _finish_run(db, run_id=run_id, outcome=outcome)
            return outcome

        api = client or _default_client()
        claims: list[Any] = []
        windows = split_claim_windows(
            start, end, max_days=int(getattr(api, "max_window_days", 10)))
        outcome.windows = len(windows)
        if len(windows) > 1:
            _log.info("클레임 따라잡기 창 %d개 (%s ~ %s)",
                      len(windows), start.date(), end.date())
        for w_start, w_end in windows:
            try:
                claims += api.fetch_claims(api_key=api_key, start=w_start, end=w_end)
            except Exception as exc:  # noqa: BLE001
                outcome.failed_windows += 1
                outcome.reason = classify_error(exc)
                _log.warning("클레임 조회 실패(%s)", outcome.reason)
                if outcome.reason in _NO_IMMEDIATE_RETRY:
                    break

        # 같은 클레임이 상태별·창별로 두 번 올 수 있다. 적재 전에 접는다.
        claims = list({c.ne_claim_no: c for c in claims if c.ne_claim_no}.values())

        try:
            result = ci.ingest_claims(
                db, org_id=org_id, actor_user_id=actor_user_id,
                external_account_id=external_account_id, claims=claims, now=now)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            outcome.status = FAILED
            outcome.reason = outcome.reason or classify_error(exc)
            _log.exception("클레임 적재 실패")
            _finish_run(db, run_id=run_id, outcome=outcome)
            return outcome

        outcome.ingest = result.as_dict()
        if not result.reconciles:
            outcome.status = FAILED
            outcome.reason = RECONCILIATION_FAILED
        elif outcome.failed_windows:
            outcome.status = PARTIAL
        else:
            outcome.status = SUCCESS
            outcome.reason = None

        _finish_run(db, run_id=run_id, outcome=outcome)

    return outcome


def _default_client() -> Any:
    from app.connectors.next_engine.claim_client import NextEngineClaimClient

    return NextEngineClaimClient()


def sync_org(
    db: Session, *, org_id: UUID, policy: dict[str, Any], actor_user_id: UUID,
    trigger: str = "SCHEDULED", now: datetime | None = None, client: Any = None,
) -> list[ClaimSyncOutcome]:
    """정책에 설정된 계정을 순서대로. 한 계정의 실패가 다음을 막지 않는다."""
    from app.services.order_sync import _account_blocker, classify_error, ready_accounts

    configs = read_config(policy)
    if not configs:
        return []

    accounts = {UUID(str(a["id"])): a for a in ready_accounts(db, org_id=org_id)}
    outcomes: list[ClaimSyncOutcome] = []

    for account_id, config in configs.items():
        account = accounts.get(account_id)
        if account is None:
            outcomes.append(ClaimSyncOutcome(
                external_account_id=account_id, status=SKIPPED,
                reason=NOT_CONFIGURED))
            continue
        blocker = _account_blocker(db, org_id=org_id, account=account)
        if blocker is not None:
            outcomes.append(ClaimSyncOutcome(
                external_account_id=account_id, status=SKIPPED, reason=blocker))
            continue
        try:
            outcomes.append(sync_account(
                db, org_id=org_id, external_account_id=account_id, config=config,
                actor_user_id=actor_user_id, trigger=trigger, now=now,
                client=client))
        except ClaimSyncAlreadyRunningError:
            outcomes.append(ClaimSyncOutcome(
                external_account_id=account_id, status=SKIPPED,
                reason=ALREADY_RUNNING))
        except Exception as exc:  # noqa: BLE001 — 계정 단위 격리
            db.rollback()
            _log.exception("클레임 동기화 실패 account=%s", account_id)
            outcomes.append(ClaimSyncOutcome(
                external_account_id=account_id, status=FAILED,
                reason=classify_error(exc)))
    return outcomes


# ── 화면·운영 ───────────────────────────────────────────────────────────

def _policy(db: Session, *, org_id: UUID) -> dict[str, Any] | None:
    from app.services import automation as auto

    return next(
        (p for p in auto.list_policies(db, org_id=org_id)
         if p["automation_type"] == AUTOMATION_TYPE and p["id"]),
        None)


def status(db: Session, *, org_id: UUID) -> dict[str, Any]:
    """운영자가 보는 클레임 수집 상태.

    **미해결 건수를 함께 준다.** 그 숫자가 0 이 아니면 그만큼 매출이 아직
    줄지 않았다는 뜻이다 — 수집이 성공했다는 것과 장부가 맞다는 것은 다르다.
    """
    from app.services.order_sync import _account_blocker, ready_accounts

    policy = _policy(db, org_id=org_id)
    configs = read_config(policy) if policy else {}

    accounts = []
    for account in ready_accounts(db, org_id=org_id):
        account_id = UUID(str(account["id"]))
        config = configs.get(account_id)
        # **마지막 시도와 마지막 성공을 따로 준다** — 주문 상태와 같은 규칙이다.
        # 하나로 합치면 지금 몇 시간째 못 읽고 있는 상태가 성공 시각 뒤에 숨는다.
        # 개인 PC 운영에서는 이 구분이 특히 중요하다: 앱이 떠 있다는 사실과
        # 데이터가 최신이라는 사실이 자주 어긋난다.
        last = db.execute(text("""
            SELECT status, started_at, error_code
              FROM external_sync_run
             WHERE org_id = :o AND external_account_id = :a AND sync_type = :t
             ORDER BY started_at DESC LIMIT 1
        """), {"o": str(org_id), "a": str(account_id),
               "t": SYNC_TYPE}).mappings().first()
        accounts.append({
            "external_account_id": str(account_id),
            "display_name": account["display_name"],
            "configured": config is not None,
            "initial_sync_from": (
                config.initial_sync_from.isoformat() if config else None),
            "claim_lookback_days": config.lookback_days if config else None,
            "blocked_by": _account_blocker(db, org_id=org_id, account=account),
            "last_attempt_at": last["started_at"].isoformat() if last else None,
            "last_status": last["status"] if last else None,
            "last_error_code": last["error_code"] if last else None,
            "last_success_at": last_success_at(
                db, org_id=org_id, external_account_id=account_id),
        })

    totals = db.execute(text("""
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE financial_effect = 'EFFECTIVE') AS effective,
               COUNT(*) FILTER (WHERE financial_effect = 'PENDING') AS pending,
               COUNT(*) FILTER (WHERE financial_effect = 'UNRESOLVED') AS unresolved,
               -- 원 주문이 장부에 없어 되돌릴 매출이 없는 것. `unresolved` 와
               -- 섞으면 "매출이 안 줄었다" 는 잘못된 신호가 된다(M054).
               COUNT(*) FILTER (WHERE financial_effect = 'NO_LEDGER_IMPACT')
                 AS no_ledger_impact,
               COUNT(*) FILTER (WHERE sales_order_id IS NULL) AS unmatched
          FROM sales_claim WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org_id)}).mappings().one()

    impact = db.execute(text("""
        SELECT COALESCE(SUM(amount_delta), 0) FROM sales_adjustment
         WHERE org_id = :o AND deleted_at IS NULL
    """), {"o": str(org_id)}).scalar_one()

    return {
        "enabled": bool(policy and policy["enabled"]),
        "mode": policy["mode"] if policy else None,
        "interval_minutes": policy["interval_minutes"] if policy else None,
        "last_run_at": policy["last_run_at"] if policy else None,
        "accounts": accounts,
        "claims": {k: int(v) for k, v in totals.items()},
        "revenue_impact": int(impact),
        # 매출은 반영했지만 반품 원가·수수료 환급은 정산서로만 알 수 있다.
        "profit_complete": False,
    }


def run_now(
    db: Session, *, org_id: UUID, actor_user_id: UUID,
    external_account_id: UUID | None = None,
) -> list[ClaimSyncOutcome]:
    """화면에서 누른 즉시 실행. **자동과 같은 서비스·같은 잠금.**"""
    policy = _policy(db, org_id=org_id)
    if policy is None:
        raise ValidationError("취소·반품 자동 수집 정책이 아직 설정되지 않았습니다.")
    configs = read_config(policy)
    if not configs:
        raise ValidationError("클레임 조회 기간과 대상 계정·시작일을 먼저 설정해 주세요.")
    if external_account_id is not None:
        configs = {k: v for k, v in configs.items() if k == external_account_id}
        if not configs:
            raise ValidationError("그 계정은 클레임 수집 대상이 아닙니다.")

    return sync_org(
        db, org_id=org_id,
        policy=dict(policy, scope_json={
            SCOPE_ACCOUNTS: [
                {SCOPE_ACCOUNT_ID: str(k),
                 SCOPE_INITIAL_FROM: v.initial_sync_from.isoformat()}
                for k, v in configs.items()
            ],
        }),
        actor_user_id=actor_user_id, trigger="MANUAL")
