/**
 * 외부 시스템 연동 클라이언트 계약 테스트.
 *
 * 여기서 특히 지키는 것
 * ------------------
 * 1. **비밀이 응답 타입에 없다** — 화면이 실수로 렌더할 수 없어야 한다
 * 2. 카탈로그에 있는 것과 연동되는 것을 구분한다
 * 3. 미지원(UNSUPPORTED)과 실패(FAILED)를 구분한다
 * 4. 아직 안 끝난 실행의 건수는 `null` 이다 — 0 과 다르다
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  SYNC_STATUS_LABEL, TEST_STATUS_LABEL, externalApi, supportedCapabilities,
  type ExternalAccount, type SyncRun,
} from "./external";

const calls: { url: string; method: string; body?: unknown }[] = [];
let nextPayload: unknown = { data: [], meta: { total: 0 } };

beforeEach(() => {
  calls.length = 0;
  nextPayload = { data: [], meta: { total: 0 } };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        url: String(input),
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      });
      return {
        ok: true, status: 200,
        text: async () => JSON.stringify(nextPayload),
      } as unknown as Response;
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

const lastUrl = () => calls[calls.length - 1].url;
const lastCall = () => calls[calls.length - 1];

describe("경로", () => {
  it("외부 시스템·계정·매핑·동기화 경로", async () => {
    await externalApi.systems();
    expect(lastUrl()).toContain("/external/systems");

    await externalApi.accounts();
    expect(lastUrl()).toContain("/external/accounts");

    await externalApi.mappings({ internal_entity_id: "p1" });
    expect(lastUrl()).toContain("/external/mappings");
    expect(lastUrl()).toContain("internal_entity_id=p1");

    await externalApi.runs({ limit: 5 });
    expect(lastUrl()).toContain("/external/sync-runs");
  });

  it("판매처 연결 경로로 새지 않는다", async () => {
    // `/integrations/marketplaces` 는 다른 개념이다 — 주문이 귀속되는 판매자 계정.
    await externalApi.accounts();
    expect(lastUrl()).not.toContain("/integration-accounts");
    expect(lastUrl()).not.toContain("/marketplaces");
  });

  it("동기화는 계정 하위 POST 다", async () => {
    nextPayload = { data: { run_id: "r1", status: "SUCCESS" } };
    await externalApi.syncProducts("acc1");
    expect(lastCall().method).toBe("POST");
    expect(lastUrl()).toContain("/external/accounts/acc1/sync/products");
  });

  it("재연결은 사유를 함께 보낸다", async () => {
    // 연결을 옮기면 그 뒤의 재고·원가가 다른 상품으로 들어간다.
    nextPayload = { data: { id: "m1" } };
    await externalApi.remap("m1", "p2", "상품 통합");
    expect(lastCall().body).toEqual({ internal_entity_id: "p2", reason: "상품 통합" });
  });
});

describe("비밀 노출 금지", () => {
  it("계정 응답에 자격증명 필드가 없다", async () => {
    const account: ExternalAccount = {
      id: "a1", account_code: "MAIN", display_name: "본사",
      external_account_identifier: "CORP-1", status: "CONNECTED",
      last_connected_at: null, last_successful_sync_at: null,
      credential_configured: true, vault_note_linked: false,
      credential_kind: "API_KEY", credential_updated_at: "2026-08-10T00:00:00Z",
      credential_updated_by: "u1",
      last_test_status: "OK", last_test_at: null, last_test_message: null,
      connector_available: false, capabilities: {}, mapping_count: 0,
      system_code: "NEXT_ENGINE", system_name: "NEXT ENGINE", system_type: "SELLER_TOOL",
    };
    nextPayload = { data: account };

    const res = await externalApi.getAccount("a1");
    // 설정 여부만 안다.
    expect(res.credential_configured).toBe(true);
    for (const banned of ["secret", "password", "access_token", "api_key",
                          "credentials_vault_entry_id", "credential_secret_ref",
                          "ciphertext"]) {
      expect(Object.keys(res)).not.toContain(banned);
    }
  });

  it("비밀은 자격증명 endpoint 로만 나간다", async () => {
    // 계정 생성·수정에 섞으면 일반 요청이 전부 비밀 취급 대상이 된다.
    nextPayload = { data: { id: "a1" } };
    await externalApi.setCredential("a1", {
      credential_kind: "API_KEY", secret: { api_key: "TEST_SECRET" },
    });
    const call = lastCall();
    expect(call.method).toBe("PUT");
    expect(call.url).toContain("/external/accounts/a1/credential");
    expect(Object.keys(call.body as object).sort())
      .toEqual(["credential_kind", "secret"]);
  });

  it("보관함 쪽지는 자격증명과 다른 경로다", async () => {
    // 쪽지에는 값이 없다 — 이걸로 무인 동기화가 되지 않는다.
    nextPayload = { data: { id: "a1" } };
    await externalApi.linkVaultNote("a1", "v1");
    expect(lastUrl()).toContain("/external/accounts/a1/vault-note");
    expect(lastCall().body).toEqual({ vault_entry_id: "v1" });
  });

  it("자격증명을 되읽는 함수가 없다", () => {
    // 되읽기 경로를 만들면 그게 가장 약한 고리가 된다.
    for (const name of Object.keys(externalApi)) {
      expect(name).not.toMatch(/^(get|read|reveal|show)Credential/);
    }
  });

  it("계정 생성 요청에 비밀번호를 담지 않는다", async () => {
    // 등록 폼에 비밀번호 칸을 두면 그 값이 요청 로그에 남는다.
    nextPayload = { data: { id: "a1" } };
    await externalApi.createAccount({
      external_system_id: "s1", display_name: "본사", account_code: "MAIN",
    });
    const body = lastCall().body as Record<string, unknown>;
    expect(Object.keys(body).some((k) => /secret|password|token|key/i.test(k)))
      .toBe(false);
  });
});

describe("있는 것과 되는 것을 구분한다", () => {
  it("커넥터가 없으면 지원 기능이 하나도 없다", () => {
    const caps = { PRODUCT_IMPORT: false, ORDER_IMPORT: false };
    expect(supportedCapabilities(caps)).toEqual([]);
  });

  it("지원하는 것만 사람이 읽는 이름으로 추린다", () => {
    const caps = { PRODUCT_IMPORT: true, ORDER_IMPORT: false, SOURCE_COST_IMPORT: true };
    const labels = supportedCapabilities(caps);
    expect(labels).toHaveLength(2);
    expect(labels).toContain("상품 가져오기");
    expect(labels).not.toContain("주문 가져오기");
  });

  it("연결 실패 사유를 서로 다른 말로 표시한다", () => {
    // "연결 실패" 하나로 뭉치면 무엇을 고쳐야 할지 알 수 없다.
    const labels = Object.values(TEST_STATUS_LABEL);
    expect(new Set(labels).size).toBe(labels.length);
    expect(TEST_STATUS_LABEL.CONNECTOR_UNAVAILABLE).toContain("우리 쪽");
    expect(TEST_STATUS_LABEL.AUTHENTICATION_FAILED).toContain("자격증명");
  });

  it("미지원과 실패를 다른 말로 표시한다", () => {
    // 둘을 같은 말로 쓰면 사용자가 우리 쪽 버그로 오해한다.
    expect(SYNC_STATUS_LABEL.UNSUPPORTED).not.toBe(SYNC_STATUS_LABEL.FAILED);
    expect(SYNC_STATUS_LABEL.UNSUPPORTED).toContain("미지원");
  });
});

describe("0 과 미완료를 구분한다", () => {
  it("아직 안 끝난 실행의 건수는 null 이다", async () => {
    const run: SyncRun = {
      id: "r1", external_account_id: "a1", sync_type: "PRODUCTS",
      trigger: "MANUAL", status: "RUNNING",
      started_at: "2026-08-10T00:00:00Z", finished_at: null,
      read_count: null, created_count: null, updated_count: null,
      skipped_count: null, conflict_count: null, error_count: null,
      error_code: null, error_message: null,
    };
    nextPayload = { data: [run], meta: { total: 1 } };

    const res = await externalApi.runs();
    // 0 으로 내려오면 "아무것도 못 가져왔다" 로 읽힌다.
    expect(res.items[0].read_count).toBeNull();
    expect(res.items[0].status).toBe("RUNNING");
  });
});
