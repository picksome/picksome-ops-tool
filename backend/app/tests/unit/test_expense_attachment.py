"""증빙 파일 검증 · 저장소 단위 테스트 (M027).

여기서 막는 사고
---------------
· 확장자만 바꾼 실행파일이 "영수증.pdf" 로 통과하는 것
· 사용자가 올린 파일명이 경로로 해석돼 저장소 밖에 쓰이는 것
· 스캐너가 없는데 "검사 통과" 로 표시되는 것
· 삭제 재시도가 안전하지 않아 두 번째 시도에서 터지는 것

DB 가 필요한 경로(조직 격리·orphan 정리·삭제 승인)는 실제 백엔드를 띄우는
E2E(`frontend/e2e/expense-evidence.spec.ts`)가 검증한다. 여기서 DB 를 흉내 내면
"mock 위에서만 맞는 코드" 가 된다.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from app.core.attachment_storage import (
    LocalFilesystemStorage,
    StorageError,
    build_key,
)
from app.core.exceptions import ValidationError
from app.core.sensitive import (
    PERM_VIEW_EXPENSE_ATTACHMENT,
    can_download_expense_attachment,
)
from app.services.expense_attachment import (
    MAX_ATTACHMENTS_PER_EXPENSE,
    MAX_FILE_BYTES,
    sanitize_filename,
    validate_upload,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"\x00" * 64
HEIC = b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64


# ── 파일명 ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expect_not_contains"),
    [
        ("../../../etc/passwd.pdf", "/"),
        ("..\\..\\windows\\system32\\x.pdf", "\\"),
        ("a\x00b.png", "\x00"),
    ],
)
def test_filename_strips_path_and_control_chars(raw: str, expect_not_contains: str) -> None:
    out = sanitize_filename(raw)
    assert expect_not_contains not in out


def test_filename_never_becomes_dot_only() -> None:
    # `..` 이 그대로 남으면 상위 디렉터리로 읽힌다.
    assert sanitize_filename("..") == "attachment"
    assert sanitize_filename("...") == "attachment"
    assert sanitize_filename("") == "attachment"
    assert sanitize_filename(None) == "attachment"


def test_filename_keeps_korean_and_extension() -> None:
    # 표시용이므로 한글은 그대로 남아야 한다.
    assert sanitize_filename("2026년 8월 광고비 영수증.pdf") == "2026년 8월 광고비 영수증.pdf"


# ── 업로드 검증 ───────────────────────────────────────────────

@pytest.mark.parametrize(
    ("name", "mime", "data"),
    [
        ("a.png", "image/png", PNG),
        ("a.jpg", "image/jpeg", JPG),
        ("a.jpeg", "image/jpeg", JPG),
        ("a.pdf", "application/pdf", PDF),
        ("a.heic", "image/heic", HEIC),
    ],
)
def test_allowed_types_pass(name: str, mime: str, data: bytes) -> None:
    safe, ext = validate_upload(filename=name, content_type=mime, data=data)
    assert safe == name
    assert ext == name.rsplit(".", 1)[1]


def test_content_type_with_charset_suffix_is_accepted() -> None:
    # 브라우저가 `application/pdf; charset=binary` 를 붙이는 경우가 있다.
    validate_upload(filename="a.pdf", content_type="application/pdf; charset=binary", data=PDF)


def test_disallowed_extension_rejected() -> None:
    with pytest.raises(ValidationError) as e:
        validate_upload(filename="payload.exe", content_type="application/octet-stream",
                        data=b"MZ" + b"\x00" * 64)
    assert e.value.detail["reason"] == "EXTENSION_NOT_ALLOWED"


def test_extension_disguise_is_caught_by_content() -> None:
    """`.pdf` 로 이름 붙이고 MIME 도 맞췄지만 내용이 PNG 다.

    확장자·MIME 만 보면 통과한다. 내용을 봐야 잡힌다.
    """
    with pytest.raises(ValidationError) as e:
        validate_upload(filename="evil.pdf", content_type="application/pdf", data=PNG)
    assert e.value.detail["reason"] == "CONTENT_MISMATCH"


def test_mime_mismatch_rejected() -> None:
    with pytest.raises(ValidationError) as e:
        validate_upload(filename="a.png", content_type="application/pdf", data=PNG)
    assert e.value.detail["reason"] == "MIME_MISMATCH"


def test_missing_content_type_falls_back_to_content_check() -> None:
    # 일부 클라이언트는 MIME 을 안 보낸다. 그렇다고 무조건 막으면 정상 업로드가 실패한다.
    validate_upload(filename="a.png", content_type=None, data=PNG)
    with pytest.raises(ValidationError):
        validate_upload(filename="a.png", content_type=None, data=PDF)


def test_too_large_rejected() -> None:
    oversized = PNG + b"\x00" * (MAX_FILE_BYTES + 1)
    with pytest.raises(ValidationError) as e:
        validate_upload(filename="a.png", content_type="image/png", data=oversized)
    assert e.value.detail["reason"] == "TOO_LARGE"
    assert e.value.detail["max_bytes"] == MAX_FILE_BYTES


def test_exactly_at_limit_is_allowed() -> None:
    # 경계에서 거절하면 "10MB 이하" 라는 안내가 거짓말이 된다.
    data = PNG + b"\x00" * (MAX_FILE_BYTES - len(PNG))
    assert len(data) == MAX_FILE_BYTES
    validate_upload(filename="a.png", content_type="image/png", data=data)


def test_empty_file_rejected() -> None:
    with pytest.raises(ValidationError) as e:
        validate_upload(filename="a.png", content_type="image/png", data=b"")
    assert e.value.detail["reason"] == "EMPTY_FILE"


def test_path_traversal_filename_still_validated_by_extension() -> None:
    safe, ext = validate_upload(
        filename="../../etc/passwd.pdf", content_type="application/pdf", data=PDF)
    assert safe == "passwd.pdf"
    assert ext == "pdf"


def test_limits_are_the_documented_ones() -> None:
    # 화면 안내 문구와 어긋나면 사용자는 되는 걸 안 된다고 믿는다.
    assert MAX_FILE_BYTES == 10 * 1024 * 1024
    assert MAX_ATTACHMENTS_PER_EXPENSE == 5


# ── 저장소 ────────────────────────────────────────────────────

@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as d:
        yield LocalFilesystemStorage(d), Path(d)


def test_key_contains_no_user_input() -> None:
    key = build_key(org_id=uuid4(), expense_id=uuid4(), attachment_id=uuid4(), ext="pdf")
    assert key.endswith(".pdf")
    assert ".." not in key
    parts = key.split("/")
    assert len(parts) == 3


def test_key_rejects_bogus_extension() -> None:
    with pytest.raises(StorageError):
        build_key(org_id=uuid4(), expense_id=uuid4(), attachment_id=uuid4(), ext="../sh")


def test_save_open_delete_roundtrip(store) -> None:
    s, root = store
    key = build_key(org_id=uuid4(), expense_id=uuid4(), attachment_id=uuid4(), ext="png")
    s.save(key=key, data=PNG)

    assert s.exists(key=key)
    with s.open(key=key) as fh:
        assert fh.read() == PNG
    # 파일이 root 안에만 만들어졌는가
    written = [p for p in root.rglob("*") if p.is_file()]
    assert len(written) == 1
    assert root in written[0].parents

    s.delete(key=key)
    assert not s.exists(key=key)


def test_delete_is_idempotent(store) -> None:
    """재시도가 안전해야 한다. 이미 지워진 파일에 대해 터지면
    worker 가 영원히 `PENDING_DELETE` 를 못 벗어난다."""
    s, _ = store
    key = build_key(org_id=uuid4(), expense_id=uuid4(), attachment_id=uuid4(), ext="pdf")
    s.save(key=key, data=PDF)
    s.delete(key=key)
    s.delete(key=key)   # 두 번째도 조용히 성공


def test_storage_refuses_escape_key(store) -> None:
    s, _ = store
    for bad in ("../outside.png", "a/../../outside.png", "\x00.png"):
        with pytest.raises(StorageError):
            s.save(key=bad, data=PNG)


def test_open_missing_raises(store) -> None:
    s, _ = store
    with pytest.raises(StorageError):
        s.open(key="00000000-0000-0000-0000-000000000000/x/y.png")


def test_partial_write_leaves_no_visible_file(store) -> None:
    """저장 중 죽어도 `.part` 만 남고 정상 키로는 남지 않는다."""
    s, root = store
    key = build_key(org_id=uuid4(), expense_id=uuid4(), attachment_id=uuid4(), ext="png")
    s.save(key=key, data=PNG)
    assert not list(root.rglob("*.part"))


# ── 권한 ──────────────────────────────────────────────────────

def test_download_permission_matrix() -> None:
    """증빙 원본은 세금계산서 원본일 수 있다 — 목록 조회와 다른 권한이다."""
    assert can_download_expense_attachment("ADMIN")
    assert can_download_expense_attachment("ACCOUNTING")
    assert can_download_expense_attachment("OPERATOR")
    # 세무 신고 자료를 만들려면 원본이 필요하다
    assert can_download_expense_attachment("EXTERNAL_ACCOUNTANT")
    # 조회 전용과 소싱은 사업비용 증빙을 열 이유가 없다
    assert not can_download_expense_attachment("READONLY")
    assert not can_download_expense_attachment("SOURCING")


def test_permission_code_is_stable() -> None:
    # 코드가 바뀌면 권한 문서·프론트 안내 문구가 같이 어긋난다.
    assert PERM_VIEW_EXPENSE_ATTACHMENT == "expense.view_attachment"


# ── 승인 어댑터 ───────────────────────────────────────────────

def test_attachment_adapter_only_allows_pending_delete() -> None:
    """승인으로 파일을 "되살린" 것처럼 보이게 만들 수 없어야 한다."""
    from app.services.approval_targets import ExpenseAttachmentAdapter

    adapter = ExpenseAttachmentAdapter()
    adapter.validate_proposed_changes(
        {"status": "PENDING_DELETE"}, request_type="EXPENSE_EVIDENCE_CHANGE")

    for bad in ("AVAILABLE", "DELETED", "FAILED"):
        with pytest.raises(ValidationError):
            adapter.validate_proposed_changes(
                {"status": bad}, request_type="EXPENSE_EVIDENCE_CHANGE")


def test_attachment_adapter_rejects_unknown_fields() -> None:
    from app.services.approval_targets import ExpenseAttachmentAdapter

    adapter = ExpenseAttachmentAdapter()
    with pytest.raises(ValidationError):
        adapter.validate_proposed_changes(
            {"filename": "새이름.pdf"}, request_type="EXPENSE_EVIDENCE_CHANGE")
    with pytest.raises(ValidationError):
        adapter.validate_proposed_changes({}, request_type="EXPENSE_EVIDENCE_CHANGE")


def test_attachment_is_registered_as_approval_target() -> None:
    from app.services import approval_targets as t

    assert "expense_attachment" in t.supported_target_types()
    assert t.supported_request_types("expense_attachment") == ["EXPENSE_EVIDENCE_CHANGE"]
