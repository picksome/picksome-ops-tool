"use client";

import { toast as sonnerToast } from "sonner";

/** Sonner 기반 toast helper. 직접 sonner 를 import 해도 되지만 이 wrapper 로 통일한다. */
export function useToast() {
  return {
    toast: (message: string) => sonnerToast(message),
    success: (message: string) => sonnerToast.success(message),
    error: (message: string) => sonnerToast.error(message),
    warning: (message: string) => sonnerToast.warning(message),
    promise: <T,>(
      promise: Promise<T>,
      messages: { loading: string; success: string; error: string },
    ) => sonnerToast.promise(promise, messages),
  };
}

// 훅 없이 직접 호출할 때 (mutation onSuccess 등)
export { sonnerToast as toast };
