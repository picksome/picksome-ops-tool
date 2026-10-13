import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn/ui 표준 className 결합 helper. Tailwind 충돌 규칙 자동 병합. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
