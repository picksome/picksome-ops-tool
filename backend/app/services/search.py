"""통합 검색 — 상품 / 판매 / 사업비용을 한 질의로 찾는다.

설계: docs/29_global_search.md

왜 별도 모듈인가:
  검색창이 두 곳이다(상품 목록 필터의 `q`, ⌘K 팔레트). 매칭 규칙이 갈리면
  "목록에선 나오는데 ⌘K 에선 안 나온다" 같은 혼란이 생긴다. 규칙을 여기 한 곳에
  모아 두고 양쪽이 같은 헬퍼를 쓴다.
  나중에 pg_trgm + GIN 으로 갈아탈 때도 이 파일만 바꾸면 되게 하려는 의도.

매칭 규칙:
  - 공백으로 토큰을 나눠 **AND**. `쿨링 스카프` → `쿨링` AND `스카프`.
    각 토큰은 대상 컬럼 중 아무 데나 걸리면 된다(컬럼끼리는 OR).
    엑셀에서 온 상품명이 `국산 쿨링 스카프 쿨스카프 얼음스카프` 처럼 길어서,
    단일 토큰 `%q%` 로는 띄어쓰기가 섞인 질의가 통째로 실패한다.
  - `ILIKE` (대소문자 무시), 2자 미만 질의는 빈 결과.
  - LIKE 와일드카드(`%` `_`)와 이스케이프 문자(`\`)는 반드시 이스케이프한다.
    안 하면 `%` 한 글자가 전체 매칭이 되어 무의미한 결과가 쏟아진다.
"""

from __future__ import annotations

from typing import Any

MIN_QUERY_LEN = 2
MAX_TOKENS = 8  # 질의 폭주 방지 — 토큰 수만큼 AND 절이 늘어난다


def escape_like(raw: str) -> str:
    r"""LIKE 와일드카드 무력화. `\` 를 먼저 바꿔야 이중 이스케이프가 안 된다."""
    return raw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def tokenize(q: str | None) -> list[str]:
    """검색어 → 토큰 리스트. 너무 짧거나 비면 빈 리스트(= 검색 안 함)."""
    if not q:
        return []
    stripped = q.strip()
    if len(stripped) < MIN_QUERY_LEN:
        return []
    return [t for t in stripped.split() if t][:MAX_TOKENS]


def build_match(
    tokens: list[str], columns: list[str], *, prefix: str = "s"
) -> tuple[str, dict[str, Any]]:
    """토큰 AND × 컬럼 OR 의 WHERE 조각과 바인드 파라미터를 만든다.

    반환 SQL 예 (tokens=['쿨링','스카프'], columns=['name_ko','sku']):
        ((name_ko ILIKE :s0 ESCAPE '\\') OR (sku ILIKE :s0 ESCAPE '\\'))
        AND ((name_ko ILIKE :s1 ESCAPE '\\') OR (sku ILIKE :s1 ESCAPE '\\'))

    tokens 가 비면 ("TRUE", {}) — 호출자가 "검색어 없음 = 전체" 로 쓸 수 있다.
    NULL 컬럼은 ILIKE 가 NULL 을 반환하므로 자동으로 제외된다(원하는 동작).
    """
    if not tokens:
        return "TRUE", {}

    clauses: list[str] = []
    params: dict[str, Any] = {}
    for i, token in enumerate(tokens):
        key = f"{prefix}{i}"
        params[key] = f"%{escape_like(token)}%"
        ors = " OR ".join(f"{col} ILIKE :{key} ESCAPE '\\'" for col in columns)
        clauses.append(f"({ors})")
    return " AND ".join(clauses), params


def build_rank(tokens: list[str], column: str, *, prefix: str = "r") -> tuple[str, dict[str, Any]]:
    """관련도 순위 SQL. 0=완전일치, 1=앞부분 일치, 2=그 외.

    첫 토큰만 본다 — 사용자가 `PICK0294` 같은 식별자를 치면 그 행이 1번으로
    올라오게 하는 게 목적이고, 여러 토큰을 가중합하는 건 지금 규모에 과하다.
    """
    if not tokens:
        return "0", {}
    first = tokens[0]
    params = {
        f"{prefix}_eq": first,
        f"{prefix}_pre": f"{escape_like(first)}%",
    }
    sql = (
        f"CASE WHEN {column} ILIKE :{prefix}_eq THEN 0 "
        f"WHEN {column} ILIKE :{prefix}_pre ESCAPE '\\' THEN 1 "
        f"ELSE 2 END"
    )
    return sql, params


# 종류별 검색 대상 컬럼 — docs/29_global_search.md 의 표와 동일하게 유지할 것
PRODUCT_COLUMNS = ["name_ko", "sku", "business_key", "category_path"]
SALES_COLUMNS = ["product_snapshot_name", "business_key", "marketplace_order_no"]
EXPENSE_COLUMNS = ["description", "memo"]
