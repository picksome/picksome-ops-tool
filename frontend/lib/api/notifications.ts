/**
 * 알림 API 클라이언트 (Phase 39).
 *
 * ⚠️ 이 파일의 이전 버전은 백엔드와 **필드 이름이 전부 달랐다** (D9).
 *
 * ```
 * 화면이 기대한 것          실제 백엔드 (api/v1/notifications.py)
 * unread_count              unread
 * is_read                   read_at (timestamp 또는 null)
 * notification_type         kind
 * action_url                target_url
 * is_read / page / page_size  only_unread / limit / offset
 * ```
 *
 * 그 결과 **헤더 벨의 미읽음 배지가 항상 0이었다.** `unread_count` 가 undefined 라
 * `?? 0` 으로 떨어졌기 때문이다. 알림이 쌓여도 사용자는 알 수 없었다.
 * name vs name_ko, stock vs stock_quantity 와 같은 종류의 결함이다 —
 * 그래서 필드 이름을 계약 테스트로 고정한다.
 */

import { api, type ListResponse } from "@/lib/api";

/**
 * 알림 종류. 백엔드 `enum_notification_kind` 와 **정확히** 같아야 한다.
 * 앞의 7종은 기존 일정 알림, 뒤의 9종은 M031 에서 추가한 실제 운영 사건이다.
 */
export type NotificationKind =
  | "SETTLEMENT_DEADLINE"
  | "VAT_DEADLINE"
  | "MARGIN_DROP"
  | "STOCKOUT_RISK"
  | "POLICY_REVIEW"
  | "ACCOUNTANT_COMMENT"
  | "SYSTEM"
  | "APPROVAL_REQUESTED"
  | "APPROVAL_REJECTED"
  | "APPROVAL_APPLY_FAILED"
  | "MONTH_CLOSE_BLOCKED"
  | "MONTH_CLOSE_READY"
  | "PAYOUT_PENDING"
  | "ATTACHMENT_DELETE_FAILED"
  | "CHANNEL_PRICE_PUBLISH_FAILED"
  | "WORKER_FAILURE";

export const NOTIFICATION_KIND_LABEL: Record<NotificationKind, string> = {
  SETTLEMENT_DEADLINE: "정산 마감일",
  VAT_DEADLINE: "부가세 신고일",
  MARGIN_DROP: "마진 하락",
  STOCKOUT_RISK: "품절 위험",
  POLICY_REVIEW: "정책 검토",
  ACCOUNTANT_COMMENT: "세무사 코멘트",
  SYSTEM: "시스템",
  APPROVAL_REQUESTED: "승인 요청",
  APPROVAL_REJECTED: "승인 거절",
  APPROVAL_APPLY_FAILED: "승인 적용 실패",
  MONTH_CLOSE_BLOCKED: "월 마감 차단",
  MONTH_CLOSE_READY: "월 마감 가능",
  PAYOUT_PENDING: "지급 예정",
  ATTACHMENT_DELETE_FAILED: "증빙 삭제 실패",
  CHANNEL_PRICE_PUBLISH_FAILED: "가격 반영 실패",
  WORKER_FAILURE: "백그라운드 작업 실패",
};

/**
 * 조치가 필요한 알림.
 *
 * "정보" 와 "지금 뭔가 잘못됐다" 를 같은 무게로 보여주면 둘 다 무시된다.
 */
export const CRITICAL_KINDS: ReadonlySet<string> = new Set<NotificationKind>([
  "APPROVAL_APPLY_FAILED",
  "MONTH_CLOSE_BLOCKED",
  "ATTACHMENT_DELETE_FAILED",
  "CHANNEL_PRICE_PUBLISH_FAILED",
  "WORKER_FAILURE",
]);

export type DeliveryStatus = "SENT" | "QUEUED" | "FAILED";
export type ChannelCode = "IN_APP" | "EMAIL" | "KAKAO" | "SLACK" | "TELEGRAM" | "WEBHOOK";

export interface NotificationItem {
  id: string;
  kind: NotificationKind;
  title: string;
  body: string | null;
  channel_code: ChannelCode;
  target_url: string | null;
  sent_at: string | null;
  /** SMTP 미설정 환경의 EMAIL 은 QUEUED 다 — "보냈다" 고 말하지 않는다. */
  delivery_status: DeliveryStatus;
  /** null 이면 안 읽음. 백엔드에 `is_read` 불리언은 **없다**. */
  read_at: string | null;
  meta_json: Record<string, unknown> | null;
}

export interface ChannelMeta {
  code: ChannelCode;
  display_name: string;
  /** MVP 에서 실제로 동작하는 채널인가. false 면 연결할 수 없다. */
  is_mvp_active: boolean;
  sort_order: number;
}

export interface NotificationFilters {
  only_unread?: boolean;
  limit?: number;
  offset?: number;
}

export function isUnread(n: NotificationItem): boolean {
  return n.read_at === null;
}

export const notificationsApi = {
  list: (filters: NotificationFilters = {}) =>
    api.get<ListResponse<NotificationItem>>("/notifications", {
      query: {
        only_unread: filters.only_unread || undefined,
        limit: filters.limit ?? 50,
        offset: filters.offset ?? 0,
      },
    }),

  /** 응답은 `{ unread: n }` 이다. `unread_count` 가 아니다 (D9). */
  unreadCount: () => api.get<{ unread: number }>("/notifications/unread-count"),

  markRead: (id: string) => api.post<null>(`/notifications/${id}/read`),

  markAllRead: () => api.post<{ marked: number }>("/notifications/mark-all-read"),

  /** 등록된 채널 메타. **연결 상태가 아니라 지원 여부다.** */
  channels: () => api.get<ChannelMeta[]>("/notifications/channels"),
};

/**
 * 본인 알림 환경설정.
 *
 * 경로가 `/admin/...` 이지만 **관리자 전용이 아니다** — 서버가 항상 요청자 본인의
 * 설정만 읽고 쓴다(`api/v1/notifications.py` prefs_router). 남의 설정은 볼 수 없다.
 */
export interface NotificationPrefs {
  /** 종류별 수신 여부. 키가 없으면 기본 수신. */
  kind_settings_json: Record<string, boolean>;
  /** 채널별 수신 여부. 기본은 EMAIL·IN_APP 모두 true. */
  channel_settings_json: Record<string, boolean>;
}

export const notificationPrefsApi = {
  get: () => api.get<NotificationPrefs>("/admin/notification-prefs"),
  update: (body: Partial<NotificationPrefs>) =>
    api.patch<NotificationPrefs>("/admin/notification-prefs", body),
};
