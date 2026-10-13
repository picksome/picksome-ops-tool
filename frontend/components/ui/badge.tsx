import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium",
  {
    variants: {
      variant: {
        default: "bg-[--color-muted] text-[--color-foreground]",
        success: "bg-green-100 text-green-800",
        warning: "bg-amber-100 text-amber-800",
        danger: "bg-red-100 text-red-800",
        info: "bg-sky-100 text-sky-800",
        locked: "bg-[--color-muted] text-[--color-muted-foreground]",
        "manual-override": "bg-amber-50 text-amber-700 border border-amber-200",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}

export { Badge, badgeVariants };

/** 정산 상태 배지 */
export function SettlementStatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; variant: BadgeProps["variant"] }> = {
    DRAFT: { label: "임시", variant: "default" },
    CONFIRMED: { label: "확정", variant: "success" },
    LOCKED: { label: "잠김 🔒", variant: "locked" },
    REOPENED: { label: "재개", variant: "danger" },
  };
  const { label, variant } = map[status] ?? { label: status, variant: "default" };
  return <Badge variant={variant}>{label}</Badge>;
}

/** 정책 신뢰도 배지 */
export function PolicyReliabilityBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; variant: BadgeProps["variant"] }> = {
    CONFIRMED: { label: "확정", variant: "success" },
    AUTO_DETECTED: { label: "자동감지", variant: "info" },
    NEEDS_REVIEW: { label: "검토 필요", variant: "warning" },
    NEEDS_VERIFICATION: { label: "검증 필요", variant: "warning" },
    EXPIRED: { label: "만료", variant: "danger" },
    DEPRECATED: { label: "폐기", variant: "locked" },
  };
  const { label, variant } = map[status] ?? { label: status, variant: "default" };
  return <Badge variant={variant}>{label}</Badge>;
}

/** 증빙 상태 배지 */
export function EvidenceStatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; variant: BadgeProps["variant"] }> = {
    NONE: { label: "증빙없음", variant: "danger" },
    NEEDS_CHECK: { label: "확인필요", variant: "warning" },
    PRESENT: { label: "증빙있음", variant: "info" },
    SUBMITTED_TO_ACCOUNTANT: { label: "전달완료", variant: "success" },
  };
  const { label, variant } = map[status] ?? { label: status, variant: "default" };
  return <Badge variant={variant}>{label}</Badge>;
}
