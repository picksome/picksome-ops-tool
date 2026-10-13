"""거래처(external_partner) 조회 라우터 (M027).

**무엇이 "거래처" 인가 — 비슷한 이름의 5개를 섞지 않는다**

| 테이블 | 무엇인가 | 비용과의 관계 |
|---|---|---|
| `external_partner` | **돈을 지불하는 상대**. 매입처·물류사·광고대행사 | `expenses.supplier_id` → **여기** |
| `partner` | 조직 내부 파트너. **수익을 배분받는 주체** | `payment_card_holder_id`, `attributable_partner_id` |
| `sale_channel` | 판매처 카탈로그(쿠팡·스마트스토어). 조직 무관 마스터 | 비용과 직접 관계 없음 |
| `integration_account` | 판매처의 **우리 계정**(자격증명 vault 참조) | 없음 |
| "supplier" | 별도 테이블이 아니다 — `external_partner` 를 가리키는 **컬럼 이름** | `product.supplier_id` 도 같은 곳 |

즉 비용의 거래처 = `external_partner` 이고, `partner` 와 합치면
**수익을 나눠 갖는 사람과 돈을 지불하는 상대가 한 목록에 섞인다.**

읽기 전용인 이유
--------------
거래처 생성·수정 화면이 아직 없다. 조회만 열어 두고, 생성 API 를 지금 만들면
검증(사업자번호 형식·중복·vault 연결) 없이 행이 생기기 시작한다.
화면은 "등록 기능은 아직 지원하지 않습니다" 라고 **명시**한다 —
동작하지 않는 버튼을 두는 것보다 낫다.

`business_number` 는 **스키마에 없다.** 있는 척 내려주지 않는다.
필요해지면 컬럼 추가 + 마스킹 정책 + 권한을 함께 설계한다.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.rbac import require_roles
from app.core.response import envelope
from app.deps import CurrentUser, get_db

router = APIRouter(prefix="/external-partners", tags=["external-partners"])

_read = require_roles("ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY")


@router.get("")
def list_(
    q: str | None = Query(default=None, description="거래처명·메모 부분 일치"),
    type: str | None = Query(default=None, alias="type", description="kind 값"),
    active: bool | None = Query(
        default=None,
        description="미지정이면 전체. 선택 목록은 true, 과거 데이터 조회는 미지정",
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    where = ["org_id = :o", "deleted_at IS NULL"]
    params: dict[str, Any] = {"o": str(user.org_id), "lim": limit, "off": offset}
    if active is not None:
        where.append("is_active = :act")
        params["act"] = active
    if type:
        where.append("kind = :kind")
        params["kind"] = type
    if q and q.strip():
        # 거래처명과 메모만 본다. `contact_masked` 는 검색 대상이 아니다 —
        # 마스킹된 연락처로 검색하게 두면 마스킹 규칙이 역으로 노출된다.
        where.append("(name ILIKE :q OR memo ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    where_sql = " AND ".join(where)

    total = int(db.execute(
        text(f"SELECT count(*) FROM external_partner WHERE {where_sql}"), params,
    ).scalar_one())

    rows = db.execute(text(f"""
        SELECT id, name, kind, is_active, memo
          FROM external_partner
         WHERE {where_sql}
         ORDER BY is_active DESC, name
         LIMIT :lim OFFSET :off
    """), params).mappings().all()

    return envelope(
        [{
            "id": str(r["id"]),
            "display_name": r["name"],
            "type": r["kind"],
            "is_active": r["is_active"],
            "memo": r["memo"],
            # `business_number` 는 스키마에 없다. 키 자체를 내리지 않는다 —
            # null 로 내려주면 화면이 "값이 비어 있다" 로 오해하고 입력란을 만든다.
        } for r in rows],
        meta={"total": total, "limit": limit, "offset": offset,
              # 생성 기능이 없다는 사실을 서버가 알려준다. 화면이 각자 판단하면
              # 언젠가 한쪽에만 "신규 등록" 버튼이 생긴다.
              "can_create": False},
    )


@router.get("/{partner_id}")
def detail(
    partner_id: UUID,
    user: CurrentUser = Depends(_read),
    db: Session = Depends(get_db),
) -> dict:
    """단건 조회.

    **삭제된 거래처도 돌려준다.** 과거 비용이 가리키는 거래처가 조회되지 않으면
    화면이 그 비용을 "거래처 없음" 으로 보여주게 되고, 그건 사실이 아니다.
    """
    from app.core.exceptions import NotFoundError

    row = db.execute(text("""
        SELECT id, name, kind, is_active, memo, deleted_at
          FROM external_partner
         WHERE id = :i AND org_id = :o
    """), {"i": str(partner_id), "o": str(user.org_id)}).mappings().first()
    if row is None:
        raise NotFoundError("거래처를 찾을 수 없습니다.")
    return envelope({
        "id": str(row["id"]),
        "display_name": row["name"],
        "type": row["kind"],
        "is_active": row["is_active"] and row["deleted_at"] is None,
        "is_deleted": row["deleted_at"] is not None,
        "memo": row["memo"],
    })
