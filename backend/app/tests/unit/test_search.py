"""통합 검색 매칭 규칙 단위테스트 (services/search.py).

DB 없이 순수 함수만 검증한다 — SQL 조각과 바인드 파라미터가 의도대로 나오는지.
설계: docs/29_global_search.md
"""

from app.services.search import (
    MAX_TOKENS,
    PRODUCT_COLUMNS,
    build_match,
    build_rank,
    escape_like,
    tokenize,
)

# ── tokenize ────────────────────────────────────────────────────────────────

def test_tokenize_splits_on_whitespace():
    assert tokenize("쿨링 스카프") == ["쿨링", "스카프"]


def test_tokenize_collapses_repeated_spaces():
    assert tokenize("쿨링   스카프  ") == ["쿨링", "스카프"]


def test_tokenize_rejects_too_short():
    # 한 글자로 전체를 긁으면 팔레트가 쓸모없어진다
    assert tokenize("쿨") == []
    assert tokenize(" ") == []
    assert tokenize("") == []
    assert tokenize(None) == []


def test_tokenize_caps_token_count():
    q = " ".join(str(i) * 2 for i in range(20))
    assert len(tokenize(q)) == MAX_TOKENS


# ── escape_like ─────────────────────────────────────────────────────────────

def test_escape_like_neutralizes_wildcards():
    # `%` 를 안 막으면 한 글자로 전체 매칭이 된다
    assert escape_like("100%") == "100\\%"
    assert escape_like("a_b") == "a\\_b"


def test_escape_like_handles_backslash_first():
    # `\` 를 나중에 치환하면 앞서 만든 이스케이프까지 다시 이스케이프된다
    assert escape_like("a\\b") == "a\\\\b"
    assert escape_like("\\%") == "\\\\\\%"


# ── build_match ─────────────────────────────────────────────────────────────

def test_build_match_empty_tokens_is_passthrough():
    sql, params = build_match([], PRODUCT_COLUMNS)
    assert sql == "TRUE"
    assert params == {}


def test_build_match_ands_tokens_and_ors_columns():
    sql, params = build_match(["쿨링", "스카프"], ["name_ko", "sku"])
    # 토큰끼리는 AND
    assert sql.count(" AND ") == 1
    # 각 토큰이 모든 컬럼에 대해 OR
    assert sql.count("name_ko ILIKE") == 2
    assert sql.count("sku ILIKE") == 2
    assert params == {"s0": "%쿨링%", "s1": "%스카프%"}


def test_build_match_escapes_wildcards_in_params():
    _, params = build_match(["50%"], ["name_ko"])
    assert params["s0"] == "%50\\%%"


def test_build_match_prefix_avoids_param_collision():
    _, a = build_match(["x1"], ["name_ko"], prefix="p")
    assert "p0" in a


# ── build_rank ──────────────────────────────────────────────────────────────

def test_build_rank_empty_is_constant_zero():
    sql, params = build_rank([], "name_ko")
    assert sql == "0"
    assert params == {}


def test_build_rank_uses_first_token_only():
    sql, params = build_rank(["PICK0294", "무시됨"], "name_ko")
    assert params["r_eq"] == "PICK0294"
    assert params["r_pre"] == "PICK0294%"
    assert "무시됨" not in str(params)
    # 완전일치 0 → prefix 1 → 나머지 2
    assert "THEN 0" in sql and "THEN 1" in sql and "ELSE 2" in sql


def test_build_rank_escapes_prefix_pattern():
    _, params = build_rank(["50%"], "name_ko")
    assert params["r_pre"] == "50\\%%"
