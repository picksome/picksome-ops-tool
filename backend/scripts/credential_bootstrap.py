"""실제 API 자격증명 부트스트랩 — 평문 파일 → SecretStore.

⚠️ 이 스크립트가 다루는 파일에는 **실제 운영 자격증명**이 들어 있다
---------------------------------------------------------------
그래서 규칙이 하나다: **값은 프로세스 밖으로 나가지 않는다.**

```
파일 → 메모리 → SecretStore(암호화)      허용
파일 → stdout / 로그 / 예외 메시지        금지
```

값의 길이·앞뒤 몇 글자·해시도 찍지 않는다. "마스킹했으니 괜찮다" 는 습관이
생기면 언젠가 마스킹을 잊는다. 이 스크립트가 출력하는 것은 **어떤 항목이
있는가** 뿐이다.

왜 파일을 계속 쓰지 않나
--------------------
부트스트랩 전용이다. 커넥터는 실행할 때 이 파일을 읽지 않는다.

```
Connector → Credential Resolver → SecretStore
```

파일을 런타임 의존성으로 두면 서버를 옮길 때마다 평문이 따라다닌다.

파일 형식
--------
`이름: 값` 한 줄에 하나. 서비스 구분은 `[NAVER]` 같은 절 머리 또는
`naver.txt` 같은 파일명으로 한다 — 실제 파일을 읽고 맞춘다.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

#: 서비스별로 **반드시 있어야 하는** 항목.
#:
#: 공식 문서 기준이다. 없는 항목을 지어내지 않는다 — 지어내면 "등록됐다" 고
#: 보고해 놓고 호출할 때 실패한다.
REQUIRED: dict[str, tuple[str, ...]] = {
    "NAVER": ("client_id", "client_secret"),
    "COUPANG": ("vendor_id", "access_key", "secret_key"),
    "ELEVENST": ("api_key",),
    # NEXT ENGINE 은 `Api-Auth-Key` 헤더 하나로 인증한다(공식 문서 기준,
    # base URL `https://neapi.next-engine.co.kr`). 처음에는 OAuth 를 쓰는
    # 줄 알고 client_id/client_secret 을 필수로 뒀는데, 실제 발급된 것은
    # **단일 API 키**였다. 필수 항목을 지어내면 "미완성" 으로 보고해 놓고
    # 등록을 막는다 — 실제 인증 방식에 맞춘다.
    "NEXT_ENGINE": ("api_key",),
}

#: 파일에서 만나는 이름 → 우리 항목 이름.
#:
#: 사람이 손으로 적은 파일이라 표기가 제각각이다. **추측해서 매핑하지 않는다** —
#: 여기 없는 이름은 `unmapped` 로 보고하고 사용자가 정하게 한다.
ALIASES: dict[str, dict[str, str]] = {
    "NAVER": {
        "api": "client_id", "client_id": "client_id", "clientid": "client_id",
        "application_id": "client_id", "app_id": "client_id",
        "sec": "client_secret", "secret": "client_secret",
        "client_secret": "client_secret", "clientsecret": "client_secret",
        "application_secret": "client_secret",
    },
    "COUPANG": {
        "vendor": "vendor_id", "vendor_id": "vendor_id", "vendorid": "vendor_id",
        "access": "access_key", "access_key": "access_key",
        "accesskey": "access_key",
        "secret": "secret_key", "secret_key": "secret_key",
        "secretkey": "secret_key",
    },
    "ELEVENST": {
        "11st": "api_key", "elevenst": "api_key", "api": "api_key",
        "api_key": "api_key", "apikey": "api_key", "key": "api_key",
    },
    "NEXT_ENGINE": {
        "api": "api_key", "api_key": "api_key", "apikey": "api_key",
        "auth_key": "api_key", "authkey": "api_key",
        # 헤더 이름 그대로 적어 둔 파일도 받는다.
        "api_auth_key": "api_key",
        "uid": "uid", "state": "state",
    },
}

#: 파일 이름 → 서비스. 절 머리가 없는 파일을 위한 것이다.
FILE_HINTS: dict[str, str] = {
    "naver": "NAVER",
    # 사람이 한글로 적어 둔 이름. 실제 파일에 있는 표기만 넣는다 —
    # 없는 표기를 미리 넣어 두면 오타를 조용히 받아들이게 된다.
    "네이버": "NAVER",
    "쿠팡": "COUPANG",
    "11번가": "ELEVENST",
    "11번가api": "ELEVENST",
    "넥스트엔진": "NEXT_ENGINE",
    "넥스트엔진api": "NEXT_ENGINE",
    "coupang": "COUPANG",
    "11st": "ELEVENST",
    "elevenst": "ELEVENST",
    "nextengine": "NEXT_ENGINE",
    "next_engine": "NEXT_ENGINE",
}

_SECTION = re.compile(r"^\s*[\[#]?\s*([A-Za-z_0-9]+)\s*[\]]?\s*$")

#: 이름 → (서비스, 필드). `COUPANG_VENDOR_ID` 처럼 **서비스와 필드가 한 이름에
#: 붙어 있는** 표기를 푼다. 실제 파일이 이 표기를 쓰고 있었다.
#:
#: 값의 생김새로 추측하는 것과 다르다 — 이건 사용자가 이름으로 **명시한** 것이라
#: 근거가 있다.
_FIELD_TOKENS: dict[str, str] = {
    "vendor_id": "vendor_id", "vendorid": "vendor_id", "vendor": "vendor_id",
    "access_key": "access_key", "accesskey": "access_key", "access": "access_key",
    "secret_key": "secret_key", "secretkey": "secret_key",
    "client_id": "client_id", "clientid": "client_id",
    "client_secret": "client_secret", "clientsecret": "client_secret",
    "api_key": "api_key", "apikey": "api_key", "api": "api_key",
}


def split_service_field(name: str) -> tuple[str | None, str | None]:
    """`COUPANG_VENDOR_ID` → `("COUPANG", "vendor_id")`.

    서비스 접두어가 우리 목록에 있을 때만 푼다. 모르는 접두어는 풀지 않고
    호출자가 `unmapped` 로 보고하게 둔다 — 추측해서 배정하지 않는다.
    """
    key = name.strip().lower().replace("-", "_").replace(" ", "_")
    for prefix, service in sorted(FILE_HINTS.items(), key=lambda kv: -len(kv[0])):
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix):].lstrip("_")
        if not rest:
            return service, None
        field = _FIELD_TOKENS.get(rest)
        return service, field
    return None, None


@dataclass
class ServiceCredential:
    """한 서비스의 자격증명.

    `fields` 는 **값을 담고 있다.** 이 객체를 절대 로깅하거나 repr 하지 않는다.
    dataclass 의 자동 `__repr__` 을 쓰지 않는 이유다.
    """

    service: str
    fields: dict[str, str] = field(default_factory=dict)
    unmapped: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def __repr__(self) -> str:  # pragma: no cover - 값이 없다는 것이 요점
        return (f"<ServiceCredential {self.service} "
                f"fields={sorted(self.fields)} unmapped={self.unmapped}>")

    __str__ = __repr__

    @property
    def missing(self) -> list[str]:
        return [f for f in REQUIRED.get(self.service, ()) if f not in self.fields]

    @property
    def complete(self) -> bool:
        return self.service in REQUIRED and not self.missing

    def report(self) -> dict[str, Any]:
        """보고용. **값이 하나도 들어가지 않는다.**"""
        return {
            "service": self.service,
            "fields_present": sorted(self.fields),
            "missing": self.missing,
            "unmapped_names": self.unmapped,
            "problems": self.problems,
            "complete": self.complete,
        }


def _clean_value(raw: str) -> tuple[str, list[str]]:
    """값 정규화. **위험한 교정은 하지 않는다.**

    공백·BOM·따옴표는 사람이 붙여넣다 흔히 섞이는 것이라 떼어낸다. 그 외에는
    손대지 않는다 — 자동 교정이 자격증명을 조용히 망가뜨리면 원인을 찾기
    어렵다.
    """
    problems: list[str] = []
    value = raw

    if value.startswith("﻿"):
        value = value.lstrip("﻿")
        problems.append("BOM 제거")
    stripped = value.strip()
    if stripped != value:
        problems.append("앞뒤 공백 제거")
    value = stripped

    for quote in ('"', "'"):
        if len(value) >= 2 and value.startswith(quote) and value.endswith(quote):
            value = value[1:-1].strip()
            problems.append("따옴표 제거")
            break

    # 눈에 안 보이는 문자는 제거하지 않고 **알리기만** 한다. 지웠다가 값이
    # 달라지면 인증 실패 원인을 영원히 못 찾는다.
    if any(unicodedata.category(c) in ("Cc", "Cf") for c in value):
        problems.append("보이지 않는 제어문자가 포함돼 있습니다(그대로 두었습니다)")
    if any(c.isspace() for c in value):
        problems.append("값 안에 공백이 있습니다(그대로 두었습니다)")
    return value, problems


def parse_file(path: Path) -> list[ServiceCredential]:
    """파일 하나 → 서비스별 자격증명.

    값을 반환하되 **출력하지 않는다.** 호출자가 SecretStore 로만 보낸다.
    """
    text = path.read_text(encoding="utf-8-sig", errors="strict")

    hint = None
    stem = path.stem.lower().replace(" ", "")
    for needle, service in FILE_HINTS.items():
        if needle in stem:
            hint = service
            break

    out: dict[str, ServiceCredential] = {}
    current = hint

    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue

        # 주석 처리된 **할당**은 없는 줄로 본다.
        #
        # 사람이 값을 지우는 대신 `#` 를 앞에 붙이는 일이 흔하다. 그걸 살려
        # 읽으면 지운 값이 되살아나고, `unmapped` 에 쌓여 보고서만 지저분해진다.
        #
        # `# NAVER` 같은 **절 머리**는 살린다 — `_SECTION` 이 `#` 를 절 머리
        # 표기로 이미 받고 있고, 그건 값이 아니라 구분선이다. 그래서 구분자
        # (`=` 또는 `:`)가 있는 주석만 버린다.
        stripped = line.strip()
        if stripped.startswith("#") and ("=" in stripped or ":" in stripped):
            continue

        # `NAME=VALUE` 도 받는다. 실제 파일이 두 표기를 섞어 쓴다.
        if ":" not in line and "=" in line:
            name, _, raw = line.partition("=")
            service, field_name = split_service_field(name)
            if service is None:
                out.setdefault("UNKNOWN", ServiceCredential("UNKNOWN")).unmapped.append(
                    name.strip(),
                )
                continue
            svc = out.setdefault(service, ServiceCredential(service))
            value, problems = _clean_value(raw)
            svc.problems.extend(f"{lineno}행: {p}" for p in problems)
            if not value:
                svc.problems.append(f"{lineno}행: {name.strip()} 값이 비어 있습니다")
                continue
            if field_name is None:
                svc.unmapped.append(name.strip())
                continue
            if field_name in svc.fields and svc.fields[field_name] != value:
                # 같은 항목에 다른 값이 두 번 나왔다. 조용히 덮으면 어느 쪽이
                # 쓰이는지 아무도 모른다.
                svc.problems.append(
                    f"{lineno}행: {field_name} 가 앞의 값과 다릅니다(뒤엣것 무시)")
                continue
            svc.fields[field_name] = value
            continue

        if ":" not in line:
            # 절 머리일 수 있다. 아니면 알 수 없는 줄이다.
            m = _SECTION.match(line)
            token = (m.group(1).lower() if m else "")
            matched = next(
                (s for n, s in FILE_HINTS.items() if n == token or n in token), None,
            )
            if matched:
                current = matched
                out.setdefault(matched, ServiceCredential(matched))
            elif token.upper() in REQUIRED:
                current = token.upper()
                out.setdefault(current, ServiceCredential(current))
            else:
                svc = out.setdefault(current or "UNKNOWN",
                                     ServiceCredential(current or "UNKNOWN"))
                svc.problems.append(f"{lineno}행: 이름과 값을 구분할 수 없습니다")
            continue

        name, _, raw = line.partition(":")
        key = name.strip().lower().replace(" ", "").replace("-", "_")

        # 이름 자체가 서비스를 가리키는 경우: `11st: <값>`
        direct = next((s for n, s in FILE_HINTS.items() if n == key), None)
        if direct is None:
            # `11번가 api` 처럼 서비스 이름 + 접미어. 접두 일치만 허용한다 —
            # 부분 포함까지 허용하면 엉뚱한 줄이 딸려 들어온다.
            direct = next((s for n, s in FILE_HINTS.items()
                           if key.startswith(n)), None)
        service = direct or current
        if service is None:
            out.setdefault("UNKNOWN", ServiceCredential("UNKNOWN")).problems.append(
                f"{lineno}행: 어느 서비스인지 알 수 없습니다",
            )
            continue

        svc = out.setdefault(service, ServiceCredential(service))
        value, problems = _clean_value(raw)
        svc.problems.extend(f"{lineno}행: {p}" for p in problems)

        if not value:
            svc.problems.append(f"{lineno}행: 값이 비어 있습니다")
            continue

        field_name = ALIASES.get(service, {}).get(key)
        if direct and field_name is None:
            # `11st: <값>` 처럼 이름이 곧 서비스면 그 서비스의 유일 필수 항목으로 본다.
            required = REQUIRED.get(service, ())
            field_name = required[0] if len(required) == 1 else None

        if field_name is None:
            svc.unmapped.append(name.strip())
            continue

        if field_name in svc.fields:
            # 조용히 덮으면 어느 값이 쓰이는지 아무도 모른다.
            svc.problems.append(f"{lineno}행: {field_name} 가 중복됩니다(뒤엣것 무시)")
            continue
        svc.fields[field_name] = value

    return list(out.values())


def parse_dir(directory: Path) -> list[ServiceCredential]:
    """디렉터리의 모든 파일을 읽어 서비스별로 합친다."""
    merged: dict[str, ServiceCredential] = {}
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        for cred in parse_file(path):
            target = merged.setdefault(cred.service, ServiceCredential(cred.service))
            for k, v in cred.fields.items():
                if k in target.fields:
                    target.problems.append(f"{path.name}: {k} 가 다른 파일과 중복됩니다")
                    continue
                target.fields[k] = v
            target.unmapped.extend(cred.unmapped)
            target.problems.extend(f"{path.name} {p}" for p in cred.problems)
    return list(merged.values())


#: 서비스 → SecretStore 형태. 공식 인증 방식에 맞춘다.
KIND: dict[str, str] = {
    "NAVER": "CLIENT_CREDENTIALS",
    "COUPANG": "ACCESS_SECRET",
    "ELEVENST": "API_KEY",
    "NEXT_ENGINE": "API_KEY",
}


def store_for_account(
    db: Any, *, org_id: UUID, owner_type: str, owner_id: UUID,
    cred: ServiceCredential,
) -> str:
    """SecretStore 에 넣는다. 값은 여기서 프로세스를 떠나지 않는다."""
    from app.core.secret_store import get_secret_store

    payload = dict(cred.fields)
    kind = KIND[cred.service]
    # COUPANG 의 vendor_id 는 비밀이 아니지만 같은 봉투에 넣는다 — 나눠 두면
    # 어느 쪽이 짝인지 추적해야 한다.
    if kind == "ACCESS_SECRET":
        payload = {"access_key": payload["access_key"],
                   "secret_key": payload["secret_key"],
                   "vendor_id": payload["vendor_id"]}
    _ = db
    return get_secret_store().put(
        org_id=org_id, owner_type=owner_type, owner_id=owner_id,
        credential_kind=kind, payload=payload,
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="자격증명 파일 구조 점검 (값은 출력하지 않는다)")
    ap.add_argument("path", help="파일 또는 디렉터리")
    ap.add_argument("--register", action="store_true",
                    help="SecretStore 에 실제로 등록한다(기본은 점검만)")
    ap.add_argument("--org", help="조직 UUID (--register 에 필요)")
    ap.add_argument("--actor", help="실행자 UUID (--register 에 필요)")
    # 하나만 등록하고 싶을 때. 없으면 파일에 있는 서비스를 전부 등록하는데,
    # 그러면 멀쩡한 다른 자격증명까지 **교체(rotate)** 된다. 한 건만 넣으러
    # 들어왔다가 전부 갈아 끼우는 사고를 막는다.
    ap.add_argument("--service", help="이 서비스만 처리 (예: NEXT_ENGINE)")
    args = ap.parse_args()

    target = Path(args.path)
    if not target.exists():
        print(f"경로가 없습니다: {target}")
        return 2

    creds = parse_dir(target) if target.is_dir() else parse_file(target)
    if args.service:
        creds = [c for c in creds if c.service == args.service.upper()]

    print(f"credential_file_found=true  (파일 {1 if target.is_file() else '여러 개'})")
    for cred in sorted(creds, key=lambda c: c.service):
        r = cred.report()
        print(f"\n[{r['service']}]")
        print(f"  fields_present   = {r['fields_present']}")
        print(f"  missing          = {r['missing'] or '없음'}")
        print(f"  unmapped_names   = {r['unmapped_names'] or '없음'}")
        print(f"  complete         = {r['complete']}")
        for p in r["problems"]:
            # 표식은 ASCII 다. `⚠` 는 Windows 기본 콘솔(cp949)에서
            # UnicodeEncodeError 를 내는데, 하필 **경고가 있을 때만** 터진다 —
            # 조용한 실행은 멀쩡하고 문제가 있는 실행만 죽는다.
            print(f"  ! {p}")

    if args.register:
        if not (args.org and args.actor):
            print("--register 에는 --org 와 --actor 가 필요합니다.")
            return 2
        from app.db.session import SessionLocal

        db = SessionLocal()
        try:
            print("--- SecretStore 등록 ---")
            for cred in sorted(creds, key=lambda c: c.service):
                # 판매처(integration_account)와 셀러툴(external_account)은
                # 붙는 테이블이 다르다. 서비스마다 경로가 하나뿐이라 겹치지 않는다.
                if cred.service in MARKETPLACE_PROVIDER:
                    fn = register_marketplace
                elif cred.service in EXTERNAL_SYSTEM_CODE:
                    fn = register_external_system
                else:
                    continue
                out = fn(
                    db, org_id=UUID(args.org), actor_user_id=UUID(args.actor),
                    cred=cred,
                )
                print(f"  {out}")
        finally:
            db.close()
    return 0



# ──────────────────────────────────────────────────────────────────────────
# 판매처 계정에 등록
# ──────────────────────────────────────────────────────────────────────────

#: 서비스 → 판매처 코드(`sale_channel.code` / `integration_account.provider`).
MARKETPLACE_PROVIDER: dict[str, str] = {
    "NAVER": "SMARTSTORE",
    "COUPANG": "COUPANG",
    "ELEVENST": "ELEVENST",
}


def register_marketplace(
    db: Any, *, org_id: UUID, actor_user_id: UUID, cred: ServiceCredential,
) -> dict[str, Any]:
    """판매처 계정을 찾거나 만들고 자격증명을 SecretStore 에 넣는다.

    **값은 이 함수 안에서 프로세스를 떠나지 않는다.** 반환값에도 없다.

    `external_account` 가 아니라 `integration_account` 에 붙인다 — 판매처
    계정에는 주문이 귀속되고, 셀러툴 계정과 책임이 다르다(M047).
    """
    from sqlalchemy import text

    provider = MARKETPLACE_PROVIDER.get(cred.service)
    if provider is None:
        return {"service": cred.service, "registered": False,
                "reason": "판매처 계정 경로가 정의되지 않은 서비스입니다."}
    if not cred.complete:
        return {"service": cred.service, "registered": False,
                "reason": f"필수 항목 누락: {cred.missing}"}

    row = db.execute(text("""
        SELECT id FROM integration_account
         WHERE org_id = :o AND provider = :p AND deleted_at IS NULL
         ORDER BY created_at LIMIT 1
    """), {"o": str(org_id), "p": provider}).scalar()

    if row is None:
        channel = db.execute(text("""
            SELECT id FROM sale_channel WHERE org_id = :o AND code = :c
        """), {"o": str(org_id), "c": provider}).scalar()
        row = db.execute(text("""
            INSERT INTO integration_account
                   (org_id, provider, account_label, sale_channel_id,
                    status, auth_kind, created_by, updated_by)
            VALUES (:o, :p, :label, :ch, 'INACTIVE', 'API_KEY', :a, :a)
            RETURNING id
        """), {"o": str(org_id), "p": provider, "label": f"{provider} 본점",
               "ch": str(channel) if channel else None,
               "a": str(actor_user_id)}).scalar_one()

    account_id = UUID(str(row))
    ref = store_for_account(
        db, org_id=org_id, owner_type="integration_account",
        owner_id=account_id, cred=cred,
    )
    db.execute(text("""
        UPDATE integration_account
           SET credential_secret_ref = :ref, credential_kind = :k,
               credential_updated_at = now(), credential_updated_by = :a,
               updated_at = now(), updated_by = :a
         WHERE id = :i
    """), {"ref": ref, "k": KIND[cred.service], "a": str(actor_user_id),
           "i": str(account_id)})

    from app.core.audit import record_audit

    record_audit(
        db, org_id=org_id, domain_table="integration_account",
        domain_row_id=account_id, action="UPDATE", actor_user_id=actor_user_id,
        # 값도 참조도 남기지 않는다 — 무엇을 했는지만.
        after={"credential_configured": True, "credential_kind": KIND[cred.service]},
        reason="CREDENTIAL_BOOTSTRAPPED",
    )
    db.commit()

    from app.core.secret_store import get_secret_store

    return {
        "service": cred.service,
        "provider": provider,
        "integration_account_id": str(account_id),
        "registered": True,
        # 실제로 읽히는지 확인한다. 참조만 남고 값이 없는 상태를 잡는다.
        "secretstore_exists": get_secret_store().exists(
            org_id=org_id, secret_ref=ref),
    }


# ──────────────────────────────────────────────────────────────────────────
# 외부 시스템 계정에 등록 (셀러툴)
# ──────────────────────────────────────────────────────────────────────────

#: 서비스 → `external_system.code`.
#:
#: 판매처(`integration_account`)와 **다른 테이블이다.** 셀러툴은 우리가 나가서
#: 부르는 outbound 대상이고 주문이 귀속되지 않는다 — M038 이 둘을 나눈 이유다.
EXTERNAL_SYSTEM_CODE: dict[str, str] = {
    "NEXT_ENGINE": "NEXT_ENGINE",
}

#: 계정이 없을 때 만들 기본 코드. 조직이 같은 셀러툴 계정을 여럿 쓸 수 있어
#: `account_code` 가 유일키의 일부다(uq_external_account_code).
DEFAULT_ACCOUNT_CODE = "DEFAULT"


def register_external_system(
    db: Any, *, org_id: UUID, actor_user_id: UUID, cred: ServiceCredential,
) -> dict[str, Any]:
    """셀러툴 계정을 찾거나 만들고 자격증명을 SecretStore 에 넣는다.

    **값은 이 함수 안에서 프로세스를 떠나지 않는다.** 반환값에도 없다.

    새로 만들지 않고 기존 서비스 함수를 부른다
    ------------------------------------
    ```
    external_sync.create_account          감사 기록 포함
    external_sync.set_credential_secret   교체 정책(새 것 먼저, 옛 것 나중) 포함
    ```

    여기서 SQL 을 직접 쓰면 그 두 가지가 조용히 빠진다. 이미 있는 계정이면
    **새로 만들지 않고 교체한다** — `set_credential_secret` 이 rotate 로 처리하고
    옛 비밀을 지운다.
    """
    from sqlalchemy import text

    from app.services import external_sync

    code = EXTERNAL_SYSTEM_CODE.get(cred.service)
    if code is None:
        return {"service": cred.service, "registered": False,
                "reason": "외부 시스템 계정 경로가 정의되지 않은 서비스입니다."}
    if not cred.complete:
        return {"service": cred.service, "registered": False,
                "reason": f"필수 항목 누락: {cred.missing}"}

    system_id = db.execute(text("""
        SELECT id FROM external_system WHERE code = :c AND is_active
    """), {"c": code}).scalar()
    if system_id is None:
        return {"service": cred.service, "registered": False,
                "reason": f"external_system 카탈로그에 {code} 가 없습니다."}

    existing = db.execute(text("""
        SELECT id FROM external_account
         WHERE org_id = :o AND external_system_id = :s AND deleted_at IS NULL
         ORDER BY created_at LIMIT 1
    """), {"o": str(org_id), "s": str(system_id)}).scalar()

    if existing is None:
        created = external_sync.create_account(
            db, org_id=org_id, actor_user_id=actor_user_id,
            external_system_id=UUID(str(system_id)),
            display_name=f"{code} 기본 계정", account_code=DEFAULT_ACCOUNT_CODE,
        )
        account_id = UUID(created["id"])
        reused = False
    else:
        account_id = UUID(str(existing))
        reused = True

    kind = KIND[cred.service]
    account = external_sync.set_credential_secret(
        db, org_id=org_id, actor_user_id=actor_user_id, account_id=account_id,
        credential_kind=kind, payload=dict(cred.fields),
    )

    from app.core.secret_store import get_secret_store

    ref = db.execute(text("""
        SELECT credential_secret_ref FROM external_account WHERE id = :i
    """), {"i": str(account_id)}).scalar()

    return {
        "service": cred.service,
        "system_code": code,
        "external_account_id": str(account_id),
        "account_reused": reused,
        "credential_kind": kind,
        "registered": True,
        "credential_configured": account["credential_configured"],
        # 참조만 남고 값이 없는 상태를 잡는다. 참조 문자열 자체는 돌려주지 않는다.
        "secretstore_exists": get_secret_store().exists(
            org_id=org_id, secret_ref=ref),
    }


if __name__ == "__main__":
    sys.exit(main())
