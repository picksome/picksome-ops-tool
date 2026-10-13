/**
 * API wrapper envelope 처리 (D7 회귀).
 *
 * 발견한 결함
 * ---------
 * `GET /distributions/{ym}` 은 아직 확정 배분이 없으면 `{"data": null}` 을 준다.
 * 예전 unwrap 은 `body?.data ?? body` 였고, `??` 는 null 에서 오른쪽으로 넘어간다.
 * 그래서 **envelope 객체 자체**(`{data: null}`)가 호출자에게 갔다.
 *
 * 호출자는 그걸 "값이 있다" 로 읽었고 `run.results.length` 에서 터졌다 —
 * 배분 화면 전체가 Unhandled Runtime Error 로 죽었다.
 *
 * null 을 돌려주는 엔드포인트는 앞으로도 늘어난다. 여기서 고정한다.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";

function respond(payload: unknown, ok = true, status = 200) {
  return {
    ok,
    status,
    text: async () => (payload === undefined ? "" : JSON.stringify(payload)),
  } as unknown as Response;
}

let next: () => Response;

beforeEach(() => {
  next = () => respond({ data: null });
  vi.stubGlobal("fetch", vi.fn(async () => next()));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("envelope unwrap", () => {
  it("data 가 null 이면 null 을 준다 — envelope 객체를 주지 않는다", async () => {
    next = () => respond({ data: null });
    const res = await api.get("/distributions/2026-05");
    expect(res).toBeNull();
  });

  it("data 가 0 이나 false 여도 그대로 준다", async () => {
    next = () => respond({ data: 0 });
    expect(await api.get("/x")).toBe(0);

    next = () => respond({ data: false });
    expect(await api.get("/x")).toBe(false);
  });

  it("객체 응답은 data 만 꺼낸다", async () => {
    next = () => respond({ data: { id: "run-1", results: [] }, meta: { x: 1 } });
    const res = await api.get<{ id: string; results: unknown[] }>("/x");
    expect(res.id).toBe("run-1");
    expect(res.results).toEqual([]);
  });

  it("목록 응답은 meta 를 합쳐 { items, total } 로 준다", async () => {
    next = () => respond({ data: [{ id: 1 }], meta: { total: 7, limit: 50 } });
    const res = await api.get<{ items: unknown[]; total: number }>("/x");
    expect(res.items).toHaveLength(1);
    expect(res.total).toBe(7);
  });

  it("빈 본문(204)은 null 이다", async () => {
    next = () => respond(undefined);
    expect(await api.get("/x")).toBeNull();
  });

  it("envelope 이 아닌 응답은 그대로 준다", async () => {
    next = () => respond({ status: "ok" });
    expect(await api.get<{ status: string }>("/x")).toEqual({ status: "ok" });
  });

  it("오류는 ApiError 로 던진다", async () => {
    next = () => respond({ error: { code: "FORBIDDEN", message: "권한 없음" } }, false, 403);
    await expect(api.get("/x")).rejects.toBeInstanceOf(ApiError);
  });
});
