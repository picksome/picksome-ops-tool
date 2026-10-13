import * as React from "react"
import { cn } from "@/lib/utils"

export interface InputProps extends React.ComponentProps<"input"> {
  /** 우측 단위 텍스트 (예: "원", "%") */
  suffix?: string;
}

function Input({ className, type, suffix, ...props }: InputProps) {
  if (suffix) {
    return (
      <div className="relative flex items-center">
        <input
          type={type}
          data-slot="input"
          className={cn(
            "h-8 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-1 pr-8 text-right tabular-nums text-base transition-colors outline-none placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50 md:text-sm",
            className
          )}
          {...props}
        />
        <span className="pointer-events-none absolute right-2.5 text-xs text-muted-foreground">
          {suffix}
        </span>
      </div>
    );
  }

  return (
    <input
      type={type}
      data-slot="input"
      className={cn(
        "h-8 w-full min-w-0 rounded-lg border border-input bg-transparent px-2.5 py-1 text-base transition-colors outline-none file:inline-flex file:h-6 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 disabled:pointer-events-none disabled:opacity-50 md:text-sm",
        className
      )}
      {...props}
    />
  )
}

export { Input }
