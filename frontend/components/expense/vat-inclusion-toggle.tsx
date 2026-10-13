"use client";

import { useEffect, useRef } from "react";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import type { VatInclusionType } from "@/lib/api/expenses";

interface VatInclusionToggleProps {
  value: VatInclusionType;
  onChange: (v: VatInclusionType) => void;
  grossAmount: number;
  supplyAmount: number;
  vatAmount: number;
  onGrossChange: (v: number) => void;
  onSupplyChange: (v: number) => void;
  onVatChange: (v: number) => void;
  /** 수동 수정 여부 */
  isManualOverride: boolean;
  onManualOverrideChange: (v: boolean) => void;
  /** 현재 부가세율 (0.1 = 10%) */
  vatRate?: number;
}

const OPTIONS: { value: VatInclusionType; label: string; desc: string }[] = [
  { value: "INCLUDED", label: "포함",  desc: "합계금액에서 자동 분리" },
  { value: "EXCLUDED", label: "별도",  desc: "공급가액 + 부가세 직접 입력" },
  { value: "EXEMPT",   label: "면세",  desc: "부가세 0원" },
];

/**
 * D-284 vat_inclusion_type 입력 컴포넌트.
 * INCLUDED: gross → supply/vat 자동 계산 (수동 수정 가능)
 * EXCLUDED: supply 입력 → vat = supply * rate 자동
 * EXEMPT:   vat = 0, supply = gross
 */
export function VatInclusionToggle({
  value,
  onChange,
  grossAmount,
  supplyAmount,
  vatAmount,
  onGrossChange,
  onSupplyChange,
  onVatChange,
  isManualOverride,
  onManualOverrideChange,
  vatRate = 0.1,
}: VatInclusionToggleProps) {
  /**
   * **입력이 실제로 바뀌었을 때만 계산한다.**
   *
   * 저장된 비용을 열자마자 다시 분리하면, 사용자가 아무것도 건드리지 않았는데
   * 공급가액·부가세가 화면에서 바뀐다. 그 값이 그대로 변경분으로 잡혀
   * "승인 요청" 버튼이 뜨고, 메모만 고치려던 사람이 금액 변경 승인을 올리게 된다.
   * 저장된 값의 주인은 서버다 — 저장 시점의 세금 정책으로 박제한 값이고,
   * 화면의 기본 세율로 덮어쓸 근거가 없다.
   *
   * "첫 렌더면 건너뛴다" 로는 부족하다. 개발 모드의 StrictMode 는 effect 를
   * 두 번 돌리는데 ref 는 그대로 살아 있어서, 두 번째 실행이 곧바로 덮어썼다.
   * 그래서 마운트 여부가 아니라 **직전 값과의 차이**를 본다.
   */
  const prev = useRef({ value, grossAmount, supplyAmount, isManualOverride });

  useEffect(() => {
    const p = prev.current;
    const userChanged =
      p.value !== value || p.grossAmount !== grossAmount ||
      p.supplyAmount !== supplyAmount || p.isManualOverride !== isManualOverride;
    prev.current = { value, grossAmount, supplyAmount, isManualOverride };
    if (!userChanged) return;
    if (isManualOverride) return;

    if (value === "INCLUDED" && grossAmount > 0) {
      const vat = Math.round((grossAmount * vatRate) / (1 + vatRate));
      onSupplyChange(grossAmount - vat);
      onVatChange(vat);
    } else if (value === "EXCLUDED" && supplyAmount > 0) {
      onGrossChange(supplyAmount + Math.round(supplyAmount * vatRate));
      onVatChange(Math.round(supplyAmount * vatRate));
    } else if (value === "EXEMPT") {
      onVatChange(0);
      onSupplyChange(grossAmount);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, grossAmount, supplyAmount, vatRate, isManualOverride]);

  return (
    <div className="space-y-3">
      {/* 유형 선택 */}
      <div>
        <Label className="mb-1.5 block">부가세 처리 방식</Label>
        <div className="flex gap-2">
          {OPTIONS.map((opt) => (
            <button
              key={opt.value}
              type="button"
              onClick={() => { onChange(opt.value); onManualOverrideChange(false); }}
              className={cn(
                "flex-1 rounded-lg border px-3 py-2 text-left text-xs transition-colors",
                value === opt.value
                  ? "border-[--color-accent] bg-[--color-info-bg] text-[--color-accent]"
                  : "border-[--color-border] hover:bg-[--color-muted]",
              )}
            >
              <span className="font-semibold">{opt.label}</span>
              <span className="mt-0.5 block text-[--color-muted-foreground]">{opt.desc}</span>
            </button>
          ))}
        </div>
        <p className="mt-1.5 text-[11px] text-[--color-muted-foreground]">
          ⚠ 세무사 검토용 참고자료입니다. 확정 세무 계산이 아닙니다.
        </p>
      </div>

      {/* 금액 입력 */}
      <div className="grid grid-cols-3 gap-2">
        <div className="space-y-1">
          {/* htmlFor 를 붙인다 — 라벨과 입력이 연결돼 있지 않으면 스크린리더가
              "숫자 입력" 이라고만 읽고, 어떤 금액인지 알 수 없다. */}
          <Label htmlFor="e-gross" className="text-xs">
            {value === "EXCLUDED" ? "공급가액 *" : "합계금액 *"}
          </Label>
          <Input
            id="e-gross"
            type="number"
            suffix="원"
            value={value === "EXCLUDED" ? supplyAmount || "" : grossAmount || ""}
            onChange={(e) => {
              const n = parseInt(e.target.value) || 0;
              onManualOverrideChange(false);
              if (value === "EXCLUDED") {
                onSupplyChange(n);
              } else {
                onGrossChange(n);
              }
            }}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="e-supply" className="text-xs">
            공급가액{value === "INCLUDED" && <span className="text-[--color-muted-foreground]"> (자동)</span>}
          </Label>
          <Input
            id="e-supply"
            type="number"
            suffix="원"
            value={supplyAmount || ""}
            readOnly={!isManualOverride && value !== "EXCLUDED"}
            className={cn(
              !isManualOverride && value !== "EXCLUDED" && "bg-[--color-surface-muted] text-[--color-muted-foreground]",
            )}
            onChange={(e) => {
              onManualOverrideChange(true);
              onSupplyChange(parseInt(e.target.value) || 0);
            }}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="e-vat" className="text-xs">
            부가세{value !== "EXCLUDED" && <span className="text-[--color-muted-foreground]"> (자동)</span>}
          </Label>
          <Input
            id="e-vat"
            type="number"
            suffix="원"
            value={vatAmount || ""}
            readOnly={!isManualOverride && value !== "EXCLUDED"}
            className={cn(
              value === "EXEMPT" && "bg-[--color-surface-muted] text-[--color-muted-foreground]",
              !isManualOverride && value !== "EXCLUDED" && "bg-[--color-surface-muted] text-[--color-muted-foreground]",
            )}
            onChange={(e) => {
              onManualOverrideChange(true);
              onVatChange(parseInt(e.target.value) || 0);
            }}
          />
        </div>
      </div>

      {isManualOverride && (
        <p className="text-xs text-[--color-warning]">
          ⚠ 수동 수정됨 — 자동 계산이 비활성화되었습니다.{" "}
          <button
            type="button"
            className="underline"
            onClick={() => onManualOverrideChange(false)}
          >
            자동으로 되돌리기
          </button>
        </p>
      )}
    </div>
  );
}
