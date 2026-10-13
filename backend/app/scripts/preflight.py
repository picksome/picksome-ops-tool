"""기동 전 환경 점검 — **DB 에 붙지 않는다.**

왜 DB 없이 도나
--------------
이 점검은 DB 를 켜기 **전에** 돈다. 접속 문자열이 비어 있는데 DB 를 먼저
띄우면, 실패가 "DB 가 안 뜬다" 로 보이고 사람이 엉뚱한 곳을 고친다.

무엇을 보나
----------
```
DATABASE_URL        있는가 · 형식이 맞는가 · 어느 DB 를 가리키나
JWT_SECRET          기본값이 아닌가
APP_TIMEZONE        Asia/Seoul 인가 (마감 귀속월 판정이 이 값을 따른다)
CREDENTIAL_MASTER_KEY  있는가            ← 없으면 무인 동기화가 전부 막힌다
증빙 저장 경로        쓸 수 있는가
```

값을 출력하지 않는다
------------------
있다/없다와 **DB 이름까지만** 말한다. 비밀번호·키·토큰은 길이도 찍지
않는다 — 길이만으로도 무엇을 쓰는지 좁혀진다. 이 스크립트의 출력은
로그 파일에 그대로 남으므로 여기가 마지막 방어선이다.

무엇을 고치지 않나
----------------
아무것도 안 고친다. `.env` 를 쓰지도, 키를 만들지도 않는다. 없는 키를
자동 생성하면 기존 암호문을 영영 못 읽는다(docs/60).

종료 코드
--------
```
0  전부 통과
1  기동을 막아야 하는 문제가 있다
```

경고(WARN)는 종료 코드를 바꾸지 않는다. 켜지 않은 선택 기능(SMTP·MCP)까지
기동을 막으면 아무도 이 점검을 안 켜게 된다.

실행
----
```
python -m app.scripts.preflight
python -m app.scripts.preflight --json
```
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any
from urllib.parse import urlsplit

OK, WARN, FAIL = "OK", "WARN", "FAIL"

#: 이 값이 그대로면 토큰을 위조할 수 있다. `config.py` 의 default 다.
_JWT_PLACEHOLDER = "change-me-in-env"


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"check": name, "status": status, "detail": detail}


def _database(settings: Any) -> dict[str, str]:
    url = settings.DATABASE_URL or ""
    if not url:
        return _check("Database Config", FAIL, "DATABASE_URL 이 비어 있습니다")
    parts = urlsplit(url)
    database = (parts.path or "/").lstrip("/")
    if not parts.hostname or not database:
        return _check("Database Config", FAIL,
                      "DATABASE_URL 형식이 올바르지 않습니다 (host/db 누락)")
    if not parts.password:
        # 비밀번호 없이 붙는 구성(trust/peer)도 있으므로 막지는 않는다.
        return _check("Database Config", WARN,
                      f"{database} — 접속 비밀번호가 설정돼 있지 않습니다")
    # **DB 이름까지만.** 사용자·호스트·비밀번호는 찍지 않는다.
    return _check("Database Config", OK, database)


def _jwt(settings: Any) -> dict[str, str]:
    secret = settings.JWT_SECRET or ""
    if not secret or secret == _JWT_PLACEHOLDER:
        return _check("JWT Secret", FAIL,
                      "기본값 그대로입니다. 이 값을 알면 누구나 토큰을 위조합니다")
    if len(secret) < 32:
        return _check("JWT Secret", WARN, "32자 미만입니다")
    return _check("JWT Secret", OK, "설정됨")


def _master_key(settings: Any) -> dict[str, str]:
    """자격증명 마스터 키. **값도 길이도 출력하지 않는다.**

    없으면 무인 동기화가 통째로 막힌다 — 자격증명을 복호화할 수 없어
    `CREDENTIAL_STORE_NOT_CONFIGURED` 로 거부된다. 그런데 앱 자체는 뜬다.
    그래서 기동은 막지 않고 경고로 남긴다: 화면은 열리지만 주문이 안 들어온다.
    """
    if not settings.CREDENTIAL_MASTER_KEY:
        return _check("Master Key", WARN,
                      "없음 — 외부 동기화가 자격증명을 읽지 못합니다")
    return _check("Master Key", OK, "설정됨")


def _timezone(settings: Any) -> dict[str, str]:
    if settings.APP_TIMEZONE != "Asia/Seoul":
        return _check("Timezone", WARN,
                      f"{settings.APP_TIMEZONE} — 마감 귀속월 판정이 이 값을 따릅니다")
    return _check("Timezone", OK, settings.APP_TIMEZONE)


def _attachment_dir(settings: Any) -> dict[str, str]:
    """증빙 저장 경로가 쓸 수 있는가. **없으면 만들지 않는다.**

    기동 스크립트가 조용히 디렉터리를 만들면, 경로 오타가 새 빈 폴더로
    덮여서 기존 증빙이 사라진 것처럼 보인다.
    """
    if settings.ATTACHMENT_STORAGE_BACKEND != "local":
        return _check("Attachment Storage", OK,
                      settings.ATTACHMENT_STORAGE_BACKEND)
    path = pathlib.Path(settings.ATTACHMENT_STORAGE_DIR)
    if not path.exists():
        return _check("Attachment Storage", WARN,
                      f"{path} 가 아직 없습니다 (첫 첨부 때 만들어집니다)")
    return _check("Attachment Storage", OK, str(path))


def _env_file() -> dict[str, str]:
    from app.config import _ENV_FILE

    if not _ENV_FILE.exists():
        return _check("Env File", WARN,
                      f"{_ENV_FILE.name} 이 없습니다 — 실제 환경변수만 사용합니다")
    return _check("Env File", OK, str(_ENV_FILE))


def collect() -> dict[str, Any]:
    from app.config import get_settings

    settings = get_settings()
    checks = [
        _env_file(),
        _database(settings),
        _jwt(settings),
        _master_key(settings),
        _timezone(settings),
        _attachment_dir(settings),
    ]
    failed = [c for c in checks if c["status"] == FAIL]
    return {
        "checks": checks,
        "failed": [c["check"] for c in failed],
        "warned": [c["check"] for c in checks if c["status"] == WARN],
        "ok": not failed,
    }


def _print_human(result: dict[str, Any]) -> None:
    print("")
    for c in result["checks"]:
        print(f"  {c['check']:<20} {c['status']:<5} {c['detail']}")
    print("")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="preflight", description="기동 전 환경 점검 (DB 접속 없음)")
    parser.add_argument("--json", action="store_true", help="JSON 만 출력")
    args = parser.parse_args(argv)

    try:
        result = collect()
    except Exception as exc:  # noqa: BLE001 — 설정 로딩 자체가 실패할 수 있다
        print(json.dumps({"error": type(exc).__name__, "ok": False},
                         ensure_ascii=False))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.json:
        _print_human(result)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
