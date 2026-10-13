/**
 * 승인 오류 안내 · diff 렌더 데이터 단위 테스트.
 *
 * "실패했습니다" 하나로 뭉치면 사용자는 무엇을 해야 할지 모른다.
 * 코드마다 **다음 행동**이 달라야 하고, 특히 409 계열은 화면을 다시 불러와야 한다.
 */

import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/api";
import { buildDiffRows, describeApprovalError } from "@/lib/api/approvals";

function err(code: string, status = 409, details?: unknown) {
  return new ApiError(code, "서버 메시지", status, details);
}

describe("describeApprovalError", () => {
  it("이미 열린 요청이면 그 요청 id 를 넘겨 이동시킬 수 있게 한다", () => {
    const g = describeApprovalError(
      err("APPROVAL_OPEN_REQUEST_EXISTS", 409, { existing_request_id: "req-1" }),
    );
    expect(g.existingRequestId).toBe("req-1");
    expect(g.shouldRefetch).toBe(true);
  });

  it("상품 version 충돌은 '덮어쓰지 않았다' 를 알리고 다시 불러온다", () => {
    const g = describeApprovalError(err("PRODUCT_VERSION_CONFLICT"));
    expect(g.title).toContain("다른 사용자");
    expect(g.shouldRefetch).toBe(true);
  });

  it("승인 필요는 '값이 잘못됐다' 로 읽히면 안 된다", () => {
    // 403 을 422 처럼 안내하면 사용자가 값을 고치려 든다. 문제는 값이 아니라 경로다.
    const g = describeApprovalError(err("APPROVAL_REQUIRED", 403));
    expect(g.description).toContain("승인");
  });

  it("본인 승인 금지는 다시 불러올 이유가 없다", () => {
    const g = describeApprovalError(err("APPROVAL_SELF_APPROVAL_FORBIDDEN", 403));
    expect(g.shouldRefetch).toBe(false);
  });

  it("월 잠금은 자동 해제를 암시하지 않는다", () => {
    const g = describeApprovalError(err("SETTLEMENT_LOCKED"));
    expect(g.title).toContain("잠긴");
    expect(g.description).not.toContain("자동으로 해제");
  });

  it("executor 중복 실행은 기다리라고 안내한다", () => {
    const g = describeApprovalError(err("APPROVAL_EXECUTOR_ALREADY_RUNNING"));
    expect(g.title).toContain("이미 실행 중");
  });

  it("모르는 코드라도 409 면 다시 불러온다", () => {
    expect(describeApprovalError(err("SOMETHING_NEW", 409)).shouldRefetch).toBe(true);
    expect(describeApprovalError(err("SOMETHING_NEW", 500)).shouldRefetch).toBe(false);
  });

  it("ApiError 가 아니면 네트워크 문제로 안내한다", () => {
    const g = describeApprovalError(new TypeError("Failed to fetch"));
    expect(g.description).toContain("네트워크");
    expect(g.shouldRefetch).toBe(false);
  });
});

describe("buildDiffRows", () => {
  const before = { cost_amount: 10000, name_ko: "이전 이름", memo: "그대로" };

  it("바뀐 필드만 changed 로 표시한다", () => {
    const rows = buildDiffRows("product", before, { name_ko: "새 이름" });
    const name = rows.find((r) => r.field === "name_ko");
    expect(name?.changed).toBe(true);
    expect(name?.before).toBe("이전 이름");
    expect(name?.after).toBe("새 이름");
  });

  it("제안에 없는 필드는 before 를 그대로 after 로 둔다 — 변경 아님", () => {
    const rows = buildDiffRows("product", before, { name_ko: "새 이름" });
    const memo = rows.find((r) => r.field === "memo");
    expect(memo?.changed).toBe(false);
    expect(memo?.after).toBe("그대로");
  });

  it("서버가 값을 지운 민감 필드는 키가 남아 '가려짐' 을 알 수 있다", () => {
    // 서버 redaction 은 값을 null 로 바꾸되 **키는 남긴다.**
    // 키까지 지우면 "변경 없음" 과 "볼 수 없음" 이 구분되지 않는다.
    const rows = buildDiffRows("product", { cost_amount: null }, { cost_amount: null });
    expect(rows.some((r) => r.field === "cost_amount")).toBe(true);
  });

  it("금액 필드는 money 로 분류해 원화 포맷을 태운다", () => {
    const rows = buildDiffRows("product", before, { cost_amount: 20000 });
    const cost = rows.find((r) => r.field === "cost_amount");
    expect(cost?.kind).toBe("money");
    expect(cost?.label).toBe("원가");
  });

  it("서버 관리 필드는 승인 화면에 노출하지 않는다", () => {
    const rows = buildDiffRows("product", { id: "x", updated_at: "t", memo: "m" }, { memo: "n" });
    expect(rows.map((r) => r.field)).toEqual(["memo"]);
  });

  it("status 는 감춰진 필드가 아니다 — 정당한 승인 대상이다", () => {
    const rows = buildDiffRows("product", { status: "ACTIVE" }, { status: "PAUSED" });
    expect(rows.find((r) => r.field === "status")?.changed).toBe(true);
  });
});
