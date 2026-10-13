"use client";

import { create } from "zustand";
import { clearTokens } from "@/lib/auth";

export type UserRole =
  | "ADMIN"
  | "OPERATOR"
  | "SOURCING"
  | "ACCOUNTING"
  | "READONLY"
  | "EXTERNAL_ACCOUNTANT";

export interface CurrentUser {
  id: string;
  org_id: string;
  email: string;
  display_name: string;
  role: UserRole;
  is_active: boolean;
}

interface AuthStore {
  user: CurrentUser | null;
  setUser: (user: CurrentUser | null) => void;
  logout: () => void;
}

export const useAuthStore = create<AuthStore>((set) => ({
  user: null,
  setUser: (user) => set({ user }),
  logout: () => {
    clearTokens();
    set({ user: null });
  },
}));
