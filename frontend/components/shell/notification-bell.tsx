"use client";

/**
 * 헤더 알림 벨.
 *
 * ⚠️ 이전 구현은 백엔드와 필드 이름이 달라서 **배지가 항상 0이었다** (D9).
 * `unread_count` 를 읽었는데 서버는 `unread` 를 준다. `is_read` 를 읽었는데
 * 서버는 `read_at` 을 준다. `created_at` 을 표시했는데 목록은 `sent_at` 기준이다.
 *
 * 타입 없이 `api.get<로컬 인터페이스>` 로 부르면 이런 어긋남을 아무도 못 잡는다.
 * 지금은 `lib/api/notifications.ts` 의 공용 타입을 쓰고, 그 계약은 테스트로 고정돼 있다.
 */

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import type { Route } from "next";
import { Bell, CircleAlert } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import {
  CRITICAL_KINDS, NOTIFICATION_KIND_LABEL, isUnread, notificationsApi,
} from "@/lib/api/notifications";
import { formatRelative } from "@/lib/format";
import { cn } from "@/lib/utils";

export function NotificationBell() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();

  // 30초 polling — docs/24_frontend.md D-281
  const { data: countData } = useQuery({
    queryKey: ["notifications", "unread-count"],
    queryFn: () => notificationsApi.unreadCount(),
    refetchInterval: 30_000,
  });

  const { data: listData } = useQuery({
    queryKey: ["notifications", "list"],
    queryFn: () => notificationsApi.list({ limit: 20 }),
    enabled: open,
  });

  const markAllRead = useMutation({
    mutationFn: () => notificationsApi.markAllRead(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  const markOne = useMutation({
    mutationFn: (id: string) => notificationsApi.markRead(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  useEffect(() => {
    function handler(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const unread = countData?.unread ?? 0;
  const items = listData?.items ?? [];

  return (
    <div ref={ref} className="relative">
      <Button
        variant="ghost"
        size="icon"
        onClick={() => setOpen((v) => !v)}
        aria-label={`알림${unread > 0 ? ` (미확인 ${unread}건)` : ""}`}
      >
        <Bell className="h-4 w-4" />
        {unread > 0 && (
          <span
            className="absolute right-1 top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-[--color-danger] px-1 text-[10px] font-bold text-white"
            data-testid="notification-unread-badge"
          >
            {unread > 99 ? "99+" : unread}
          </span>
        )}
      </Button>

      {open && (
        <div className="absolute right-0 top-12 z-50 w-96 rounded-xl border border-[--color-border] bg-[--color-card] shadow-lg">
          <div className="flex items-center justify-between border-b border-[--color-border] px-4 py-3">
            <span className="text-sm font-semibold">알림</span>
            <div className="flex items-center gap-2">
              {unread > 0 && (
                <button
                  onClick={() => markAllRead.mutate()}
                  className="ops-focusable text-xs text-[--color-muted-foreground] hover:text-[--color-foreground]"
                >
                  모두 읽음
                </button>
              )}
              <Link
                href={"/notifications" as Route}
                onClick={() => setOpen(false)}
                className="ops-focusable text-xs font-medium"
              >
                전체 보기
              </Link>
            </div>
          </div>

          <ul className="max-h-96 overflow-y-auto">
            {items.length === 0 && (
              <li className="py-8 text-center text-sm text-[--color-muted-foreground]">
                알림이 없습니다
              </li>
            )}
            {items.map((n) => {
              const unreadItem = isUnread(n);
              const critical = CRITICAL_KINDS.has(n.kind);
              const content = (
                <>
                  <div className="flex items-center gap-1.5">
                    {critical && (
                      <CircleAlert className="h-3.5 w-3.5 shrink-0 text-[--color-danger]" aria-hidden />
                    )}
                    <span className="text-[10px] text-[--color-muted-foreground]">
                      {NOTIFICATION_KIND_LABEL[n.kind] ?? n.kind}
                    </span>
                    {unreadItem && (
                      <span className="h-1.5 w-1.5 rounded-full bg-[--color-accent]" aria-label="안 읽음" />
                    )}
                  </div>
                  <p className="mt-0.5 text-sm font-medium">{n.title}</p>
                  {n.body && (
                    <p className="mt-0.5 text-xs text-[--color-muted-foreground]">{n.body}</p>
                  )}
                  <p className="mt-1 text-[10px] text-[--color-muted-foreground]">
                    {formatRelative(n.sent_at)}
                  </p>
                </>
              );

              return (
                <li
                  key={n.id}
                  className={cn(
                    "border-b border-[--color-border] last:border-0",
                    unreadItem && "bg-[--color-muted]/60",
                  )}
                >
                  {n.target_url ? (
                    <Link
                      href={n.target_url as Route}
                      onClick={() => {
                        if (unreadItem) markOne.mutate(n.id);
                        setOpen(false);
                      }}
                      className="ops-focusable block px-4 py-3 hover:bg-[--color-muted]"
                    >
                      {content}
                    </Link>
                  ) : (
                    <div className="px-4 py-3">{content}</div>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
