import { expect, test } from "@playwright/test";
import { SEED, dbOne, dbQuery, reseed } from "./db";
import { apiAs, loginAs } from "./fixtures";

/**
 * IN_APP 알림 E2E (Phase 39).
 *
 * 왜 이 파일이 있나
 * --------------
 * 알림 인프라는 있었는데 **아무 도메인 서비스도 부르지 않았다.** 승인이 거절돼도
 * 월 마감이 막혀도 알림이 생기지 않았고, 화면은 mock 을 보고 있어서 그럴듯했다.
 * 게다가 헤더 벨은 필드 이름이 어긋나 **배지가 항상 0** 이었다 (D9).
 *
 * 확인하는 것
 * ---------
 * · 실제 업무 사건이 `notification` 행을 만든다
 * · 같은 사건이 반복돼도 하나다
 * · 반려는 요청자에게 간다 (승인자가 아니라)
 * · 남의 알림은 못 읽는다
 * · 벨 배지가 실제 숫자를 보여준다
 */

const YM = SEED.months.dashboard;

interface NotiRow {
  id: string;
  kind: string;
  recipient_user_id: string;
  dedupe_key: string;
  read_at: string | null;
  channel_code: string;
}

function notifications(kind?: string): NotiRow[] {
  const filter = kind ? ` AND kind = '${kind}'` : "";
  return dbQuery<NotiRow>(
    `SELECT id::text, kind::text, recipient_user_id::text, dedupe_key,
            read_at::text, channel_code::text
       FROM notification
      WHERE org_id = '${SEED.orgId}' AND deleted_at IS NULL${filter}
      ORDER BY created_at`,
  );
}

test.beforeEach(() => {
  reseed();
});

test.describe("실제 사건이 알림을 만든다", () => {
  test("월 마감 검증이 차단되면 알림이 생긴다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    expect(notifications()).toHaveLength(0);

    // seed 의 2026-06 은 상품 미연결 판매가 없어 차단 항목이 없다 → READY.
    const res = await api.post(`/api/v1/monthly-close/${YM}/validate`);
    expect(res.ok()).toBeTruthy();

    const rows = notifications();
    expect(rows.length).toBeGreaterThan(0);
    // 차단이 없으면 "확정 가능" 알림이다.
    expect(rows.map((r) => r.kind)).toContain("MONTH_CLOSE_READY");
    expect(rows.every((r) => r.channel_code === "IN_APP")).toBeTruthy();
  });

  test("같은 검증을 여러 번 돌려도 알림은 하나다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");

    for (let i = 0; i < 4; i += 1) {
      await api.post(`/api/v1/monthly-close/${YM}/validate`);
    }

    const ready = notifications("MONTH_CLOSE_READY");
    // 관리자 계정 수만큼만 생긴다 — 검증 횟수만큼이 아니다.
    const admins = dbQuery(
      `SELECT id FROM app_user WHERE org_id = '${SEED.orgId}'
         AND role IN ('ADMIN','ACCOUNTING') AND is_active = TRUE`,
    );
    expect(ready).toHaveLength(admins.length);
  });

  test("승인 요청 상신이 승인 권한자에게 간다", async ({ page }) => {
    const { api } = await loginAs(page, "operator");

    const created = await api.post("/api/v1/approvals", {
      data: {
        target_type: "product",
        target_id: SEED.products.memo,
        request_type: "PRODUCT_INFO_CHANGE",
        proposed_changes: { name_ko: "알림 테스트 상품" },
        reason: "E2E",
        submit: true,
      },
    });
    expect(created.ok(), await created.text()).toBeTruthy();

    const rows = notifications("APPROVAL_REQUESTED");
    expect(rows.length).toBeGreaterThan(0);
    // 요청자 본인에게는 가지 않는다.
    expect(rows.map((r) => r.recipient_user_id)).not.toContain(SEED.users.operator.id);
  });

  test("반려는 승인자가 아니라 요청자에게 간다", async ({ page }) => {
    const { api } = await loginAs(page, "operator");
    const created = await api.post("/api/v1/approvals", {
      data: {
        target_type: "product",
        target_id: SEED.products.memo,
        request_type: "PRODUCT_INFO_CHANGE",
        proposed_changes: { name_ko: "반려될 이름" },
        reason: "E2E",
        submit: true,
      },
    });
    const id = (await created.json()).data.id;

    const admin = await apiAs("approver");
    const rejected = await admin.post(`/api/v1/approvals/${id}/reject`, {
      data: { comment: "근거가 부족합니다" },
    });
    expect(rejected.ok(), await rejected.text()).toBeTruthy();

    const rows = notifications("APPROVAL_REJECTED");
    expect(rows).toHaveLength(1);
    expect(rows[0].recipient_user_id).toBe(SEED.users.operator.id);
  });
});

test.describe("읽음 처리", () => {
  test("미읽음 카운트가 실제 숫자를 준다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    await api.post(`/api/v1/monthly-close/${YM}/validate`);

    const res = await api.get("/api/v1/notifications/unread-count");
    const body = (await res.json()).data as { unread: number };
    // 예전 클라이언트가 읽던 unread_count 는 존재하지 않는다 (D9).
    expect(body).not.toHaveProperty("unread_count");
    expect(body.unread).toBeGreaterThan(0);
  });

  test("남의 알림은 읽음 처리할 수 없다", async ({ page }) => {
    const { api } = await loginAs(page, "operator");
    const created = await api.post("/api/v1/approvals", {
      data: {
        target_type: "product", target_id: SEED.products.memo,
        request_type: "PRODUCT_INFO_CHANGE",
        proposed_changes: { name_ko: "격리 확인" }, reason: "E2E", submit: true,
      },
    });
    const id = (await created.json()).data.id;
    const admin = await apiAs("approver");
    await admin.post(`/api/v1/approvals/${id}/reject`, { data: { comment: "거절" } });

    // 요청자(operator)에게 간 알림을 승인자(approver)가 읽으려 한다.
    const target = notifications("APPROVAL_REJECTED")[0];
    const res = await admin.post(`/api/v1/notifications/${target.id}/read`);
    expect(res.status()).toBe(404);

    const after = dbOne<{ read_at: string | null }>(
      `SELECT read_at::text FROM notification WHERE id = '${target.id}'`,
    );
    expect(after!.read_at).toBeNull();
  });

  test("모두 읽음은 본인 것만 처리한다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    await api.post(`/api/v1/monthly-close/${YM}/validate`);

    const before = notifications().filter((n) => n.read_at === null).length;
    expect(before).toBeGreaterThan(0);

    const res = await api.post("/api/v1/notifications/mark-all-read");
    expect(res.ok()).toBeTruthy();

    const mine = notifications().filter(
      (n) => n.recipient_user_id === SEED.users.accountant.id,
    );
    expect(mine.every((n) => n.read_at !== null)).toBeTruthy();
  });
});

test.describe("알림 화면", () => {
  test("벨 배지가 실제 미읽음 수를 보여준다 (D9)", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    await api.post(`/api/v1/monthly-close/${YM}/validate`);

    await page.goto("/dashboard");
    const badge = page.getByTestId("notification-unread-badge");
    await expect(badge).toBeVisible({ timeout: 20_000 });
    await expect(badge).not.toHaveText("0");
  });

  test("알림센터가 실제 알림을 보여준다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    await api.post(`/api/v1/monthly-close/${YM}/validate`);

    await page.goto("/notifications");
    await expect(page.getByText("월 마감 가능").first()).toBeVisible({ timeout: 20_000 });
    await expect(page.getByText("안 읽음").first()).toBeVisible();
  });

  test("알림이 없으면 지어내지 않는다", async ({ page }) => {
    await loginAs(page, "accountant");
    await page.goto("/notifications");

    await expect(page.getByText("알림이 없습니다")).toBeVisible({ timeout: 20_000 });
  });

  test("연결되지 않은 채널을 연결됨으로 표시하지 않는다", async ({ page }) => {
    await loginAs(page, "accountant");
    await page.goto("/notifications?tab=channels");

    await expect(page.getByText("전달 채널").first()).toBeVisible({ timeout: 20_000 });
    // Slack 은 webhook 이 없다 — 미연결이어야 한다.
    await expect(page.getByText("webhook URL 이 없어 연결할 수 없습니다.")).toBeVisible();
    // 성공 mock 을 만들던 연결·테스트 버튼이 없다.
    await expect(page.getByRole("button", { name: "연결" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "테스트 전송" })).toHaveCount(0);
  });
});
