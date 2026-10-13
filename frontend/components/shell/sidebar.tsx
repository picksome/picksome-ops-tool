"use client";

import type { Route } from "next";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  Link2,
  ListChecks,
  Package,
  BookOpen,
  Receipt,
  FileText,
  PieChart,
  Bell,
  MessageSquare,
  Plug,
  UserCircle,
  Users,
  Landmark,
  Tag,
  Percent,
  Calculator,
  ChevronLeft,
  ChevronRight,
  ChevronDown,
  ClipboardCheck,
  ClipboardList,
  CalendarCheck,
  Wallet,
  Warehouse,
  TriangleAlert,
  Activity,
  Banknote,
  Factory,
  Boxes,
  Bot,
  Sparkles,
  Sunrise,
  ShieldCheck,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useUIStore } from "@/stores/ui";
import { useCurrentUser } from "@/hooks/useCurrentUser";
import type { UserRole } from "@/stores/auth";

/**
 * 사이드바 정보구조 (docs/41_redesign_worklog.md §3).
 *
 * 26개 항목이 8그룹에 흩어져 있던 것을 6그룹 16항목으로 재편했다.
 * 재편 기준은 "셀러의 업무 흐름" 이다 — 상품을 등록하고(2) 팔고(3) 정산·비용을 맞추고(4)
 * 자동화를 붙이고(5) 설정을 만진다(6).
 *
 * 제거·통합:
 *   아이템리스트          → 상품 관리 (status=CANDIDATE 탭)
 *   옵션/Variant          → 상품 관리 (옵션 수 클릭 → modal)
 *   등록가 시뮬레이션      → 도구 > 마진 계산기 (모드 전환)
 *   실마진 분석           → 판매장부 (view=margin)
 *   증빙 관리             → 사업비용 (tab=evidence)
 *   정책 5화면 / 관리자 5화면 → 설정 5항목
 *
 * 최상위 단일 항목(대시보드/상품)은 그룹 헤더 없이 노출한다 —
 * 항목이 1개인 그룹 헤더는 스캔만 방해한다.
 */

type NavLeaf = {
  path: Route;
  icon: React.ElementType;
  label: string;
  /** 정확히 일치할 때만 active (하위 경로가 다른 메뉴인 경우) */
  exact?: boolean;
  /**
   * 이 항목을 볼 수 있는 역할. 없으면 전원에게 보인다.
   *
   * **메뉴를 숨기는 것이 권한이 아니다** — 라우터가 진짜로 막는다. 여기서
   * 거르는 이유는 다르다: 눌러 봐야 403 이 나는 메뉴를 보여주면 사용자는
   * 자기 권한을 오해하고 "고장났다" 고 판단한다.
   *
   * 값은 백엔드 `require_roles(...)` 와 같아야 한다.
   * 어긋나면 test_permission_matrix 가 먼저 깨진다.
   */
  roles?: readonly UserRole[];
};

/** 금액이 드러나는 화면. 지급 장부와 같은 등급이다. */
const FINANCE_ROLES = ["ADMIN", "ACCOUNTING"] as const;

type NavSection =
  | { kind: "item"; item: NavLeaf }
  | { kind: "group"; group: string; items: NavLeaf[] };

const NAV: NavSection[] = [
  {
    kind: "item",
    item: { path: "/dashboard", icon: LayoutDashboard, label: "대시보드" },
  },
  {
    kind: "item",
    item: { path: "/products", icon: Package, label: "상품 관리" },
  },
  {
    kind: "group",
    group: "소싱",
    items: [
      // 매입가는 민감 정보라 라우터가 별도로 막는다. 메뉴는 소싱 담당과
      // 관리자에게만 보인다 — 눌러야 403 이 나는 메뉴를 두지 않는다.
      {
        path: "/sourcing/suppliers", icon: Factory, label: "공급처",
        roles: ["ADMIN", "SOURCING"],
      },
      // 재고와 재주문은 한 화면이다 — "몇 개 남았나" 다음에 오는 질문이
      // 곧바로 "언제 주문하나" 라서 나누면 두 번 열어야 한다.
      // 정산 담당도 본다: 재고 평가액이 결산에 들어간다(바꾸지는 못한다).
      // 재주문이 NO_SOURCE 로 막히는 것을 푸는 화면. 공급처 바로 아래 둔다.
      {
        path: "/sourcing/import", icon: Link2, label: "소싱 연결",
        roles: ["ADMIN", "SOURCING"],
      },
      {
        path: "/inventory", icon: Warehouse, label: "재고·재주문",
        roles: ["ADMIN", "OPERATOR", "SOURCING", "ACCOUNTING"],
      },
      {
        path: "/purchase-orders", icon: ClipboardList, label: "발주",
        roles: ["ADMIN", "OPERATOR", "SOURCING"],
      },
    ],
  },
  {
    kind: "group",
    group: "판매 관리",
    items: [{ path: "/sales", icon: BookOpen, label: "판매장부" }],
  },
  {
    kind: "group",
    group: "정산·재무",
    items: [
      { path: "/settlements/marketplace", icon: FileText, label: "쇼핑몰 정산" },
      { path: "/expenses", icon: Receipt, label: "사업비용" },
      // 월 마감은 이 흐름의 종착점인데 사이드바에 없어서 URL 을 직접 쳐야
      // 들어갈 수 있었다. 판매·비용 다음, 배분 앞에 둔다 — 실제 순서가 그렇다.
      { path: "/settlements/monthly", icon: CalendarCheck, label: "월 마감" },
      {
        path: "/settlements/distribution", icon: PieChart, label: "수익배분",
        roles: ["ADMIN", "ACCOUNTING", "OPERATOR"],
      },
      { path: "/payouts", icon: Wallet, label: "지급 장부", roles: FINANCE_ROLES },
      // 손익(월 마감)과 현금은 다른 질문이다. 지급 장부 바로 뒤에 둔다 —
      // "얼마를 줘야 하나" 다음에 오는 질문이 "줄 돈이 통장에 있나" 다.
      { path: "/finance/cashflow", icon: Banknote, label: "현금흐름", roles: FINANCE_ROLES },
    ],
  },
  {
    kind: "group",
    group: "운영",
    items: [
      // 운영 이슈는 "지금 뭐가 잘못돼 있나" 를 묻는 화면이라 승인과 성격이 같다.
      // 아침에 처음 여는 화면. "오늘 내가 손댈 것" 이 맨 위에 온다 —
      // 이슈보다 앞에 두는 이유는 이슈·승인·마진을 한 장에 모으기 때문이다.
      { path: "/operations/daily", icon: Sunrise, label: "오늘의 운영" },
      // 오늘의 운영이 "요약" 이면 여기는 "목록" 이다. 같은 읽기 모델을 쓰기
      // 때문에 두 화면의 숫자가 갈라지지 않는다.
      { path: "/operations/exceptions", icon: ListChecks, label: "확인 필요" },
      // "화면 숫자가 맞나" 를 묻는 자리. 확인 필요 바로 아래 둔다 — 대사에서
      // 걸린 것이 곧 확인할 목록이 되므로 두 화면을 오간다.
      { path: "/operations/verification", icon: ShieldCheck, label: "검증센터" },
      { path: "/operations/issues", icon: TriangleAlert, label: "운영 이슈" },
      // worker 가 멈춘 것을 알아차릴 방법이 없어서 URL 을 직접 쳐야 했다.
      // 이슈 바로 아래 둔다 — "이슈가 안 생긴다" 의 원인이 대개 여기다.
      { path: "/operations/system", icon: Activity, label: "시스템 상태" },
    ],
  },
  {
    kind: "item",
    // 승인은 "내가 처리해야 할 일" 이라 알림과 성격이 같다.
    // 건수가 적을 때 독립 그룹으로 두면 과해서 최상위 단일 항목으로 둔다.
    item: { path: "/approvals", icon: ClipboardCheck, label: "승인" },
  },
  {
    kind: "group",
    group: "알림·연동",
    items: [
      { path: "/notifications", icon: Bell, label: "알림센터" },
      { path: "/integrations/messaging", icon: MessageSquare, label: "메신저 연결" },
      { path: "/integrations/marketplaces", icon: Plug, label: "쇼핑몰 API 연결" },
      // 판매처 계정과 다른 개념이다 — 셀러툴처럼 우리가 나가서 호출하는 시스템.
      // 자격증명을 다루므로 관리자만 본다.
      {
        path: "/integrations/external", icon: Boxes, label: "외부 시스템",
        roles: ["ADMIN"],
      },
    ],
  },
  {
    kind: "group",
    group: "설정",
    items: [
      { path: "/settings/profile", icon: UserCircle, label: "내 프로필" },
      { path: "/settings/members", icon: Users, label: "구성원 및 권한" },
      { path: "/settings/tax", icon: Landmark, label: "사업자·세금 설정" },
      { path: "/settings/expense-categories", icon: Tag, label: "비용 카테고리" },
      { path: "/settings/fee-policies", icon: Percent, label: "수수료 정책" },
      // 자동화와 AI 접근을 나눈다. 합치면 "AI 를 안 켜면 자동화도 안 되나" 로
      // 읽힌다 — 자동화는 AI 없이 돈다.
      { path: "/settings/automation", icon: Bot, label: "자동화" },
      {
        path: "/settings/ai-api", icon: Sparkles, label: "AI · API",
        roles: ["ADMIN"],
      },
    ],
  },
];

/** 사이드바 최하단 보조 도구 — 주 업무 흐름이 아니므로 본 메뉴와 분리한다. */
const TOOLS: NavLeaf[] = [
  { path: "/tools/calculator", icon: Calculator, label: "마진 계산기" },
];

function isActive(pathname: string, item: NavLeaf): boolean {
  if (item.exact) return pathname === item.path;
  return pathname === item.path || pathname.startsWith(item.path + "/");
}

function NavLink({
  item,
  collapsed,
  active,
}: {
  item: NavLeaf;
  collapsed: boolean;
  active: boolean;
}) {
  const Icon = item.icon;
  return (
    <Link
      href={item.path}
      title={collapsed ? item.label : undefined}
      aria-current={active ? "page" : undefined}
      className={cn(
        "ops-focusable flex items-center gap-2.5 rounded-md px-2 py-1.5 text-[13px] transition-colors",
        collapsed && "justify-center px-0",
        active
          ? "bg-[--color-sidebar-active] font-medium text-[--color-sidebar-active-foreground]"
          : "text-[--color-sidebar-foreground] hover:bg-[--color-sidebar-hover]",
      )}
    >
      <Icon className="h-4 w-4 shrink-0" aria-hidden />
      {!collapsed && <span className="flex-1 truncate">{item.label}</span>}
      {/* 색(배경)만으로 현재 위치를 표시하지 않는다 — 좌측 바를 함께 둔다 */}
      {active && !collapsed && (
        <span className="h-4 w-[3px] rounded-full bg-current opacity-70" aria-hidden />
      )}
    </Link>
  );
}

/**
 * 역할이 확인되기 전에는 제한 항목을 보여주지 않는다.
 *
 * 로딩 중에 잠깐 보였다가 사라지면 사용자는 메뉴가 깜빡였다고 느끼고,
 * 더 나쁘게는 "아까 있었는데" 하고 찾게 된다. 모를 때는 감추는 쪽을 고른다.
 */
function visibleTo(role: UserRole | null, item: NavLeaf): boolean {
  if (!item.roles) return true;
  return role !== null && item.roles.includes(role);
}

function visibleItems(role: UserRole | null, items: NavLeaf[]): NavLeaf[] {
  return items.filter((i) => visibleTo(role, i));
}

export function Sidebar() {
  const pathname = usePathname();
  const { sidebarCollapsed, toggleSidebar } = useUIStore();
  const { user } = useCurrentUser();
  const role = user?.role ?? null;

  return (
    <aside
      aria-label="주 메뉴"
      className={cn(
        "relative hidden h-full shrink-0 flex-col border-r border-[--color-sidebar-border] bg-[--color-sidebar] transition-[width] duration-200 md:flex",
      )}
      style={{
        width: sidebarCollapsed
          ? "var(--layout-sidebar-collapsed-width)"
          : "var(--layout-sidebar-width)",
      }}
    >
      <nav className="flex-1 overflow-y-auto py-3">
        {NAV.map((section, idx) =>
          section.kind === "item" ? (
            !visibleTo(role, section.item) ? null : (
            <ul key={section.item.path} className="mb-1 px-2">
              <li>
                <NavLink
                  item={section.item}
                  collapsed={sidebarCollapsed}
                  active={isActive(pathname, section.item)}
                />
              </li>
            </ul>
            )
          ) : visibleItems(role, section.items).length === 0 ? (
            // 항목이 전부 걸러진 그룹은 헤더만 남는다 — 빈 제목은 스캔만 방해한다.
            null
          ) : (
            <div key={section.group} className="mb-1">
              {!sidebarCollapsed ? (
                <p className="mb-1 px-3 pt-2 text-[11px] font-semibold tracking-wide text-[--color-sidebar-muted]">
                  {section.group}
                </p>
              ) : (
                idx > 0 && <div className="mx-3 my-2 border-t border-[--color-sidebar-border]" />
              )}
              <ul className="space-y-0.5 px-2">
                {visibleItems(role, section.items).map((item) => (
                  <li key={item.path}>
                    <NavLink
                      item={item}
                      collapsed={sidebarCollapsed}
                      active={isActive(pathname, item)}
                    />
                  </li>
                ))}
              </ul>
            </div>
          ),
        )}
      </nav>

      <div className="border-t border-[--color-sidebar-border] py-2">
        <ul className="space-y-0.5 px-2">
          {TOOLS.map((item) => (
            <li key={item.path}>
              <NavLink
                item={item}
                collapsed={sidebarCollapsed}
                active={isActive(pathname, item)}
              />
            </li>
          ))}
        </ul>
      </div>

      <button
        onClick={toggleSidebar}
        className="ops-focusable absolute -right-3 top-5 flex h-6 w-6 items-center justify-center rounded-full border border-[--color-sidebar-border] bg-[--color-sidebar] text-[--color-sidebar-foreground] shadow-sm hover:bg-[--color-sidebar-hover]"
        aria-label={sidebarCollapsed ? "사이드바 펼치기" : "사이드바 접기"}
      >
        {sidebarCollapsed ? (
          <ChevronRight className="h-3 w-3" aria-hidden />
        ) : (
          <ChevronLeft className="h-3 w-3" aria-hidden />
        )}
      </button>
    </aside>
  );
}

/**
 * 모바일용 메뉴 — 사이드바는 md 미만에서 숨기고 상단바의 드로어로 대체한다.
 * 좁은 화면에서 240px 사이드바는 콘텐츠 폭을 절반 가까이 먹는다.
 */
export function MobileNav({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  return (
    <nav aria-label="주 메뉴 (모바일)" className="flex flex-col gap-1 p-2">
      {NAV.map((section) =>
        section.kind === "item" ? (
          <Link
            key={section.item.path}
            href={section.item.path}
            onClick={onNavigate}
            className={cn(
              "ops-focusable flex items-center gap-2.5 rounded-md px-3 py-2 text-sm",
              isActive(pathname, section.item)
                ? "bg-[--color-accent-bg] font-medium text-[--color-accent]"
                : "hover:bg-[--color-muted]",
            )}
          >
            <section.item.icon className="h-4 w-4" aria-hidden />
            {section.item.label}
          </Link>
        ) : (
          <details key={section.group} open className="group">
            <summary className="ops-focusable flex cursor-pointer list-none items-center justify-between rounded-md px-3 py-2 text-xs font-semibold text-[--color-muted-foreground]">
              {section.group}
              <ChevronDown className="h-3 w-3 transition-transform group-open:rotate-180" aria-hidden />
            </summary>
            <div className="flex flex-col gap-0.5 pl-2">
              {section.items.map((item) => (
                <Link
                  key={item.path}
                  href={item.path}
                  onClick={onNavigate}
                  className={cn(
                    "ops-focusable flex items-center gap-2.5 rounded-md px-3 py-2 text-sm",
                    isActive(pathname, item)
                      ? "bg-[--color-accent-bg] font-medium text-[--color-accent]"
                      : "hover:bg-[--color-muted]",
                  )}
                >
                  <item.icon className="h-4 w-4" aria-hidden />
                  {item.label}
                </Link>
              ))}
            </div>
          </details>
        ),
      )}
      <div className="mt-2 border-t border-[--color-border] pt-2">
        {TOOLS.map((item) => (
          <Link
            key={item.path}
            href={item.path}
            onClick={onNavigate}
            className="ops-focusable flex items-center gap-2.5 rounded-md px-3 py-2 text-sm hover:bg-[--color-muted]"
          >
            <item.icon className="h-4 w-4" aria-hidden />
            {item.label}
          </Link>
        ))}
      </div>
    </nav>
  );
}
