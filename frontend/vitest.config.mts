import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tsconfigPaths from "vite-tsconfig-paths";

/**
 * 프론트 단위 테스트.
 *
 * `tsc` 와 `next build` 는 **타입이 맞는지**만 본다. 값이 맞는지는 보지 않는다.
 * 원가가 승인 경로로 가는지, 빈 문자열과 null 을 구분하는지 같은 것은
 * 타입 검사로는 절대 잡히지 않는다.
 *
 * Next.js 플러그인이 아니라 vite 로 도는 이유: 순수 로직(분류·diff·오류 매핑)이
 * 주 대상이고, 그건 서버 런타임이 필요 없다. 서버 컴포넌트 렌더링이 필요해지면
 * 그때 환경을 나눈다.
 */
export default defineConfig({
  plugins: [react(), tsconfigPaths()],
  // tsconfig 의 `jsx: "preserve"` 는 Next 빌드용이다. 테스트에서는 esbuild 가
  // 직접 변환하므로 automatic runtime 을 명시해야 한다 — 안 하면 classic 으로 떨어져
  // `import React` 가 없는 컴포넌트마다 "React is not defined" 가 난다.
  esbuild: { jsx: "automatic" },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["**/*.test.{ts,tsx}"],
    exclude: ["node_modules/**", ".next/**", "e2e/**"],
    coverage: {
      provider: "v8",
      include: ["lib/**/*.ts", "components/**/*.tsx"],
      exclude: ["lib/adapters/mock-data.ts", "**/*.d.ts"],
    },
  },
});
