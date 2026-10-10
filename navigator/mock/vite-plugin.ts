/**
 * Vite plugin that serves the mock API under /v1 in `vite` (dev) and
 * `vite preview`. Disable with WSP_MOCK=0 to proxy a real daemon instead
 * (see vite.config.ts).
 */
import type { IncomingMessage, ServerResponse } from 'node:http';
import type { Connect, Plugin } from 'vite';
import { handle } from './handler.ts';

async function readBody(req: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  if (chunks.length === 0) return undefined;
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    return undefined;
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export function mockMiddleware(latencyMs = 25): Connect.NextHandleFunction {
  return (req: IncomingMessage, res: ServerResponse, next: Connect.NextFunction) => {
    const url = new URL(req.url ?? '/', 'http://mock.invalid');
    if (!url.pathname.startsWith('/v1/')) {
      next();
      return;
    }
    void (async () => {
      const body = req.method === 'POST' ? await readBody(req) : undefined;
      const result = handle(req.method ?? 'GET', url.pathname, url.searchParams, body);
      await sleep(latencyMs);
      if ('stream' in result) {
        res.writeHead(200, {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-store',
          Connection: 'keep-alive',
        });
        let closed = false;
        req.on('close', () => {
          closed = true;
        });
        for (const { delayMs, event } of result.stream) {
          if (closed) return;
          await sleep(delayMs);
          res.write(`data: ${JSON.stringify(event)}\n\n`);
        }
        res.end();
        return;
      }
      res.writeHead(result.status, {
        'Content-Type': 'application/json; charset=utf-8',
        'Cache-Control': 'no-store',
      });
      res.end(JSON.stringify(result.json));
    })().catch((err: unknown) => {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: { code: 'mock_error', message: String(err) } }));
    });
  };
}

export function wspMockApi(): Plugin {
  return {
    name: 'wsp-mock-api',
    configureServer(server) {
      server.middlewares.use(mockMiddleware());
    },
    configurePreviewServer(server) {
      server.middlewares.use(mockMiddleware());
    },
  };
}
