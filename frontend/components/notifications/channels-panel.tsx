"use client";

/**
 * 전달 채널 상태.
 *
 * ⚠️ 이전 구현은 **성공 mock 이었다.** `connectChannel(kind, target)` 이 메모리
 * 플래그를 켜고 화면에 "연결됨" 을 표시했고, `testChannel()` 은 연결 플래그만 보고
 * `SUCCESS` 를 돌려줬다. **아무 데도 아무것도 보내지 않았다.**
 *
 * 그건 알림 기능에서 가장 나쁜 종류의 거짓말이다 — 사용자는 장애 알림을 받고
 * 있다고 믿게 되고, 정작 장애가 났을 때 아무 연락도 못 받는다.
 *
 * 지금은 서버가 알려주는 **지원 여부**만 보여준다. 자격증명이 없는 채널에는
 * 연결 버튼도, 테스트 버튼도 만들지 않는다.
 */

import { useQuery } from "@tanstack/react-query";
import { Info, Mail, MonitorSmartphone } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { notificationsApi, type ChannelMeta } from "@/lib/api/notifications";

const CHANNEL_ICON: Record<string, typeof Mail> = {
  IN_APP: MonitorSmartphone,
  EMAIL: Mail,
};

/** 왜 못 쓰는지 사실대로 적는다. "곧 지원" 같은 약속을 하지 않는다. */
const CHANNEL_NOTE: Record<string, string> = {
  IN_APP: "앱 안에서 바로 확인합니다. 별도 설정이 필요 없습니다.",
  EMAIL: "SMTP 설정이 있어야 실제로 발송됩니다. 없으면 발송 대기 상태로 저장됩니다.",
  KAKAO: "알림톡 사전심사와 발신프로필이 필요합니다.",
  SLACK: "webhook URL 이 없어 연결할 수 없습니다.",
  TELEGRAM: "봇 토큰이 없어 연결할 수 없습니다.",
  WEBHOOK: "수신 URL 이 없어 연결할 수 없습니다.",
};

export function ChannelsPanel() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["notification-channels"],
    queryFn: () => notificationsApi.channels(),
  });

  if (error) {
    return (
      <p role="alert" className="rounded-md border border-[--color-danger] bg-[--color-danger-bg] p-3 text-sm">
        채널 정보를 불러오지 못했습니다.
      </p>
    );
  }
  if (isLoading) {
    return <p className="text-sm text-[--color-muted-foreground]">불러오는 중…</p>;
  }

  return (
    <div className="space-y-3">
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

      <p className="flex items-start gap-1.5 rounded-md bg-[--color-muted] p-2 text-[10px] leading-relaxed text-[--color-muted-foreground]">
        <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
        <span>
          미연결 채널에는 <strong>연결·테스트 버튼을 제공하지 않습니다.</strong> 자격증명이
          없는 상태에서 &quot;연결됨&quot; 이나 &quot;테스트 성공&quot; 을 보여주면, 아무 데도
          보내지지 않는데 사용자는 알림을 받고 있다고 믿게 됩니다.
        </span>
      </p>
    </div>
  );
}
