"use client";

import { useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { LogOut, Menu, Search, Settings, User } from "lucide-react";
import { useAuthStore } from "@/stores/auth";
import { Button } from "@/components/ui/button";
import { NotificationBell } from "@/components/shell/notification-bell";
import { CmdKSearch } from "@/components/shell/cmd-k-search";
import { MobileNav } from "@/components/shell/sidebar";
import { Sheet, SheetContent, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { api } from "@/lib/api";

/**
 * 상단바 (높이 64px 고정 — --layout-header-height).
 *
 * 브랜드는 헤더 좌측에 둔다. 사이드바가 접히거나(64px) 모바일에서 사라져도
 * 제품 정체성이 유지되어야 하기 때문이다.
 */
export function Header() {
  const { user, logout } = useAuthStore();
  const router = useRouter();
  const [mobileNavOpen, setMobileNavOpen] = useState(false);

  async function handleLogout() {
    try {
      await api.post("/auth/logout");
    } catch {
      // 실패해도 로컬 토큰 제거 후 이동
    }
    logout();
    router.push("/login");
  }

  return (
    <header
      className="flex shrink-0 items-center gap-3 border-b border-[--color-border] bg-[--color-card] px-4"
      style={{ height: "var(--layout-header-height)" }}
    >
      {/* 모바일 메뉴 토글 */}
      <Button
        variant="ghost"
        size="icon"
        className="md:hidden"
        onClick={() => setMobileNavOpen(true)}
        aria-label="메뉴 열기"
      >
        <Menu className="h-5 w-5" aria-hidden />
      </Button>

      <Link
        href="/dashboard"
        className="ops-focusable flex items-center gap-2.5"
        aria-label="PICKSOME OPS TOOL 홈"
      >
        <span
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-[--color-accent] text-[13px] font-bold text-white"
          aria-hidden
        >
          P
        </span>
        <span className="hidden leading-tight sm:block">
          <span className="block text-[13px] font-bold">PICKSOME</span>
          <span className="block text-[10px] text-[--color-muted-foreground]">OPS TOOL</span>
        </span>
      </Link>

      <div className="flex-1" />

      <button
        type="button"
        onClick={() =>
          document.dispatchEvent(
            new KeyboardEvent("keydown", { metaKey: true, key: "k", bubbles: true }),
          )
        }
        className="ops-focusable hidden items-center gap-2 rounded-md border border-[--color-border] bg-[--color-background] px-3 py-1.5 text-xs text-[--color-muted-foreground] transition-colors hover:border-[--color-accent] hover:text-[--color-foreground] sm:flex"
        title="통합 검색 (Ctrl+K)"
      >
        <Search className="h-3.5 w-3.5" aria-hidden />
        <span>상품·주문·비용 검색</span>
        <kbd className="rounded bg-[--color-border] px-1 py-0.5 font-mono text-[10px]">⌘K</kbd>
      </button>

      <CmdKSearch />
      <NotificationBell />

      <div className="flex items-center gap-1 border-l border-[--color-border] pl-3">
        <Link
          href="/settings/profile"
          className="ops-focusable flex items-center gap-2 rounded-md px-2 py-1 hover:bg-[--color-muted]"
          title="내 프로필"
        >
          <User className="h-4 w-4 text-[--color-muted-foreground]" aria-hidden />
          <span className="hidden text-sm sm:inline">{user?.display_name ?? "-"}</span>
        </Link>
        <Button variant="ghost" size="icon" asChild title="설정">
          <Link href="/settings/profile" aria-label="설정">
            <Settings className="h-4 w-4" aria-hidden />
          </Link>
        </Button>
        <Button variant="ghost" size="icon" onClick={handleLogout} title="로그아웃" aria-label="로그아웃">
          <LogOut className="h-4 w-4" aria-hidden />
        </Button>
      </div>

      <Sheet open={mobileNavOpen} onOpenChange={setMobileNavOpen}>
        <SheetContent side="left" className="[--sheet-w:280px] w-[280px] p-0">
          <SheetHeader className="border-b border-[--color-border] p-4">
            <SheetTitle>메뉴</SheetTitle>
          </SheetHeader>
          <div className="overflow-y-auto">
            <MobileNav onNavigate={() => setMobileNavOpen(false)} />
          </div>
        </SheetContent>
      </Sheet>
    </header>
  );
}
