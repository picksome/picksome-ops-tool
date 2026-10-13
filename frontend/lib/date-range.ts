/**
 * 기간 필터 공통 로직.
 *
 * 모든 목록 화면이 같은 프리셋과 같은 직렬화 형식을 쓴다. 화면마다 다르면
 * URL 을 공유했을 때 다르게 해석되고, "이번 달"의 정의가 화면마다 갈린다.
 *
 * 기준 시간대는 **Asia/Seoul**. 서버가 UTC 인 환경에서 `new Date()` 의 로컬 날짜를
 * 그대로 쓰면 한국 새벽 시간대에 하루가 밀린다. 그래서 KST 기준 연/월/일을 직접 계산한다.
 */

export type DateRange = {
  /** YYYY-MM-DD (포함) */
  from: string;
  /** YYYY-MM-DD (포함) */
  to: string;
};

export type DateRangePresetKey =
  | "today"
  | "last7"
  | "last30"
  | "thisMonth"
  | "lastMonth"
  | "thisYear"
  | "custom";

export const DATE_RANGE_PRESETS: Array<{ key: DateRangePresetKey; label: string }> = [
  { key: "today", label: "오늘" },
  { key: "last7", label: "최근 7일" },
  { key: "last30", label: "최근 30일" },
  { key: "thisMonth", label: "이번 달" },
  { key: "lastMonth", label: "지난달" },
  { key: "thisYear", label: "올해" },
];

/** KST 기준 오늘의 연/월/일. */
function kstParts(base = new Date()): { y: number; m: number; d: number } {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(base);
  const get = (t: string) => Number(parts.find((p) => p.type === t)?.value ?? 0);
  return { y: get("year"), m: get("month"), d: get("day") };
}

function iso(y: number, m: number, d: number): string {
  return `${String(y).padStart(4, "0")}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

/** 날짜 문자열에 일수를 더한다. UTC 기준 산술이라 DST/시간대 영향이 없다. */
export function addDays(dateStr: string, days: number): string {
  const [y, m, d] = dateStr.split("-").map(Number);
  const t = Date.UTC(y, m - 1, d) + days * 86_400_000;
  const dt = new Date(t);
  return iso(dt.getUTCFullYear(), dt.getUTCMonth() + 1, dt.getUTCDate());
}

/** 해당 월의 마지막 날. */
export function lastDayOfMonth(y: number, m: number): number {
  return new Date(Date.UTC(y, m, 0)).getUTCDate();
}

/** 오늘(KST) YYYY-MM-DD. */
export function todayKst(): string {
  const { y, m, d } = kstParts();
  return iso(y, m, d);
}

/** 이번 달(KST) YYYY-MM. */
export function currentYearMonth(): string {
  const { y, m } = kstParts();
  return `${y}-${String(m).padStart(2, "0")}`;
}

export function resolvePreset(key: DateRangePresetKey): DateRange | null {
  const { y, m, d } = kstParts();
  const today = iso(y, m, d);
  switch (key) {
    case "today":
      return { from: today, to: today };
    case "last7":
      // "최근 7일" 은 오늘 포함 7일이다. 오늘을 빼면 사용자가 오늘 매출을 못 본다.
      return { from: addDays(today, -6), to: today };
    case "last30":
      return { from: addDays(today, -29), to: today };
    case "thisMonth":
      return { from: iso(y, m, 1), to: iso(y, m, lastDayOfMonth(y, m)) };
    case "lastMonth": {
      const py = m === 1 ? y - 1 : y;
      const pm = m === 1 ? 12 : m - 1;
      return { from: iso(py, pm, 1), to: iso(py, pm, lastDayOfMonth(py, pm)) };
    }
    case "thisYear":
      return { from: iso(y, 1, 1), to: iso(y, 12, 31) };
    case "custom":
    default:
      return null;
  }
}

/** 현재 범위가 어떤 프리셋과 일치하는지 역판정. 칩 활성화 표시에 쓴다. */
export function matchPreset(range: DateRange | null): DateRangePresetKey {
  if (!range?.from || !range?.to) return "custom";
  for (const { key } of DATE_RANGE_PRESETS) {
    const r = resolvePreset(key);
    if (r && r.from === range.from && r.to === range.to) return key;
  }
  return "custom";
}

/**
 * 직전 동일 길이 기간. KPI 의 "이전 기간 대비" 비교 기준.
 * 30일을 봤으면 그 직전 30일과 비교한다 — 달력상 전월과 비교하면 일수가 달라 왜곡된다.
 */
export function previousRange(range: DateRange): DateRange {
  const from = new Date(range.from + "T00:00:00Z").getTime();
  const to = new Date(range.to + "T00:00:00Z").getTime();
  const lengthDays = Math.round((to - from) / 86_400_000) + 1;
  return {
    from: addDays(range.from, -lengthDays),
    to: addDays(range.to, -lengthDays),
  };
}

/** 범위 내 일수. */
export function rangeLengthDays(range: DateRange): number {
  const from = new Date(range.from + "T00:00:00Z").getTime();
  const to = new Date(range.to + "T00:00:00Z").getTime();
  return Math.round((to - from) / 86_400_000) + 1;
}

/**
 * 범위가 길면 일별 대신 월별로 집계한다.
 * 6개월(약 180일)을 일 단위로 그리면 점이 너무 촘촘해 추세가 안 읽힌다.
 */
export function suggestBucket(range: DateRange): "day" | "month" {
  return rangeLengthDays(range) > 92 ? "month" : "day";
}

/** 범위 내 월 목록(YYYY-MM). 월별 집계 축 생성용. */
export function monthsInRange(range: DateRange): string[] {
  const [fy, fm] = range.from.split("-").map(Number);
  const [ty, tm] = range.to.split("-").map(Number);
  const out: string[] = [];
  let y = fy;
  let m = fm;
  while (y < ty || (y === ty && m <= tm)) {
    out.push(`${y}-${String(m).padStart(2, "0")}`);
    m += 1;
    if (m > 12) { m = 1; y += 1; }
  }
  return out;
}

/** 범위 내 날짜 목록(YYYY-MM-DD). 일별 집계 축 생성용. 최대 366일로 제한. */
export function daysInRange(range: DateRange): string[] {
  const out: string[] = [];
  let cur = range.from;
  let guard = 0;
  while (cur <= range.to && guard < 366) {
    out.push(cur);
    cur = addDays(cur, 1);
    guard += 1;
  }
  return out;
}

/** 유효한 범위인지. from > to 이면 무효. */
export function isValidRange(range: Partial<DateRange> | null | undefined): range is DateRange {
  if (!range?.from || !range?.to) return false;
  return range.from <= range.to;
}

/** 표시용 라벨. "2026-08-01 ~ 2026-08-31" 또는 프리셋 이름. */
export function formatRangeLabel(range: DateRange | null): string {
  if (!range) return "전체 기간";
  const preset = matchPreset(range);
  if (preset !== "custom") {
    return DATE_RANGE_PRESETS.find((p) => p.key === preset)?.label ?? "";
  }
  return `${range.from} ~ ${range.to}`;
}
