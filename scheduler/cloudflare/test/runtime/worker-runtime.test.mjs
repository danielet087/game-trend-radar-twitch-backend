import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { Miniflare, convertV4MiniflareOptions } from 'miniflare';

const sourcePath = process.env.WORKER_SOURCE || new URL('../../src/worker.mjs', import.meta.url);
const source = await readFile(sourcePath, 'utf8');
// Execute the production helper with real workerd Request/fetch semantics.
// Only the test entry point is replaced; ALL outbound requests are intercepted.
const testSource = source.replace('export default {', 'const scheduledWorker = {') + `
export default {
  async fetch(request, env) {
    try {
      return Response.json(await checkAndDispatch(env, { now: Date.parse('2026-09-30T02:25:00Z') }));
    } catch (error) {
      return Response.json({ name: error.name, code: error.code, message: error.message });
    }
  },
};
`;

async function withRuntime(receiptStatus, callback) {
  const requests = [];
  const runtime = new Miniflare(convertV4MiniflareOptions({
    modules: true,
    script: testSource,
    compatibilityDate: '2026-09-29',
    bindings: { GITHUB_ACTIONS_TOKEN: 'runtime-test-token-not-a-real-secret' },
    outboundService: async (request) => {
      const url = new URL(request.url);
      const record = { method: request.method, url: request.url, authorization: request.headers.get('Authorization') };
      if (request.method === 'POST') record.body = await request.json();
      requests.push(record);
      if (url.hostname === 'raw.githubusercontent.com' && url.pathname.endsWith('/data/twitch_collection_status.json')) {
        return new Response(null, { status: receiptStatus, ...(receiptStatus === 302 ? { headers: { Location: 'https://fixture.invalid/redirect-target' } } : {}) });
      }
      if (url.hostname === 'api.github.com' && url.pathname === '/repos/danielet087/game-trend-radar-twitch-backend/actions/workflows/369223512/runs') {
        return Response.json({ total_count: 0, workflow_runs: [] });
      }
      if (url.hostname === 'api.github.com' && url.pathname === '/repos/danielet087/game-trend-radar-twitch-backend/actions/workflows/369223512/dispatches' && request.method === 'POST') {
        return new Response(null, { status: 204 });
      }
      // Do not delegate to global fetch: these tests must never contact GitHub.
      return new Response('unexpected fixture request', { status: 500 });
    },
  }));
  try { await callback(runtime, requests); }
  finally { await runtime.dispose(); }
}

test('production helper dispatches through workerd after a missing receipt', async () => {
  await withRuntime(404, async (runtime, requests) => {
    const result = await (await runtime.dispatchFetch('http://fixture.local/check')).json();
    assert.equal(result.action, 'dispatch');
    assert.equal(result.target_slot, '2026-09-30T02:00:00Z');
    assert.equal(requests.length, 8); // receipt, 5 active statuses, recent runs, dispatch
    assert.equal(requests[0].authorization, null);
    const posts = requests.filter((request) => request.method === 'POST');
    assert.equal(posts.length, 1);
    assert.equal(new URL(posts[0].url).hostname, 'api.github.com');
    assert.deepEqual(posts[0].body, {
      ref: 'main',
      inputs: { target_slot: '2026-09-30T02:00:00Z', trigger_source: 'cloudflare', force: 'false' },
    });
  });
});

test('production helper rejects a receipt redirect without following it or dispatching', async () => {
  await withRuntime(302, async (runtime, requests) => {
    const result = await (await runtime.dispatchFetch('http://fixture.local/check')).json();
    assert.equal(result.code, 'receipt_redirect_rejected');
    assert.equal(requests.length, 1);
    assert.equal(new URL(requests[0].url).hostname, 'raw.githubusercontent.com');
    assert.equal(requests[0].authorization, null);
  });
});
