"""M003 audit_action enum 정합화 — EXPORT / PERMISSION_CHANGE / IMPORT 추가.

배경:
  DB `enum_audit_action`(M001, 8종: CREATE/UPDATE/DELETE/RESTORE/STATUS_CHANGE/
  POLICY_OVERRIDE/LOGIN/LOGOUT) 과 app/core/audit.py 의 Python `AuditAction`
  Literal(9종) 이 최초 설계 이후 어긋나 있었다. 코드 조사 결과 실제로 두 곳이
  런타임에 깨져 있었다:
    - app/api/v1/reports.py, app/api/v1/admin_audit.py 는 EXPORT 를 쓰고
      싶었지만 DB 에 값이 없어 POLICY_OVERRIDE 로 임시 매핑 (TODO 주석 존재).
    - app/services/user.py:170 은 role 변경 시 action="PERMISSION_CHANGE" 를
      그대로 INSERT 하는데, DB enum 에 없어 실행 시 `invalid input value for
      enum enum_audit_action` 오류로 매 role 변경마다 실패하는 상태였다.

조치:
  - enum_audit_action 에 EXPORT / PERMISSION_CHANGE / IMPORT 3종 추가.
    (POLICY_OVERRIDE, RESTORE 는 유지 — 각각 향후 실제 정책 override 기록,
    soft-delete 복구 로깅 용도로 설계된 값이며 아직 미사용일 뿐 제거 대상 아님.)
  - 기존 audit_log 에서 export 이벤트를 임시로 POLICY_OVERRIDE 로 남겨뒀던
    행(domain_table IN ('audit_log','tax_report_export'))을 EXPORT 로 백필.
    POLICY_OVERRIDE 는 이 두 임시 호출 외에 실사용된 적이 없어 전량 안전하게
    이관 가능 (코드 전체 grep 으로 확인).

주의 (Postgres ALTER TYPE ADD VALUE 트랜잭션 제약):
  PG12+ 부터 트랜잭션 안에서 실행은 되지만, **같은 트랜잭션에서 추가한 값은
  즉시 사용할 수 없다.** autocommit_block 으로 ADD VALUE 를 먼저 커밋시키고
  이어서 백필 UPDATE 를 별도 실행한다.

동반 코드 변경 (이 마이그레이션과 함께 적용됨):
  - app/core/audit.py 의 AuditAction Literal 을 DB 와 11종 1:1 로 동기화.
  - app/api/v1/reports.py, app/api/v1/admin_audit.py 임시 매핑 제거 →
    action="EXPORT".
"""
from collections.abc import Sequence

from alembic import op

revision: str = "M003"
down_revision: str | None = "M002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_VALUES = ("EXPORT", "PERMISSION_CHANGE", "IMPORT")

_ORIGINAL_VALUES = (
    "CREATE", "UPDATE", "DELETE", "RESTORE", "STATUS_CHANGE",
    "POLICY_OVERRIDE", "LOGIN", "LOGOUT",
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in _NEW_VALUES:
            op.execute(f"ALTER TYPE enum_audit_action ADD VALUE IF NOT EXISTS '{value}'")

    # 임시로 POLICY_OVERRIDE 로 남아있던 export 이벤트를 EXPORT 로 백필.
    op.execute(
        """
        UPDATE audit_log
           SET action = 'EXPORT'
         WHERE action = 'POLICY_OVERRIDE'
           AND domain_table IN ('audit_log', 'tax_report_export')
        """
    )


def downgrade() -> None:
    # 1) 추가된 값을 쓰고 있는 기존 행을 원래 있던 값으로 되돌린다
    #    (정확한 원복 대상이 없으므로 STATUS_CHANGE/POLICY_OVERRIDE 로 흡수).
    op.execute(
        """
        UPDATE audit_log
           SET action = 'POLICY_OVERRIDE'
         WHERE action = 'EXPORT'
           AND domain_table IN ('audit_log', 'tax_report_export')
        """
    )
    op.execute(
        """
        UPDATE audit_log
           SET action = 'STATUS_CHANGE'
         WHERE action IN ('PERMISSION_CHANGE', 'IMPORT')
        """
    )
    op.execute(
        """
        UPDATE policy_change_history
           SET change_action = 'POLICY_OVERRIDE'
         WHERE change_action = 'EXPORT'
        """
    )
    op.execute(
        """
        UPDATE policy_change_history
           SET change_action = 'STATUS_CHANGE'
         WHERE change_action IN ('PERMISSION_CHANGE', 'IMPORT')
        """
    )

    # 2) Postgres 는 enum 값 제거를 직접 지원하지 않으므로 타입을 재생성한다.
    #    두 컬럼(audit_log.action, policy_change_history.change_action)이
    #    같은 타입을 참조하므로 둘 다 재캐스팅해야 한다.
    op.execute("ALTER TYPE enum_audit_action RENAME TO enum_audit_action_old")
    values_sql = ", ".join(f"'{v}'" for v in _ORIGINAL_VALUES)
    op.execute(f"CREATE TYPE enum_audit_action AS ENUM ({values_sql})")
    op.execute(
        """
        ALTER TABLE audit_log
          ALTER COLUMN action TYPE enum_audit_action
          USING action::text::enum_audit_action
        """
    )
    op.execute(
        """
        ALTER TABLE policy_change_history
          ALTER COLUMN change_action TYPE enum_audit_action
          USING change_action::text::enum_audit_action
        """
    )
    op.execute("DROP TYPE enum_audit_action_old")
