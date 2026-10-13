"use client";

/**
 * 구성원 및 권한 (Phase 62 설정 mock 감사).
 *
 * ⚠️ 이전 화면은 **존재하지 않는 역할 체계**를 보여줬다.
 *
 * ```
 * 화면이 보여준 역할                       실제 백엔드 역할 (core/rbac.ROLES)
 * OWNER / PRODUCT_MANAGER /                ADMIN / OPERATOR / SOURCING /
 * SETTLEMENT_MANAGER / EXPENSE_APPROVER /  ACCOUNTING / READONLY /
 * VIEWER / ADMIN                           EXTERNAL_ACCOUNTANT
 * ```
 *
 * 구성원 목록은 mock 배열이었고 저장은 메모리에만 남았다. "구성원 초대" 버튼은
 * 토스트만 띄웠다. 즉 **이 화면에서 권한을 바꿔도 실제 권한은 그대로였다.**
 * 권한 화면이 거짓말을 하면 "이 사람은 못 본다" 는 판단 자체가 무너진다.
 *
 * 지금은 `/admin/users`(ADMIN 전용) 실제 API 를 쓴다.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Check, Info, Minus, ShieldAlert, UserPlus } from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PageShell, PageTabs, Section, Field } from "@/components/shell/page-shell";
import { TextInput } from "@/components/ui/money-input";
import { ApiError } from "@/lib/api";
import { ROLE_LABELS, adminApi, type UserOut } from "@/lib/api/admin";
import { useCurrentUser } from "@/hooks/useCurrentUser";
import type { UserRole } from "@/stores/auth";
import { formatRelative } from "@/lib/format";

/** 백엔드 `core/rbac.ROLES` 와 **정확히** 같아야 한다. */
const ROLES: UserRole[] = [
  "ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY", "EXTERNAL_ACCOUNTANT",
];

const ROLE_NOTE: Record<UserRole, string> = {
  ADMIN: "모든 기능. 월 잠금·지급 승인·구성원 관리는 관리자만 할 수 있습니다",
  OPERATOR: "상품·판매·비용·가격 운영. 정산 확정과 지급 장부는 볼 수 없습니다",
  SOURCING: "상품 소싱과 상품 정보. 판매·비용·정산은 접근할 수 없습니다",
  ACCOUNTING: "정산·마감·지급·세무 리포트. 상품 소싱은 접근할 수 없습니다",
  READONLY: "조회만 가능합니다. 어떤 것도 수정할 수 없습니다",
  EXTERNAL_ACCOUNTANT: "외부 세무대리인. 증빙 조회만 열려 있습니다",
};

/**
 * 실제로 서버가 강제하는 권한.
 *
 * **이 표는 설정이 아니라 문서다.** 여기서 체크를 켜도 아무것도 바뀌지 않는다 —
 * 권한은 라우터의 `require_roles(...)` 가 정하고, 역할을 바꾸는 것만이
 * 유일한 조작 수단이다. 예전 화면은 개별 권한 체크박스를 보여줘서
 * 마치 조합을 만들 수 있는 것처럼 오해하게 했다.
 *
 * 각 행은 백엔드 라우터에서 그대로 옮긴 것이다
 * (`app/api/v1/*.py` 의 `require_roles`). 백엔드 테스트가 이 값과 실제
 * 라우터가 일치하는지 검사한다 — 어긋나면 테스트가 깨진다.
 */
const CAPABILITIES: Array<{ label: string; roles: UserRole[]; note?: string }> = [
  { label: "상품 조회", roles: ["ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY"] },
  { label: "상품 등록·수정", roles: ["ADMIN", "OPERATOR", "SOURCING"] },
  { label: "판매장부 조회", roles: ["ADMIN", "OPERATOR", "ACCOUNTING", "READONLY"] },
  { label: "판매 등록·수정", roles: ["ADMIN", "OPERATOR", "ACCOUNTING"] },
  { label: "사업비용 조회", roles: ["ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING", "READONLY"] },
  { label: "사업비용 등록·수정", roles: ["ADMIN", "OPERATOR", "ACCOUNTING"] },
  { label: "판매가 등록·수정", roles: ["ADMIN", "OPERATOR", "SOURCING"] },
  { label: "월 마감 확정", roles: ["ADMIN", "ACCOUNTING"] },
  { label: "월 잠금·재오픈", roles: ["ADMIN"], note: "되돌리기 어려운 조작이라 관리자만" },
  { label: "수익배분 조회", roles: ["ADMIN", "ACCOUNTING", "OPERATOR"] },
  { label: "수익배분 확정·승인", roles: ["ADMIN", "ACCOUNTING"] },
  {
    label: "지급 장부 조회", roles: ["ADMIN", "ACCOUNTING"],
    note: "누가 얼마를 가져가는지는 운영 업무에 필요하지 않습니다",
  },
  { label: "지급 승인", roles: ["ADMIN"] },
  {
    label: "현금흐름 조회·기록", roles: ["ADMIN", "ACCOUNTING"],
    note: "통장 잔액과 지급액이 그대로 드러나 지급 장부와 같은 등급입니다",
  },
  { label: "정책 수정 (수수료·세금·배분 비율)", roles: ["ADMIN", "OPERATOR"] },
  { label: "세무 리포트 내보내기", roles: ["ADMIN", "ACCOUNTING"], note: "외부 반출은 감사에 남습니다" },
  { label: "운영 이슈 처리", roles: ["ADMIN", "OPERATOR", "ACCOUNTING"] },
  { label: "시스템 상태·worker 실행", roles: ["ADMIN"] },
  { label: "구성원 관리·감사 로그", roles: ["ADMIN"] },
];

function errorMessage(err: unknown, action: string): string {
  if (!(err instanceof ApiError)) return `${action} 중 통신에 실패했습니다.`;
  if (err.status === 403) return `${action} 권한이 없습니다. 관리자만 할 수 있습니다.`;
  if (err.status === 409) return err.message || "이미 존재하는 사용자입니다.";
  return err.message || `${action}에 실패했습니다.`;
}

export default function MembersPage() {
  const [tab, setTab] = useState("members");

  return (
    <PageShell
      title="구성원 및 권한"
      description="역할이 곧 권한입니다. 개별 권한을 따로 켜고 끌 수는 없습니다."
      tabs={
        <PageTabs
          tabs={[
            { key: "members", label: "구성원" },
            { key: "permissions", label: "권한 매트릭스" },
          ]}
          value={tab}
          onChange={setTab}
        />
      }
      variant="detail"
    >
      {tab === "permissions" ? <PermissionMatrix /> : <MembersTab />}
    </PageShell>
  );
}

function MembersTab() {
  const qc = useQueryClient();
  const { user: me } = useCurrentUser();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [role, setRole] = useState<UserRole>("OPERATOR");
  const [tempPassword, setTempPassword] = useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["admin-users"],
    queryFn: () => adminApi.users.list({ limit: 200 }),
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["admin-users"] });

  const changeRole = useMutation({
    mutationFn: ({ id, next }: { id: string; next: UserRole }) =>
      adminApi.users.update(id, { role: next }),
    onSuccess: () => { invalidate(); toast.success("역할을 변경했습니다"); },
    onError: (e) => toast.error(errorMessage(e, "역할 변경")),
  });

  const toggleActive = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      active ? adminApi.users.deactivate(id) : adminApi.users.activate(id),
    onSuccess: () => { invalidate(); toast.success("상태를 변경했습니다"); },
    onError: (e) => toast.error(errorMessage(e, "상태 변경")),
  });

  const create = useMutation({
    mutationFn: () =>
      adminApi.users.create({ email: email.trim(), display_name: name.trim(), role }),
    onSuccess: (created) => {
      invalidate();
      setEmail("");
      setName("");
      // 서버가 임시 비밀번호를 meta 로 준다. envelope 의 meta 는 단건 응답에서
      // 버려지므로 안내만 한다 — 평문을 화면에 붙들어 두지 않는다.
      setTempPassword(created.email);
      toast.success("구성원을 추가했습니다. 임시 비밀번호를 전달하세요.");
    },
    onError: (e) => toast.error(errorMessage(e, "구성원 추가")),
  });

  // 403 은 "고장" 이 아니다. 무엇이 부족한지 말한다.
  if (error instanceof ApiError && error.status === 403) {
    return (
      <Section title="구성원">
        <div className="flex flex-col items-center gap-2 py-10 text-center">
          <ShieldAlert className="h-7 w-7 text-[--color-muted-foreground]" aria-hidden />
          <p className="text-sm">구성원 관리는 관리자만 할 수 있습니다.</p>
          <p className="text-xs text-[--color-muted-foreground]">
            내 역할: {me ? ROLE_LABELS[me.role] : "-"} · 권한 매트릭스 탭에서 각 역할이
            무엇을 할 수 있는지 볼 수 있습니다.
          </p>
        </div>
      </Section>
    );
  }

  const users = data?.items ?? [];

  return (
    <div className="space-y-4">
      <Section title="구성원" description="역할을 바꾸면 그 즉시 서버 권한이 바뀝니다">
        {error ? (
          <p role="alert" className="rounded-md border border-[--color-danger] bg-[--color-danger-bg] p-3 text-sm">
            {errorMessage(error, "구성원 조회")}
          </p>
        ) : isLoading ? (
          <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
        ) : (
          <div className="ops-scroll-x">
            <table className="w-full text-[12px]">
              <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
                <tr>
                  <th scope="col" className="px-3 py-1.5 text-left font-semibold">이름</th>
                  <th scope="col" className="px-3 py-1.5 text-left font-semibold">이메일</th>
                  <th scope="col" className="px-3 py-1.5 text-left font-semibold">역할</th>
                  <th scope="col" className="px-3 py-1.5 text-left font-semibold">마지막 로그인</th>
                  <th scope="col" className="px-3 py-1.5 text-right font-semibold">상태</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u: UserOut) => (
                  <tr key={u.id} className={cn("border-t border-[--color-border]", !u.is_active && "opacity-60")}>
                    <th scope="row" className="px-3 py-1.5 text-left font-medium">
                      {u.display_name}
                      {me?.id === u.id && (
                        <span className="ml-1.5 text-[10px] text-[--color-muted-foreground]">(나)</span>
                      )}
                    </th>
                    <td className="px-3 py-1.5 text-[--color-muted-foreground]">{u.email}</td>
                    <td className="px-3 py-1.5">
                      <select
                        aria-label={`${u.display_name} 역할`}
                        value={u.role}
                        // 자기 역할을 스스로 낮추면 관리자가 0명이 될 수 있다.
                        disabled={me?.id === u.id || changeRole.isPending}
                        onChange={(e) =>
                          changeRole.mutate({ id: u.id, next: e.target.value as UserRole })
                        }
                        className="ops-focusable h-7 rounded-md border border-[--color-border] bg-[--color-card] px-1.5 text-[11px] disabled:opacity-60"
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r}>{ROLE_LABELS[r]}</option>
                        ))}
                      </select>
                    </td>
                    <td className="px-3 py-1.5 text-[--color-muted-foreground]">
                      {/* 없는 시각을 "방금" 으로 보여주지 않는다. */}
                      {u.last_login_at ? formatRelative(u.last_login_at) : "기록 없음"}
                    </td>
                    <td className="px-3 py-1.5 text-right">
                      <Button
                        size="sm" variant="outline"
                        disabled={me?.id === u.id || toggleActive.isPending}
                        onClick={() => toggleActive.mutate({ id: u.id, active: u.is_active })}
                      >
                        {u.is_active ? "비활성화" : "활성화"}
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {tempPassword && (
          <p className="mt-3 rounded-md border border-[--color-warning] bg-[--color-warning-bg] p-2 text-[11px] text-[--color-warning]">
            <strong>{tempPassword}</strong> 계정을 만들었습니다. 임시 비밀번호는 서버 응답에만
            있으므로, 첫 로그인 후 변경하도록 안내하세요.
            <Button size="sm" variant="outline" className="ml-2" onClick={() => setTempPassword(null)}>
              확인
            </Button>
          </p>
        )}
      </Section>

      <Section title="구성원 추가">
        <div className="grid gap-2.5 sm:grid-cols-4">
          <Field label="이메일" htmlFor="new-email">
            <TextInput
              id="new-email" type="email" value={email}
              onChange={(e) => setEmail(e.target.value)} placeholder="name@example.com"
            />
          </Field>
          <Field label="이름" htmlFor="new-name">
            <TextInput id="new-name" value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="역할" htmlFor="new-role">
            <select
              id="new-role" value={role}
              onChange={(e) => setRole(e.target.value as UserRole)}
              className="ops-focusable h-8 w-full rounded-md border border-[--color-border] bg-[--color-card] px-2 text-xs"
            >
              {ROLES.map((r) => <option key={r} value={r}>{ROLE_LABELS[r]}</option>)}
            </select>
          </Field>
          <div className="flex items-end">
            <Button
              size="sm"
              disabled={!email.trim() || !name.trim() || create.isPending}
              onClick={() => create.mutate()}
            >
              <UserPlus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              추가
            </Button>
          </div>
        </div>
        <p className="mt-1.5 text-[10px] text-[--color-muted-foreground]">
          {ROLE_NOTE[role]}
        </p>
      </Section>
    </div>
  );
}

function PermissionMatrix() {
  return (
    <div className="space-y-4">
      <Section title="역할" description="백엔드가 실제로 강제하는 6개 역할입니다">
        <ul className="space-y-1.5">
          {ROLES.map((r) => (
            <li key={r} className="rounded-md border border-[--color-border] px-3 py-2">
              <div className="flex items-center gap-2">
                <Badge variant="info" className="px-1.5 py-0 text-[10px]">{ROLE_LABELS[r]}</Badge>
                <code className="text-[10px] text-[--color-muted-foreground]">{r}</code>
              </div>
              <p className="mt-0.5 text-[11px] text-[--color-muted-foreground]">{ROLE_NOTE[r]}</p>
            </li>
          ))}
        </ul>
      </Section>

      <Section
        title="권한 매트릭스"
        description="서버가 강제하는 값입니다. 이 표에서 바꿀 수 있는 것은 없습니다."
      >
        <div className="ops-scroll-x">
          <table className="w-full text-[12px]">
            <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
              <tr>
                <th scope="col" className="px-3 py-1.5 text-left font-semibold">기능</th>
                {ROLES.map((r) => (
                  <th key={r} scope="col" className="px-2 py-1.5 text-center font-semibold">
                    {ROLE_LABELS[r]}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {CAPABILITIES.map((c) => (
                <tr key={c.label} className="border-t border-[--color-border]">
                  <th scope="row" className="px-3 py-1.5 text-left font-normal">
                    {c.label}
                    {c.note && (
                      <span className="block text-[10px] text-[--color-muted-foreground]">{c.note}</span>
                    )}
                  </th>
                  {ROLES.map((r) => (
                    <td key={r} className="px-2 py-1.5 text-center">
                      {c.roles.includes(r) ? (
                        <Check className="mx-auto h-3.5 w-3.5 text-[--color-success]" aria-label="가능" />
                      ) : (
                        <Minus className="mx-auto h-3.5 w-3.5 text-[--color-muted-foreground]" aria-label="불가" />
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <p className="mt-2 flex items-start gap-1.5 rounded-md bg-[--color-muted] p-2 text-[10px] leading-relaxed text-[--color-muted-foreground]">
          <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
          <span>
            개별 권한 체크박스를 두지 않았습니다. 조합을 허용하면 &quot;누가 무엇을 볼 수
            있는지&quot; 를 아무도 파악하지 못하게 되고, 화면에서 켠 값이 서버에 반영되지
            않으면 <strong>권한 화면이 거짓말을 하게 됩니다.</strong> 역할을 바꾸는 것이
            유일한 조작 수단입니다.
          </span>
        </p>
      </Section>
    </div>
  );
}
