/**
 * 금액·비율 산술 (프론트엔드).
 *
 * CLAUDE.md Calculation Rules: **금액에 float 를 쓰지 않는다.**
 * BE 는 `int`(원 단위) / `Decimal` 을 쓰지만, JS 에는 정수 타입이 없고 모든 number 가
 * IEEE-754 double 이다. 그래서 다음 규칙을 강제한다.
 *
 *   1. 금액은 항상 **원 단위 정수**로 들고 다닌다. 소수점 원은 존재하지 않는다.
 *   2. 비율(수수료율 등)은 소수(0.11)로 들고 다니되, **곱셈 결과는 즉시 반올림**해서
 *      정수로 되돌린다. 중간값을 실수로 누적하면 0.1 + 0.2 = 0.30000000000000004 류의
 *      오차가 여러 항목에 걸쳐 누적된다.
 *   3. 비율 자체의 덧셈(수수료율 합계 등)은 **basis point(1/10000) 정수**로 환산해서 더한다.
 *      역산 공식의 분모에서 여러 요율을 더하는데, 실수로 더하면 마지막 자리가 흔들려
 *      권장가가 1원씩 튄다.
 *
 * 반올림은 **round-half-up**(사사오입)으로 통일한다. JS 의 `Math.round` 는 음수에서
 * half-up 이 아니라 half-toward-+∞ 라 손실 금액에서 결과가 어긋난다.
 */

/** 원 단위 정수 금액. 의미 표시용 별칭 — 런타임 강제는 아니다. */
export type Won = number;

/** 0.1234 형태의 비율. */
export type Rate = number;

/** 비율의 정수 표현. 1 bp = 0.0001. 11% → 1100bp. */
export type Bp = number;

const BP_SCALE = 10_000;

/** round-half-up. -0.5 → -1, 0.5 → 1. */
export function roundHalfUp(value: number): number {
  return value < 0 ? -Math.round(-value) : Math.round(value);
}

/** 실수를 원 단위 정수로 확정. 계산 파이프라인의 모든 출구에서 호출한다. */
export function toWon(value: number): Won {
  if (!Number.isFinite(value)) return 0;
  return roundHalfUp(value);
}

/** 비율 → basis point 정수. */
export function rateToBp(rate: Rate | null | undefined): Bp {
  if (rate === null || rate === undefined || !Number.isFinite(rate)) return 0;
  return roundHalfUp(rate * BP_SCALE);
}

/** basis point → 비율. */
export function bpToRate(bp: Bp): Rate {
  return bp / BP_SCALE;
}

/**
 * 금액 × 비율 → 원 단위 정수.
 * 비율을 bp 정수로 바꾼 뒤 곱하고 나눠서, 실수 곱셈이 한 번만 일어나게 한다.
 */
export function applyRate(amount: Won, rate: Rate | null | undefined): Won {
  const bp = rateToBp(rate);
  if (bp === 0) return 0;
  return toWon((amount * bp) / BP_SCALE);
}

/** 여러 비율의 합 — bp 정수로 더한 뒤 되돌린다. 실수 누적 오차 방지. */
export function sumRates(...rates: Array<Rate | null | undefined>): Rate {
  const total = rates.reduce<number>((acc, r) => acc + rateToBp(r), 0);
  return bpToRate(total);
}

/** 금액 합계 — 정수 덧셈이므로 안전하지만 null 처리를 한 곳에 모은다. */
export function sumWon(...amounts: Array<Won | null | undefined>): Won {
  return amounts.reduce<number>((acc, v) => acc + (Number.isFinite(v as number) ? (v as number) : 0), 0);
}

/**
 * 비율 계산 (마진율 등). 분모가 0이면 null — 0을 반환하면 "마진율 0%" 로 잘못 표시된다.
 */
export function safeRate(numerator: Won, denominator: Won): Rate | null {
  if (!denominator) return null;
  return numerator / denominator;
}

/**
 * 부가세 포함가에서 세액 분리. 예: 33,000원(VAT포함) → 공급가 30,000 / 세액 3,000.
 * `price * 10/110` 을 그대로 쓰면 실수 오차가 생기므로 정수 연산으로 처리한다.
 */
export function splitVatIncluded(grossAmount: Won, vatRate: Rate): { supply: Won; vat: Won } {
  const bp = rateToBp(vatRate);
  if (bp === 0) return { supply: grossAmount, vat: 0 };
  // vat = gross * bp / (BP_SCALE + bp)
  const vat = toWon((grossAmount * bp) / (BP_SCALE + bp));
  return { supply: grossAmount - vat, vat };
}

/** 부가세 별도가에 세액 부가. */
export function addVat(supplyAmount: Won, vatRate: Rate): { gross: Won; vat: Won } {
  const vat = applyRate(supplyAmount, vatRate);
  return { gross: supplyAmount + vat, vat };
}

/**
 * 나눗셈 후 원 단위 확정. 목표가 역산의 마지막 단계에 쓴다.
 * 분모가 0 이하이면 null — 호출자가 "달성 불가" 로 처리해야 한다.
 */
export function divideToWon(numerator: number, denominator: number): Won | null {
  if (!Number.isFinite(denominator) || denominator <= 0) return null;
  return toWon(numerator / denominator);
}

/**
 * 비율 배분 시 1원 단위 잔수 처리.
 * 각자 내림 → 남은 잔수를 비율이 큰 순서로 1원씩 나눠준다.
 * (BE 는 "마지막 파트너 귀속" 이지만, 화면 시뮬레이션에서는 편차를 최소화하는 쪽이
 *  사용자에게 덜 이상해 보인다. 실제 정산 저장은 BE 값을 따른다.)
 */
export function distributeByRatio(total: Won, ratios: Rate[]): Won[] {
  if (ratios.length === 0) return [];
  const raw = ratios.map((r) => total * r);
  const floored = raw.map((v) => Math.floor(v));
  let remainder = total - floored.reduce((a, b) => a + b, 0);
  const order = raw
    .map((v, i) => ({ i, frac: v - Math.floor(v) }))
    .sort((a, b) => b.frac - a.frac);
  const result = [...floored];
  let k = 0;
  while (remainder > 0 && order.length > 0) {
    result[order[k % order.length].i] += 1;
    remainder -= 1;
    k += 1;
  }
  return result;
}

/**
 * 사용자 입력 문자열 → 금액 정수.
 * "1,234,500원", " 12345 ", "1.2만" 같은 입력을 견딘다. 빈 문자열은 null.
 */
export function parseAmountInput(input: string): Won | null {
  const trimmed = input.trim();
  if (!trimmed) return null;
  const cleaned = trimmed.replace(/[,\s원₩]/g, "");
  if (!cleaned || cleaned === "-") return null;
  const n = Number(cleaned);
  if (!Number.isFinite(n)) return null;
  return toWon(n);
}

/**
 * 사용자 입력 문자열 → 비율.
 * "11", "11%", "0.11" 을 전부 받는다. **1 이상이면 퍼센트로 간주**한다 —
 * 실무에서 "11" 을 치면 11% 를 의도하지, 1100% 를 의도하지 않는다.
 */
export function parsePercentInput(input: string): Rate | null {
  const trimmed = input.trim().replace(/[%\s]/g, "");
  if (!trimmed) return null;
  const n = Number(trimmed);
  if (!Number.isFinite(n)) return null;
  return n >= 1 ? n / 100 : n;
}
