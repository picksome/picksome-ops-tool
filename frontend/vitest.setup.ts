import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

// 테스트마다 DOM 을 비운다. 안 그러면 앞 테스트의 노드가 남아
// `getByText` 가 엉뚱한 것을 잡고, 실패 원인이 테스트 순서에 따라 달라진다.
afterEach(() => {
  cleanup();
});

// jsdom 에 없는 브라우저 API — radix/shadcn 컴포넌트가 마운트 시 호출한다.
if (!window.matchMedia) {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false, media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
}
if (!window.ResizeObserver) {
  window.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}
