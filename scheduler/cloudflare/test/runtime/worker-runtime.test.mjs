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

const adapterSource = await readFile(new URL('../../src/entry.mjs', import.meta.url), 'utf8');
const DAILY_SLOT = '2026-10-03T16:00:00Z';
const fixtureAdapter = adapterSource.replace('now: Date.now()', "now: Date.parse('2026-10-03T16:02:00Z')") + `
// Miniflare provides Workflow bindings but no native schedule firing API. Only
// this fixture subclass injects the platform schedule metadata for the test.
export class FixtureScheduledDaily extends SteamDailyScheduler {
  async run(event, step) {
    const options = [];
    const inspectedStep = {
      do(name, config, callback) {
        options.push({ name, ...config });
        return step.do(name, config, callback);
      },
    };
    const result = await super.run({ ...event, schedule: event.payload?.fixtureSchedule }, inspectedStep);
    return { result, options };
  }
}
`;

async function withWorkflowRuntime(apiStatus, callback) {
  const requests = [];
  const runtime = new Miniflare(convertV4MiniflareOptions({
    modules: [
      { type: 'ESModule', path: '/entry.mjs', contents: fixtureAdapter },
      { type: 'ESModule', path: '/worker.mjs', contents: source },
    ],
    modulesRoot: '/',
    compatibilityDate: '2026-09-29',
    bindings: { GITHUB_ACTIONS_TOKEN: 'runtime-test-token-not-a-real-secret', RADAR_ENABLED_JOBS: 'steam_daily' },
    workflows: {
      DAILY_NATIVE: { name: 'fixture-native-daily', className: 'SteamDailyScheduler' },
      DAILY_SCHEDULED: { name: 'fixture-scheduled-daily', className: 'FixtureScheduledDaily' },
    },
    outboundService: async (request) => {
      const url = new URL(request.url);
      const record = { method: request.method, url: request.url, authorization: request.headers.get('Authorization') };
      if (request.method === 'POST') record.body = await request.json();
      requests.push(record);
      assert.equal(url.hostname, 'api.github.com');
      assert.equal(url.pathname.startsWith('/repos/danielet087/game-trend-radar-backend/actions/workflows/steam-two-phase.yml/'), true);
      if (apiStatus !== 200) return new Response('private fixture body must not escape', { status: apiStatus });
      if (url.pathname.endsWith('/runs')) return Response.json({ total_count: 0, workflow_runs: [] });
      if (url.pathname.endsWith('/dispatches') && request.method === 'POST') return new Response(null, { status: 204 });
      return new Response('unexpected fixture request', { status: 500 });
    },
  }));
  try { await callback(runtime, await runtime.getBindings(), requests); }
  finally { await runtime.dispose(); }
}

async function settledWorkflow(instance) {
  const deadline = Date.now() + 10_000;
  while (Date.now() < deadline) {
    const status = await instance.status();
    if (['complete', 'errored', 'terminated'].includes(status.status)) return status;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error('fixture workflow did not settle');
}

test('daily adapter dispatches through a real Workflow binding with zero automatic retries and preserves HTTP 404', async () => {
  await withWorkflowRuntime(200, async (runtime, bindings, requests) => {
    assert.equal((await runtime.dispatchFetch('http://fixture.local/dispatch')).status, 404);
    const instance = await bindings.DAILY_SCHEDULED.create({ id: 'scheduled-daily', params: {
      fixtureSchedule: { cron: '0 16 * * *', scheduledTime: Date.parse(DAILY_SLOT) },
    } });
    const status = await settledWorkflow(instance);
    assert.equal(status.status, 'complete');
    assert.equal(status.output.result.job_id, 'steam_daily');
    assert.equal(status.output.result.action, 'dispatch');
    assert.equal(status.output.result.target_slot, DAILY_SLOT);
    assert.deepEqual(status.output.options, [{ name: 'dispatch-steam-daily-slot',
      retries: { limit: 0, delay: '1 second', backoff: 'constant' }, timeout: '2 minutes' }]);
    const posts = requests.filter((request) => request.method === 'POST');
    assert.equal(posts.length, 1);
    assert.deepEqual(posts[0].body, { ref: 'main', inputs: {
      refresh_today: 'true', target_slot: DAILY_SLOT, trigger_source: 'cloudflare',
    } });
    assert.ok(requests.every((request) => request.authorization === 'Bearer runtime-test-token-not-a-real-secret'));
  });
});

test('daily adapter refuses API-created instances even if their parameters contain a schedule', async () => {
  await withWorkflowRuntime(200, async (_runtime, bindings, requests) => {
    const instance = await bindings.DAILY_NATIVE.create({ id: 'manual-daily', params: {
      schedule: { cron: '0 16 * * *', scheduledTime: Date.parse(DAILY_SLOT) },
    } });
    const status = await settledWorkflow(instance);
    assert.equal(status.status, 'errored');
    assert.equal(status.error.message, 'steam_daily_scheduler_blocked');
    assert.equal(requests.length, 0);
  });
});

test('daily adapter refuses a schedule for another job without reading or dispatching GitHub', async () => {
  await withWorkflowRuntime(200, async (_runtime, bindings, requests) => {
    const instance = await bindings.DAILY_SCHEDULED.create({ id: 'wrong-cron-daily', params: {
      fixtureSchedule: { cron: '5 * * * *', scheduledTime: Date.parse(DAILY_SLOT) },
    } });
    assert.equal((await settledWorkflow(instance)).status, 'errored');
    assert.equal(requests.length, 0);
  });
});

test('a blocked GitHub check marks the daily Workflow errored and never dispatches or leaks its response body', async () => {
  await withWorkflowRuntime(403, async (_runtime, bindings, requests) => {
    const instance = await bindings.DAILY_SCHEDULED.create({ id: 'blocked-daily', params: {
      fixtureSchedule: { cron: '0 16 * * *', scheduledTime: Date.parse(DAILY_SLOT) },
    } });
    const status = await settledWorkflow(instance);
    assert.equal(status.status, 'errored');
    assert.equal(status.error.message, 'steam_daily_scheduler_blocked');
    assert.equal(JSON.stringify(status).includes('private fixture body'), false);
    assert.equal(requests.length, 5); // one status scan, no Workflow retry
    assert.equal(requests.filter((request) => request.method === 'POST').length, 0);
  });
});
