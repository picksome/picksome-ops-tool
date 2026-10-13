"use client";

import { X } from "lucide-react";
import { Button } from "@/components/ui/button";

interface BulkAction {
  label: string;
  variant?: "default" | "destructive" | "outline";
  onClick: () => void;
}

interface BulkActionBarProps {
  selectedCount: number;
  actions: BulkAction[];
  onClear: () => void;
}

export function BulkActionBar({ selectedCount, actions, onClear }: BulkActionBarProps) {
  if (selectedCount === 0) return null;

  return (
    <div className="flex items-center gap-3 rounded-lg border border-[--color-accent]/30 bg-[--color-info-bg] px-4 py-2.5">
      <span className="text-sm font-medium text-[--color-accent]">
        {selectedCount}개 선택됨
      </span>
      <div className="h-4 w-px bg-[--color-border]" />
      <div className="flex items-center gap-2">
        {actions.map((action) => (
          <Button
            key={action.label}
            size="sm"
            variant={action.variant ?? "outline"}
            onClick={action.onClick}
          >
            {action.label}
          </Button>
        ))}
      </div>
      <button
        onClick={onClear}
        className="ml-auto text-[--color-muted-foreground] hover:text-[--color-foreground]"
        aria-label="선택 해제"
      >
        <X className="h-4 w-4" />
      </button>
    </div>
  );
}
