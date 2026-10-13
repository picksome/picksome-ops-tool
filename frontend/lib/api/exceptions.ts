/**
 * 예외 센터 API 클라이언트.
 *
 * `status` 를 건수와 섞지 않는다
 * ---------------------------
 * ```
 * OK              확인했고 아무것도 없다
 * ATTENTION       손댈 것이 있다
 * NOT_CONNECTED   셀 수 없다 — API 가 없다
 * NOT_CONFIGURED  셀 수 없다 — 기준을 안 정했다
 * ERROR           불러오지 못했다
 * ```
 *
 * `count = 0` 은 다섯 경우 모두에서 나온다. 그래서 화면은 **반드시 status 를
 * 보고** 문구를 정해야 한다. 0 만 보고 "이상 없음" 이라 쓰면 미연결 상태를
 * 정상으로 표시하게 된다.
 */

import { api } from "@/lib/api";

export type ExceptionCategory =
  | "ORDER" | "INVENTORY" | "SETTLEMENT" | "APPROVAL" | "INTEGRATION";

export type CategoryStatus =
  | "OK" | "ATTENTION" | "NOT_CONNECTED" | "NOT_CONFIGURED" | "ERROR";

/** 기존 operation_issue 와 같은 체계. 새 점수를 만들지 않았다. */
export type Severity = "BLOCKING" | "WARNING" | "INFO";

export const SEVERITY_LABEL: Record<Severity, string> = {
  BLOCKING: "차단",
  WARNING: "확인 필요",
  INFO: "참고",
};

export interface ExceptionItem {
  category: ExceptionCategory;
  kind: string;
  severity: Severity;
  title: string;
  detail: string | null;
  link: string | null;
  entity_type: string | null;
  entity_id: string | null;
  /** 무엇을 하면 없어지는지. 없으면 사용자가 목록만 보고 막힌다 */
  fix_label: string | null;
  fix_link: string | null;
}

export interface ExceptionCategorySummary {
  category: ExceptionCategory;
  label: string;
  status: CategoryStatus;
  count: number;
  note: string | null;
}

export interface ExceptionCenter {
  total: number;
  by_severity: Record<Severity, number>;
  categories: ExceptionCategorySummary[];
  items: ExceptionItem[];
  filtered_by?: ExceptionCategory;
}

/**
 * 카테고리 한 줄에 쓸 문구.
 *
 * 건수를 그대로 쓰지 않는 이유는 위 주석 참고 — `0` 이 다섯 가지 뜻을 갖는다.
 */
export function categoryText(c: ExceptionCategorySummary): string {
  switch (c.status) {
    case "NOT_CONNECTED":
      return "미연결";
    case "NOT_CONFIGURED":
      return "설정 필요";
    case "ERROR":
      return "불러오지 못함";
    case "OK":
      return "이상 없음";
    default:
      return `${c.count}건`;
  }
}

export const exceptionsApi = {
  list: (category?: ExceptionCategory) =>
    api.get<ExceptionCenter>(
      `/operations/exceptions${category ? `?category=${category}` : ""}`,
    ),
};
