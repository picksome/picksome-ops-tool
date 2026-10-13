import { redirect } from "next/navigation";

/**
 * 개편으로 이동한 화면 — 아이템리스트 → 상품 관리 소싱검토 탭
 * 라우트를 삭제하지 않고 리다이렉트로 남긴다. 북마크와 외부 링크가 살아 있고,
 * typedRoutes 가 켜져 있어 남아 있는 참조가 있으면 빌드가 알려준다.
 */
export default function Page() {
  redirect("/products?status=CANDIDATE");
}
