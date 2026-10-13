/**
 * 비용 승인 분류 · 잠긴 월 안내 · 개인카드 상환 단위 테스트.
 *
 * 여기서 막는 사고
 *   · 금액 변경이 승인 없이 직접 저장으로 가는 것
 *   · "수정할 수 없습니다" 로만 끝나서 사용자가 뭘 해야 할지 모르는 것
 *   · 비용 승인과 상환 완료가 한 상태로 뭉쳐 승인=지급이 되는 것
 */

import { describe, expect, it } from "vitest";
import { classifyProductChanges, type FieldOwnership } from "@/lib/api/products";
import {
  EXPENSE_FIELD_LABELS, EXPENSE_REQUEST_TYPE_LABELS,
  lockGuidance, reimbursementOf, ADJUSTMENT_TYPE_LABEL,
  type ExpenseLockState,
} from "@/lib/api/expenses-approval";

/** 서버 `/approvals/field-ownership` 의 expense 항목과 같은 모양. */
const EXPENSE_OWNERSHIP: FieldOwnership = {
  request_types: {
    EXPENSE_FINANCIAL_CHANGE: [
      "gross_amount", "supply_amount", "vat_amount", "expense_date",
      "category_id", "payment_method", "payment_card_holder_id",
      "is_personal_card", "is_tax_deductible", "is_settlement_deductible",
      "attributable_partner_id", "supplier_id", "description",
    ],
    EXPENSE_EVIDENCE_CHANGE: ["evidence_type", "evidence_status"],
  },
  approval_fields: [
    "gross_amount", "supply_amount", "vat_amount", "expense_date",
    "category_id", "payment_method", "payment_card_holder_id",
    "is_personal_card", "is_tax_deductible", "is_settlement_deductible",
    "attributable_partner_id", "supplier_id", "description",
    "evidence_type", "evidence_status",
  ],
  sensitive_fields: [
    "gross_amount", "supply_amount", "vat_amount",
    "payment_card_holder_id", "is_personal_card", "payment_method",
  ],
};

describe("비용 변경 분류 — 상품과 같은 규칙을 쓴다", () => {
  it("금액은 절대 직접 저장으로 가지 않는다", () => {
    const r = classifyProductChanges({ gross_amount: 185000 }, EXPENSE_OWNERSHIP);
    expect(r.direct).toEqual({});
    expect(r.approval).toEqual({
      EXPENSE_FINANCIAL_CHANGE: { gross_amount: 185000 },
    });
  });

  it("메모는 직접 저장이다", () => {
    // 운영 영향이 없는 값까지 승인을 태우면 승인함이 잡음으로 찬다.
    const r = classifyProductChanges({ memo: "영수증 재발급 요청함" }, EXPENSE_OWNERSHIP);
    expect(r.direct).toEqual({ memo: "영수증 재발급 요청함" });
    expect(r.hasApproval).toBe(false);
  });

  it("증빙 변경은 별도 요청 유형으로 간다", () => {
    const r = classifyProductChanges(
      { evidence_status: "NONE", gross_amount: 1000 }, EXPENSE_OWNERSHIP,
    );
    // 두 유형이 각각 자기 요청으로 나뉜다 — 한 요청에 섞으면 소유권이 깨진다.
    expect(Object.keys(r.approval).sort()).toEqual([
      "EXPENSE_EVIDENCE_CHANGE", "EXPENSE_FINANCIAL_CHANGE",
    ]);
  });

  it("귀속일 변경도 승인 대상이다", () => {
    // 날짜가 바뀌면 두 달의 순이익이 모두 바뀐다.
    const r = classifyProductChanges({ expense_date: "2026-09-01" }, EXPENSE_OWNERSHIP);
    expect(r.approval.EXPENSE_FINANCIAL_CHANGE).toEqual({ expense_date: "2026-09-01" });
  });

  it("금액 0 을 falsy 로 빠뜨리지 않는다", () => {
    const r = classifyProductChanges({ gross_amount: 0 }, EXPENSE_OWNERSHIP);
    expect(r.approval.EXPENSE_FINANCIAL_CHANGE).toEqual({ gross_amount: 0 });
  });

  it("false 로 바꾸는 공제 해제도 변경이다", () => {
    const r = classifyProductChanges({ is_tax_deductible: false }, EXPENSE_OWNERSHIP);
    expect(r.approval.EXPENSE_FINANCIAL_CHANGE).toEqual({ is_tax_deductible: false });
  });
});

describe("한글 라벨 — JSON 을 그대로 보여주지 않는다", () => {
  it("승인 대상 필드에 라벨이 있다", () => {
    for (const field of EXPENSE_OWNERSHIP.approval_fields) {
      expect(EXPENSE_FIELD_LABELS[field], `${field} 라벨 없음`).toBeTruthy();
    }
  });

  it("요청 유형에도 라벨이 있다", () => {
    for (const rt of Object.keys(EXPENSE_OWNERSHIP.request_types)) {
      expect(EXPENSE_REQUEST_TYPE_LABELS[rt]).toBeTruthy();
    }
  });

  it("조정 유형 4종에 라벨이 있다", () => {
    for (const t of ["LATE_ENTRY", "CORRECTION", "REVERSAL", "RECLASSIFICATION"] as const) {
      expect(ADJUSTMENT_TYPE_LABEL[t]).toBeTruthy();
    }
  });
});

describe("잠긴 월 안내", () => {
  function state(over: Partial<ExpenseLockState> = {}): ExpenseLockState {
    return {
      expense_id: "e1", year_month: "2026-07", close_state: "LOCKED",
      requires_adjustment: true, ...over,
    };
  }

  it("열린 달이면 안내가 없다", () => {
    expect(lockGuidance(state({ close_state: "OPEN", requires_adjustment: false })))
      .toBeNull();
  });

  it("잠긴 달이면 무엇을 해야 하는지 알려준다", () => {
    const msg = lockGuidance(state())!;
    // "수정할 수 없습니다" 로 끝나면 사용자는 그래서 뭘 하냐고 묻는다.
    expect(msg).toContain("2026년 7월");
    expect(msg).toContain("정정 조정");
  });

  it("확정 상태와 잠금 상태의 표현이 다르다", () => {
    expect(lockGuidance(state({ close_state: "LOCKED" }))).toContain("잠겨");
    expect(lockGuidance(state({ close_state: "CONFIRMED" }))).toContain("확정되어");
  });

  it("월 표기에 0 을 붙이지 않는다", () => {
    // "2026년 07월" 은 어색하다.
    expect(lockGuidance(state({ year_month: "2026-07" }))).toContain("7월");
  });
});

describe("개인카드 상환 — 비용 승인과 별개다", () => {
  it("법인카드면 상환이 필요 없다", () => {
    const r = reimbursementOf({
      is_personal_card: false, gross_amount: 120000, payment_card_holder_id: "u1",
    });
    expect(r.status).toBe("NOT_REQUIRED");
    expect(r.amount).toBeNull();
  });

  it("개인카드면 상환 대상자와 금액을 낸다", () => {
    const r = reimbursementOf({
      is_personal_card: true, gross_amount: 120000, payment_card_holder_id: "u1",
    });
    expect(r.status).toBe("PENDING");
    expect(r.amount).toBe(120000);
    expect(r.payeeId).toBe("u1");
  });

  it("지급 장부가 없으므로 PAID 를 만들지 않는다", () => {
    // 지급됐다고 표시할 근거가 없는데 PAID 를 내면 거짓말이 된다.
    const r = reimbursementOf({
      is_personal_card: true, gross_amount: 1, payment_card_holder_id: null,
    });
    expect(["NOT_REQUIRED", "PENDING"]).toContain(r.status);
  });

  it("비용 승인 상태를 상환 상태로 쓰지 않는다", () => {
    // 승인 완료가 곧 지급 완료가 되면, 승인만 하고 돈은 안 준 상태를
    // 시스템이 "지급됨" 으로 본다.
    const r = reimbursementOf({
      is_personal_card: true, gross_amount: 50000, payment_card_holder_id: "u2",
    });
    expect(r.status).not.toBe("APPROVED");
    expect(r.status).not.toBe("PAID");
  });
});
