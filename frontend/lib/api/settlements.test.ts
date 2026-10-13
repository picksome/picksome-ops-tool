/**
 * 월 마감 · 지급 API 클라이언트 계약 테스트.
 *
 * 왜 이 테스트가 있나 (D2)
 * ---------------------
 * 이 파일의 이전 버전은 존재하지 않는 `/settlements/monthly/…` 를 호출했다.
 * 백엔드에 그런 라우터가 없어서 월정산 화면의 모든 요청이 404 였고,
 * **타입체크도 린트도 빌드도 그걸 잡지 못했다** — 경로는 그냥 문자열이기 때문이다.
 *
 * 그래서 경로를 테스트로 고정한다. 백엔드 라우터가 바뀌면 이 테스트가 깨져야 한다.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { monthlyCloseApi } from "./settlements";
import { operationsApi } from "./operations";
import { payoutsApi, distributionsApi } from "./payouts";

const calls: { url: string; method: string; body?: unknown }[] = [];

function jsonResponse(data: unknown, meta?: unknown) {
  // api.ts 는 `res.text()` 로 읽고 비면 null 로 본다 (204 대응).
  const payload = meta ? { data, meta } : { data };
  return {
    ok: true,
    status: 200,
    text: async () => JSON.stringify(payload),
  } as unknown as Response;
}

beforeEach(() => {
  calls.length = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        url: String(input),
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      });
      return jsonResponse([], { total: 0 });
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const lastUrl = () => calls[calls.length - 1].url;
const lastCall = () => calls[calls.length - 1];

describe("월 마감 클라이언트가 실제 백엔드 경로를 부른다", () => {
  it("목록은 /monthly-close 다 — /settlements/* 가 아니다", async () => {
    await monthlyCloseApi.list(12);
    expect(lastUrl()).toContain("/monthly-close");
    expect(lastUrl()).not.toContain("/settlements/");
    expect(lastUrl()).toContain("limit=12");
  });

  it("상세·검증·확정·잠금 경로", async () => {
    await monthlyCloseApi.get("2026-03");
    expect(lastUrl()).toContain("/monthly-close/2026-03");

    await monthlyCloseApi.validate("2026-03");
    expect(lastUrl()).toContain("/monthly-close/2026-03/validate");
    expect(lastCall().method).toBe("POST");

    await monthlyCloseApi.lock("2026-03");
    expect(lastUrl()).toContain("/monthly-close/2026-03/lock");
  });

  it("확정은 확인한 경고 코드를 함께 보낸다", async () => {
    await monthlyCloseApi.confirm("2026-03", ["MISSING_EVIDENCE", "LOSS_SALE"]);
    expect(lastUrl()).toContain("/monthly-close/2026-03/confirm");
    expect(lastCall().body).toEqual({
      acknowledged_warnings: ["MISSING_EVIDENCE", "LOSS_SALE"],
    });
  });

  it("재오픈은 요청을 만들 뿐 잠금을 풀지 않는다 (경로가 reopen-request)", async () => {
    await monthlyCloseApi.requestReopen("2026-03", "정산서 지연 도착");
    expect(lastUrl()).toContain("/monthly-close/2026-03/reopen-request");
    expect(lastUrl()).not.toContain("/unlock");
    expect(lastCall().body).toEqual({ reason: "정산서 지연 도착", submit: true });
  });

  it("drift · snapshots · quota 경로", async () => {
    await monthlyCloseApi.drift("2026-03");
    expect(lastUrl()).toContain("/monthly-close/2026-03/drift");

    await monthlyCloseApi.snapshots("2026-03");
    expect(lastUrl()).toContain("/monthly-close/2026-03/snapshots");

    await monthlyCloseApi.reopenQuota("2026-03");
    expect(lastUrl()).toContain("/monthly-close/2026-03/reopen-quota");
  });
});

describe("운영 이슈 클라이언트", () => {
  it("배열 필터를 반복 쿼리 파라미터로 편다", async () => {
    // 쉼표로 이어 붙이면 FastAPI 가 값 하나짜리 리스트로 파싱한다.
    await operationsApi.listIssues({
      severity: ["BLOCKING", "WARNING"],
      open_only: true,
    });
    const url = lastUrl();
    expect(url).toContain("severity=BLOCKING");
    expect(url).toContain("severity=WARNING");
    expect(url).not.toContain("severity=BLOCKING%2CWARNING");
    expect(url).toContain("open_only=true");
  });

  it("빈 배열은 아예 보내지 않는다", async () => {
    await operationsApi.listIssues({ severity: [], status: [] });
    expect(lastUrl()).not.toContain("severity=");
    expect(lastUrl()).not.toContain("status=");
  });

  it("무시 처리는 사유를 함께 보낸다", async () => {
    await operationsApi.updateIssue("abc", {
      status: "IGNORED",
      resolution_note: "증정 건이라 정상",
    });
    expect(lastCall().method).toBe("PATCH");
    expect(lastCall().body).toEqual({
      status: "IGNORED",
      resolution_note: "증정 건이라 정상",
    });
  });
});

describe("지급 장부 클라이언트", () => {
  it("지급 기록은 항목 하위 경로로 간다", async () => {
    await payoutsApi.recordPayment("item-1", {
      payment_date: "2026-03-10",
      amount: 2_000_000,
      payment_reference: "TRX-1",
    });
    expect(lastUrl()).toContain("/payouts/items/item-1/payments");
    expect(lastCall().method).toBe("POST");
    expect(lastCall().body).toMatchObject({ amount: 2_000_000 });
  });

  it("상환 배치 생성은 비용 id 목록을 보낸다", async () => {
    await payoutsApi.createReimbursement({
      expense_ids: ["e1", "e2"],
      title: "3월 상환",
    });
    expect(lastUrl()).toContain("/payouts/reimbursements");
    expect(lastCall().body).toMatchObject({ expense_ids: ["e1", "e2"] });
  });

  it("배분 확정과 미리보기는 다른 경로다", async () => {
    await distributionsApi.preview("2026-03");
    expect(lastUrl()).toContain("/distributions/2026-03/preview");

    await distributionsApi.create("2026-03");
    expect(lastUrl()).toContain("/distributions/2026-03");
    expect(lastCall().method).toBe("POST");
  });
});
