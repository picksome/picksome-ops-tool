"use client";

import Link from "next/link";
import { PageShell } from "@/components/shell/page-shell";
import { ChannelsPanel } from "@/components/notifications/channels-panel";

/**
 * 메신저 연결.
 *
 * 알림센터의 "전달 채널" 탭과 **같은 컴포넌트**를 쓴다.
 * 두 곳에서 각자 구현하면 한쪽만 고쳐지는 일이 생긴다.
 * 사이드바에 따로 둔 이유는 "연결 작업"이 알림 확인과 다른 맥락의 일이기 때문이다.
 */
export default function MessagingPage() {
  return (
    <PageShell
      title="메신저 연결"
      description="어떤 채널로 알림이 나가는지 보여줍니다"
      variant="detail"
    >
      <ChannelsPanel />
      <p className="mt-2 text-[11px] text-[--color-muted-foreground]">
        받은 알림은{" "}
        <Link href="/notifications" className="text-[--color-accent] underline">
          알림센터
        </Link>
        에서 확인합니다. 어떤 사건이 알림을 만드는지는 코드에 고정돼 있어
        화면에서 규칙을 바꿀 수 없습니다.
      </p>
    </PageShell>
  );
}
