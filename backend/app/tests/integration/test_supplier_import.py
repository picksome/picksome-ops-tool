"""공급처 Excel import (`도매 매입 사이트들.xlsx`).

이 파일이 고정하는 것
------------------
1. **평문 ID/PW 는 어디에도 저장되지 않는다** — 있다는 사실만 남는다
2. 열은 이름으로 찾는다. 모르는 열은 추측하지 않고 보고한다
3. 평점 `???` 는 0 이 아니라 NULL 이다
4. 이미 있는 공급처를 **덮지 않는다** — 빈칸만 채우고 차이는 보고한다
5. 두 번 돌려도 같다 (코드는 행 번호가 아니라 도메인 기반)

1 이 이 파일의 존재 이유다
-----------------------
실제 운영 Excel 에 로그인 비밀번호가 평문으로 들어 있다. import 경로가 그것을
DB 로 옮기는 순간 CLAUDE.md Security Rules 가 깨지고, 되돌리기도 어렵다.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.supplier_import import (
    import_suppliers,
    parse_supplier_sheet,
)
from app.tests.fixtures.factories import make_app_user, make_organization

# 실제 운영 Excel 과 같은 헤더. **값은 전부 가짜다** —
# 개발 fixture 에 실제 credential 을 넣지 않는다.
HEADER = ("공급업체", "공급업체 유형", "문의", "주소", "웹사이트",
          "평점(주관적)", "메모", "ID", "PW", "참조")

SECRET = "TEST_SECRET"


def _rows() -> list[tuple]:
    return [
        HEADER,
        ("도매꾹", "생활/잡화", "홍길동", "서울시 어딘가",
         "https://domeggook.com/main/index.php", "★ 5점", "선불 결제",
         "test_account", SECRET, "가입 필요"),
        ("천유", "생활/소싱, 생활/잡화", "김철수", "서울시 어딘가",
         "https://www.cheonyu.com/", "???", "카드결제 가능",
         "test_account", SECRET, None),
        # 이름 없는 행 — 건너뛴다.
        (None, "펫", None, None, None, None, None, None, None, None),
    ]


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def actor(db: Session, org: uuid.UUID) -> uuid.UUID:
    return make_app_user(db, org_id=org, role="ADMIN")


# ── 파싱 ────────────────────────────────────────────────────────

def test_password_column_is_read_but_never_carried():
    """비밀 열은 **있다는 사실만** 남는다."""
    report = parse_supplier_sheet(_rows())

    assert report.dropped_credential_headers == ["ID", "PW"]
    assert [r.name for r in report.rows] == ["도매꾹", "천유"]
    assert all(r.credential_present for r in report.rows)

    # 어떤 필드에도 값이 없다.
    for row in report.rows:
        serialized = repr(vars(row))
        assert SECRET not in serialized
        assert "test_account" not in serialized


def test_unknown_headers_are_reported_not_guessed():
    """모르는 열을 추측해서 넣으면 어느 날 조용히 엉뚱한 칸이 채워진다."""
    rows = [HEADER + ("결제조건", "담당MD"), _rows()[1] + ("선불", "A")]
    report = parse_supplier_sheet(rows)

    assert report.unmapped_headers == ["결제조건", "담당MD"]
    # 무시했을 뿐 행은 정상 처리된다.
    assert len(report.rows) == 1


def test_missing_rating_is_unknown_not_zero():
    """`???` 는 0 점이 아니다. 0 으로 넣으면 '최악' 이라는 판정이 된다."""
    report = parse_supplier_sheet(_rows())
    by_name = {r.name: r for r in report.rows}

    assert by_name["도매꾹"].rating == 5
    assert by_name["천유"].rating is None


def test_code_is_stable_across_reparse():
    """행 번호를 코드로 쓰면 행 하나만 밀려도 전부 어긋난다."""
    first = {r.name: r.supplier_code for r in parse_supplier_sheet(_rows()).rows}

    shifted = [_rows()[0], ("새 공급처", None, None, None, None, None, None,
                            None, None, None)] + _rows()[1:]
    second = {r.name: r.supplier_code for r in parse_supplier_sheet(shifted).rows}

    assert first["도매꾹"] == second["도매꾹"]
    assert first["천유"] == second["천유"]


def test_domain_is_normalized():
    report = parse_supplier_sheet(_rows())
    by_name = {r.name: r for r in report.rows}
    assert by_name["도매꾹"].website_domain == "domeggook.com"
    # www. 는 떼어낸다 — 같은 곳을 두 곳으로 만들면 안 된다.
    assert by_name["천유"].website_domain == "cheonyu.com"


def test_row_without_name_is_reported_but_blank_filler_is_not():
    """이름만 빠진 행은 데이터 손실이다. 시트 아래 여백은 아니다."""
    rows = _rows() + [(None,) * len(HEADER), (None,) * len(HEADER)]
    report = parse_supplier_sheet(rows)

    assert report.skipped_rows == [4]   # 유형 '펫' 만 있고 이름이 없던 행
    assert report.blank_rows == 2


def test_non_url_website_cell_is_kept_but_reported():
    """실제 시트에 웹사이트 칸에 상호명이 적힌 행이 있었다.

    그걸 호스트로 받아들이면 상호명이 도메인이 되고 중복 판정까지 오염된다.
    """
    rows = [HEADER, ("스몰해피", None, None, None, "스몰해피", None, None,
                     None, None, None)]
    report = parse_supplier_sheet(rows)

    assert report.rows[0].website_domain is None
    assert report.rows[0].website_url == "스몰해피"   # 지우지 않는다
    assert report.unparsable_urls == [(2, "스몰해피")]


def test_korean_only_names_get_distinct_codes():
    """ASCII 로 남는 게 없다고 서로 다른 공급처가 같은 코드를 가지면 안 된다."""
    rows = [HEADER,
            ("스몰해피", None, None, None, "스몰해피", None, None, None, None, None),
            ("행복상사", None, None, None, None, None, None, None, None, None)]
    report = parse_supplier_sheet(rows)

    codes = [r.supplier_code for r in report.rows]
    assert len(set(codes)) == 2
    # 이름이 같으면 코드도 같다 — 재실행에 안정적이어야 한다.
    assert parse_supplier_sheet(rows).rows[0].supplier_code == codes[0]


def test_missing_name_column_is_an_error():
    with pytest.raises(ValueError):
        parse_supplier_sheet([("유형", "주소"), ("펫", "서울")])


# ── 반영 ────────────────────────────────────────────────────────

def test_dry_run_writes_nothing(db, org, actor):
    """운영 데이터를 건드리는 일을 기본값으로 두지 않는다."""
    rows = parse_supplier_sheet(_rows()).rows
    result = import_suppliers(db, org_id=org, actor_user_id=actor, rows=rows)

    assert result.mode == "DRY_RUN"
    assert result.dry_run is True
    assert result.created == 2
    assert db.execute(text("SELECT count(*) FROM supplier_site WHERE org_id = :o"),
                      {"o": str(org)}).scalar() == 0


def test_apply_creates_suppliers_without_any_secret(db, org, actor):
    rows = parse_supplier_sheet(_rows()).rows
    import_suppliers(db, org_id=org, actor_user_id=actor, rows=rows, mode="APPLY")
    db.commit()

    saved = db.execute(text("""
        SELECT name, website_domain, rating, credential_present,
               contact_name, handling_categories, reference_note
          FROM supplier_site WHERE org_id = :o ORDER BY name
    """), {"o": str(org)}).mappings().all()
    assert len(saved) == 2
    assert saved[0]["credential_present"] is True
    assert saved[0]["contact_name"]

    # DB 전체를 뒤져도 비밀은 없다.
    dumped = db.execute(text("""
        SELECT string_agg(t::text, ' ') FROM supplier_site t WHERE org_id = :o
    """), {"o": str(org)}).scalar() or ""
    assert SECRET not in dumped
    assert "test_account" not in dumped

    # 감사 기록에도 없다.
    audit = db.execute(text("""
        SELECT string_agg(a::text, ' ') FROM audit_log a
         WHERE org_id = :o AND domain_table = 'supplier_site'
    """), {"o": str(org)}).scalar() or ""
    assert SECRET not in audit
    assert "test_account" not in audit


def test_second_import_does_not_duplicate(db, org, actor):
    rows = parse_supplier_sheet(_rows()).rows
    import_suppliers(db, org_id=org, actor_user_id=actor, rows=rows, mode="APPLY")
    db.commit()

    again = import_suppliers(db, org_id=org, actor_user_id=actor, rows=rows,
                             mode="APPLY")
    db.commit()

    assert again.created == 0
    assert again.unchanged == 2
    assert db.execute(text("SELECT count(*) FROM supplier_site WHERE org_id = :o"),
                      {"o": str(org)}).scalar() == 2


def test_user_edits_are_not_overwritten(db, org, actor):
    """앱에서 고친 값을 Excel 이 되돌리면 사용자는 같은 수정을 반복하게 된다."""
    rows = parse_supplier_sheet(_rows()).rows
    import_suppliers(db, org_id=org, actor_user_id=actor, rows=rows, mode="APPLY")
    db.commit()

    db.execute(text("""
        UPDATE supplier_site SET memo = '사람이 고친 메모'
         WHERE org_id = :o AND name = '도매꾹'
    """), {"o": str(org)})
    db.commit()

    result = import_suppliers(db, org_id=org, actor_user_id=actor, rows=rows,
                              mode="APPLY")
    db.commit()

    assert db.execute(text("""
        SELECT memo FROM supplier_site WHERE org_id = :o AND name = '도매꾹'
    """), {"o": str(org)}).scalar() == "사람이 고친 메모"
    # 조용히 무시하지 않는다 — 무엇이 달랐는지 보고한다.
    diffs = [d for d in result.differences if d["field"] == "memo"]
    assert len(diffs) == 1
    assert diffs[0]["excel"] == "선불 결제"


def test_blank_excel_cell_fills_but_never_erases(db, org, actor):
    """Excel 이 비었다고 우리 값을 지우지 않는다."""
    rows = parse_supplier_sheet(_rows()).rows
    import_suppliers(db, org_id=org, actor_user_id=actor, rows=rows, mode="APPLY")
    db.commit()

    blank = [HEADER, ("도매꾹", None, None, None,
                      "https://domeggook.com/main/index.php", None, None,
                      None, None, None)]
    import_suppliers(db, org_id=org, actor_user_id=actor,
                     rows=parse_supplier_sheet(blank).rows, mode="APPLY")
    db.commit()

    row = db.execute(text("""
        SELECT memo, credential_present FROM supplier_site
         WHERE org_id = :o AND name = '도매꾹'
    """), {"o": str(org)}).mappings().one()
    assert row["memo"] == "선불 결제"
    # 자격증명 유무도 마찬가지 — Excel 이 비었다고 없는 것으로 만들지 않는다.
    assert row["credential_present"] is True


# ── 단계 분리 · 운영 차단 · 실행 기록 ────────────────────────────

def test_apply_is_blocked_in_production(db, org, actor, monkeypatch):
    """CLI 한 줄로 운영 공급처 마스터가 바뀌는 경로를 남기지 않는다."""
    from app.config import get_settings
    from app.services.supplier_import import ImportNotAllowedError

    monkeypatch.setattr(get_settings(), "APP_ENV", "prod", raising=False)
    rows = parse_supplier_sheet(_rows()).rows

    with pytest.raises(ImportNotAllowedError):
        import_suppliers(db, org_id=org, actor_user_id=actor, rows=rows, mode="APPLY")

    # 미리보기는 운영에서도 된다 — 못 하게 하면 사람이 결국 우회한다.
    preview = import_suppliers(db, org_id=org, actor_user_id=actor, rows=rows,
                               mode="DRY_RUN")
    assert preview.created == 2
    assert db.execute(text("SELECT count(*) FROM supplier_site WHERE org_id = :o"),
                      {"o": str(org)}).scalar() == 0


def test_unknown_mode_is_rejected(db, org, actor):
    with pytest.raises(ValueError):
        import_suppliers(db, org_id=org, actor_user_id=actor, rows=[], mode="FORCE")


def test_dry_run_distinguishes_new_conflicting_and_unchanged(db, org, actor):
    """세 상태를 하나로 뭉치면 미리보기를 보고 판단할 수 없다."""
    rows = parse_supplier_sheet(_rows()).rows
    import_suppliers(db, org_id=org, actor_user_id=actor, rows=rows, mode="APPLY")
    db.execute(text("""
        UPDATE supplier_site SET memo = '사람이 고친 메모'
         WHERE org_id = :o AND name = '도매꾹'
    """), {"o": str(org)})
    db.commit()

    extra = _rows() + [("새 공급처", None, None, None, "https://new.example.com",
                        None, None, None, None, None)]
    preview = import_suppliers(db, org_id=org, actor_user_id=actor,
                               rows=parse_supplier_sheet(extra).rows, mode="DRY_RUN")

    assert preview.created == 1          # 새 공급처
    assert preview.unchanged == 1        # 천유
    # 도매꾹은 값이 달라 사람이 봐야 한다 — "변화 없음" 이 아니다.
    assert preview.conflicted == 1
    assert len(preview.differences) == 1
    assert preview.differences[0]["field"] == "memo"
    # 미리보기는 아무것도 쓰지 않는다.
    assert db.execute(text("SELECT count(*) FROM supplier_site WHERE org_id = :o"),
                      {"o": str(org)}).scalar() == 2


def test_import_run_is_recorded_with_the_file_hash(db, org, actor):
    """파일명은 바뀌고 Excel 은 열기만 해도 수정 시각이 바뀐다.

    "지난주에 돌린 그 파일" 을 확인할 방법은 내용 해시뿐이다.
    """
    from app.services.supplier_import import record_import_run

    report = parse_supplier_sheet(_rows())
    result = import_suppliers(db, org_id=org, actor_user_id=actor,
                              rows=report.rows, mode="APPLY")
    record_import_run(
        db, org_id=org, actor_user_id=actor, mode="APPLY",
        source_name="도매 매입 사이트들.xlsx", source_sha256="a" * 64,
        sheet_name="공급업체", report=report, result=result,
    )
    db.commit()

    row = db.execute(text("""
        SELECT mode, app_env, created, credential_skipped, source_sha256,
               finished_at
          FROM supplier_import_run WHERE org_id = :o
    """), {"o": str(org)}).mappings().one()
    assert row["mode"] == "APPLY"
    assert row["created"] == 2
    # 버린 열의 **개수**다. 값이 아니다.
    assert row["credential_skipped"] == 2
    assert row["source_sha256"] == "a" * 64
    assert row["finished_at"] is not None

    # 실행 기록에도 비밀은 없다.
    dumped = db.execute(text("""
        SELECT string_agg(t::text, ' ') FROM supplier_import_run t WHERE org_id = :o
    """), {"o": str(org)}).scalar() or ""
    assert SECRET not in dumped
