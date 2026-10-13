"""로컬 운영 진입점 — 환경 점검과 상태 조회.

무엇을 고정하나
--------------
이 두 스크립트의 출력은 **로그 파일에 그대로 남고** 기동 스크립트가 그
값으로 판단한다. 그래서 두 가지를 고정한다.

```
1. 비밀값이 출력에 섞이지 않는다      (로그가 마지막 방어선이다)
2. 판정이 사실을 뭉개지 않는다        (막아야 할 것과 알려야 할 것을 나눈다)
```

1 이 특히 중요하다. "출력에 비밀을 넣지 말자" 는 규칙은 지켜지지 않는다 —
테스트가 지켜야 한다.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.scripts import ops_status, preflight

# ── 환경 점검 ───────────────────────────────────────────────────────────

class FakeSettings:
    """설정 한 벌. 실제 `.env` 를 건드리지 않는다."""

    def __init__(self, **over):
        self.DATABASE_URL = "postgresql+psycopg://u:pw@localhost:5432/picksome_ops"
        self.JWT_SECRET = "x" * 40
        self.CREDENTIAL_MASTER_KEY = "k" * 44
        self.APP_TIMEZONE = "Asia/Seoul"
        self.ATTACHMENT_STORAGE_BACKEND = "local"
        self.ATTACHMENT_STORAGE_DIR = "./var/attachments"
        for key, value in over.items():
            setattr(self, key, value)


def test_the_database_check_names_the_database_but_not_the_password():
    """접속 문자열을 통째로 찍으면 비밀번호가 로그에 남는다."""
    check = preflight._database(FakeSettings())

    assert check["status"] == preflight.OK
    assert check["detail"] == "picksome_ops"
    assert "pw" not in check["detail"]
    assert "localhost" not in check["detail"]


def test_a_placeholder_jwt_secret_blocks_startup():
    """기본값을 그대로 쓰면 누구나 토큰을 위조한다. 경고로 넘기지 않는다."""
    check = preflight._jwt(FakeSettings(JWT_SECRET="change-me-in-env"))

    assert check["status"] == preflight.FAIL


def test_a_missing_master_key_warns_but_does_not_block():
    """앱은 뜬다. 다만 외부 동기화가 자격증명을 못 읽는다.

    막아 버리면 키 없이도 할 수 있는 일(장부 확인·정산)까지 못 하게 된다.
    """
    check = preflight._master_key(FakeSettings(CREDENTIAL_MASTER_KEY=None))

    assert check["status"] == preflight.WARN


def test_the_master_key_check_never_reveals_the_key():
    """길이만 흘려도 무엇을 쓰는지 좁혀진다."""
    secret = "super-secret-master-key-value-000000000000"
    check = preflight._master_key(FakeSettings(CREDENTIAL_MASTER_KEY=secret))

    assert secret not in check["detail"]
    assert str(len(secret)) not in check["detail"]


def test_a_malformed_database_url_is_a_failure_not_a_warning():
    check = preflight._database(FakeSettings(DATABASE_URL="not-a-url"))

    assert check["status"] == preflight.FAIL


def test_warnings_alone_do_not_fail_the_preflight(monkeypatch):
    """켜지 않은 선택 기능까지 기동을 막으면 아무도 이 점검을 안 켠다."""
    monkeypatch.setattr(
        preflight, "get_settings", lambda: FakeSettings(CREDENTIAL_MASTER_KEY=None),
        raising=False)
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: FakeSettings(CREDENTIAL_MASTER_KEY=None))

    result = preflight.collect()

    assert result["ok"] is True
    assert "Master Key" in result["warned"]


# ── 상태 조회 ───────────────────────────────────────────────────────────

def test_schema_states_separate_behind_from_ahead(db: Session, monkeypatch):
    """되돌리기가 필요한 상태와 올리기만 하면 되는 상태는 다르다.

    앞선 DB 에 뒤처진 코드를 붙이면 없는 컬럼을 읽는다. downgrade 로 맞추면
    데이터를 잃는다(docs/55 §5) — 그래서 둘을 절대 뭉치지 않는다.
    """
    monkeypatch.setattr(ops_status, "_code_head", lambda: "M900")
    assert ops_status._schema_state(db)["state"] == "BEHIND"

    monkeypatch.setattr(ops_status, "_code_head", lambda: "M000")
    assert ops_status._schema_state(db)["state"] == "AHEAD"

    monkeypatch.setattr(ops_status, "_code_head", lambda: None)
    assert ops_status._schema_state(db)["state"] == "UNKNOWN"


def test_the_code_head_is_actually_readable():
    """리비전을 못 읽으면 상태 판정이 통째로 UNKNOWN 이 된다."""
    assert ops_status._code_head() is not None


def test_a_never_run_backup_is_not_reported_as_current(tmp_path):
    """백업이 한 번도 안 돌았는데 CURRENT 로 보이면 아무도 안 고친다."""
    state = ops_status._backup(str(tmp_path))

    assert state["state"] == "NEVER_RUN"
    assert state["last_success_at"] is None


def test_a_recent_backup_is_current(tmp_path):
    daily = tmp_path / "daily"
    daily.mkdir()
    (daily / "picksome_ops_2026-08-17.dump").write_bytes(b"x")

    state = ops_status._backup(str(tmp_path))

    assert state["state"] == "CURRENT"
    assert state["count"] == 1


def test_an_old_backup_is_reported_as_due(tmp_path):
    import os
    import time

    daily = tmp_path / "daily"
    daily.mkdir()
    dump = daily / "picksome_ops_old.dump"
    dump.write_bytes(b"x")
    stale = time.time() - (ops_status.BACKUP_DUE_HOURS + 6) * 3600
    os.utime(dump, (stale, stale))

    assert ops_status._backup(str(tmp_path))["state"] == "DUE"


def test_the_status_payload_carries_the_database_name_but_no_credentials(db: Session):
    """이 payload 는 JSON 으로 로그와 화면에 흘러간다."""
    state = ops_status.collect(db, backup_root="backup")

    blob = str(state)
    assert "postgresql+psycopg://" not in blob
    assert state["database"]
    assert "://" not in state["database"]


def test_degraded_reasons_are_codes_not_sentences(db: Session):
    """화면·스크립트가 문구로 분기하면 문구를 바꾸는 순간 깨진다."""
    state = ops_status.collect(db, backup_root="backup")

    for reason in state["degraded"]:
        assert reason == reason.upper()
        assert " " not in reason


@pytest.mark.parametrize("state_name", ["CURRENT", "BEHIND", "AHEAD", "UNKNOWN"])
def test_only_current_is_healthy(db: Session, monkeypatch, state_name):
    """`CURRENT` 가 아닌 스키마 상태는 전부 확인 대상이다."""
    monkeypatch.setattr(
        ops_status, "_schema_state",
        lambda _db: {"db_revision": "M1", "code_head": "M1", "state": state_name})

    state = ops_status.collect(db, backup_root="backup")
    flagged = any(r.startswith("SCHEMA_") for r in state["degraded"])

    assert flagged == (state_name != "CURRENT")
