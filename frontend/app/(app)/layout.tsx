import { Header } from "@/components/shell/header";
import { Sidebar } from "@/components/shell/sidebar";
import { AppGuard } from "@/components/shell/app-guard";

/**
 * 앱 셸 — 상단바 64px + 좌측 사이드바 240px + 콘텐츠.
 *
 * 콘텐츠 패딩을 여기서 주지 않는다. 목록 화면은 필터 바가 상단에 sticky 로 붙어야 해서
 * 바깥 패딩이 있으면 스크롤 시 배경이 비친다. 여백은 `PageShell` 이 화면별로 책임진다.
 */
export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <AppGuard>
      <div className="flex h-screen flex-col overflow-hidden bg-[--color-background]">
        <a
          href="#main-content"
          className="sr-only-focusable absolute left-2 top-2 z-[100] rounded-md bg-[--color-accent] px-3 py-2 text-sm text-white"
        >
          본문으로 건너뛰기
        </a>
        <Header />
        <div className="flex flex-1 overflow-hidden">
          <Sidebar />
          <main id="main-content" className="flex-1 overflow-y-auto">
            {children}
          </main>
        </div>
      </div>
    </AppGuard>
  );
}
