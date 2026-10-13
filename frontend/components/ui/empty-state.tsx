import * as React from "react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

/**
 * 빈 상태.
 *
 * `action` 은 기존 `{ label, onClick }` 형태와 임의 ReactNode 를 모두 받는다 —
 * 개편 화면에서는 "상품 등록" 버튼 + "필터 초기화" 링크처럼 액션이 둘 이상인 경우가 있다.
 */
interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: { label: string; onClick: () => void } | React.ReactNode;
  className?: string;
}

function isActionSpec(
  value: unknown,
): value is { label: string; onClick: () => void } {
  return (
    typeof value === "object" &&
    value !== null &&
    "label" in value &&
    "onClick" in value &&
    typeof (value as { onClick: unknown }).onClick === "function"
  );
}

export function EmptyState({ icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        "flex flex-col items-center justify-center gap-2 py-12 text-center",
        className,
      )}
    >
      {icon && <div className="text-[--color-muted-foreground]" aria-hidden>{icon}</div>}
      <p className="text-sm font-medium text-[--color-foreground]">{title}</p>
      {description && (
        <p className="max-w-md text-xs leading-relaxed text-[--color-muted-foreground]">
          {description}
        </p>
      )}
      {action ? (
        isActionSpec(action) ? (
          <Button size="sm" onClick={action.onClick} className="mt-2">
            {action.label}
          </Button>
        ) : (
          <div className="mt-2 flex items-center gap-2">{action}</div>
        )
      ) : null}
    </div>
  );
}
