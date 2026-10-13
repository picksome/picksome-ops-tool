import { redirect } from "next/navigation";

/**
 * 개편으로 이동한 화면 — 옵션/Variant → 상품 목록의 옵션 모달
 * 라우트를 삭제하지 않고 리다이렉트로 남긴다. 북마크와 외부 링크가 살아 있고,
 * typedRoutes 가 켜져 있어 남아 있는 참조가 있으면 빌드가 알려준다.
 */
export default function Page() {
  redirect("/products");
}
