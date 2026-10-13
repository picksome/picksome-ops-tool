"""파트너(내부 구성원) 조회 라우터.

**읽기 전용이다.** 파트너 추가·수정은 조직 구조 변경이라 수익배분 비율
(`partner_share_policy`)과 함께 다뤄야 하고, 그 화면은 아직 없다.
여기서 만들어 두면 배분 비율이 없는 파트너가 생겨 정산이 합계 1.0 을 못 채운다.

왜 필요한가
----------
`expenses.payment_card_holder_id` · `attributable_partner_id` 는 `partner` 를
가리키는데, 지금까지 파트너 목록을 내려주는 엔드포인트가 없었다.
그래서 화면은 결제자·귀속 파트너를 **선택할 수 없었다** — 목록 조회가
`/admin/users` 뿐이었고 그건 ADMIN 전용이라 ACCOUNTING 이 비용을 입력할 때 403 이다.

`app_user` 와 다르다: 파트너는 **수익을 배분받는 주체**고, 로그인 계정이 없을 수도
있다(`linked_user_id` 가 nullable). 둘을 같은 목록으로 합치지 않는다.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db

router = APIRouter(prefix="/partners", tags=["partners"])

# 비용 입력은 ACCOUNTING 도 한다. 읽기는 READONLY 까지 연다 —
# 이름 목록이 없으면 목록 화면이 UUID 를 그대로 보여주게 된다.
_read = require_roles("ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY")


@router.get("")
def list_(
    include_inactive: bool = Query(
        default=False,
        description="비활성 파트너 포함. 과거 비용의 결제자를 표시할 때 필요하다",
    ),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    where = ["org_id = :o", "deleted_at IS NULL"]
    if not include_inactive:
        where.append("is_active = TRUE")
    rows = db.execute(
        text(f"""
          SELECT id, name, display_order, is_active, linked_user_id
            FROM partner
           WHERE {" AND ".join(where)}
           ORDER BY display_order, name
        """),
        {"o": str(user.org_id)},
    ).mappings().all()
    return envelope([
        {
            "id": str(r["id"]),
            "name": r["name"],
            "display_order": r["display_order"],
            "is_active": r["is_active"],
            "linked_user_id": str(r["linked_user_id"]) if r["linked_user_id"] else None,
        }
        for r in rows
    ])


# ──────────────────────────────────────────────────────────────
# 사용자 이름 조회 (디렉터리)
# ──────────────────────────────────────────────────────────────

directory_router = APIRouter(prefix="/users", tags=["users"])


@directory_router.get("/directory")
def user_directory(
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """조직 구성원의 **이름만** 조회한다.

    왜 필요한가
    ----------
    승인 화면은 `requested_by` / `reviewed_by` 로 UUID 만 받는다. 이름을 붙이려면
    사용자 목록이 필요한데 `/admin/users` 는 ADMIN 전용이라, OPERATOR 가 승인함을
    열면 요청자가 UUID 로 보이거나 "-" 로 보인다. 실제로 화면은 그 자리를
    **mock 구성원 목록**으로 채우고 있었다 — 실존하지 않는 이름이 표시됐다.

    무엇을 내려주지 않는가
    -------------------
    **이메일·마지막 로그인·활성 여부를 주지 않는다.** 이름을 붙이는 데 필요 없고,
    구성원 관리 화면(ADMIN 전용 `/admin/users`)의 정보를 모든 역할에 열어 줄
    이유가 없다. 역할은 남긴다 — "이 사람이 승인 권한자인가" 를 화면이 표시한다.

    비활성 사용자도 포함한다. 퇴사자가 작년에 올린 승인 요청의 이름이
    사라지면 이력을 읽을 수 없다.
    """
    rows = db.execute(
        text("""
          SELECT id, display_name, role, is_active
            FROM app_user
           WHERE org_id = :o AND deleted_at IS NULL
           ORDER BY display_name
        """),
        {"o": str(user.org_id)},
    ).mappings().all()
    return envelope([
        {
            "id": str(r["id"]),
            "display_name": r["display_name"],
            "role": r["role"],
            "is_active": r["is_active"],
        }
        for r in rows
    ])
