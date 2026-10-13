/**
 * 판매 주문 API 클라이언트 계약 테스트 (M032, Phase 5).
 *
 * 고정하는 것
 * ---------
 * 1. `integration_account_id` 는 **`marketplace` 와 다른 값**이다.
 *    같은 판매처에 계정이 여럿일 수 있다(쿠팡 본점 / 쿠팡 2호점).
 * 2. `null` 을 채널 이름으로 **대신 채우지 않는다.**
 *    "연동계정 미확인" 과 "쿠팡" 은 다른 말이다.
 * 3. 계정 필터가 서버로 나간다.
 *
 * 왜 필요한가
 * ---------
 * 이 저장소에서 프론트 타입이 백엔드 계약과 어긋난 사고가 네 번 있었다
 * (D2·D6·D9·D11). 그중 하나(`product_variant_id`)는 이 파일에도 있었다 —
 * 백엔드 `SaleOrderOut` 에 없는 필드를 타입이 선언하고 있었다.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { salesApi, type SaleOrder } from "./sales";

const calls: { url: string; method: string; body?: unknown }[] = [];
let payload: unknown = { data: [], meta: { total: 0 } };

beforeEach(() => {
  calls.length = 0;
  payload = { data: [], meta: { total: 0 } };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        url: String(input),
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      });
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify(payload),
      } as unknown as Response;
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

const last = () => calls[calls.length - 1];

/** 백엔드 `SaleOrderOut` (app/domain/sales.py) 이 실제로 주는 모양. */
function beOrder(overrides: Partial<SaleOrder> = {}): SaleOrder {
  return {
    id: "so-1",
    business_key: "SO-2026-00001",
    sale_date: "2026-03-10",
    marketplace: "COUPANG",
    marketplace_order_no: "ORD-1",
    line_no: 1,
    product_id: null,
    product_snapshot_name: "상품",
    quantity: 1,
    sale_price: 100_000,
    ad_cost: 0,
    shipping_fee: 0,
    vat_inclusion_type: "INCLUDED",
    memo: null,
    import_source: "MANUAL",
    created_at: "2026-03-10T00:00:00Z",
    integration_account_id: null,
    integration_account_label: null,
    ...overrides,
  };
}

describe("계정은 판매처와 다른 값이다", () => {
  it("같은 marketplace 의 서로 다른 계정을 구분해서 전달한다", async () => {
    payload = {
      data: [
        beOrder({
          id: "so-a", marketplace_order_no: "ORD-A",
          integration_account_id: "acc-main", integration_account_label: "본점",
        }),
        beOrder({
          id: "so-b", marketplace_order_no: "ORD-B",
          integration_account_id: "acc-second", integration_account_label: "2호점",
        }),
      ],
      meta: { total: 2 },
    };
    const res = await salesApi.list();

    // 판매처는 같다.
    expect(res.items.map((o) => o.marketplace)).toEqual(["COUPANG", "COUPANG"]);
    // 계정은 다르다 — 이게 이 컬럼의 존재 이유다.
    expect(res.items.map((o) => o.integration_account_id)).toEqual(["acc-main", "acc-second"]);
    expect(res.items.map((o) => o.integration_account_label)).toEqual(["본점", "2호점"]);
  });
});

describe("legacy NULL 계정", () => {
  it("null 을 그대로 유지한다 — 채널 이름으로 채우지 않는다", async () => {
    payload = { data: [beOrder()], meta: { total: 1 } };
    const res = await salesApi.list();
    const order = res.items[0];

    expect(order.integration_account_id).toBeNull();
    expect(order.integration_account_label).toBeNull();
    // "COUPANG" 이 계정 자리로 새어 들어오면 안 된다.
    expect(order.integration_account_label).not.toBe(order.marketplace);
  });

  it("계정이 없는 주문도 목록에서 빠지지 않는다", async () => {
    payload = {
      data: [beOrder(), beOrder({ id: "so-2", integration_account_id: "acc-1" })],
      meta: { total: 2 },
    };
    const res = await salesApi.list();
    expect(res.items).toHaveLength(2);
  });
});

describe("계정 필터", () => {
  it("integration_account_id 를 쿼리로 보낸다", async () => {
    await salesApi.list({ integration_account_id: "acc-main" });
    expect(last().url).toContain("integration_account_id=acc-main");
  });

  it("계정을 지정하지 않으면 쿼리에 넣지 않는다 — 전체 조회다", async () => {
    await salesApi.list({ marketplace: "COUPANG" });
    expect(last().url).toContain("marketplace=COUPANG");
    expect(last().url).not.toContain("integration_account_id");
  });
});

describe("주문 생성", () => {
  it("계정을 지정하면 그대로 보낸다", async () => {
    payload = { data: beOrder({ integration_account_id: "acc-main" }) };
    await salesApi.create({
      sale_date: "2026-03-10",
      marketplace: "COUPANG",
      marketplace_order_no: "ORD-NEW",
      quantity: 1,
      sale_price: 100_000,
      integration_account_id: "acc-main",
    });
    expect(last().method).toBe("POST");
    expect(last().body).toMatchObject({ integration_account_id: "acc-main" });
  });

  it("계정 없이도 만들 수 있다 — 선택값이다", async () => {
    payload = { data: beOrder() };
    await salesApi.create({
      sale_date: "2026-03-10",
      marketplace: "COUPANG",
      marketplace_order_no: "ORD-NEW-2",
      quantity: 1,
      sale_price: 100_000,
    });
    expect(last().body).not.toHaveProperty("integration_account_id");
  });
});
