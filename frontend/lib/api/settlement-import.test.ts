/**
 * 정산서 가져오기 클라이언트.
 *
 * 이 파일이 고정하는 것
 * ------------------
 * 1. 실제로 존재하는 endpoint 를 부른다 — 예전 화면은 없는 경로를 부르고 있었다
 * 2. multipart 를 브라우저가 만들게 둔다 (Content-Type 을 우리가 정하면 boundary 가 빠진다)
 * 3. mode 와 판매계정을 **반드시 함께** 보낸다
 * 4. 실패를 성공처럼 반환하지 않는다
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { settlementImportApi } from "./settlement-import";

vi.mock("@/lib/auth", () => ({ getAccessToken: () => "test-token" }));

const ACCOUNT = "11111111-2222-3333-4444-555555555555";

function okResponse(data: unknown) {
  return { ok: true, status: 200, json: async () => ({ data }) };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("settlementImportApi.run", () => {
  it("실제 endpoint 로 multipart 를 보낸다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse({ inserted: 3 }));
    vi.stubGlobal("fetch", fetchMock);

    const file = new File(["x"], "settlement.xlsx");
    await settlementImportApi.run(file, ACCOUNT, "DRY_RUN");

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/v1/settlements/import");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);

    // Content-Type 을 지정하면 boundary 가 빠져 서버가 파싱하지 못한다.
    expect(init.headers).toEqual({ Authorization: "Bearer test-token" });
  });

  it("판매계정과 mode 를 함께 보낸다", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse({}));
    vi.stubGlobal("fetch", fetchMock);

    await settlementImportApi.run(new File(["x"], "s.xlsx"), ACCOUNT, "COMMIT");

    const form = fetchMock.mock.calls[0][1].body as FormData;
    expect(form.get("integration_account_id")).toBe(ACCOUNT);
    expect(form.get("mode")).toBe("COMMIT");
    expect(form.get("file")).toBeInstanceOf(File);
  });

  it("서버가 거절하면 던진다 — 성공처럼 반환하지 않는다", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        json: async () => ({
          error: { code: "VALIDATION_ERROR", message: "정산서 필수 컬럼이 없습니다" },
        }),
      }),
    );
    await expect(
      settlementImportApi.run(new File(["x"], "s.xlsx"), ACCOUNT, "DRY_RUN"),
    ).rejects.toThrow("정산서 필수 컬럼이 없습니다");
  });
});

describe("settlementImportApi.runs", () => {
  it("이력을 읽는다", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(okResponse([{ id: "a", record_count: 34 }]));
    vi.stubGlobal("fetch", fetchMock);

    const runs = await settlementImportApi.runs(5);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/settlements/import/runs?limit=5");
    expect(runs[0].record_count).toBe(34);
  });
});
