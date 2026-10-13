"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useCurrentUser } from "@/hooks/useCurrentUser";
import { isAuthenticated } from "@/lib/auth";

/**
 * 인증 가드.
 * 토큰이 없으면 즉시 /login 으로 이동. 토큰이 있으면 /me 조회 후 user 복구.
 */
export function AppGuard({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { user, isLoading } = useCurrentUser();

  useEffect(() => {
    if (!isAuthenticated()) {
      router.push("/login");
    }
  }, [router]);

  useEffect(() => {
    if (!isLoading && !user && !isAuthenticated()) {
      router.push("/login");
    }
  }, [isLoading, user, router]);

  if (isLoading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-[--color-primary] border-t-transparent" />
      </div>
    );
  }

  return <>{children}</>;
}
