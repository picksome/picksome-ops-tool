"""판매처 가격 반영 — outbox 적재와 runner (M024).

무엇을 하고 무엇을 하지 않나
--------------------------
**한다**: 승인 적용과 같은 트랜잭션에서 작업을 큐에 넣고, 별도 runner 가 lease 를 잡아
하나씩 처리한다.

**하지 않는다**: 실제 판매처 API 호출. connector 가 하나도 없다.
빈 껍데기를 만들어 성공을 반환하지 않는다 — 사용자가 반영됐다고 믿는 순간
실제 판매처 가격은 그대로이고, 그 어긋남은 가장 늦게 발견된다.

지금 runner 를 돌리면 모든 작업이 `CONNECTOR_UNAVAILABLE` 로 끝난다.
그건 실패가 아니라 **정직한 상태**다. 그래서 재시도하지 않는다 —
connector 가 없는 건 기다린다고 해결되지 않는다.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.exceptions import ConnectorUnavailableError, NotFoundError
from app.services import channel_connector

# lease 길이. 워커가 죽으면 이 시간이 지난 뒤 다른 워커가 회수한다.
# 너무 짧으면 정상 작업을 뺏고, 너무 길면 죽은 작업이 오래 묶인다.
LEASE_SECONDS = 120

MAX_ATTEMPTS = 5
_BACKOFF_MINUTES = (1, 5, 15, 60, 240)

# runner 중복 실행 방지용 advisory lock. approval executor 와 다른 namespace 를 쓴다.
_LOCK_NAMESPACE = 0x43505042  # 'CPPB'


def _now() -> datetime:
    return datetime.now(tz=UTC)


def dedupe_key_for(price_id: UUID) -> str:
    """같은 가격 버전을 두 번 보내지 않기 위한 키.

    가격 버전 id 그 자체다 — 가격이 바뀌면 새 버전이 생기므로 키도 자동으로 달라진다.
    """
    return f"price:{price_id}"


# ──────────────────────────────────────────────────────────────
# 적재 — 승인 적용과 **같은 트랜잭션**
# ──────────────────────────────────────────────────────────────

def enqueue_if_auto_publish(
    db: Session, *, org_id: UUID, listing_id: UUID, price_id: UUID, actor: UUID
) -> UUID | None:
    """listing 이 자동 반영이면 outbox 에 넣는다. **커밋하지 않는다.**

    자동 반영이 꺼져 있으면 아무것도 하지 않는다 — 기본값이 수동이고,
    사용자가 직접 "판매처에 반영" 을 눌러야 큐에 들어간다.
    """
    row = db.execute(text("""
        SELECT auto_publish_price, integration_account_id
          FROM product_channel_listing
         WHERE id = :i AND org_id = :o AND deleted_at IS NULL
    """), {"i": str(listing_id), "o": str(org_id)}).first()
    if row is None or not row.auto_publish_price:
        return None
    return _insert_job(db, org_id=org_id, account_id=UUID(str(row.integration_account_id)),
                       listing_id=listing_id, price_id=price_id, actor=actor)


def request_publish(
    db: Session, *, org_id: UUID, price_id: UUID, actor: UUID, _commit: bool = True
) -> UUID:
    """사용자가 직접 반영을 요청한다 (수동 경로).

    connector 가 없으면 **큐에 넣지 않고 즉시 거절한다.** 넣어봐야
    `CONNECTOR_UNAVAILABLE` 로 끝날 작업이고, 큐에 쌓이면 "처리 중" 처럼 보인다.
    """
    row = db.execute(text("""
        SELECT p.listing_id, l.integration_account_id,
               COALESCE(sc.code, ia.provider) AS marketplace
          FROM product_channel_price p
          JOIN product_channel_listing l ON l.id = p.listing_id
          JOIN integration_account ia ON ia.id = l.integration_account_id
     LEFT JOIN sale_channel sc ON sc.id = ia.sale_channel_id
         WHERE p.id = :i AND p.org_id = :o AND p.deleted_at IS NULL
    """), {"i": str(price_id), "o": str(org_id)}).first()
    if row is None:
        raise NotFoundError("가격을 찾을 수 없습니다.", detail={"price_id": str(price_id)})

    channel_connector.assert_publish_supported(row.marketplace)

    job_id = _insert_job(db, org_id=org_id,
                         account_id=UUID(str(row.integration_account_id)),
                         listing_id=UUID(str(row.listing_id)),
                         price_id=price_id, actor=actor)
    if _commit:
        db.commit()
    return job_id


def _insert_job(
    db: Session, *, org_id: UUID, account_id: UUID, listing_id: UUID,
    price_id: UUID, actor: UUID,
) -> UUID | None:
    job_id = uuid.uuid4()
    try:
        savepoint = db.begin_nested()
        db.execute(text("""
            INSERT INTO channel_price_publish_outbox
                   (id, org_id, integration_account_id, listing_id, channel_price_id,
                    status, dedupe_key, next_attempt_at, created_by, updated_by)
            VALUES (:i, :o, :a, :l, :p, 'QUEUED', :dk, now(), :actor, :actor)
        """), {"i": str(job_id), "o": str(org_id), "a": str(account_id),
               "l": str(listing_id), "p": str(price_id),
               "dk": dedupe_key_for(price_id), "actor": str(actor)})
        savepoint.commit()
    except IntegrityError:
        # 같은 가격 버전의 활성 작업이 이미 있다. 중복 요청은 조용히 무시한다 —
        # 오류로 만들면 사용자가 버튼을 두 번 눌렀다는 이유로 승인이 실패한다.
        savepoint.rollback()
        return None

    # 가격 행에도 상태를 반영한다. 화면이 outbox 를 조인하지 않고 읽을 수 있어야 한다.
    db.execute(text("""
        UPDATE product_channel_price
           SET publish_status = 'QUEUED', publish_requested_at = now(),
               publish_error = NULL, updated_by = :actor
         WHERE id = :i AND org_id = :o
    """), {"i": str(price_id), "o": str(org_id), "actor": str(actor)})
    return job_id


# ──────────────────────────────────────────────────────────────
# runner
# ──────────────────────────────────────────────────────────────

@dataclass(slots=True)
class PublishBatchResult:
    batch_id: str
    started_at: str
    finished_at: str | None = None
    claimed: int = 0
    published: int = 0
    connector_unavailable: int = 0
    retry_pending: int = 0
    failed: int = 0
    reclaimed_stale: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id, "started_at": self.started_at,
            "finished_at": self.finished_at, "claimed": self.claimed,
            "published": self.published,
            "connector_unavailable": self.connector_unavailable,
            "retry_pending": self.retry_pending, "failed": self.failed,
            "reclaimed_stale": self.reclaimed_stale, "details": self.details,
        }


@contextmanager
def _runner_lock(db: Session, org_id: UUID | None) -> Iterator[None]:
    """runner 전체 중복 실행 방지.

    **개별 작업 선점은 이 잠금이 하지 않는다** — 그건 outbox row 의 상태와 lease 가 한다.
    이 잠금은 배치가 여러 개 나란히 도는 것만 막는다.
    """
    from app.core.exceptions import ConflictError

    conn = db.get_bind().connect()
    key = 0 if org_id is None else (org_id.int % (2**31 - 1)) or 1
    try:
        got = bool(conn.execute(
            text("SELECT pg_try_advisory_lock(:ns, :k)"),
            {"ns": _LOCK_NAMESPACE, "k": key},
        ).scalar())
        if not got:
            raise ConflictError(
                "가격 반영 배치가 이미 실행 중입니다.",
                detail={"org_id": str(org_id) if org_id else None},
            )
        yield
    finally:
        try:
            conn.execute(text("SELECT pg_advisory_unlock(:ns, :k)"),
                         {"ns": _LOCK_NAMESPACE, "k": key})
        except Exception:  # noqa: BLE001 - close() 가 최종 회수 경로다
            pass
        conn.close()


def _reclaim_stale(db: Session, org_id: UUID | None) -> int:
    """lease 가 만료된 PROCESSING 작업을 회수한다.

    이게 없으면 워커가 죽었을 때 그 작업이 영원히 PROCESSING 으로 남는다.
    """
    where = "status = 'PROCESSING' AND lease_expires_at IS NOT NULL AND lease_expires_at < now()"
    params: dict[str, Any] = {}
    if org_id is not None:
        where += " AND org_id = :org"
        params["org"] = str(org_id)
    result = db.execute(text(f"""
        UPDATE channel_price_publish_outbox
           SET status = 'RETRY_PENDING', locked_at = NULL, lease_expires_at = NULL,
               locked_by = NULL, next_attempt_at = now(),
               last_error_code = 'LEASE_EXPIRED',
               last_error_message = '작업 프로세스가 응답하지 않아 회수했습니다.'
         WHERE {where}
    """), params)
    db.commit()
    return result.rowcount or 0


def _claim(db: Session, org_id: UUID | None, worker: str, batch_size: int) -> list[dict]:
    """작업 선점. `FOR UPDATE SKIP LOCKED` + lease.

    선점과 lease 설정이 한 트랜잭션이라, 다른 워커가 같은 행을 집을 수 없다.
    """
    where = ["status IN ('QUEUED','RETRY_PENDING')",
             "(next_attempt_at IS NULL OR next_attempt_at <= now())"]
    params: dict[str, Any] = {"lim": batch_size, "worker": worker,
                              "lease": LEASE_SECONDS}
    if org_id is not None:
        where.append("org_id = :org")
        params["org"] = str(org_id)

    rows = db.execute(text(f"""
        WITH claimed AS (
          SELECT id FROM channel_price_publish_outbox
           WHERE {' AND '.join(where)}
           ORDER BY created_at, id
           FOR UPDATE SKIP LOCKED
           LIMIT :lim
        )
        UPDATE channel_price_publish_outbox o
           SET status = 'PROCESSING', locked_at = now(), locked_by = :worker,
               lease_expires_at = now() + make_interval(secs => :lease),
               started_at = COALESCE(o.started_at, now())
          FROM claimed c
         WHERE o.id = c.id
     RETURNING o.id, o.org_id, o.listing_id, o.channel_price_id,
               o.integration_account_id, o.attempt_count
    """), params).mappings().all()
    db.commit()
    return [dict(r) for r in rows]


def _marketplace_of(db: Session, account_id: UUID) -> str | None:
    return db.execute(text("""
        SELECT COALESCE(sc.code, ia.provider)
          FROM integration_account ia
     LEFT JOIN sale_channel sc ON sc.id = ia.sale_channel_id
         WHERE ia.id = :i
    """), {"i": str(account_id)}).scalar()


def _finish(
    db: Session, job_id: UUID, price_id: UUID, *,
    job_status: str, price_status: str,
    error_code: str | None = None, error_message: str | None = None,
    next_attempt_minutes: int | None = None, bump_attempt: bool = True,
) -> None:
    db.execute(text("""
        UPDATE channel_price_publish_outbox
           SET status = :st,
               attempt_count = attempt_count + CASE WHEN :bump THEN 1 ELSE 0 END,
               locked_at = NULL, lease_expires_at = NULL, locked_by = NULL,
               last_error_code = :code, last_error_message = :msg,
               next_attempt_at = CASE WHEN CAST(:mins AS INTEGER) IS NULL THEN NULL
                                      ELSE now() + make_interval(mins => CAST(:mins AS INTEGER))
                                 END,
               finished_at = CASE WHEN :st IN ('PUBLISHED','FAILED','CONNECTOR_UNAVAILABLE',
                                               'VERIFICATION_FAILED','CANCELLED')
                                  THEN now() ELSE NULL END
         WHERE id = :i
    """), {"st": job_status, "code": error_code, "msg": (error_message or "")[:500] or None,
           "mins": next_attempt_minutes, "i": str(job_id), "bump": bump_attempt})

    db.execute(text("""
        UPDATE product_channel_price
           SET publish_status = :ps,
               publish_error = :msg,
               publish_attempt_count = publish_attempt_count + CASE WHEN :bump THEN 1 ELSE 0 END,
               published_at = CASE WHEN :ps = 'PUBLISHED' THEN now() ELSE published_at END
         WHERE id = :i
    """), {"ps": price_status, "msg": (error_message or "")[:500] or None,
           "i": str(price_id), "bump": bump_attempt})
    db.commit()


def run_publish_batch(
    db: Session, *, org_id: UUID | None = None, batch_size: int = 20,
    worker: str | None = None,
) -> PublishBatchResult:
    """대기 중인 가격 반영 작업을 처리한다.

    connector 가 없으므로 **현재는 모든 작업이 `CONNECTOR_UNAVAILABLE` 로 끝난다.**
    이건 실패가 아니라 정직한 상태다. 재시도하지 않는다 — connector 가 없는 건
    기다린다고 해결되지 않고, 재시도하면 실패 카운트만 부풀어 진짜 오류가 묻힌다.
    """
    worker = worker or f"worker-{uuid.uuid4().hex[:8]}"
    result = PublishBatchResult(
        batch_id=uuid.uuid4().hex[:12],
        started_at=_now().isoformat(),
    )

    with _runner_lock(db, org_id):
        result.reclaimed_stale = _reclaim_stale(db, org_id)
        claimed = _claim(db, org_id, worker, batch_size)
        result.claimed = len(claimed)

        for job in claimed:
            job_id = UUID(str(job["id"]))
            price_id = UUID(str(job["channel_price_id"]))
            attempt = int(job["attempt_count"])
            marketplace = _marketplace_of(db, UUID(str(job["integration_account_id"])))
            entry: dict[str, Any] = {"job_id": str(job_id), "marketplace": marketplace}

            try:
                channel_connector.assert_publish_supported(marketplace)
            except ConnectorUnavailableError as exc:
                _finish(db, job_id, price_id,
                        job_status="CONNECTOR_UNAVAILABLE",
                        price_status="NOT_REQUESTED",
                        error_code=exc.code, error_message=exc.message,
                        next_attempt_minutes=None, bump_attempt=False)
                result.connector_unavailable += 1
                entry["result"] = "CONNECTOR_UNAVAILABLE"
                result.details.append(entry)
                continue

            # ── 여기부터가 실제 전송 자리. connector 가 생기면 이 블록만 채운다. ──
            #
            # 지금 도달할 수 없다(위에서 전부 걸러진다). 도달했다면 레지스트리에
            # 지원한다고 등록된 판매처인데 구현이 없는 것이므로, 성공으로 처리하지 않고
            # 영구 실패로 남긴다. **절대 mock 성공을 만들지 않는다.**
            _finish(db, job_id, price_id,
                    job_status="FAILED", price_status="FAILED",
                    error_code="CONNECTOR_NOT_IMPLEMENTED",
                    error_message="지원한다고 등록됐으나 전송 구현이 없습니다.",
                    next_attempt_minutes=(
                        _BACKOFF_MINUTES[min(attempt, len(_BACKOFF_MINUTES) - 1)]
                        if attempt + 1 < MAX_ATTEMPTS else None
                    ))
            result.failed += 1
            entry["result"] = "FAILED"
            result.details.append(entry)

    result.finished_at = _now().isoformat()
    return result
