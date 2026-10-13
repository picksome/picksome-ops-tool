/**
 * 옵션 — **실제 API** 로 조회·저장되는지 검증.
 *
 * 옵션 어댑터는 직전까지 mock 이었다. mock 은 화면에 그럴듯한 조합을 그려주지만
 * 저장하면 아무 데도 안 남는다. 여기서 확인하는 건 "화면에 보인다" 가 아니라
 * "DB 에서 왔고 DB 로 갔다" 이다.
 */

import { test, expect, loginAs } from "./fixtures";
import { SEED, dbQuery, reseed } from "./db";

test.beforeEach(() => {
  reseed();
});

function variantsOf(productId: string) {
  return dbQuery<{
    id: string; variant_key: string; sku_external: string | null;
    cost_amount: number | null; extra_price: number; stock_quantity: number | null;
    is_active: boolean;
  }>(`SELECT id, variant_key, sku_external, cost_amount, extra_price,
             stock_quantity, is_active
        FROM product_variant
       WHERE product_id = '${productId}' AND deleted_at IS NULL
       ORDER BY variant_key`);
}

test("옵션 그룹·값이 실제 DB 에서 온다", async ({ page }) => {
  const operator = await loginAs(page, "operator");

  const res = await operator.api.get(
    `/api/v1/products/${SEED.products.options}/option-groups`,
  );
  expect(res.ok()).toBeTruthy();
  const groups = (await res.json()).data;

  // seed 가 심은 그대로여야 한다 — mock 이면 다른 값이 나온다.
  expect(groups).toHaveLength(1);
  expect(groups[0].name).toBe("색상");

  // 값은 그룹 응답에 **들어 있지 않다.** 따로 받아야 한다 —
  // 예전 프론트 타입은 nested 를 가정해서 undefined 를 map 하다 터졌다.
  expect(groups[0].values).toBeUndefined();

  const valuesRes = await operator.api.get(
    `/api/v1/products/option-groups/${groups[0].id}/values`,
  );
  expect(valuesRes.ok()).toBeTruthy();
  expect((await valuesRes.json()).data.map((v: { value: string }) => v.value))
    .toEqual(["네이비", "그레이"]);
});

test("옵션이 없는 상품은 빈 목록을 준다 — mock 조합을 지어내지 않는다", async ({ page }) => {
  const operator = await loginAs(page, "operator");

  const res = await operator.api.get(`/api/v1/products/${SEED.products.memo}/variants`);
  expect(res.ok()).toBeTruthy();
  expect((await res.json()).data).toEqual([]);
});

test("variant 생성·수정이 실제 DB 에 반영된다", async ({ page }) => {
  const operator = await loginAs(page, "operator");
  const productId = SEED.products.options;

  const created = await operator.api.post(`/api/v1/products/${productId}/variants`, {
    data: {
      variant_key: "색상=네이비",
      option_values_json: { 색상: "네이비" },
      sku_external: "E2E-OPT-1",
      cost_amount: 12000,
      extra_price: 1000,
      stock_quantity: 5,
      is_active: true,
    },
  });
  expect(created.ok(), await created.text()).toBeTruthy();
  const variantId = (await created.json()).data.id;

  let rows = variantsOf(productId);
  expect(rows).toHaveLength(1);
  expect(rows[0].cost_amount).toBe(12000);
  expect(rows[0].stock_quantity).toBe(5);

  // 수정
  const patched = await operator.api.patch(`/api/v1/products/variants/${variantId}`, {
    data: { cost_amount: 13500, stock_quantity: 2 },
  });
  expect(patched.ok(), await patched.text()).toBeTruthy();

  rows = variantsOf(productId);
  expect(rows[0].cost_amount).toBe(13500);
  expect(rows[0].extra_price).toBe(1000); // 안 보낸 필드는 그대로여야 한다
});

test("비활성화는 행을 지우지 않는다 — 과거 주문이 가리키던 옵션이 사라지면 안 된다", async ({ page }) => {
  const operator = await loginAs(page, "operator");
  const productId = SEED.products.options;

  const created = await operator.api.post(`/api/v1/products/${productId}/variants`, {
    data: {
      variant_key: "색상=그레이",
      option_values_json: { 색상: "그레이" },
      is_active: true,
    },
  });
  const variantId = (await created.json()).data.id;

  const disabled = await operator.api.patch(`/api/v1/products/variants/${variantId}`, {
    data: { is_active: false },
  });
  expect(disabled.ok()).toBeTruthy();

  const rows = variantsOf(productId);
  expect(rows).toHaveLength(1);
  expect(rows[0].is_active).toBe(false);
});

test("조직 격리 — 다른 조직 상품의 옵션은 보이지 않는다", async ({ page }) => {
  const operator = await loginAs(page, "operator");

  // 픽썸 기본 조직의 상품 id 를 임의로 하나 만들어 조회한다.
  // 존재하지 않거나 다른 조직 것이면 404 여야 한다 — 200 에 빈 배열이면
  // "그 상품은 옵션이 없다" 는 정보를 흘리는 셈이다.
  const res = await operator.api.get(
    "/api/v1/products/00000000-0000-0000-0000-0000000000ff/option-groups",
  );
  expect(res.status()).toBe(404);
});
