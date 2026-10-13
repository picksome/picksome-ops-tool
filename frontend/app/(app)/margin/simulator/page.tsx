import { redirect } from "next/navigation";

/**
 * 개편으로 이동한 화면 — 등록가 시뮬레이션 → 마진 계산기(가격 역산 모드)
 * 라우트를 삭제하지 않고 리다이렉트로 남긴다. 북마크와 외부 링크가 살아 있고,
 * typedRoutes 가 켜져 있어 남아 있는 참조가 있으면 빌드가 알려준다.
 */
export default function Page() {
  redirect("/tools/calculator?mode=price");
}
