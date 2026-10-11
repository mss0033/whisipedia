/**
 * Global setup for the live target: wake the library before the first test,
 * so a cold NAS does not eat the per-test timeouts. Read-only GETs only.
 */
import type { FullConfig } from '@playwright/test';

const PATHS = ['/v1/health', '/v1/sources?limit=200', '/v1/portal', '/v1/directory?path='];

export default async function warmUp(config: FullConfig): Promise<void> {
  const base = config.projects[0]?.use.baseURL;
  if (!process.env.WSP_E2E_URL || !base) return;
  for (const path of PATHS) {
    const started = Date.now();
    try {
      const res = await fetch(new URL(path, base), { signal: AbortSignal.timeout(180_000) });
      await res.arrayBuffer();
      console.log(
        `warm-up ${path}: ${res.status} in ${((Date.now() - started) / 1000).toFixed(1)} s`,
      );
    } catch (err) {
      throw new Error(`The live library at ${base} did not answer ${path}: ${String(err)}`);
    }
  }
}
