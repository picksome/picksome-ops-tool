import { expect, test } from "@playwright/test";
import { SEED, dbExec, dbOne, dbQuery, reseed } from "./db";
import { apiAs, loginAs } from "./fixtures";

/**
 * 재고 → 소싱 → 재주문 → 발주 → 입고 E2E (실제 FastAPI + PostgreSQL).
 *
 * 여기서 확인하는 것
 * ---------------
 * 1. 재고 가져오기가 **원장**을 만든다 — 잔량 덮어쓰기가 아니다
 * 2. 같은 파일 재적용에 원장이 늘지 않는다
 * 3. 수동 조정이 화면에서 되고 잔량이 계산된다
 * 4. 소싱 연결 → 재주문이 **실제로 뚫린다**
 * 5. 재주문 기간은 화면에서 사람이 정한다 (기본값 없음)
 * 6. 발주 초안 → 입고 → 재고 증가
 * 7. **취득원가는 발주 단가다** — 그 뒤 소싱 원가가 바뀌어도 불변
 * 8. 일일 리포트와 예외 센터 숫자가 같다
 *
 * 4 와 7 이 핵심이다
 * ----------------
 * 지금까지 재주문이 `NO_SOURCE` 로 막혀 있었고, 그게 뚫리는지가 이번 세션의
 * 목적이다. 그리고 원가 세 종류가 섞이면 마진이 통째로 틀어진다.
 *
 * 외부 API 를 부르지 않는다
 * ----------------------
 * 여기 있는 것은 전부 우리 DB 안에서 끝난다. 실제 공급처에 주문하지 않는다.
 */

const P = SEED.products.memo;      // 재고를 붙일 상품
const SUPPLIER_CODE = "E2E-SUP-1";

test.beforeEach(() => {
  reseed();
});

type Api = Awaited<ReturnType<typeof apiAs>>;

async function json(res: Awaited<ReturnType<Api["get"]>>) {
  expect(res.ok(), await res.text()).toBeTruthy();
  return (await res.json()).data;
}

/** 이 파일 전용 공급처. 시드에 없으므로 직접 만든다. */
function makeSupplier(): string {
  const id = "e2e0e2e0-0000-0000-0000-0000000000d1";
  dbExec(`
    INSERT INTO supplier_site (id, org_id, supplier_code, name, country_code,
                               website_domain)
    VALUES ('${id}', '${SEED.orgId}', '${SUPPLIER_CODE}', 'E2E공급처', 'KR',
            'e2e-supplier.test')
    ON CONFLICT DO NOTHING
  `);
  return id;
}

function onHand(productId: string): number {
  const row = dbOne<{ n: string }>(`
    SELECT COALESCE(SUM(quantity_delta), 0) AS n
      FROM inventory_transaction
     WHERE org_id = '${SEED.orgId}' AND product_id = '${productId}'
  `);
  return Number(row?.n ?? 0);
}

/**
 * 시드 상품에는 `sku` 가 없고 `business_key` 만 있다. 둘 다 완전일치 열쇠이므로
 * 있는 쪽을 쓴다 — 테스트를 위해 시드에 sku 를 억지로 넣지 않는다.
 */
function key(productId: string): string {
  const row = dbOne<{ business_key: string }>(
    `SELECT business_key FROM product WHERE id = '${productId}'`);
  return row!.business_key;
}

/** 상품 이름. 화면에서 행을 찾을 때 쓴다. */
function productName(productId: string): string {
  const row = dbOne<{ name_ko: string }>(
    `SELECT name_ko FROM product WHERE id = '${productId}'`);
  return row!.name_ko;
}

// ──────────────────────────────────────────────────────────────
// A. 재고 가져오기
// ──────────────────────────────────────────────────────────────

test("재고 가져오기: 검토 → 적용 → 원장, 재적용해도 늘지 않는다", async () => {
  const api = await apiAs("operator");
  const code = key(P);
  const csv = `상품번호,수량\n${code},17\n`;

  // 검토는 아무것도 쓰지 않는다.
  const dry = await json(await api.post("/api/v1/inventory/import", {
    multipart: {
      file: { name: "재고.csv", mimeType: "text/csv", buffer: Buffer.from(csv) },
      mode: "DRY_RUN",
      as_of_date: "2026-08-11",
    },
  }));
  expect(dry.counts.OPENING).toBe(1);
  expect(onHand(P)).toBe(0);

  // 적용하면 원장이 생긴다.
  const applied = await json(await api.post("/api/v1/inventory/import", {
    multipart: {
      file: { name: "재고.csv", mimeType: "text/csv", buffer: Buffer.from(csv) },
      mode: "APPLY",
      as_of_date: "2026-08-11",
    },
  }));
  expect(applied.applied).toBe(1);
  expect(onHand(P)).toBe(17);

  // **잔량 컬럼이 아니라 원장이다.**
  const ledger = dbQuery<{ transaction_type: string; quantity_delta: number }>(`
    SELECT transaction_type, quantity_delta FROM inventory_transaction
     WHERE org_id = '${SEED.orgId}' AND product_id = '${P}'
  `);
  expect(ledger).toHaveLength(1);
  expect(ledger[0].transaction_type).toBe("OPENING");

  // 같은 파일 재적용 — 원장이 늘지 않는다.
  const again = await json(await api.post("/api/v1/inventory/import", {
    multipart: {
      file: { name: "재고.csv", mimeType: "text/csv", buffer: Buffer.from(csv) },
      mode: "APPLY",
      as_of_date: "2026-08-11",
    },
  }));
  expect(again.counts.DUPLICATE).toBe(1);
  expect(onHand(P)).toBe(17);
  expect(dbQuery(`
    SELECT 1 FROM inventory_transaction
     WHERE org_id = '${SEED.orgId}' AND product_id = '${P}'
  `)).toHaveLength(1);
});

test("상품명으로는 연결하지 않는다", async () => {
  const api = await apiAs("operator");
  const csv = "상품명,수량\n메모테스트상품,10\n";
  const res = await json(await api.post("/api/v1/inventory/import", {
    multipart: {
      file: { name: "이름.csv", mimeType: "text/csv", buffer: Buffer.from(csv) },
      mode: "DRY_RUN",
    },
  }));
  expect(res.file_problems.length).toBeGreaterThan(0);
  expect(onHand(P)).toBe(0);
});

// ──────────────────────────────────────────────────────────────
// B. 재고 화면 · 수동 조정
// ──────────────────────────────────────────────────────────────

test("재고 화면에서 조정하면 원장에 차이가 남고 잔량이 바뀐다", async ({ page }) => {
  // 기초 재고 17
  const api = await apiAs("operator");
  await json(await api.post("/api/v1/inventory/import", {
    multipart: {
      file: {
        name: "재고.csv", mimeType: "text/csv",
        buffer: Buffer.from(`상품번호,수량\n${key(P)},17\n`),
      },
      mode: "APPLY",
      as_of_date: "2026-08-11",
    },
  }));

  await loginAs(page, "operator");
  await page.goto("/inventory");

  // 행을 펼치고 조정한다.
  const row = page.getByRole("row").filter({ hasText: productName(P) }).first();
  await row.click();

  await page.getByRole("button", { name: "감소", exact: true }).click();
  await page.getByPlaceholder("수량").fill("2");
  await page.getByPlaceholder("사유 (필수)").fill("실사 차이");
  await page.getByRole("button", { name: "조정", exact: true }).click();

  await expect.poll(() => onHand(P), { timeout: 10_000 }).toBe(15);

  // **덮어쓰기가 아니라 차이**가 기록됐다.
  const deltas = dbQuery<{ quantity_delta: number }>(`
    SELECT quantity_delta FROM inventory_transaction
     WHERE org_id = '${SEED.orgId}' AND product_id = '${P}'
     ORDER BY occurred_at
  `).map((r) => Number(r.quantity_delta));
  expect(deltas).toEqual([17, -2]);
});

test("사유 없는 조정은 저장되지 않는다", async ({ page }) => {
  const api = await apiAs("operator");
  await json(await api.post("/api/v1/inventory/import", {
    multipart: {
      file: {
        name: "재고.csv", mimeType: "text/csv",
        buffer: Buffer.from(`상품번호,수량\n${key(P)},5\n`),
      },
      mode: "APPLY",
    },
  }));

  await loginAs(page, "operator");
  await page.goto("/inventory");
  await page.getByRole("row").filter({ hasText: productName(P) }).first().click();
  await page.getByPlaceholder("수량").fill("1");
  // 사유를 비워 둔다.
  await expect(
    page.getByRole("button", { name: "조정", exact: true }),
  ).toBeDisabled();
  expect(onHand(P)).toBe(5);
});

// ──────────────────────────────────────────────────────────────
// C. 소싱 연결 → 재주문이 뚫린다
// ──────────────────────────────────────────────────────────────

test("소싱 연결 전에는 NO_SOURCE, 연결하면 재주문이 계산된다", async () => {
  const api = await apiAs("sourcing");
  const code = key(P);

  // 재고 1개 + 최근 판매 이력을 깐다 — 곧 떨어질 상품.
  const opApi = await apiAs("operator");
  await json(await opApi.post("/api/v1/inventory/import", {
    multipart: {
      file: {
        name: "재고.csv", mimeType: "text/csv",
        buffer: Buffer.from(`상품번호,수량\n${code},1\n`),
      },
      mode: "APPLY",
    },
  }));
  for (let i = 0; i < 30; i += 1) {
    dbExec(`
      INSERT INTO sales_order (org_id, business_key, sale_date, marketplace,
                               marketplace_order_no, line_no, product_id,
                               quantity, sale_price)
      VALUES ('${SEED.orgId}', 'E2E-RO-${i}', CURRENT_DATE - ${i % 30},
              'COUPANG', 'E2E-RO-${i}', 1, '${P}', 1, 10000)
    `);
  }

  // 연결 전 — 계산할 수 없다.
  const before = await json(await api.get(
    "/api/v1/inventory/reorder?velocity_days=30&coverage_days=30"));
  const blockedBefore = before.blocked.find(
    (b: { product_id: string }) => b.product_id === P);
  expect(blockedBefore.block_reason).toBe("NO_SOURCE");

  // 소싱 연결 가져오기.
  makeSupplier();
  const csv =
    `상품번호,공급처코드,리드타임,MOQ,주소싱처,매입가\n${code},${SUPPLIER_CODE},7,10,Y,3000\n`;
  const applied = await json(await api.post("/api/v1/sourcing/import", {
    multipart: {
      file: { name: "소싱.csv", mimeType: "text/csv", buffer: Buffer.from(csv) },
      mode: "APPLY",
    },
  }));
  expect(applied.created).toBe(1);
  expect(applied.cost_versions).toBe(1);

  // 상품에 적용된 원가는 **바뀌지 않았다.**
  const product = dbOne<{ cost_amount: number }>(
    `SELECT cost_amount FROM product WHERE id = '${P}'`);
  expect(Number(product!.cost_amount)).not.toBe(3000);

  // 연결 후 — 재주문이 계산된다.
  const after = await json(await api.get(
    "/api/v1/inventory/reorder?velocity_days=30&coverage_days=30"));
  const candidate = after.candidates.find(
    (c: { product_id: string }) => c.product_id === P);
  expect(candidate).toBeTruthy();
  expect(candidate.lead_time_days).toBe(7);
  expect(candidate.moq).toBe(10);
  // MOQ 배수로 올라간다.
  expect(candidate.recommended_quantity % 10).toBe(0);
  // 매입가가 발주 단가 근거가 된다.
  expect(candidate.price_basis).toBe("SOURCE_COST_VERSION");
  expect(candidate.unit_price).toBe(3000);
});

test("재주문 화면은 기간을 정해야 계산한다", async ({ page }) => {
  await loginAs(page, "sourcing");
  await page.goto("/inventory");

  // 기본값이 없다 — 안내만 보인다.
  await expect(page.getByText("기본값을 정해 두지 않는 것은")).toBeVisible();
  await expect(page.getByRole("button", { name: "계산" })).toBeDisabled();

  await page.getByPlaceholder("예: 30").first().fill("30");
  await page.getByPlaceholder("예: 30").nth(1).fill("30");
  await expect(page.getByRole("button", { name: "계산" })).toBeEnabled();
});

// ──────────────────────────────────────────────────────────────
// D. 발주 → 입고 → 취득원가
// ──────────────────────────────────────────────────────────────

test("발주 초안 → 확정 → 입고 → 재고 증가, 취득원가는 발주 단가", async () => {
  const api = await apiAs("operator");
  const supplierId = makeSupplier();

  const draft = await json(await api.post("/api/v1/purchase-orders", {
    data: {
      supplier_site_id: supplierId,
      lines: [{ product_id: P, quantity: 20, unit_price: 3000 }],
    },
  }));
  expect(draft.status).toBe("DRAFT");
  // 초안은 재고를 건드리지 않는다.
  expect(onHand(P)).toBe(0);

  await json(await api.post(`/api/v1/purchase-orders/${draft.id}/status`,
    { data: { status: "ORDERED" } }));

  // 입고 전에 소싱 원가가 올랐다고 하자.
  dbExec(`
    UPDATE product_source_cost_version SET purchase_price = 9999
     WHERE org_id = '${SEED.orgId}'
  `);

  const received = await json(await api.post(
    `/api/v1/purchase-orders/${draft.id}/receive`, {
      data: {
        receipts: [{ line_id: draft.lines[0].id, quantity: 8 }],
        receipt_key: "e2e-recv-1",
      },
    }));
  expect(received.status).toBe("PARTIALLY_RECEIVED");
  expect(onHand(P)).toBe(8);

  // **취득원가는 발주 단가다.** 그 사이 오른 소싱 원가가 아니다.
  const cost = dbOne<{ unit_cost: number }>(`
    SELECT unit_cost FROM inventory_transaction
     WHERE org_id = '${SEED.orgId}' AND product_id = '${P}'
       AND transaction_type = 'PURCHASE_RECEIPT'
  `);
  expect(Number(cost!.unit_cost)).toBe(3000);

  // 같은 입고 키 재전송 — 재고가 두 번 늘지 않는다.
  const dup = await api.post(`/api/v1/purchase-orders/${draft.id}/receive`, {
    data: {
      receipts: [{ line_id: draft.lines[0].id, quantity: 8 }],
      receipt_key: "e2e-recv-1",
    },
  });
  expect(dup.ok()).toBeFalsy();
  expect(onHand(P)).toBe(8);
});

test("발주 화면에서 초안을 만들 수 있다", async ({ page }) => {
  makeSupplier();
  await loginAs(page, "operator");
  await page.goto("/purchase-orders");

  await page.getByRole("button", { name: "발주 초안 만들기" }).click();
  await page.getByRole("combobox").selectOption({ label: "E2E공급처" });
  await page.getByPlaceholder("상품명 또는 SKU 로 검색").fill(productName(P));
  await page.getByRole("button").filter({ hasText: productName(P) }).first().click();

  await page.getByRole("button", { name: "초안 만들기" }).click();

  await expect.poll(() => dbQuery(`
    SELECT 1 FROM purchase_order
     WHERE org_id = '${SEED.orgId}' AND status = 'DRAFT'
  `).length, { timeout: 10_000 }).toBe(1);
});

// ──────────────────────────────────────────────────────────────
// E. 예외 센터 · 일일 리포트
// ──────────────────────────────────────────────────────────────

test("예외 센터와 일일 리포트의 숫자가 같다", async () => {
  const api = await apiAs("approver");

  // 음수 재고를 만든다 — 확실한 예외.
  await json(await api.post("/api/v1/inventory/adjust", {
    data: {
      product_id: P, quantity_delta: -3, reason: "E2E 실사 차이",
    },
  }));

  const center = await json(await api.get("/api/v1/operations/exceptions"));
  const report = await json(await api.get("/api/v1/automation/daily-report"));

  expect(report.exception_summary.total).toBe(center.total);
  expect(center.items.some(
    (i: { kind: string }) => i.kind === "NEGATIVE_INVENTORY")).toBeTruthy();
});

test("미연결 카테고리를 0건 정상으로 표시하지 않는다", async ({ page }) => {
  await loginAs(page, "approver");
  await page.goto("/operations/exceptions");

  // 클레임은 커넥터가 없어 셀 수 없다.
  await expect(page.getByText("미연결").first()).toBeVisible();
  await expect(page.getByText("아직 확인할 수 없는 것")).toBeVisible();
});

test("일일 리포트에서 예외 센터로 드릴다운된다", async ({ page }) => {
  const api = await apiAs("approver");
  await json(await api.post("/api/v1/inventory/adjust", {
    data: { product_id: P, quantity_delta: -2, reason: "E2E 실사" },
  }));

  await loginAs(page, "approver");
  await page.goto("/operations/daily");

  await page.getByRole("link", { name: /재고·발주/ }).click();
  await expect(page).toHaveURL(/category=INVENTORY/);
  await expect(page.getByText("NEGATIVE_INVENTORY").first()).toBeVisible();
});

// ──────────────────────────────────────────────────────────────
// F. 재주문 → 발주 초안 (화면)
// ──────────────────────────────────────────────────────────────

test("재주문 추천에서 발주 초안을 만들고, 두 번째는 기존 초안으로 보낸다",
  async ({ page }) => {
    // 곧 떨어질 상품 + 소싱 연결을 만든다.
    const op = await apiAs("operator");
    await json(await op.post("/api/v1/inventory/import", {
      multipart: {
        file: {
          name: "재고.csv", mimeType: "text/csv",
          buffer: Buffer.from(`상품번호,수량
${key(P)},1
`),
        },
        mode: "APPLY",
      },
    }));
    for (let i = 0; i < 30; i += 1) {
      dbExec(`
        INSERT INTO sales_order (org_id, business_key, sale_date, marketplace,
                                 marketplace_order_no, line_no, product_id,
                                 quantity, sale_price)
        VALUES ('${SEED.orgId}', 'E2E-DR-${i}', CURRENT_DATE - ${i % 30},
                'COUPANG', 'E2E-DR-${i}', 1, '${P}', 1, 10000)
      `);
    }
    makeSupplier();
    const sourcing = await apiAs("sourcing");
    await json(await sourcing.post("/api/v1/sourcing/import", {
      multipart: {
        file: {
          name: "소싱.csv", mimeType: "text/csv",
          buffer: Buffer.from(
            `상품번호,공급처코드,리드타임,MOQ,주소싱처,매입가
` +
            `${key(P)},${SUPPLIER_CODE},7,10,Y,3000
`),
        },
        mode: "APPLY",
      },
    }));

    await loginAs(page, "operator");
    await page.goto("/inventory");
    await page.getByPlaceholder("예: 30").first().fill("30");
    await page.getByPlaceholder("예: 30").nth(1).fill("30");
    await page.getByRole("button", { name: "계산" }).click();

    // 추천이 떴고, 초안 버튼이 있다.
    await expect(page.getByText("E2E공급처")).toBeVisible();
    await page.getByRole("button", { name: "발주 초안 만들기" }).click();

    // 추천값이 채워져 있고 "추천" 임을 밝힌다.
    await expect(page.getByText("재주문 분석에서 넘어온")).toBeVisible();
    await page.getByRole("button", { name: "초안 만들기" }).click();

    await expect.poll(() => dbQuery(`
      SELECT 1 FROM purchase_order
       WHERE org_id = '${SEED.orgId}' AND status = 'DRAFT'
    `).length, { timeout: 10_000 }).toBe(1);

    // 두 번째 — 새로 만들지 않고 기존 초안으로 보낸다.
    await page.reload();
    await page.getByPlaceholder("예: 30").first().fill("30");
    await page.getByPlaceholder("예: 30").nth(1).fill("30");
    await page.getByRole("button", { name: "계산" }).click();
    await expect(page.getByText(/기존 초안 PO-.* 보기/)).toBeVisible();
    expect(dbQuery(`
      SELECT 1 FROM purchase_order
       WHERE org_id = '${SEED.orgId}' AND status = 'DRAFT'
    `)).toHaveLength(1);
  });
