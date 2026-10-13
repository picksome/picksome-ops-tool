"""운영 사건 → 알림 (Phase 39).

무엇이 문제였나
-------------
알림 인프라는 D-281 에서 만들어졌는데 **아무 도메인 서비스도 부르지 않았다.**
승인이 거절돼도, 월 마감이 막혀도, worker 가 죽어도 알림이 생기지 않았다.
화면은 `MOCK_NOTIFICATIONS` 를 보고 있었으므로 그럴듯해 보였을 뿐이다.

이 모듈의 책임
------------
"어떤 사건이 났을 때 누구에게 무엇을 보낼지" 한 곳에서 정한다.
각 도메인 서비스가 알아서 문구를 만들면 같은 사건이 화면마다 다르게 표현되고,
수신자 규칙도 제각각이 된다.

세 가지 규칙
----------

**① 알림이 업무를 막지 않는다.**
알림 전송 실패로 승인이 롤백되면, 알림 기능이 핵심 업무의 장애 지점이 된다.
그래서 `emit()` 은 예외를 밖으로 내보내지 않는다. 실패는 로그로 남긴다.
단, **조용히 성공한 척하지도 않는다** — 돌려주는 값에 사실이 담긴다.

**② 같은 사건으로 반복 생성하지 않는다.**
worker 가 5분마다 돌면서 "월 마감 차단" 을 매번 새로 만들면 하루 288건이다.
사용자는 그 시점부터 벨을 보지 않는다. `dedupe_key` 로 막고, 동시 실행까지
막히도록 DB 부분 UNIQUE 인덱스(M031)를 최종 방어선으로 둔다.

**③ 사건마다 받아야 할 사람이 다르다.**
승인 거절은 **요청자**가 받아야 한다(승인자가 아니라). worker 실패는 관리자다.
"전체 발송" 으로 뭉뚱그리면 관계없는 사람의 알림함이 먼저 무의미해진다.

Slack / Discord
--------------
**성공 mock 을 만들지 않는다.** webhook 이 없으므로 채널은 `DISCONNECTED` 상태를
유지한다. `get_mvp_active_channels()` 가 IN_APP·EMAIL 만 돌려주고, EMAIL 은 SMTP
미설정 시 어댑터가 `QUEUED` 로 표시한다 — 보냈다고 말하지 않는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.ids import new_id
from app.core.notification import NotificationPayload, Recipient, get_channel

_log = logging.getLogger("notification_events")

# 이 모듈이 다루는 사건. `enum_notification_kind` (M031)와 **정확히** 같아야 한다.
APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
APPROVAL_REJECTED = "APPROVAL_REJECTED"
APPROVAL_APPLY_FAILED = "APPROVAL_APPLY_FAILED"
MONTH_CLOSE_BLOCKED = "MONTH_CLOSE_BLOCKED"
MONTH_CLOSE_READY = "MONTH_CLOSE_READY"
PAYOUT_PENDING = "PAYOUT_PENDING"
ATTACHMENT_DELETE_FAILED = "ATTACHMENT_DELETE_FAILED"
CHANNEL_PRICE_PUBLISH_FAILED = "CHANNEL_PRICE_PUBLISH_FAILED"
WORKER_FAILURE = "WORKER_FAILURE"
DAILY_REPORT_READY = "DAILY_REPORT_READY"

EVENT_KINDS = frozenset({
    APPROVAL_REQUESTED, APPROVAL_REJECTED, APPROVAL_APPLY_FAILED,
    MONTH_CLOSE_BLOCKED, MONTH_CLOSE_READY, PAYOUT_PENDING,
    ATTACHMENT_DELETE_FAILED, CHANNEL_PRICE_PUBLISH_FAILED, WORKER_FAILURE,
    DAILY_REPORT_READY,
})

# 사건별 기본 수신 역할.
#
# 승인 관련은 **요청자 본인**에게 가므로 역할 목록이 아니다(아래 emit 호출부 참고).
# 여기 있는 것은 "역할로 정해지는" 사건뿐이다.
_ROLE_RECIPIENTS: dict[str, tuple[str, ...]] = {
    # 일일 리포트는 운영하는 사람이 본다. 정산 담당까지 넣는 이유는 마감·정산
    # 이상이 리포트 맨 위에 오기 때문이다. 읽기 전용 역할에는 보내지 않는다 —
    # 볼 수는 있어도 손댈 것이 없어 알림이 소음이 된다.
    DAILY_REPORT_READY: ("ADMIN", "OPERATOR", "ACCOUNTING"),
    MONTH_CLOSE_BLOCKED: ("ADMIN", "ACCOUNTING"),
    MONTH_CLOSE_READY: ("ADMIN", "ACCOUNTING"),
    PAYOUT_PENDING: ("ADMIN", "ACCOUNTING"),
    ATTACHMENT_DELETE_FAILED: ("ADMIN",),
    CHANNEL_PRICE_PUBLISH_FAILED: ("ADMIN", "OPERATOR"),
    WORKER_FAILURE: ("ADMIN",),
}


@dataclass(slots=True)
class EmitResult:
    """무슨 일이 일어났는지 사실대로 담는다."""

    created: int = 0
    deduped: int = 0
    failed: int = 0
    recipients: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created, "deduped": self.deduped,
            "failed": self.failed, "recipients": self.recipients,
        }


def _users_with_roles(
    db: Session, *, org_id: UUID, roles: tuple[str, ...]
) -> list[dict[str, Any]]:
    rows = db.execute(text("""
        SELECT id, email, display_name FROM app_user
         WHERE org_id = :org AND is_active = TRUE AND deleted_at IS NULL
           AND role = ANY(:roles)
    """), {"org": str(org_id), "roles": list(roles)}).mappings().all()
    return [dict(r) for r in rows]


def _user(db: Session, *, org_id: UUID, user_id: UUID) -> dict[str, Any] | None:
    row = db.execute(text("""
        SELECT id, email, display_name FROM app_user
         WHERE id = :id AND org_id = :org AND deleted_at IS NULL
    """), {"id": str(user_id), "org": str(org_id)}).mappings().first()
    return dict(row) if row else None


def emit(
    db: Session, *, org_id: UUID, kind: str, title: str,
    dedupe_key: str,
    body: str | None = None,
    target_url: str | None = None,
    meta: dict[str, Any] | None = None,
    to_user_ids: list[UUID] | None = None,
    channels: tuple[str, ...] = ("IN_APP",),
    _commit: bool = False,
) -> EmitResult:
    """사건 하나를 알림으로 만든다.

    **예외를 밖으로 내보내지 않는다.** 알림 실패가 승인·마감을 롤백시키면
    알림이 핵심 업무의 장애 지점이 된다. 대신 결과에 사실을 담아 돌려준다.

    수신자를 지정하지 않으면 `_ROLE_RECIPIENTS` 의 역할로 찾는다.

    기본 채널은 IN_APP 뿐이다. EMAIL 을 함께 보내려면 호출자가 명시한다 —
    SMTP 가 없는 환경에서 모든 사건마다 QUEUED 행이 쌓이는 것을 막는다.
    """
    result = EmitResult()

    if kind not in EVENT_KINDS:
        # 오타를 새 사건으로 받아들이면 알림이 조용히 갈라진다.
        _log.error("알 수 없는 알림 종류: %s", kind)
        result.failed += 1
        return result

    try:
        if to_user_ids is not None:
            targets = [u for u in (_user(db, org_id=org_id, user_id=uid) for uid in to_user_ids) if u]
        else:
            roles = _ROLE_RECIPIENTS.get(kind, ("ADMIN",))
            targets = _users_with_roles(db, org_id=org_id, roles=roles)
    except Exception:  # noqa: BLE001 — 수신자 조회 실패가 업무를 막지 않는다
        _log.exception("알림 수신자 조회 실패: kind=%s", kind)
        result.failed += 1
        return result

    result.recipients = len(targets)
    payload = NotificationPayload(
        kind=kind, title=title, body=body, target_url=target_url, meta=meta
    )

    for target in targets:
        for code in channels:
            try:
                adapter = get_channel(code)  # type: ignore[arg-type]
                delivery = adapter.send(payload, Recipient(
                    user_id=UUID(str(target["id"])), org_id=org_id,
                    email=target.get("email"), display_name=target.get("display_name") or "",
                ))
                inserted = _insert(
                    db, org_id=org_id, user_id=UUID(str(target["id"])), kind=kind,
                    title=title, body=body, target_url=target_url, meta=meta,
                    channel_code=delivery.channel_code,
                    delivery_status=delivery.delivery_status,
                    error_message=delivery.error_message,
                    dedupe_key=dedupe_key,
                )
                if inserted:
                    result.created += 1
                else:
                    result.deduped += 1
            except Exception:  # noqa: BLE001
                _log.exception(
                    "알림 생성 실패: kind=%s channel=%s user=%s", kind, code, target["id"]
                )
                result.failed += 1

    if _commit and result.created:
        db.commit()
    return result


def _insert(
    db: Session, *, org_id: UUID, user_id: UUID, kind: str, title: str,
    body: str | None, target_url: str | None, meta: dict[str, Any] | None,
    channel_code: str, delivery_status: str, error_message: str | None,
    dedupe_key: str,
) -> bool:
    """알림 1행. 이미 같은 dedupe_key 가 있으면 넣지 않는다.

    `ON CONFLICT DO NOTHING` 을 쓴다 — 조회 후 삽입은 동시 실행에서 뚫린다.
    두 worker 가 같은 순간에 같은 사건을 보면 둘 다 "없다" 를 읽는다.

    `SAVEPOINT` 안에서 실행한다. 바깥 트랜잭션(승인 적용 등)이 진행 중일 때
    여기서 오류가 나면 그 트랜잭션 전체가 무효화되기 때문이다 —
    알림 때문에 승인이 취소되면 안 된다.
    """
    import json

    with db.begin_nested():
        row = db.execute(text("""
            INSERT INTO notification (
              id, org_id, recipient_user_id, kind, title, body,
              channel_code, target_url, sent_at, delivery_status,
              error_message, meta_json, dedupe_key
            ) VALUES (
              :id, :o, :u, CAST(:k AS enum_notification_kind), :t, :b,
              CAST(:c AS enum_notification_channel_code), :tu,
              CASE WHEN :st = 'SENT' THEN now() ELSE NULL END, :st,
              :err, CAST(:meta AS JSONB), :dk
            )
            ON CONFLICT (recipient_user_id, channel_code, dedupe_key)
              WHERE dedupe_key IS NOT NULL AND deleted_at IS NULL
              DO NOTHING
            RETURNING id
        """), {
            "id": str(new_id()), "o": str(org_id), "u": str(user_id), "k": kind,
            "t": title[:300], "b": body, "c": channel_code, "tu": target_url,
            "st": delivery_status, "err": error_message,
            "meta": json.dumps(meta, ensure_ascii=False, default=str) if meta else None,
            "dk": dedupe_key,
        }).first()
    return row is not None


# ──────────────────────────────────────────────────────────────────────────
# 사건별 진입점
#
# 도메인 서비스는 이 함수들만 부른다. 문구와 수신자 규칙이 여기 모여 있어야
# 같은 사건이 화면마다 다르게 보이지 않는다.
# ──────────────────────────────────────────────────────────────────────────

def approval_requested(
    db: Session, *, org_id: UUID, approval_id: UUID, business_key: str,
    request_type: str, requester_name: str,
) -> EmitResult:
    """승인 요청이 올라왔다 → 승인 권한자에게.

    요청자 본인에게는 보내지 않는다 — 자기가 방금 한 일이다.
    """
    return emit(
        db, org_id=org_id, kind=APPROVAL_REQUESTED,
        title=f"승인 요청: {request_type}",
        body=f"{requester_name} 님이 승인을 요청했습니다 ({business_key})",
        target_url=f"/approvals?id={approval_id}",
        dedupe_key=f"approval:{approval_id}:requested",
        meta={"approval_id": str(approval_id), "business_key": business_key},
        to_user_ids=[
            UUID(str(u["id"]))
            for u in _users_with_roles(db, org_id=org_id, roles=("ADMIN", "ACCOUNTING"))
        ],
    )


def approval_rejected(
    db: Session, *, org_id: UUID, approval_id: UUID, business_key: str,
    requester_user_id: UUID, reason: str | None,
) -> EmitResult:
    """승인이 거절됐다 → **요청자**에게.

    승인자에게 보내면 자기가 방금 누른 버튼을 다시 알려주는 꼴이다.
    거절 사실을 알아야 하는 사람은 요청한 사람이다.
    """
    return emit(
        db, org_id=org_id, kind=APPROVAL_REJECTED,
        title="승인 요청이 거절되었습니다",
        body=(f"사유: {reason}" if reason else "사유가 기록되지 않았습니다") + f" ({business_key})",
        target_url=f"/approvals?id={approval_id}",
        dedupe_key=f"approval:{approval_id}:rejected",
        meta={"approval_id": str(approval_id)},
        to_user_ids=[requester_user_id],
    )


def approval_apply_failed(
    db: Session, *, org_id: UUID, approval_id: UUID, business_key: str,
    error_code: str | None, requester_user_id: UUID | None = None,
) -> EmitResult:
    """승인은 됐는데 적용이 실패했다.

    **가장 위험한 상태다.** 승인자는 처리했다고 믿고, 요청자는 반영됐다고 믿는데
    데이터는 그대로다. 관리자와 요청자 모두에게 알린다.
    """
    admins = [
        UUID(str(u["id"]))
        for u in _users_with_roles(db, org_id=org_id, roles=("ADMIN",))
    ]
    if requester_user_id and requester_user_id not in admins:
        admins.append(requester_user_id)
    return emit(
        db, org_id=org_id, kind=APPROVAL_APPLY_FAILED,
        title="승인된 요청의 적용이 실패했습니다",
        body=f"{business_key} — 오류 {error_code or '알 수 없음'}. 승인은 됐지만 데이터에 반영되지 않았습니다.",
        target_url=f"/approvals?id={approval_id}",
        dedupe_key=f"approval:{approval_id}:apply_failed",
        meta={"approval_id": str(approval_id), "error_code": error_code},
        to_user_ids=admins,
    )


def month_close_blocked(
    db: Session, *, org_id: UUID, year_month: str, blocking_codes: list[str],
) -> EmitResult:
    """마감을 막는 항목이 있다.

    dedupe 키에 차단 항목 조합을 넣는다 — **차단 사유가 바뀌면 새 알림이다.**
    같은 사유가 계속되는 동안에는 하나로 유지된다.
    """
    codes = ",".join(sorted(blocking_codes))
    return emit(
        db, org_id=org_id, kind=MONTH_CLOSE_BLOCKED,
        title=f"{year_month} 마감이 차단되었습니다",
        body=f"차단 항목 {len(blocking_codes)}건을 먼저 처리해야 합니다.",
        target_url=f"/settlements/monthly/{year_month}",
        dedupe_key=f"close:{year_month}:blocked:{codes}",
        meta={"year_month": year_month, "blocking_codes": sorted(blocking_codes)},
    )


def month_close_ready(db: Session, *, org_id: UUID, year_month: str) -> EmitResult:
    """차단 항목이 없다 — 확정할 수 있다."""
    return emit(
        db, org_id=org_id, kind=MONTH_CLOSE_READY,
        title=f"{year_month} 마감을 확정할 수 있습니다",
        body="차단 항목이 없습니다. 경고를 확인한 뒤 확정하세요.",
        target_url=f"/settlements/monthly/{year_month}",
        dedupe_key=f"close:{year_month}:ready",
        meta={"year_month": year_month},
    )


def payout_pending(
    db: Session, *, org_id: UUID, batch_id: UUID, title: str, outstanding: int,
) -> EmitResult:
    """지급할 것이 남아 있다."""
    return emit(
        db, org_id=org_id, kind=PAYOUT_PENDING,
        title="지급 예정 금액이 있습니다",
        body=f"{title} — 미지급 {outstanding:,}원",
        target_url="/payouts",
        dedupe_key=f"payout:{batch_id}:pending",
        meta={"batch_id": str(batch_id), "outstanding": outstanding},
    )


def attachment_delete_failed(
    db: Session, *, org_id: UUID, attachment_id: UUID, error_code: str | None,
) -> EmitResult:
    """증빙 파일 삭제 실패.

    **저장 경로를 본문에 넣지 않는다.** 관리 화면에 내부 파일 경로가 노출되면
    그 자체가 정보 누출이다.
    """
    return emit(
        db, org_id=org_id, kind=ATTACHMENT_DELETE_FAILED,
        title="증빙 파일 삭제가 실패했습니다",
        body=f"오류 {error_code or '알 수 없음'}. 파일이 디스크에 남아 있을 수 있습니다.",
        target_url="/operations/issues?issue_type=ATTACHMENT_DELETE_FAILED",
        dedupe_key=f"attachment:{attachment_id}:delete_failed",
        meta={"attachment_id": str(attachment_id), "error_code": error_code},
    )


def channel_price_publish_failed(
    db: Session, *, org_id: UUID, outbox_id: UUID, marketplace: str,
    error_code: str | None,
) -> EmitResult:
    """판매처 가격 반영 실패 — 내부 가격과 판매처 가격이 어긋난 상태다."""
    return emit(
        db, org_id=org_id, kind=CHANNEL_PRICE_PUBLISH_FAILED,
        title=f"{marketplace} 가격 반영이 실패했습니다",
        body=f"오류 {error_code or '알 수 없음'}. 내부 확정가와 판매처 가격이 다를 수 있습니다.",
        target_url="/operations/issues?issue_type=CHANNEL_PRICE_PUBLISH_FAILED",
        dedupe_key=f"channel_price:{outbox_id}:publish_failed",
        meta={"outbox_id": str(outbox_id), "marketplace": marketplace},
    )


def worker_failure(
    db: Session, *, org_id: UUID, worker_name: str, run_id: UUID,
    error_code: str | None,
) -> EmitResult:
    """worker 실행 실패.

    run 마다 새 알림이다 — 같은 worker 가 반복 실패하는 것은 **다른 사건**이고,
    반복 횟수 자체가 정보다. 다만 같은 run 이 두 번 알림을 만들지는 않는다.
    """
    return emit(
        db, org_id=org_id, kind=WORKER_FAILURE,
        title=f"백그라운드 작업 실패: {worker_name}",
        body=f"오류 {error_code or '알 수 없음'}. 시스템 상태 화면에서 확인하세요.",
        target_url="/operations/system",
        dedupe_key=f"worker:{run_id}:failed",
        meta={"worker_name": worker_name, "run_id": str(run_id)},
    )


def daily_report_ready(
    db: Session, *, org_id: UUID, report_date: date, attention_count: int,
) -> EmitResult:
    """오늘의 리포트가 준비됐다 → 운영·정산 담당에게.

    `dedupe_key` 가 날짜다. worker 가 두 번 돌아도 같은 날 알림이 두 번 가지
    않는다 — 매일 아침 같은 알림이 두 개씩 오면 사용자는 벨을 아예 안 본다.

    채널은 IN_APP 뿐이다. EMAIL 을 넣으면 SMTP 가 없는 지금 보내지지도 않을
    QUEUED 행만 쌓이고 화면에는 발송된 것처럼 보인다.
    """
    body = (
        f"확인이 필요한 항목 {attention_count}건이 있습니다."
        if attention_count
        else "확인이 필요한 항목이 없습니다."
    )
    return emit(
        db, org_id=org_id, kind=DAILY_REPORT_READY,
        title=f"{report_date} 운영 리포트",
        body=body,
        target_url="/operations/daily",
        dedupe_key=f"daily_report:{report_date.isoformat()}",
        meta={"report_date": report_date.isoformat(),
              "attention_count": attention_count},
    )
