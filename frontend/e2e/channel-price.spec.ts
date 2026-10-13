/**
 * 판매처 가격 — **실제 백엔드와 실제 DB** 로 운영 흐름 전체를 검증.
 *
 * 여기서 지키는 것
 *   · 승인 전에는 가격이 바뀌지 않는다
 *   · 과거 가격은 금액이 그대로 남는다
 *   · variant 가격이 없으면 기본 가격으로 떨어진다
 *   · connector 가 없으면 성공을 만들지 않는다
 */

import { test, expect, apiAs, loginAs } from "./fixtures";
import { SEED, dbQuery, reseed } from "./db";

test.beforeEach(() => {
  reseed();
});

function pricesOf(listingId: string) {
  return dbQuery<{
    id: string; regular_price: number | null; sale_price: number | null;
    effective_from: string; effective_to: string | null; status: string;
    product_variant_id: string | null; publish_status: string;
    approval_request_id: string | null;
  }>(`SELECT id, regular_price, sale_price, effective_from, effective_to, status,
             product_variant_id, publish_status, approval_request_id
        FROM product_channel_price
       WHERE listing_id = '${listingId}' AND deleted_at IS NULL
       ORDER BY effective_from`);
}

async function createListing(api: import("@playwright/test").APIRequestContext,
                             accountId: string, external: string) {
  const res = await api.post(
    `/api/v1/products/${SEED.products.price}/channel-listings`,
    { data: { integration_account_id: accountId, external_product_id: external,
              status: "ACTIVE" } },
  );
  expect(res.ok(), await res.text()).toBeTruthy();
  return (await res.json()).data;
}

test("판매처 연결 생성 — 같은 상품을 두 스토어에 건다", async ({ page }) => {
  const op = await loginAs(page, "operator");

  const first = await createListing(op.api, SEED.accounts.main, "COUPANG-A-1");
  const second = await createListing(op.api, SEED.accounts.second, "COUPANG-B-1");

  expect(first.id).not.toBe(second.id);
  // 계정 라벨이 함께 온다 — 화면이 계정 UUID 를 보여줄 수는 없다.
  expect(first.account_label).toBe("E2E 본점");
  expect(second.account_label).toBe("E2E 2호점");

  // connector 가 없으므로 미지원이라고 정직하게 답한다.
  expect(first.price_publish_supported).toBe(false);
  expect(first.price_publish_unsupported_reason).toBeTruthy();
});

test("같은 계정에 같은 상품을 두 번 걸 수 없다", async ({ page }) => {
  const op = await loginAs(page, "operator");
  await createListing(op.api, SEED.accounts.main, "COUPANG-DUP-1");

  const again = await op.api.post(
    `/api/v1/products/${SEED.products.price}/channel-listings`,
    { data: { integration_account_id: SEED.accounts.main,
              external_product_id: "COUPANG-DUP-2" } },
  );
  expect(again.status()).toBe(409);
});

test("connector 미연결이면 auto_publish 를 켤 수 없다", async ({ page }) => {
  const op = await loginAs(page, "operator");

  const blocked = await op.api.post(
    `/api/v1/products/${SEED.products.price}/channel-listings`,
    { data: { integration_account_id: SEED.accounts.main, auto_publish_price: true } },
  );
  expect(blocked.status()).toBe(403);
  expect((await blocked.json()).error.code).toBe("CONNECTOR_UNAVAILABLE");

  // 수정 경로로도 못 켠다 — 생성에서 막고 수정에서 열어두면 우회된다.
  const listing = await createListing(op.api, SEED.accounts.main, "COUPANG-AUTO-1");
  const patched = await op.api.patch(`/api/v1/channel-listings/${listing.id}`, {
    data: { auto_publish_price: true },
  });
  expect(patched.status()).toBe(403);
});

test("가격 승인 — 승인 전 미변경, 적용 후 새 버전 생성", async ({ page }) => {
  const op = await loginAs(page, "operator");
  const listing = await createListing(op.api, SEED.accounts.main, "COUPANG-PRICE-1");

  // 최초 가격은 승인을 거쳐 만든다 — 직접 생성 경로를 열어두면 승인이 장식이 된다.
  const first = await op.api.post("/api/v1/approvals", {
    data: {
      target_type: "product_channel_listing",
      target_id: listing.id,
      request_type: "CHANNEL_PRICE_CHANGE",
      proposed_changes: { regular_price: 19900, effective_from: "2026-01-01" },
      reason: "최초 가격 등록",
      submit: true,
    },
  });
  expect(first.ok(), await first.text()).toBeTruthy();
  const firstId = (await first.json()).data.id;

  // ① 승인 전에는 가격이 없다
  expect(pricesOf(listing.id)).toHaveLength(0);

  const admin = await apiAs("approver");
  expect((await admin.post(`/api/v1/approvals/${firstId}/approve`, {
    data: { comment: "승인" },
  })).ok()).toBeTruthy();
  expect((await admin.post(`/api/v1/approvals/${firstId}/apply`)).ok()).toBeTruthy();

  // ② 적용 후 가격 버전이 생겼고 승인 요청과 연결돼 있다
  let rows = pricesOf(listing.id);
  expect(rows).toHaveLength(1);
  expect(rows[0].regular_price).toBe(19900);
  expect(rows[0].approval_request_id).toBe(firstId);
  // 내부 가격 확정과 외부 반영은 분리된다.
  expect(rows[0].publish_status).toBe("NOT_REQUESTED");

  // ── 인상 요청 ──
  const second = await op.api.post("/api/v1/approvals", {
    data: {
      target_type: "product_channel_listing",
      target_id: listing.id,
      request_type: "CHANNEL_PRICE_CHANGE",
      proposed_changes: { regular_price: 24900, effective_from: "2026-09-01" },
      reason: "매입가 인상 반영",
      submit: true,
    },
  });
  const secondId = (await second.json()).data.id;

  // 승인 전에는 여전히 19,900
  const beforeApply = await op.api.get(
    `/api/v1/channel-listings/${listing.id}/resolved-price?at=2026-09-01`);
  expect((await beforeApply.json()).data.resolved_price).toBe(19900);

  expect((await admin.post(`/api/v1/approvals/${secondId}/approve`, {
    data: { comment: "승인" },
  })).ok()).toBeTruthy();
  expect((await admin.post(`/api/v1/approvals/${secondId}/apply`)).ok()).toBeTruthy();
  await admin.dispose();

  // ③ **과거 가격의 금액이 그대로 남아 있다.** 기간만 닫혔다.
  rows = pricesOf(listing.id);
  expect(rows).toHaveLength(2);
  expect(rows[0].regular_price).toBe(19900);
  expect(rows[0].effective_to).toBe("2026-09-01");
  expect(rows[1].regular_price).toBe(24900);

  // ④ 시점별로 다른 가격이 나온다
  const before = await op.api.get(
    `/api/v1/channel-listings/${listing.id}/resolved-price?at=2026-08-31`);
  const after = await op.api.get(
    `/api/v1/channel-listings/${listing.id}/resolved-price?at=2026-09-01`);
  expect((await before.json()).data.resolved_price).toBe(19900);
  expect((await after.json()).data.resolved_price).toBe(24900);
});

test("미래 가격 예약 — 오늘은 옛 가격, 예약일부터 새 가격", async ({ page }) => {
  const op = await loginAs(page, "operator");
  const listing = await createListing(op.api, SEED.accounts.main, "COUPANG-FUTURE-1");
  const admin = await apiAs("approver");

  for (const [price, from] of [[19900, "2026-01-01"], [29900, "2030-01-01"]] as const) {
    const req = await op.api.post("/api/v1/approvals", {
      data: {
        target_type: "product_channel_listing", target_id: listing.id,
        request_type: "CHANNEL_PRICE_CHANGE",
        proposed_changes: { regular_price: price, effective_from: from },
        reason: `가격 ${price}`, submit: true,
      },
    });
    const id = (await req.json()).data.id;
    await admin.post(`/api/v1/approvals/${id}/approve`, { data: { comment: "ok" } });
    await admin.post(`/api/v1/approvals/${id}/apply`);
  }
  await admin.dispose();

  const now = await op.api.get(
    `/api/v1/channel-listings/${listing.id}/resolved-price?at=2026-06-01`);
  const future = await op.api.get(
    `/api/v1/channel-listings/${listing.id}/resolved-price?at=2030-06-01`);
  expect((await now.json()).data.resolved_price).toBe(19900);
  expect((await future.json()).data.resolved_price).toBe(29900);
});

test("variant 가격 — 없으면 기본 가격으로 떨어진다", async ({ page }) => {
  const op = await loginAs(page, "operator");
  const listing = await createListing(op.api, SEED.accounts.main, "COUPANG-VAR-1");
  const admin = await apiAs("approver");

  // 옵션 2개를 만든다.
  const variants: string[] = [];
  for (const color of ["네이비", "그레이"]) {
    const res = await op.api.post(`/api/v1/products/${SEED.products.price}/variants`, {
      data: { variant_key: `색상=${color}`, option_values_json: { 색상: color } },
    });
    expect(res.ok(), await res.text()).toBeTruthy();
    variants.push((await res.json()).data.id);
  }

  async function applyPrice(changes: Record<string, unknown>) {
    const req = await op.api.post("/api/v1/approvals", {
      data: {
        target_type: "product_channel_listing", target_id: listing.id,
        request_type: "CHANNEL_PRICE_CHANGE", proposed_changes: changes,
        reason: "가격", submit: true,
      },
    });
    expect(req.ok(), await req.text()).toBeTruthy();
    const id = (await req.json()).data.id;
    await admin.post(`/api/v1/approvals/${id}/approve`, { data: { comment: "ok" } });
    const applied = await admin.post(`/api/v1/approvals/${id}/apply`);
    expect(applied.ok(), await applied.text()).toBeTruthy();
  }

  await applyPrice({ regular_price: 19900, effective_from: "2026-01-01" });
  // 네이비만 별도 가격. **완전 판매가**지 추가금이 아니다.
  await applyPrice({ regular_price: 22900, effective_from: "2026-01-01",
                     product_variant_id: variants[0] });
  await admin.dispose();

  const navy = await op.api.get(
    `/api/v1/channel-listings/${listing.id}/resolved-price` +
    `?variant_id=${variants[0]}&at=2026-06-01`);
  const grey = await op.api.get(
    `/api/v1/channel-listings/${listing.id}/resolved-price` +
    `?variant_id=${variants[1]}&at=2026-06-01`);

  const navyBody = (await navy.json()).data;
  const greyBody = (await grey.json()).data;

  expect(navyBody.resolved_price).toBe(22900);
  expect(navyBody.variant_fallback).toBe(false);

  // 그레이는 자기 가격이 없다 → 기본 가격. **fallback 이었음을 표시한다.**
  expect(greyBody.resolved_price).toBe(19900);
  expect(greyBody.variant_fallback).toBe(true);
});

test("중복 기간 가격은 거부된다", async ({ page }) => {
  const op = await loginAs(page, "operator");
  const listing = await createListing(op.api, SEED.accounts.main, "COUPANG-OVERLAP-1");
  const admin = await apiAs("approver");

  async function applyPrice(from: string) {
    const req = await op.api.post("/api/v1/approvals", {
      data: {
        target_type: "product_channel_listing", target_id: listing.id,
        request_type: "CHANNEL_PRICE_CHANGE",
        proposed_changes: { regular_price: 19900, effective_from: from },
        reason: "가격", submit: true,
      },
    });
    const id = (await req.json()).data.id;
    await admin.post(`/api/v1/approvals/${id}/approve`, { data: { comment: "ok" } });
    return admin.post(`/api/v1/approvals/${id}/apply`);
  }

  expect((await applyPrice("2026-06-01")).ok()).toBeTruthy();
  // 같은 시작일은 과거를 덮어쓰는 셈이라 막힌다.
  const dup = await applyPrice("2026-06-01");
  expect(dup.ok()).toBeFalsy();
  await admin.dispose();

  // 가격은 여전히 한 건이다.
  expect(pricesOf(listing.id)).toHaveLength(1);
});

test("가격 민감정보 — 권한 없는 사용자에게 금액을 내려주지 않는다", async ({ page }) => {
  const op = await loginAs(page, "operator");
  const listing = await createListing(op.api, SEED.accounts.main, "COUPANG-SENS-1");

  const req = await op.api.post("/api/v1/approvals", {
    data: {
      target_type: "product_channel_listing", target_id: listing.id,
      request_type: "CHANNEL_PRICE_CHANGE",
      proposed_changes: { regular_price: 88800, effective_from: "2026-09-01" },
      reason: "민감값 확인", submit: true,
    },
  });
  const id = (await req.json()).data.id;

  const readonly = await apiAs("readonly");
  const body = (await (await readonly.get(`/api/v1/approvals/${id}`)).json()).data;
  // **응답 본문에 금액이 없어야 한다.** CSS 로 가리는 건 방어가 아니다.
  expect(JSON.stringify(body)).not.toContain("88800");
  expect(body.sensitive_visible).toBe(false);
  await readonly.dispose();

  const admin = await apiAs("approver");
  const visible = (await (await admin.get(`/api/v1/approvals/${id}`)).json()).data;
  expect(visible.proposed_changes_json.regular_price).toBe(88800);
  await admin.dispose();
});

test("판매처 반영 — connector 미연결이면 큐에 넣지 않고 거절한다", async ({ page }) => {
  const op = await loginAs(page, "operator");
  const listing = await createListing(op.api, SEED.accounts.main, "COUPANG-PUB-1");
  const admin = await apiAs("approver");

  const req = await op.api.post("/api/v1/approvals", {
    data: {
      target_type: "product_channel_listing", target_id: listing.id,
      request_type: "CHANNEL_PRICE_CHANGE",
      proposed_changes: { regular_price: 19900, effective_from: "2026-01-01" },
      reason: "가격", submit: true,
    },
  });
  const id = (await req.json()).data.id;
  await admin.post(`/api/v1/approvals/${id}/approve`, { data: { comment: "ok" } });
  await admin.post(`/api/v1/approvals/${id}/apply`);

  const priceId = pricesOf(listing.id)[0].id;
  const publish = await op.api.post(`/api/v1/channel-listings/prices/${priceId}/publish`);
  expect(publish.status()).toBe(403);
  expect((await publish.json()).error.code).toBe("CONNECTOR_UNAVAILABLE");

  // 큐에 아무것도 안 들어갔다 — 넣으면 "처리 중" 처럼 보인다.
  const jobs = dbQuery(`SELECT id FROM channel_price_publish_outbox
                         WHERE channel_price_id = '${priceId}'`);
  expect(jobs).toHaveLength(0);

  // 배치를 돌려도 성공이 만들어지지 않는다.
  const run = await admin.post("/api/v1/channel-listings/publish/run-due");
  expect(run.ok()).toBeTruthy();
  expect((await run.json()).data.published).toBe(0);
  await admin.dispose();
});

test("connector 지원 표 — 지금은 전부 미연결", async ({ page }) => {
  const op = await loginAs(page, "operator");
  const res = await op.api.get("/api/v1/channel-listings/connector-support");
  expect(res.ok()).toBeTruthy();

  const rows = (await res.json()).data as Array<{
    marketplace: string; price_publish_supported: boolean; reason: string | null;
  }>;
  expect(rows.length).toBeGreaterThan(0);
  for (const row of rows) {
    // 하나라도 true 면 성공 mock 이 섞인 것이다.
    expect(row.price_publish_supported).toBe(false);
    expect(row.reason).toBeTruthy();
  }
});

test("가격 이력 — 과거·현재·미래가 모두 남는다", async ({ page }) => {
  const op = await loginAs(page, "operator");
  const listing = await createListing(op.api, SEED.accounts.main, "COUPANG-HIST-1");
  const admin = await apiAs("approver");

  for (const [price, from] of [[19900, "2026-01-01"], [21900, "2026-06-01"],
                               [24900, "2030-01-01"]] as const) {
    const req = await op.api.post("/api/v1/approvals", {
      data: {
        target_type: "product_channel_listing", target_id: listing.id,
        request_type: "CHANNEL_PRICE_CHANGE",
        proposed_changes: { regular_price: price, effective_from: from },
        reason: "가격", submit: true,
      },
    });
    const id = (await req.json()).data.id;
    await admin.post(`/api/v1/approvals/${id}/approve`, { data: { comment: "ok" } });
    await admin.post(`/api/v1/approvals/${id}/apply`);
  }
  await admin.dispose();

  const res = await op.api.get(`/api/v1/channel-listings/${listing.id}/prices`);
  expect(res.ok()).toBeTruthy();
  const body = await res.json();

  expect(body.data.total).toBe(3);
  // 최신 예정 가격이 위에 온다.
  expect(body.data.items[0].regular_price).toBe(24900);
  // 화면이 "현재/예정" 을 구분할 수 있게 서버가 기준일을 알려준다.
  expect(body.meta.today_kst).toMatch(/^\d{4}-\d{2}-\d{2}$/);

  // 과거 금액이 그대로 남아 있다.
  const amounts = body.data.items.map((i: { regular_price: number }) => i.regular_price);
  expect(amounts.sort((a: number, b: number) => a - b)).toEqual([19900, 21900, 24900]);
});
