import { api } from "@/lib/api";
import type { UserRole } from "@/stores/auth";

export type BusinessType = "SOLE" | "JOINT" | "CORP" | "TEAM";

export const BUSINESS_TYPE_LABELS: Record<BusinessType, string> = {
  SOLE: "1인 사업자",
  JOINT: "공동 사업자",
  CORP: "법인",
  TEAM: "팀/내부운영",
};

export const ROLE_LABELS: Record<UserRole, string> = {
  ADMIN: "관리자",
  OPERATOR: "운영 담당자",
  SOURCING: "상품 소싱",
  ACCOUNTING: "정산·세무",
  READONLY: "읽기 전용",
  EXTERNAL_ACCOUNTANT: "외부 세무사",
};

export interface UserOut {
  id: string;
  org_id: string;
  email: string;
  display_name: string;
  role: UserRole;
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
}

/**
 * 조직 구성원 이름 조회 (모든 인증 사용자).
 *
 * `/admin/users` 는 ADMIN 전용이라 OPERATOR 가 승인함을 열면 요청자 이름을
 * 붙일 수 없다. 그 자리를 **mock 구성원 목록**이 채우고 있었다 —
 * 실존하지 않는 이름이 화면에 표시됐다.
 *
 * 이메일·마지막 로그인은 내려오지 않는다. 이름을 붙이는 데 필요 없다.
 */
export interface DirectoryUser {
  id: string;
  display_name: string;
  role: UserRole;
  is_active: boolean;
}

export interface UserListResponse {
  items: UserOut[];
  total: number;
}

export interface OrgSettings {
  id: string;
  name: string;
  business_type: BusinessType;
  created_at: string;
}

export interface OrgSettingsUpdate {
  name?: string;
  business_type?: BusinessType;
}

/** 조직 구성원 이름. 모든 인증 사용자가 부를 수 있다. */
export const usersApi = {
  directory: () => api.get<DirectoryUser[]>("/users/directory"),
};

/**
 * ⚠️ 이 객체의 이전 버전은 존재하지 않는 경로를 불렀다.
 *
 * ```
 * PATCH /admin/users/{id}/role         ← 없음. 실제는 PATCH /admin/users/{id}
 * PATCH /admin/users/{id}/deactivate   ← 없음. 실제는 DELETE /admin/users/{id}
 * PATCH /admin/users/{id}/activate     ← 없음. 실제는 PATCH + {is_active:true}
 * ?page=&page_size=                    ← 서버는 limit/offset 을 읽는다
 * ```
 *
 * 이 클라이언트를 쓰는 화면이 없어서 아무도 몰랐다(D6 과 같은 종류).
 * 실제 라우터에 맞춰 다시 썼고 경로를 계약 테스트로 고정했다.
 */
export const adminApi = {
  users: {
    list: (params?: { limit?: number; offset?: number }) =>
      api.get<UserListResponse>("/admin/users", {
        query: params as Record<string, number | undefined>,
      }),

    create: (body: { email: string; display_name: string; role: UserRole }) =>
      api.post<UserOut>("/admin/users", body),

    /** 역할·이름·활성 여부를 한 경로에서 바꾼다. */
    update: (
      id: string,
      body: Partial<{ display_name: string; role: UserRole; is_active: boolean }>,
    ) => api.patch<UserOut>(`/admin/users/${id}`, body),

    /** 삭제가 아니라 비활성이다 — 과거 이력의 이름이 사라지면 안 된다. */
    deactivate: (id: string) => api.delete(`/admin/users/${id}`),

    activate: (id: string) => api.patch<UserOut>(`/admin/users/${id}`, { is_active: true }),
  },

  org: {
    get: () => api.get<OrgSettings>("/admin/org"),
    update: (body: OrgSettingsUpdate) =>
      api.patch<OrgSettings>("/admin/org", body),
  },
};
