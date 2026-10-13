"""승인 대상 어댑터 (docs/43 §8).

승인 서비스가 상품·정책·비용 테이블을 거대한 if/elif 로 직접 수정하지 않게 한다.
대상별로 이 인터페이스를 구현하고 레지스트리에 등록한다.

    validate_target()            대상이 존재하고 이 조직 것인지
    build_snapshot()             요청 시점 값 박제 (민감정보는 여기서 마스킹)
    validate_proposed_changes()  바꾸려는 키가 허용 목록에 있는지
    get_effective_date()         회계 귀속일 — 월 잠금 판정에 쓴다
    get_current_version()        낙관적 잠금용 지문
    apply_changes()              실제 반영

**M018 에서 지원하는 대상만 등록한다.** 지원하지 않는 target_type 은 422 로 거부한다.
Phase 4 이후 대상(비용 승인, 정산 강제 매칭 등)을 미리 만들지 않는다.

첫 지원 대상은 `product` — 원가·판매가 변경이 docs/43 §2 Phase 3 의 승인 대상 목록
맨 앞에 있고, 실제로 마진에 바로 영향을 준다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ValidationError


class TargetAdapter(Protocol):
    """승인 대상 어댑터 인터페이스."""

    target_type: str
    allowed_fields: frozenset[str]
    request_types: frozenset[str]

    def validate_target(self, db: Session, *, org_id: UUID, target_id: UUID) -> None: ...
    def build_snapshot(self, db: Session, *, org_id: UUID, target_id: UUID) -> dict[str, Any]: ...
    def get_current_version(self, db: Session, *, org_id: UUID, target_id: UUID) -> str: ...
    def get_effective_date(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], effective_from: date | None,
    ) -> date: ...
    def apply_changes(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], actor_user_id: UUID,
    ) -> dict[str, Any]: ...


def version_of(snapshot: dict[str, Any]) -> str:
    """스냅샷 → 지문.

    별도 version 컬럼을 모든 도메인 테이블에 추가하는 것보다 침습적이지 않다.
    정렬된 JSON 을 해싱하므로 키 순서에 흔들리지 않는다.
    """
    payload = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class ProductAdapter:
    """상품 원가·판매가·기본정보 변경.

    `sale_price` 는 product 테이블에 없다(판매가는 판매 시점 값이라 sales_order 에 있고,
    상품 단위 정가 개념은 아직 스키마에 없다). 그래서 M018 은 **원가와 기본정보**만
    다룬다 — 없는 컬럼을 승인 대상으로 열어두면 적용 단계에서 조용히 실패한다.
    """

    target_type = "product"

    # ── 요청 유형별 필드 소유권 ────────────────────────────────────
    #
    # 열린 요청 UNIQUE 가 (org, target_type, target_id, request_type) 라서
    # **서로 다른 request_type 은 동시에 열릴 수 있다.** 그래서 두 유형이 같은 필드를
    # 건드리면 나중에 적용되는 쪽이 앞의 승인 내용을 조용히 덮어쓴다.
    #
    # 그래서 필드를 유형별로 **배타적으로** 나눈다. 겹치면 import 시점에 터진다
    # (아래 _assert_no_field_overlap). 겹치는 필드가 정말 필요해지면 그때
    # 요청 생성 시 열린 요청과의 changed-field 교집합을 검사하는 방식으로 바꾼다.
    #
    # ⚠️ 여기 없는 필드는 승인으로 바꿀 수 없다. product 테이블에 실제로 있는
    #    컬럼만 넣는다 — 없는 컬럼을 열어두면 적용 단계에서 조용히 실패한다.
    FIELD_OWNERSHIP: dict[str, frozenset[str]] = {
        # 상품 식별·분류 정보
        "PRODUCT_INFO_CHANGE": frozenset({
            "name_ko", "name_en", "category_path", "supply_url", "supplier_id",
        }),
        # 원가 구성 — 마진에 직접 영향. 민감 필드가 여기 모여 있다.
        "COST_CHANGE": frozenset({
            "cost_amount", "cost_currency", "cost_fx_rate", "cost_amount_foreign",
        }),
        # 판매 상태
        "STATUS_CHANGE": frozenset({"status", "margin_type"}),
    }

    allowed_fields = frozenset().union(*FIELD_OWNERSHIP.values())
    request_types = frozenset(FIELD_OWNERSHIP)

    def fields_for(self, request_type: str) -> frozenset[str]:
        return self.FIELD_OWNERSHIP.get(request_type, frozenset())

    _SELECT = """
        SELECT id, business_key, name_ko, cost_amount, cost_currency, cost_fx_rate,
               cost_amount_foreign, category_path, supply_url, memo, status, created_at
          FROM product
         WHERE id = :id AND org_id = :org AND deleted_at IS NULL
    """

    def _row(self, db: Session, org_id: UUID, target_id: UUID):
        row = db.execute(
            text(self._SELECT), {"id": str(target_id), "org": str(org_id)}
        ).mappings().first()
        if row is None:
            # 조직 격리 — 다른 org 의 상품도 여기서 NOT_FOUND 로 떨어진다.
            # "존재하지만 권한 없음" 을 구분해 주면 다른 조직의 ID 존재 여부가 새어나간다.
            raise NotFoundError("상품을 찾을 수 없습니다.")
        return row

    def validate_target(self, db: Session, *, org_id: UUID, target_id: UUID) -> None:
        self._row(db, org_id, target_id)

    def build_snapshot(self, db: Session, *, org_id: UUID, target_id: UUID) -> dict[str, Any]:
        row = self._row(db, org_id, target_id)
        # 허용 필드만 담는다. 전체 행을 박제하면 무관한 컬럼 변화가 base_version 을 흔든다.
        return {k: row[k] for k in sorted(self.allowed_fields) if k in row}

    def get_current_version(self, db: Session, *, org_id: UUID, target_id: UUID) -> str:
        return version_of(self.build_snapshot(db, org_id=org_id, target_id=target_id))

    def get_effective_date(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], effective_from: date | None,
    ) -> date:
        """회계 귀속일.

        원가 변경은 **적용일이 속한 월**에 귀속된다. 지정이 없으면 오늘.
        과거 월을 지정하면 그 월의 잠금 여부로 판정된다 — 잠긴 달의 원가를
        바꾸면 이미 확정한 마진이 달라지기 때문이다.
        """
        return effective_from or date.today()

    def validate_proposed_changes(
        self, proposed: dict[str, Any], *, request_type: str | None = None
    ) -> None:
        """필드 검증. `request_type` 을 주면 **그 유형이 소유한 필드인지**까지 본다.

        유형을 모르는 호출(레거시)은 전체 allowlist 로만 검사한다.
        """
        if not proposed:
            raise ValidationError("변경할 내용이 없습니다.")
        unknown = set(proposed) - self.allowed_fields
        if unknown:
            raise ValidationError(
                f"승인으로 변경할 수 없는 필드입니다: {sorted(unknown)}",
                detail={"allowed": sorted(self.allowed_fields)},
            )
        if request_type is not None:
            owned = self.fields_for(request_type)
            trespass = sorted(set(proposed) - owned)
            if trespass:
                # 다른 유형이 소유한 필드다. 같이 넣으면 두 승인이 서로를 덮어쓴다.
                owner_of = {
                    f: rt for rt, fs in self.FIELD_OWNERSHIP.items() for f in fs if f in trespass
                }
                raise ValidationError(
                    f"{request_type} 요청으로 바꿀 수 없는 필드입니다: {trespass}",
                    detail={"owned_by": owner_of, "allowed_for_request_type": sorted(owned)},
                )

    def apply_changes(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], actor_user_id: UUID,
        request_type: str | None = None, approval_request_id: UUID | None = None,
    ) -> dict[str, Any]:
        # 상품은 컬럼을 덮어쓰는 방식이라 승인 요청 id 를 저장할 자리가 없다.
        # 추적은 audit_log 가 담당한다.
        _ = approval_request_id
        # executor 가 우회할 수 없도록 적용 시점에도 같은 검증을 한다.
        self.validate_proposed_changes(proposed, request_type=request_type)

        # 기존 상품 서비스를 재사용한다. raw UPDATE 를 직접 쓰면 환율 자동 채움
        # (_resolve_fx_rate) 같은 기존 로직이 빠져 원가가 어긋난다.
        # `_internal_approval_apply=True` 로 승인 우회 가드를 통과한다 —
        # 이 인자는 라우터에 노출되지 않아 클라이언트가 설정할 수 없다.
        from app.domain.product import ProductUpdate
        from app.services.product import update_product

        update_product(
            db, org_id=org_id, actor=actor_user_id, product_id=target_id,
            body=ProductUpdate(**proposed), _internal_approval_apply=True,
        )
        return self.build_snapshot(db, org_id=org_id, target_id=target_id)



class ChannelListingAdapter:
    """판매처별 가격 변경 (M022).

    왜 target 이 `product` 가 아니라 `product_channel_listing` 인가
    -----------------------------------------------------------
    열린 요청 UNIQUE 가 `(org, target_type, target_id, request_type)` 이다.
    target 을 상품으로 두면 **판매처가 둘일 때 한쪽 가격만 요청할 수 있다** —
    쿠팡 가격 승인이 열려 있는 동안 스마트스토어 가격은 손도 못 댄다.

    `product_channel_price` 를 target 으로 두는 안도 있었지만, 아직 없는 가격 행을
    대상으로 요청할 수 없어 **신규 가격 등록을 표현하지 못한다.**

    적용 결과가 UPDATE 가 아니다
    --------------------------
    승인 적용은 기존 가격을 고치는 게 아니라 **새 가격 버전을 만든다.**
    과거 가격을 덮어쓰면 "그때 얼마에 팔았나" 를 잃는다.
    """

    target_type = "product_channel_listing"

    # 가격 관련 필드는 전부 한 유형이 소유한다. 정상가와 할인가를 다른 유형으로 쪼개면
    # 두 승인이 각자 새 가격 버전을 만들어 기간이 겹친다.
    FIELD_OWNERSHIP: dict[str, frozenset[str]] = {
        "CHANNEL_PRICE_CHANGE": frozenset({
            "product_variant_id", "currency",
            "regular_price", "sale_price", "sale_start_at", "sale_end_at",
            "effective_from", "effective_to",
        }),
    }
    allowed_fields = frozenset().union(*FIELD_OWNERSHIP.values())
    request_types = frozenset(FIELD_OWNERSHIP)

    def fields_for(self, request_type: str) -> frozenset[str]:
        return self.FIELD_OWNERSHIP.get(request_type, frozenset())

    def _listing(self, db: Session, org_id: UUID, target_id: UUID):
        from app.services.channel_price import get_listing

        return get_listing(db, org_id=org_id, listing_id=target_id)

    def validate_target(self, db: Session, *, org_id: UUID, target_id: UUID) -> None:
        listing = self._listing(db, org_id, target_id)
        if listing.status == "ENDED":
            raise ValidationError(
                "종료된 판매처 연결의 가격은 바꿀 수 없습니다.",
                detail={"listing_status": listing.status},
            )

    def build_snapshot(self, db: Session, *, org_id: UUID, target_id: UUID) -> dict[str, Any]:
        """변경 **전** 가격.

        listing 의 속성이 아니라 **현재 유효한 가격**을 박제한다. 승인자가 비교해야
        하는 건 "지금 얼마인가" 지 리스팅 설정이 아니다.

        variant 별 가격이 여럿일 수 있어 기본 가격(variant NULL)을 기준으로 잡는다.
        요청이 특정 variant 를 대상으로 하면 적용 시점에 그 variant 기준으로 다시 본다.
        """
        from app.services.channel_price import resolve_price

        resolved = resolve_price(db, org_id=org_id, listing_id=target_id)
        return {
            "regular_price": resolved.regular_price,
            "sale_price": resolved.sale_price,
            "sale_start_at": resolved.sale_start_at,
            "sale_end_at": resolved.sale_end_at,
            "currency": resolved.currency,
            "effective_from": resolved.effective_from,
            "effective_to": resolved.effective_to,
            "product_variant_id": None,
        }

    def get_current_version(self, db: Session, *, org_id: UUID, target_id: UUID) -> str:
        return version_of(self.build_snapshot(db, org_id=org_id, target_id=target_id))

    def get_effective_date(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], effective_from: date | None,
    ) -> date:
        """회계 귀속일 = **가격이 실제로 적용되기 시작하는 날**.

        승인 요청의 `effective_from`(언제 적용할까)과 payload 의 `effective_from`
        (가격이 언제부터 유효한가)이 둘 다 있다. 혼란을 줄이려고 **가격 시작일을
        단일 진실로 삼는다** — 승인 요청 쪽 값은 executor 가 "언제 실행할지" 에만 쓴다.

        둘이 다르면 "8월에 승인 실행해서 9월부터 적용" 같은 정상적인 경우다.
        월 잠금 판정은 가격이 적용되는 달로 해야 맞다.
        """
        payload_from = proposed.get("effective_from")
        if payload_from:
            if isinstance(payload_from, str):
                return date.fromisoformat(payload_from[:10])
            if isinstance(payload_from, date):
                return payload_from
        return effective_from or date.today()

    def validate_proposed_changes(
        self, proposed: dict[str, Any], *, request_type: str | None = None
    ) -> None:
        if not proposed:
            raise ValidationError("변경할 내용이 없습니다.")
        unknown = set(proposed) - self.allowed_fields
        if unknown:
            raise ValidationError(
                f"승인으로 변경할 수 없는 필드입니다: {sorted(unknown)}",
                detail={"allowed": sorted(self.allowed_fields)},
            )
        if request_type is not None:
            trespass = sorted(set(proposed) - self.fields_for(request_type))
            if trespass:
                raise ValidationError(
                    f"{request_type} 요청으로 바꿀 수 없는 필드입니다: {trespass}",
                    detail={"allowed_for_request_type": sorted(self.fields_for(request_type))},
                )

        # **JSONB 를 임의 저장소로 쓰지 않는다.** 도메인 모델로 형태를 강제한다.
        # 여기서 안 막으면 적용 시점(승인이 끝난 뒤)에야 형식 오류가 드러난다.
        from app.domain.channel_price import ChannelPriceIn

        try:
            ChannelPriceIn(**proposed)
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError(f"가격 값이 올바르지 않습니다: {exc}") from exc

    def apply_changes(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], actor_user_id: UUID,
        request_type: str | None = None, approval_request_id: UUID | None = None,
    ) -> dict[str, Any]:
        # 적용 시점에도 같은 검증을 한다 — DB 를 직접 건드려 심어도 막힌다.
        self.validate_proposed_changes(proposed, request_type=request_type)

        from app.domain.channel_price import ChannelPriceIn
        from app.services.channel_price import create_price_version

        payload = ChannelPriceIn(**proposed)
        # **커밋하지 않는다.** 가격 생성 · 승인 상태 전이 · 감사 로그 · outbox 가
        # 한 트랜잭션이어야 한다. 커밋 책임은 approval.apply() 에 있다.
        created = create_price_version(
            db, org_id=org_id, actor=actor_user_id, listing_id=target_id,
            payload=payload, approval_request_id=approval_request_id, _commit=False,
        )

        # 자동 반영이 켜져 있으면 **같은 트랜잭션에서** outbox 에 넣는다.
        # 외부 API 는 여기서 부르지 않는다 — 전송이 느리거나 실패하면
        # 승인 트랜잭션이 통째로 길어지거나 롤백된다.
        from app.services.channel_publish import enqueue_if_auto_publish

        enqueue_if_auto_publish(
            db, org_id=org_id, listing_id=target_id, price_id=created.id,
            actor=actor_user_id,
        )

        return self.build_snapshot(db, org_id=org_id, target_id=target_id)



class ExpenseAdapter:
    """사업비용 변경 승인.

    왜 비용이 승인 대상인가
    ---------------------
    비용은 **순이익의 입력값**이고, 순이익이 곧 파트너 수익배분이다.
    금액 하나가 바뀌면 그 달의 배분 금액이 바뀐다. 상품 원가와 같은 무게다.

    잠긴 월은 여기서 다루지 않는다
    ---------------------------
    `expenses` 는 `assert_period_writable` 로 이미 잠긴 월 쓰기를 막는다.
    승인이 그걸 우회하지 **않는다** — `approval.apply()` 도 같은 가드를 통과한다.
    잠긴 월을 고쳐야 하면 `EXPENSE_ADJUSTMENT` 로 지금 열린 달에 조정을 만든다.
    """

    target_type = "expense"

    # 요청 유형별 소유 필드. 겹치면 import 시점에 터진다(_assert_no_field_overlap).
    #
    # ⚠️ `expenses` 테이블에 **실제로 있는 컬럼만** 넣는다. 없는 컬럼을 열어두면
    #    적용 단계에서 조용히 실패한다.
    FIELD_OWNERSHIP: dict[str, frozenset[str]] = {
        # 금액·세무·정산에 영향을 주는 변경. 여기가 수익배분을 움직인다.
        "EXPENSE_FINANCIAL_CHANGE": frozenset({
            "gross_amount", "supply_amount", "vat_amount", "vat_inclusion_type",
            "tax_type", "is_manual_tax_override",
            "expense_date",          # 귀속월이 여기서 나온다
            "category_id",
            "payment_method", "payment_card_holder_id", "is_personal_card",
            "is_business_use",
            "is_tax_deductible", "is_settlement_deductible",
            "attributable_partner_id",   # 누구에게 귀속되는 비용인가 = 배분에 직결
            "supplier_id",
            "description",
        }),
        # 증빙 상태 되돌리기 — 승인 후 증빙을 지우는 건 감사 대상이다.
        "EXPENSE_EVIDENCE_CHANGE": frozenset({
            "evidence_type", "evidence_status",
        }),
    }

    allowed_fields = frozenset().union(*FIELD_OWNERSHIP.values())
    request_types = frozenset(FIELD_OWNERSHIP)

    def fields_for(self, request_type: str) -> frozenset[str]:
        return self.FIELD_OWNERSHIP.get(request_type, frozenset())

    _SELECT = """
        SELECT id, expense_date, category_id, description,
               gross_amount, supply_amount, vat_amount, vat_inclusion_type,
               tax_type, is_manual_tax_override,
               payment_method, payment_card_holder_id, is_personal_card,
               is_business_use, evidence_type, evidence_status,
               is_tax_deductible, is_settlement_deductible,
               attributable_partner_id, supplier_id, memo
          FROM expenses
         WHERE id = :id AND org_id = :org AND deleted_at IS NULL
    """

    def _row(self, db: Session, org_id: UUID, target_id: UUID):
        row = db.execute(
            text(self._SELECT), {"id": str(target_id), "org": str(org_id)}
        ).mappings().first()
        if row is None:
            # 조직 격리 — 다른 org 의 비용도 404 다.
            raise NotFoundError("사업비용을 찾을 수 없습니다.")
        return row

    def validate_target(self, db: Session, *, org_id: UUID, target_id: UUID) -> None:
        self._row(db, org_id, target_id)

    def build_snapshot(self, db: Session, *, org_id: UUID, target_id: UUID) -> dict[str, Any]:
        row = self._row(db, org_id, target_id)
        return {k: row[k] for k in sorted(self.allowed_fields) if k in row}

    def get_current_version(self, db: Session, *, org_id: UUID, target_id: UUID) -> str:
        return version_of(self.build_snapshot(db, org_id=org_id, target_id=target_id))

    def get_effective_date(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], effective_from: date | None,
    ) -> date:
        """회계 귀속일 = **비용 발생일**.

        승인 요청의 `effective_from`(언제 실행할까)이 아니라 비용이 귀속되는 날짜로
        월 잠금을 판정해야 한다. 8월에 7월 비용을 고치는 건 흔한 일이고,
        그건 **7월**이 잠겼는지로 판단해야 맞다.

        비용 발생일 자체를 바꾸는 요청이면 **바뀐 날짜와 원래 날짜 둘 다** 열려 있어야
        한다 — 7월에서 8월로 옮기면 7월 합계도 줄어들기 때문이다.
        여기서는 더 이른 쪽을 돌려주고, 나머지 한쪽은 서비스가 따로 검사한다.
        """
        row = self._row(db, org_id, target_id)
        current: date = row["expense_date"]

        proposed_date = proposed.get("expense_date")
        if proposed_date is None:
            return current
        if isinstance(proposed_date, str):
            proposed_date = date.fromisoformat(proposed_date[:10])
        return min(current, proposed_date)

    def validate_proposed_changes(
        self, proposed: dict[str, Any], *, request_type: str | None = None
    ) -> None:
        if not proposed:
            raise ValidationError("변경할 내용이 없습니다.")
        unknown = set(proposed) - self.allowed_fields
        if unknown:
            raise ValidationError(
                f"승인으로 변경할 수 없는 필드입니다: {sorted(unknown)}",
                detail={"allowed": sorted(self.allowed_fields)},
            )
        if request_type is not None:
            owned = self.fields_for(request_type)
            trespass = sorted(set(proposed) - owned)
            if trespass:
                owner_of = {
                    f: rt for rt, fs in self.FIELD_OWNERSHIP.items() for f in fs if f in trespass
                }
                raise ValidationError(
                    f"{request_type} 요청으로 바꿀 수 없는 필드입니다: {trespass}",
                    detail={"owned_by": owner_of, "allowed_for_request_type": sorted(owned)},
                )

        # 금액은 음수가 될 수 없다. 되돌림은 조정으로 처리한다.
        for key in ("gross_amount", "supply_amount", "vat_amount"):
            value = proposed.get(key)
            if value is not None and int(value) < 0:
                raise ValidationError(
                    f"{key} 는 음수가 될 수 없습니다. 되돌림은 조정 비용으로 처리하세요.",
                    detail={"field": key},
                )

    def apply_changes(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], actor_user_id: UUID,
        request_type: str | None = None, approval_request_id: UUID | None = None,
    ) -> dict[str, Any]:
        # 적용 시점에도 같은 검증을 한다 — DB 를 직접 건드려 심어도 막힌다.
        self.validate_proposed_changes(proposed, request_type=request_type)
        _ = approval_request_id  # 비용은 컬럼을 덮어써서 요청 id 를 담을 자리가 없다(감사 로그가 담당)

        from app.domain.expense import ExpenseUpdate
        from app.services.expense import update_expense

        before_date = self._row(db, org_id, target_id)["expense_date"]

        # 기존 비용 서비스를 재사용한다. raw UPDATE 를 쓰면 VAT 분리·세금 정책 스냅샷
        # 같은 기존 로직이 빠져 금액이 어긋난다.
        update_expense(
            db, org_id=org_id, actor=actor_user_id, expense_id=target_id,
            body=ExpenseUpdate(**proposed), _internal_approval_apply=True,
        )

        after = self.build_snapshot(db, org_id=org_id, target_id=target_id)

        # 손익이 낡았다고 **표시만** 남긴다. 여기서 전체를 재집계하지 않는다 —
        # 느리고, 실패하면 승인이 통째로 롤백된다.
        # 날짜가 바뀌었으면 두 달 모두 낡는다.
        from app.services.period_recalc import mark_period_dirty

        for d in {before_date, after.get("expense_date", before_date)}:
            if d:
                mark_period_dirty(db, org_id=org_id, at=d,
                                  reason="사업비용 승인 적용", actor=actor_user_id)

        return after


class ExpenseAttachmentAdapter:
    """증빙 파일 **삭제** 승인 (M027).

    왜 별도 target_type 인가
    ----------------------
    승인 프레임워크는 `target_id` 로 대상을 특정한다. 지우려는 것은 첨부 한 건이지
    비용 전체가 아니다. `expense` 대상에 "attachment_id" 같은 가짜 필드를 만들면
    한 비용에 첨부가 여러 개일 때 어느 것을 지우는지 요청만 봐서는 알 수 없고,
    열린 요청 UNIQUE(org, target_type, target_id, request_type)가
    **첨부 단위가 아니라 비용 단위**로 걸려 두 번째 삭제 요청이 막힌다.

    request_type 은 `EXPENSE_EVIDENCE_CHANGE` 를 그대로 쓴다 — 증빙 상태를 되돌리는
    것과 증빙 파일을 지우는 것은 같은 성격의 감사 대상이고, 승인자 입장에서
    구분할 이유가 없다.

    무엇을 바꾸나
    -----------
    `status` 하나뿐이고 값은 `PENDING_DELETE` 만 허용한다.
    **적용 시점에 파일을 지우지 않는다** — 승인 트랜잭션이 뒤에 롤백되면
    파일만 사라지고 되돌릴 방법이 없다. worker 가 지운 뒤에야 `DELETED` 가 된다.
    """

    target_type = "expense_attachment"

    FIELD_OWNERSHIP: dict[str, frozenset[str]] = {
        "EXPENSE_EVIDENCE_CHANGE": frozenset({"status"}),
    }
    allowed_fields = frozenset().union(*FIELD_OWNERSHIP.values())
    request_types = frozenset(FIELD_OWNERSHIP)

    # 승인으로 갈 수 있는 상태는 삭제 요청 하나뿐이다. 다른 값을 열어 주면
    # 승인으로 파일을 "되살린" 것처럼 보이게 만들 수 있다.
    ALLOWED_STATUS = frozenset({"PENDING_DELETE"})

    def fields_for(self, request_type: str) -> frozenset[str]:
        return self.FIELD_OWNERSHIP.get(request_type, frozenset())

    _SELECT = """
        SELECT a.id, a.expense_id, a.original_filename, a.file_hash,
               a.mime_type, a.size_bytes, a.status, e.expense_date
          FROM expense_attachment a
          JOIN expenses e ON e.id = a.expense_id AND e.org_id = a.org_id
         WHERE a.id = :id AND a.org_id = :org AND a.deleted_at IS NULL
    """

    def _row(self, db: Session, org_id: UUID, target_id: UUID):
        row = db.execute(
            text(self._SELECT), {"id": str(target_id), "org": str(org_id)}
        ).mappings().first()
        if row is None:
            raise NotFoundError("증빙 파일을 찾을 수 없습니다.")
        return row

    def validate_target(self, db: Session, *, org_id: UUID, target_id: UUID) -> None:
        self._row(db, org_id, target_id)

    def build_snapshot(self, db: Session, *, org_id: UUID, target_id: UUID) -> dict[str, Any]:
        row = self._row(db, org_id, target_id)
        # 파일명·해시를 스냅샷에 남긴다. 파일이 지워진 뒤에도 승인 이력만 보고
        # **무엇을 지웠는지** 말할 수 있어야 한다.
        return {
            "status": row["status"],
            "filename": row["original_filename"],
            "file_hash": row["file_hash"],
            "size_bytes": row["size_bytes"],
        }

    def get_current_version(self, db: Session, *, org_id: UUID, target_id: UUID) -> str:
        return version_of(self.build_snapshot(db, org_id=org_id, target_id=target_id))

    def get_effective_date(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], effective_from: date | None,
    ) -> date:
        """귀속일은 **비용의 발생일**이다. 증빙은 비용에 딸린 것이라
        그 비용이 속한 달의 잠금을 따른다."""
        _ = (proposed, effective_from)
        return self._row(db, org_id, target_id)["expense_date"]

    def validate_proposed_changes(
        self, proposed: dict[str, Any], *, request_type: str | None = None
    ) -> None:
        if not proposed:
            raise ValidationError("변경할 내용이 없습니다.")
        unknown = set(proposed) - self.allowed_fields
        if unknown:
            raise ValidationError(
                f"승인으로 변경할 수 없는 필드입니다: {sorted(unknown)}",
                detail={"allowed": sorted(self.allowed_fields)},
            )
        if request_type is not None and request_type not in self.request_types:
            raise ValidationError(
                f"{request_type} 요청으로는 증빙을 바꿀 수 없습니다.",
                detail={"allowed_request_types": sorted(self.request_types)},
            )
        status = proposed.get("status")
        if status not in self.ALLOWED_STATUS:
            raise ValidationError(
                "증빙 승인으로 지정할 수 있는 상태는 삭제 요청뿐입니다.",
                detail={"allowed": sorted(self.ALLOWED_STATUS), "got": status},
            )

    def apply_changes(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], actor_user_id: UUID,
        request_type: str | None = None, approval_request_id: UUID | None = None,
    ) -> dict[str, Any]:
        self.validate_proposed_changes(proposed, request_type=request_type)

        from app.services.expense_attachment import mark_pending_delete

        # **커밋하지 않는다.** 상태 전이 · 승인 상태 · 감사 로그가 한 트랜잭션이어야
        # 한다 — 커밋 책임은 approval.apply() 에 있다.
        mark_pending_delete(
            db, org_id=org_id, actor=actor_user_id, attachment_id=target_id,
            approval_request_id=approval_request_id, _commit=False,
        )
        return self.build_snapshot(db, org_id=org_id, target_id=target_id)


class MonthReopenAdapter:
    """월 재오픈 승인 (Phase 29).

    잠긴 달을 다시 여는 것은 **확정한 숫자를 무효화하는 행위**다. 그 달의 순이익으로
    파트너 배분이 이미 계산됐을 수 있고, 지급까지 끝났을 수도 있다.
    그래서 승인을 거친다.

    왜 월 잠금 가드를 면제하는가
    -------------------------
    승인 적용 단계는 `assert_period_writable` 로 잠긴 달의 변경을 막는다.
    그런데 **재오픈이 바로 그 잠금을 푸는 연산**이다. 대상 월이 잠겨 있는 것은
    이 요청의 전제 조건이지 거부 사유가 아니다. 가드를 그대로 두면
    이 요청은 영원히 `HELD_BY_LOCK` 으로 남는다.

    면제를 안전하게 만드는 것
    ----------------------
    · `month_lock_exempt` 는 **어댑터 클래스 속성**이다. 요청 payload 의 문자열이
      아니므로 클라이언트가 설정할 수 없다
    · 이 어댑터가 바꿀 수 있는 것은 `close_state` → `OPEN` 하나뿐이다.
      판매·비용·정산 데이터를 건드리는 경로가 없다
    · 재오픈 이후의 변경은 다시 열린 달에 대한 것이므로 정상적으로 가드를 받는다

    무엇을 보존하는가
    ---------------
    기존 snapshot 을 **지우지 않는다.** `monthly_close.unlock` 이 superseded 로
    내리고 세대를 남긴다. "재오픈 전에 뭐라고 확정했었나" 를 나중에 답할 수 있어야 한다.
    """

    target_type = "monthly_close"

    # ⚠️ 이 플래그가 있는 어댑터는 이것 하나여야 한다. 늘어나면
    #    승인이 월 잠금을 우회하는 일반 통로가 된다 (아래 _assert_single_lock_exemption).
    month_lock_exempt = True

    # ⚠️ 필드명이 `close_state` 가 아니다. 그 이름은 `SERVER_MANAGED_FIELDS` 로
    #    전역 차단돼 있다 — 승인 payload 가 워크플로 상태를 직접 지정하는 것을
    #    막기 위해서다. 여기서 요청하는 것은 컬럼 값이 아니라 **연산**이므로
    #    `reopen_to_state` 라는 별도 이름을 쓴다. 실제 전이는 `unlock` 이 한다.
    FIELD_OWNERSHIP: dict[str, frozenset[str]] = {
        "MONTH_REOPEN": frozenset({"reopen_to_state", "reopen_reason"}),
    }
    allowed_fields = frozenset().union(*FIELD_OWNERSHIP.values())
    request_types = frozenset(FIELD_OWNERSHIP)

    # 재오픈으로 갈 수 있는 상태는 하나뿐이다. 승인으로 임의 상태를 꽂을 수 없다.
    ALLOWED_STATE = "OPEN"
    MIN_REASON_LENGTH = 5

    def fields_for(self, request_type: str) -> frozenset[str]:
        return self.FIELD_OWNERSHIP.get(request_type, frozenset())

    _SELECT = """
        SELECT id, year_month, close_state, status, reopen_count,
               reopen_limit_override, net_profit, distributable_amount,
               close_generation, close_snapshot_id, locked_at
          FROM monthly_settlement
         WHERE id = :id AND org_id = :org AND deleted_at IS NULL
    """

    def _row(self, db: Session, org_id: UUID, target_id: UUID):
        row = db.execute(
            text(self._SELECT), {"id": str(target_id), "org": str(org_id)}
        ).mappings().first()
        if row is None:
            raise NotFoundError("월 마감 정보를 찾을 수 없습니다.")
        return row

    def validate_target(self, db: Session, *, org_id: UUID, target_id: UUID) -> None:
        row = self._row(db, org_id, target_id)
        if row["close_state"] != "LOCKED":
            # 열린 달을 "재오픈" 하는 것은 의미가 없다. 승인 없이 되돌릴 수 있는
            # 상태(CONFIRMED → REVIEW)를 승인 대상으로 만들면 절차만 늘어난다.
            raise ValidationError(
                f"{row['year_month']} 은 잠긴 달이 아닙니다(현재 {row['close_state']}). "
                "재오픈 승인은 LOCKED 상태에만 필요합니다.",
                detail={"close_state": row["close_state"]},
            )

    def build_snapshot(self, db: Session, *, org_id: UUID, target_id: UUID) -> dict[str, Any]:
        row = self._row(db, org_id, target_id)
        # 승인자가 "무엇을 무효화하는지" 를 보고 판단할 수 있어야 한다.
        # 확정 순이익과 세대를 스냅샷에 담는다.
        return {
            "year_month": row["year_month"],
            "close_state": row["close_state"],
            "reopen_count": row["reopen_count"],
            "close_generation": row["close_generation"],
            "confirmed_net_profit": row["net_profit"],
            "confirmed_distributable": row["distributable_amount"],
            "locked_at": row["locked_at"],
        }

    def get_current_version(self, db: Session, *, org_id: UUID, target_id: UUID) -> str:
        return version_of(self.build_snapshot(db, org_id=org_id, target_id=target_id))

    def get_effective_date(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], effective_from: date | None,
    ) -> date:
        """귀속일은 **그 달의 1일**이다.

        `month_lock_exempt` 때문에 이 값이 잠금 판정에 쓰이지는 않지만,
        승인 목록이 "어느 달에 대한 요청인가" 를 표시하려면 정확해야 한다.
        오늘 날짜를 돌려주면 8월 목록에 7월 재오픈 요청이 섞인다.
        """
        _ = (proposed, effective_from)
        ym = self._row(db, org_id, target_id)["year_month"]
        return date(int(ym[:4]), int(ym[5:7]), 1)

    def validate_proposed_changes(
        self, proposed: dict[str, Any], *, request_type: str | None = None
    ) -> None:
        if not proposed:
            raise ValidationError("변경할 내용이 없습니다.")
        unknown = set(proposed) - self.allowed_fields
        if unknown:
            raise ValidationError(
                f"승인으로 변경할 수 없는 필드입니다: {sorted(unknown)}",
                detail={"allowed": sorted(self.allowed_fields)},
            )
        if request_type is not None and request_type not in self.request_types:
            raise ValidationError(
                f"{request_type} 요청으로는 월 마감을 바꿀 수 없습니다.",
                detail={"allowed_request_types": sorted(self.request_types)},
            )
        if proposed.get("reopen_to_state") != self.ALLOWED_STATE:
            raise ValidationError(
                "재오픈 승인으로 지정할 수 있는 상태는 OPEN 뿐입니다.",
                detail={"allowed": self.ALLOWED_STATE,
                        "got": proposed.get("reopen_to_state")},
            )
        reason = (proposed.get("reopen_reason") or "").strip()
        if len(reason) < self.MIN_REASON_LENGTH:
            # 사유 없는 재오픈은 나중에 아무도 설명하지 못한다.
            raise ValidationError(
                f"재오픈 사유를 {self.MIN_REASON_LENGTH}자 이상 입력하세요.",
                detail={"field": "reopen_reason"},
            )

    def apply_changes(
        self, db: Session, *, org_id: UUID, target_id: UUID,
        proposed: dict[str, Any], actor_user_id: UUID,
        request_type: str | None = None, approval_request_id: UUID | None = None,
    ) -> dict[str, Any]:
        self.validate_proposed_changes(proposed, request_type=request_type)

        from app.services import monthly_close

        year_month = self._row(db, org_id, target_id)["year_month"]
        # `unlock` 이 한도(D-285) 검사, snapshot supersede, 재오픈 감사까지 한다.
        # 여기서 UPDATE 를 직접 쓰면 그게 전부 빠진다.
        #
        # ⚠️ `unlock` 은 내부에서 커밋한다. 승인 적용 트랜잭션과 분리되지만,
        #    적용 상태 전이가 뒤에 실패하면 "달은 열렸는데 요청은 APPROVED" 가 된다.
        #    그 경우 재실행하면 `validate_target` 이 "잠긴 달이 아니다" 로 막고,
        #    사용자는 요청을 취소하면 된다 — 데이터가 틀어지지는 않는다.
        monthly_close.unlock(
            db, org_id=org_id, year_month=year_month, actor_user_id=actor_user_id,
            reason=proposed["reopen_reason"], _approved_via_request=approval_request_id,
        )
        return self.build_snapshot(db, org_id=org_id, target_id=target_id)


# ── 레지스트리 ────────────────────────────────────────────────
# M018 에서 지원하는 대상만 등록한다.
_REGISTRY: dict[str, Any] = {
    ProductAdapter.target_type: ProductAdapter(),
    ExpenseAttachmentAdapter.target_type: ExpenseAttachmentAdapter(),
    ChannelListingAdapter.target_type: ChannelListingAdapter(),
    ExpenseAdapter.target_type: ExpenseAdapter(),
    MonthReopenAdapter.target_type: MonthReopenAdapter(),
}

# 재무 정책 (Phase A-2). 세율·수수료·배분 비율은 계산의 입력값이라
# 상품 원가·사업비용과 같은 무게로 승인 대상이다.
# 정책 종류마다 테이블과 컬럼이 달라 별도 모듈에서 만든다.
from app.services.approval_policy import build_adapters as _build_policy_adapters  # noqa: E402

_REGISTRY.update(_build_policy_adapters())

# 정산 수동 매칭 (docs/47 §3). 정산 건을 주문에 강제로 연결하는 것은 손익
# 귀속을 바꾸는 행위라 비용 금액 변경과 같은 무게다.
# 별도 모듈인 이유는 이 파일이 이미 충분히 길고, 정산은 어댑터 하나로
# 끝나지 않을 도메인이기 때문이다(조정·확정이 뒤따른다).
from app.services.approval_settlement import build_adapters as _build_settlement  # noqa: E402

_REGISTRY.update(_build_settlement())


def get_adapter(target_type: str):
    adapter = _REGISTRY.get(target_type)
    if adapter is None:
        raise ValidationError(
            f"아직 승인 대상으로 지원하지 않는 유형입니다: {target_type}",
            detail={"supported": sorted(_REGISTRY)},
        )
    return adapter


def supported_target_types() -> list[str]:
    return sorted(_REGISTRY)


def supported_request_types(target_type: str) -> list[str]:
    return sorted(get_adapter(target_type).request_types)


def owned_fields(target_type: str, request_type: str) -> frozenset[str]:
    """해당 요청 유형이 소유한 필드."""
    adapter = get_adapter(target_type)
    getter = getattr(adapter, "fields_for", None)
    return getter(request_type) if getter else adapter.allowed_fields


def all_approval_fields(target_type: str) -> frozenset[str]:
    """이 대상에서 **승인을 거쳐야만** 바꿀 수 있는 필드 전체.

    일반 수정 API 가 이 목록을 거부해야 승인이 우회되지 않는다.
    """
    return get_adapter(target_type).allowed_fields


def _assert_no_field_overlap() -> None:
    """한 필드가 두 request_type 에 속하면 **import 시점에 터진다.**

    런타임에 발견하면 이미 두 승인이 같은 필드를 물고 있는 상태다.
    설정 실수는 배포 전에 알아야 한다.
    """
    for target_type, adapter in _REGISTRY.items():
        ownership = getattr(adapter, "FIELD_OWNERSHIP", None)
        if not ownership:
            continue
        seen: dict[str, str] = {}
        for request_type, fields in ownership.items():
            for f in fields:
                if f in seen:
                    raise RuntimeError(
                        f"승인 필드 소유권 충돌: {target_type}.{f} 가 "
                        f"{seen[f]} 와 {request_type} 양쪽에 정의되어 있습니다. "
                        "한 필드는 하나의 request_type 만 소유해야 합니다 "
                        "(그렇지 않으면 두 승인이 서로를 덮어씁니다)."
                    )
                seen[f] = request_type


def _assert_single_lock_exemption() -> None:
    """월 잠금 면제 어댑터는 **하나뿐이어야 한다.**

    면제가 늘어나면 승인이 월 잠금을 우회하는 일반 통로가 된다. 재오픈은
    "잠금을 푸는 연산" 이라 면제가 논리적으로 필요하지만, 그 외의 어떤 변경도
    잠긴 달에 들어가면 안 된다.

    새 면제가 정말 필요하면 이 목록을 **의도적으로** 고치고 그 이유를 남겨야 한다.
    실수로 플래그를 켠 경우는 import 시점에 터진다.
    """
    exempt = sorted(
        t for t, a in _REGISTRY.items() if getattr(a, "month_lock_exempt", False)
    )
    if exempt != ["monthly_close"]:
        raise RuntimeError(
            f"월 잠금 면제 어댑터가 예상과 다릅니다: {exempt}. "
            "재오픈(monthly_close) 외의 대상이 잠긴 달을 바꿀 수 있으면 안 됩니다."
        )


_assert_no_field_overlap()
_assert_single_lock_exemption()
