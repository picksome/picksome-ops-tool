"use client";

/**
 * 알림센터 (Phase 39).
 *
 * 이전 화면은 `notificationAdapter`(메모리 배열)를 썼다. 알림 목록도, 읽음 처리도,
 * 규칙 저장도 전부 mock 이었다. 새로고침하면 되돌아갔고, 실제로는 어떤 사건도
 * 알림을 만들지 않고 있었다.
 *
 * 이 화면이 지키는 것
 * ----------------
 * · 목록·읽음 처리는 실제 `/notifications` 다. 조회는 서버가 **본인 것만** 준다
 * · **알림 규칙 편집은 지원하지 않는다.** 규칙 테이블이 없다 —
 *   어떤 사건이 알림을 만드는지는 코드에 있고(`services/notification_events.py`),
 *   화면에서 조건을 바꿀 수 없다. 그 사실을 적는다
 * · 채널은 **지원 여부**를 보여준다. Slack/Discord 는 webhook 이 없으므로
 *   연결됨으로 표시하지 않는다
 */

import { useState } from "react";
import Link from "next/link";
import type { Route } from "next";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Check, CircleAlert, Info, Mail, MonitorSmartphone } from "lucide-react";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { PageShell, PageTabs, Section } from "@/components/shell/page-shell";
import { useListQuery } from "@/hooks/useListQuery";
import { ApiError } from "@/lib/api";
import {
  CRITICAL_KINDS, NOTIFICATION_KIND_LABEL, isUnread, notificationsApi,
  type ChannelMeta, type NotificationItem,
} from "@/lib/api/notifications";
import { formatDateTime, formatRelative } from "@/lib/format";

export default function NotificationsPage() {
  const { state, set } = useListQuery({ defaults: { tab: "list" } });
  const tab = state.tab || "list";

  return (
    <PageShell
      title="알림센터"
      description="운영 중 일어난 사건을 모아 봅니다"
      tabs={
        <PageTabs
          tabs={[
            { key: "list", label: "알림 목록" },
            { key: "channels", label: "전달 채널" },
          ]}
          value={tab}
          onChange={(k) => set({ tab: k })}
        />
      }
      variant="detail"
    >
      {tab === "channels" ? <ChannelsTab /> : <NotificationList />}
    </PageShell>
  );
}

function NotificationList() {
  const qc = useQueryClient();
  const [onlyUnread, setOnlyUnread] = useState(false);

  const { data, isLoading, error } = useQuery({
    queryKey: ["notifications", "page", onlyUnread],
    queryFn: () => notificationsApi.list({ only_unread: onlyUnread, limit: 100 }),
  });

  const invalidate = () => qc.invalidateQueries({ queryKey: ["notifications"] });

  const markOne = useMutation({
    mutationFn: (id: string) => notificationsApi.markRead(id),
    onSuccess: invalidate,
    onError: () => toast.error("읽음 처리에 실패했습니다"),
  });

  const markAll = useMutation({
    mutationFn: () => notificationsApi.markAllRead(),
    onSuccess: (r) => {
      invalidate();
      toast.success(`${r.marked}건을 읽음 처리했습니다`);
    },
    onError: () => toast.error("읽음 처리에 실패했습니다"),
  });

  const items = data?.items ?? [];
  const unreadCount = items.filter(isUnread).length;

  return (
    <div className="space-y-4">
      <Section
        title="알림"
        description="운영 이슈·승인·마감·지급에서 일어난 사건입니다"
        actions={
          <div className="flex gap-2">
            <Button
              size="sm"
              variant={onlyUnread ? "default" : "outline"}
              onClick={() => setOnlyUnread((v) => !v)}
            >
              안 읽은 것만
            </Button>
            <Button
              size="sm" variant="outline"
              onClick={() => markAll.mutate()}
              disabled={markAll.isPending || unreadCount === 0}
            >
              <Check className="mr-1.5 h-3.5 w-3.5" aria-hidden />
              모두 읽음
            </Button>
          </div>
        }
      >
        {error ? (
          <p role="alert" className="rounded-md border border-[--color-danger] bg-[--color-danger-bg] p-3 text-sm">
            {error instanceof ApiError ? error.message : "알림을 불러오지 못했습니다."}
          </p>
        ) : isLoading ? (
          <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-10 text-center">
            <Info className="h-7 w-7 text-[--color-muted-foreground]" aria-hidden />
            <p className="text-sm">
              {onlyUnread ? "안 읽은 알림이 없습니다" : "알림이 없습니다"}
            </p>
            <p className="max-w-md text-xs text-[--color-muted-foreground]">
              승인 요청·거절, 월 마감 차단, 지급 예정, 백그라운드 작업 실패가 생기면
              여기에 쌓입니다.
            </p>
          </div>
        ) : (
          <ul className="divide-y divide-[--color-border] overflow-hidden rounded-lg border border-[--color-border]">
            {items.map((n) => (
              <NotificationRow
                key={n.id}
                item={n}
                onRead={() => markOne.mutate(n.id)}
                busy={markOne.isPending}
              />
            ))}
          </ul>
        )}
      </Section>

      <p className="flex items-start gap-1.5 rounded-md bg-[--color-muted] p-2 text-[10px] leading-relaxed text-[--color-muted-foreground]">
        <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
        <span>
          <strong>알림 규칙 편집은 지원하지 않습니다.</strong> 어떤 사건이 알림을 만드는지는
          코드에 고정돼 있고(조건을 저장할 규칙 테이블이 없습니다), 화면에서 임계값을
          바꿀 수 없습니다. 조치가 필요한 항목은{" "}
          <Link href={"/operations/issues" as Route} className="underline">운영 이슈</Link> 에서
          담당자·기한과 함께 관리하세요.
        </span>
      </p>
    </div>
  );
}

function NotificationRow({
  item, onRead, busy,
}: {
  item: NotificationItem;
  onRead: () => void;
  busy: boolean;
}) {
  const unread = isUnread(item);
  const critical = CRITICAL_KINDS.has(item.kind);

  return (
    <li className={cn("flex items-start gap-3 px-3 py-2.5", unread && "bg-[--color-muted]/50")}>
      {critical ? (
        <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-[--color-danger]" aria-hidden />
      ) : (
        <Info className="mt-0.5 h-4 w-4 shrink-0 text-[--color-info]" aria-hidden />
      )}

      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <Badge variant={critical ? "danger" : "info"} className="px-1.5 py-0 text-[10px]">
            {NOTIFICATION_KIND_LABEL[item.kind] ?? item.kind}
          </Badge>
          {unread && (
            <span className="rounded-full bg-[--color-accent] px-1.5 py-0.5 text-[9px] font-semibold text-white">
              안 읽음
            </span>
          )}
          {/* 보냈다고 단정하지 않는다 — SMTP 미설정이면 EMAIL 은 대기 상태다. */}
          {item.delivery_status !== "SENT" && (
            <span className="text-[10px] text-[--color-warning]">
              {item.channel_code} {item.delivery_status === "QUEUED" ? "발송 대기" : "발송 실패"}
            </span>
          )}
        </div>

        <p className="mt-0.5 text-[13px] font-medium">
          {item.target_url ? (
            <Link href={item.target_url as Route} className="ops-focusable hover:underline">
              {item.title}
            </Link>
          ) : (
            item.title
          )}
        </p>
        {item.body && (
          <p className="mt-0.5 text-xs text-[--color-muted-foreground]">{item.body}</p>
        )}
        <p className="mt-1 text-[10px] text-[--color-muted-foreground]" title={formatDateTime(item.sent_at)}>
          {formatRelative(item.sent_at)}
        </p>
      </div>

      {unread && (
        <Button variant="ghost" size="sm" onClick={onRead} disabled={busy}>
          읽음
        </Button>
      )}
    </li>
  );
}

/* ────────────────────────────────────────────────────────────
 * 전달 채널 — 연결 상태를 지어내지 않는다
 * ──────────────────────────────────────────────────────────── */

const CHANNEL_ICON: Record<string, typeof Mail> = {
  IN_APP: MonitorSmartphone,
  EMAIL: Mail,
};

/**
 * 채널별 실제 상태.
 *
 * **성공 mock 을 만들지 않는다.** webhook 이 없으면 `DISCONNECTED` 다.
 * 예전 화면은 `connectChannel()` 이 메모리 플래그를 켜고 "연결됨" 을 보여줬다 —
 * 아무 데도 보내지지 않는데 사용자는 연결됐다고 믿었다.
 */
const CHANNEL_NOTE: Record<string, string> = {
  IN_APP: "앱 안에서 바로 확인합니다. 별도 설정이 필요 없습니다.",
  EMAIL: "SMTP 설정이 있어야 실제로 발송됩니다. 없으면 발송 대기 상태로 저장됩니다.",
  KAKAO: "알림톡 사전심사와 발신프로필이 필요합니다.",
  SLACK: "webhook URL 이 없어 연결할 수 없습니다.",
  TELEGRAM: "봇 토큰이 없어 연결할 수 없습니다.",
  WEBHOOK: "수신 URL 이 없어 연결할 수 없습니다.",
};

function ChannelsTab() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["notification-channels"],
    queryFn: () => notificationsApi.channels(),
  });

  return (
    <div className="space-y-4">
      <Section
        title="전달 채널"
        description="어떤 채널이 실제로 동작하는지 보여줍니다"
      >
        {error ? (
          <p role="alert" className="rounded-md border border-[--color-danger] bg-[--color-danger-bg] p-3 text-sm">
            채널 정보를 불러오지 못했습니다.
          </p>
        ) : isLoading ? (
          <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>
        ) : (
          <ul className="divide-y divide-[--color-border] overflow-hidden rounded-lg border border-[--color-border]">
            {(data ?? []).map((c: ChannelMeta) => {
              const Icon = CHANNEL_ICON[c.code] ?? MonitorSmartphone;
              return (
                <li key={c.code} className="flex items-start gap-3 px-3 py-3">
                  <Icon className="mt-0.5 h-4 w-4 shrink-0 text-[--color-muted-foreground]" aria-hidden />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-[13px] font-medium">{c.display_name}</span>
                      <Badge
                        variant={c.is_mvp_active ? "success" : "warning"}
                        className="px-1.5 py-0 text-[10px]"
                      >
                        {c.is_mvp_active ? "사용 중" : "미연결"}
                      </Badge>
                    </div>
                    <p className="mt-0.5 text-[11px] text-[--color-muted-foreground]">
                      {CHANNEL_NOTE[c.code] ?? "설정이 필요합니다."}
                    </p>
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </Section>

      <p className="flex items-start gap-1.5 rounded-md bg-[--color-muted] p-2 text-[10px] leading-relaxed text-[--color-muted-foreground]">
        <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
        <span>
          미연결 채널은 <strong>연결 버튼을 제공하지 않습니다.</strong> 자격증명(webhook URL,
          봇 토큰)이 없는 상태에서 &quot;연결됨&quot; 으로 표시하면, 아무 데도 보내지지 않는데
          사용자는 알림을 받고 있다고 믿게 됩니다. 자격증명이 준비되면 서버 설정으로
          활성화합니다.
        </span>
      </p>
    </div>
  );
}
