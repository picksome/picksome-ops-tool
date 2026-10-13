/**
 * 자격증명 대화상자 — 값이 남을 수 있는 자리를 하나씩 막았는지 본다.
 *
 * 이 파일이 고정하는 것
 * ------------------
 * 1. 비밀 칸은 `type="password"` 다 (어깨너머·화면공유·screenshot)
 * 2. 자동완성을 끈다 — 브라우저 비밀번호 관리자에 저장되면 우리 통제 밖이다
 * 3. **저장 성공 즉시 입력칸을 비운다**
 * 4. 취소·닫기에서도 비운다 — 다시 열었을 때 앞사람 값이 보이면 안 된다
 * 5. `localStorage` / `sessionStorage` 에 쓰지 않는다
 * 6. 형태를 바꾸면 이전 형태의 값이 남지 않는다
 * 7. 실패해도 기존 자격증명이 살아 있다는 것을 화면이 말한다
 */

import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CredentialDialog } from "./credential-dialog";
import type { ExternalAccount } from "@/lib/api/external";

const setCredential = vi.fn();

vi.mock("@/lib/api/external", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/external")>("@/lib/api/external");
  return {
    ...actual,
    externalApi: {
      ...actual.externalApi,
      setCredential: (...args: unknown[]) => setCredential(...args),
    },
  };
});

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
}));

const SECRET = "SELLER_OS_SUPER_SECRET_12345";

function account(over: Partial<ExternalAccount> = {}): ExternalAccount {
  return {
    id: "a1", account_code: "MAIN", display_name: "본사",
    external_account_identifier: null, status: "DISCONNECTED",
    last_connected_at: null, last_successful_sync_at: null,
    credential_configured: false, vault_note_linked: false,
    credential_kind: null, credential_updated_at: null, credential_updated_by: null,
    last_test_status: null, last_test_at: null, last_test_message: null,
    connector_available: false, capabilities: {}, mapping_count: 0,
    system_code: "NEXT_ENGINE", system_name: "NEXT ENGINE", system_type: "SELLER_TOOL",
    ...over,
  };
}

function renderDialog(props: Partial<React.ComponentProps<typeof CredentialDialog>> = {}) {
  const onOpenChange = vi.fn();
  const onChanged = vi.fn();
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={qc}>
      <CredentialDialog
        account={account()} open onOpenChange={onOpenChange} onChanged={onChanged}
        {...props}
      />
    </QueryClientProvider>,
  );
  return { ...view, onOpenChange, onChanged };
}

beforeEach(() => {
  setCredential.mockReset();
  setCredential.mockResolvedValue(account({ credential_configured: true }));
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("자격증명 입력", () => {
  it("비밀 칸은 가려지고 자동완성이 꺼져 있다", () => {
    renderDialog();
    const input = document.querySelector("#cred-api_key") as HTMLInputElement;

    expect(input.type).toBe("password");
    // 브라우저 비밀번호 관리자에 저장되면 우리 통제 밖이다.
    expect(input.autocomplete).toBe("off");
  });

  it("형태에 맞는 칸만 보여준다 — vendor 별 폼을 만들지 않는다", async () => {
    const user = userEvent.setup();
    renderDialog();

    expect(document.querySelector("#cred-api_key")).toBeTruthy();
    expect(document.querySelector("#cred-secret_key")).toBeNull();

    await user.selectOptions(screen.getByLabelText(/인증 방식/), "ACCESS_SECRET");
    expect(document.querySelector("#cred-access_key")).toBeTruthy();
    expect(document.querySelector("#cred-secret_key")).toBeTruthy();
    expect(document.querySelector("#cred-api_key")).toBeNull();
  });

  it("형태를 바꾸면 이전 형태에 넣던 값이 남지 않는다", async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.type(document.querySelector("#cred-api_key") as HTMLElement, SECRET);
    await user.selectOptions(screen.getByLabelText(/인증 방식/), "BASIC");
    await user.selectOptions(screen.getByLabelText(/인증 방식/), "API_KEY");

    const input = document.querySelector("#cred-api_key") as HTMLInputElement;
    expect(input.value).toBe("");
  });

  it("필수 칸이 비면 저장할 수 없다", async () => {
    const user = userEvent.setup();
    renderDialog();
    await user.selectOptions(screen.getByLabelText(/인증 방식/), "ACCESS_SECRET");

    const save = screen.getByRole("button", { name: /저장/ });
    expect(save).toBeDisabled();

    await user.type(document.querySelector("#cred-access_key") as HTMLElement, "AK");
    expect(save).toBeDisabled();
    await user.type(document.querySelector("#cred-secret_key") as HTMLElement, SECRET);
    expect(save).toBeEnabled();
  });
});

describe("값이 남지 않는다", () => {
  it("저장에 성공하면 입력칸을 즉시 비운다", async () => {
    const user = userEvent.setup();
    const { onOpenChange } = renderDialog();

    await user.type(document.querySelector("#cred-api_key") as HTMLElement, SECRET);
    await user.click(screen.getByRole("button", { name: /저장/ }));

    await waitFor(() => expect(setCredential).toHaveBeenCalledWith("a1", {
      credential_kind: "API_KEY", secret: { api_key: SECRET },
    }));
    await waitFor(() => {
      const input = document.querySelector("#cred-api_key") as HTMLInputElement | null;
      // 대화상자가 닫혔거나, 열려 있다면 비어 있다.
      expect(input === null || input.value === "").toBe(true);
    });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("취소하면 입력한 값이 남지 않는다", async () => {
    const user = userEvent.setup();
    const { onOpenChange } = renderDialog();

    await user.type(document.querySelector("#cred-api_key") as HTMLElement, SECRET);
    await user.click(screen.getByRole("button", { name: "취소" }));

    expect(onOpenChange).toHaveBeenCalledWith(false);
    const input = document.querySelector("#cred-api_key") as HTMLInputElement | null;
    expect(input === null || input.value === "").toBe(true);
    expect(setCredential).not.toHaveBeenCalled();
  });

  it("브라우저 저장소에 쓰지 않는다", async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.type(document.querySelector("#cred-api_key") as HTMLElement, SECRET);
    await user.click(screen.getByRole("button", { name: /저장/ }));
    await waitFor(() => expect(setCredential).toHaveBeenCalled());

    const dump = JSON.stringify({ ...window.localStorage, ...window.sessionStorage });
    expect(dump).not.toContain(SECRET);
  });

  it("설정된 뒤에도 값을 보여주지 않는다", () => {
    // 서버가 값을 주지 않으므로 보여줄 것이 없다 — 화면도 그 전제로 만든다.
    renderDialog({
      account: account({
        credential_configured: true, credential_kind: "API_KEY",
        credential_updated_at: "2026-08-10T00:00:00Z",
      }),
    });

    const input = document.querySelector("#cred-api_key") as HTMLInputElement;
    expect(input.value).toBe("");
    expect(document.body.textContent).not.toContain(SECRET);
    // 대신 실패해도 기존 것이 남는다는 사실을 말해 준다.
    expect(document.body.textContent).toContain("기존 자격증명은 그대로 남습니다");
  });
});

describe("교체", () => {
  it("이미 설정돼 있으면 '교체' 로 부른다", () => {
    renderDialog({ account: account({ credential_configured: true }) });
    expect(screen.getByRole("button", { name: /교체/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^저장$/ })).toBeNull();
  });
});
