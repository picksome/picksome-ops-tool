/**
 * 상품 drawer — 저장 경로 분기 통합 동작.
 *
 * 순수 함수 테스트(`lib/api/products.test.ts`)는 분류 규칙이 맞는지만 본다.
 * 여기서는 **drawer 가 그 규칙을 실제로 쓰는지**를 본다. 분류는 정확한데
 * 저장 버튼이 그걸 무시하고 전부 PATCH 로 보내면 순수 함수 테스트는 통과한다.
 */

import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { ProductDetail } from "@/lib/adapters/types";

const saveMock = vi.fn();
const detailMock = vi.fn();
const ownershipMock = vi.fn();

vi.mock("@/lib/adapters", async () => {
  const actual = await vi.importActual<typeof import("@/lib/adapters")>("@/lib/adapters");
  return {
    ...actual,
    productAdapter: {
      detail: (id: string) => detailMock(id),
      save: (...args: unknown[]) => saveMock(...args),
      fieldOwnership: () => ownershipMock(),
      options: async () => [],
      optionGroups: async () => [],
    },
  };
});

// 정책·구성원은 이제 실제 API 를 읽는다. 예전에는 `settingsAdapter` mock 이었고,
// 그 mock 이 남아 있으면 컴포넌트가 더 이상 쓰지 않는 계약을 테스트하게 된다.
vi.mock("@/hooks/usePolicies", () => ({
  useTaxRules: () => ({ taxRules: [], isLoading: false }),
  useFeePolicies: () => ({ feePolicies: [], unverifiedVatCount: 0, isLoading: false }),
}));

vi.mock("@/lib/api/admin", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/admin")>("@/lib/api/admin");
  return { ...actual, usersApi: { directory: async () => [] } };
});

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

import { ProductDrawer } from "@/components/products/product-drawer";

const OWNERSHIP = {
  request_types: {
    PRODUCT_INFO_CHANGE: ["name_ko", "supply_url"],
    COST_CHANGE: ["cost_amount", "cost_currency", "cost_fx_rate", "cost_amount_foreign"],
    STATUS_CHANGE: ["status", "margin_type"],
  },
  approval_fields: [
    "name_ko", "supply_url", "cost_amount", "cost_currency",
    "cost_fx_rate", "cost_amount_foreign", "status", "margin_type",
  ],
  sensitive_fields: ["cost_amount"],
};

function makeDetail(over: Partial<ProductDetail> = {}): ProductDetail {
  return {
    id: "p-1", businessKey: "PROD-000001", name: "테스트 상품", sku: "PS-1",
    imageUrl: null, status: "ACTIVE", marginType: "GENERAL_RESELL", category: "생활",
    marketplaces: [], marketplaceProductNos: {}, sourceUrl: null, supplierName: null,
    costEffectiveFrom: null, priceEffectiveFrom: null, totalCost: 10000, salePrice: null,
    expectedSettlement: null, expectedNetProfit: null, marginRate: null, optionCount: 0,
    ownerId: null, ownerName: null, memo: "기존 메모",
    createdAt: "2026-01-01T00:00:00Z", updatedAt: "2026-01-02T00:00:00Z",
    versionToken: "2026-01-02T00:00:00Z",
    openApprovals: [],
    unsupportedFields: [],
    sourcing: {
      sourceUrl: null, supplierName: null, purchaseCurrency: "KRW", purchasePrice: 10000,
      exchangeRate: 1, domesticShippingAbroad: 0, internationalShipping: 0, customsDuty: 0,
      importVat: 0, domesticShipping: 0, packagingCost: 0, otherCost: 0,
      costEffectiveFrom: null, sourceCheckedAt: null,
    },
    pricing: {
      salePrice: 0, buyerShippingFee: 0, priceEffectiveFrom: null, commissionRate: 0,
      adCostRate: 0, returnRate: 0, fixedCostAllocation: 0, targetMarginRate: 0,
    },
    channels: [], options: [], history: [],
    ...over,
  };
}

function renderDrawer() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <ProductDrawer productId="p-1" open onOpenChange={() => {}} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  detailMock.mockResolvedValue(makeDetail());
  ownershipMock.mockResolvedValue(OWNERSHIP);
  saveMock.mockResolvedValue({ detail: makeDetail(), approvals: [], directSaved: true });
});

describe("ProductDrawer 저장 경로", () => {
  it("실제 상세 API 를 부른다 — mock 상수를 읽지 않는다", async () => {
    renderDrawer();
    await waitFor(() => expect(detailMock).toHaveBeenCalledWith("p-1"));
    expect(await screen.findByDisplayValue("테스트 상품")).toBeInTheDocument();
  });

  it("승인 대상이 아닌 변경은 확인 창 없이 바로 저장한다", async () => {
    const user = userEvent.setup();
    renderDrawer();
    const memo = await screen.findByDisplayValue("기존 메모");

    await user.clear(memo);
    await user.type(memo, "새 메모");
    await user.click(screen.getByRole("button", { name: "저장" }));

    await waitFor(() => expect(saveMock).toHaveBeenCalled());
    // 승인 확인 창이 뜨면 안 된다 — 메모까지 승인을 태우면 승인함이 잡음으로 찬다.
    expect(screen.queryByText("승인 요청을 올릴까요?")).not.toBeInTheDocument();
  });

  it("승인 대상 변경이면 버튼 문구가 '승인 요청' 으로 바뀐다", async () => {
    const user = userEvent.setup();
    renderDrawer();
    const name = await screen.findByDisplayValue("테스트 상품");

    await user.clear(name);
    await user.type(name, "바뀐 이름");

    // 저장을 누르기 **전에** 알아야 한다. 누르고 나서 알면 이미 늦다.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "승인 요청" })).toBeInTheDocument(),
    );
  });

  it("승인 대상 변경은 확인 창을 거쳐야 저장된다", async () => {
    const user = userEvent.setup();
    renderDrawer();
    const name = await screen.findByDisplayValue("테스트 상품");

    await user.clear(name);
    await user.type(name, "바뀐 이름");
    await user.click(await screen.findByRole("button", { name: "승인 요청" }));

    expect(await screen.findByText("승인 요청을 올릴까요?")).toBeInTheDocument();
    // 아직 저장은 안 됐다 — 확인 창을 띄우기만 했다.
    expect(saveMock).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "승인 요청 올리기" }));
    await waitFor(() => expect(saveMock).toHaveBeenCalled());
  });

  it("확인 창이 어떤 항목이 승인으로 가는지 이름으로 보여준다", async () => {
    const user = userEvent.setup();
    renderDrawer();
    const name = await screen.findByDisplayValue("테스트 상품");

    await user.clear(name);
    await user.type(name, "바뀐 이름");
    await user.click(await screen.findByRole("button", { name: "승인 요청" }));

    expect(await screen.findByText("상품 정보 변경")).toBeInTheDocument();
    // 필드명(name_ko)이 아니라 한글 라벨이어야 한다.
    expect(screen.getByText("· 상품명")).toBeInTheDocument();
  });

  it("저장 시 원본과 필드 소유권 표를 함께 넘긴다", async () => {
    const user = userEvent.setup();
    renderDrawer();
    const memo = await screen.findByDisplayValue("기존 메모");

    await user.clear(memo);
    await user.type(memo, "새 메모");
    await user.click(screen.getByRole("button", { name: "저장" }));

    await waitFor(() => expect(saveMock).toHaveBeenCalled());
    const opts = saveMock.mock.calls[0][2];
    // base 가 없으면 diff 를 못 만들어 안 바뀐 필드까지 전송된다.
    expect(opts.base?.versionToken).toBe("2026-01-02T00:00:00Z");
    expect(opts.ownership).toEqual(OWNERSHIP);
  });

  it("진행 중 승인 요청이 있으면 미리 알린다", async () => {
    detailMock.mockResolvedValue(
      makeDetail({
        openApprovals: [{
          id: "req-9", businessKey: "APR-000009", requestType: "COST_CHANGE",
          status: "PENDING", requestedAt: "2026-01-03T00:00:00Z", effectiveFrom: null,
        }],
      }),
    );
    renderDrawer();
    // 저장을 눌러 409 를 맞고 나서 아는 게 아니라, 열자마자 보여야 한다.
    expect(await screen.findByText("진행 중인 승인 요청 1건")).toBeInTheDocument();
    expect(screen.getByText("APR-000009")).toBeInTheDocument();
  });

  it("변경이 없으면 저장 버튼이 비활성이다", async () => {
    renderDrawer();
    await screen.findByDisplayValue("테스트 상품");
    expect(screen.getByRole("button", { name: "저장" })).toBeDisabled();
  });
});

describe("승인 정책 로딩 실패 — fail-closed", () => {
  beforeEach(() => {
    ownershipMock.mockRejectedValue(new Error("네트워크 오류"));
  });

  it("실패를 사용자에게 알린다", async () => {
    renderDrawer();
    expect(await screen.findByText("승인 정책을 불러오지 못했습니다.")).toBeInTheDocument();
  });

  it("저장 버튼을 비활성화한다", async () => {
    const user = userEvent.setup();
    renderDrawer();
    await screen.findByText("승인 정책을 불러오지 못했습니다.");

    const memo = screen.getByDisplayValue("기존 메모");
    await user.clear(memo);
    await user.type(memo, "정책 없이 저장 시도");

    // 변경이 있어도 저장할 수 없다. 열어두면 화면이 "승인 대상이 아니다" 라고
    // 추정해서 원가까지 직접 저장으로 보낸다.
    expect(screen.getByRole("button", { name: "저장" })).toBeDisabled();
  });

  it("입력한 내용을 지우지 않는다", async () => {
    const user = userEvent.setup();
    renderDrawer();
    await screen.findByText("승인 정책을 불러오지 못했습니다.");

    const memo = screen.getByDisplayValue("기존 메모");
    await user.clear(memo);
    await user.type(memo, "지켜져야 하는 입력");

    // 저장을 막는다고 사용자가 쓴 걸 날리면 두 번 일하게 된다.
    expect(screen.getByDisplayValue("지켜져야 하는 입력")).toBeInTheDocument();
  });

  it("저장 API 를 부르지 않는다", async () => {
    const user = userEvent.setup();
    renderDrawer();
    await screen.findByText("승인 정책을 불러오지 못했습니다.");

    const memo = screen.getByDisplayValue("기존 메모");
    await user.clear(memo);
    await user.type(memo, "시도");
    // 버튼이 비활성이라 클릭해도 아무 일도 없어야 한다.
    await user.click(screen.getByRole("button", { name: "저장" }));

    expect(saveMock).not.toHaveBeenCalled();
  });

  it("다시 불러오기가 성공하면 저장이 풀리고 분류가 재실행된다", async () => {
    const user = userEvent.setup();
    renderDrawer();
    await screen.findByText("승인 정책을 불러오지 못했습니다.");

    // 이번엔 성공하도록 바꾼다.
    ownershipMock.mockResolvedValue(OWNERSHIP);
    await user.click(screen.getByRole("button", { name: "다시 불러오기" }));

    await waitFor(() =>
      expect(screen.queryByText("승인 정책을 불러오지 못했습니다.")).not.toBeInTheDocument(),
    );

    // 분류가 다시 돌아 승인 대상 변경을 알아본다.
    const name = screen.getByDisplayValue("테스트 상품");
    await user.clear(name);
    await user.type(name, "정책 복구 후 이름");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "승인 요청" })).toBeInTheDocument(),
    );
  });
});

describe("어댑터 계약", () => {
  it("소유권 표 없이 save 를 부르면 거부한다", async () => {
    // 화면 버튼 비활성은 1차 방어다. 어댑터가 2차 방어를 해야
    // 다른 호출부가 생겨도 같은 사고가 안 난다.
    const { productAdapter } = await vi.importActual<typeof import("@/lib/adapters")>(
      "@/lib/adapters",
    );
    await expect(
      productAdapter.save("p-1", { memo: "x" }, { base: makeDetail(), ownership: null }),
    ).rejects.toThrow(/승인 정책/);
  });
});
