/// <reference types="vitest/config" />
import { defineConfig, type Plugin } from 'vite';
import { wspMockApi } from './mock/vite-plugin.ts';

/**
 * Content Security Policy for the built bundle (PLAN §10.1, §10.5). The
 * daemon should send the same policy as a header; the meta tag is a second
 * line of defence when dist/ is served by something else.
 */
export const CSP = [
  "default-src 'self'",
  "base-uri 'none'",
  "object-src 'none'",
  "form-action 'self'",
].join('; ');

function cspMeta(): Plugin {
  return {
    name: 'wsp-csp-meta',
    apply: 'build',
    transformIndexHtml() {
      return [
        {
          tag: 'meta',
          attrs: { 'http-equiv': 'Content-Security-Policy', content: CSP },
          injectTo: 'head-prepend',
        },
      ];
    },
  };
}

const useMock = process.env.WSP_MOCK !== '0';
const apiTarget = process.env.WSP_API ?? 'http://127.0.0.1:8737'; // `wsp serve` default

export default defineConfig({
  base: '/',
  plugins: [cspMeta(), ...(useMock ? [wspMockApi()] : [])],
  server: {
    host: '127.0.0.1',
    port: 5173,
    ...(useMock ? {} : { proxy: { '/v1': { target: apiTarget, changeOrigin: false } } }),
  },
  preview: {
    host: '127.0.0.1',
    port: 4173,
    headers: { 'Content-Security-Policy': CSP },
    ...(useMock ? {} : { proxy: { '/v1': { target: apiTarget, changeOrigin: false } } }),
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2022',
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    sourcemap: false,
    reportCompressedSize: false,
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.ts'],
  },
});
