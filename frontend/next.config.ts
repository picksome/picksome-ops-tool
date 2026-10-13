import type { NextConfig } from "next";

/**
 * Next.js 설정.
 *
 * - `rewrites`: 개발 환경에서 /api/* 요청을 백엔드(FastAPI)로 프록시.
 *   브라우저 단에서 별도 CORS 처리를 피하고 통일된 origin 으로 요청 (docs/22_api_spec.md §1-1).
 * - `BACKEND_ORIGIN` 환경변수 (.env) 에서 백엔드 주소 결정. 기본은 docker-compose 내부 호스트.
 */
const backendOrigin = process.env.BACKEND_ORIGIN ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: process.env.NODE_ENV === "production" ? "standalone" : undefined,
  experimental: {
    typedRoutes: true,
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${backendOrigin}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
