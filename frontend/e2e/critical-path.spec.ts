import { expect, test } from "@playwright/test";
import { SEED, dbOne, dbQuery, reseed, todayKst } from "./db";
import { apiAs } from "./fixtures";

/**
 * 임계 경로 E2E (Phase 18) — 실제 FastAPI + PostgreSQL.
 *
 * 왜 이 파일이 따로 있나
 * -------------------
 * 다른 spec 들은 각자 한 화면을 검증한다. 그런데 이 제품의 위험은 **화면 하나가
 * 아니라 이어 붙인 흐름**에 있다: 판매가 손익이 되고, 손익이 확정 snapshot 이
 * 되고, snapshot 이 배분이 되고, 배분이 지급 의무가 되고, 지급이 현금이 된다.
 *
 * 중간에 한 칸이라도 어긋나면 마지막에 나오는 "이번 달 얼마 남았나" 가 거짓이
 * 되는데, 단위 테스트는 각자 통과하므로 아무도 모른다.
 *
 * 여기서 한 달을 처음부터 끝까지 통과시킨다.
 *
 * ```
 * 판매·비용 → 검증 → 확정(snapshot) → 배분 → 지급 배치 → 지급 기록
 *          → 현금흐름 → 잠금 → 재오픈 요청 → 재확정
 * ```
 *
 * 무엇을 확인하나
 * ------------
 * 화면 문구가 아니라 **DB 행과 금액**이다. 특히 각 단계의 숫자가 앞 단계에서
 * 나온 값과 같은지를 본다 — 어딘가에서 다시 계산하면 두 숫자가 갈라진다.
 */

const YM = SEED.months.open;
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

test("한 달을 판매부터 현금까지 통과시킨다", async () => {
  const admin = await apiAs("approver");

  // ── 1. 판매 ────────────────────────────────────────────────
  // 판매가 있어야 손익이 생긴다. 계정 귀속(M032)까지 함께 넣는다 —
  // 계정을 모르면 정산 대사에서 본점과 2호점이 섞인다.
  //
  // **상품을 반드시 연결한다.** 상품이 없으면 원가를 알 수 없어 마진이 매출
  // 전액으로 잡히고, 마감 검증이 `SALE_WITHOUT_PRODUCT` 로 확정을 막는다.
  // (그 차단이 실제로 동작한다는 것은 이 테스트를 쓰다가 확인했다.)
  const sale = await json(await admin.post("/api/v1/sales", {
    data: {
      sale_date: `${YM}-05`,
      marketplace: "COUPANG",
      marketplace_order_no: "CP-CRITICAL-1",
      product_id: SEED.products.price,
      quantity: 1,
      sale_price: 1_000_000,
      integration_account_id: SEED.sales.coupang.integrationAccountId,
    },
  }));
  expect(sale.integration_account_id).toBe(SEED.sales.coupang.integrationAccountId);

  // ── 2. 검증 ────────────────────────────────────────────────
  // 차단 항목이 있으면 확정할 수 없다. 경고는 확인하고 넘어갈 수 있다.
  const checks = await json(await admin.post(`/api/v1/monthly-close/${YM}/validate`));
  const blocking = (checks as { code: string; grade: string }[])
    .filter((c) => c.grade === "BLOCKING");
  expect(blocking, `차단 항목이 남아 있다: ${blocking.map((b) => b.code).join(", ")}`)
    .toHaveLength(0);

  // ── 3. 확정 → snapshot 박제 ────────────────────────────────
  const acknowledged = (checks as { code: string; grade: string }[])
    .filter((c) => c.grade !== "BLOCKING")
    .map((c) => c.code);
  await json(await admin.post(`/api/v1/monthly-close/${YM}/confirm`, {
    data: { acknowledged_warnings: acknowledged },
  }));

  const snap = dbOne<{ net_profit: string; generation: number }>(
    `SELECT net_profit, generation FROM monthly_close_snapshot
      WHERE org_id = '${SEED.orgId}' AND year_month = '${YM}'
        AND superseded_at IS NULL`,
  );
  expect(snap, "확정했는데 snapshot 이 없다").not.toBeNull();
  const confirmedProfit = Number(snap!.net_profit);

  // ── 4. 배분 — snapshot 순이익을 나눈다 ─────────────────────
  const run = await json(await admin.post(`/api/v1/distributions/${YM}`, { data: {} }));
  // 배분 기준이 snapshot 과 다르면 "어느 순이익을 나눈 것인가" 를 답할 수 없다.
  expect(run.distributable_base).toBe(confirmedProfit);
  expect(run.total_distributed).toBe(confirmedProfit);

  await json(await admin.post(`/api/v1/distributions/${YM}/approve`, {
    data: {},
    params: { run_id: run.id },
  }));

  // ── 5. 지급 배치 — 배분 결과가 지급 의무가 된다 ────────────
  const batch = await json(await admin.post(`/api/v1/payouts/from-distribution/${YM}`, {
    data: { due_date: TODAY },
  }));
  const dueTotal = batch.items.reduce(
    (s: number, i: { due_amount: number }) => s + i.due_amount, 0,
  );
  expect(dueTotal, "지급 예정 총액이 배분액과 다르다").toBe(confirmedProfit);

  await json(await admin.post(`/api/v1/payouts/${batch.id}/approve`));

  // 승인만으로는 아직 현금이 나가지 않았다 — 예정 출금이다.
  let cash = await json(await admin.get("/api/v1/cashflow?preset=30d"));
  expect(cash.outflow.actual).toBe(0);
  expect(cash.outflow.expected).toBe(confirmedProfit);

  // ── 6. 지급 기록 — 여기서 처음 실제 현금이 움직인다 ────────
  const first = batch.items[0];
  await json(await admin.post(`/api/v1/payouts/items/${first.id}/payments`, {
    data: { payment_date: TODAY, amount: first.due_amount, payment_reference: "E2E-CP" },
  }));

  // 지급 기록 없이 PAID 가 될 수 없다 — 트리거가 paid_amount 를 계산한다.
  const item = dbOne<{ paid_amount: string; status: string }>(
    `SELECT paid_amount, status FROM payout_item WHERE id = '${first.id}'`,
  );
  expect(Number(item!.paid_amount)).toBe(first.due_amount);
  expect(item!.status).toBe("PAID");

  cash = await json(await admin.get("/api/v1/cashflow?preset=30d"));
  expect(cash.outflow.actual).toBe(first.due_amount);
  // 지급한 만큼 예정 출금에서 빠진다. 빼지 않으면 같은 돈이 두 번 잡힌다.
  expect(cash.outflow.expected).toBe(confirmedProfit - first.due_amount);

  // 현금흐름의 모든 줄은 원본으로 되돌아갈 수 있어야 한다.
  const paymentRow = (cash.rows as { source_type: string; drill_href: string }[])
    .find((r) => r.source_type === "payout_payment");
  expect(paymentRow?.drill_href).toBeTruthy();

  // ── 7. 잠금 ────────────────────────────────────────────────
  await json(await admin.post(`/api/v1/monthly-close/${YM}/lock`));

  // 잠근 달에는 판매를 넣을 수 없다. 화면에서 버튼을 숨기는 것으로는 못 막는다.
  const blocked = await admin.post("/api/v1/sales", {
    data: {
      sale_date: `${YM}-06`, marketplace: "COUPANG",
      marketplace_order_no: "CP-AFTER-LOCK", quantity: 1, sale_price: 500_000,
    },
  });
  expect(blocked.status(), "잠긴 달에 판매가 들어갔다").toBeGreaterThanOrEqual(400);

  // ── 8. 재오픈은 요청일 뿐 잠금을 풀지 않는다 ───────────────
  await json(await admin.post(`/api/v1/monthly-close/${YM}/reopen-request`, {
    data: { reason: "정산서 지연 도착", submit: true },
  }));
  const stillLocked = dbOne<{ close_state: string }>(
    `SELECT close_state FROM monthly_settlement
      WHERE org_id = '${SEED.orgId}' AND year_month = '${YM}'`,
  );
  expect(stillLocked!.close_state, "요청만으로 잠금이 풀렸다").toBe("LOCKED");

  // ── 9. 확정 이후 숫자가 흔들리지 않는다 ────────────────────
  const after = dbQuery<{ net_profit: string }>(
    `SELECT net_profit FROM monthly_close_snapshot
      WHERE org_id = '${SEED.orgId}' AND year_month = '${YM}' AND superseded_at IS NULL`,
  );
  expect(Number(after[0].net_profit)).toBe(confirmedProfit);

  await admin.dispose();
});

test("확정 snapshot 은 UPDATE 로 바꿀 수 없다", async () => {
  // 이 흐름 전체의 신뢰는 "확정한 숫자는 변하지 않는다" 에 걸려 있다.
  // 애플리케이션이 안 고치는 것과 고칠 수 없는 것은 다르다 — DB 가 막아야 한다.
  const admin = await apiAs("approver");
  const checks = await json(await admin.post(`/api/v1/monthly-close/${YM}/validate`));
  await json(await admin.post(`/api/v1/monthly-close/${YM}/confirm`, {
    data: {
      acknowledged_warnings: (checks as { code: string; grade: string }[])
        .filter((c) => c.grade !== "BLOCKING").map((c) => c.code),
    },
  }));

  let blocked = false;
  try {
    dbQuery(`UPDATE monthly_close_snapshot SET net_profit = 99999999
              WHERE org_id = '${SEED.orgId}' AND year_month = '${YM}'`);
  } catch {
    blocked = true;
  }
  expect(blocked, "확정 snapshot 이 UPDATE 로 바뀌었다").toBe(true);

  await admin.dispose();
});
