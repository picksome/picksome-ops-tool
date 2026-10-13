"""사용자 비밀번호 재설정 — **운영자가 직접 실행하는 복구 수단이다.**

왜 필요한가
----------
이 앱에는 "비밀번호 찾기" 가 없다. 시드 스크립트가 만든 admin 의 초기
비밀번호는 콘솔에 1회만 출력되고 어디에도 저장되지 않으며(`picksome_init`),
시드를 다시 돌려도 조직이 이미 있으면 즉시 종료하므로 덮어쓰이지 않는다.

즉 비밀번호를 잃으면 **들어갈 방법이 없다.** 그 하나를 메운다.

무엇을 하지 않나
--------------
```
평문 저장 · 로그 기록 · audit 기록      안 한다
명령행 인자로 비밀번호 받기             안 한다 (프로세스 목록에 보인다)
대상 이메일 기본값                      없다 (실수로 엉뚱한 계정을 바꾸지 않게)
없는 사용자 자동 생성                   안 한다 (오타를 새 계정으로 만들면 더 나쁘다)
비밀번호 정책 재정의                    안 한다 (core.security.hash_password 를 그대로 쓴다)
```

비밀번호를 어디서 받나
-------------------
```
1. 터미널이 있으면      getpass 로 두 번 입력받는다 (화면에 안 찍힌다)
2. ADMIN_RESET_PASSWORD  환경변수 (무인 실행용)
3. --generate            임의 생성 후 **1회만** 출력
```

3 을 쓰면 그 값이 터미널 스크롤백에 남는다. 로그인한 뒤 바꾸는 것이 원칙인데
현재 화면에 변경 기능이 없으므로, 바꾸려면 이 스크립트를 다시 돌려야 한다.

audit 은 남기되 값은 남기지 않는다
-------------------------------
`PERMISSION_CHANGE` 로 "언제 누가 재설정했다" 만 적는다. before/after 에
해시조차 넣지 않는다 — 감사 로그는 권한이 넓은 화면에서 읽힌다.

실행
----
```
python -m scripts.reset_user_password --email admin@picksome.co.kr
python -m scripts.reset_user_password --email admin@picksome.co.kr --generate
python -m scripts.reset_user_password --list          # 계정 목록만 (변경 없음)
```
"""

from __future__ import annotations

import argparse
import getpass
import os
import pathlib
import secrets
import sys

from sqlalchemy import text

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from app.core.audit import record_audit  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402

#: 무인 실행용 환경변수. `picksome_init` 의 `ADMIN_SEED_PASSWORD` 와 같은 규약.
ENV_PASSWORD = "ADMIN_RESET_PASSWORD"


def _list_users(db) -> int:
    """계정 목록. **해시는 출력하지 않는다.**"""
    rows = db.execute(text("""
        SELECT email, display_name, role, is_active, last_login_at
          FROM app_user WHERE deleted_at IS NULL
         ORDER BY role, created_at
    """)).mappings().all()
    if not rows:
        print("계정이 없습니다.")
        return 1
    print(f"{'이메일':<50} {'이름':<14} {'역할':<10} 활성  마지막 로그인")
    for r in rows:
        last = r["last_login_at"].strftime("%Y-%m-%d %H:%M") if r["last_login_at"] else "—"
        print(f"{r['email']:<50} {r['display_name'] or '':<14} "
              f"{r['role']:<10} {'Y' if r['is_active'] else 'N'}     {last}")
    return 0


def _read_password(*, generate: bool) -> tuple[str, bool]:
    """새 비밀번호와 "생성한 것인가" 를 돌려준다.

    입력받은 것은 출력하지 않는다. 생성한 것만 호출자가 1회 출력한다.
    """
    if generate:
        # `secrets` 를 쓴다. `random` 은 예측 가능하다.
        return secrets.token_urlsafe(12), True

    from_env = os.environ.get(ENV_PASSWORD)
    if from_env:
        return from_env, False

    if not sys.stdin.isatty():
        raise SystemExit(
            f"터미널이 아니어서 비밀번호를 입력받을 수 없습니다.\n"
            f"  {ENV_PASSWORD} 환경변수를 쓰거나 --generate 를 주세요.")

    first = getpass.getpass("새 비밀번호 (화면에 표시되지 않습니다): ")
    second = getpass.getpass("한 번 더: ")
    if first != second:
        raise SystemExit("두 입력이 다릅니다. 아무것도 바꾸지 않았습니다.")
    return first, False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="reset_user_password", description=__doc__.splitlines()[0])
    ap.add_argument("--email", help="대상 계정. 기본값 없음 — 반드시 지정한다")
    ap.add_argument("--generate", action="store_true",
                    help="임의 비밀번호를 만들어 1회 출력한다")
    ap.add_argument("--list", action="store_true",
                    help="계정 목록만 보고 끝낸다 (아무것도 바꾸지 않는다)")
    args = ap.parse_args(argv)

    try:
        db = SessionLocal()
    except Exception as exc:  # noqa: BLE001
        print(f"DB 접속 실패: {type(exc).__name__}", file=sys.stderr)
        return 2

    try:
        if args.list:
            return _list_users(db)

        if not args.email:
            print("--email 을 지정하세요. 목록은 --list 로 볼 수 있습니다.",
                  file=sys.stderr)
            return 2

        user = db.execute(text("""
            SELECT id, org_id, email, display_name, role
              FROM app_user WHERE email = :e AND deleted_at IS NULL
        """), {"e": args.email}).mappings().first()
        if user is None:
            # 만들지 않는다. 오타를 새 계정으로 바꾸면 원본은 그대로 잠긴 채
            # 로그인되는 껍데기가 하나 더 생긴다.
            print(f"그런 계정이 없습니다: {args.email}", file=sys.stderr)
            print("--list 로 정확한 이메일을 확인하세요.", file=sys.stderr)
            return 1

        password, generated = _read_password(generate=args.generate)

        # 길이 정책은 `hash_password` 가 판정한다. 여기서 다시 정하지 않는다.
        try:
            new_hash = hash_password(password)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

        db.execute(text("""
            UPDATE app_user
               SET password_hash = :h, is_active = TRUE, updated_at = now()
             WHERE id = :i
        """), {"h": new_hash, "i": str(user["id"])})

        # **값도 해시도 남기지 않는다.** 언제 누가 재설정했는지만.
        record_audit(
            db, org_id=user["org_id"], domain_table="app_user",
            domain_row_id=user["id"], action="PERMISSION_CHANGE",
            actor_user_id=user["id"],
            after={"action": "PASSWORD_RESET", "by": "operator_cli"},
            reason="운영자가 CLI 로 비밀번호를 재설정했습니다.",
        )
        db.commit()
    finally:
        db.close()

    print("")
    print(f"  비밀번호를 재설정했습니다: {user['email']} ({user['role']})")
    if generated:
        print("")
        print(f"  새 비밀번호   {password}")
        print("")
        print("  이 값은 여기서만 보입니다. 저장한 뒤 이 화면을 지우세요.")
    print("")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
