import test from "node:test";
import assert from "node:assert/strict";
import worker, { checkAndDispatch, hourSlot } from "../src/worker.mjs";

const NOW = Date.parse("2026-09-29T22:35:00Z");
const SLOT = "2026-09-29T22:00:00Z";
const ENV = { GITHUB_ACTIONS_TOKEN: "test-only-token" };
const json = (value, status = 200) => new Response(JSON.stringify(value), { status });

function receipt(overrides = {}) {
  return {
    schema_version: 1,
    observed_slot: SLOT,
    target_slot: SLOT,
    collection_started_at: "2026-09-29T22:05:00Z",
    completed_at: "2026-09-29T22:30:00Z",
    generated_at: "2026-09-29T22:30:00Z",
    collection_complete: true,
    run_id: "12345",
    history_path: "data/twitch_history/2026-09-30.json",
    ...overrides,
  };
}

function run(overrides = {}) {
  return {
    id: 12345,
    created_at: "2026-09-29T22:05:00Z",
    updated_at: "2026-09-29T22:15:00Z",
    run_attempt: 1,
    display_title: `Collect Twitch | slot=${SLOT} | cloudflare`,
    status: "completed",
    conclusion: "failure",
    ...overrides,
  };
}

function mockApi({ published = null, runs = [], active = {}, dispatchStatus = 204, responder } = {}) {
  const requests = [];
  const fetchImpl = async (input, options = {}) => {
    const url = new URL(input);
    requests.push({ url, options });
    assert.equal(options.redirect, "manual", "outbound requests must not follow redirects");
    if (responder) {
      const replacement = await responder(url, options);
      if (replacement) return replacement;
    }
    if (url.hostname === "raw.githubusercontent.com") {
      assert.equal(options.headers.Authorization, undefined, "never send a token to the public receipt host");
      assert.ok(url.searchParams.has("watchdog"), "avoid cached receipt");
      return published === null ? new Response("not found", { status: 404 }) : json(published);
    }
    assert.equal(url.hostname, "api.github.com");
    assert.equal(options.headers.Authorization, "Bearer test-only-token");
    if (url.pathname.endsWith("/dispatches")) {
      assert.equal(options.method, "POST");
      return new Response(dispatchStatus === 204 ? null : "{}", { status: dispatchStatus });
    }
    const status = url.searchParams.get("status");
    if (status) {
      const results = active[status] || [];
      return json({ total_count: results.length, workflow_runs: results.slice(0, 1) });
    }
    const page = Number(url.searchParams.get("page"));
    const pageSize = Number(url.searchParams.get("per_page"));
    return json({ total_count: runs.length, workflow_runs: runs.slice((page - 1) * pageSize, page * pageSize) });
  };
  return { fetchImpl, requests, posts: () => requests.filter(({ options }) => options.method === "POST") };
}

async function check(api, now = NOW) {
  return checkAndDispatch(ENV, { fetchImpl: api.fetchImpl, now });
}

test("UTC hour slots survive Taiwan midnight and year boundaries", () => {
  assert.equal(hourSlot("2026-09-30T00:03:00+08:00"), "2026-09-29T16:00:00Z");
  assert.equal(hourSlot("2027-01-01T00:00:01+08:00"), "2026-12-31T16:00:00Z");
  assert.throws(() => hourSlot("bad"), /invalid_clock/);
});

test("first five minutes do not call either API", async () => {
  const api = mockApi();
  assert.equal((await check(api, Date.parse("2026-09-29T22:04:59Z"))).reason, "before_collection_window");
  assert.equal(api.requests.length, 0);
});

test("published current measurement skips dispatch and all GitHub reads", async () => {
  const api = mockApi({ published: receipt() });
  assert.equal((await check(api)).reason, "already_published");
  assert.equal(api.requests.length, 1);
});

test("a late old target is valid only when actual observation is this hour", async () => {
  const api = mockApi({ published: receipt({ target_slot: "2026-09-29T21:00:00Z" }) });
  assert.equal((await check(api)).reason, "already_published");
});

test("404 bootstrap dispatches this hour with explicit safe inputs", async () => {
  const api = mockApi();
  assert.equal((await check(api)).action, "dispatch");
  assert.equal(api.posts().length, 1);
  assert.deepEqual(JSON.parse(api.posts()[0].options.body), {
    ref: "main", inputs: { target_slot: SLOT, trigger_source: "cloudflare", force: "false" },
  });
});

test("stale completed receipt does not suppress this hour", async () => {
  const api = mockApi({ published: receipt({
    observed_slot: "2026-09-29T21:00:00Z", target_slot: "2026-09-29T21:00:00Z",
    collection_started_at: "2026-09-29T21:45:00Z", generated_at: "2026-09-29T22:10:00Z",
    completed_at: "2026-09-29T22:10:00Z",
  }) });
  assert.equal((await check(api)).action, "dispatch");
});

test("partial receipt does not masquerade as success", async () => {
  const api = mockApi({ published: receipt({ collection_complete: false }) });
  assert.equal((await check(api)).action, "dispatch");
});

for (const status of ["queued", "in_progress", "waiting", "pending", "requested"]) {
  test(`an old ${status} run blocks new collection regardless of recent history`, async () => {
    const api = mockApi({ active: { [status]: [run({ status, created_at: "2026-09-28T00:00:00Z" })] } });
    assert.equal((await check(api)).reason, "workflow_active");
    assert.equal(api.posts().length, 0);
    assert.equal(api.requests.filter(({ url }) => url.searchParams.has("created")).length, 0);
  });
}

test("a run appearing between active queries and history prevents duplicate dispatch", async () => {
  const api = mockApi({ runs: [run({ status: "queued" })] });
  assert.equal((await check(api)).reason, "workflow_active");
  assert.equal(api.posts().length, 0);
});

test("failed run cools down for ten minutes after completion", async () => {
  const api = mockApi({ runs: [run({ updated_at: "2026-09-29T22:26:00Z" })] });
  assert.equal((await check(api)).reason, "retry_cooldown");
  assert.equal(api.posts().length, 0);
});

test("failed run retries after cooldown", async () => {
  const api = mockApi({ runs: [run()] });
  assert.deepEqual(await check(api), { target_slot: SLOT, action: "dispatch", reason: "missing_published_collection", attempts: 2 });
});

test("successful workflow without a receipt is retried, not called published", async () => {
  const api = mockApi({ runs: [run({ conclusion: "success" })] });
  assert.equal((await check(api)).action, "dispatch");
});

test("two attempts stop automatic retries until the next hour", async () => {
  const api = mockApi({ runs: [run(), run({ id: 12346 })] });
  assert.equal((await check(api)).reason, "hourly_attempt_limit");
  assert.equal(api.posts().length, 0);
});

test("GitHub re-runs count toward the hourly retry cap", async () => {
  const api = mockApi({ runs: [run({ run_attempt: 2 })] });
  assert.equal((await check(api)).reason, "hourly_attempt_limit");
});

test("automatic native run counts by current creation hour", async () => {
  const api = mockApi({ runs: [run({ display_title: "Collect Twitch | slot=automatic | schedule", run_attempt: 2 })] });
  assert.equal((await check(api)).reason, "hourly_attempt_limit");
});

test("pagination includes an attempt that is outside the first page", async () => {
  const runs = Array.from({ length: 10 }, (_, index) => run({
    id: 100 + index, created_at: "2026-09-29T21:05:00Z", updated_at: "2026-09-29T21:30:00Z",
    display_title: "Collect Twitch | slot=automatic | schedule",
  }));
  runs.push(run({ run_attempt: 2 }));
  const api = mockApi({ runs });
  assert.equal((await check(api)).reason, "hourly_attempt_limit");
  assert.equal(api.requests.filter(({ url }) => url.searchParams.has("created")).length, 2);
});

test("too much history blocks safely instead of ignoring remaining pages", async () => {
  const runs = Array.from({ length: 51 }, (_, index) => run({ id: 100 + index }));
  const api = mockApi({ runs });
  await assert.rejects(check(api), /github_runs_page_limit/);
  assert.equal(api.posts().length, 0);
});

test("only successful HTTP dispatch responses are accepted", async () => {
  for (const status of [200, 202, 204]) {
    assert.equal((await check(mockApi({ dispatchStatus: status }))).action, "dispatch");
  }
  await assert.rejects(check(mockApi({ dispatchStatus: 403 })), /github_dispatch_http_403/);
});

for (const status of [403, 429, 500]) {
  test(`GitHub ${status} blocks dispatch rather than treating history as empty`, async () => {
    const api = mockApi({ responder: (url) => url.hostname === "api.github.com" ? new Response("secret body", { status }) : undefined });
    await assert.rejects(check(api), new RegExp(`github_runs_http_${status}`));
    assert.equal(api.posts().length, 0);
  });
}

test("receipt HTTP failure blocks dispatch", async () => {
  const api = mockApi({ responder: (url) => url.hostname === "raw.githubusercontent.com" ? new Response("failure", { status: 503 }) : undefined });
  await assert.rejects(check(api), /receipt_http_503/);
  assert.equal(api.posts().length, 0);
});

test("receipt redirects are rejected without querying GitHub or dispatching", async () => {
  for (const status of [301, 302, 303, 307, 308]) {
    const api = mockApi({ responder: (url) => url.hostname === "raw.githubusercontent.com"
      ? new Response(null, { status, headers: { Location: "https://untrusted.invalid/receipt" } }) : undefined });
    await assert.rejects(check(api), /receipt_redirect_rejected/);
    assert.equal(api.requests.length, 1);
    assert.equal(api.posts().length, 0);
  }
});

test("GitHub redirects cannot forward the token or lead to dispatch", async () => {
  const api = mockApi({ responder: (url) => url.hostname === "api.github.com"
    ? new Response(null, { status: 302, headers: { Location: "https://untrusted.invalid/runs" } }) : undefined });
  await assert.rejects(check(api), /github_runs_redirect_rejected/);
  assert.equal(api.posts().length, 0);
  assert.ok(api.requests.every(({ url }) => ["api.github.com", "raw.githubusercontent.com"].includes(url.hostname)));
});

test("dispatch redirects are not reported as a successful trigger", async () => {
  const api = mockApi({ responder: (url) => url.pathname.endsWith("/dispatches")
    ? new Response(null, { status: 307, headers: { Location: "https://untrusted.invalid/dispatches" } }) : undefined });
  await assert.rejects(check(api), /github_dispatch_redirect_rejected/);
  assert.equal(api.posts().length, 1);
});

test("null and malformed response shapes fail closed", async () => {
  for (const value of [null, {}, { total_count: 1, workflow_runs: [] }, { total_count: 0, workflow_runs: [run()] }]) {
    const api = mockApi({ responder: (url) => url.hostname === "api.github.com" ? json(value) : undefined });
    await assert.rejects(check(api), /github_runs_invalid_shape/);
    assert.equal(api.posts().length, 0);
  }
  const api = mockApi({ responder: (url) => url.hostname === "raw.githubusercontent.com" ? json(null) : undefined });
  await assert.rejects(check(api), /receipt_invalid_shape/);
});

test("malformed active run cannot be mistaken for an empty queue", async () => {
  const api = mockApi({ responder: (url) => url.hostname === "api.github.com" ? json({ total_count: 1, workflow_runs: [null] }) : undefined });
  await assert.rejects(check(api), /github_run_invalid_shape/);
  assert.equal(api.posts().length, 0);
});

test("oversized response without Content-Length fails closed", async () => {
  const api = mockApi({ responder: (url) => url.hostname === "raw.githubusercontent.com" ? new Response(" ".repeat(16_385)) : undefined });
  await assert.rejects(check(api), /receipt_too_large/);
  assert.equal(api.posts().length, 0);
});

test("invalid receipt metadata and future timestamps cannot suppress collection", async () => {
  for (const overrides of [
    { observed_slot: "2026-09-29T21:00:00Z" },
    { completed_at: "2026-09-29T22:01:00Z" },
    { completed_at: "2026-09-29T23:00:00Z" },
    { generated_at: "2026-09-29T23:00:00Z" },
    { completed_at: "2026-09-29T22:31:00Z" },
    { history_path: "data/twitch_history/2026-09-29.json" },
    { run_id: null },
    { target_slot: "2026-09-29T22:10:00Z" },
  ]) {
    const api = mockApi({ published: receipt(overrides) });
    await assert.rejects(check(api), /receipt_invalid_metadata/);
    assert.equal(api.posts().length, 0);
  }
});

test("no HTTP route can dispatch, and no fetch is needed to answer it", async () => {
  const response = worker.fetch(new Request("https://example.com/dispatch"), ENV, {});
  assert.equal(response.status, 404);
});

test("missing secret or unexpected repository configuration fails before network I/O", async () => {
  const api = mockApi();
  await assert.rejects(checkAndDispatch({}, { now: NOW, fetchImpl: api.fetchImpl }), /invalid_configuration/);
  await assert.rejects(checkAndDispatch({ ...ENV, GITHUB_OWNER: "evil.example/a" }, { now: NOW, fetchImpl: api.fetchImpl }), /invalid_configuration/);
  assert.equal(api.requests.length, 0);
});
