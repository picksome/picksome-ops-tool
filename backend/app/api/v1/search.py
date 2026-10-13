"""통합 검색 라우터 — GET /search.

설계: docs/29_global_search.md

프론트 `components/shell/cmd-k-search.tsx` (⌘K 팔레트)가 이 라우트를 호출한다.
그동안 라우트가 없어서 팔레트가 정적 페이지 목록만 필터링하고 있었다.

응답 키는 프론트 기존 타입에 맞춘다 — 상품명은 `name_ko` 가 아니라 **`name`**.
"""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db
from app.services.search import (
    EXPENSE_COLUMNS,
    PRODUCT_COLUMNS,
    SALES_COLUMNS,
    build_match,
    build_rank,
    tokenize,
)

router = APIRouter(prefix="/search", tags=["search"])

_read = require_roles("ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY")


def _run(
    db: Session,
    *,
    org_id: str,
    select_sql: str,
    table: str,
    columns: list[str],
    rank_column: str,
    order_time: str,
    tokens: list[str],
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """검색 1종 실행 → (행 목록, limit 을 걷어낸 전체 건수).

    counts 를 따로 세는 이유: 팔레트에 "12건 중 5건" 을 보여주려면 limit 이
    잘라내기 전의 수가 필요하다.
    """
    where, params = build_match(tokens, columns)
    rank_sql, rank_params = build_rank(tokens, rank_column)
    params = {**params, **rank_params, "org": org_id, "limit": limit}

    total = db.execute(
        text(f"SELECT COUNT(*) FROM {table} WHERE org_id = :org AND deleted_at IS NULL AND ({where})"),
        params,
    ).scalar_one()

    rows = db.execute(
        text(
            f"""
            SELECT {select_sql}, {rank_sql} AS _rank
              FROM {table}
             WHERE org_id = :org AND deleted_at IS NULL AND ({where})
             ORDER BY _rank, {order_time} DESC, id DESC
             LIMIT :limit
            """
        ),
        params,
    ).mappings().all()

    out = []
    for r in rows:
        d = dict(r)
        d.pop("_rank", None)
        d["id"] = str(d["id"])
        out.append(d)
    return out, int(total)


@router.get("")
def search(
    q: str | None = Query(default=None, description="검색어. 2자 미만이면 빈 결과"),
    limit: int = Query(5, ge=1, le=20, description="종류별 최대 건수"),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    tokens = tokenize(q)
    if not tokens:
        # 2자 미만은 조회하지 않는다 — 한 글자로 전체를 긁으면 팔레트가 쓸모없어진다.
        return envelope(
            {"products": [], "sales": [], "expenses": [],
             "counts": {"products": 0, "sales": 0, "expenses": 0}}
        )

    org = str(user.org_id)

    products, n_products = _run(
        db, org_id=org,
        select_sql="id, business_key, sku, name_ko AS name, status, margin_type",
        table="product", columns=PRODUCT_COLUMNS,
        rank_column="name_ko", order_time="created_at",
        tokens=tokens, limit=limit,
    )
    sales, n_sales = _run(
        db, org_id=org,
        select_sql=(
            "id, business_key, sale_date, marketplace, "
            "product_snapshot_name AS name, sale_price"
        ),
        table="sales_order", columns=SALES_COLUMNS,
        rank_column="product_snapshot_name", order_time="sale_date",
        tokens=tokens, limit=limit,
    )
    expenses, n_expenses = _run(
        db, org_id=org,
        select_sql="id, expense_date, description AS name, gross_amount, memo",
        table="expenses", columns=EXPENSE_COLUMNS,
        rank_column="description", order_time="expense_date",
        tokens=tokens, limit=limit,
    )

    return envelope(
        {
            "products": products,
            "sales": sales,
            "expenses": expenses,
            "counts": {
                "products": n_products,
                "sales": n_sales,
                "expenses": n_expenses,
            },
        }
    )
