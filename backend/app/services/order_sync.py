"""주문 자동 동기화 오케스트레이션 (Phase 2).

이 파일은 **적재하지 않는다**
-------------------------
적재는 Phase 1 에서 실데이터로 검증된 `next_engine_order_sync.ingest_orders`
하나뿐이다. 여기서 하는 일은 그 앞뒤다.

```
언제까지 읽을 것인가      창 계산 (cutover clamp + overlap)
누가 읽을 자격이 있나      계정 준비 상태
동시에 돌지 않게          single-flight
읽다 실패하면 어떻게       오류 분류 · PARTIAL
읽은 뒤 무엇을 남기나      external_sync_run · operation_issue
```

수동과 자동이 같은 길을 쓴다
------------------------
```
Manual  ─┐
         ├─► sync_account() ─► ingest_orders() ─► sales_order
Worker  ─┘
```

둘이 다른 업무 로직을 가지면, 사람이 손으로 확인한 동작이 자동 실행에서
재현되지 않는다. 그 차이는 새벽에 드러난다.

watermark 가 아니라 겹쳐 읽기
--------------------------
NEXT ENGINE 에는 수정일이 없다(docs/73). "지난번 성공 이후" 만 읽으면
**이미 받은 주문이 나중에 바뀌어도 영원히 모른다.** 그래서 매번 최근 구간을
통째로 다시 읽는다. 다시 읽어도 안전한 이유는 적재가 멱등이기 때문이다.

```
pay_date              조회 기준점  (watermark 아님)
last_successful_sync  창을 **넓히기만** 한다 (좁히지 않는다)
```

`last_successful_sync_at` 은 watermark 가 아니다
---------------------------------------------
예전에는 이 값을 조회 범위 계산에 아예 쓰지 않았다. 이유는 위와 같다 —
"지난번 성공 이후" 로 시작을 **올리면** 수정분을 놓친다.

그런데 쓰지 않으면 반대쪽 구멍이 생긴다. 실행이 며칠 멈춰 있었을 때
(개인 PC 운영에서는 전원을 끄면 그렇게 된다) 창이 여전히 `now - overlap`
이라서 **그 사이 결제된 주문을 영원히 읽지 않는다.**

그래서 지금은 이렇게 쓴다.

```
start = min(now, 마지막 성공) - overlap      ← 넓히기만 한다
```

`min` 이 핵심이다. 마지막 성공이 방금이면 `now - overlap` 과 같고, 오래
전이면 그만큼 뒤로 밀린다. 어떤 경우에도 창이 `now - overlap` 보다
**짧아지지 않는다** — 위의 "매번 최근 구간을 다시 읽는다" 가 그대로 산다.

이 계산은 운영체제도 스케줄러도 모른다. 얼마나 자주 도는지와 무관하게
같은 답을 내므로, 놓친 주기를 따로 보정하는 장치가 필요 없다.
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

_log = logging.getLogger("order_sync")

#: 이 자동화의 정책 유형. `automation.AUTOMATION_TYPES` 의 키다.
AUTOMATION_TYPE = "ORDER_SYNC"

#: `external_sync_run.sync_type`.
SYNC_TYPE = "ORDERS"

#: advisory lock 이름공간. 다른 기능과 겹치지 않게 예약한다.
_LOCK_NAMESPACE = 0x4F52534E  # 'ORSN'

#: 겹쳐 읽기 폭의 한도 이름. `automation_policy.guardrail_json` 에 들어간다.
#:
#: **기본값을 코드에 두지 않는다.** "보통 3일" 을 넣으면 아무도 자기 주문
#: 유입 속도에 맞게 정하지 않고, 나중에 그 숫자가 어디서 왔는지 모른다.
#: `REORDER_MONITOR` 의 `velocity_days` 와 같은 이유다.
OVERLAP_GUARDRAIL = "overlap_days"

#: 계정별 최초 조회 시작일. `automation_policy.scope_json` 에 들어간다.
#:
#: cutover 를 코드 상수로 박지 않기 위한 자리다. 벤더 이름이 들어가지 않는
#: 일반 개념이다 — "이 계정은 언제부터 우리 장부의 것인가".
SCOPE_ACCOUNTS = "accounts"
SCOPE_ACCOUNT_ID = "external_account_id"
SCOPE_INITIAL_FROM = "initial_sync_from"


# ── 결과 ────────────────────────────────────────────────────────────────

#: 실행 상태. `external_sync_run.ck_esr_status` 와 같은 어휘를 쓴다.
SUCCESS = "SUCCESS"
PARTIAL = "PARTIAL"
FAILED = "FAILED"
UNSUPPORTED = "UNSUPPORTED"
#: 실행 자체를 하지 않은 경우. `external_sync_run` 에는 남기지 않는다 —
#: 돌지 않은 것을 실행 기록으로 남기면 "몇 번 돌았나" 가 부풀어 오른다.
SKIPPED = "SKIPPED"

#: 왜 건너뛰었나 / 왜 실패했나. 예외 센터와 화면이 이 토큰을 그대로 쓴다.
NOT_CONFIGURED = "NOT_CONFIGURED"
ALREADY_RUNNING = "ALREADY_RUNNING"
DISABLED = "DISABLED"
CONNECTOR_UNAVAILABLE = "CONNECTOR_UNAVAILABLE"
CREDENTIAL_NOT_CONFIGURED = "CREDENTIAL_NOT_CONFIGURED"
AUTH_FAILED = "AUTH_FAILED"
RATE_LIMITED = "RATE_LIMITED"
TIMEOUT = "TIMEOUT"
SERVER_ERROR = "SERVER_ERROR"
SCHEMA_CHANGED = "SCHEMA_CHANGED"
PAGINATION_STALLED = "PAGINATION_STALLED"
RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
READ_FAILED = "READ_FAILED"


@dataclass
class AccountSyncOutcome:
    """계정 하나의 동기화 결과. **개인정보 없음.**"""

    external_account_id: UUID
    status: str
    reason: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    sync_run_id: UUID | None = None

    fetched: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    conflict: int = 0
    rejected: int = 0
    #: 관문에서 걸러진 것 — 과거·미매핑·마감월·다른 경로.
    protected: int = 0
    #: 이번 실행이 되살린 보류 주문 수.
    replayed: int = 0
    #: 겹쳐 읽기 창 중 실패한 개수. 0 이 아니면 SUCCESS 가 아니다.
    failed_windows: int = 0
    #: 벤더 한도로 나눈 창의 총 개수. 오래 멈췄다가 돌면 이 값이 커진다 —
    #: "몇 일치를 따라잡았나" 를 사람이 읽을 수 있는 유일한 숫자다.
    windows: int = 0
    funnel: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == SUCCESS

    def as_dict(self) -> dict[str, Any]:
        return {
            "external_account_id": str(self.external_account_id),
            "status": self.status, "reason": self.reason,
            "window_start": self.window_start.isoformat() if self.window_start else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "sync_run_id": str(self.sync_run_id) if self.sync_run_id else None,
            "fetched": self.fetched, "created": self.created,
            "updated": self.updated, "unchanged": self.unchanged,
            "conflict": self.conflict, "rejected": self.rejected,
            "protected": self.protected, "replayed": self.replayed,
            "failed_windows": self.failed_windows, "windows": self.windows,
            "funnel": self.funnel,
        }


class OrderSyncAlreadyRunningError(Exception):
    """같은 계정에 대해 이미 동기화가 돌고 있다."""


# ── single-flight ───────────────────────────────────────────────────────

def _lock_key(external_account_id: UUID) -> int:
    """계정 UUID 를 advisory lock 의 int4 키로 접는다.

    잠금 범위는 **계정 단위**다. 조직 단위로 잡으면 계정이 둘일 때 하나가
    느린 동안 다른 하나가 통째로 밀린다 — 한 계정의 장애가 다른 계정의
    동기화를 멈추게 하면 안 된다(다중 계정 격리).
    """
    folded = external_account_id.int % (2**31 - 1)
    return folded or 1


def _new_connection(db: Session) -> Any:
    """잠금 전용 **새 연결**.

    운영에서는 Session 이 Engine 에 묶여 있어 `get_bind().connect()` 면 된다.
    테스트 하네스는 Session 을 이미 열린 Connection 에 묶는다(savepoint 롤백을
    쓰려고). 그 Connection 에는 `connect()` 가 없다.

    둘 다 지원하는 이유는 편의가 아니다 — 여기서 갈라지면 잠금을 **실제로**
    거는지 확인할 방법이 테스트에 없어지고, 동시성 보장은 코드에 그렇게 적혀
    있다는 말만 남는다.
    """
    bind = db.get_bind()
    engine = getattr(bind, "engine", None)
    return (engine or bind).connect()


@contextmanager
def single_flight(db: Session, external_account_id: UUID) -> Iterator[None]:
    """계정 하나에 대해 동시에 하나만 실행한다.

    **전용 connection 에서 잡는다.** advisory lock 은 연결 단위인데 이 서비스는
    도중에 커밋하고, Session 은 커밋 후 커넥션을 풀에 반납할 수 있다. 그러면
    잠금이 실행 도중 풀리거나 반납된 커넥션에 남는다. 승인 배치가 같은 이유로
    같은 방식을 쓴다(`approval_executor`).

    수동과 자동이 **같은 이 문을 지난다.** 자동만 잠그면 사람이 화면에서 누른
    순간 두 실행이 겹친다.
    """
    conn = _new_connection(db)
    key = _lock_key(external_account_id)
    try:
        acquired = bool(conn.execute(
            text("SELECT pg_try_advisory_lock(:ns, :k)"),
            {"ns": _LOCK_NAMESPACE, "k": key},
        ).scalar())
        if not acquired:
            raise OrderSyncAlreadyRunningError(str(external_account_id))
        yield
    finally:
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:ns, :k)"),
                         {"ns": _LOCK_NAMESPACE, "k": key})
        except Exception:  # noqa: BLE001 — close() 가 최종 회수 경로다
            pass
        conn.close()


# ── 설정 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class AccountConfig:
    """계정 하나의 동기화 설정. 전부 정책에서 온다 — 코드 상수가 아니다."""

    external_account_id: UUID
    initial_sync_from: date
    overlap_days: int


def read_config(policy: dict[str, Any]) -> dict[UUID, AccountConfig]:
    """정책에서 계정별 설정을 읽는다.

    빠진 값을 기본값으로 채우지 않는다
    -----------------------------
    `overlap_days` 가 없으면 이 자동화는 돌지 않는다. 임의의 폭으로 돌면
    사용자는 자기가 정하지 않은 범위로 외부 API 를 두들기게 되고, 그 숫자가
    어디서 왔는지 아무도 모른다.

    `initial_sync_from` 이 없으면 그 계정은 돌지 않는다. 시작일을 모르는 채
    조회하면 cutover 이전 과거까지 끌어와 엑셀 장부와 겹친다.
    """
    guardrails = policy.get("guardrail_json") or {}
    raw_overlap = guardrails.get(OVERLAP_GUARDRAIL)
    if raw_overlap is None:
        return {}
    overlap = int(float(raw_overlap))
    if overlap < 1:
        raise ValidationError("겹쳐 읽기 일수는 1일 이상이어야 합니다.")

    scope = policy.get("scope_json") or {}
    entries = scope.get(SCOPE_ACCOUNTS) or []
    if not isinstance(entries, list):
        raise ValidationError("주문 동기화 대상 계정 설정이 목록이 아닙니다.")

    out: dict[UUID, AccountConfig] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValidationError("주문 동기화 대상 계정 설정이 올바르지 않습니다.")
        raw_id = entry.get(SCOPE_ACCOUNT_ID)
        raw_from = entry.get(SCOPE_INITIAL_FROM)
        if not raw_id or not raw_from:
            # 반쯤 채워진 설정을 조용히 무시하지 않는다 — 사용자는 켰다고 믿는다.
            raise ValidationError(
                "주문 동기화 계정 설정에는 계정과 시작일이 모두 필요합니다.")
        try:
            account_id = UUID(str(raw_id))
            initial_from = date.fromisoformat(str(raw_from))
        except ValueError as exc:
            raise ValidationError("주문 동기화 계정 설정 형식이 올바르지 않습니다.") from exc
        out[account_id] = AccountConfig(
            external_account_id=account_id,
            initial_sync_from=initial_from,
            overlap_days=overlap,
        )
    return out


def align_tz(value: datetime, reference: datetime) -> datetime:
    """`value` 의 tz 인식 여부를 `reference` 에 맞춘다.

    창 계산에는 세 군데서 온 시각이 섞인다 — 호출자가 준 `now`(테스트는
    naive 를 준다), 정책의 날짜(naive), DB 의 `timestamptz`(aware). 그냥
    비교하면 `TypeError` 로 죽거나, 더 나쁘게는 한쪽만 naive 인 채로
    비교되어 9시간 어긋난 창이 조용히 만들어진다.
    """
    if reference.tzinfo is None and value.tzinfo is not None:
        return value.astimezone().replace(tzinfo=None)
    if reference.tzinfo is not None and value.tzinfo is None:
        return value.replace(tzinfo=reference.tzinfo)
    return value


def compute_window(
    *, now: datetime, config: AccountConfig, max_window_days: int,
    last_success_at: datetime | None = None,
) -> tuple[datetime, datetime]:
    """조회 창. **cutover 이전으로는 내려가지 않는다.**

    ```
    anchor       = min(now, 마지막 성공)          ← 없으면 now
    window_start = max(초기 시작일, anchor - 겹쳐읽기)
    window_end   = now
    ```

    앞을 자르는 것이 핵심이다. 자르지 않으면 자동 실행이 매번 과거를 다시
    읽고, 엑셀로 들어온 초기 장부와 같은 주문을 두고 경쟁한다. 과거 보호는
    적재 쪽에도 있지만(`cutover_after`), 애초에 읽지 않는 편이 싸고 확실하다.

    `anchor` 가 하는 일은 하나뿐이다 — **못 돈 기간을 창에 포함시킨다.**
    `min` 때문에 창이 짧아지는 방향으로는 절대 움직이지 않는다(모듈 docstring).
    마지막 성공이 미래로 적혀 있는 이상한 데이터가 있어도 `min` 이 막는다.

    창을 며칠로 자를지는 여기서 정하지 않는다. 커넥터가 자기 한도(NEXT ENGINE
    은 10일)로 나눈다 — 그래서 한 달을 못 돌았어도 요청이 한도를 넘지 않는다.
    """
    anchor = now
    if last_success_at is not None:
        anchor = min(align_tz(last_success_at, now), now)
    overlap_start = anchor - timedelta(days=config.overlap_days)
    cutover_start = datetime.combine(config.initial_sync_from, datetime.min.time())
    cutover_start = align_tz(cutover_start, overlap_start)
    start = max(overlap_start, cutover_start)
    if start > now:
        # 시작일이 미래다. 읽을 것이 없는 게 맞고, 빈 창으로 벤더를 부르지 않는다.
        start = now
    _ = max_window_days  # 창 분할은 커넥터가 자기 한도로 한다.
    return start, now


def last_success_at(
    db: Session, *, org_id: UUID, external_account_id: UUID,
) -> datetime | None:
    """이 계정에서 주문을 마지막으로 **성공적으로** 읽은 시각.

    `_finalize` 가 성공했을 때만 옮기는 값을 그대로 읽는다. 부분 실행은
    이 값을 옮기지 않으므로, 못 읽은 구간은 다음 실행의 창에 다시 들어온다.
    """
    return db.execute(text("""
        SELECT last_successful_sync_at FROM external_account
         WHERE id = :i AND org_id = :o AND deleted_at IS NULL
    """), {"i": str(external_account_id), "o": str(org_id)}).scalar()


# ── 오류 분류 ────────────────────────────────────────────────────────────

def classify_error(exc: BaseException) -> str:
    """실패를 운영자가 행동할 수 있는 범주로 나눈다.

    전부 "동기화 실패" 로 뭉치면 화면을 봐도 무엇을 해야 할지 모른다.
    자격증명을 고쳐야 하는 것과 잠시 기다리면 되는 것은 다른 일이다.

    **예외 메시지를 범주 판정에 쓰지 않는다.** 벤더 문구가 바뀌면 조용히
    오분류되고, 그 문구에는 조회 조건이 섞여 있다.
    """
    import httpx

    from app.connectors.next_engine.client import (
        NextEnginePaginationError,
        NextEngineReadError,
        NextEngineSchemaError,
        NextEngineWindowError,
    )

    if isinstance(exc, NextEnginePaginationError):
        return PAGINATION_STALLED
    if isinstance(exc, (NextEngineSchemaError, NextEngineWindowError)):
        return SCHEMA_CHANGED
    if isinstance(exc, NextEngineReadError):
        detail = getattr(exc, "detail", None) or {}
        status = detail.get("http_status")
        if status in (401, 403):
            return AUTH_FAILED
        if status == 429:
            return RATE_LIMITED
        if isinstance(status, int) and 500 <= status < 600:
            return SERVER_ERROR
        if isinstance(status, int) and 400 <= status < 500:
            # 우리 요청이 거부됐다. 같은 요청을 반복해도 결과가 같다 —
            # 스키마나 파라미터가 바뀐 것으로 보고 사람이 보게 한다.
            return SCHEMA_CHANGED
        cause = detail.get("cause")
        if cause in ("ReadTimeout", "ConnectTimeout", "PoolTimeout", "TimeoutException"):
            return TIMEOUT
        return READ_FAILED
    if isinstance(exc, httpx.TimeoutException):
        return TIMEOUT
    if isinstance(exc, httpx.HTTPError):
        return READ_FAILED
    if isinstance(exc, (KeyError, TypeError, ValueError)):
        # 정규화가 기대한 모양이 아니다. 잘못된 주문을 만들어내지 않는다.
        return SCHEMA_CHANGED
    return READ_FAILED


#: 재시도해도 같은 결과인 범주. 다음 주기에 다시 시도하는 것이 유일한 대응이다.
_NO_IMMEDIATE_RETRY = frozenset({
    AUTH_FAILED, RATE_LIMITED, SCHEMA_CHANGED, PAGINATION_STALLED,
})


# ── 실행 기록 ────────────────────────────────────────────────────────────

def _start_run(
    db: Session, *, org_id: UUID, account_id: UUID, trigger: str,
    actor_user_id: UUID | None, worker_run_id: UUID | None,
) -> UUID:
    """`external_sync_run` 시작 행.

    `created_by` 는 **사람일 때만** 채운다. 이 컬럼은 `app_user` 를 가리키고,
    자동화의 주체는 사람이 아니라 서비스 계정이다 — 그대로 넣으면 외래키가
    깨진다.

    trigger 로만 판단하지 않는다. 수동 실행도 CLI 에서는 서비스 계정으로
    돈다(사람이 로그인한 게 아니다). **실제로 `app_user` 에 있는지**를 보고
    정한다 — 그래야 새 진입점이 생겨도 여기서 터지지 않는다.

    자동 실행의 주체는 `trigger` 와 `automation_run` 이 가리킨다.

    `started_at` 을 `clock_timestamp()` 로 박는다. 기본값 `now()` 는 **트랜잭션
    시작 시각**이라 한 트랜잭션 안에서 두 번 실행하면 두 행의 시각이 같아진다.
    그러면 "가장 최근 실행" 을 뽑는 화면·예외 센터가 어느 쪽을 고를지 정할 수
    없고, 어제 실패가 오늘 성공을 덮어 계속 떠 있게 된다.
    """
    if actor_user_id is not None:
        is_person = db.execute(text("""
            SELECT 1 FROM app_user WHERE id = :i AND org_id = :o
        """), {"i": str(actor_user_id), "o": str(org_id)}).first() is not None
        if not is_person:
            actor_user_id = None

    row = db.execute(text("""
        INSERT INTO external_sync_run
               (org_id, external_account_id, sync_type, trigger, status,
                started_at, created_by, worker_run_id)
        VALUES (:org, :a, :t, :tr, 'RUNNING', clock_timestamp(), :actor, :wr)
        RETURNING id
    """), {
        "org": str(org_id), "a": str(account_id), "t": SYNC_TYPE, "tr": trigger,
        "actor": str(actor_user_id) if actor_user_id else None,
        "wr": str(worker_run_id) if worker_run_id else None,
    }).mappings().one()
    db.commit()
    return UUID(str(row["id"]))


def _finish_run(
    db: Session, *, run_id: UUID, outcome: AccountSyncOutcome,
) -> None:
    """실행 기록 마감. **개인정보·비밀을 남기지 않는다.**

    `error_message` 에 벤더 응답을 넣지 않는다 — 주문 응답에는 수취인과
    주소가 들어 있고, 실패 로그는 권한이 넓은 화면에서 읽힌다.
    """
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
        "read": outcome.fetched, "created": outcome.created,
        "updated": outcome.updated,
        # 넘어간 것 = 관문 보호 + 변화 없음. 둘 다 "쓰지 않았다" 다.
        "skipped": outcome.protected + outcome.unchanged,
        "conflicts": outcome.conflict, "errors": outcome.rejected,
        "ec": outcome.reason,
        "em": _reason_message(outcome.reason),
    })
    db.commit()


_REASON_MESSAGE = {
    AUTH_FAILED: "NEXT ENGINE 이 자격증명을 거부했습니다.",
    RATE_LIMITED: "NEXT ENGINE 요청 한도에 걸렸습니다.",
    TIMEOUT: "NEXT ENGINE 응답이 시간 안에 오지 않았습니다.",
    SERVER_ERROR: "NEXT ENGINE 서버가 오류를 반환했습니다.",
    SCHEMA_CHANGED: "NEXT ENGINE 응답 형식이 예상과 다릅니다.",
    PAGINATION_STALLED: "NEXT ENGINE 주문 페이지가 넘어가지 않습니다.",
    RECONCILIATION_FAILED: "건수 대사가 맞지 않아 성공으로 기록하지 않았습니다.",
    READ_FAILED: "NEXT ENGINE 주문 조회에 실패했습니다.",
    NOT_CONFIGURED: "주문 동기화 설정이 완료되지 않았습니다.",
    CREDENTIAL_NOT_CONFIGURED: "자격증명이 설정되지 않았습니다.",
    CONNECTOR_UNAVAILABLE: "이 외부 시스템의 연동이 아직 없습니다.",
}


def _reason_message(reason: str | None) -> str | None:
    return _REASON_MESSAGE.get(reason) if reason else None


#: `external_sync_run` 이 이 시간을 넘겨 RUNNING 이면 프로세스가 죽은 것으로
#: 본다. `worker_runtime.STALE_RUN_MINUTES` 와 같은 값을 쓴다 — 두 곳에서 다른
#: 숫자를 쓰면 같은 사고에 두 화면이 다른 말을 한다.
STALE_RUN_MINUTES = 60

#: 중단된 실행에 남기는 오류 코드.
STALE_RUN = "STALE_RUN"


def reclaim_stale_sync_runs(
    db: Session, *, minutes: int = STALE_RUN_MINUTES,
) -> int:
    """죽은 프로세스가 남긴 `external_sync_run` RUNNING 을 정리한다.

    왜 필요한가
    ----------
    개인 PC 운영에서는 동기화 도중 전원이 꺼진다. advisory lock 은 연결이
    끊기면 풀리므로 **다음 실행이 막히지는 않는다.** 문제는 기록이다.

    ```
    RUNNING 인 채로 남은 행  → 화면은 "지금 조회 중" 이라고 말한다
                             → 마지막 시도가 영원히 그 행이다
                             → 다음 성공이 와도 실패 목록에 계속 보인다
    ```

    `worker_runtime.reclaim_stale_runs` 가 `worker_run` 에 대해 하는 일을
    외부 동기화 기록에 대해 그대로 한다. 그쪽 함수를 재사용하지 않는 이유는
    테이블도 컬럼도 다르기 때문이고, 판단 기준(시간·문구)은 맞춰 두었다.

    **읽은 건수를 추측하지 않는다.** 중단된 실행이 몇 건을 읽었는지는 알
    수 없다. 카운트를 0 으로 덮으면 "0건 읽고 성공" 처럼 보이므로 건드리지
    않고, 상태만 FAILED 로 바꾼다.
    """
    rows = db.execute(text("""
        UPDATE external_sync_run
           SET status = 'FAILED', finished_at = now(),
               error_code = :code,
               error_message = '실행 기록이 완료되지 않았습니다(프로세스 중단 추정).'
         WHERE status = 'RUNNING'
           AND started_at < now() - make_interval(mins => :m)
        RETURNING id
    """), {"m": minutes, "code": STALE_RUN}).scalars().all()
    db.commit()
    return len(rows)


# ── 계정 준비 상태 ───────────────────────────────────────────────────────

def ready_accounts(db: Session, *, org_id: UUID) -> list[dict[str, Any]]:
    """이 조직에서 주문을 읽을 수 있는 외부 계정.

    **질의 자체를 조직으로 좁힌다.** 전부 읽고 파이썬에서 거르면, 나중에 그
    필터 한 줄이 빠지는 날 다른 회사 계정을 건드린다. 자동 실행에는 요청
    맥락이 없어서 그런 실수를 잡아 줄 것이 없다.
    """
    return [dict(r) for r in db.execute(text("""
        SELECT a.id, a.display_name, a.status,
               s.code AS system_code,
               a.credential_secret_ref IS NOT NULL AS has_credential
          FROM external_account a
          JOIN external_system s ON s.id = a.external_system_id
         WHERE a.org_id = :o AND a.deleted_at IS NULL
           AND a.status <> 'DISABLED'
         ORDER BY a.created_at
    """), {"o": str(org_id)}).mappings().all()]


def _account_blocker(db: Session, *, org_id: UUID, account: dict[str, Any]) -> str | None:
    """이 계정을 지금 동기화할 수 없는 이유. 없으면 None."""
    from app.services import connector as conn

    if not conn.has_connector(str(account["system_code"])):
        return CONNECTOR_UNAVAILABLE
    if "ORDER_IMPORT" not in conn.get_connector(str(account["system_code"])).capabilities():
        return CONNECTOR_UNAVAILABLE
    if not account["has_credential"]:
        return CREDENTIAL_NOT_CONFIGURED
    # 판매처 계정 매핑이 하나도 없으면 읽어 와도 전부 관문에서 막힌다.
    # "0건 성공" 으로 보이는 것이 가장 나쁘다 — 확인해 봤다는 뜻으로 읽힌다.
    mapped = db.execute(text("""
        SELECT count(*) FROM external_entity_mapping
         WHERE org_id = :o AND external_account_id = :a
           AND external_entity_type = 'MARKETPLACE_ACCOUNT'
           AND mapping_status = 'ACTIVE' AND deleted_at IS NULL
    """), {"o": str(org_id), "a": str(account["id"])}).scalar()
    if not mapped:
        return NOT_CONFIGURED
    return None


# ── 동기화 ──────────────────────────────────────────────────────────────

def sync_account(
    db: Session, *, org_id: UUID, external_account_id: UUID,
    config: AccountConfig, actor_user_id: UUID,
    trigger: str = "SCHEDULED", now: datetime | None = None,
    client: Any = None, worker_run_id: UUID | None = None,
) -> AccountSyncOutcome:
    """계정 하나를 동기화한다. **수동·자동이 함께 쓰는 유일한 입구.**

    창을 벤더 한도로 잘라 순서대로 읽고, 읽은 것을 Phase 1 의 적재 서비스에
    넘긴다. 창 하나가 실패해도 그 앞에서 읽은 것은 버리지 않는다 — 적재가
    멱등이라 다시 읽어도 안전하고, 버리면 그만큼 반영이 늦어진다.

    다만 그 실행은 **성공이 아니다.** 일부만 읽고 성공으로 기록하면
    `last_successful_sync_at` 이 앞으로 가고, 못 읽은 구간은 영원히 안 읽힌다.
    """
    from app.connectors.next_engine.client import split_windows
    from app.services import next_engine_order_sync as nes
    from app.services.credential_resolver import resolve_for_account

    now = now or datetime.now()
    outcome = AccountSyncOutcome(
        external_account_id=external_account_id, status=FAILED)

    with single_flight(db, external_account_id):
        # 못 돈 기간을 창에 포함시킨다. 잠금을 잡은 **뒤에** 읽어야 앞선
        # 실행이 방금 옮긴 값을 본다 — 먼저 읽으면 두 실행이 같은 구간을
        # 중복 조회한다(적재는 멱등이라 데이터는 안전하지만 호출이 낭비된다).
        start, end = compute_window(
            now=now, config=config,
            max_window_days=_max_window_days(client),
            last_success_at=last_success_at(
                db, org_id=org_id, external_account_id=external_account_id))
        outcome.window_start, outcome.window_end = start, end
        run_id = _start_run(
            db, org_id=org_id, account_id=external_account_id, trigger=trigger,
            actor_user_id=actor_user_id, worker_run_id=worker_run_id)
        outcome.sync_run_id = run_id

        try:
            api_key = resolve_for_account(
                db, org_id=org_id, account_id=external_account_id,
            ).as_dict()["api_key"]
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            outcome.status = FAILED
            outcome.reason = CREDENTIAL_NOT_CONFIGURED
            _log.warning("주문 동기화 자격증명 실패: %s", type(exc).__name__)
            _finish_run(db, run_id=run_id, outcome=outcome)
            return outcome

        api = client or _default_client()
        lines: list[Any] = []
        windows = split_windows(start, end)
        outcome.windows = len(windows)
        if len(windows) > 1:
            # 개인정보 없음 — 창 개수와 범위만 남긴다. 오래 꺼져 있었다는
            # 사실이 로그에 남아야 "왜 이번 실행만 오래 걸렸나" 를 답할 수 있다.
            _log.info("주문 따라잡기 창 %d개 (%s ~ %s)",
                      len(windows), start.date(), end.date())
        for w_start, w_end in windows:
            try:
                lines += api.fetch_orders(api_key=api_key, start=w_start, end=w_end)
            except Exception as exc:  # noqa: BLE001 — 범주로 나눠 기록한다
                outcome.failed_windows += 1
                outcome.reason = classify_error(exc)
                _log.warning("주문 조회 실패(%s)", outcome.reason)
                # 재시도해도 결과가 같은 범주는 남은 창도 시도하지 않는다.
                # 자격증명이 거부됐는데 창마다 다시 부르면 잠금 정책에 걸린다.
                if outcome.reason in _NO_IMMEDIATE_RETRY:
                    break

        # 같은 주문이 상태별·창별로 두 번 올 수 있다. 적재 전에 접는다.
        lines = list({ln.identity(): ln for ln in lines}.values())

        outcome = _ingest_and_reconcile(
            db, org_id=org_id, actor_user_id=actor_user_id,
            external_account_id=external_account_id, lines=lines,
            config=config, outcome=outcome, nes=nes)

        outcome.replayed = _replay_recovered_mappings(
            db, org_id=org_id, actor_user_id=actor_user_id,
            external_account_id=external_account_id, config=config,
            api_key=api_key, api=api, now=now, nes=nes,
            already_read=(start, end), outcome=outcome)

        _finalize(db, org_id=org_id, external_account_id=external_account_id,
                  outcome=outcome, now=now)
        _finish_run(db, run_id=run_id, outcome=outcome)

    return outcome


def _max_window_days(client: Any) -> int:
    from app.connectors.next_engine.client import NEXT_ENGINE_MAX_WINDOW_DAYS

    return int(getattr(client, "max_window_days", NEXT_ENGINE_MAX_WINDOW_DAYS))


def _default_client() -> Any:
    from app.connectors.next_engine import NextEngineOrderClient

    return NextEngineOrderClient()


def _ingest_and_reconcile(
    db: Session, *, org_id: UUID, actor_user_id: UUID, external_account_id: UUID,
    lines: list[Any], config: AccountConfig, outcome: AccountSyncOutcome, nes: Any,
) -> AccountSyncOutcome:
    """읽은 것을 적재하고 건수를 맞춰 본다."""
    try:
        result = nes.ingest_orders(
            db, org_id=org_id, actor_user_id=actor_user_id,
            external_account_id=external_account_id, lines=lines,
            # cutover 는 창 계산에서 이미 잘렸다. 적재에서 한 번 더 막는 것은
            # 중복이 아니라 이중 안전장치다 — 창 계산이 틀려도 과거는 안 바뀐다.
            cutover_after=config.initial_sync_from)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        outcome.status = FAILED
        outcome.reason = outcome.reason or classify_error(exc)
        _log.exception("주문 적재 실패")
        return outcome

    outcome.fetched = result.fetched
    outcome.created = result.created
    outcome.updated = result.updated
    outcome.unchanged = result.unchanged
    outcome.conflict = result.conflict
    outcome.rejected = result.rejected
    # 관문에서 멈춘 것 = 읽었지만 적재 대상이 아니었던 것.
    # `gate_total` 에는 `eligible` 이 포함돼 있으므로 빼야 한다 — 빼지 않으면
    # 정상 적재된 주문까지 "보호됨" 으로 세어 화면 숫자가 부풀어 오른다.
    outcome.protected = result.gate_total - result.eligible
    outcome.funnel = result.as_dict().get("funnel", {})

    if not result.reconciles:
        # 대사가 안 맞으면 어떤 라인이 조용히 사라진 것이다. 그 상태를 성공으로
        # 기록하면 화면과 장부가 갈라진다.
        outcome.status = FAILED
        outcome.reason = RECONCILIATION_FAILED
        return outcome

    if outcome.failed_windows:
        outcome.status = PARTIAL
        return outcome

    outcome.status = SUCCESS
    outcome.reason = None
    return outcome


def _finalize(
    db: Session, *, org_id: UUID, external_account_id: UUID,
    outcome: AccountSyncOutcome, now: datetime,
) -> None:
    """성공했을 때만 마지막 성공 시각을 옮긴다.

    실패·부분 실행을 마지막 성공으로 적으면, 화면은 방금 확인한 것처럼 보이고
    실제로는 몇 시간째 못 읽고 있는 상태가 숨는다. 그게 이 한 줄의 전부다.
    """
    if outcome.status != SUCCESS:
        return
    db.execute(text("""
        UPDATE external_account
           SET last_successful_sync_at = :t, last_connected_at = :t,
               status = CASE WHEN status = 'ERROR' THEN 'CONNECTED' ELSE status END
         WHERE id = :i AND org_id = :o
    """), {"t": now, "i": str(external_account_id), "o": str(org_id)})
    db.commit()


# ── 보류 주문 되살리기 (no-loss) ─────────────────────────────────────────

def pending_replays(
    db: Session, *, org_id: UUID, external_account_id: UUID,
) -> list[dict[str, Any]]:
    """매핑이 생겨서 이제 되살릴 수 있게 된 보류 건.

    왜 필요한가
    ---------
    자동 동기화는 최근 며칠만 겹쳐 읽는다. 8/1 주문이 미매핑으로 보류됐는데
    사용자가 8/20 에 상품을 연결하면, 그 주문은 **이미 일반 창 밖**이다.
    아무도 다시 읽어 주지 않으면 그 매출은 영원히 장부에 없다.

    사람이 무시(IGNORED)한 것도 대상이다
    --------------------------------
    "이 경고를 그만 보겠다" 와 "그 매출을 버리겠다" 는 다른 결정이다. 경고를
    다시 띄우지는 않지만(§61), 매핑이 생겼으면 데이터는 되살린다.
    """
    from app.services.next_engine_order_sync import (
        ENTITY_PRODUCT,
        ISSUE_UNMAPPED_PRODUCT,
    )

    prefix = f"NE_UNMAPPED_PRODUCT:{external_account_id}:"
    return [dict(r) for r in db.execute(text("""
        SELECT i.id AS issue_id, i.status,
               m.external_entity_id AS external_product_id,
               COALESCE(i.year_month, to_char(i.first_detected_at, 'YYYY-MM'))
                 AS from_month
          FROM operation_issue i
          JOIN external_entity_mapping m
            ON m.external_account_id = :acc
           AND m.external_entity_type = :etype
           AND m.mapping_status = 'ACTIVE'
           AND m.deleted_at IS NULL
           AND i.dedupe_key = :prefix || m.external_entity_id
         WHERE i.org_id = :org AND i.deleted_at IS NULL
           AND i.issue_type = :itype
           AND i.status <> 'RESOLVED'
         ORDER BY from_month
    """), {"org": str(org_id), "acc": str(external_account_id),
           "etype": ENTITY_PRODUCT, "itype": ISSUE_UNMAPPED_PRODUCT,
           "prefix": prefix}).mappings().all()]


def _replay_recovered_mappings(
    db: Session, *, org_id: UUID, actor_user_id: UUID, external_account_id: UUID,
    config: AccountConfig, api_key: str, api: Any, now: datetime, nes: Any,
    already_read: tuple[datetime, datetime], outcome: AccountSyncOutcome,
) -> int:
    """보류됐던 구간을 겨냥해 다시 읽는다.

    매핑 저장만으로 이슈를 닫지 않는 이유
    -------------------------------
    매핑이 생겼다는 것과 빠졌던 주문이 장부에 들어왔다는 것은 다른 사실이다.
    저장 시점에 닫으면 목록은 깨끗해지고 매출은 비어 있다 — 아무도 그걸
    발견하지 못한다. **실제로 다시 들어온 뒤에** 닫는다.

    실패하면 이슈를 열어 둔다. 매핑은 되돌리지 않는다 — 매핑이 틀려서 실패한
    게 아니다. 다음 실행이 다시 시도한다.
    """
    from app.connectors.next_engine.client import split_windows

    pending = pending_replays(
        db, org_id=org_id, external_account_id=external_account_id)
    if not pending:
        return 0

    normal_start = already_read[0]
    recovered = 0

    for row in pending:
        month_start = _month_start(str(row["from_month"]), tzinfo=now.tzinfo)
        cutover_start = datetime.combine(
            config.initial_sync_from, datetime.min.time())
        if now.tzinfo is not None:
            cutover_start = cutover_start.replace(tzinfo=now.tzinfo)
        start = max(month_start, cutover_start)
        # 방금 읽은 구간은 다시 부르지 않는다. 멱등이라 틀리지는 않지만,
        # 같은 창을 두 번 읽는 것은 벤더 한도를 그냥 태우는 일이다.
        end = min(normal_start, now)
        if start >= end:
            # 일반 창이 이미 그 구간을 덮었다. 적재는 이번 실행에서 끝났다.
            recovered += _close_replay_issue(db, issue_id=row["issue_id"])
            continue

        lines: list[Any] = []
        failed = False
        for w_start, w_end in split_windows(start, end):
            try:
                lines += api.fetch_orders(
                    api_key=api_key, start=w_start, end=w_end)
            except Exception as exc:  # noqa: BLE001
                failed = True
                outcome.failed_windows += 1
                outcome.reason = outcome.reason or classify_error(exc)
                _log.warning("보류 주문 재조회 실패(%s)", outcome.reason)
                break
        if failed:
            # 이슈를 닫지 않는다. 매핑은 그대로 둔다.
            continue

        lines = list({ln.identity(): ln for ln in lines}.values())
        try:
            result = nes.ingest_orders(
                db, org_id=org_id, actor_user_id=actor_user_id,
                external_account_id=external_account_id, lines=lines,
                cutover_after=config.initial_sync_from)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            outcome.reason = outcome.reason or READ_FAILED
            _log.exception("보류 주문 재적재 실패")
            continue

        outcome.created += result.created
        outcome.updated += result.updated
        outcome.unchanged += result.unchanged
        # 되살리기가 또 미매핑을 만나면 그건 다른 상품이다. 이 이슈는 닫는다 —
        # 이 상품 때문에 막혀 있던 것은 풀렸다.
        recovered += _close_replay_issue(db, issue_id=row["issue_id"])

    if recovered:
        db.commit()
    return recovered


def _close_replay_issue(db: Session, *, issue_id: Any) -> int:
    """되살리기가 끝난 이슈를 닫는다.

    사람이 IGNORED 한 것도 여기서 RESOLVED 가 된다. 경고를 **다시 띄우는**
    것이 아니라 이미 닫힌 것을 정확한 사유로 바꾸는 것이다 — 무시한 이유는
    "볼 필요 없다" 였고, 지금은 실제로 해결됐다.
    """
    updated = db.execute(text("""
        UPDATE operation_issue
           SET status = 'RESOLVED', resolved_at = now(),
               resolution_note = '상품 매핑 후 보류 주문 재적재 완료'
         WHERE id = :i AND status <> 'RESOLVED'
        RETURNING id
    """), {"i": str(issue_id)}).scalars().all()
    return len(updated)


def _month_start(year_month: str, *, tzinfo: Any = None) -> datetime:
    parts = year_month.split("-")
    out = datetime(int(parts[0]), int(parts[1]), 1)
    return out.replace(tzinfo=tzinfo) if tzinfo is not None else out


# ── 조직 단위 실행 ───────────────────────────────────────────────────────

def sync_org(
    db: Session, *, org_id: UUID, policy: dict[str, Any], actor_user_id: UUID,
    trigger: str = "SCHEDULED", now: datetime | None = None,
    client: Any = None, worker_run_id: UUID | None = None,
) -> list[AccountSyncOutcome]:
    """정책에 설정된 계정을 **순서대로** 동기화한다.

    왜 병렬로 부르지 않나
    ------------------
    NEXT ENGINE 의 요청 한도가 문서로 확인되지 않았다. 근거 없이 동시에
    두들기면 한도에 걸리는 것이 우리 잘못이 되고, 그때 막히는 것은 주문
    수집 전체다. 계정 수가 늘어 실제로 느려지면 그때 근거를 갖고 바꾼다.

    한 계정의 실패가 다음 계정을 막지 않는다.
    """
    configs = read_config(policy)
    if not configs:
        return []

    accounts = {UUID(str(a["id"])): a for a in ready_accounts(db, org_id=org_id)}
    outcomes: list[AccountSyncOutcome] = []

    for account_id, config in configs.items():
        account = accounts.get(account_id)
        if account is None:
            # 설정에는 있는데 쓸 수 없는 계정(삭제·중지·타 조직)이다.
            outcomes.append(AccountSyncOutcome(
                external_account_id=account_id, status=SKIPPED,
                reason=NOT_CONFIGURED))
            continue

        blocker = _account_blocker(db, org_id=org_id, account=account)
        if blocker is not None:
            outcomes.append(AccountSyncOutcome(
                external_account_id=account_id, status=SKIPPED, reason=blocker))
            continue

        try:
            outcomes.append(sync_account(
                db, org_id=org_id, external_account_id=account_id, config=config,
                actor_user_id=actor_user_id, trigger=trigger, now=now,
                client=client, worker_run_id=worker_run_id))
        except OrderSyncAlreadyRunningError:
            # 성공인 척하지 않는다. 다음 주기가 다시 본다.
            outcomes.append(AccountSyncOutcome(
                external_account_id=account_id, status=SKIPPED,
                reason=ALREADY_RUNNING))
        except Exception as exc:  # noqa: BLE001 — 계정 단위 격리
            db.rollback()
            _log.exception("주문 동기화 실패 account=%s", account_id)
            outcomes.append(AccountSyncOutcome(
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
    """운영자가 보는 주문 동기화 상태.

    "마지막 시도" 와 "마지막 성공" 을 **따로** 보여 준다. 하나로 합치면 지금
    몇 시간째 못 읽고 있는 상태가 성공 시각 뒤에 숨는다.
    """
    policy = _policy(db, org_id=org_id)
    configs = read_config(policy) if policy else {}

    accounts = []
    for account in ready_accounts(db, org_id=org_id):
        account_id = UUID(str(account["id"]))
        config = configs.get(account_id)
        last = db.execute(text("""
            SELECT status, trigger, started_at, finished_at, error_code,
                   read_count, created_count, updated_count, skipped_count,
                   error_count
              FROM external_sync_run
             WHERE org_id = :o AND external_account_id = :a AND sync_type = :t
             ORDER BY started_at DESC LIMIT 1
        """), {"o": str(org_id), "a": str(account_id),
               "t": SYNC_TYPE}).mappings().first()
        accounts.append({
            "external_account_id": str(account_id),
            "display_name": account["display_name"],
            "system_code": account["system_code"],
            # 설정에 없으면 자동으로 돌지 않는다. "0건" 과 구분해야 한다.
            "configured": config is not None,
            "initial_sync_from": (
                config.initial_sync_from.isoformat() if config else None),
            "overlap_days": config.overlap_days if config else None,
            "blocked_by": _account_blocker(db, org_id=org_id, account=account),
            "last_attempt_at": last["started_at"].isoformat() if last else None,
            "last_status": last["status"] if last else None,
            "last_error_code": last["error_code"] if last else None,
            "last_success_at": db.execute(text("""
                SELECT last_successful_sync_at FROM external_account
                 WHERE id = :i AND org_id = :o
            """), {"i": str(account_id), "o": str(org_id)}).scalar(),
            "last_counts": {
                "fetched": last["read_count"], "created": last["created_count"],
                "updated": last["updated_count"], "skipped": last["skipped_count"],
                "rejected": last["error_count"],
            } if last else None,
        })

    return {
        "enabled": bool(policy and policy["enabled"]),
        "mode": policy["mode"] if policy else None,
        "interval_minutes": policy["interval_minutes"] if policy else None,
        "last_run_at": policy["last_run_at"] if policy else None,
        # 주문이 자동으로 들어온다고 취소·반품까지 들어오는 것은 아니다.
        # 한 덩어리로 보이면 사용자는 정산이 맞는다고 믿는다.
        #
        # **"코드가 있는가" 가 아니라 "이 조직에서 실제로 도는가" 다.**
        # Phase 3 로 클레임 수집이 생겼어도 켜지 않았으면 취소·반품은
        # 정말로 반영되지 않는다 — 그때 경고를 지우면 거짓말이 된다.
        "claim_ingest_implemented": _claim_sync_enabled(db, org_id=org_id),
        "accounts": accounts,
    }


def _claim_sync_enabled(db: Session, *, org_id: UUID) -> bool:
    """취소·반품 수집이 이 조직에서 켜져 있고 대상 계정까지 정해졌는가.

    `enabled` 만 보면 부족하다. 켜 두고 조회 기간·대상 계정을 안 넣으면
    자동화가 `NOT_CONFIGURED` 로 막혀 한 건도 안 들어온다 — 그 상태에서
    "반영되고 있다" 고 말하면 화면이 거짓말을 한다.
    """
    from app.services import claim_sync as csync

    policy = csync._policy(db, org_id=org_id)
    if not policy or not policy["enabled"]:
        return False
    try:
        return bool(csync.read_config(policy))
    except Exception:  # noqa: BLE001 — 설정이 깨졌으면 켜졌다고 하지 않는다
        return False


def run_now(
    db: Session, *, org_id: UUID, actor_user_id: UUID,
    external_account_id: UUID | None = None,
) -> list[AccountSyncOutcome]:
    """화면에서 누른 즉시 실행. **자동과 같은 서비스·같은 잠금.**"""
    policy = _policy(db, org_id=org_id)
    if policy is None:
        raise ValidationError("주문 자동 수집 정책이 아직 설정되지 않았습니다.")
    configs = read_config(policy)
    if not configs:
        raise ValidationError("겹쳐 읽기 일수와 대상 계정·시작일을 먼저 설정해 주세요.")
    if external_account_id is not None:
        configs = {k: v for k, v in configs.items() if k == external_account_id}
        if not configs:
            raise ValidationError("그 계정은 주문 동기화 대상이 아닙니다.")

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
