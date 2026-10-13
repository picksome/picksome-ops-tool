"use client";

import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { PageShell, PageTabs, Section } from "@/components/shell/page-shell";
import { ROLE_LABELS } from "@/lib/api/admin";
import { useCurrentUser } from "@/hooks/useCurrentUser";
import { useListQuery } from "@/hooks/useListQuery";
import {
  NOTIFICATION_KIND_LABEL, notificationPrefsApi, notificationsApi,
  type NotificationKind, type NotificationPrefs,
} from "@/lib/api/notifications";

/**
 * 내 프로필 (Phase 62 설정 mock 감사).
 *
 * ⚠️ 이전 화면은 **`app_user` 에 없는 필드를 편집하게 했다.**
 *
 * ```
 * 화면이 편집시킨 것                     app_user 실제 컬럼
 * 전화번호 / 프로필 이미지 / 부서 /       id, org_id, email, display_name,
 * 직급 / 입사일 / 담당 판매처 /           role, is_active, created_at,
 * 담당 카테고리                          last_login_at
 * ```
 *
 * 게다가 "나" 를 mock 구성원 목록의 **첫 번째 사람**으로 잡았다. 저장하면
 * "프로필을 저장했습니다" 토스트가 떴지만 아무 데도 저장되지 않았다.
 *
 * 지금은 `/auth/me` 로 실제 로그인 사용자를 보여주고, 저장할 수 없는 항목은
 * 만들지 않는다. 이름·역할 변경은 관리자만 할 수 있으므로 그 사실을 안내한다.
 */
export default function ProfilePage() {
  const { state, set } = useListQuery({ defaults: { tab: "profile" } });
  const tab = state.tab || "profile";
  const { user, isLoading } = useCurrentUser();

  return (
    <PageShell
      title="내 프로필"
      description="로그인 계정 정보와 알림 수신 설정입니다"
      tabs={
        <PageTabs
          tabs={[
            { key: "profile", label: "프로필" },
            { key: "notifications", label: "알림 설정" },
          ]}
          value={tab}
          onChange={(k) => set({ tab: k })}
        />
      }
      variant="detail"
    >
      {tab === "notifications" ? (
        <NotificationPrefsSection />
      ) : isLoading || !user ? (
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
      ) : (
        <div className="space-y-4">
          <Section title="계정">
            <dl className="divide-y divide-[--color-border] overflow-hidden rounded-lg border border-[--color-border]">
              <Row label="이름" value={user.display_name} />
              <Row label="이메일" value={user.email} />
              <Row
                label="권한 역할"
                value={
                  <Badge variant="info" className="px-2 py-0.5 text-[11px]">
                    {ROLE_LABELS[user.role]}
                  </Badge>
                }
              />
              <Row label="계정 상태" value={user.is_active ? "활성" : "비활성"} />
            </dl>
            <p className="mt-2 text-[11px] text-[--color-muted-foreground]">
              이름과 역할은 <strong>관리자만</strong> 변경할 수 있습니다
              (설정 &gt; 구성원 및 권한). 본인이 자기 역할을 바꿀 수 있으면
              권한 체계가 의미를 잃습니다.
            </p>
          </Section>

          <Section title="저장되지 않는 항목">
            <p className="text-[11px] text-[--color-muted-foreground]">
              아래 항목은 <strong>저장할 곳이 없습니다.</strong> 예전 화면은 입력란을
              두고 &quot;저장했습니다&quot; 를 띄웠지만 아무 데도 남지 않았습니다 —
              입력란을 없애고 사실을 적습니다.
            </p>
            <ul className="mt-2 flex flex-wrap gap-1.5">
              {UNSUPPORTED_PROFILE_FIELDS.map((f) => (
                <li
                  key={f}
                  className="rounded-full border border-dashed border-[--color-border] px-2 py-0.5 text-[10px] text-[--color-muted-foreground]"
                >
                  {f} — 미지원
                </li>
              ))}
            </ul>
          </Section>

          <Section title="보안">
            <dl className="divide-y divide-[--color-border] overflow-hidden rounded-lg border border-[--color-border]">
              <Row
                label="비밀번호 변경"
                value={
                  <span className="text-[11px] text-[--color-muted-foreground]">
                    화면에서 지원하지 않습니다 — 관리자에게 요청하세요
                  </span>
                }
              />
            </dl>
            <p className="mt-2 text-[11px] text-[--color-muted-foreground]">
              계좌·카드·인증서 정보는 이 시스템에 저장하지 않습니다. 보관 위치만 기록합니다.
            </p>
          </Section>
        </div>
      )}
    </PageShell>
  );
}

/** `app_user` 에 컬럼이 없어 저장할 수 없는 항목. */
const UNSUPPORTED_PROFILE_FIELDS = [
  "전화번호",
  "프로필 이미지",
  "부서",
  "직급",
  "입사일",
  "담당 판매처",
  "담당 카테고리",
  "비밀번호 재설정 메일",
];

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 px-3 py-2.5">
      <dt className="text-xs text-[--color-muted-foreground]">{label}</dt>
      <dd className="text-[13px]">{value}</dd>
    </div>
  );
}

/**
 * 본인 알림 수신 설정 — 실제 `/admin/notification-prefs` 를 쓴다.
 *
 * 이전 화면은 mock 알림 규칙 목록을 읽기 전용으로 보여줬다. 저장할 수도 없었고
 * 그 규칙들은 어디에도 존재하지 않았다.
 *
 * 여기서 다루는 것은 **수신 여부**뿐이다. 어떤 사건이 알림을 만드는지(임계값·조건)는
 * 코드에 고정돼 있고 화면에서 바꿀 수 없다 — 그 사실을 아래에 적는다.
 */
function NotificationPrefsSection() {
  const qc = useQueryClient();
  const { data: prefs, isLoading } = useQuery({
    queryKey: ["notification-prefs"],
    queryFn: () => notificationPrefsApi.get(),
  });
  const { data: channelMeta } = useQuery({
    queryKey: ["notification-channels"],
    queryFn: () => notificationsApi.channels(),
  });

  const update = useMutation({
    mutationFn: (body: Partial<NotificationPrefs>) => notificationPrefsApi.update(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["notification-prefs"] });
      toast.success("알림 설정을 저장했습니다");
    },
    onError: () => toast.error("저장에 실패했습니다"),
  });

  if (isLoading || !prefs) {
    return (
      <Section title="알림 설정">
        <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
      </Section>
    );
  }

  const channelOn = (code: string) => prefs.channel_settings_json[code] !== false;
  const kindOn = (kind: string) => prefs.kind_settings_json[kind] !== false;

  return (
    <div className="space-y-4">
      <Section title="받을 채널" description="끄면 그 채널로는 알림이 생성되지 않습니다">
        <ul className="space-y-1.5">
          {(channelMeta ?? []).filter((c) => c.is_mvp_active).map((c) => (
            <li
              key={c.code}
              className="flex items-center justify-between rounded-md border border-[--color-border] px-3 py-2"
            >
              <div>
                <p className="text-[12px] font-medium">{c.display_name}</p>
                {c.code === "EMAIL" && (
                  <p className="text-[10px] text-[--color-muted-foreground]">
                    SMTP 설정이 없으면 발송 대기 상태로만 남습니다
                  </p>
                )}
              </div>
              <label className="flex items-center gap-1.5 text-[11px]">
                <input
                  type="checkbox"
                  className="ops-focusable"
                  checked={channelOn(c.code)}
                  onChange={(e) =>
                    update.mutate({
                      channel_settings_json: {
                        ...prefs.channel_settings_json,
                        [c.code]: e.target.checked,
                      },
                    })
                  }
                />
                받기
              </label>
            </li>
          ))}
        </ul>
      </Section>

      <Section title="받을 알림 종류">
        <ul className="grid gap-1.5 sm:grid-cols-2">
          {NOTIFY_KINDS.map((kind) => (
            <li
              key={kind}
              className="flex items-center justify-between rounded-md border border-[--color-border] px-3 py-2"
            >
              <span className="text-[12px]">{NOTIFICATION_KIND_LABEL[kind]}</span>
              <input
                type="checkbox"
                aria-label={NOTIFICATION_KIND_LABEL[kind]}
                className="ops-focusable"
                checked={kindOn(kind)}
                onChange={(e) =>
                  update.mutate({
                    kind_settings_json: {
                      ...prefs.kind_settings_json,
                      [kind]: e.target.checked,
                    },
                  })
                }
              />
            </li>
          ))}
        </ul>
      </Section>

      <p className="rounded-md bg-[--color-muted] p-2 text-[10px] leading-relaxed text-[--color-muted-foreground]">
        여기서 정하는 것은 <strong>받을지 말지</strong>뿐입니다. 어떤 조건에서 알림이
        생기는지(임계값·주기)는 코드에 고정돼 있어 화면에서 바꿀 수 없습니다.
      </p>
    </div>
  );
}

/** 사용자가 끄고 켤 수 있는 사건. 백엔드 `enum_notification_kind` 의 부분집합이다. */
const NOTIFY_KINDS: NotificationKind[] = [
  "APPROVAL_REQUESTED",
  "APPROVAL_REJECTED",
  "APPROVAL_APPLY_FAILED",
  "MONTH_CLOSE_BLOCKED",
  "MONTH_CLOSE_READY",
  "PAYOUT_PENDING",
  "ATTACHMENT_DELETE_FAILED",
  "CHANNEL_PRICE_PUBLISH_FAILED",
  "WORKER_FAILURE",
];
