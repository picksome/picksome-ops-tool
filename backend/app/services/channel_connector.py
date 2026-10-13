"""판매처 connector 레지스트리 — **지원 여부를 정직하게 보고한다.**

왜 이 파일이 있나
----------------
가격을 판매처에 전송하려면 마켓별 API connector 가 필요하다. 지금은 **하나도 없다.**

그럴 때 흔히 하는 두 가지가 다 나쁘다.

  · 빈 껍데기 connector 를 만들고 성공을 반환한다 → 사용자는 반영됐다고 믿는다.
    실제 판매처 가격은 그대로다. 이건 거짓말이고, 가장 늦게 발견된다.
  · 아무것도 안 만들고 화면이 판매처 이름으로 추측한다 → 나중에 connector 가 생겨도
    화면을 찾아 고쳐야 하고, 빠뜨린 화면이 남는다.

그래서 **레지스트리가 지원 여부를 명시적으로 답한다.** 지금은 전부 미지원이고,
connector 가 생기면 이 표에 등록하는 것만으로 화면·outbox·API 가 함께 바뀐다.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ConnectorSupport:
    marketplace: str
    price_publish_supported: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "marketplace": self.marketplace,
            "price_publish_supported": self.price_publish_supported,
            "reason": self.reason,
        }


_NOT_CONNECTED = "가격 변경 connector가 아직 연결되지 않았습니다."

# 판매처 → 가격 전송 지원 여부.
#
# **전부 False 다.** 실제 자격증명도 API 구현도 없다.
# connector 를 만들면 여기에 True 로 등록한다 — 그 순간부터 화면의 반영 버튼이
# 살아나고 auto_publish 설정이 허용된다.
_REGISTRY: dict[str, ConnectorSupport] = {
    m: ConnectorSupport(m, False, _NOT_CONNECTED)
    for m in ("COUPANG", "SMARTSTORE", "ELEVENST", "GMARKET",
              "AUCTION", "SELF_MALL", "TEMU", "OTHER")
}


def support_for(marketplace: str | None) -> ConnectorSupport:
    """판매처의 가격 전송 지원 여부.

    모르는 판매처도 **미지원으로 답한다.** 기본값을 "지원함" 으로 두면
    새 판매처가 추가될 때마다 조용히 전송을 시도하고 실패가 쌓인다.
    """
    if not marketplace:
        return ConnectorSupport("UNKNOWN", False, _NOT_CONNECTED)
    return _REGISTRY.get(
        marketplace.upper(),
        ConnectorSupport(marketplace.upper(), False, _NOT_CONNECTED),
    )


def all_support() -> list[dict[str, object]]:
    return [s.as_dict() for s in sorted(_REGISTRY.values(), key=lambda s: s.marketplace)]


def assert_publish_supported(marketplace: str | None) -> None:
    """전송을 시도하기 전에 부른다. 미지원이면 여기서 멈춘다."""
    from app.core.exceptions import ConnectorUnavailableError

    support = support_for(marketplace)
    if not support.price_publish_supported:
        raise ConnectorUnavailableError(
            support.reason or _NOT_CONNECTED,
            detail={"marketplace": support.marketplace},
        )
