"use client";

/**
 * 구간별 수수료 편집기 (Phase A-3).
 *
 * 왜 검증이 까다로운가
 * -----------------
 * 구간이 겹치면 같은 판매가에 수수료가 두 개다. 비면 하나도 없다.
 * 둘 다 조용히 틀린 마진을 만든다 — 오류가 나는 게 아니라 **다른 숫자가 나온다.**
 *
 * ```
 * 0 ~ 10,000,000        10.8%
 * 10,000,001 ~ 30,000,000  10.2%
 * 30,000,001 ~            9.8%
 * ```
 *
 * 여기 검증은 **사용자를 돕기 위한 것**이고, 진짜 방어는 백엔드다
 * (`services/approval_policy.validate_tiers`). 화면에서만 막으면 API 를
 * 직접 부르는 순간 뚫린다. 두 곳의 규칙이 같아야 한다.
 *
 * 경계는 포함이다
 * ------------
 * `[min, max]` 양끝 포함. 다음 구간은 `max + 1` 에서 시작한다.
 * 이 규칙이 백엔드와 다르면 1원짜리 빈틈이 생긴다.
 */

import * as React from "react";
import { Plus, Trash2, TriangleAlert } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PercentInput } from "@/components/ui/money-input";

export interface Tier {
  min: number;
  /** null = 상한 없음. 마지막 구간에만 올 수 있다. */
  max: number | null;
  rate: number;
}

/**
 * 백엔드 `validate_tiers` 와 **같은 규칙**.
 *
 * 여기서 통과했는데 서버가 거절하면 사용자는 이유를 알 수 없다.
 */
export function validateTiers(tiers: Tier[]): string[] {
  if (tiers.length === 0) return ["구간을 하나 이상 입력하세요."];

  const errors: string[] = [];
  const sorted = [...tiers].sort((a, b) => a.min - b.min);

  sorted.forEach((t, i) => {
    if (t.min < 0) errors.push(`${i + 1}번째 구간의 최소 금액이 음수입니다.`);
    if (t.max !== null && t.max <= t.min) {
      errors.push(`${i + 1}번째 구간의 최대 금액이 최소 금액보다 작거나 같습니다.`);
    }
    if (t.rate < 0) errors.push(`${i + 1}번째 구간의 수수료율이 음수입니다.`);
    // 10.8% 를 10.8 로 넣는 실수. 그대로 두면 수수료가 1080% 가 된다.
    if (t.rate > 1) {
      errors.push(`${i + 1}번째 구간의 수수료율은 0~1 사이입니다 (10.8% 는 0.108).`);
    }
  });

  if (sorted[0].min !== 0) {
    errors.push(
      `첫 구간은 0원부터 시작해야 합니다. 그 아래 금액의 수수료를 알 수 없습니다.`,
    );
  }

  for (let i = 0; i < sorted.length - 1; i += 1) {
    const cur = sorted[i];
    const next = sorted[i + 1];
    if (cur.max === null) {
      errors.push("상한 없는 구간은 마지막에 하나만 올 수 있습니다.");
      break;
    }
    if (next.min <= cur.max) {
      errors.push(
        `구간이 겹칩니다: ${cur.min.toLocaleString()}~${cur.max.toLocaleString()} 와 `
        + `${next.min.toLocaleString()} 부터가 겹칩니다.`,
      );
    } else if (next.min > cur.max + 1) {
      errors.push(
        `구간 사이가 비었습니다: ${cur.max.toLocaleString()}원과 `
        + `${next.min.toLocaleString()}원 사이의 수수료를 알 수 없습니다.`,
      );
    }
  }

  if (sorted[sorted.length - 1].max !== null) {
    errors.push("마지막 구간은 상한이 없어야 합니다. 그보다 큰 금액의 수수료를 알 수 없습니다.");
  }

  return [...new Set(errors)];
}

export function TierEditor({
  tiers,
  onChange,
}: {
  tiers: Tier[];
  onChange: (t: Tier[]) => void;
}) {
  const errors = React.useMemo(() => validateTiers(tiers), [tiers]);

  function patch(i: number, updater: (t: Tier) => void) {
    const next = tiers.map((t) => ({ ...t }));
    updater(next[i]);
    onChange(next);
  }

  function addTier() {
    // 새 구간은 앞 구간 바로 다음 원부터 시작한다 — 빈틈을 만들지 않는다.
    const last = tiers[tiers.length - 1];
    if (!last) {
      onChange([{ min: 0, max: null, rate: 0 }]);
      return;
    }
    const boundary = last.max ?? last.min + 10_000_000;
    onChange([
      ...tiers.slice(0, -1),
      { ...last, max: boundary },
      { min: boundary + 1, max: null, rate: last.rate },
    ]);
  }

  function removeTier(i: number) {
    const next = tiers.filter((_, idx) => idx !== i);
    // 마지막을 지우면 새 마지막의 상한을 연다 — 안 그러면 그 위 금액이 미정이 된다.
    if (next.length > 0) next[next.length - 1] = { ...next[next.length - 1], max: null };
    onChange(next);
  }

  return (
    <div className="space-y-2">
      <div className="ops-scroll-x">
        <table className="w-full text-[12px]">
          <thead className="bg-[--color-muted] text-[11px] text-[--color-muted-foreground]">
            <tr>
              <th scope="col" className="px-2 py-1.5 text-right font-semibold">최소 금액</th>
              <th scope="col" className="px-2 py-1.5 text-right font-semibold">최대 금액</th>
              <th scope="col" className="px-2 py-1.5 text-right font-semibold">수수료율</th>
              <th scope="col" className="w-8 px-2 py-1.5" />
            </tr>
          </thead>
          <tbody>
            {tiers.map((t, i) => (
              <tr key={i} className="border-t border-[--color-border]">
                <td className="px-2 py-1.5 text-right">
                  <input
                    type="number"
                    value={t.min}
                    aria-label={`${i + 1}번째 구간 최소 금액`}
                    onChange={(e) => patch(i, (x) => { x.min = Number(e.target.value) || 0; })}
                    className="ops-focusable h-8 w-32 rounded-md border border-[--color-border] bg-[--color-card] px-1.5 text-right text-xs tabular-nums"
                  />
                </td>
                <td className="px-2 py-1.5 text-right">
                  {i === tiers.length - 1 ? (
                    // 마지막 구간은 상한이 없어야 한다. 입력칸을 두면 채우게 된다.
                    <span className="text-[11px] text-[--color-muted-foreground]">
                      상한 없음
                    </span>
                  ) : (
                    <input
                      type="number"
                      value={t.max ?? ""}
                      aria-label={`${i + 1}번째 구간 최대 금액`}
                      onChange={(e) =>
                        patch(i, (x) => {
                          x.max = e.target.value === "" ? null : Number(e.target.value);
                        })
                      }
                      className="ops-focusable h-8 w-32 rounded-md border border-[--color-border] bg-[--color-card] px-1.5 text-right text-xs tabular-nums"
                    />
                  )}
                </td>
                <td className="px-2 py-1.5 text-right">
                  <PercentInput
                    className="w-24"
                    value={t.rate}
                    onChange={(v) => patch(i, (x) => { x.rate = v ?? 0; })}
                  />
                </td>
                <td className="px-2 py-1.5 text-center">
                  <button
                    type="button"
                    onClick={() => removeTier(i)}
                    aria-label={`${i + 1}번째 구간 삭제`}
                    className="ops-focusable text-[--color-muted-foreground] hover:text-[--color-danger]"
                  >
                    <Trash2 className="h-3.5 w-3.5" aria-hidden />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <Button variant="outline" size="sm" onClick={addTier}>
        <Plus className="mr-1.5 h-3.5 w-3.5" aria-hidden />
        구간 추가
      </Button>

      {errors.length > 0 && (
        <ul className="space-y-1 rounded-md border border-[--color-danger] bg-[--color-surface-muted] px-3 py-2">
          {errors.map((e) => (
            <li key={e} className="flex items-start gap-1.5 text-[11px] text-[--color-danger]">
              <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
              {e}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
