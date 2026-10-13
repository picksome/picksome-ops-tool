"""마이그레이션 대상 DB 판별과 파괴적 작업 차단.

왜 이 파일이 있나
--------------
2026-08-11 에 실제로 일어난 일:

```
ALEMBIC_DATABASE_URL 을 지정   ← 임시 DB 를 향한다고 믿었다
env.py 가 그걸 무시            ← 무조건 settings.DATABASE_URL 이었다
개발 DB 에 downgrade 실행      ← 검증하려던 대상이 아닌 DB 가 내려갔다
M048 테이블 drop
```

그때는 그 테이블에 스모크 데이터만 있어서 손실이 없었다. **운영이었으면
복구 불가능한 사고다.**

env.py 를 고쳐 URL 은 이제 존중된다. 하지만 그것만으로는 부족하다 — 사람이
임시 DB 이름을 잘못 적거나, 임시 DB 가 아예 존재하지 않아 다른 DB 로 흘러가는
경우를 막지 못한다.

그래서 **되돌리기(downgrade)를 자동으로 실행하는 경로에는 세 겹의 문**을 둔다.

```
1. 대상이 scratch 이름인가        _SCRATCH_PATTERNS
2. 운영 환경이 아닌가              APP_ENV
3. 개발 메인 DB 가 아닌가          settings.DATABASE_URL 과 비교
```

셋 중 하나라도 아니면 **실행하지 않는다.** fail-closed 다 — 판단이 안 서면
멈춘다. 마이그레이션 검증은 편의보다 안전이 앞선다.

사람이 직접 치는 CLI 는 막지 않는다
------------------------------
`alembic downgrade` 를 사람이 의도적으로 실행하는 것까지 막으면 정상적인
개발이 불가능해진다. 이 가드는 **자동 검증 스크립트**에만 적용된다. 둘을
구분하는 것이 이 파일의 요점이다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

#: 파괴적 검증을 허용할 DB 이름. 이 패턴에 맞지 않으면 downgrade 를 돌리지 않는다.
#:
#: 이름으로 판별하는 것이 조잡해 보이지만, 실수는 대개 **이름을 착각해서**
#: 일어난다. `picksome_ops` 와 `picksome_migcheck` 를 헷갈린 사람에게
#: "이 DB 는 지워도 되는 DB 인가" 를 이름으로 되묻는 것이 가장 확실하다.
_SCRATCH_PATTERNS = (
    re.compile(r"_migcheck(_[a-z0-9]+)?$"),
    re.compile(r"_migration_test(_[a-z0-9]+)?$"),
)

#: 운영으로 간주하는 값. 여기서는 파괴적 검증을 절대 하지 않는다.
_PRODUCTION_ENVS = ("prod", "production", "staging")


class UnsafeMigrationTargetError(RuntimeError):
    """파괴적 마이그레이션을 돌려선 안 되는 대상이다."""


@dataclass(frozen=True)
class MigrationTarget:
    """마이그레이션이 향하는 곳. **비밀은 담지 않는다.**

    `url` 을 통째로 들고 다니면 로그·예외 메시지·리포트 어딘가에서 비밀번호가
    새어 나간다. 그래서 접속에 필요한 URL 은 호출자가 따로 갖고, 이 객체는
    "사람이 보고 판단할 정보" 만 담는다.
    """

    host: str
    port: int
    database: str
    app_env: str
    is_scratch: bool
    is_development_main: bool

    def describe(self) -> str:
        """사람이 읽는 한 줄. 비밀번호·사용자명은 넣지 않는다."""
        kind = (
            "scratch" if self.is_scratch
            else "개발 메인" if self.is_development_main
            else "기타"
        )
        return (
            f"{self.host}:{self.port}/{self.database} "
            f"[{kind}, APP_ENV={self.app_env}]"
        )


def is_scratch_name(database: str) -> bool:
    """이름만 보고 "지워도 되는 DB" 인지 판단."""
    return any(p.search(database) for p in _SCRATCH_PATTERNS)


def describe_target(url: str, *, app_env: str | None = None,
                    development_url: str | None = None) -> MigrationTarget:
    """접속 URL 에서 **비밀이 아닌 것만** 뽑아낸다.

    `urlsplit` 은 `postgresql+psycopg://` 같은 scheme 도 그대로 다룬다.
    비밀번호는 읽지도 않는다.
    """
    parts = urlsplit(url)
    database = (parts.path or "/").lstrip("/")
    env = (app_env or os.environ.get("APP_ENV") or "dev").strip().lower()

    dev_db = None
    if development_url:
        dev_db = (urlsplit(development_url).path or "/").lstrip("/")

    return MigrationTarget(
        host=parts.hostname or "?",
        port=parts.port or 5432,
        database=database or "?",
        app_env=env,
        is_scratch=is_scratch_name(database),
        # 개발 메인 DB 는 "지워도 되는 DB" 가 아니다. 사람의 작업 데이터가 있다.
        is_development_main=dev_db is not None and database == dev_db,
    )


def assert_safe_for_destructive(target: MigrationTarget) -> None:
    """파괴적 검증(downgrade/drop)을 돌려도 되는 대상인지 확인.

    셋 다 통과해야 한다. 순서가 있다 — 운영 여부를 먼저 본다. 이름이 우연히
    `_migcheck` 로 끝나는 운영 DB 가 있어도 막히도록.
    """
    if target.app_env in _PRODUCTION_ENVS:
        raise UnsafeMigrationTargetError(
            f"APP_ENV={target.app_env} 에서는 되돌리기 검증을 하지 않습니다. "
            f"대상: {target.describe()}",
        )
    if target.is_development_main:
        raise UnsafeMigrationTargetError(
            "개발 메인 DB 를 되돌리기 검증 대상으로 삼을 수 없습니다. "
            "사람의 작업 데이터가 들어 있습니다. "
            f"대상: {target.describe()}",
        )
    if not target.is_scratch:
        raise UnsafeMigrationTargetError(
            f"'{target.database}' 는 임시 검증용 DB 이름이 아닙니다. "
            "이름이 _migcheck 또는 _migration_test 로 끝나야 합니다 — "
            "지워도 되는 DB 라는 것을 이름으로 확인합니다.",
        )
