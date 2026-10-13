/**
 * 가격 표시 규칙 단위 테스트.
 *
 * 여기서 막는 사고
 *   · 미입력 가격이 "0원" 으로 보여 사용자가 판매가가 0원이라고 믿는 것
 *   · 승인 상태 / 내부 적용 / 외부 반영이 한 덩어리로 보여
 *     "승인은 났는데 반영이 실패한" 상태를 못 읽는 것
 *   · connector 가 없는데 "미요청" 으로 보여 "요청하면 되겠네" 라고 오해하는 것
 */

import { describe, expect, it } from "vitest";
import {
  canPublish, formatPriceCell, internalPhaseOf, publishLabel,
  INTERNAL_PHASE_LABEL, NOT_ENTERED,
  type ChannelPrice,
} from "@/lib/api/channel-prices";

function price(over: Partial<ChannelPrice> = {}): ChannelPrice {
  return {
    id: "p1", listing_id: "l1", product_variant_id: null, currency: "KRW",
    regular_price: 19900, sale_price: null, sale_start_at: null, sale_end_at: null,
    effective_from: "2026-01-01", effective_to: null, status: "ACTIVE",
    approval_request_id: null, publish_status: "NOT_REQUESTED",
    publish_requested_at: null, published_at: null, publish_error: null,
    publish_attempt_count: 0, created_at: "2026-01-01T00:00:00Z",
    ...over,
  };
}

describe("formatPriceCell — 미입력과 0원은 다르다", () => {
  it("null 은 '미입력' 이다", () => {
    expect(formatPriceCell(null)).toBe(NOT_ENTERED);
    expect(formatPriceCell(undefined)).toBe(NOT_ENTERED);
  });

  it("0 은 '0원' 이다 — 미입력으로 뭉개지 않는다", () => {
    // 이 둘을 같게 표시하면 사용자가 판매가가 0원이라고 믿고 마진을 판단한다.
    expect(formatPriceCell(0)).toBe("0원");
  });

  it("금액에 천 단위 구분을 넣는다", () => {
    expect(formatPriceCell(19900)).toBe("19,900원");
    expect(formatPriceCell(1234567)).toBe("1,234,567원");
  });
});

describe("internalPhaseOf — 반개구간 [from, to)", () => {
  it("시작일 이후·종료일 전이면 적용 중", () => {
    expect(internalPhaseOf(price({ effective_from: "2026-01-01" }), "2026-06-01"))
      .toBe("ACTIVE");
  });

  it("시작일이 미래면 적용 예정", () => {
    expect(internalPhaseOf(price({ effective_from: "2030-01-01" }), "2026-06-01"))
      .toBe("SCHEDULED");
  });

  it("시작일 당일은 적용 중이다", () => {
    expect(internalPhaseOf(price({ effective_from: "2026-06-01" }), "2026-06-01"))
      .toBe("ACTIVE");
  });

  it("종료일 당일은 이미 종료다 — 반개구간이라 그 날 다음 가격이 적용된다", () => {
    expect(internalPhaseOf(
      price({ effective_from: "2026-01-01", effective_to: "2026-06-01" }), "2026-06-01",
    )).toBe("EXPIRED");
  });

  it("종료일 하루 전은 아직 적용 중", () => {
    expect(internalPhaseOf(
      price({ effective_from: "2026-01-01", effective_to: "2026-06-01" }), "2026-05-31",
    )).toBe("ACTIVE");
  });

  it("취소된 가격은 기간과 무관하게 취소다", () => {
    expect(internalPhaseOf(price({ status: "CANCELLED" }), "2026-06-01")).toBe("CANCELLED");
  });

  it("모든 상태에 한글 라벨이 있다", () => {
    for (const phase of ["ACTIVE", "SCHEDULED", "EXPIRED", "CANCELLED"] as const) {
      expect(INTERNAL_PHASE_LABEL[phase]).toBeTruthy();
    }
  });
});

describe("publishLabel — 외부 반영은 내부 적용과 별개다", () => {
  it("connector 가 없으면 '미연결' 이다", () => {
    // "미요청" 으로 보이면 사용자가 "요청하면 되겠네" 라고 생각한다. 눌러도 안 된다.
    expect(publishLabel("NOT_REQUESTED", false)).toBe("미연결");
  });

  it("connector 가 없으면 다른 상태여도 미연결로 덮는다", () => {
    expect(publishLabel("FAILED", false)).toBe("미연결");
  });

  it("connector 가 있으면 실제 상태를 보여준다", () => {
    expect(publishLabel("NOT_REQUESTED", true)).toBe("미요청");
    expect(publishLabel("QUEUED", true)).toBe("반영 대기");
    expect(publishLabel("PUBLISHED", true)).toBe("반영 완료");
    expect(publishLabel("FAILED", true)).toBe("반영 실패");
    expect(publishLabel("VERIFICATION_FAILED", true)).toBe("반영 확인 실패");
  });
});

describe("canPublish — 버튼 활성 조건과 사유", () => {
  const connected = {
    price_publish_supported: true, price_publish_unsupported_reason: null,
  };
  const notConnected = {
    price_publish_supported: false,
    price_publish_unsupported_reason: "가격 변경 connector가 아직 연결되지 않았습니다.",
  };

  it("connector 가 없으면 비활성 + 서버가 준 사유를 그대로 보여준다", () => {
    const r = canPublish(notConnected, price());
    expect(r.enabled).toBe(false);
    // 화면이 사유를 지어내지 않는다 — 서버가 아는 것을 그대로 전한다.
    expect(r.reason).toBe(notConnected.price_publish_unsupported_reason);
  });

  it("사유가 비어 있어도 안내 문구는 남는다", () => {
    const r = canPublish(
      { price_publish_supported: false, price_publish_unsupported_reason: null },
      price(),
    );
    expect(r.enabled).toBe(false);
    expect(r.reason).toContain("연결되지 않았습니다");
  });

  it("이미 진행 중이면 비활성 — 연타로 중복 작업이 쌓이지 않게", () => {
    expect(canPublish(connected, price({ publish_status: "QUEUED" })).enabled).toBe(false);
    expect(canPublish(connected, price({ publish_status: "PUBLISHING" })).enabled).toBe(false);
  });

  it("반영 실패는 다시 시도할 수 있다", () => {
    const r = canPublish(connected, price({ publish_status: "FAILED" }));
    expect(r.enabled).toBe(true);
    expect(r.reason).toBeNull();
  });

  it("connector 가 있고 미요청이면 활성", () => {
    expect(canPublish(connected, price()).enabled).toBe(true);
  });
});

describe("상태 분리 — 세 축이 서로를 덮지 않는다", () => {
  it("내부 적용 중인데 외부 반영은 실패일 수 있다", () => {
    const p = price({ publish_status: "FAILED", effective_from: "2026-01-01" });
    expect(internalPhaseOf(p, "2026-06-01")).toBe("ACTIVE");
    expect(publishLabel(p.publish_status, true)).toBe("반영 실패");
  });

  it("적용 예정인데 외부 반영은 아직 미요청일 수 있다", () => {
    const p = price({ effective_from: "2030-01-01" });
    expect(internalPhaseOf(p, "2026-06-01")).toBe("SCHEDULED");
    expect(publishLabel(p.publish_status, true)).toBe("미요청");
  });

  it("승인 요청 연결은 별도 필드다 — 상태에 섞이지 않는다", () => {
    const p = price({ approval_request_id: "req-1" });
    expect(internalPhaseOf(p, "2026-06-01")).toBe("ACTIVE");
    expect(p.approval_request_id).toBe("req-1");
  });
});
