/**
 * 알림 API 클라이언트 계약 테스트 (D9 회귀).
 *
 * 발견한 결함
 * ---------
 * 이전 클라이언트와 헤더 벨은 백엔드와 **필드 이름이 전부 달랐다.**
 *
 * ```
 * 화면            서버
 * unread_count    unread          ← 배지가 항상 0이었다
 * is_read         read_at
 * notification_type  kind
 * action_url      target_url
 * page/page_size  limit/offset
 * ```
 *
 * `unread_count` 가 undefined 라 `?? 0` 으로 떨어졌고, 알림이 아무리 쌓여도
 * 벨에 숫자가 뜨지 않았다. name vs name_ko, stock vs stock_quantity 와 같은
 * 종류의 결함이다 — 타입도 린트도 빌드도 잡지 못한다.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CRITICAL_KINDS, NOTIFICATION_KIND_LABEL, isUnread, notificationPrefsApi,
  notificationsApi, type NotificationKind,
} from "./notifications";

const calls: { url: string; method: string; body?: unknown }[] = [];
let payload: unknown = { data: null };

beforeEach(() => {
  calls.length = 0;
  payload = { data: null };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        url: String(input),
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      });
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify(payload),
      } as unknown as Response;
    }),
  );
});

afterEach(() => vi.unstubAllGlobals());

const last = () => calls[calls.length - 1];

/** 백엔드 `enum_notification_kind` (M001 + M031). 순서 무관, 집합이 같아야 한다. */
const BACKEND_KINDS: NotificationKind[] = [
  "SETTLEMENT_DEADLINE", "VAT_DEADLINE", "MARGIN_DROP", "STOCKOUT_RISK",
  "POLICY_REVIEW", "ACCOUNTANT_COMMENT", "SYSTEM",
  "APPROVAL_REQUESTED", "APPROVAL_REJECTED", "APPROVAL_APPLY_FAILED",
  "MONTH_CLOSE_BLOCKED", "MONTH_CLOSE_READY", "PAYOUT_PENDING",
  "ATTACHMENT_DELETE_FAILED", "CHANNEL_PRICE_PUBLISH_FAILED", "WORKER_FAILURE",
];

describe("미읽음 카운트 필드 이름 (D9)", () => {
  it("서버가 주는 키는 unread 다 — unread_count 가 아니다", async () => {
    payload = { data: { unread: 7 } };
    const res = await notificationsApi.unreadCount();
    expect(res.unread).toBe(7);
    // 예전 이름이 살아 있으면 배지가 다시 0이 된다.
    expect((res as unknown as Record<string, unknown>).unread_count).toBeUndefined();
  });

  it("경로는 /notifications/unread-count 다", async () => {
    await notificationsApi.unreadCount();
    expect(last().url).toContain("/notifications/unread-count");
  });
});

describe("읽음 여부는 read_at 으로 판단한다", () => {
  it("read_at 이 null 이면 안 읽음", () => {
    expect(isUnread({ read_at: null } as never)).toBe(true);
    expect(isUnread({ read_at: "2026-08-09T00:00:00Z" } as never)).toBe(false);
  });
});

describe("목록 쿼리", () => {
  it("only_unread / limit / offset 을 보낸다 — is_read / page 가 아니다", async () => {
    payload = { data: [], meta: { total: 0 } };
    await notificationsApi.list({ only_unread: true, limit: 20, offset: 40 });

    expect(last().url).toContain("only_unread=true");
    expect(last().url).toContain("limit=20");
    expect(last().url).toContain("offset=40");
    expect(last().url).not.toContain("is_read");
  });

  it("only_unread 가 false 면 아예 보내지 않는다", async () => {
    payload = { data: [], meta: { total: 0 } };
    await notificationsApi.list({ only_unread: false });
    expect(last().url).not.toContain("only_unread");
  });
});

describe("읽음 처리 경로", () => {
  it("단건은 POST /notifications/{id}/read", async () => {
    await notificationsApi.markRead("n-1");
    expect(last().method).toBe("POST");
    expect(last().url).toContain("/notifications/n-1/read");
  });

  it("전체는 POST /notifications/mark-all-read", async () => {
    payload = { data: { marked: 3 } };
    const res = await notificationsApi.markAllRead();
    expect(last().url).toContain("/notifications/mark-all-read");
    expect(res.marked).toBe(3);
  });
});

describe("환경설정", () => {
  it("본인 설정 경로는 /admin/notification-prefs 다", async () => {
    payload = { data: { kind_settings_json: {}, channel_settings_json: {} } };
    await notificationPrefsApi.get();
    expect(last().url).toContain("/admin/notification-prefs");

    await notificationPrefsApi.update({ channel_settings_json: { EMAIL: false } });
    expect(last().method).toBe("PATCH");
    expect(last().body).toMatchObject({ channel_settings_json: { EMAIL: false } });
  });
});

describe("표시 라벨", () => {
  it("모든 백엔드 종류에 한글 라벨이 있다", () => {
    for (const kind of BACKEND_KINDS) {
      expect(NOTIFICATION_KIND_LABEL[kind], kind).toBeTruthy();
    }
    expect(Object.keys(NOTIFICATION_KIND_LABEL).sort()).toEqual([...BACKEND_KINDS].sort());
  });

  it("조치가 필요한 종류를 구분한다 — 정보와 같은 무게로 두면 둘 다 무시된다", () => {
    expect(CRITICAL_KINDS.has("WORKER_FAILURE")).toBe(true);
    expect(CRITICAL_KINDS.has("MONTH_CLOSE_READY")).toBe(false);
  });
});
