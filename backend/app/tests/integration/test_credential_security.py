"""자격증명 보안 · 외부 연동 데이터 품질 (M040).

가장 중요한 테스트
---------------
`test_a_submitted_secret_never_comes_back` — **실제로 새고 있던 경로**를 막는다.

`validation_error_handler` 가 `RequestValidationError.errors()` 를 그대로
응답에 넣고 있었고, Pydantic v2 의 오류 항목에는 제출한 값이 `input` 으로
들어 있다. 자격증명이 형식 검증에 걸리는 순간 그 값이 422 본문으로 돌아왔다.

이 프로젝트는 비밀을 저장하지 않는다
--------------------------------
암호화 저장소가 없다. 의도적이다(CLAUDE.md: "보관 위치만 저장한다").
그래서 자격증명 API 는 **vault 항목을 가리킬 뿐** 값을 받지 않는다.
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.redaction import redact, redact_validation_errors
from app.core.security import create_access_token
from app.deps import get_db
from app.main import app
from app.services import external_sync as ex
from app.tests.fixtures.factories import make_app_user, make_organization, make_product

#: 어디에도 나타나면 안 되는 값. 실제 비밀이 아니다.
SECRET = "SECRET_TEST_VALUE_123"


@pytest.fixture
def client(db: Session):
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def org(db: Session) -> uuid.UUID:
    return make_organization(db)


@pytest.fixture
def admin_token(db: Session, org: uuid.UUID) -> str:
    uid = make_app_user(db, org_id=org, role="ADMIN")
    db.commit()
    return create_access_token(user_id=uid, org_id=org, role="ADMIN")


def auth(t: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def account(db: Session, org: uuid.UUID) -> dict:
    actor = make_app_user(db, org_id=org, role="ADMIN")
    sysid = db.execute(text("""
        INSERT INTO external_system (code, name, system_type, supports_api_key)
        VALUES ('CRED_TEST', '자격증명 테스트', 'SELLER_TOOL', TRUE)
        ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
    """)).scalar()
    db.commit()
    return ex.create_account(
        db, org_id=org, actor_user_id=actor,
        external_system_id=uuid.UUID(str(sysid)),
        display_name="자격증명 계정", account_code="CRED-1",
    )


@pytest.fixture
def vault_entry(db: Session, org: uuid.UUID) -> uuid.UUID:
    row = db.execute(text("""
        INSERT INTO password_vault_entry
               (org_id, service_name, vault_provider, vault_item_name, account_masked)
        VALUES (:o, '테스트 셀러툴', 'BITWARDEN', 'SellerTool - 본사', 'ad***@example.com')
        RETURNING id
    """), {"o": str(org)}).scalar()
    db.commit()
    return uuid.UUID(str(row))


# ──────────────────────────────────────────────────────────────────────────
# 유출 회귀 — 이 파일의 핵심
# ──────────────────────────────────────────────────────────────────────────

def test_a_submitted_secret_never_comes_back(client, admin_token, account):
    """검증 오류 응답이 제출한 값을 되돌려주지 않는다.

    이게 실제로 새던 경로다. Pydantic v2 는 오류 항목에 `input` 으로 제출값을
    담고, 핸들러가 그걸 그대로 내보내고 있었다.
    """
    res = client.put(
        f"/api/v1/external/accounts/{account['id']}/credential",
        json={
            # secret 이 dict 가 아니라 검증에 걸린다.
            "credential_kind": "API_KEY",
            "secret": SECRET,
            "api_secret": SECRET,
            "password": SECRET,
        },
        headers=auth(admin_token),
    )
    assert res.status_code == 422
    assert SECRET not in res.text, "제출한 비밀이 오류 응답으로 돌아왔다"


def test_secret_never_reaches_the_audit_log(db, client, admin_token, account, vault_entry, org):
    """감사에는 무엇이 일어났는지만 남긴다 — 값은 절대 넣지 않는다."""
    res = client.put(
        f"/api/v1/external/accounts/{account['id']}/credential",
        json={"credential_kind": "API_KEY", "secret": {"api_key": SECRET}},
        headers=auth(admin_token),
    )
    assert res.status_code == 200, res.text

    rows = db.execute(text("""
        SELECT before_json::text, after_json::text, reason FROM audit_log
         WHERE org_id = :o AND domain_table = 'external_account'
    """), {"o": str(org)}).all()
    blob = " ".join(str(c) for r in rows for c in r)
    assert SECRET not in blob
    # 저장소 참조도 감사에 넣지 않는다 — 무엇을 노려야 하는지 알려 준다.
    assert "cs_" not in blob and "mem_" not in blob
    assert "CREDENTIAL_CONFIGURED" in blob


def test_account_response_has_no_credential_reference(
    db, client, admin_token, account, vault_entry, org
):
    actor = make_app_user(db, org_id=org, role="ADMIN")
    ex.link_vault_note(db, org_id=org, actor_user_id=actor,
                       account_id=uuid.UUID(account["id"]), vault_entry_id=vault_entry)
    ex.set_credential_secret(
        db, org_id=org, actor_user_id=actor, account_id=uuid.UUID(account["id"]),
        credential_kind="API_KEY", payload={"api_key": SECRET},
    )
    ref = db.execute(text("""
        SELECT credential_secret_ref FROM external_account WHERE id = :i
    """), {"i": account["id"]}).scalar()

    res = client.get(f"/api/v1/external/accounts/{account['id']}",
                     headers=auth(admin_token))
    body = res.text

    assert res.json()["data"]["credential_configured"] is True
    assert SECRET not in body
    assert str(vault_entry) not in body, "vault 항목 id 가 응답에 있다"
    assert ref not in body, "저장소 참조가 응답에 있다"
    for banned in ("credentials_vault_entry_id", "credential_secret_ref",
                   "secret", "password", "token"):
        assert banned not in body, f"{banned} 가 응답에 있다"


# ──────────────────────────────────────────────────────────────────────────
# redaction 유틸
# ──────────────────────────────────────────────────────────────────────────

def test_redaction_hides_secrets_but_keeps_business_data():
    """값의 생김새로 추측하지 않는다 — 키 이름으로 판단한다.

    긴 랜덤 문자열을 가리면 상품 설명·메모 같은 정상 데이터가 `***` 가 되고,
    사용자는 왜 자기 데이터가 안 보이는지 알 수 없다.
    """
    payload = {
        "api_key": SECRET,
        "client_secret": SECRET,
        "product_name": "무선 이어폰 케이스 (한정판)",
        "memo": "재고 확인 필요",
        "nested": {"refresh_token": SECRET, "quantity": 12},
        "items": [{"password": SECRET, "sku": "SKU-1"}],
    }
    out = redact(payload)

    assert out["api_key"] == "***"
    assert out["client_secret"] == "***"
    assert out["nested"]["refresh_token"] == "***"
    assert out["items"][0]["password"] == "***"
    # 업무 데이터는 그대로다.
    assert out["product_name"] == "무선 이어폰 케이스 (한정판)"
    assert out["memo"] == "재고 확인 필요"
    assert out["nested"]["quantity"] == 12
    assert out["items"][0]["sku"] == "SKU-1"
    # 원본을 훼손하지 않는다 — 로깅하려다 데이터가 바뀌면 안 된다.
    assert payload["api_key"] == SECRET


def test_status_flags_are_not_mistaken_for_secrets():
    """`credential_configured` 를 가려 버리면 화면이 상태를 표시할 수 없다."""
    out = redact({"credential_configured": True, "credential_present": False})
    assert out["credential_configured"] is True
    assert out["credential_present"] is False


def test_validation_errors_never_echo_the_submitted_value():
    """제출값을 **통째로** 뺀다.

    비밀 키만 골라 지우면, 사용자가 비밀을 엉뚱한 칸에 붙여넣었을 때 그대로
    새어 나간다. 키 이름 판단은 제출값에는 통하지 않는다.

    `loc`(어느 필드)·`msg`(무엇이 잘못됐나)는 남으므로 고치는 데 지장이 없다.
    """
    errors = [
        {"loc": ("body", "api_secret"), "msg": "too short", "type": "x", "input": SECRET},
        {"loc": ("body", "vault_entry_id"), "msg": "not a UUID", "type": "y",
         "input": SECRET},
    ]
    out = redact_validation_errors(errors)

    assert all("input" not in e for e in out)
    assert out[0]["msg"] == "too short"
    assert out[1]["loc"] == ("body", "vault_entry_id")


# ──────────────────────────────────────────────────────────────────────────
# 자격증명 흐름
# ──────────────────────────────────────────────────────────────────────────

def test_only_the_credential_endpoint_accepts_a_secret(client, admin_token, account):
    """비밀은 **한 곳으로만** 들어온다.

    계정 생성·수정에 섞으면 일반 요청 로그가 전부 비밀 취급 대상이 된다.
    """
    from app.api.v1.external import AccountIn, CredentialIn, VaultNoteIn

    assert set(CredentialIn.model_fields) == {"credential_kind", "secret"}
    for model in (AccountIn, VaultNoteIn):
        assert not any(
            re.search(r"secret|password|token|key", f) for f in model.model_fields
        ), f"{model.__name__} 에 비밀 필드가 있다"


def test_no_middleware_reads_the_request_body(client):
    """요청 본문이 로그로 흘러갈 경로 **자체**를 만들지 않는다.

    redaction 만 믿는 것보다 본문을 읽는 코드가 없는 편이 낫다. 미들웨어가
    하나라도 본문을 읽기 시작하면 그때부터 모든 endpoint 가 비밀 취급 대상이
    된다. 그래서 미들웨어가 늘어나면 이 테스트가 먼저 깨진다.
    """
    from starlette.middleware.cors import CORSMiddleware

    from app.main import app as real_app

    for mw in real_app.user_middleware:
        assert mw.cls is CORSMiddleware, (
            f"본문을 읽을 수 있는 미들웨어가 늘었다: {mw.cls.__name__}. "
            "자격증명 endpoint 본문이 로그로 새지 않는지 확인해야 한다."
        )


def test_there_is_no_way_to_read_a_secret_back():
    """되읽기 경로를 만들면 그게 가장 약한 고리가 된다.

    잊었으면 새로 넣는다 — [비밀번호 보기] 는 만들지 않았다.
    """
    from app.main import app as real_app

    for route in real_app.routes:
        path = getattr(route, "path", "")
        if "credential" in path:
            assert "GET" not in getattr(route, "methods", set()), (
                f"자격증명을 읽는 경로가 있다: {path}"
            )


def test_rotation_keeps_working_credential_when_the_new_one_is_rejected(
    db, org, account, secret_store,
):
    """새 값이 검증에 걸리면 기존 자격증명이 살아 있어야 한다.

    먼저 지우고 나중에 넣으면 오타 하나로 멀쩡하던 연동이 끊긴다.
    """
    from app.core.exceptions import ValidationError

    actor = make_app_user(db, org_id=org, role="ADMIN")
    ex.set_credential_secret(db, org_id=org, actor_user_id=actor,
                             account_id=uuid.UUID(account["id"]),
                             credential_kind="API_KEY",
                             payload={"api_key": "OLD_TEST_SECRET"})
    old_ref = db.execute(text("""
        SELECT credential_secret_ref FROM external_account WHERE id = :i
    """), {"i": account["id"]}).scalar()

    with pytest.raises(ValidationError):
        # ACCESS_SECRET 은 secret_key 도 있어야 한다.
        ex.set_credential_secret(db, org_id=org, actor_user_id=actor,
                                 account_id=uuid.UUID(account["id"]),
                                 credential_kind="ACCESS_SECRET",
                                 payload={"access_key": "TEST"})

    still = ex.get_account(db, org_id=org, account_id=uuid.UUID(account["id"]))
    assert still["credential_configured"] is True
    # 기존 비밀이 그대로 읽힌다 — 실패가 연동을 끊지 않았다.
    assert secret_store.get(org_id=org, secret_ref=old_ref) == {
        "api_key": "OLD_TEST_SECRET",
    }


def test_rotation_discards_the_previous_secret(db, org, account, secret_store):
    """교체 뒤 옛 비밀이 남으면 언제 폐기되는지 아무도 모른다."""
    actor = make_app_user(db, org_id=org, role="ADMIN")
    ex.set_credential_secret(db, org_id=org, actor_user_id=actor,
                             account_id=uuid.UUID(account["id"]),
                             credential_kind="API_KEY",
                             payload={"api_key": "FIRST_TEST_SECRET"})
    first_ref = db.execute(text("""
        SELECT credential_secret_ref FROM external_account WHERE id = :i
    """), {"i": account["id"]}).scalar()

    ex.set_credential_secret(db, org_id=org, actor_user_id=actor,
                             account_id=uuid.UUID(account["id"]),
                             credential_kind="API_KEY",
                             payload={"api_key": "SECOND_TEST_SECRET"})
    second_ref = db.execute(text("""
        SELECT credential_secret_ref FROM external_account WHERE id = :i
    """), {"i": account["id"]}).scalar()

    assert second_ref != first_ref
    assert secret_store.exists(org_id=org, secret_ref=first_ref) is False
    assert secret_store.get(org_id=org, secret_ref=second_ref) == {
        "api_key": "SECOND_TEST_SECRET",
    }


def test_cannot_point_at_another_orgs_vault_entry(db, org, account):
    """남의 보관함 쪽지를 가리킬 수 없다."""
    from app.core.exceptions import NotFoundError

    other = make_organization(db)
    foreign = db.execute(text("""
        INSERT INTO password_vault_entry
               (org_id, service_name, vault_provider, vault_item_name)
        VALUES (:o, '남의 것', 'BITWARDEN', 'x') RETURNING id
    """), {"o": str(other)}).scalar()
    db.commit()

    actor = make_app_user(db, org_id=org, role="ADMIN")
    with pytest.raises(NotFoundError):
        ex.link_vault_note(db, org_id=org, actor_user_id=actor,
                           account_id=uuid.UUID(account["id"]),
                           vault_entry_id=uuid.UUID(str(foreign)))


def test_secret_cannot_be_read_across_orgs(db, org, account, secret_store):
    """참조를 알아도 남의 조직 비밀은 못 꺼낸다.

    조회는 항상 org 와 함께 한다 — 참조 하나가 새도 그것만으로는 아무것도
    열리지 않아야 한다.
    """
    from app.core.secret_store import SecretNotFoundError

    actor = make_app_user(db, org_id=org, role="ADMIN")
    ex.set_credential_secret(db, org_id=org, actor_user_id=actor,
                             account_id=uuid.UUID(account["id"]),
                             credential_kind="API_KEY", payload={"api_key": SECRET})
    ref = db.execute(text("""
        SELECT credential_secret_ref FROM external_account WHERE id = :i
    """), {"i": account["id"]}).scalar()

    other = make_organization(db)
    with pytest.raises(SecretNotFoundError):
        secret_store.get(org_id=other, secret_ref=ref)


def test_removing_credential_disconnects_but_keeps_the_account(
    db, org, account, secret_store,
):
    actor = make_app_user(db, org_id=org, role="ADMIN")
    ex.set_credential_secret(db, org_id=org, actor_user_id=actor,
                             account_id=uuid.UUID(account["id"]),
                             credential_kind="API_KEY", payload={"api_key": SECRET})
    ref = db.execute(text("""
        SELECT credential_secret_ref FROM external_account WHERE id = :i
    """), {"i": account["id"]}).scalar()

    out = ex.remove_credential(db, org_id=org, actor_user_id=actor,
                               account_id=uuid.UUID(account["id"]))
    assert out["credential_configured"] is False
    # 비밀도 실제로 사라진다 — 참조만 끊고 남겨 두면 언제 지워지는지 모른다.
    assert secret_store.exists(org_id=org, secret_ref=ref) is False
    # 계정과 연결 기록은 남는다.
    assert len(ex.list_accounts(db, org_id=org)) == 1


def test_only_admin_can_manage_credentials(db, client, org, account, vault_entry):
    """화면에서 버튼을 숨기는 것으로 끝내지 않는다 — API 를 직접 부르면 뚫린다."""
    for role in ("OPERATOR", "SOURCING", "ACCOUNTING", "READONLY"):
        uid = make_app_user(db, org_id=org, role=role)
        db.commit()
        headers = auth(create_access_token(user_id=uid, org_id=org, role=role))

        res = client.put(
            f"/api/v1/external/accounts/{account['id']}/credential",
            json={"credential_kind": "API_KEY", "secret": {"api_key": SECRET}},
            headers=headers,
        )
        assert res.status_code == 403, f"{role} 이 자격증명을 설정할 수 있다"

        res = client.delete(
            f"/api/v1/external/accounts/{account['id']}/credential", headers=headers,
        )
        assert res.status_code == 403, f"{role} 이 자격증명을 지울 수 있다"


# ──────────────────────────────────────────────────────────────────────────
# 연결 시험 — 실패 사유를 뭉치지 않는다
# ──────────────────────────────────────────────────────────────────────────

def test_connection_test_distinguishes_why_it_failed(db, org, account):
    """"연결 실패" 하나로 보여주면 무엇을 고쳐야 할지 알 수 없다."""
    actor = make_app_user(db, org_id=org, role="ADMIN")

    # 커넥터 자체가 없다.
    out = ex.test_connection(db, org_id=org, actor_user_id=actor,
                             account_id=uuid.UUID(account["id"]))
    assert out["status"] == "CONNECTOR_UNAVAILABLE"

    # 커넥터는 있는데 자격증명이 없다 — 다른 문제다.
    from app.tests.fixtures.fake_connector import FakeSellerToolConnector, use_fake_connector

    fake = FakeSellerToolConnector()
    fake.system_code = "CRED_TEST"
    with use_fake_connector(fake):
        out = ex.test_connection(db, org_id=org, actor_user_id=actor,
                                 account_id=uuid.UUID(account["id"]))
        assert out["status"] == "CREDENTIAL_NOT_CONFIGURED"

        ex.set_credential_secret(db, org_id=org, actor_user_id=actor,
                                 account_id=uuid.UUID(account["id"]),
                                 credential_kind="API_KEY",
                                 payload={"api_key": SECRET})
        out = ex.test_connection(db, org_id=org, actor_user_id=actor,
                                 account_id=uuid.UUID(account["id"]))
        assert out["status"] == "OK"
        assert out["account"]["status"] == "CONNECTED"
        # 시험 결과 어디에도 값이 없다.
        assert SECRET not in str(out)


# ──────────────────────────────────────────────────────────────────────────
# 데이터 품질
# ──────────────────────────────────────────────────────────────────────────

def _codes(db: Session, org: uuid.UUID) -> dict[str, str]:
    from app.services import close_checks

    return {i.code: i.grade
            for i in close_checks.run_checks(db, org_id=org, year_month="2026-08")}


def test_missing_credential_is_informational(db, org, account):
    """등록만 해 둔 상태는 정상이다 — 경고로 올리면 의미 없는 알림이 쌓인다."""
    assert _codes(db, org).get("CREDENTIAL_NOT_CONFIGURED") == "INFO"


def test_failed_sync_is_a_warning_and_counted_per_account(db, org, account):
    """매 실패마다 쌓으면 이슈 목록이 실패 로그가 된다 — 계정당 한 건이다."""
    for _ in range(3):
        db.execute(text("""
            INSERT INTO external_sync_run
                   (org_id, external_account_id, sync_type, status, finished_at)
            VALUES (:o, :a, 'PRODUCTS', 'FAILED', now())
        """), {"o": str(org), "a": account["id"]})
    db.commit()

    codes = _codes(db, org)
    assert codes.get("SYNC_FAILED") == "WARNING"
    from app.services import close_checks

    item = next(i for i in close_checks.run_checks(db, org_id=org, year_month="2026-08")
                if i.code == "SYNC_FAILED")
    assert item.count == 1, "실패 실행마다 세고 있다"


def test_partial_sync_is_not_reported_as_success(db, org, account):
    db.execute(text("""
        INSERT INTO external_sync_run
               (org_id, external_account_id, sync_type, status, finished_at,
                read_count, updated_count, error_count)
        VALUES (:o, :a, 'PRODUCTS', 'PARTIAL', now(), 1000, 995, 5)
    """), {"o": str(org), "a": account["id"]})
    db.commit()

    assert _codes(db, org).get("SYNC_PARTIAL_FAILURE") == "WARNING"
    assert "SYNC_FAILED" not in _codes(db, org)


def test_orphan_mapping_is_flagged(db, org, account):
    """FK 가 없는 컬럼이라 DB 가 막지 못한다."""
    actor = make_app_user(db, org_id=org, role="ADMIN")
    product = make_product(db, org_id=org)
    db.commit()
    ex.upsert_mapping(db, org_id=org, actor_user_id=actor,
                      external_account_id=uuid.UUID(account["id"]),
                      external_entity_type="PRODUCT", external_entity_id="1",
                      internal_entity_type="PRODUCT", internal_entity_id=product)
    db.execute(text("UPDATE product SET deleted_at = now() WHERE id = :i"),
               {"i": str(product)})
    db.commit()

    assert _codes(db, org).get("EXTERNAL_MAPPING_ORPHAN") == "WARNING"


def test_external_checks_never_block_the_close(db, org, account):
    """외부 동기화가 멈춰도 그 달의 손익은 계산된다."""
    from app.services import close_checks

    db.execute(text("""
        INSERT INTO external_sync_run
               (org_id, external_account_id, sync_type, status, finished_at)
        VALUES (:o, :a, 'PRODUCTS', 'FAILED', now())
    """), {"o": str(org), "a": account["id"]})
    db.commit()

    external = [i for i in close_checks.run_checks(db, org_id=org, year_month="2026-08")
                if i.domain == "EXTERNAL"]
    assert external
    assert not any(i.blocking for i in external)
