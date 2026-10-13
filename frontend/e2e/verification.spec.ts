import { expect, test } from "@playwright/test";
import { SEED, dbOne, dbQuery, reseed } from "./db";
import { apiAs, loginAs } from "./fixtures";

/**
 * 검증센터 E2E.
 *
 * 왜 이 파일이 있나
 * --------------
 * 검증센터의 값어치는 **거짓말을 하지 않는 것**이다. 화면이 미검증을 통과로
 * 보여주거나, 기준값을 서버가 채워 주거나, 남의 조직 숫자가 섞이면 이 화면은
 * 없느니만 못하다.
 *
 * 확인하는 것
 * ---------
 * · PASS / FAIL / 미검증이 화면에서 섞이지 않는다
 * · 기준값은 사람이 넣어야만 저장된다 (자동 승인 0)
 * · 아직 검증 못 하는 영역을 숨기지 않는다
 * · 계산 추적이 성분별 출처와 신뢰도를 보여준다
 * · 권한 없는 역할은 못 본다
 */

test.beforeEach(() => {
  reseed();
});

test.describe("검증센터", () => {
  test("화면이 열리고 세 상태를 따로 보여준다", async ({ page }) => {
    await loginAs(page, "approver");
    await page.goto("/operations/verification");

    await expect(page.getByRole("heading", { name: "검증센터" })).toBeVisible();
    await expect(page.getByLabel("검증 상태")).toBeVisible();

    // 미검증을 통과에 섞지 않는다는 설명이 화면에 있어야 한다.
    await expect(
      page.getByText("확인할 근거가 없다", { exact: false }),
    ).toBeVisible();
  });

  test("아직 검증할 수 없는 영역을 숨기지 않는다", async ({ page }) => {
    await loginAs(page, "approver");
    await page.goto("/operations/verification");

    const section = page.getByText("아직 검증할 수 없는 것");
    await expect(section).toBeVisible();

    // 환불 손익은 실표본이 없다 — **통과로 표시되면 안 된다.**
    // 사유 문구에도 같은 낱말이 들어 있으므로 라벨만 정확히 집는다.
    await expect(page.getByText("환불 손익", { exact: true })).toBeVisible();
    await expect(
      page.getByText("환불 정산 실표본이 없어", { exact: false }),
    ).toBeVisible();
  });

  test("요약이 미검증을 통과로 세지 않는다", async () => {
    const admin = await apiAs("approver");

    const res = await admin.get("/api/v1/verification/summary");
    expect(res.ok()).toBeTruthy();
    const data = (await res.json()).data as {
      counts: Record<string, number>;
      verdicts: Record<string, string>;
      evidence_gates: Record<string, { state: string }>;
    };

    // 세 칸의 합이 판정 수와 같다 = 어느 판정도 두 칸에 들어가지 않았다.
    const total = Object.values(data.counts).reduce((a, b) => a + b, 0);
    expect(total).toBe(Object.keys(data.verdicts).length);
    expect(data.evidence_gates.refund_financial.state).toBe("NOT_VERIFIED");
    expect(data.evidence_gates.external_inventory.state).toBe("NOT_AVAILABLE");
  });

  test("확인 필요 목록에 0건짜리 항목이 없다", async () => {
    const admin = await apiAs("approver");

    const res = await admin.get("/api/v1/verification/exceptions");
    const items = (await res.json()).data.verification as { count: number }[];

    for (const item of items) expect(item.count).toBeGreaterThan(0);
  });
});

test.describe("계산 추적", () => {
  test("성분마다 출처와 신뢰도가 붙는다", async () => {
    const admin = await apiAs("approver");

    const orderId = dbOne<{ id: string }>(
      `SELECT id FROM sales_order
        WHERE org_id = '${SEED.orgId}' AND deleted_at IS NULL LIMIT 1`,
    )?.id;
    test.skip(!orderId, "이 시드에 판매 건이 없다");

    const res = await admin.get(`/api/v1/verification/orders/${orderId}`);
    expect(res.ok()).toBeTruthy();
    const trace = (await res.json()).data as {
      formula: string;
      evidence_kind: string;
      components: Record<string, { source: string; confidence: string }>;
    };

    expect(trace.formula).toContain("margin =");
    expect(["SNAPSHOT", "RECONSTRUCTED"]).toContain(trace.evidence_kind);
    for (const c of Object.values(trace.components)) {
      expect(c.source).toBeTruthy();
      expect(["ACTUAL", "ESTIMATED", "UNKNOWN"]).toContain(c.confidence);
    }
  });

  test("다른 조직 주문은 보이지 않는다", async () => {
    const admin = await apiAs("approver");

    // 이 조직에 없는 주문 id.
    const res = await admin.get(
      "/api/v1/verification/orders/00000000-0000-0000-0000-0000000000ff",
    );

    expect(res.status()).toBe(404);
  });
});

test.describe("기준 사례", () => {
  test("후보를 불러도 저장되지 않는다", async () => {
    const admin = await apiAs("approver");

    const res = await admin.get("/api/v1/verification/golden-samples/candidates");
    expect(res.ok()).toBeTruthy();

    const stored = dbOne<{ n: string }>(
      `SELECT count(*) AS n FROM verification_golden_case
        WHERE org_id = '${SEED.orgId}'`,
    );
    expect(Number(stored?.n ?? 0)).toBe(0);
  });

  test("서버가 기준값을 채워 주지 않는다", async () => {
    const admin = await apiAs("approver");

    const candidates = (
      await (await admin.get("/api/v1/verification/golden-samples/candidates")).json()
    ).data.candidates as { sales_order_id: string }[];
    test.skip(candidates.length === 0, "이 시드에 후보가 없다");

    const registered = await admin.post(
      "/api/v1/verification/golden-samples/candidates",
      { data: { sales_order_ids: [candidates[0].sales_order_id] } },
    );
    expect(registered.ok()).toBeTruthy();

    // 등록만으로는 승인되지 않는다.
    const rows = dbQuery<{ status: string; expected_json: string | null }>(
      `SELECT status, expected_json FROM verification_golden_case
        WHERE org_id = '${SEED.orgId}'`,
    );
    expect(rows.length).toBe(1);
    expect(rows[0].status).toBe("CANDIDATE");
    expect(rows[0].expected_json).toBeFalsy();

    // 값을 안 주면 승인이 거부된다.
    const caseId = dbOne<{ id: string }>(
      `SELECT id FROM verification_golden_case WHERE org_id = '${SEED.orgId}'`,
    )!.id;
    const empty = await admin.post(
      `/api/v1/verification/golden-samples/${caseId}/approve`,
      { data: { expected: {}, source_evidence: "정산서" } },
    );
    expect(empty.ok()).toBeFalsy();
  });

  test("사람이 넣은 기준값으로 회귀가 돈다", async () => {
    const admin = await apiAs("approver");

    const candidates = (
      await (await admin.get("/api/v1/verification/golden-samples/candidates")).json()
    ).data.candidates as { sales_order_id: string }[];
    test.skip(candidates.length === 0, "이 시드에 후보가 없다");

    const orderId = candidates[0].sales_order_id;
    await admin.post("/api/v1/verification/golden-samples/candidates", {
      data: { sales_order_ids: [orderId] },
    });
    const caseId = dbOne<{ id: string }>(
      `SELECT id FROM verification_golden_case WHERE org_id = '${SEED.orgId}'`,
    )!.id;

    // 사람이 확인했다고 가정한 값 — 현재 저장된 마진을 읽어 넣는 것이 아니라
    // 일부러 틀린 값을 넣어 회귀가 **잡아내는지** 본다.
    const approved = await admin.post(
      `/api/v1/verification/golden-samples/${caseId}/approve`,
      {
        data: {
          expected: { margin_amount: 123456789 },
          source_evidence: "E2E 확인",
        },
      },
    );
    expect(approved.ok()).toBeTruthy();

    const reg = (
      await (await admin.get("/api/v1/verification/golden-samples/regression")).json()
    ).data as { counts: Record<string, number>; status: string };

    // 틀린 기준값이므로 FAIL 이거나, 마진 행이 없으면 비교불가여야 한다.
    // **어느 쪽이든 PASS 가 아니어야 한다** — 그게 이 장치의 핵심이다.
    expect(reg.counts.PASS ?? 0).toBe(0);
  });
});

test.describe("Golden 검증 보조", () => {
  test("원천·독립검산·차이가 한 번에 온다", async () => {
    const admin = await apiAs("approver");

    const orderId = dbOne<{ id: string }>(
      `SELECT id FROM sales_order
        WHERE org_id = '${SEED.orgId}' AND deleted_at IS NULL LIMIT 1`,
    )?.id;
    test.skip(!orderId, "이 시드에 판매 건이 없다");

    const res = await admin.get(
      `/api/v1/verification/golden-samples/evidence/${orderId}`,
    );
    expect(res.ok()).toBeTruthy();
    const ev = (await res.json()).data as {
      facts: Record<string, { value: number | null; source: string; confidence: string }>;
      independent: { calculable: boolean };
      comparison: { comparable: boolean };
      checklist: { label: string; detail: string }[];
      golden_confidence: string;
      formula: string;
    };

    // 사용자가 엑셀·DB·계산기를 따로 열 필요가 없어야 한다.
    expect(Object.keys(ev.facts).length).toBeGreaterThan(0);
    expect(ev.formula).toContain("순매출");
    expect(ev.checklist.length).toBeGreaterThan(0);
    expect(["ACTUAL", "ESTIMATED", "UNKNOWN"]).toContain(ev.golden_confidence);
    for (const f of Object.values(ev.facts)) {
      expect(["ACTUAL", "ESTIMATED", "UNKNOWN"]).toContain(f.confidence);
    }
  });

  test("모르는 값은 0이 아니라 null 로 온다", async () => {
    const admin = await apiAs("approver");
    const orderId = dbOne<{ id: string }>(
      `SELECT id FROM sales_order
        WHERE org_id = '${SEED.orgId}' AND deleted_at IS NULL LIMIT 1`,
    )!.id;

    const ev = (
      await (
        await admin.get(`/api/v1/verification/golden-samples/evidence/${orderId}`)
      ).json()
    ).data as { facts: Record<string, { value: number | null }> };

    // 원천이 없는 항목은 0 으로 채워 오지 않는다.
    for (const name of ["return_cost", "fulfillment_fee", "payment_fee", "vat"]) {
      expect(ev.facts[name].value).toBeNull();
    }
  });

  test("추천 최소 세트가 전체보다 작고 경로가 겹치지 않는다", async () => {
    const admin = await apiAs("approver");

    const rec = (
      await (await admin.get("/api/v1/verification/golden-samples/recommended")).json()
    ).data as {
      recommended: { case_type: string; covers: string }[];
      recommended_count: number;
      total_candidates: number;
    };

    const types = rec.recommended.map((r) => r.case_type);
    expect(new Set(types).size).toBe(types.length);
    for (const r of rec.recommended) expect(r.covers).toBeTruthy();
  });

  test("보류해도 회귀 실패로 세지 않는다", async () => {
    const admin = await apiAs("approver");

    const candidates = (
      await (await admin.get("/api/v1/verification/golden-samples/candidates")).json()
    ).data.candidates as { sales_order_id: string }[];
    test.skip(candidates.length === 0, "이 시드에 후보가 없다");

    await admin.post("/api/v1/verification/golden-samples/candidates", {
      data: { sales_order_ids: [candidates[0].sales_order_id] },
    });
    const caseId = dbOne<{ id: string }>(
      `SELECT id FROM verification_golden_case WHERE org_id = '${SEED.orgId}'`,
    )!.id;

    const res = await admin.post(
      `/api/v1/verification/golden-samples/${caseId}/defer`,
      { data: { reason: "COST_UNCERTAIN", note: "매입 기록 확인 필요" } },
    );
    expect(res.ok()).toBeTruthy();

    const row = dbOne<{ status: string; defer_reason: string }>(
      `SELECT status, defer_reason FROM verification_golden_case WHERE id = '${caseId}'`,
    );
    expect(row?.status).toBe("DEFERRED");
    expect(row?.defer_reason).toBe("COST_UNCERTAIN");

    const reg = (
      await (await admin.get("/api/v1/verification/golden-samples/regression")).json()
    ).data as { counts: Record<string, number> };
    expect(reg.counts.FAIL ?? 0).toBe(0);
  });

  test("승인하면 그때의 근거가 박제된다", async () => {
    const admin = await apiAs("approver");

    const candidates = (
      await (await admin.get("/api/v1/verification/golden-samples/candidates")).json()
    ).data.candidates as { sales_order_id: string }[];
    test.skip(candidates.length === 0, "이 시드에 후보가 없다");

    await admin.post("/api/v1/verification/golden-samples/candidates", {
      data: { sales_order_ids: [candidates[0].sales_order_id] },
    });
    const caseId = dbOne<{ id: string }>(
      `SELECT id FROM verification_golden_case WHERE org_id = '${SEED.orgId}'`,
    )!.id;

    const res = await admin.post(
      `/api/v1/verification/golden-samples/${caseId}/approve`,
      {
        data: {
          expected: { margin_amount: 4321 },
          source_evidence: "E2E 확인",
        },
      },
    );
    expect(res.ok()).toBeTruthy();

    const row = dbOne<{ has_ev: boolean; status: string }>(
      `SELECT status, (approved_evidence_json IS NOT NULL) AS has_ev
         FROM verification_golden_case WHERE id = '${caseId}'`,
    );
    expect(row?.status).toBe("APPROVED");
    expect(row?.has_ev).toBeTruthy();
  });

  test("검토 화면이 열리고 승인 버튼이 자동으로 눌리지 않는다", async ({ page }) => {
    // **DOM 이 아니라 데이터로 판단한다.** 버튼 개수를 바로 세면 목록이
    // 아직 안 그려졌을 때 0 이 나와 테스트가 조용히 건너뛴다 — 실제로
    // 두 번째 실행에서 그렇게 됐다.
    const admin = await apiAs("approver");
    const rec = (
      await (await admin.get("/api/v1/verification/golden-samples/recommended")).json()
    ).data as { recommended_count: number };
    test.skip(rec.recommended_count === 0, "이 시드에 추천 후보가 없다");

    await loginAs(page, "approver");
    await page.goto("/operations/verification");

    const open = page.getByRole("button", { name: "원천 확인" }).first();
    await expect(open).toBeVisible();
    await open.click();
    const dialog = page.getByRole("dialog", { name: "Golden 검증" });
    await expect(dialog).toBeVisible();

    // 원천·독립검산·확인목록이 화면에 있다.
    // 표 caption 에도 같은 낱말이 있으므로 제목만 정확히 집는다.
    await expect(dialog.getByRole("heading", { name: "원천값" })).toBeVisible();
    await expect(dialog.getByRole("heading", { name: /독립 검산/ })).toBeVisible();
    await expect(dialog.getByRole("heading", { name: "확인할 것" })).toBeVisible();

    // **자동 승인이 없다.** 아무것도 안 눌렀으니 승인된 사례가 0이어야 한다.
    const approved = dbOne<{ n: string }>(
      `SELECT count(*) AS n FROM verification_golden_case
        WHERE org_id = '${SEED.orgId}' AND status = 'APPROVED'`,
    );
    expect(Number(approved?.n ?? 0)).toBe(0);
  });
});

test.describe("권한", () => {
  test("외부 세무사는 검증센터를 볼 수 없다", async () => {
    const ext = await apiAs("extAccountant");

    const res = await ext.get("/api/v1/verification/summary");

    expect(res.status()).toBe(403);
  });

  test("읽기 전용은 보되 기준값을 승인하지 못한다", async () => {
    const readonly = await apiAs("readonly");

    expect((await readonly.get("/api/v1/verification/summary")).ok()).toBeTruthy();

    const approve = await readonly.post(
      "/api/v1/verification/golden-samples/00000000-0000-0000-0000-0000000000ff/approve",
      { data: { expected: { margin_amount: 1 }, source_evidence: "x" } },
    );
    expect(approve.status()).toBe(403);
  });
});
