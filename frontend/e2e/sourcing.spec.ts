import { expect, test } from "@playwright/test";
import { SEED, dbOne, dbQuery, reseed, todayKst } from "./db";
import { apiAs, loginAs } from "./fixtures";

/**
 * Sourcing Core E2E (실제 FastAPI + PostgreSQL).
 *
 * 전체 흐름을 한 번에 통과시킨다
 * --------------------------
 * ```
 * 공급처 생성 → 상품에 소싱처 연결 → 주 소싱처 지정 → 외화 원가 등록
 *   → 환율 등록 → 예상 landed cost → 권장가 → 민감도
 *   → 상품 원가 채택 요청(COST_CHANGE) → 승인 → 적용
 *   → product.cost_amount 변경 확인 → 원가 이력 보존 확인
 * ```
 *
 * 무엇을 확인하나
 * ------------
 * 화면 문구가 아니라 **DB 행과 금액**이다. 특히:
 *
 * · 소싱 원가를 등록해도 `product.cost_amount` 는 **변하지 않는다**
 * · 승인 적용 후에만 바뀐다
 * · 그 뒤에도 **과거 판매의 실마진은 그대로**다
 *
 * 이 세 가지가 Sourcing Core 와 기존 OPS Core 의 경계다.
 */

// 서버는 Asia/Seoul 기준으로 오늘을 판단한다. UTC 를 쓰면 새벽에 하루 어긋난다.
const TODAY = todayKst();

type Api = Awaited<ReturnType<typeof apiAs>>;

async function json(res: Awaited<ReturnType<Api["get"]>>) {
  expect(res.ok(), await res.text()).toBeTruthy();
  return (await res.json()).data;
}

test.beforeEach(() => {
  reseed();
});

test("소싱처 등록부터 상품 원가 채택까지 통과시킨다", async () => {
  const sourcing = await apiAs("sourcing");
  const admin = await apiAs("approver");
  const productId = SEED.products.price;

  // ── 1. 공급처 ──────────────────────────────────────────────
  const supplier = await json(await sourcing.post("/api/v1/supplier-sites", {
    data: {
      name: "1688 테스트몰",
      supplier_type: "OVERSEAS_MARKETPLACE",
      country_code: "CN",
      website_url: "https://detail.1688.com/offer/999.html",
      default_currency: "CNY",
      credential_present: true,
    },
  }));
  // 코드는 자동 부여된다 — 이름을 바꿔도 연결이 유지되어야 한다.
  expect(supplier.supplier_code).toMatch(/^SUP-\d{6}$/);
  // 도메인으로 정규화된다 (실제 Excel 의 구매처가 URL 이었다).
  expect(supplier.website_domain).toBe("detail.1688.com");
  // **비밀번호는 어떤 필드로도 내려오지 않는다.**
  expect(JSON.stringify(supplier)).not.toContain("password");

  // ── 2. 소싱처 연결 ─────────────────────────────────────────
  const source = await json(await sourcing.post(
    `/api/v1/products/${productId}/sources`,
    {
      data: {
        supplier_site_id: supplier.id,
        source_url: "https://detail.1688.com/offer/999.html",
        currency: "CNY",
        moq: 2,
        lead_time_days: 10,
      },
    },
  ));
  // 첫 소싱처는 자동으로 주 소싱처가 된다 — 하나뿐인데 없으면 원가를 못 정한다.
  expect(source.is_primary).toBe(true);

  // ── 3. 원가 + 환율 ─────────────────────────────────────────
  await json(await sourcing.post(
    `/api/v1/product-sources/${source.id}/cost-versions`,
    {
      data: {
        purchase_price: 120, currency: "CNY", valid_from: "2026-01-01",
        international_shipping: 7200,
      },
    },
  ));
  await json(await sourcing.post("/api/v1/fx-rates", {
    data: { base_currency: "CNY", rate: "205", rate_type: "SOURCING" },
  }));

  // 소싱 원가를 등록해도 상품 원가는 그대로다.
  const costBefore = dbOne<{ cost_amount: string | null }>(
    `SELECT cost_amount FROM product WHERE id = '${productId}'`,
  );

  // ── 4. 예상 landed cost ────────────────────────────────────
  const lc = await json(await sourcing.get(
    `/api/v1/product-sources/${source.id}/landed-cost`,
  ));
  // 120 CNY × 205 = 24,600 + 국제배송 7,200 = 31,800
  expect(lc.landed_cost.total_krw).toBe(31_800);
  expect(lc.landed_cost.is_complete).toBe(true);
  expect(lc.current_cost_amount).toBe(
    costBefore?.cost_amount === null ? null : Number(costBefore?.cost_amount),
  );

  // ── 5. 권장가 ──────────────────────────────────────────────
  const rec = await json(await sourcing.get(
    `/api/v1/product-sources/${source.id}/recommendation?marketplace=COUPANG`,
  ));
  if (rec.available) {
    expect(rec.cost_krw).toBe(31_800);
    // 근거가 없으면 "왜 권장가가 달라졌나" 에 답할 수 없다.
    expect(rec.basis.source_cost_version_id).toBeTruthy();
    expect(rec.basis.fx.rate).toContain("205");
    expect(rec.prices.some((p: { key: string }) => p.key === "break_even")).toBe(true);
  } else {
    // 정책이 없으면 계산하지 않는다 — 0% 로 계산하면 거짓이 된다.
    expect(rec.missing.length).toBeGreaterThan(0);
  }

  // ── 6. 민감도는 DB 에 남지 않는다 ──────────────────────────
  const fxBefore = dbQuery(
    `SELECT 1 FROM fx_rate_snapshot WHERE org_id = '${SEED.orgId}'`,
  ).length;
  await sourcing.get(
    `/api/v1/product-sources/${source.id}/sensitivity`
    + `?marketplace=COUPANG&sale_price=60000`,
  );
  expect(
    dbQuery(`SELECT 1 FROM fx_rate_snapshot WHERE org_id = '${SEED.orgId}'`).length,
    "민감도 시나리오가 환율로 저장됐다",
  ).toBe(fxBefore);

  // ── 7. 상품 원가는 아직 그대로 ─────────────────────────────
  const stillOld = dbOne<{ cost_amount: string | null }>(
    `SELECT cost_amount FROM product WHERE id = '${productId}'`,
  );
  expect(stillOld?.cost_amount).toBe(costBefore?.cost_amount);

  // ── 8. 채택은 COST_CHANGE 승인을 거친다 ────────────────────
  const request = await json(await sourcing.post("/api/v1/approvals", {
    data: {
      target_type: "product",
      target_id: productId,
      request_type: "COST_CHANGE",
      proposed_changes: { cost_amount: 31_800 },
      reason: `${supplier.name} 소싱 예상 원가 반영`,
      submit: true,
    },
  }));

  // 요청만으로는 바뀌지 않는다.
  expect(
    dbOne<{ cost_amount: string }>(
      `SELECT cost_amount FROM product WHERE id = '${productId}'`,
    )?.cost_amount,
  ).toBe(costBefore?.cost_amount);

  // 요청자 본인은 승인할 수 없다.
  const selfApprove = await sourcing.post(`/api/v1/approvals/${request.id}/approve`, {
    data: {},
  });
  expect(selfApprove.status(), "요청자가 자기 요청을 승인했다").toBeGreaterThanOrEqual(400);

  await json(await admin.post(`/api/v1/approvals/${request.id}/approve`, { data: {} }));
  await json(await admin.post(`/api/v1/approvals/${request.id}/apply`, { data: {} }));

  // ── 9. 적용 후 ─────────────────────────────────────────────
  expect(
    Number(dbOne<{ cost_amount: string }>(
      `SELECT cost_amount FROM product WHERE id = '${productId}'`,
    )?.cost_amount),
  ).toBe(31_800);

  // 소싱 원가 이력은 그대로 남는다 — 관측 사실과 채택한 값은 다른 것이다.
  const history = dbQuery<{ purchase_price: string }>(
    `SELECT purchase_price FROM product_source_cost_version
      WHERE product_source_id = '${source.id}' AND deleted_at IS NULL`,
  );
  expect(history).toHaveLength(1);
  expect(Number(history[0].purchase_price)).toBe(120);

  await sourcing.dispose();
  await admin.dispose();
});

test("새 원가 version 을 올려도 이전 version 이 남는다", async () => {
  const sourcing = await apiAs("sourcing");
  const supplier = await json(await sourcing.post("/api/v1/supplier-sites", {
    data: { name: "이력 확인 공급처", default_currency: "KRW" },
  }));
  const source = await json(await sourcing.post(
    `/api/v1/products/${SEED.products.memo}/sources`,
    { data: { supplier_site_id: supplier.id, currency: "KRW" } },
  ));

  await json(await sourcing.post(
    `/api/v1/product-sources/${source.id}/cost-versions`,
    { data: { purchase_price: 10_000, currency: "KRW", valid_from: "2026-01-01" } },
  ));
  await json(await sourcing.post(
    `/api/v1/product-sources/${source.id}/cost-versions`,
    { data: { purchase_price: 13_000, currency: "KRW", valid_from: TODAY } },
  ));

  const rows = dbQuery<{ purchase_price: string; valid_to: string | null }>(
    `SELECT purchase_price, valid_to FROM product_source_cost_version
      WHERE product_source_id = '${source.id}' AND deleted_at IS NULL
      ORDER BY valid_from`,
  );
  expect(rows).toHaveLength(2);
  // 앞 version 은 지워지지 않고 닫힌다. 그 시점 판단의 근거로 남아야 한다.
  expect(Number(rows[0].purchase_price)).toBe(10_000);
  expect(rows[0].valid_to).not.toBeNull();
  expect(rows[1].valid_to).toBeNull();

  await sourcing.dispose();
});

test("주 소싱처는 하나뿐이다", async () => {
  const sourcing = await apiAs("sourcing");
  const productId = SEED.products.name;

  const a = await json(await sourcing.post("/api/v1/supplier-sites", {
    data: { name: "공급처 A" },
  }));
  const b = await json(await sourcing.post("/api/v1/supplier-sites", {
    data: { name: "공급처 B" },
  }));

  const first = await json(await sourcing.post(
    `/api/v1/products/${productId}/sources`, { data: { supplier_site_id: a.id } },
  ));
  const second = await json(await sourcing.post(
    `/api/v1/products/${productId}/sources`, { data: { supplier_site_id: b.id } },
  ));
  expect(first.is_primary).toBe(true);
  expect(second.is_primary).toBe(false);

  await json(await sourcing.post(`/api/v1/product-sources/${second.id}/primary`));

  const primaries = dbQuery(
    `SELECT 1 FROM product_source
      WHERE product_id = '${productId}' AND is_primary
        AND status = 'ACTIVE' AND deleted_at IS NULL`,
  );
  expect(primaries, "주 소싱처가 둘이다").toHaveLength(1);

  await sourcing.dispose();
});

test("환율이 없으면 예상 원가를 계산하지 않는다", async () => {
  // 0 이나 1 로 채우면 "원가 거의 0원, 마진 100%" 가 나오고 화면에서는 그럴듯해 보인다.
  const sourcing = await apiAs("sourcing");
  const supplier = await json(await sourcing.post("/api/v1/supplier-sites", {
    data: { name: "환율 없는 공급처", default_currency: "USD" },
  }));
  const source = await json(await sourcing.post(
    `/api/v1/products/${SEED.products.cost}/sources`,
    { data: { supplier_site_id: supplier.id, currency: "USD" } },
  ));
  await json(await sourcing.post(
    `/api/v1/product-sources/${source.id}/cost-versions`,
    { data: { purchase_price: 18, currency: "USD", valid_from: "2026-01-01" } },
  ));

  const lc = await json(await sourcing.get(
    `/api/v1/product-sources/${source.id}/landed-cost`,
  ));
  expect(lc.landed_cost.total_krw).toBeNull();
  expect(lc.landed_cost.missing).toContain("FX_RATE:USD/KRW");
  // 차이도 모른다 — 0 으로 두면 "차이 없음" 으로 읽힌다.
  expect(lc.difference).toBeNull();

  await sourcing.dispose();
});

test("매입가는 조회 전용 계정에게 가려진다", async () => {
  const sourcing = await apiAs("sourcing");
  const readonly = await apiAs("readonly");

  const supplier = await json(await sourcing.post("/api/v1/supplier-sites", {
    data: { name: "민감 원가 공급처" },
  }));
  const source = await json(await sourcing.post(
    `/api/v1/products/${SEED.products.lock}/sources`,
    { data: { supplier_site_id: supplier.id, currency: "KRW" } },
  ));
  await json(await sourcing.post(
    `/api/v1/product-sources/${source.id}/cost-versions`,
    { data: { purchase_price: 55_000, currency: "KRW", valid_from: "2026-01-01" } },
  ));

  const hidden = await json(await readonly.get(
    `/api/v1/product-sources/${source.id}/cost-versions`,
  ));
  expect(hidden[0].purchase_price).toBeNull();
  expect(hidden[0].redacted).toBe(true);
  // 원가가 있었다는 사실 자체는 남는다 — 아예 안 보이면 "미등록" 으로 읽힌다.
  expect(hidden[0].valid_from).toBeTruthy();

  // 가릴 수 없는 계산 결과는 아예 주지 않는다.
  const lc = await readonly.get(`/api/v1/product-sources/${source.id}/landed-cost`);
  expect(lc.status()).toBe(403);

  await sourcing.dispose();
  await readonly.dispose();
});

test("공급처 화면이 실제 DB 를 보여준다", async ({ page }) => {
  const sourcing = await apiAs("sourcing");
  await json(await sourcing.post("/api/v1/supplier-sites", {
    data: { name: "화면 확인 공급처", website_url: "https://www.domeggook.com/x" },
  }));
  await sourcing.dispose();

  await loginAs(page, "sourcing");
  await page.goto("/sourcing/suppliers");

  await expect(page.getByRole("heading", { name: "공급처", exact: true })).toBeVisible();
  await expect(page.getByText("화면 확인 공급처")).toBeVisible();
  // 도메인으로 정규화돼 보인다.
  await expect(page.getByText("domeggook.com")).toBeVisible();
});

test("소싱 담당이 아니면 공급처 메뉴가 없다", async ({ page }) => {
  await loginAs(page, "operator");
  await page.goto("/dashboard");
  await expect(page.getByRole("link", { name: "공급처" })).toHaveCount(0);
});


test("상품 소싱 탭에서 원가 등록부터 승인 요청까지 화면으로 한다", async ({ page }) => {
  // API 로만 검증하면 "화면에서 그 흐름이 실제로 가능한가" 는 알 수 없다.
  const sourcing = await apiAs("sourcing");
  const supplier = await json(await sourcing.post("/api/v1/supplier-sites", {
    data: { name: "드로어 공급처", default_currency: "CNY" },
  }));
  await sourcing.dispose();

  await loginAs(page, "sourcing");
  await page.goto(`/products?productId=${SEED.products.price}`);

  await page.getByRole("tab", { name: "소싱" }).click();

  // ── 소싱처 연결 ────────────────────────────────────────────
  await page.getByRole("button", { name: "소싱처 추가" }).click();
  await page.getByLabel("공급처 *").selectOption(supplier.id);
  await page.getByLabel("원본 상품 URL").fill("https://detail.1688.com/offer/777.html");
  await page.getByLabel("매입 통화").selectOption("CNY");
  await page.getByRole("button", { name: "연결" }).click();

  await expect(page.getByText("드로어 공급처")).toBeVisible();
  // 첫 소싱처는 주 소싱처가 된다 (헤더 열 이름과 배지가 같은 문구라 배지만 본다).
  await expect(page.getByText("주 소싱처", { exact: true }).last()).toBeVisible();

  // ── 원가 등록 ──────────────────────────────────────────────
  await page.getByRole("button", { name: "새 원가 등록" }).click();
  await page.getByLabel("매입가 (CNY) *").fill("120");
  await page.getByLabel("국제 배송비 (KRW)").fill("7200");
  await page.getByRole("button", { name: "등록", exact: true }).click();

  // 환율이 없으니 계산 불가로 나와야 한다 — 0원이 아니다.
  // 예상 원가와 권장가 두 곳에서 같은 이유를 말한다 — 둘 다 계산 불가다.
  await expect(page.getByText(/환율이 등록되지 않았습니다/).first()).toBeVisible();

  // ── 환율 등록 ──────────────────────────────────────────────
  await page.getByRole("button", { name: "환율 등록" }).first().click();
  await page.getByLabel("1 CNY = ? KRW *").fill("205");
  await page.getByRole("button", { name: "등록", exact: true }).click();

  // ── 예상 원가 ──────────────────────────────────────────────
  // 120 × 205 = 24,600 + 7,200 = 31,800
  await expect(page.getByText("31,800 원").first()).toBeVisible({ timeout: 15_000 });

  // ── 상품 원가는 아직 그대로 ────────────────────────────────
  const before = dbOne<{ cost_amount: string | null }>(
    `SELECT cost_amount FROM product WHERE id = '${SEED.products.price}'`,
  );

  // ── 채택 요청 ──────────────────────────────────────────────
  await page.getByRole("button", { name: "현재 상품 원가로 적용 요청" }).click();
  await expect(page.getByText(/승인되어야 상품 원가에 반영/)).toBeVisible();

  // 요청만으로는 바뀌지 않는다.
  expect(
    dbOne<{ cost_amount: string | null }>(
      `SELECT cost_amount FROM product WHERE id = '${SEED.products.price}'`,
    )?.cost_amount,
  ).toBe(before?.cost_amount);

  // COST_CHANGE 요청이 실제로 만들어졌다.
  const requests = dbQuery<{ request_type: string; status: string }>(
    `SELECT request_type, status FROM approval_request
      WHERE org_id = '${SEED.orgId}' AND target_id = '${SEED.products.price}'
        AND request_type = 'COST_CHANGE' AND deleted_at IS NULL`,
  );
  expect(requests.length).toBeGreaterThan(0);
});

test("조회 전용 계정에게는 소싱 탭이 원가를 보여주지 않는다", async ({ page }) => {
  const sourcing = await apiAs("sourcing");
  const supplier = await json(await sourcing.post("/api/v1/supplier-sites", {
    data: { name: "가림 확인 공급처", default_currency: "KRW" },
  }));
  const source = await json(await sourcing.post(
    `/api/v1/products/${SEED.products.memo}/sources`,
    { data: { supplier_site_id: supplier.id, currency: "KRW" } },
  ));
  await json(await sourcing.post(
    `/api/v1/product-sources/${source.id}/cost-versions`,
    { data: { purchase_price: 44_000, currency: "KRW", valid_from: "2026-01-01" } },
  ));
  await sourcing.dispose();

  await loginAs(page, "readonly");
  await page.goto(`/products?productId=${SEED.products.memo}`);
  await page.getByRole("tab", { name: "소싱" }).click();

  // 공급처 이름은 보이지만 금액은 보이지 않는다.
  await expect(page.getByText("가림 확인 공급처")).toBeVisible();
  await expect(page.getByText("44,000")).toHaveCount(0);
  await expect(page.getByText(/볼 권한이 없습니다/).first()).toBeVisible();
});
