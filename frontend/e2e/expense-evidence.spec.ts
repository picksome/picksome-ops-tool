/**
 * 증빙 파일 — **실제 백엔드 · 실제 DB · 실제 디스크**.
 *
 * mock storage 로는 이 테스트를 만들 수 없다. 확인해야 하는 것이
 * "저장했다고 응답했다" 가 아니라 **파일이 진짜 디스크에 있고, 삭제 뒤에는
 * 진짜 사라졌는가** 이기 때문이다. 그래서 매 단언이 DB 와 파일시스템을 함께 본다.
 *
 * 여기서 지키는 것
 *   · 업로드한 파일이 새로고침 후에도 남는다
 *   · 승인된 비용의 증빙은 승인 없이 못 지운다
 *   · 승인이 적용돼도 **worker 가 돌기 전까지는 파일이 남아 있다**
 *   · storage 내부 키가 API 응답에 새지 않는다
 *   · 다른 조직의 첨부는 존재조차 알 수 없다
 */

import { test, expect, apiAs, loginAs } from "./fixtures";
import { SEED, attachmentFiles, attachmentsOf, dbQuery, reseed } from "./db";

test.beforeEach(() => {
  reseed();
});

/** 최소 유효 PDF/PNG. 내용 검사(magic byte)를 통과해야 한다. */
const PDF = Buffer.concat([Buffer.from("%PDF-1.7\n"), Buffer.alloc(256)]);
const PNG = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]), Buffer.alloc(256),
]);

function upload(
  api: import("@playwright/test").APIRequestContext,
  expenseId: string,
  file: { name: string; mimeType: string; buffer: Buffer },
) {
  return api.post(`/api/v1/expenses/${expenseId}/attachments`, {
    multipart: { file },
  });
}

async function openEvidence(page: import("@playwright/test").Page, expenseId: string) {
  await page.goto(`/expenses?expenseId=${expenseId}`);
  await expect(page.getByRole("dialog")).toBeVisible({ timeout: 20_000 });
}

/* ── 업로드 ─────────────────────────────────────────────── */

test("PDF 업로드 — DB 행과 실제 파일이 함께 생긴다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  const before = attachmentFiles().length;

  const res = await upload(acc.api, SEED.expenses.open, {
    name: "8월 광고비 세금계산서.pdf", mimeType: "application/pdf", buffer: PDF,
  });
  expect(res.ok(), await res.text()).toBeTruthy();
  const body = (await res.json()).data;

  // ① 응답에 storage 내부 키가 없다 — 저장 구조가 새면 안 된다
  expect(Object.keys(body).some((k) => k.includes("storage"))).toBe(false);
  expect(JSON.stringify(body)).not.toContain(SEED.orgId);

  // ② DB 행
  const rows = attachmentsOf(SEED.expenses.open);
  expect(rows).toHaveLength(1);
  expect(rows[0].original_filename).toBe("8월 광고비 세금계산서.pdf");
  expect(rows[0].status).toBe("AVAILABLE");
  // 스캐너가 없다 — CLEAN 으로 표시하면 거짓말이다
  expect(rows[0].scan_status).toBe("SCAN_UNAVAILABLE");

  // ③ 실제 파일. 파일명은 UUID 고 원본 이름이 경로에 들어가지 않는다
  const files = attachmentFiles();
  expect(files.length).toBe(before + 1);
  const added = files.find((f) => f.includes(rows[0].id));
  expect(added, "저장 키에 첨부 id 가 없다").toBeTruthy();
  expect(added).not.toContain("광고비");
  expect(added).toMatch(/\.pdf$/);

  // ④ 증빙 상태가 자동으로 올라간다 — 증빙 누락 리포트에서 빠져야 한다
  const ev = dbQuery<{ evidence_status: string }>(
    `SELECT evidence_status FROM expenses WHERE id = '${SEED.expenses.open}'`);
  expect(ev[0].evidence_status).toBe("PRESENT");
});

test("확장자를 위장한 파일은 거부된다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  // 이름도 .pdf, MIME 도 application/pdf — 내용만 PNG 다.
  const res = await upload(acc.api, SEED.expenses.open, {
    name: "evil.pdf", mimeType: "application/pdf", buffer: PNG,
  });
  expect(res.status()).toBe(422);
  expect((await res.json()).error.details.reason).toBe("CONTENT_MISMATCH");

  expect(attachmentsOf(SEED.expenses.open)).toHaveLength(0);
  // 거부된 파일이 디스크에 남으면 안 된다
  expect(attachmentFiles().filter((f) => f.endsWith(".pdf"))).toHaveLength(0);
});

test("허용하지 않는 형식은 거부된다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  const res = await upload(acc.api, SEED.expenses.open, {
    name: "payload.exe", mimeType: "application/octet-stream",
    buffer: Buffer.from("MZ\x00\x00"),
  });
  expect(res.status()).toBe(422);
  expect((await res.json()).error.details.reason).toBe("EXTENSION_NOT_ALLOWED");
});

test("같은 파일을 두 번 올리면 기존 것을 알려준다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  const first = await upload(acc.api, SEED.expenses.open, {
    name: "영수증.png", mimeType: "image/png", buffer: PNG,
  });
  expect(first.ok()).toBeTruthy();

  const dup = await upload(acc.api, SEED.expenses.open, {
    name: "같은파일-다른이름.png", mimeType: "image/png", buffer: PNG,
  });
  expect(dup.status()).toBe(409);
  const detail = (await dup.json()).error.details;
  expect(detail.reason).toBe("DUPLICATE_FILE");
  expect(detail.existing_attachment_id).toBe((await first.json()).data.id);

  // 중복은 저장되지 않는다
  expect(attachmentsOf(SEED.expenses.open)).toHaveLength(1);
});

test("첨부 개수 상한을 넘기면 거부된다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  for (let i = 0; i < 5; i++) {
    // 해시가 달라야 중복으로 걸리지 않는다
    const buf = Buffer.concat([PDF, Buffer.from(`unique-${i}`)]);
    const res = await upload(acc.api, SEED.expenses.open, {
      name: `증빙${i}.pdf`, mimeType: "application/pdf", buffer: buf,
    });
    expect(res.ok(), await res.text()).toBeTruthy();
  }
  const over = await upload(acc.api, SEED.expenses.open, {
    name: "여섯번째.pdf", mimeType: "application/pdf",
    buffer: Buffer.concat([PDF, Buffer.from("sixth")]),
  });
  expect(over.status()).toBe(422);
  expect((await over.json()).error.details.reason).toBe("TOO_MANY_ATTACHMENTS");
  expect(attachmentsOf(SEED.expenses.open)).toHaveLength(5);
});

test("잠긴 달에도 증빙은 추가할 수 있다 — 금액을 바꾸지 않기 때문", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  const res = await upload(acc.api, SEED.expenses.locked, {
    name: "마감후 받은 계산서.pdf", mimeType: "application/pdf", buffer: PDF,
  });
  expect(res.ok(), await res.text()).toBeTruthy();
  expect(attachmentsOf(SEED.expenses.locked)).toHaveLength(1);
});

/* ── 조직 격리 ──────────────────────────────────────────── */

test("다른 조직의 비용에는 올릴 수 없다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  // 존재하지 않는(=다른 조직의) 비용 id. 404 여야 한다 —
  // 403 으로 구분해 주면 id 존재 여부가 새어나간다.
  const res = await upload(acc.api, "00000000-0000-0000-0000-0000000000ff", {
    name: "x.pdf", mimeType: "application/pdf", buffer: PDF,
  });
  expect(res.status()).toBe(404);
});

/* ── 다운로드 ───────────────────────────────────────────── */

test("다운로드 — 올린 그대로 받고, 권한 없으면 못 받는다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  const created = await upload(acc.api, SEED.expenses.open, {
    name: "카드전표.png", mimeType: "image/png", buffer: PNG,
  });
  const id = (await created.json()).data.id as string;

  const ok = await acc.api.get(`/api/v1/expense-attachments/${id}/download`);
  expect(ok.ok()).toBeTruthy();
  expect(Buffer.compare(Buffer.from(await ok.body()), PNG)).toBe(0);
  // 파일명이 헤더에서 깨지지 않는다(RFC 5987)
  expect(ok.headers()["content-disposition"]).toContain("filename*=UTF-8''");
  expect(ok.headers()["x-content-type-options"]).toBe("nosniff");

  // READONLY 는 목록은 봐도 원본은 못 연다 — 증빙에는 거래 상대가 그대로 찍혀 있다
  const ro = await apiAs("readonly");
  const denied = await ro.get(`/api/v1/expense-attachments/${id}/download`);
  expect(denied.status()).toBe(403);
  expect((await denied.json()).error.details.required_permission)
    .toBe("expense.view_attachment");
  await ro.dispose();
});

/* ── 화면 ───────────────────────────────────────────────── */

test("화면에서 업로드하면 목록에 뜨고 새로고침해도 남는다", async ({ page }) => {
  await loginAs(page, "accountant");
  await openEvidence(page, SEED.expenses.open);

  await page.locator('input[type="file"]').setInputFiles({
    name: "화면업로드.pdf", mimeType: "application/pdf", buffer: PDF,
  });

  await expect(page.getByText("화면업로드.pdf").first()).toBeVisible({ timeout: 20_000 });
  // 스캐너가 없다는 사실을 화면이 말한다 — "안전" 이라고 하지 않는다
  await expect(page.getByText("검사 안 함").first()).toBeVisible();

  // DB 와 디스크에 실제로 있다
  const rows = attachmentsOf(SEED.expenses.open);
  expect(rows).toHaveLength(1);
  expect(attachmentFiles().some((f) => f.includes(rows[0].id))).toBe(true);

  // 새로고침 — 브라우저 메모리에만 있던 예전 방식이면 여기서 사라진다
  await page.reload();
  await expect(page.getByRole("dialog")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText("화면업로드.pdf").first()).toBeVisible();
});

test("10MB 초과는 화면에서 걸러 서버까지 가지 않는다", async ({ page }) => {
  await loginAs(page, "accountant");
  await openEvidence(page, SEED.expenses.open);

  const huge = Buffer.concat([PDF, Buffer.alloc(10 * 1024 * 1024)]);
  await page.locator('input[type="file"]').setInputFiles({
    name: "너무큰파일.pdf", mimeType: "application/pdf", buffer: huge,
  });

  await expect(page.getByText(/10MB를 넘습니다/)).toBeVisible({ timeout: 20_000 });
  expect(attachmentsOf(SEED.expenses.open)).toHaveLength(0);
});

/* ── 삭제 ───────────────────────────────────────────────── */

test("미승인 비용의 증빙은 바로 삭제 요청되고 worker 가 파일을 지운다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  const created = await upload(acc.api, SEED.expenses.open, {
    name: "삭제대상.pdf", mimeType: "application/pdf", buffer: PDF,
  });
  const id = (await created.json()).data.id as string;
  expect(attachmentFiles().some((f) => f.includes(id))).toBe(true);

  const del = await acc.api.delete(`/api/v1/expense-attachments/${id}`);
  expect(del.ok(), await del.text()).toBeTruthy();

  // ① 상태만 바뀌고 **파일은 아직 있다** — 승인/삭제 트랜잭션에서 파일을 지우면
  //    롤백됐을 때 되돌릴 방법이 없다
  expect(attachmentsOf(SEED.expenses.open)[0].status).toBe("PENDING_DELETE");
  expect(attachmentFiles().some((f) => f.includes(id))).toBe(true);

  // ② worker 가 돈 뒤에야 사라진다
  const admin = await apiAs("approver");   // ADMIN
  const run = await admin.post("/api/v1/expense-attachments/run-delete");
  expect(run.ok(), await run.text()).toBeTruthy();
  expect((await run.json()).data.deleted).toBe(1);
  await admin.dispose();

  expect(attachmentFiles().some((f) => f.includes(id))).toBe(false);
  // 살아 있는 첨부가 없으니 증빙 상태도 되돌아간다
  const ev = dbQuery<{ evidence_status: string }>(
    `SELECT evidence_status FROM expenses WHERE id = '${SEED.expenses.open}'`);
  expect(ev[0].evidence_status).toBe("NONE");
});

test("승인된 비용의 증빙은 승인 없이 못 지운다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  const created = await upload(acc.api, SEED.expenses.open, {
    name: "승인된비용 증빙.pdf", mimeType: "application/pdf", buffer: PDF,
  });
  const attachmentId = (await created.json()).data.id as string;

  // 이 비용에 적용된 승인을 하나 만든다 → 이제 증빙 삭제는 승인 대상이다
  const reqRes = await acc.api.post("/api/v1/approvals", {
    data: {
      target_type: "expense", target_id: SEED.expenses.open,
      request_type: "EXPENSE_FINANCIAL_CHANGE",
      proposed_changes: { gross_amount: 130000 }, reason: "E2E", submit: true,
    },
  });
  const requestId = (await reqRes.json()).data.id as string;
  const admin = await apiAs("approver");
  const ap = await admin.post(`/api/v1/approvals/${requestId}/approve`,
    { data: { comment: "E2E 승인" } });
  expect(ap.ok(), await ap.text()).toBeTruthy();
  const apl = await admin.post(`/api/v1/approvals/${requestId}/apply`);
  expect(apl.ok(), await apl.text()).toBeTruthy();

  // 직접 삭제는 막힌다
  const blocked = await acc.api.delete(`/api/v1/expense-attachments/${attachmentId}`);
  expect(blocked.status()).toBe(403);
  expect((await blocked.json()).error.code).toBe("APPROVAL_REQUIRED");
  expect(attachmentsOf(SEED.expenses.open)[0].status).toBe("AVAILABLE");
  expect(attachmentFiles().some((f) => f.includes(attachmentId))).toBe(true);

  // 승인 요청 → 승인 → 적용
  const delReq = await acc.api.post("/api/v1/approvals", {
    data: {
      target_type: "expense_attachment", target_id: attachmentId,
      request_type: "EXPENSE_EVIDENCE_CHANGE",
      proposed_changes: { status: "PENDING_DELETE" },
      reason: "잘못 첨부함", submit: true,
    },
  });
  expect(delReq.ok(), await delReq.text()).toBeTruthy();
  const delReqId = (await delReq.json()).data.id as string;

  const delApproved = await admin.post(`/api/v1/approvals/${delReqId}/approve`,
    { data: { comment: "E2E 삭제 승인" } });
  expect(delApproved.ok(), await delApproved.text()).toBeTruthy();
  const applied = await admin.post(`/api/v1/approvals/${delReqId}/apply`);
  expect(applied.ok(), await applied.text()).toBeTruthy();

  // 적용 직후에도 파일은 남아 있다
  expect(attachmentsOf(SEED.expenses.open)[0].status).toBe("PENDING_DELETE");
  expect(attachmentFiles().some((f) => f.includes(attachmentId))).toBe(true);

  // worker 가 지운다
  const run = await admin.post("/api/v1/expense-attachments/run-delete");
  expect((await run.json()).data.deleted).toBe(1);
  expect(attachmentFiles().some((f) => f.includes(attachmentId))).toBe(false);
  await admin.dispose();
});

test("잠긴 달의 증빙은 삭제할 수 없다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  const created = await upload(acc.api, SEED.expenses.locked, {
    name: "잠긴달 증빙.pdf", mimeType: "application/pdf", buffer: PDF,
  });
  const id = (await created.json()).data.id as string;

  const blocked = await acc.api.delete(`/api/v1/expense-attachments/${id}`);
  expect(blocked.status()).toBe(409);
  expect(attachmentsOf(SEED.expenses.locked)[0].status).toBe("AVAILABLE");
  expect(attachmentFiles().some((f) => f.includes(id))).toBe(true);
});

test("화면 — 승인된 비용은 삭제 대신 승인 요청을 안내한다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  await upload(acc.api, SEED.expenses.open, {
    name: "승인안내 대상.pdf", mimeType: "application/pdf", buffer: PDF,
  });
  const reqRes = await acc.api.post("/api/v1/approvals", {
    data: {
      target_type: "expense", target_id: SEED.expenses.open,
      request_type: "EXPENSE_FINANCIAL_CHANGE",
      proposed_changes: { gross_amount: 140000 }, reason: "E2E", submit: true,
    },
  });
  const requestId = (await reqRes.json()).data.id as string;
  const admin = await apiAs("approver");
  const approved = await admin.post(`/api/v1/approvals/${requestId}/approve`,
    { data: { comment: "E2E 승인" } });
  expect(approved.ok(), await approved.text()).toBeTruthy();
  const appliedRes = await admin.post(`/api/v1/approvals/${requestId}/apply`);
  expect(appliedRes.ok(), await appliedRes.text()).toBeTruthy();
  await admin.dispose();

  await openEvidence(page, SEED.expenses.open);
  await page.getByRole("button", { name: "승인안내 대상.pdf 삭제" }).click();

  await expect(page.getByText("이 증빙을 삭제하려면 승인이 필요합니다.")).toBeVisible();
  await page.getByPlaceholder("예: 잘못 첨부한 다른 건의 영수증").fill("E2E 삭제 사유");
  await page.getByRole("button", { name: "삭제 승인 요청" }).click();
  await expect(page.getByText("증빙 삭제 승인을 요청했습니다")).toBeVisible();

  const reqs = dbQuery<{ target_type: string; request_type: string; status: string }>(
    `SELECT target_type, request_type, status FROM approval_request
      WHERE target_type = 'expense_attachment' AND deleted_at IS NULL`);
  expect(reqs).toHaveLength(1);
  expect(reqs[0].request_type).toBe("EXPENSE_EVIDENCE_CHANGE");
  expect(reqs[0].status).toBe("PENDING");
  // 승인 전이므로 파일은 그대로다
  expect(attachmentsOf(SEED.expenses.open)[0].status).toBe("AVAILABLE");
});

test("화면 — 잠긴 달은 재오픈 안내만 하고 가짜 버튼을 두지 않는다", async ({ page }) => {
  const acc = await loginAs(page, "accountant");
  await upload(acc.api, SEED.expenses.locked, {
    name: "잠긴달 안내.pdf", mimeType: "application/pdf", buffer: PDF,
  });

  await openEvidence(page, SEED.expenses.locked);
  await page.getByRole("button", { name: "잠긴달 안내.pdf 삭제" }).click();

  await expect(page.getByText(/잠긴 월의 증빙은 직접 제거할 수 없습니다/)).toBeVisible();
  await expect(page.getByText(/월 재오픈 승인 기능은 아직 제공되지 않습니다/)).toBeVisible();
  // 월 재오픈은 미구현이다 — 동작하지 않는 버튼을 만들지 않는다
  await expect(page.getByRole("button", { name: /재오픈/ })).toHaveCount(0);
});
