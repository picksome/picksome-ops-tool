"""APScheduler 잡 정의 (docs/25_devops.md §5).

등록된 잡:
  1. settlement_close_notify  — 매일 09:00 KST, 정산 마감 D-3·D-1 알림
  2. vat_deadline_notify      — 매일 09:00 KST, 부가세 신고 D-7·D-1 알림 (분기 25일)
  3. margin_drop_detect       — 매일 09:00 KST, 30일 이동평균 대비 -10% 감지
  4. policy_needs_review      — 매일 03:00 KST, effective_from 180일 초과 정책 상태 갱신
  5. backup_verify_remind     — 매주 일 04:00 KST, 백업 검증 알림

apscheduler 3.x + AsyncIOScheduler 사용.
DB 접근은 SessionLocal() 직접 생성 (FastAPI Depends 컨텍스트 밖).
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from app.db.session import SessionLocal

_log = logging.getLogger("scheduler")

KST = "Asia/Seoul"


# ────────────────────────────────────────────────────────────────────────────
# 1. 정산 마감 알림
# ────────────────────────────────────────────────────────────────────────────

def _settlement_close_notify() -> None:
    today = date.today()
    with SessionLocal() as db:
        rows = db.execute(
            text("""
                SELECT o.id AS org_id, sp.close_day
                FROM organization o
                JOIN settlement_policy sp ON sp.org_id = o.id
                    AND sp.effective_from <= :today
                    AND (sp.effective_to IS NULL OR sp.effective_to >= :today)
                    AND sp.deleted_at IS NULL
                WHERE o.deleted_at IS NULL
            """),
            {"today": today},
        ).fetchall()

        for row in rows:
            close_day: int = row.close_day
            # 이번 달 마감일 계산
            deadline = today.replace(day=close_day)
            if deadline < today:
                # 이미 지났으면 다음달
                next_month = today.replace(day=1) + timedelta(days=32)
                deadline = next_month.replace(day=close_day)

            days_left = (deadline - today).days
            if days_left not in (3, 1):
                continue

            _log.info(
                "settlement_close_notify: org=%s deadline=%s days_left=%d",
                row.org_id, deadline, days_left,
            )
            db.execute(
                text("""
                    INSERT INTO notification (id, org_id, channel, title, body, is_read, created_at)
                    VALUES (gen_random_uuid(), :org_id, 'IN_APP',
                        :title, :body, false, now())
                """),
                {
                    "org_id": str(row.org_id),
                    "title": f"정산 마감 D-{days_left}",
                    "body": f"{deadline.strftime('%Y년 %m월 %d일')} 정산 마감입니다. 비용·매출 입력을 완료해주세요.",
                },
            )
        db.commit()


# ────────────────────────────────────────────────────────────────────────────
# 2. 부가세 신고 알림 (분기 25일 기준)
# ────────────────────────────────────────────────────────────────────────────

_VAT_DEADLINE_MONTHS = (1, 4, 7, 10)  # 1·4·7·10월 25일


def _vat_deadline_notify() -> None:
    today = date.today()
    if today.month not in _VAT_DEADLINE_MONTHS:
        return

    deadline = today.replace(day=25)
    if deadline < today:
        return

    days_left = (deadline - today).days
    if days_left not in (7, 1):
        return

    with SessionLocal() as db:
        org_ids = db.execute(
            text("SELECT id FROM organization WHERE deleted_at IS NULL"),
        ).fetchall()

        for row in org_ids:
            _log.info("vat_deadline_notify: org=%s days_left=%d", row.id, days_left)
            db.execute(
                text("""
                    INSERT INTO notification (id, org_id, channel, title, body, is_read, created_at)
                    VALUES (gen_random_uuid(), :org_id, 'IN_APP',
                        :title, :body, false, now())
                """),
                {
                    "org_id": str(row.id),
                    "title": f"부가세 신고 D-{days_left}",
                    "body": f"{today.strftime('%Y년 %m월')} 부가세 신고 마감이 {days_left}일 남았습니다.",
                },
            )
        db.commit()


# ────────────────────────────────────────────────────────────────────────────
# 3. 마진 하락 감지 (30일 이동평균 대비 -10%)
# ────────────────────────────────────────────────────────────────────────────

def _margin_drop_detect() -> None:
    today = date.today()
    window_start = today - timedelta(days=30)
    prev_start = today - timedelta(days=60)

    with SessionLocal() as db:
        rows = db.execute(
            text("""
                SELECT
                    pma.product_id,
                    p.org_id,
                    p.name AS product_name,
                    AVG(CASE WHEN pma.sale_date >= :window_start THEN pma.margin_rate END) AS recent_avg,
                    AVG(CASE WHEN pma.sale_date >= :prev_start
                              AND pma.sale_date < :window_start THEN pma.margin_rate END) AS prev_avg
                FROM product_margin_actual pma
                JOIN product p ON p.id = pma.product_id
                WHERE pma.sale_date >= :prev_start
                  AND pma.deleted_at IS NULL
                GROUP BY pma.product_id, p.org_id, p.name
                HAVING
                    AVG(CASE WHEN pma.sale_date >= :prev_start
                              AND pma.sale_date < :window_start THEN pma.margin_rate END) > 0
                    AND AVG(CASE WHEN pma.sale_date >= :window_start THEN pma.margin_rate END)
                        < AVG(CASE WHEN pma.sale_date >= :prev_start
                                    AND pma.sale_date < :window_start THEN pma.margin_rate END) * 0.9
            """),
            {
                "window_start": window_start,
                "prev_start": prev_start,
            },
        ).fetchall()

        for row in rows:
            drop_pct = round((1 - float(row.recent_avg) / float(row.prev_avg)) * 100, 1)
            _log.warning(
                "margin_drop: product=%s drop=%.1f%%", row.product_id, drop_pct
            )
            db.execute(
                text("""
                    INSERT INTO notification (id, org_id, channel, title, body, is_read, created_at)
                    VALUES (gen_random_uuid(), :org_id, 'IN_APP',
                        :title, :body, false, now())
                """),
                {
                    "org_id": str(row.org_id),
                    "title": f"마진 하락 경고: {row.product_name}",
                    "body": f"최근 30일 평균 마진율이 이전 대비 {drop_pct}% 하락했습니다.",
                },
            )
        db.commit()


# ────────────────────────────────────────────────────────────────────────────
# 4. 정책 NEEDS_REVIEW 자동 갱신 (180일 초과)
# ────────────────────────────────────────────────────────────────────────────

_POLICY_TABLES = [
    "marketplace_fee_policy",
    "payment_fee_policy",
    "tax_policy",
    "fulfillment_fee_policy",
    "return_risk_policy",
    "target_margin_policy",
    "settlement_policy",
    "partner_share_policy",
]


def _policy_needs_review() -> None:
    cutoff = date.today() - timedelta(days=180)

    with SessionLocal() as db:
        total = 0
        for table in _POLICY_TABLES:
            result = db.execute(
                text(f"""
                    UPDATE {table}
                    SET reliability_status = 'NEEDS_REVIEW'
                    WHERE effective_from <= :cutoff
                      AND reliability_status = 'RELIABLE'
                      AND deleted_at IS NULL
                """),  # noqa: S608
                {"cutoff": cutoff},
            )
            total += result.rowcount
        db.commit()

    if total:
        _log.info("policy_needs_review: %d policies marked NEEDS_REVIEW", total)


# ────────────────────────────────────────────────────────────────────────────
# 5. 백업 검증 알림 (주 1회 — 실제 restore 는 수동)
# ────────────────────────────────────────────────────────────────────────────

def _backup_verify_remind() -> None:
    _log.info(
        "backup_verify_remind: 이번 주 백업 파일을 임시 컨테이너에서 복구 테스트해주세요. "
        "(docs/25_devops.md §4-2)"
    )
    with SessionLocal() as db:
        org_ids = db.execute(
            text("""
                SELECT id FROM organization
                WHERE deleted_at IS NULL
                LIMIT 1
            """),
        ).fetchall()

        for row in org_ids:
            db.execute(
                text("""
                    INSERT INTO notification (id, org_id, channel, title, body, is_read, created_at)
                    VALUES (gen_random_uuid(), :org_id, 'IN_APP',
                        :title, :body, false, now())
                """),
                {
                    "org_id": str(row.id),
                    "title": "주간 백업 검증 알림",
                    "body": "이번 주 DB 백업 파일을 임시 환경에서 복구 테스트해주세요. (docs/25_devops.md §4-2)",
                },
            )
        db.commit()


# ────────────────────────────────────────────────────────────────────────────
# 스케줄러 팩토리
# ────────────────────────────────────────────────────────────────────────────

def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=KST)

    scheduler.add_job(
        _settlement_close_notify,
        CronTrigger(hour=9, minute=0, timezone=KST),
        id="settlement_close_notify",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _vat_deadline_notify,
        CronTrigger(hour=9, minute=0, timezone=KST),
        id="vat_deadline_notify",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _margin_drop_detect,
        CronTrigger(hour=9, minute=0, timezone=KST),
        id="margin_drop_detect",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _policy_needs_review,
        CronTrigger(hour=3, minute=0, timezone=KST),
        id="policy_needs_review",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _backup_verify_remind,
        CronTrigger(day_of_week="sun", hour=4, minute=0, timezone=KST),
        id="backup_verify_remind",
        replace_existing=True,
        misfire_grace_time=300,
    )

    return scheduler
