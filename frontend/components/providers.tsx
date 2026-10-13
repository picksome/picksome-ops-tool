"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { useState } from "react";
import { Toaster } from "sonner";
import { TooltipProvider } from "@/components/ui/tooltip";

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { staleTime: 30_000, retry: 1 },
        },
      }),
  );

  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider attribute="data-theme" defaultTheme="light" disableTransitionOnChange>
        <TooltipProvider delayDuration={300}>
        {children}
        </TooltipProvider>
        <Toaster
          position="top-right"
          richColors
          duration={4000}
          toastOptions={{
            style: { fontFamily: "var(--font-sans)", fontSize: "13px" },
          }}
        />
      </ThemeProvider>
    </QueryClientProvider>
  );
}
