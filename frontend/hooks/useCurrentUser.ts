"use client";

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAuthStore, type CurrentUser } from "@/stores/auth";

export function useCurrentUser() {
  const { user, setUser } = useAuthStore();

  const query = useQuery<CurrentUser>({
    queryKey: ["me"],
    queryFn: () => api.get<CurrentUser>("/auth/me"),
    retry: false,
    staleTime: 5 * 60 * 1000,
  });

  useEffect(() => {
    if (query.data) setUser(query.data);
    if (query.error) setUser(null);
  }, [query.data, query.error, setUser]);

  return { user: user ?? query.data ?? null, isLoading: query.isLoading };
}
