"""재고 대량 가져오기 (M050).

왜 필요한가
---------
상품이 1,000개가 넘는데 초기 재고를 화면에서 한 건씩 넣게 두면 아무도 재고
기능을 쓰지 않는다. 재고를 안 넣으면 재주문도 못 돌고, 결국 이 모든 것이
장식이 된다.

단계는 셋이다
-----------
```
ANALYZE   파일만 읽는다. DB 를 보지도 않는다
DRY_RUN   DB 와 대조해 무엇이 바뀔지 계산한다. 쓰지 않는다
APPLY     원장에 반영한다
```

`supplier_import.MODES` 와 같은 어휘를 쓴다. 가져오기마다 다른 단계 이름을
쓰면 사용자가 매번 새로 배워야 한다.

수량을 덮어쓰지 않는다
-------------------
Excel 에 `17` 이 있다고 잔량을 17 로 쓰지 않는다. **차이를 원장에 넣는다.**

```
원장에 기록 없음        → OPENING +17
원장 잔량 14, 실사 17   → ADJUSTMENT +3
원장 잔량 17, 실사 17   → UNCHANGED (아무것도 안 만든다)
```

덮어쓰면 "왜 17 인가" 에 답할 수 없고, 두 사람이 동시에 올렸을 때 한쪽이
조용히 사라진다.

같은 파일을 두 번 올려도 안전하다
-----------------------------
`source_id` 를 **(파일해시 + 상품 + 옵션 + 기준일)에서 결정적으로** 만든다.
`uq_inv_txn_source` 가 두 번째를 막는다.

실행 기록을 조회해서 판단하지 않는 이유: 두 사람이 동시에 올리면 둘 다
"없다" 를 보고 둘 다 넣는다. DB 제약만이 확실하다.

상품을 추측해서 맞추지 않는다
-------------------------
이름이 비슷하다고 같은 상품으로 보지 않는다. 잘못 맞추면 **엉뚱한 상품의
재고가 바뀌고**, 그 상태로 발주까지 나간다. 확실한 열쇠만 쓴다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import ValidationError
from app.services.supplier_import import (  # noqa: F401  (같은 어휘를 쓴다)
    MODES,
    ImportNotAllowedError,
    assert_import_allowed,
    sha256_of,
)

#: 상품을 찾는 열쇠. **순서가 우선순위다.**
#:
#: `sku` 가 먼저인 이유: 사용자가 실제로 쓰는 번호다. `business_key` 는 우리가
#: 만든 것이라 사용자 파일에 없을 가능성이 높다.
IDENTITY_COLUMNS = ("sku", "business_key", "product_id")

#: 파일에서 찾을 열 이름. 여러 표기를 받는다 — 사용자가 우리 표기법을 외울
#: 이유가 없다. 다만 **뜻이 다른 열을 같은 것으로 받지는 않는다.**
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "sku": ("sku", "상품코드", "품번", "제품코드"),
    "business_key": ("business_key", "상품번호", "관리번호"),
    "product_id": ("product_id", "상품id", "product uuid"),
    "variant_key": ("variant_key", "옵션코드", "옵션"),
    "quantity": ("quantity", "수량", "재고", "재고수량", "실사수량"),
    "as_of_date": ("as_of_date", "기준일", "실사일", "일자"),
    "memo": ("memo", "메모", "비고"),
}

#: 한 행의 처리 결과.
OUTCOMES = (
    "OPENING",      # 원장이 없던 상품의 최초 재고
    "ADJUSTMENT",   # 실사와 원장의 차이
    "UNCHANGED",    # 같다 — 원장에 아무것도 안 만든다
    "UNMATCHED",    # 상품을 못 찾았다
    "AMBIGUOUS",    # 어느 옵션인지 모른다
    "INVALID",      # 수량·날짜가 읽히지 않는다
    "DUPLICATE",    # 이미 같은 출처로 반영됐다 (재적용의 정상 결과)
)


@dataclass
class RowPlan:
    """한 행을 어떻게 처리할지. DRY_RUN 이 만들고 APPLY 가 실행한다."""

    line_no: int
    outcome: str
    identity: str | None = None
    product_id: UUID | None = None
    product_variant_id: UUID | None = None
    product_name: str | None = None
    counted: int | None = None
    current: int | None = None
    delta: int | None = None
    message: str | None = None
    memo: str | None = None


@dataclass
class ImportPlan:
    mode: str
    as_of_date: date | None = None
    rows: list[RowPlan] = field(default_factory=list)
    #: 파일 자체의 문제. 행이 아니라 파일 수준이다.
    file_problems: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in OUTCOMES}
        for r in self.rows:
            out[r.outcome] += 1
        return out

    def as_dict(self) -> dict[str, Any]:
        counts = self.counts()
        return {
            "mode": self.mode,
            "as_of_date": self.as_of_date.isoformat() if self.as_of_date else None,
            "rows_total": len(self.rows),
            "counts": counts,
            # 사람이 손봐야 하는 것만 앞에 모은다. 1,000행을 다 보여주면
            # 정작 봐야 할 25행이 묻힌다.
            "problems": [
                _row_dict(r) for r in self.rows
                if r.outcome in ("UNMATCHED", "AMBIGUOUS", "INVALID")
            ][:200],
            "changes": [
                _row_dict(r) for r in self.rows
                if r.outcome in ("OPENING", "ADJUSTMENT")
            ][:200],
            "file_problems": self.file_problems,
            "dry_run": self.mode != "APPLY",
        }


def _row_dict(r: RowPlan) -> dict[str, Any]:
    return {
        "line_no": r.line_no, "outcome": r.outcome, "identity": r.identity,
        "product_name": r.product_name, "counted": r.counted,
        "current": r.current, "delta": r.delta, "message": r.message,
    }


# ── 파일 읽기 ──────────────────────────────────────────────────


def _norm_header(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("_", "")


def _map_headers(header_row: tuple[Any, ...]) -> dict[str, int]:
    """열 이름 → 열 번호.

    모르는 열은 **조용히 무시한다.** 사용자 파일에는 우리가 안 쓰는 열이
    당연히 있다. 다만 필요한 열이 없으면 아래에서 파일 문제로 보고한다.
    """
    lookup: dict[str, str] = {}
    for field_name, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            lookup[_norm_header(alias)] = field_name

    out: dict[str, int] = {}
    for idx, cell in enumerate(header_row):
        field_name = lookup.get(_norm_header(cell))
        # 같은 뜻의 열이 두 번 나오면 **앞의 것**을 쓴다. 뒤엣것으로 덮으면
        # 어느 열이 쓰였는지 사용자가 알 수 없다.
        if field_name and field_name not in out:
            out[field_name] = idx
    return out


def _parse_quantity(value: Any) -> tuple[int | None, str | None]:
    """수량. **0 은 유효한 값이다** — "실제로 0개" 를 말할 수 있어야 한다.

    음수도 거부하지 않는다. 창고가 실제로 마이너스일 수는 없지만, 장부를
    맞추려고 음수를 넣는 상황(먼저 판 것을 나중에 입고)이 실제로 있다.
    음수 재고는 예외 센터가 따로 잡는다.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, "수량이 비어 있습니다"
    try:
        # Excel 은 정수를 float 로 준다. 12.0 은 받고 12.5 는 거부한다 —
        # 재고에 소수점 개수는 없다.
        number = float(str(value).replace(",", "").strip())
    except ValueError:
        return None, f"수량을 숫자로 읽을 수 없습니다: {value!r}"
    if number != int(number):
        return None, f"수량이 정수가 아닙니다: {value!r}"
    return int(number), None


def _parse_date(value: Any) -> tuple[date | None, str | None]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if isinstance(value, date):
        return value, None
    try:
        return date.fromisoformat(str(value).strip()[:10]), None
    except ValueError:
        return None, f"날짜를 읽을 수 없습니다: {value!r}"


# ── 계획 세우기 ────────────────────────────────────────────────


def plan(
    db: Session, *, org_id: UUID, rows: list[tuple[Any, ...]], mode: str,
    as_of_date: date | None = None,
    source_sha256: str | None = None,
) -> ImportPlan:
    """파일을 읽고 무엇이 일어날지 계산한다. **아무것도 쓰지 않는다.**

    `ANALYZE` 는 DB 를 보지 않는다 — 파일만 검사한다. `DRY_RUN` 은 DB 와
    대조해 OPENING/ADJUSTMENT/UNCHANGED 를 가른다.
    """
    if mode not in MODES:
        raise ValidationError(f"알 수 없는 단계입니다: {mode}")

    result = ImportPlan(mode=mode, as_of_date=as_of_date)
    if not rows:
        result.file_problems.append("파일이 비어 있습니다.")
        return result

    headers = _map_headers(rows[0])
    if "quantity" not in headers:
        result.file_problems.append(
            "수량 열을 찾지 못했습니다. 머리글에 '수량' 또는 'quantity' 가 "
            "있어야 합니다.")
        return result
    if not any(k in headers for k in IDENTITY_COLUMNS):
        result.file_problems.append(
            "상품을 찾을 열이 없습니다. 'SKU'·'상품번호'·'product_id' 중 "
            "하나가 필요합니다.")
        return result

    def cell(row: tuple[Any, ...], key: str) -> Any:
        idx = headers.get(key)
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    for line_no, row in enumerate(rows[1:], start=2):
        if all(c is None or str(c).strip() == "" for c in row):
            continue  # 빈 줄은 세지 않는다
        result.rows.append(
            _plan_row(db, org_id=org_id, line_no=line_no, row=row, cell=cell,
                      mode=mode, default_date=as_of_date,
                      source_sha256=source_sha256))
    return result


def _plan_row(
    db: Session, *, org_id: UUID, line_no: int, row: tuple[Any, ...],
    cell: Any, mode: str, default_date: date | None,
    source_sha256: str | None,
) -> RowPlan:
    quantity, qty_error = _parse_quantity(cell(row, "quantity"))
    row_date, date_error = _parse_date(cell(row, "as_of_date"))
    memo = cell(row, "memo")
    memo = str(memo).strip() if memo is not None and str(memo).strip() else None

    identity_value = None
    for key in IDENTITY_COLUMNS:
        raw = cell(row, key)
        if raw is not None and str(raw).strip():
            identity_value = f"{key}={str(raw).strip()}"
            break

    if qty_error or date_error:
        return RowPlan(line_no=line_no, outcome="INVALID",
                       identity=identity_value,
                       message=qty_error or date_error)

    if mode == "ANALYZE":
        # 파일만 본다. 상품을 찾지도, 잔량을 읽지도 않는다.
        return RowPlan(line_no=line_no, outcome="UNCHANGED",
                       identity=identity_value, counted=quantity, memo=memo)

    match = _find_product(db, org_id=org_id, row=row, cell=cell)
    if match is None:
        return RowPlan(line_no=line_no, outcome="UNMATCHED",
                       identity=identity_value, counted=quantity,
                       message="이 열쇠로 상품을 찾지 못했습니다.")
    if match == "AMBIGUOUS":
        return RowPlan(line_no=line_no, outcome="AMBIGUOUS",
                       identity=identity_value, counted=quantity,
                       message="옵션이 여러 개라 어느 옵션인지 알 수 없습니다. "
                               "옵션코드를 함께 넣어 주세요.")

    product_id, variant_id, name = match
    effective_date = row_date or default_date

    # 같은 출처로 이미 반영됐나. 재적용의 정상 결과다.
    if source_sha256:
        source_id = _source_id(source_sha256, product_id, variant_id,
                               effective_date)
        seen = db.execute(text("""
            SELECT 1 FROM inventory_transaction
             WHERE org_id = :o AND source_table = 'inventory_import'
               AND source_id = :s
        """), {"o": str(org_id), "s": str(source_id)}).scalar()
        if seen:
            return RowPlan(line_no=line_no, outcome="DUPLICATE",
                           identity=identity_value, product_id=product_id,
                           product_variant_id=variant_id, product_name=name,
                           counted=quantity,
                           message="이 파일로 이미 반영된 행입니다.")

    current = db.execute(text("""
        SELECT COALESCE(SUM(quantity_delta), 0)::int AS on_hand,
               COUNT(*)::int AS movements
          FROM inventory_transaction
         WHERE org_id = :o AND product_id = :p
           AND product_variant_id IS NOT DISTINCT FROM CAST(:v AS uuid)
    """), {"o": str(org_id), "p": str(product_id),
           "v": str(variant_id) if variant_id else None}).mappings().one()

    common = {
        "line_no": line_no, "identity": identity_value,
        "product_id": product_id, "product_variant_id": variant_id,
        "product_name": name, "counted": quantity, "memo": memo,
    }

    if current["movements"] == 0:
        # 원장이 없다 → 최초 재고. 0 이라도 "0으로 시작" 을 기록할 가치가 있다.
        return RowPlan(outcome="OPENING", current=None, delta=quantity, **common)

    delta = quantity - current["on_hand"]
    if delta == 0:
        # 같으면 아무것도 만들지 않는다. 원장에 의미 없는 줄이 쌓이면
        # 진짜 움직임을 찾기 어려워진다.
        return RowPlan(outcome="UNCHANGED", current=current["on_hand"],
                       delta=0, **common)
    return RowPlan(outcome="ADJUSTMENT", current=current["on_hand"],
                   delta=delta, **common)


def _find_product(
    db: Session, *, org_id: UUID, row: tuple[Any, ...], cell: Any,
) -> tuple[UUID, UUID | None, str] | str | None:
    """상품(과 옵션)을 찾는다. **추측하지 않는다.**

    반환:
      `(product_id, variant_id, name)`  찾음
      `"AMBIGUOUS"`                     옵션을 특정할 수 없음
      `None`                            없음
    """
    product = None
    for key in IDENTITY_COLUMNS:
        raw = cell(row, key)
        if raw is None or not str(raw).strip():
            continue
        value = str(raw).strip()
        column = "id" if key == "product_id" else key
        try:
            product = db.execute(text(f"""
                SELECT id, name_ko, has_variants FROM product
                 WHERE org_id = :o AND deleted_at IS NULL
                   AND {column} = CAST(:v AS {'uuid' if column == 'id' else 'text'})
            """), {"o": str(org_id), "v": value}).mappings().first()
        except Exception:
            # UUID 열에 UUID 아닌 값이 오면 여기로 온다. 다음 열쇠를 시도한다.
            db.rollback()
            continue
        if product is not None:
            break

    if product is None:
        return None

    variant_raw = cell(row, "variant_key")
    variant_key = str(variant_raw).strip() if variant_raw is not None and str(
        variant_raw).strip() else None

    if variant_key:
        variant = db.execute(text("""
            SELECT id FROM product_variant
             WHERE org_id = :o AND product_id = :p AND deleted_at IS NULL
               AND (variant_key = :k OR sku_external = :k)
        """), {"o": str(org_id), "p": str(product["id"]),
               "k": variant_key}).scalar()
        if variant is None:
            return None
        return UUID(str(product["id"])), UUID(str(variant)), product["name_ko"]

    # 옵션코드가 없는 행. 옵션이 있는 상품이면 어느 옵션인지 알 수 없다 —
    # 임의로 하나를 고르면 엉뚱한 옵션의 재고가 바뀐다.
    variant_count = db.execute(text("""
        SELECT COUNT(*) FROM product_variant
         WHERE org_id = :o AND product_id = :p AND deleted_at IS NULL
    """), {"o": str(org_id), "p": str(product["id"])}).scalar_one()
    if variant_count > 0:
        return "AMBIGUOUS"
    return UUID(str(product["id"])), None, product["name_ko"]


def _source_id(
    sha256: str, product_id: UUID, variant_id: UUID | None,
    as_of: date | None,
) -> UUID:
    """원장 출처 id. **같은 입력이면 항상 같은 값**이다.

    이게 재적용 안전장치의 전부다. `uq_inv_txn_source` 가 같은 값을 두 번
    넣지 못하게 한다.

    기준일을 섞는 이유: 같은 파일 형식으로 **다른 날** 실사한 결과를 올리는
    것은 정상이고, 그건 새 원장 줄이어야 한다.
    """
    seed = f"{sha256}:{product_id}:{variant_id or ''}:{as_of or ''}"
    return UUID(bytes=hashlib.sha256(seed.encode()).digest()[:16], version=4)


# ── 반영 ───────────────────────────────────────────────────────


def apply_plan(
    db: Session, *, org_id: UUID, actor_user_id: UUID, plan_result: ImportPlan,
    source_name: str, source_sha256: str,
) -> dict[str, Any]:
    """계획을 원장에 반영한다.

    `OPENING` 과 `ADJUSTMENT` 만 쓴다. 나머지는 손대지 않는다 — 못 찾은 행을
    "일단 새 상품으로" 만들지 않는다.
    """
    assert_import_allowed("APPLY")
    from app.core.audit import record_audit
    from app.services import inventory

    run_id = _start_run(
        db, org_id=org_id, actor_user_id=actor_user_id, mode="APPLY",
        source_name=source_name, source_sha256=source_sha256,
        plan_result=plan_result)

    applied = 0
    for r in plan_result.rows:
        if r.outcome not in ("OPENING", "ADJUSTMENT"):
            continue
        assert r.product_id is not None and r.delta is not None
        if r.delta == 0:
            continue
        inventory.record_transaction(
            db, org_id=org_id, product_id=r.product_id,
            product_variant_id=r.product_variant_id,
            transaction_type=("OPENING" if r.outcome == "OPENING"
                              else "ADJUSTMENT"),
            quantity_delta=r.delta, actor_user_id=actor_user_id,
            # 조정은 사유가 필수다. 어느 파일에서 왔는지가 곧 사유다.
            reason=r.memo or f"재고 가져오기 ({source_name})",
            source_table="inventory_import",
            source_id=_source_id(source_sha256, r.product_id,
                                 r.product_variant_id, plan_result.as_of_date),
        )
        applied += 1

    # **행마다 audit 를 남기지 않는다.** 1,000행이면 audit_log 가 1,000줄
    # 늘어나고 정작 사람이 한 변경이 묻힌다. 원장 자체가 행 단위 추적성을
    # 이미 갖고 있으므로(누가·언제·무엇을·왜), audit 는 **실행 단위**로 남긴다.
    record_audit(
        db, org_id=org_id, domain_table="inventory_import_run",
        domain_row_id=run_id, action="CREATE", actor_user_id=actor_user_id,
        after={"applied": applied, **plan_result.counts()},
        reason=f"재고 가져오기 반영 ({source_name})",
    )
    db.commit()

    _finish_run(db, run_id=run_id)
    return {
        "import_run_id": str(run_id),
        "applied": applied,
        **plan_result.as_dict(),
    }


def _start_run(
    db: Session, *, org_id: UUID, actor_user_id: UUID | None, mode: str,
    source_name: str, source_sha256: str, plan_result: ImportPlan,
) -> UUID:
    from app.config import get_settings

    counts = plan_result.counts()
    row = db.execute(text("""
        INSERT INTO inventory_import_run
               (org_id, source_name, source_sha256, mode, app_env, as_of_date,
                rows_total, opening, adjustment, unchanged, unmatched,
                ambiguous, invalid, duplicate, actor_user_id)
        VALUES (:o, :src, :sha, :mode, :env, :asof,
                :total, :op, :adj, :un, :um, :amb, :inv, :dup, :actor)
        RETURNING id
    """), {
        "o": str(org_id), "src": source_name[:300], "sha": source_sha256,
        "mode": mode,
        "env": str(getattr(get_settings(), "APP_ENV", "unknown")),
        "asof": plan_result.as_of_date,
        "total": len(plan_result.rows),
        "op": counts["OPENING"], "adj": counts["ADJUSTMENT"],
        "un": counts["UNCHANGED"], "um": counts["UNMATCHED"],
        "amb": counts["AMBIGUOUS"], "inv": counts["INVALID"],
        "dup": counts["DUPLICATE"],
        "actor": str(actor_user_id) if actor_user_id else None,
    }).scalar_one()
    return UUID(str(row))


def _finish_run(db: Session, *, run_id: UUID) -> None:
    db.execute(text("""
        UPDATE inventory_import_run SET finished_at = now() WHERE id = :id
    """), {"id": str(run_id)})
    db.commit()


def list_runs(
    db: Session, *, org_id: UUID, limit: int = 20,
) -> list[dict[str, Any]]:
    rows = db.execute(text("""
        SELECT r.id, r.source_name, r.mode, r.as_of_date, r.rows_total,
               r.opening, r.adjustment, r.unchanged, r.unmatched, r.ambiguous,
               r.invalid, r.duplicate, r.started_at, r.finished_at,
               u.display_name AS actor_name
          FROM inventory_import_run r
          LEFT JOIN app_user u ON u.id = r.actor_user_id
         WHERE r.org_id = :o
         ORDER BY r.started_at DESC
         LIMIT :lim
    """), {"o": str(org_id), "lim": limit}).mappings().all()
    return [
        {
            "id": str(r["id"]), "source_name": r["source_name"],
            "mode": r["mode"],
            "as_of_date": r["as_of_date"].isoformat() if r["as_of_date"] else None,
            "rows_total": r["rows_total"], "opening": r["opening"],
            "adjustment": r["adjustment"], "unchanged": r["unchanged"],
            "unmatched": r["unmatched"], "ambiguous": r["ambiguous"],
            "invalid": r["invalid"], "duplicate": r["duplicate"],
            "started_at": r["started_at"].isoformat(),
            "finished_at": (
                r["finished_at"].isoformat() if r["finished_at"] else None
            ),
            "actor_name": r["actor_name"] or "시스템",
        }
        for r in rows
    ]
