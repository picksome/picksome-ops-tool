"""정산서 파일 적재 (docs/82).

파일 → 정산 행 → 주문 매칭. **여기까지다.**

이 서비스가 하지 않는 것
--------------------
```
순이익 반영          ✗   조정을 순이익에 접을지는 아직 정해지지 않았다
파트너 배분 반영      ✗
현금흐름 입금 생성     ✗   정산예정일은 입금 증거가 아니다
판매/원가 수정        ✗   정산서를 읽었다고 장부의 판매를 고치지 않는다
월 마감 재계산        ✗
```

읽은 것을 그대로 사실로 남기고, 어느 주문에 붙는지만 판정한다.

멱등성은 파일 해시가 아니라 **행 식별자**가 지킨다
--------------------------------------------
같은 기간 정산서를 다시 내려받으면 파일 해시가 달라질 수 있다(정렬·생성
시각). 해시로만 막으면 그때 같은 행이 두 번 들어간다.

`import_idempotency_key`(M055 UNIQUE)가 진짜 방어선이다. 파일 해시는
"이 행이 어느 파일에서 왔나" 를 남기는 provenance 일 뿐이다.

원본 행을 통째로 저장하지 않는다
---------------------------
```
file hash · sheet · 원본 행 번호 · 행 지문     남긴다
Excel row 전체 JSON                          남기지 않는다
구매자명(개인정보)                             파서가 아예 읽지 않는다
```

지문을 남기는 이유는 재발행 파일에서 **금액이 바뀌었는지** 보기 위해서다.
바뀌었다면 덮어쓰지 않고 사람에게 보인다 — 그건 새 사실이고, 정정은
`settlement_adjustment` 의 일이다(docs/47 §6-3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.exceptions import NotFoundError, ValidationError
from app.io.settlement_import import coupang_msf as msf
from app.services import settlement_matching as sm

DRY_RUN = "DRY_RUN"
COMMIT = "COMMIT"

#: 이 서비스가 다루는 판매처. 파일 형식이 늘면 파서를 늘리고 여기에 등록한다.
MARKETPLACE = "COUPANG"


@dataclass(slots=True)
class ImportSummary:
    mode: str
    source_name: str
    source_sha256: str
    sheet_name: str = ""
    source_rows: int = 0
    parsed: int = 0
    rejected: int = 0
    #: 이번에 새로 적재된 행
    inserted: int = 0
    #: 이미 있던 행 (같은 식별자) — 재실행에서 여기로 온다
    unchanged: int = 0
    #: 이미 있는데 **내용이 다른** 행. 덮어쓰지 않는다.
    changed_not_applied: int = 0
    matched_exact: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    excluded: int = 0
    no_order_no: int = 0
    relationship_mismatches: int = 0
    refund_like_rows: int = 0
    unknown_headers: list[str] = field(default_factory=list)
    due_dates: list[str] = field(default_factory=list)
    period_start: str | None = None
    period_end: str | None = None
    batch_run_id: UUID | None = None
    rejects: list[dict[str, Any]] = field(default_factory=list)

    @property
    def persisted(self) -> int:
        return self.inserted + self.unchanged + self.changed_not_applied

    def reconciles(self) -> bool:
        """**한 행도 사라지지 않았는가.**

        ```
        원본 행 = 적재된 행(신규+기존+내용변경) + 파서가 거부한 행
        ```
        """
        return self.source_rows == self.persisted + self.rejected

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "source_name": self.source_name,
            "source_sha256": self.source_sha256, "sheet_name": self.sheet_name,
            "source_rows": self.source_rows, "parsed": self.parsed,
            "rejected": self.rejected, "inserted": self.inserted,
            "unchanged": self.unchanged,
            "changed_not_applied": self.changed_not_applied,
            "matched_exact": self.matched_exact, "ambiguous": self.ambiguous,
            "unmatched": self.unmatched, "excluded": self.excluded,
            "no_order_no": self.no_order_no,
            "relationship_mismatches": self.relationship_mismatches,
            "refund_like_rows": self.refund_like_rows,
            "unknown_headers": self.unknown_headers,
            "due_dates": self.due_dates,
            "period_start": self.period_start, "period_end": self.period_end,
            "persisted": self.persisted, "reconciles": self.reconciles(),
            "rejects": self.rejects[:50],
        }


def _resolve_account(db: Session, *, org_id: UUID, account_id: UUID) -> dict[str, Any]:
    """판매계정 확인. **파일만 보고 고르지 않는다** — 호출자가 지정한다."""
    row = db.execute(text("""
        SELECT ia.id, ia.account_label, sc.code AS channel_code
          FROM integration_account ia
          LEFT JOIN sale_channel sc ON sc.id = ia.sale_channel_id
         WHERE ia.id = :id AND ia.org_id = :org AND ia.deleted_at IS NULL
    """), {"id": str(account_id), "org": str(org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("판매계정을 찾을 수 없습니다.")
    code = (row["channel_code"] or "").upper()
    if code and code != MARKETPLACE:
        # 쿠팡 정산서를 네이버 계정에 붙이면 스토어별 손익이 통째로 틀어진다.
        # 마감 검증도 같은 상황을 BLOCKING 으로 잡는다.
        raise ValidationError(
            f"쿠팡 정산서는 쿠팡 판매계정에만 넣을 수 있습니다 (선택한 계정: {code}).",
            detail={"account_channel": code, "expected": MARKETPLACE},
        )
    return dict(row)


def _period_of(rows: list[msf.ParsedRow]) -> tuple[date, date]:
    """이 정산이 **포괄하는 기간**.

    결제완료일의 최소~최대를 쓴다. 정산예정일이 아니다 — 예정일은 한 점이고
    `expected_settlement_date` 에 따로 들어간다(M056).

    배송료 행은 날짜가 비어 있는 경우가 많아(실측 19/22) 날짜가 있는 행만
    본다. 하나도 없으면 정산예정일로 되돌아간다 — 그때는 그게 우리가 아는
    유일한 날짜다.
    """
    paid = [r.paid_at for r in rows if r.paid_at]
    if paid:
        return min(paid), max(paid)
    dues = [r.expected_settlement_date for r in rows if r.expected_settlement_date]
    if dues:
        return min(dues), max(dues)
    raise ValidationError("정산 기간을 정할 날짜가 파일에 없습니다.")


def import_file(
    db: Session, *, org_id: UUID, integration_account_id: UUID,
    path: str | Path, mode: str = DRY_RUN, actor_user_id: UUID | None = None,
) -> ImportSummary:
    """경로에서 읽어 적재한다. 운영자 스크립트용 얇은 껍데기다."""
    src = Path(path)
    if not src.is_file():
        raise ValidationError(f"파일을 찾을 수 없습니다: {src.name}")
    return import_bytes(
        db, org_id=org_id, integration_account_id=integration_account_id,
        source_name=src.name, raw=src.read_bytes(), mode=mode,
        actor_user_id=actor_user_id,
    )


def import_bytes(
    db: Session, *, org_id: UUID, integration_account_id: UUID,
    source_name: str, raw: bytes, mode: str = DRY_RUN,
    actor_user_id: UUID | None = None,
) -> ImportSummary:
    """정산서 1개를 읽어 적재한다.

    `mode=DRY_RUN` 이면 **업무 데이터를 한 줄도 쓰지 않는다.** 세어서 보여
    주기만 한다 — 실제 파일을 처음 넣을 때 무엇이 들어갈지 먼저 본다.

    **업로드 내용을 디스크에 쓰지 않는다.** 메모리에서 읽고 버리고, 해시와
    행 지문만 남는다.

    커밋하지 않는다. 호출자가 정한다.
    """
    if mode not in (DRY_RUN, COMMIT):
        raise ValidationError(f"알 수 없는 실행 모드입니다: {mode}")
    if not raw:
        raise ValidationError("파일이 비어 있습니다.")

    account = _resolve_account(db, org_id=org_id, account_id=integration_account_id)
    sha = msf.bytes_sha256(raw)
    try:
        parsed = msf.parse_workbook(raw)
    except msf.SettlementParseError as exc:
        # 파서는 io 계층이라 도메인 예외를 모른다. 여기서 옮겨야 화면이
        # 500 이 아니라 **읽을 수 있는 거절 사유**를 받는다.
        raise ValidationError(str(exc), detail={"source_name": source_name}) from exc

    summary = ImportSummary(
        mode=mode, source_name=source_name, source_sha256=sha,
        sheet_name=parsed.sheet_name,
        source_rows=parsed.source_rows, parsed=len(parsed.rows),
        rejected=len(parsed.rejected),
        relationship_mismatches=parsed.relationship_mismatches,
        refund_like_rows=sum(1 for r in parsed.rows if r.is_refund_like),
        unknown_headers=list(parsed.unknown_headers),
        due_dates=[d.isoformat() for d in parsed.due_dates],
        rejects=[{"row": r.source_row_no, "reason": r.reason, "detail": r.detail}
                 for r in parsed.rejected],
    )
    if not parsed.rows:
        return summary

    period_start, period_end = _period_of(parsed.rows)
    summary.period_start = period_start.isoformat()
    summary.period_end = period_end.isoformat()

    # ── 이미 있는 행 ────────────────────────────────────────────────
    keys = [msf.idempotency_key(due_date=r.expected_settlement_date,
                                order_no=r.order_no,
                                line_reference=r.line_reference)
            for r in parsed.rows]
    existing = {
        row["import_idempotency_key"]: row
        for row in db.execute(text("""
            SELECT import_idempotency_key, id, source_row_fingerprint
              FROM marketplace_settlement_record
             WHERE org_id = :org AND UPPER(marketplace) = UPPER(:mkt)
               AND deleted_at IS NULL
               AND import_idempotency_key = ANY(:keys)
        """), {"org": str(org_id), "mkt": MARKETPLACE, "keys": keys}).mappings().all()
    }

    # ── 후보 주문 (한 번에) ─────────────────────────────────────────
    index = sm.load_candidate_index(
        db, org_id=org_id, marketplace=MARKETPLACE,
        order_nos=[r.order_no for r in parsed.rows],
        integration_account_id=integration_account_id,
    )

    batch_id: UUID | None = None
    if mode == COMMIT:
        batch_id = db.execute(text("""
            INSERT INTO batch_run (org_id, status, summary_json, created_by)
            VALUES (:org, 'RUNNING', CAST(:s AS JSONB), CAST(:actor AS UUID))
            RETURNING id
        """), {
            "org": str(org_id),
            "s": _json({"source_name": source_name, "source_sha256": sha,
                        "sheet": parsed.sheet_name, "kind": msf.SOURCE_DOCUMENT}),
            "actor": str(actor_user_id) if actor_user_id else None,
        }).scalar_one()
        summary.batch_run_id = UUID(str(batch_id))

    for row, key in zip(parsed.rows, keys, strict=True):
        candidates = index.get(row.order_no, [])
        decision = sm.classify(UUID(int=0), candidates)

        prior = existing.get(key)
        if prior is not None:
            # **덮어쓰지 않는다.** 금액이 달라졌으면 새 사실이고, 사람이
            # 보고 조정으로 처리해야 한다(docs/47 §6-3).
            if prior["source_row_fingerprint"] != row.row_fingerprint:
                summary.changed_not_applied += 1
            else:
                summary.unchanged += 1
        else:
            summary.inserted += 1
            if mode == COMMIT:
                _insert(db, org_id=org_id, account_id=integration_account_id,
                        row=row, key=key, decision=decision, sha=sha,
                        period=(period_start, period_end), batch_id=batch_id,
                        actor_user_id=actor_user_id)

        # 집계는 dry-run 에서도 실제 판정 규칙으로 센다 — 그래야 미리 보는
        # 숫자와 실제 적재 결과가 같다.
        if decision.status == sm.MATCHED_EXACT:
            summary.matched_exact += 1
        elif decision.status == sm.AMBIGUOUS:
            summary.ambiguous += 1
        else:
            summary.unmatched += 1
        if decision.no_order_no:
            summary.no_order_no += 1

    if mode == COMMIT and summary.inserted:
        # 실 마진을 여기서 다시 계산하지 않는다. 매칭된 주문의 매출·수수료가
        # 정산 실측으로 바뀌지만(docs/83 D1~D3), import 가 과거 마진을 조용히
        # 덮으면 어느 달 숫자가 언제 왜 달라졌는지 아무도 모른다.
        #
        # 대신 **그 달을 "재계산 필요" 로 표시**한다 — 사업비용 승인이 쓰는
        # 것과 같은 경로다. 마감 검증이 `PERIOD_RECALC_PENDING` 으로 막고,
        # 사람이 월별 정산에서 재계산을 실행한다.
        _mark_affected_periods(
            db, org_id=org_id, source_name=source_name,
            actor_user_id=actor_user_id, batch_id=batch_id)

    if mode == COMMIT:
        db.execute(text("""
            UPDATE batch_run
               SET status = :st, finished_at = now(), affected_rows = :n,
                   summary_json = CAST(:s AS JSONB)
             WHERE id = :id
        """), {
            "id": str(batch_id),
            "st": "SUCCESS" if summary.reconciles() and not summary.rejected
                  else "PARTIAL",
            "n": summary.inserted,
            # `kind` 를 유지한다. 이력 조회가 이 키로 정산 배치를 고른다 —
            # 덮어쓰면서 빠뜨리면 방금 만든 실행이 목록에서 사라진다.
            "s": _json({**summary.as_dict(), "kind": msf.SOURCE_DOCUMENT}),
        })
        record_audit(
            db, org_id=org_id, domain_table="batch_run",
            domain_row_id=UUID(str(batch_id)), action="CREATE",
            actor_user_id=actor_user_id,
            after={"source_name": source_name, "source_sha256": sha,
                   "inserted": summary.inserted, "unchanged": summary.unchanged,
                   "account": account["account_label"]},
            reason="쿠팡 정산서 가져오기",
        )
    return summary


def _insert(
    db: Session, *, org_id: UUID, account_id: UUID, row: msf.ParsedRow,
    key: str, decision: sm.MatchDecision, sha: str,
    period: tuple[date, date], batch_id: UUID | None, actor_user_id: UUID | None,
) -> None:
    matched = decision.sales_order_id if decision.status == sm.MATCHED_EXACT else None
    db.execute(text("""
        INSERT INTO marketplace_settlement_record (
          org_id, marketplace, settlement_period_start, settlement_period_end,
          marketplace_order_no, settled_at,
          gross_revenue, marketplace_fee, ad_cost, return_cost,
          fulfillment_fee, other_deductions, net_settlement,
          matched_sales_order_id, matching_status, matched_at,
          import_batch_id, import_file_hash, import_idempotency_key,
          source_row_fingerprint, integration_account_id,
          row_type, line_reference, expected_settlement_date,
          created_by, updated_by
        ) VALUES (
          :org, :mkt, :ps, :pe,
          :ono, NULL,
          :gross, :fee, 0, 0,
          0, :other, :net,
          CAST(:soid AS UUID), :status,
          CASE WHEN CAST(:soid AS UUID) IS NULL THEN NULL ELSE now() END,
          CAST(:batch AS UUID), :hash, :key,
          :fp, :account,
          :rtype, :lref, :due,
          CAST(:actor AS UUID), CAST(:actor AS UUID)
        )
    """), {
        "org": str(org_id), "mkt": MARKETPLACE,
        "ps": period[0], "pe": period[1],
        "ono": row.order_no,
        "gross": row.gross_revenue, "fee": row.marketplace_fee,
        # 쿠폰이 없는 행(배송료)은 파서에서 None 이다. 컬럼이 NOT NULL 이라
        # 0 을 넣는다 — "쿠폰 공제가 없다" 는 사실이고, 모르는 값을 0 으로
        # 바꾸는 것과 다르다. 원본의 NULL/0 구분은 지문에 남아 있다.
        "other": row.seller_coupon or 0,
        "net": row.net_settlement,
        "soid": str(matched) if matched else None,
        "status": decision.status,
        "batch": str(batch_id) if batch_id else None,
        "hash": sha, "key": key, "fp": row.row_fingerprint,
        "account": str(account_id),
        "rtype": row.row_type, "lref": row.line_reference,
        # 예정일이지 정산일이 아니다. `settled_at` 은 NULL 로 남는다.
        "due": row.expected_settlement_date,
        "actor": str(actor_user_id) if actor_user_id else None,
    })


def _mark_affected_periods(
    db: Session, *, org_id: UUID, source_name: str, actor_user_id: UUID | None,
    batch_id: UUID | None = None,
) -> list[str]:
    """이번 적재로 실 마진이 달라지는 달을 "재계산 필요" 로 표시한다.

    기준은 **판매일**이다. 정산 회차가 아니다 — 실 마진은 `sales_order` 에
    붙고 그 주문이 속한 달의 손익이 달라진다. 2026-01 회차 정산서가 2025-11
    판매를 담고 있으면 흔들리는 것은 2025-11 이다.

    **이번 배치가 넣은 행만 본다.** 매칭된 행 전체를 보면 이미 재계산을
    끝낸 달까지 다시 dirty 로 만들고, 그러면 "재계산 필요" 가 영원히 안
    꺼져서 마감이 계속 막힌다.

    잠긴 달은 `mark_period_dirty` 가 알아서 건너뛴다 — 확정한 숫자를 다시
    계산하겠다고 표시해 두면 언젠가 배치가 그걸 실행한다.
    """
    from app.services.period_recalc import mark_period_dirty

    dates = db.execute(text("""
        SELECT DISTINCT so.sale_date
          FROM marketplace_settlement_record m
          JOIN sales_order so ON so.id = m.matched_sales_order_id
         WHERE m.org_id = :org AND m.deleted_at IS NULL
           AND so.deleted_at IS NULL
           AND m.matching_status IN ('MATCHED_EXACT', 'MATCHED_MANUAL')
           AND (CAST(:batch AS UUID) IS NULL OR m.import_batch_id = CAST(:batch AS UUID))
    """), {"org": str(org_id),
           "batch": str(batch_id) if batch_id else None}).scalars().all()

    marked: list[str] = []
    for d in dates:
        ym = mark_period_dirty(
            db, org_id=org_id, at=d,
            reason=f"정산서 가져오기({source_name}) — 실 마진 재계산 필요",
            actor=actor_user_id,
        )
        if ym and ym not in marked:
            marked.append(ym)
    return sorted(marked)


def list_runs(db: Session, *, org_id: UUID, limit: int = 20) -> list[dict[str, Any]]:
    """지난 가져오기 이력.

    `batch_run` 을 그대로 읽는다 — 정산 전용 실행 테이블을 새로 만들지 않았다.
    `marketplace_settlement_record.import_batch_id` 가 이미 이 테이블을
    가리키고 있었다(M001).
    """
    rows = db.execute(text("""
        SELECT b.id, b.started_at, b.finished_at, b.status, b.affected_rows,
               b.summary_json,
               (SELECT COUNT(*) FROM marketplace_settlement_record m
                 WHERE m.import_batch_id = b.id AND m.deleted_at IS NULL) AS record_count
          FROM batch_run b
         WHERE b.org_id = :org AND b.deleted_at IS NULL
           AND b.summary_json->>'kind' = :kind
         ORDER BY b.started_at DESC
         LIMIT :lim
    """), {"org": str(org_id), "kind": msf.SOURCE_DOCUMENT, "lim": limit}).mappings().all()
    return [dict(r) for r in rows]


def _json(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, default=str)
