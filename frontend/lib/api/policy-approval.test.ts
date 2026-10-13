/**
 * 재무 정책 저장 계약 테스트 (Phase A-2/A-3).
 *
 * 왜 이 테스트가 있나
 * ----------------
 * 백엔드가 세율·수수료를 일반 PATCH 로 못 바꾸게 막았다. 화면이 그 목록을 다르게
 * 알고 있으면 두 가지 중 하나가 일어난다.
 *
 * ```
 * 화면이 더 좁게 안다  → 직접 보낸 필드가 403 으로 튕긴다 (사용자는 이유를 모른다)
 * 화면이 더 넓게 안다  → 승인이 필요 없는 값에 승인을 요구한다 (승인 목록이 쓰레기가 된다)
 * ```
 *
 * 두 목록이 같아야 한다. 여기서 고정한다.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  APPROVAL_FIELDS,
  APPROVAL_TARGET,
  requiresApproval,
  savePolicyWithApproval,
} from "./policy-approval";
import { validateTiers, type Tier } from "@/components/settings/tier-editor";

const calls: { url: string; method: string; body?: unknown }[] = [];

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
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify({ data: { id: "req-1" } }),
      } as unknown as Response;
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

const posted = () => calls.filter((c) => c.method === "POST");
const patched = () => calls.filter((c) => c.method === "PATCH");

describe("승인 대상 구분", () => {
  it("세율·수수료율·배분비율은 승인 대상이다", () => {
    expect(requiresApproval("tax", "vat_rate")).toBe(true);
    expect(requiresApproval("marketplace_fee", "rate")).toBe(true);
    expect(requiresApproval("marketplace_fee", "tier_json")).toBe(true);
    expect(requiresApproval("partner_share", "share_ratio")).toBe(true);
  });

  it("메모·근거 링크는 승인 대상이 아니다", () => {
    // 근거 링크를 고치는 데 승인을 요구하면 사람들이 승인 자체를 우회한다.
    expect(requiresApproval("tax", "memo")).toBe(false);
    expect(requiresApproval("marketplace_fee", "source_url")).toBe(false);
  });

  it("금액과 무관한 정책 종류는 승인 대상이 아니다", () => {
    expect(requiresApproval("expense_category", "name")).toBe(false);
    expect(APPROVAL_TARGET.expense_category).toBeUndefined();
  });

  it("승인 대상 종류에는 target/request 유형이 모두 있다", () => {
    // 하나라도 빠지면 저장 시점에 "승인 대상이 아닌 종류" 로 던진다.
    for (const kind of Object.keys(APPROVAL_FIELDS)) {
      expect(APPROVAL_TARGET[kind as keyof typeof APPROVAL_TARGET]).toBeTruthy();
    }
  });
});

describe("저장 분기", () => {
  it("바뀐 승인 대상만 요청으로 나간다", async () => {
    const result = await savePolicyWithApproval({
      kind: "marketplace_fee",
      policyId: "p1",
      current: { rate: 0.108, category_path: "패션" },
      next: { rate: 0.12, category_path: "패션" },
      reason: "쿠팡 수수료 인상",
    });

    expect(result.sentForApproval).toEqual(["rate"]);
    const req = posted()[0];
    expect(req.url).toContain("/approvals");
    expect(req.body).toMatchObject({
      target_type: "policy_marketplace_fee",
      request_type: "FEE_POLICY_CHANGE",
      target_id: "p1",
      submit: true,
    });
    // 안 바뀐 필드까지 태우면 승인 목록이 메모만 고친 요청으로 가득 찬다.
    expect((req.body as { proposed_changes: object }).proposed_changes)
      .toEqual({ rate: 0.12 });
  });

  it("바뀐 게 없으면 아무것도 보내지 않는다", async () => {
    const result = await savePolicyWithApproval({
      kind: "tax", policyId: "p1",
      current: { vat_rate: 0.1 }, next: { vat_rate: 0.1 },
      reason: "변경 없음",
    });
    expect(result.sentForApproval).toEqual([]);
    expect(result.approvalRequestId).toBeNull();
    expect(posted()).toHaveLength(0);
  });

  it("메모만 바꾸면 승인 없이 바로 저장된다", async () => {
    const result = await savePolicyWithApproval({
      kind: "tax", policyId: "p1",
      current: { vat_rate: 0.1 }, next: { vat_rate: 0.1 },
      memo: "세무사 확인 2026-08", reason: "메모 갱신",
    });

    expect(result.approvalRequestId).toBeNull();
    expect(patched()).toHaveLength(1);
    expect(patched()[0].url).toContain("/policies/tax/p1");
  });

  it("구간 목록은 JSON 문자열로 보낸다", async () => {
    // 백엔드가 중첩 값을 거부한다 — 어떤 컬럼이 바뀌는지 정적으로 알기 위해서다.
    const tiers = [
      { min: 0, max: 10_000_000, rate: 0.108 },
      { min: 10_000_001, max: null, rate: 0.098 },
    ];
    await savePolicyWithApproval({
      kind: "marketplace_fee", policyId: "p1",
      current: { tier_json: null }, next: { tier_json: tiers },
      reason: "구간 도입",
    });

    const sent = (posted()[0].body as { proposed_changes: Record<string, unknown> })
      .proposed_changes;
    expect(typeof sent.tier_json).toBe("string");
    expect(JSON.parse(sent.tier_json as string)).toEqual(tiers);
  });
});

/* ────────────────────────────────────────────────────────────
 * 구간 검증 — 백엔드 `validate_tiers` 와 **같은 규칙**이어야 한다.
 * 화면에서 통과했는데 서버가 거절하면 사용자는 이유를 알 수 없다.
 * ──────────────────────────────────────────────────────────── */

describe("구간별 수수료 검증", () => {
  const valid: Tier[] = [
    { min: 0, max: 10_000_000, rate: 0.108 },
    { min: 10_000_001, max: 30_000_000, rate: 0.102 },
    { min: 30_000_001, max: null, rate: 0.098 },
  ];

  it("정상 구간은 통과한다", () => {
    expect(validateTiers(valid)).toEqual([]);
  });

  it("빈 목록은 거부한다", () => {
    expect(validateTiers([])).not.toEqual([]);
  });

  it("0원부터 시작하지 않으면 거부한다", () => {
    // 그 아래 금액의 수수료를 알 수 없다.
    expect(validateTiers([{ min: 100, max: null, rate: 0.1 }])).not.toEqual([]);
  });

  it("구간이 겹치면 거부한다", () => {
    // 같은 판매가에 수수료가 두 개가 된다.
    const overlap: Tier[] = [
      { min: 0, max: 1000, rate: 0.1 },
      { min: 500, max: null, rate: 0.2 },
    ];
    expect(validateTiers(overlap).some((e) => e.includes("겹칩니다"))).toBe(true);
  });

  it("구간 사이가 비면 거부한다", () => {
    const gap: Tier[] = [
      { min: 0, max: 1000, rate: 0.1 },
      { min: 2000, max: null, rate: 0.2 },
    ];
    expect(validateTiers(gap).some((e) => e.includes("비었습니다"))).toBe(true);
  });

  it("경계는 포함이다 — max + 1 에서 이어지면 통과한다", () => {
    // 백엔드와 경계 규칙이 다르면 1원짜리 빈틈이 생긴다.
    const contiguous: Tier[] = [
      { min: 0, max: 1000, rate: 0.1 },
      { min: 1001, max: null, rate: 0.2 },
    ];
    expect(validateTiers(contiguous)).toEqual([]);
  });

  it("마지막 구간에 상한이 있으면 거부한다", () => {
    expect(validateTiers([{ min: 0, max: 1000, rate: 0.1 }])).not.toEqual([]);
  });

  it("퍼센트를 그대로 넣으면 거부한다", () => {
    // 10.8 을 넣으면 수수료가 1080% 가 되고 마진이 크게 음수로 나온다.
    const wrong: Tier[] = [{ min: 0, max: null, rate: 10.8 }];
    expect(validateTiers(wrong).some((e) => e.includes("0.108"))).toBe(true);
  });

  it("음수 수수료율·최소금액을 거부한다", () => {
    expect(validateTiers([{ min: 0, max: null, rate: -0.1 }])).not.toEqual([]);
    expect(validateTiers([{ min: -1, max: null, rate: 0.1 }])).not.toEqual([]);
  });

  it("max <= min 을 거부한다", () => {
    expect(validateTiers([{ min: 100, max: 100, rate: 0.1 }])).not.toEqual([]);
  });
});
