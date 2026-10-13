/**
 * 비밀번호 **보관 위치** 목록 (`password_vault_entry`).
 *
 * 여기에 비밀번호는 없다
 * -------------------
 * 이 프로젝트는 실제 비밀번호를 저장하지 않고 어디에 있는지만 저장한다
 * (CLAUDE.md Security Rules). 항목 하나는 "Bitwarden 의 '쿠팡 Wing' 항목"
 * 같은 **가리키는 말**이다.
 *
 * 왜 외부 연동 화면이 이걸 읽나
 * -------------------------
 * 외부 계정에 자격증명을 연결할 때 값을 입력받지 않고 **이 목록에서 고르게**
 * 한다. 입력 칸을 만들면 그 값이 요청 본문·검증 오류·프록시 로그에 남는다.
 */

import { api, type ListResponse } from "@/lib/api";

export interface VaultEntry {
  id: string;
  service_name: string;
  vault_provider: string;
  vault_item_name: string;
  /** 마스킹된 계정 표시용. 전체 값이 아니다. */
  account_masked: string | null;
  memo: string | null;
  created_at: string;
  updated_at: string | null;
}

export const vaultApi = {
  list: (params: { limit?: number; offset?: number } = {}) =>
    api.get<ListResponse<VaultEntry>>("/admin/vault", { query: params }),
};

/** 목록에서 고를 때 보여줄 한 줄. 어느 보관함의 어떤 항목인지 알아야 고른다. */
export function vaultEntryLabel(e: VaultEntry): string {
  const account = e.account_masked ? ` · ${e.account_masked}` : "";
  return `${e.service_name} — ${e.vault_provider} / ${e.vault_item_name}${account}`;
}
