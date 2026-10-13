/**
 * 옵션 순차 저장 — 부분 성공 처리 단위 테스트.
 *
 * 여기서 막는 사고
 *   · 한 건 실패로 나머지를 포기해 사용자가 어디까지 됐는지 모르는 것
 *   · 일부만 성공했는데 전체 성공으로 표시하는 것
 *   · 재시도할 때 이미 성공한 행까지 다시 보내는 것
 */

import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import type { ProductOption } from "@/lib/adapters/types";
import {
  failedRowIds, isSameOption, runSequentialSave, savedRowIds, summarize,
} from "@/lib/adapters/option-save";

function opt(id: string, over: Partial<ProductOption> = {}): ProductOption {
  return {
    id, values: { 색상: "네이비" }, autoName: `옵션 ${id}`, customName: null,
    optionSku: null, optionCost: 10000, extraPrice: 0, stock: 5, isActive: true,
    sourceOptionUrl: null, marketplaceOptionNo: null,
    ...over,
  };
}

const okHandlers = {
  create: vi.fn(async (o: ProductOption) => o),
  update: vi.fn(async (o: ProductOption) => o),
};

describe("isSameOption", () => {
  it("저장 대상 필드가 같으면 같다", () => {
    expect(isSameOption(opt("a"), opt("a"))).toBe(true);
  });

  it("표시용 이름이 달라도 저장 대상이 같으면 같다", () => {
    // 파생값 차이로 불필요한 요청을 보내면 안 바꾼 행의 updated_at 이 갱신된다.
    expect(isSameOption(opt("a"), opt("a", { autoName: "다른 이름" }))).toBe(true);
  });

  it("원가가 다르면 다르다", () => {
    expect(isSameOption(opt("a"), opt("a", { optionCost: 12000 }))).toBe(false);
  });

  it("재고 0 과 null 은 다르다", () => {
    // 0 은 "품절", null 은 "재고 관리 안 함". 같게 보면 한쪽을 저장할 수 없다.
    expect(isSameOption(opt("a", { stock: 0 }), opt("a", { stock: null }))).toBe(false);
  });

  it("비활성 전환은 변경이다", () => {
    expect(isSameOption(opt("a"), opt("a", { isActive: false }))).toBe(false);
  });
});

describe("runSequentialSave — 변경분만 보낸다", () => {
  it("안 바뀐 행은 건너뛴다", async () => {
    const rows = [opt("a"), opt("b")];
    const original = new Map(rows.map((r) => [r.id, r]));
    const handlers = { create: vi.fn(), update: vi.fn() };

    const result = await runSequentialSave(rows, original, handlers as never);

    expect(result.skipped).toBe(2);
    expect(result.attempted).toBe(0);
    expect(handlers.update).not.toHaveBeenCalled();
  });

  it("변경 없음은 '전부 성공' 이 아니다", async () => {
    const rows = [opt("a")];
    const result = await runSequentialSave(
      rows, new Map(rows.map((r) => [r.id, r])), okHandlers,
    );
    // true 를 돌려주면 화면이 성공 토스트를 띄운다. 저장한 게 없는데.
    expect(result.allSucceeded).toBe(false);
    expect(summarize(result).title).toContain("변경된 옵션이 없습니다");
  });

  it("원본에 없는 행은 생성으로 보낸다", async () => {
    const create = vi.fn(async (o: ProductOption) => o);
    const update = vi.fn(async (o: ProductOption) => o);
    await runSequentialSave([opt("new")], new Map(), { create, update });

    expect(create).toHaveBeenCalledTimes(1);
    expect(update).not.toHaveBeenCalled();
  });
});

describe("runSequentialSave — 부분 성공", () => {
  it("한 건 실패해도 나머지를 계속 시도한다", async () => {
    const rows = [opt("a"), opt("b"), opt("c")];
    const original = new Map(rows.map((r) => [r.id, opt(r.id, { optionCost: 1 })]));

    const update = vi.fn(async (o: ProductOption) => {
      if (o.id === "b") throw new ApiError("VALIDATION_ERROR", "값이 잘못됨", 422);
      return o;
    });

    const result = await runSequentialSave(rows, original, { create: vi.fn(), update });

    // 중단하면 c 가 시도되지 않는다 — 사용자는 다시 눌러야 하고,
    // 그러면 이미 성공한 a 까지 또 보낸다.
    expect(update).toHaveBeenCalledTimes(3);
    expect(result.succeeded).toBe(2);
    expect(result.failed).toBe(1);
    expect(result.allSucceeded).toBe(false);
  });

  it("실패 행의 오류 코드와 메시지를 남긴다", async () => {
    const rows = [opt("a")];
    const original = new Map([["a", opt("a", { optionCost: 1 })]]);
    const update = vi.fn(async () => {
      throw new ApiError("VARIANT_IN_USE", "판매 이력이 있습니다", 409);
    });

    const result = await runSequentialSave(rows, original, { create: vi.fn(), update });
    const outcome = result.outcomes[0];

    expect(outcome.state).toBe("CONFLICT");
    expect(outcome.errorCode).toBe("VARIANT_IN_USE");
    expect(outcome.errorMessage).toBe("판매 이력이 있습니다");
    // 409 는 화면이 낡았다는 뜻이라 최신값을 받아야 한다.
    expect(outcome.needsRefresh).toBe(true);
  });

  it("409 가 아닌 실패는 새로고침을 요구하지 않는다", async () => {
    const rows = [opt("a")];
    const original = new Map([["a", opt("a", { optionCost: 1 })]]);
    const update = vi.fn(async () => {
      throw new ApiError("VALIDATION_ERROR", "값이 잘못됨", 422);
    });

    const result = await runSequentialSave(rows, original, { create: vi.fn(), update });
    expect(result.outcomes[0].state).toBe("FAILED");
    expect(result.outcomes[0].needsRefresh).toBe(false);
  });

  it("네트워크 오류도 행 단위로 기록한다", async () => {
    const rows = [opt("a")];
    const original = new Map([["a", opt("a", { optionCost: 1 })]]);
    const update = vi.fn(async () => { throw new TypeError("Failed to fetch"); });

    const result = await runSequentialSave(rows, original, { create: vi.fn(), update });
    expect(result.outcomes[0].state).toBe("FAILED");
    expect(result.outcomes[0].errorMessage).toContain("네트워크");
  });

  it("진행 상황을 행마다 알린다", async () => {
    const rows = [opt("a"), opt("b")];
    const original = new Map(rows.map((r) => [r.id, opt(r.id, { optionCost: 1 })]));
    const seen: string[] = [];

    await runSequentialSave(rows, original, okHandlers,
      (o) => seen.push(`${o.optionId}:${o.state}`));

    // 각 행이 SAVING → SAVED 로 두 번 보고된다. 진행률을 그리려면 필요하다.
    expect(seen).toEqual(["a:SAVING", "a:SAVED", "b:SAVING", "b:SAVED"]);
  });
});

describe("재시도 대상 선별", () => {
  it("실패·충돌 행만 재시도 대상이다", async () => {
    const rows = [opt("a"), opt("b"), opt("c")];
    const original = new Map(rows.map((r) => [r.id, opt(r.id, { optionCost: 1 })]));
    const update = vi.fn(async (o: ProductOption) => {
      if (o.id !== "a") throw new ApiError("X", "실패", 422);
      return o;
    });

    const result = await runSequentialSave(rows, original, { create: vi.fn(), update });

    // 성공한 a 를 다시 보내면 updated_at 이 또 갱신되어 이력이 지저분해진다.
    expect([...failedRowIds(result)].sort()).toEqual(["b", "c"]);
    expect([...savedRowIds(result)]).toEqual(["a"]);
  });
});

describe("summarize — 일부 성공을 전체 성공으로 말하지 않는다", () => {
  it("전부 성공이면 success", async () => {
    const rows = [opt("a")];
    const original = new Map([["a", opt("a", { optionCost: 1 })]]);
    const result = await runSequentialSave(rows, original, okHandlers);

    const s = summarize(result);
    expect(s.tone).toBe("success");
    expect(s.title).toContain("1건을 저장했습니다");
  });

  it("일부 실패면 warning 이고 건수를 정확히 말한다", async () => {
    const rows = [opt("a"), opt("b")];
    const original = new Map(rows.map((r) => [r.id, opt(r.id, { optionCost: 1 })]));
    const update = vi.fn(async (o: ProductOption) => {
      if (o.id === "b") throw new ApiError("X", "실패", 422);
      return o;
    });
    const result = await runSequentialSave(rows, original, { create: vi.fn(), update });

    const s = summarize(result);
    expect(s.tone).toBe("warning");
    expect(s.title).toContain("2건 중 1건만");
    expect(s.description).toContain("입력값이 유지");
  });

  it("전부 실패도 전체 성공이 아니다", async () => {
    const rows = [opt("a")];
    const original = new Map([["a", opt("a", { optionCost: 1 })]]);
    const update = vi.fn(async () => { throw new ApiError("X", "실패", 500); });
    const result = await runSequentialSave(rows, original, { create: vi.fn(), update });

    expect(result.allSucceeded).toBe(false);
    expect(summarize(result).tone).toBe("warning");
  });
});
