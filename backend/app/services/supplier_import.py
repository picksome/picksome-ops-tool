"""공급처 Excel(`도매 매입 사이트들.xlsx`) 읽기.

⚠️ 이 파일에는 평문 ID/PW 가 있다
------------------------------
운영 Excel 의 `ID` / `PW` 열에 실제 로그인 정보가 그대로 들어 있다.

**옮겨오지 않는다.** 값을 읽지도, 반환하지도, 로그에 남기지도 않는다. 이 모듈이
비밀에 대해 만드는 유일한 산출물은 `credential_present` 불리언 —
"이 공급처에는 로그인 계정이 있다" 는 사실뿐이다. 실제 비밀은 사람이
보관함(Bitwarden 등)에 옮기고, 우리는 `password_vault_entry` 로 **위치만**
가리킨다(CLAUDE.md Security Rules).

컬럼을 위치로 찾지 않는다
----------------------
헤더 이름으로 찾는다. 열 순서는 사람이 언제든 바꾼다 — 위치로 읽으면 어느 날
조용히 담당자 이름이 주소 칸에 들어간다. 모르는 헤더는 **추측하지 않고**
`unmapped_headers` 로 보고한다.

덮어쓰지 않는다
------------
이미 있는 공급처는 **비어 있는 칸만 채운다.** 사람이 앱에서 고친 값을 Excel 이
되돌리면 사용자는 같은 수정을 매번 반복하게 된다. 값이 다르면 `differences` 로
보고만 하고 그대로 둔다 — 무엇을 채택할지는 사람이 정한다.

모르는 것은 NULL 이다
------------------
평점 칸의 `???` 는 0 점이 아니라 **아직 안 매겼다**는 뜻이다. 0 으로 넣으면
"최악" 이라는 판정이 된다.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.audit import record_audit

#: 헤더 이름 → 우리 필드. **위치가 아니라 이름으로** 찾는다.
HEADER_MAP: dict[str, str] = {
    "공급업체": "name",
    "공급업체 유형": "handling_categories",
    "문의": "contact_name",
    "주소": "address",
    "웹사이트": "website_url",
    "평점(주관적)": "rating",
    "메모": "memo",
    "참조": "reference_note",
}

#: 읽되 **값을 쓰지 않는** 열. 있다는 사실만 `credential_present` 로 남긴다.
CREDENTIAL_HEADERS = frozenset({"ID", "PW", "아이디", "비밀번호", "패스워드"})

#: 별점 표기에서 숫자만 뽑는다. "★ 3점", "3", "★★★" 같은 표기가 섞여 있다.
_RATING_DIGIT = re.compile(r"(\d)")


@dataclass
class SupplierRow:
    """Excel 한 행에서 **비밀을 뺀** 나머지."""

    row_number: int
    name: str
    supplier_code: str
    handling_categories: str | None = None
    contact_name: str | None = None
    address: str | None = None
    website_url: str | None = None
    website_domain: str | None = None
    rating: int | None = None
    memo: str | None = None
    reference_note: str | None = None
    #: 로그인 계정이 있다는 **사실만**. 값은 어디에도 없다.
    credential_present: bool = False


@dataclass
class ParseReport:
    rows: list[SupplierRow] = field(default_factory=list)
    #: 이름을 못 알아본 헤더. 추측해서 매핑하지 않는다.
    unmapped_headers: list[str] = field(default_factory=list)
    #: 비밀 열을 발견했고 **버렸다**는 기록. 열 이름만 남긴다.
    dropped_credential_headers: list[str] = field(default_factory=list)
    #: 다른 칸에는 값이 있는데 **이름이 없어** 버린 행. 사람이 봐야 한다.
    skipped_rows: list[int] = field(default_factory=list)
    #: 통째로 빈 행. 시트 아래쪽 여백이라 문제가 아니다 — 위와 섞지 않는다.
    blank_rows: int = 0
    #: 웹사이트 칸이 URL 이 아니었던 행. 값은 지우지 않고 알리기만 한다.
    unparsable_urls: list[tuple[int, str]] = field(default_factory=list)


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parse_rating(value: Any) -> int | None:
    """`???` 나 빈칸은 **모른다**(NULL)이지 0 이 아니다."""
    s = _clean(value)
    if not s:
        return None
    m = _RATING_DIGIT.search(s)
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 5 else None


def _domain_of(url: str | None) -> str | None:
    """URL 처럼 생겼을 때만 도메인을 뽑는다.

    실제 시트의 웹사이트 칸에 상호명이 그대로 적힌 행이 있었다. 그걸 호스트로
    받아들이면 "스몰해피" 가 도메인이 되고, 그 값으로 중복 판정까지 하게 된다.
    """
    if not url:
        return None
    candidate = url if "//" in url else f"//{url}"
    host = (urlparse(candidate).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    # 점 없는 호스트는 도메인이 아니다.
    return host if "." in host else None


_CODE_SAFE = re.compile(r"[^A-Za-z0-9]+")


def _code_for(name: str, domain: str | None, used: set[str]) -> str:
    """다시 import 해도 **같은 공급처는 같은 코드**여야 한다.

    행 번호를 코드로 쓰지 않는다 — 행이 하나 밀리면 전부 어긋난다(M037 주석).
    도메인이 있으면 도메인이 가장 안정적이다.

    이름이 전부 한글이면 ASCII 로 남는 게 없다. 그때 `SUPPLIER` 같은 고정값으로
    떨어지면 **서로 다른 공급처가 같은 코드**를 갖는다. 이름 해시를 붙여
    갈라놓는다 — 이름이 같으면 코드도 같으므로 재실행에도 안정적이다.
    """
    base = _CODE_SAFE.sub("_", (domain or name)).strip("_").upper()[:40]
    if not base:
        base = "S_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8].upper()
    code = base
    n = 2
    while code in used:
        code = f"{base}_{n}"
        n += 1
    used.add(code)
    return code


def parse_supplier_sheet(rows: list[tuple[Any, ...]]) -> ParseReport:
    """헤더 한 줄 + 데이터 여러 줄 → `ParseReport`.

    `rows` 는 openpyxl `iter_rows(values_only=True)` 결과 그대로다. 파일을 여는
    책임은 호출자에게 둔다 — 이 함수를 파일 없이 테스트할 수 있어야 한다.
    """
    report = ParseReport()
    if not rows:
        return report

    header = [_clean(c) for c in rows[0]]
    index: dict[str, int] = {}
    for i, h in enumerate(header):
        if not h:
            continue
        if h in CREDENTIAL_HEADERS:
            report.dropped_credential_headers.append(h)
            continue
        field_name = HEADER_MAP.get(h)
        if field_name is None:
            report.unmapped_headers.append(h)
            continue
        index[field_name] = i

    if "name" not in index:
        raise ValueError("공급업체 이름 열을 찾지 못했습니다. 헤더를 확인하세요.")

    credential_cols = [
        i for i, h in enumerate(header) if h and h in CREDENTIAL_HEADERS
    ]

    def cell(row: tuple[Any, ...], key: str) -> Any:
        i = index.get(key)
        return row[i] if i is not None and i < len(row) else None

    used_codes: set[str] = set()
    for offset, row in enumerate(rows[1:], start=2):
        name = _clean(cell(row, "name"))
        if not name:
            # 통째로 빈 행(시트 아래 여백)과 **이름만 빠진 행**은 무게가 다르다.
            # 후자는 사람이 봐야 하는 데이터 손실이다.
            if any(_clean(c) is not None for c in row):
                report.skipped_rows.append(offset)
            else:
                report.blank_rows += 1
            continue

        url = _clean(cell(row, "website_url"))
        domain = _domain_of(url)
        if url and domain is None:
            # 값을 지우지는 않는다 — 사람이 적어 둔 정보다. 알리기만 한다.
            report.unparsable_urls.append((offset, url))
        report.rows.append(SupplierRow(
            row_number=offset,
            name=name,
            supplier_code=_code_for(name, domain, used_codes),
            handling_categories=_clean(cell(row, "handling_categories")),
            contact_name=_clean(cell(row, "contact_name")),
            address=_clean(cell(row, "address")),
            website_url=url,
            website_domain=domain,
            rating=_parse_rating(cell(row, "rating")),
            memo=_clean(cell(row, "memo")),
            reference_note=_clean(cell(row, "reference_note")),
            # 값을 읽지 않는다 — 비어 있지 않은지만 본다.
            credential_present=any(
                _clean(row[i]) is not None for i in credential_cols if i < len(row)
            ),
        ))

    return report


# ──────────────────────────────────────────────────────────────────────────
# 반영
# ──────────────────────────────────────────────────────────────────────────

#: 이미 있는 공급처에서 **비어 있으면 채우는** 칸.
_FILLABLE = (
    "handling_categories", "contact_name", "address",
    "website_url", "website_domain", "memo", "reference_note",
)


#: 실행 단계. 셋을 한 함수의 불리언으로 뭉치지 않는다 —
#: "dry_run=False" 하나로 운영 데이터가 바뀌는 구조는 실수를 부른다.
#:
#:   ANALYZE   파일만 읽는다. DB 를 보지도 않는다
#:   DRY_RUN   DB 와 대조해서 무엇이 바뀔지 계산한다. 쓰지 않는다
#:   APPLY     실제로 반영한다
MODES = ("ANALYZE", "DRY_RUN", "APPLY")


class ImportNotAllowedError(RuntimeError):
    """운영 환경에서 개발용 importer 를 막는다."""


@dataclass
class ImportResult:
    mode: str = "DRY_RUN"
    created: int = 0
    filled: int = 0
    unchanged: int = 0
    #: 값이 달라 사람이 봐야 하는 행 수. `differences` 는 필드 단위라
    #: 한 행에서 여러 건이 나올 수 있다 — 행 수와 다르다.
    conflicted: int = 0
    #: 값이 다른데 **덮지 않은** 것. (공급처명, 필드, 우리 값, Excel 값)
    differences: list[dict[str, Any]] = field(default_factory=list)

    @property
    def dry_run(self) -> bool:
        return self.mode != "APPLY"

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "created": self.created, "filled": self.filled,
            "unchanged": self.unchanged, "conflicted": self.conflicted,
            "differences": self.differences,
            "dry_run": self.dry_run,
        }


def assert_import_allowed(mode: str) -> None:
    """운영에서 개발용 importer 가 대량 반영하는 것을 막는다.

    막는 것은 `APPLY` 뿐이다. 운영에서도 "이 파일을 넣으면 무엇이 바뀌나" 는
    볼 수 있어야 한다 — 그게 못 하게 되면 사람이 결국 우회한다.

    운영 반영이 필요해지면 별도의 명시적 관리자 흐름을 만든다. CLI 한 줄로
    운영 공급처 마스터가 바뀌는 경로를 남겨 두지 않는다.
    """
    if mode != "APPLY":
        return
    from app.config import get_settings

    env = str(getattr(get_settings(), "APP_ENV", "")).lower()
    if env in ("prod", "production"):
        raise ImportNotAllowedError(
            "운영 환경에서는 개발용 Excel importer 로 반영할 수 없습니다. "
            "관리자 반영 흐름을 사용하세요.",
        )


def record_import_run(
    db: Session, *, org_id: UUID, actor_user_id: UUID | None, mode: str,
    source_name: str, source_sha256: str, sheet_name: str | None,
    report: ParseReport, result: ImportResult | None,
) -> UUID:
    """무엇을 언제 어떤 환경에서 돌렸는지 남긴다 (M043).

    파일 해시를 남기는 이유: 파일명은 바뀌고 Excel 은 열기만 해도 수정 시각이
    바뀐다. "지난주에 돌린 그 파일" 을 확인할 방법은 내용 해시뿐이다.

    `credential_skipped` 는 **버린 열의 개수**다. 값은 파서에서 이미 폐기돼
    여기까지 오지 않는다.
    """
    from app.config import get_settings

    row = db.execute(text("""
        INSERT INTO supplier_import_run
               (org_id, source_name, source_sha256, sheet_name, mode, app_env,
                created, updated, unchanged, conflicts, skipped,
                credential_skipped, finished_at, actor_user_id)
        VALUES (:org, :src, :sha, :sheet, :mode, :env,
                :c, :u, :un, :cf, :sk, :cs, now(), :actor)
        RETURNING id
    """), {
        "org": str(org_id), "src": source_name[:300], "sha": source_sha256,
        "sheet": sheet_name, "mode": mode,
        "env": str(getattr(get_settings(), "APP_ENV", "unknown")),
        "c": result.created if result else 0,
        "u": result.filled if result else 0,
        "un": result.unchanged if result else 0,
        "cf": len(result.differences) if result else 0,
        "sk": len(report.skipped_rows),
        "cs": len(report.dropped_credential_headers),
        "actor": str(actor_user_id) if actor_user_id else None,
    }).scalar_one()
    return UUID(str(row))


def sha256_of(path: str) -> str:
    """파일 내용 해시. 스트리밍으로 읽는다 — 큰 파일을 통째로 올리지 않는다."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65_536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def import_suppliers(
    db: Session, *, org_id: UUID, actor_user_id: UUID,
    rows: list[SupplierRow], mode: str = "DRY_RUN",
) -> ImportResult:
    """공급처 반영. **기본이 DRY_RUN 이다.**

    운영 데이터를 건드리는 일을 기본값으로 두지 않는다. 무엇이 바뀔지 먼저
    보고, 사람이 확인한 뒤에 `mode="APPLY"` 로 실행한다.
    """
    if mode not in MODES:
        raise ValueError(f"알 수 없는 실행 단계입니다: {mode}")
    assert_import_allowed(mode)

    dry_run = mode != "APPLY"
    result = ImportResult(mode=mode)

    for row in rows:
        existing = db.execute(text("""
            SELECT id, name, rating, credential_present,
                   handling_categories, contact_name, address,
                   website_url, website_domain, memo, reference_note
              FROM supplier_site
             WHERE org_id = :o AND supplier_code = :c AND deleted_at IS NULL
        """), {"o": str(org_id), "c": row.supplier_code}).mappings().first()

        if existing is None:
            if not dry_run:
                new_id = db.execute(text("""
                    INSERT INTO supplier_site
                           (org_id, supplier_code, name, website_url, website_domain,
                            rating, memo, contact_name, address, handling_categories,
                            reference_note, credential_present, created_by, updated_by)
                    VALUES (:o, :c, :n, :u, :d, :r, :m, :ct, :a, :hc, :rn, :cp, :by, :by)
                 RETURNING id
                """), {
                    "o": str(org_id), "c": row.supplier_code, "n": row.name,
                    "u": row.website_url, "d": row.website_domain, "r": row.rating,
                    "m": row.memo, "ct": row.contact_name, "a": row.address,
                    "hc": row.handling_categories, "rn": row.reference_note,
                    "cp": row.credential_present, "by": str(actor_user_id),
                }).scalar_one()
                # 감사 기록에도 비밀은 없다 — 자격증명은 유무만 남는다.
                record_audit(
                    db, org_id=org_id, actor_user_id=actor_user_id,
                    domain_table="supplier_site", domain_row_id=new_id,
                    action="IMPORT", reason="SUPPLIER_EXCEL_IMPORT",
                    after={"supplier_code": row.supplier_code, "name": row.name,
                           "credential_present": row.credential_present},
                )
            result.created += 1
            continue

        updates: dict[str, Any] = {}
        conflicts_here = 0
        for f in _FILLABLE:
            incoming = getattr(row, f)
            current = existing[f]
            if incoming is None:
                continue
            if current is None:
                updates[f] = incoming
            elif str(current).strip() != str(incoming).strip():
                # 덮지 않는다 — 사람이 고친 값일 수 있다.
                conflicts_here += 1
                result.differences.append({
                    "supplier_code": row.supplier_code, "name": existing["name"],
                    "field": f, "current": current, "excel": incoming,
                })

        if row.rating is not None and existing["rating"] is None:
            updates["rating"] = row.rating
        elif row.rating is not None and existing["rating"] != row.rating:
            conflicts_here += 1
            result.differences.append({
                "supplier_code": row.supplier_code, "name": existing["name"],
                "field": "rating", "current": existing["rating"], "excel": row.rating,
            })

        # 계정이 있다는 사실은 켜기만 한다. Excel 이 비었다고 해서 앱에서 등록한
        # 자격증명을 없는 것으로 만들면 안 된다.
        if row.credential_present and not existing["credential_present"]:
            updates["credential_present"] = True

        if not updates:
            # 차이가 있는데 채울 칸이 없는 행을 "변화 없음" 으로 세면 미리보기가
            # 거짓말을 한다 — 사람이 확인해야 할 행이 조용히 묻힌다.
            if conflicts_here:
                result.conflicted += 1
            else:
                result.unchanged += 1
            continue

        result.filled += 1
        if conflicts_here:
            # 일부는 채우고 일부는 다른 행. 양쪽에 다 센다 — 사람이 봐야 한다.
            result.conflicted += 1
        if dry_run:
            continue

        sets = ", ".join(f"{k} = :{k}" for k in updates)
        db.execute(
            text(f"UPDATE supplier_site SET {sets}, updated_at = now(),"
                 f" updated_by = :by WHERE id = :id"),
            {**updates, "by": str(actor_user_id), "id": str(existing["id"])},
        )
        record_audit(
            db, org_id=org_id, actor_user_id=actor_user_id,
            domain_table="supplier_site", domain_row_id=existing["id"],
            action="IMPORT", reason="SUPPLIER_EXCEL_IMPORT",
            # 채운 칸만 남긴다. 덮은 것은 없다.
            after={k: v for k, v in updates.items()},
        )

    return result
