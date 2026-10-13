import { expect, test } from "@playwright/test";
import { SEED, dbOne, dbQuery, reseed } from "./db";
import { apiAs, loginAs } from "./fixtures";

/**
 * 시스템 상태 화면 + worker 운영 API E2E (Phase 52).
 *
 * 왜 이 파일이 있나
 * --------------
 * worker 가 멈춘 것을 관리자가 알아차릴 방법이 없었다. API 는 있었지만 화면이
 * 없어서 DB 를 직접 봐야 알았다.
 *
 * 확인하는 것
 * ---------
 * · 상태 판단이 **서버**에서 온다 (화면이 임계값을 만들지 않는다)
 * · 수동 실행이 CLI 와 같은 코드를 돌리고 `worker_run` 에 남는다
 * · stack trace · SQL 이 응답에 없다
 * · ADMIN 외에는 못 본다
 */

interface WorkerHealth {
  worker_name: string;
  state: string;
  never_ran: boolean;
  has_queue: boolean;
  queue_depth: number;
  last_error_code: string | null;
  last_error_message: string | null;
}

test.beforeEach(() => {
  reseed();
});

test.describe("worker 관측 API", () => {
  test("상태를 서버가 판단해서 준다 — 화면이 계산하지 않는다", async ({ page }) => {
    await loginAs(page, "approver");
    const admin = await apiAs("approver");

    const res = await admin.get("/api/v1/operations/workers");
    expect(res.ok()).toBeTruthy();
    const body = (await res.json()).data as {
      workers: WorkerHealth[];
      stale_run_threshold_minutes: number;
    };

    // 개수가 아니라 **이름**을 본다. 숫자만 세면 worker 가 하나 사라지고
    // 하나 늘어도 통과한다.
    expect(new Set(body.workers.map((w) => w.worker_name))).toEqual(new Set([
      "due_approvals", "attachment_delete", "channel_publish",
      "period_recalc", "operation_issue_scan",
      // 자동화 배치 (M045). 이게 안 돌면 마진 감시·마감 사전점검이 멈춘다.
      "automation_scan",
    ]));
    // seed 가 worker_run 을 비우므로 전부 "실행된 적 없음" 이어야 한다.
    for (const w of body.workers) {
      expect(w.state, w.worker_name).toBe("NEVER_RAN");
      expect(w.never_ran).toBe(true);
    }
    // 중단 판정 기준을 서버가 알려준다 — 화면이 60분을 하드코딩하지 않는다.
    expect(body.stale_run_threshold_minutes).toBeGreaterThan(0);

    // 이슈 스캔은 큐 개념이 없다. "대기 0건" 을 좋은 소식으로 읽으면 안 된다.
    const scan = body.workers.find((w) => w.worker_name === "operation_issue_scan")!;
    expect(scan.has_queue).toBe(false);
  });

  test("수동 실행이 실제로 돌고 worker_run 에 남는다", async ({ page }) => {
    await loginAs(page, "approver");
    const admin = await apiAs("approver");

    const res = await admin.post("/api/v1/operations/workers/operation_issue_scan/run", {
      data: {},
    });
    expect(res.ok(), await res.text()).toBeTruthy();
    const result = (await res.json()).data as { status: string; worker: string };
    expect(result.status).toBe("SUCCESS");

    const run = dbOne<{ worker_name: string; status: string; trigger: string }>(
      `SELECT worker_name, status, trigger FROM worker_run
        WHERE worker_name = 'operation_issue_scan'
        ORDER BY started_at DESC LIMIT 1`,
    );
    expect(run!.status).toBe("SUCCESS");
    expect(run!.trigger).toBe("API");

    // 실행 후 상태가 NEVER_RAN 을 벗어난다.
    const after = (await (
      await admin.get("/api/v1/operations/workers")
    ).json()).data as { workers: WorkerHealth[] };
    const scan = after.workers.find((w) => w.worker_name === "operation_issue_scan")!;
    expect(scan.never_ran).toBe(false);
    expect(["IDLE", "PENDING_QUEUE"]).toContain(scan.state);
  });

  test("알 수 없는 worker 이름은 404 다 — 오타를 새 worker 로 만들지 않는다", async ({ page }) => {
    await loginAs(page, "approver");
    const admin = await apiAs("approver");

    const res = await admin.post("/api/v1/operations/workers/typo_worker/run", { data: {} });
    expect(res.status()).toBe(404);
    expect(dbQuery(`SELECT id FROM worker_run WHERE worker_name = 'typo_worker'`)).toHaveLength(0);
  });

  test("실행 이력에 stack trace 나 SQL 이 없다", async ({ page }) => {
    await loginAs(page, "approver");
    const admin = await apiAs("approver");
    await admin.post("/api/v1/operations/workers/due_approvals/run", { data: {} });

    const raw = await (await admin.get("/api/v1/operations/workers/runs?limit=20")).text();
    expect(raw).not.toContain("Traceback");
    expect(raw).not.toContain("SELECT ");
    expect(raw).not.toContain("site-packages");
    expect(raw).not.toContain("change-me-local");
  });

  test("중단 기록 정리가 영구 RUNNING 을 없앤다", async ({ page }) => {
    await loginAs(page, "approver");
    const admin = await apiAs("approver");

    const { dbExec } = await import("./db");
    dbExec(
      `INSERT INTO worker_run (org_id, worker_name, trigger, status, started_at)
       VALUES ('${SEED.orgId}', 'channel_publish', 'MANUAL', 'RUNNING',
               now() - INTERVAL '5 hours')`,
    );

    const res = await admin.post("/api/v1/operations/workers/reclaim-stale", { data: {} });
    expect(res.ok()).toBeTruthy();
    expect((await res.json()).data.reclaimed).toBeGreaterThanOrEqual(1);

    const row = dbOne<{ status: string; error_code: string }>(
      `SELECT status, error_code FROM worker_run
        WHERE worker_name = 'channel_publish' ORDER BY started_at DESC LIMIT 1`,
    );
    expect(row!.status).toBe("FAILED");
    expect(row!.error_code).toBe("STALE_RUN");
  });
});

test.describe("권한", () => {
  test("ADMIN 외에는 worker 상태를 볼 수 없다", async ({ page }) => {
    const { api } = await loginAs(page, "accountant");
    expect((await api.get("/api/v1/operations/workers")).status()).toBe(403);
    expect((await api.get("/api/v1/operations/workers/runs")).status()).toBe(403);
  });

  test("ADMIN 외에는 worker 를 실행할 수 없다", async ({ page }) => {
    const { api } = await loginAs(page, "operator");

    const res = await api.post("/api/v1/operations/workers/due_approvals/run", { data: {} });
    expect(res.status()).toBe(403);
    // 403 이면 실행 기록도 남지 않아야 한다.
    expect(
      dbQuery(`SELECT id FROM worker_run WHERE worker_name = 'due_approvals'`),
    ).toHaveLength(0);
  });
});

test.describe("시스템 상태 화면", () => {
  test("worker 목록과 상태가 화면에 보인다", async ({ page }) => {
    await loginAs(page, "approver");
    await page.goto("/operations/system");

    await expect(
      page.getByRole("heading", { name: "백그라운드 worker" }),
    ).toBeVisible({ timeout: 20_000 });
    // worker 이름을 코드가 아니라 한글로 보여준다.
    await expect(page.getByText("승인 자동 적용").first()).toBeVisible();
    await expect(page.getByText("운영 이슈 스캔").first()).toBeVisible();
    // 한 번도 안 돈 상태를 "정상" 으로 보여주지 않는다.
    await expect(page.getByText("실행된 적 없음").first()).toBeVisible();
    // worker 코드명을 그대로 노출하지 않는다.
    await expect(page.getByText("due_approvals")).toHaveCount(0);
  });

  test("관리자가 아니면 실행 버튼이 없고 그 사실을 알려준다", async ({ page }) => {
    await loginAs(page, "accountant");
    await page.goto("/operations/system");

    await expect(
      page.getByText("worker 실행과 실행 이력 조회는 관리자만 할 수 있습니다."),
    ).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole("button", { name: "지금 실행" })).toHaveCount(0);
  });

  test("화면에서 worker 를 실행하면 DB 에 기록이 남는다", async ({ page }) => {
    await loginAs(page, "approver");
    await page.goto("/operations/system");

    await expect(page.getByText("운영 이슈 스캔").first()).toBeVisible({ timeout: 20_000 });

    // 이슈 스캔 카드의 실행 버튼 (마지막 worker).
    const buttons = page.getByRole("button", { name: "지금 실행" });
    await buttons.last().click();

    await expect(page.getByText("실행 완료", { exact: false })).toBeVisible({ timeout: 20_000 });

    const run = dbOne<{ status: string; trigger: string }>(
      `SELECT status, trigger FROM worker_run ORDER BY started_at DESC LIMIT 1`,
    );
    expect(run!.trigger).toBe("API");
    expect(run!.status).toBe("SUCCESS");
  });
});
