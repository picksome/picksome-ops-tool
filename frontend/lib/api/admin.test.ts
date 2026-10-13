/**
 * 관리자 API 클라이언트 계약 테스트 (D11 회귀).
 *
 * 발견한 결함
 * ---------
 * 이전 클라이언트는 **존재하지 않는 경로**를 불렀다.
 *
 * ```
 * 화면                              실제 라우터
 * PATCH /admin/users/{id}/role       PATCH  /admin/users/{id}
 * PATCH /admin/users/{id}/deactivate DELETE /admin/users/{id}
 * PATCH /admin/users/{id}/activate   PATCH  /admin/users/{id} {is_active:true}
 * ?page=&page_size=                  ?limit=&offset=
 * ```
 *
 * 이 클라이언트를 쓰는 화면이 없어서 아무도 몰랐다(D6·D2 와 같은 종류).
 * 구성원 화면을 실제 API 로 배선하면서 드러났다.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ROLE_LABELS, adminApi, usersApi } from "./admin";
import type { UserRole } from "@/stores/auth";

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
        text: async () => JSON.stringify({ data: [], meta: { total: 0 } }),
      } as unknown as Response;
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

const last = () => calls[calls.length - 1];

/** 백엔드 `core/rbac.ROLES` 와 **정확히** 같아야 한다. */
const BACKEND_ROLES: UserRole[] = [
  "ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY", "EXTERNAL_ACCOUNTANT",
];

describe("역할 목록이 백엔드와 같다", () => {
  it("6개 역할에 전부 한글 라벨이 있다", () => {
    for (const r of BACKEND_ROLES) {
      expect(ROLE_LABELS[r], r).toBeTruthy();
    }
    expect(Object.keys(ROLE_LABELS).sort()).toEqual([...BACKEND_ROLES].sort());
  });

  it("존재하지 않는 역할이 섞여 있지 않다", () => {
    // 예전 구성원 화면은 OWNER / PRODUCT_MANAGER 같은 가짜 역할을 보여줬다.
    for (const fake of ["OWNER", "PRODUCT_MANAGER", "SETTLEMENT_MANAGER", "VIEWER"]) {
      expect(Object.keys(ROLE_LABELS)).not.toContain(fake);
    }
  });
});

describe("사용자 관리 경로", () => {
  it("목록은 limit/offset 을 보낸다 — page/page_size 가 아니다", async () => {
    await adminApi.users.list({ limit: 200, offset: 0 });
    expect(last().url).toContain("/admin/users");
    expect(last().url).toContain("limit=200");
    expect(last().url).not.toContain("page_size");
  });

  it("역할 변경은 PATCH /admin/users/{id} 다 — /role 하위 경로가 아니다", async () => {
    await adminApi.users.update("u-1", { role: "ACCOUNTING" });
    expect(last().method).toBe("PATCH");
    expect(last().url).toMatch(/\/admin\/users\/u-1$/);
    expect(last().url).not.toContain("/role");
    expect(last().body).toMatchObject({ role: "ACCOUNTING" });
  });

  it("비활성화는 DELETE 다 — 행을 지우지 않고 서버가 비활성 처리한다", async () => {
    await adminApi.users.deactivate("u-1");
    expect(last().method).toBe("DELETE");
    expect(last().url).toMatch(/\/admin\/users\/u-1$/);
    expect(last().url).not.toContain("deactivate");
  });

  it("활성화는 PATCH + is_active 다", async () => {
    await adminApi.users.activate("u-1");
    expect(last().method).toBe("PATCH");
    expect(last().body).toMatchObject({ is_active: true });
    expect(last().url).not.toContain("activate");
  });
});

describe("사용자 이름 조회", () => {
  it("모든 인증 사용자가 부르는 경로는 /users/directory 다", async () => {
    await usersApi.directory();
    expect(last().url).toContain("/users/directory");
    // ADMIN 전용 경로로 새어 나가면 OPERATOR 가 승인함에서 403 을 받는다.
    expect(last().url).not.toContain("/admin/users");
  });
});
