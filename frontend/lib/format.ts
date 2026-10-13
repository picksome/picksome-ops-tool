/**
 * 한국 로케일 표시 helper.
 *
 * 표시 전용이다 — 계산은 `lib/money.ts`(FE 시뮬레이션) 또는 BE 가 책임진다.
 * 시간대는 **Asia/Seoul 로 통일**한다. BE 는 UTC timestamptz 를 주고,
 * DATE 컬럼(YYYY-MM-DD)은 시간대 환산 없이 그대로 표시한다 —
 * 판매일·비용일에 UTC 환산을 걸면 하루가 밀린다.
 *
 * ⚠️ 기존 시그니처(formatKRW/formatPercent/formatDateTime/formatDate)는 그대로 유지한다.
 * 다만 formatPercent 의 기본 소수 자릿수를 2 → 1 로 바꿨다(테이블 밀도).
 * 2자리가 필요하면 두 번째 인자로 명시한다.
 */

export const APP_TIMEZONE = "Asia/Seoul";

const nf = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 });

/** KRW 정수를 "1,234,567 원" 형식으로 표시. null/undefined 는 "-". */
export function formatKRW(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  return nf.format(value) + " 원";
}

/** 단위 없이 천 단위 구분만. 단위를 테이블 헤더에 표기한 경우. */
export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  return nf.format(value);
}

/** 부호를 항상 표시. 증감액용. 음수는 하이픈이 아니라 U+2212 로 — 폭이 일정하다. */
export function formatSignedKRW(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${nf.format(Math.abs(value))} 원`;
}

/**
 * 축약 금액 — 차트 축·좁은 셀용. 1,250,000 → "125만".
 * 툴팁과 상세에서는 축약하지 않는다(정확한 금액이 필요한 자리).
 */
export function formatCompactKRW(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  const abs = Math.abs(value);
  const sign = value < 0 ? "−" : "";
  if (abs >= 100_000_000)
    return `${sign}${(abs / 100_000_000).toFixed(abs >= 1_000_000_000 ? 0 : 1)}억`;
  if (abs >= 10_000) return `${sign}${(abs / 10_000).toFixed(abs >= 1_000_000 ? 0 : 1)}만`;
  return `${sign}${nf.format(abs)}`;
}

/** 비율(0.1234) → "12.3%". */
export function formatPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  return new Intl.NumberFormat("ko-KR", {
    style: "percent",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

/** 부호 포함 비율. 증감률용. */
export function formatSignedPercent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return sign + formatPercent(Math.abs(value), digits);
}

/** ISO timestamp → "2026-06-22 14:30" (Asia/Seoul). */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  const parts = new Intl.DateTimeFormat("ko-KR", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
    timeZone: APP_TIMEZONE,
  }).formatToParts(d);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? "";
  return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}`;
}

/** DATE 컬럼(YYYY-MM-DD) 표시. 시간대 환산 없이 그대로. */
export function formatDate(value: string | null | undefined): string {
  if (!value) return "-";
  return value.slice(0, 10);
}

/** "2026-08" → "2026년 8월" */
export function formatYearMonth(ym: string | null | undefined): string {
  if (!ym) return "-";
  const [y, m] = ym.split("-");
  if (!y || !m) return ym;
  return `${y}년 ${parseInt(m, 10)}월`;
}

/** 상대 시간 — "3분 전". 최신성이 중요한 자리(알림·동기화 상태)에만. */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  const diffSec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diffSec < 0) return formatDateTime(iso);
  if (diffSec < 60) return "방금";
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}분 전`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}시간 전`;
  if (diffSec < 86400 * 7) return `${Math.floor(diffSec / 86400)}일 전`;
  return formatDate(d.toISOString());
}

/** URL 에서 표시용 도메인만 추출. 소스 상품 URL 배지에 사용. */
export function extractDomain(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return null;
  }
}

/** 알려진 공급처 도메인 → 한글 라벨. 모르는 도메인은 도메인 그대로 노출. */
const SUPPLIER_LABELS: Record<string, string> = {
  "1688.com": "1688",
  "taobao.com": "타오바오",
  "tmall.com": "티몰",
  "aliexpress.com": "알리익스프레스",
  "domeggook.com": "도매꾹",
  "domeme.com": "도매매",
  "ownerclan.com": "오너클랜",
  "temu.com": "테무",
};

export function supplierLabelFromUrl(url: string | null | undefined): string | null {
  const host = extractDomain(url);
  if (!host) return null;
  if (SUPPLIER_LABELS[host]) return SUPPLIER_LABELS[host];
  const base = host.split(".").slice(-2).join(".");
  return SUPPLIER_LABELS[base] ?? host;
}

/** 파일 크기 표시. 첨부파일 목록용. */
export function formatFileSize(bytes: number | null | undefined): string {
  if (!bytes || bytes < 0) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
