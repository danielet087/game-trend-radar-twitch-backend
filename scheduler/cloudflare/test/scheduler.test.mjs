import test from "node:test";
import assert from "node:assert/strict";
import worker, { catchupRetry, checkScheduledJobs, probeSchedulerPermissions, scheduledJobs, SCHEDULE_JOBS, tokenExpiryTimestamp, twitchRetry } from "../src/worker.mjs";

const ENV = { GITHUB_ACTIONS_TOKEN: "offline-fixture-only",
  RADAR_ENABLED_JOBS: "steam_daily,steam_catchup,steam_growth,steam_content,steam_descriptions,frontend_insights,nintendo_daily" };
const json = (value, status = 200, headers = {}) => new Response(JSON.stringify(value), { status, headers });
const epoch = (value) => Date.parse(value);
const iso = (value) => new Date(epoch(value)).toISOString().replace(".000Z", "Z");

function run(slot, overrides = {}) {
  return { id: 23456, status: "completed", conclusion: "success", run_attempt: 1,
    display_title: `Scheduled task | slot=${slot} | cloudflare`, created_at: slot, updated_at: slot, ...overrides };
}

function fixture({ runs = [], active = {}, activeWorkflows = {}, jobs = [], blockedRepo, dispatchError, responder, tokenExpiration } = {}) {
  const requests = [];
  const fetchImpl = async (input, options = {}) => {
    const url = new URL(input);
    requests.push({ url, options });
    assert.equal(options.redirect, "manual");
    assert.ok(options.signal instanceof AbortSignal);
    if (responder) {
      const replacement = await responder(url, options);
      if (replacement) return replacement;
    }
    if (url.hostname === "danielet087.github.io") {
      assert.equal(options.headers.Authorization, undefined);
      return new Response(null, { status: 404 });
    }
    assert.equal(url.hostname, "api.github.com");
    assert.equal(options.headers.Authorization, "Bearer offline-fixture-only");
    if (url.pathname === "/rate_limit") {
      assert.equal(options.method, undefined);
      if (tokenExpiration === "error") return new Response("never log this body", { status: 503 });
      return json({ resources: {} }, 200, tokenExpiration ? { "GitHub-Authentication-Token-Expiration": tokenExpiration } : {});
    }
    assert.ok(SCHEDULE_JOBS.some((job) => url.pathname.startsWith(`/repos/danielet087/${job.repo}/actions/workflows/`)) ||
      /^\/repos\/danielet087\/game-trend-radar-backend\/actions\/runs\/\d+\/jobs$/.test(url.pathname));
    if (blockedRepo && url.pathname.includes(`/repos/danielet087/${blockedRepo}/`)) return new Response("never log this body", { status: 403 });
    if (url.pathname.endsWith("/dispatches")) {
      assert.equal(options.method, "POST");
      if (dispatchError) throw new Error("private upstream details");
      return new Response(null, { status: 204 });
    }
    if (url.pathname.endsWith("/jobs")) return json({ total_count: 1, jobs: [{ status: "completed", conclusion: "success", steps: jobs }] });
    if (!url.pathname.endsWith("/runs")) {
      const workflow = url.pathname.split("/").at(-1);
      return json({ id: 45678, state: "active", path: `.github/workflows/${workflow === "369223512" ? "collect.yml" : workflow}` });
    }
    const workflow = url.pathname.split("/").at(-2);
    const status = url.searchParams.get("status");
    if (status) {
      const matching = activeWorkflows[workflow]?.[status] || active[status] || [];
      return json({ total_count: matching.length, workflow_runs: matching.slice(0, 1) });
    }
    const page = Number(url.searchParams.get("page"));
    const pageSize = Number(url.searchParams.get("per_page"));
    return json({ total_count: runs.length, workflow_runs: runs.slice((page - 1) * pageSize, page * pageSize) });
  };
  return { fetchImpl, requests, posts: () => requests.filter(({ options }) => options.method === "POST") };
}

// The other jobs' tests see only their own tick; Twitch's :15/:30 and Followers'
// :15 recoveries have their own tests.
function check(api, when, env = ENV, scheduled = when, retryTwitch = false, retryCatchup = false) {
  return checkScheduledJobs(env, { now: epoch(when), scheduledTime: epoch(scheduled), fetchImpl: api.fetchImpl,
    retryTwitch, retryCatchup });
}

test("Taiwan midnight, year boundaries and daily times produce exact UTC minute slots", () => {
  for (const [when, id, slot] of [
    ["2026-10-03T00:00:59+08:00", "steam_daily", "2026-10-02T16:00:00Z"],
    ["2027-01-01T00:00:01+08:00", "steam_daily", "2026-12-31T16:00:00Z"],
    ["2026-10-03T01:15:00+08:00", "steam_growth", "2026-10-02T17:15:00Z"],
    ["2026-10-03T07:30:00+08:00", "steam_content", "2026-10-02T23:30:00Z"],
    ["2026-10-03T19:30:00+08:00", "steam_content", "2026-10-03T11:30:00Z"],
    ["2027-01-01T01:30:00+08:00", "steam_descriptions", "2026-12-31T17:30:00Z"],
    ["2026-10-03T08:30:00+08:00", "nintendo_daily", "2026-10-03T00:30:00Z"],
    ["2027-01-01T08:30:00+08:00", "nintendo_daily", "2027-01-01T00:30:00Z"],
    ["2026-10-03T12:17:00+08:00", "frontend_insights", "2026-10-03T04:17:00Z"],
    ["2026-10-03T12:05:00+08:00", "twitch", "2026-10-03T04:00:00Z"],
    ["2026-10-03T12:17:00+08:00", "frontend_watchdog", "2026-10-03T04:17:00Z"],
    ["2026-10-03T04:30:00+08:00", "storage_retention", "2026-10-02T20:30:00Z"],
    ["2026-10-03T03:30:00+08:00", "twitch_archive", "2026-10-02T19:30:00Z"],
    ["2026-10-03T12:30:00+08:00", "frontend_pages", "2026-10-03T04:30:00Z"],
    ["2027-01-01T00:30:00+08:00", "frontend_pages", "2026-12-31T16:30:00Z"],
  ]) assert.deepEqual(scheduledJobs(when).filter((item) => item.job_id === id), [{ job_id: id, target_slot: slot }]);
  assert.throws(() => scheduledJobs("invalid"), /invalid_clock/);
});

test("Followers catchup is due only at Taiwan 03 through 23 inclusive", () => {
  for (let hour = 0; hour < 24; hour += 1) {
    const due = scheduledJobs(`2026-10-03T${String(hour).padStart(2, "0")}:00:00+08:00`);
    assert.equal(due.some((item) => item.job_id === "steam_catchup"), hour >= 3);
    assert.equal(due.some((item) => item.job_id === "steam_daily"), [0, 6, 12, 18].includes(hour));
    assert.equal(scheduledJobs(`2026-10-03T${String(hour).padStart(2, "0")}:15:00+08:00`)
      .some((item) => item.job_id === "steam_growth"), [1, 7, 13, 19].includes(hour));
  }
  assert.deepEqual(scheduledJobs("2026-10-03T02:15:00+08:00"), []);
  // Only the hourly site deploy uses 12:30.
  assert.deepEqual(scheduledJobs("2026-10-03T12:30:00+08:00").map((item) => item.job_id), ["frontend_pages"]);
  assert.deepEqual(scheduledJobs("2026-10-03T12:06:00+08:00"), []);
});

test("Nintendo is due once a day at Taiwan 08:30 without altering the other 30-minute jobs", () => {
  for (let hour = 0; hour < 24; hour += 1) {
    const due = scheduledJobs(`2026-10-03T${String(hour).padStart(2, "0")}:30:00+08:00`);
    assert.equal(due.some((item) => item.job_id === "nintendo_daily"), hour === 8);
    assert.equal(due.some((item) => item.job_id === "steam_content"), [7, 19].includes(hour));
  }
  for (const minute of [0, 5, 15, 17, 29, 31]) {
    assert.ok(!scheduledJobs(`2026-10-03T08:${String(minute).padStart(2, "0")}:00+08:00`)
      .some((item) => item.job_id === "nintendo_daily"));
  }
});

test("description refresh runs every six hours on the existing 30-minute tick", () => {
  for (let hour = 0; hour < 24; hour += 1) {
    const when = `2026-10-03T${String(hour).padStart(2, "0")}:30:00+08:00`;
    const due = scheduledJobs(when);
    assert.equal(due.some((item) => item.job_id === "steam_descriptions"), [1, 7, 13, 19].includes(hour));
    if ([7, 19].includes(hour)) {
      assert.deepEqual(due.map((item) => item.job_id), ["steam_content", "steam_descriptions", "frontend_pages"]);
    }
  }
  for (const minute of [0, 5, 15, 17, 29, 31]) {
    assert.ok(!scheduledJobs(`2026-10-03T01:${String(minute).padStart(2, "0")}:00+08:00`)
      .some((item) => item.job_id === "steam_descriptions"));
  }
});

test("description refresh stays opt-in and needs the existing scheduler token", async () => {
  const staged = fixture();
  const [result] = await check(staged, "2026-10-04T01:30:00+08:00", {
    ...ENV, RADAR_ENABLED_JOBS: "steam_content",
  });
  assert.equal(result.job_id, "steam_descriptions");
  assert.equal(result.reason, "job_staged");
  assert.equal(staged.requests.length, 0);
  const missingToken = fixture();
  const [blocked] = await check(missingToken, "2026-10-04T01:30:00+08:00", {
    RADAR_ENABLED_JOBS: "steam_descriptions",
  });
  assert.equal(blocked.action, "blocked");
  assert.equal(blocked.reason, "invalid_configuration");
  assert.equal(missingToken.requests.length, 0);
});

test("an earlier description success never suppresses the next slot that day", async () => {
  const when = "2026-10-22T07:30:00+08:00";
  const api = fixture({ runs: [run(iso("2026-10-22T01:30:00+08:00"))] });
  const results = await check(api, when, onlyJob("steam_descriptions"));
  const result = results.find((item) => item.job_id === "steam_descriptions");
  assert.equal(result.action, "dispatch");
  assert.equal(result.target_slot, iso(when));
  assert.equal(api.posts().length, 1);
  assert.ok(api.posts()[0].url.pathname.endsWith("/steam-description-refresh.yml/dispatches"));
});

test("catalog and description refresh dispatch independently at their shared minute", async () => {
  const when = "2026-10-23T07:30:00+08:00";
  const api = fixture({ activeWorkflows: {
    "steam-catalog-reconcile.yml": { in_progress: [run(iso(when), { status: "in_progress" })] },
  } });
  const results = await check(api, when);
  assert.equal(results.find((item) => item.job_id === "steam_content").reason, "workflow_active");
  assert.equal(results.find((item) => item.job_id === "steam_descriptions").action, "dispatch");
  assert.equal(api.posts().length, 1);
  assert.ok(api.posts()[0].url.pathname.endsWith("/steam-description-refresh.yml/dispatches"));
});

test("description refresh consumes each attempted slot and waits for its own active workflow", async () => {
  for (const conclusion of ["success", "failure", "cancelled"]) {
    const when = "2026-10-24T13:30:00+08:00";
    const api = fixture({ runs: [run(iso(when), { conclusion })] });
    const [result] = await check(api, when, onlyJob("steam_descriptions"));
    assert.equal(result.reason, conclusion === "success" ? "slot_completed" : "slot_already_attempted");
    assert.equal(api.posts().length, 0);
  }
  const busy = fixture({ activeWorkflows: {
    "steam-description-refresh.yml": { waiting: [run(iso("2026-10-25T01:30:00+08:00"), { status: "waiting" })] },
  } });
  const [result] = await check(busy, "2026-10-25T13:30:00+08:00", onlyJob("steam_descriptions"));
  assert.equal(result.reason, "workflow_active");
  assert.equal(busy.posts().length, 0);
});

test("Nintendo remains staged when the enable list has only the existing jobs", async () => {
  const api = fixture();
  const [result] = await check(api, "2026-10-04T08:30:00+08:00", {
    ...ENV, RADAR_ENABLED_JOBS: "steam_daily,steam_catchup,steam_growth,steam_content,frontend_insights",
  });
  assert.equal(result.job_id, "nintendo_daily");
  assert.equal(result.reason, "job_staged");
  assert.equal(api.requests.length, 0);
});

test("Nintendo uses its own workflow and consumes an already completed daily slot", async () => {
  const when = "2026-10-17T08:30:00+08:00";
  const api = fixture({ runs: [run(iso(when))], activeWorkflows: {
    "369223512": { in_progress: [run("2026-10-17T00:00:00Z", { status: "in_progress" })] },
  } });
  const [result] = await check(api, when);
  assert.equal(result.reason, "slot_completed");
  assert.equal(api.posts().length, 0);
  assert.ok(api.requests.every(({ url }) => url.pathname.includes("/collect-nintendo.yml/")));
});

test("Nintendo active runs wait, while an unrelated Twitch collection does not block Nintendo", async () => {
  const busy = fixture({ activeWorkflows: {
    "collect-nintendo.yml": { waiting: [run("2026-10-18T00:00:00Z", { status: "waiting" })] },
  } });
  assert.equal((await check(busy, "2026-10-18T08:30:00+08:00"))[0].reason, "workflow_active");
  assert.equal(busy.posts().length, 0);
  const independent = fixture({ activeWorkflows: {
    "369223512": { in_progress: [run("2026-10-19T00:00:00Z", { status: "in_progress" })] },
  } });
  assert.equal((await check(independent, "2026-10-19T08:30:00+08:00"))[0].action, "dispatch");
  assert.equal(independent.posts().length, 1);
});

test("Nintendo delayed delivery preserves its daily slot and never crosses the Taiwan date", async () => {
  const planned = "2026-10-20T08:30:00+08:00";
  const api = fixture();
  const [result] = await check(api, "2026-10-20T09:02:00+08:00", ENV, planned);
  assert.equal(result.action, "dispatch");
  assert.equal(result.target_slot, "2026-10-20T00:30:00Z");
  assert.equal(JSON.parse(api.posts()[0].options.body).inputs.target_slot, result.target_slot);
  const stale = fixture();
  assert.equal((await check(stale, "2026-10-21T00:01:00+08:00", ENV, planned))[0].reason, "stale_cron_delivery");
  assert.equal(stale.requests.length, 0);
});

test("all new jobs remain staged without an explicit allowlisted enable list", async () => {
  const api = fixture();
  const [result] = await check(api, "2026-10-04T00:00:00+08:00", { GITHUB_ACTIONS_TOKEN: ENV.GITHUB_ACTIONS_TOKEN });
  assert.equal(result.reason, "job_staged");
  assert.equal(api.requests.length, 0);
  assert.equal(SCHEDULE_JOBS.filter((job) => job.enabled).length, 1);
});

test("unknown, duplicate or injected enable values fail before I/O", async () => {
  for (const value of ["true", "steam_daily,steam_daily", "other_repo", "steam_daily,evil/target", ["steam_daily"]]) {
    const api = fixture();
    await assert.rejects(check(api, "2026-10-04T00:00:00+08:00", { ...ENV, RADAR_ENABLED_JOBS: value }), /invalid_enabled_jobs/);
    assert.equal(api.requests.length, 0);
  }
});

test("each enabled job sends only its fixed destination and exact due slot inputs", async () => {
  for (const [when, id] of [
    ["2026-10-05T00:00:00+08:00", "steam_daily"],
    ["2026-10-05T03:00:00+08:00", "steam_catchup"],
    ["2026-10-05T01:15:00+08:00", "steam_growth"],
    ["2026-10-05T07:30:00+08:00", "steam_content"],
    ["2026-10-05T01:30:00+08:00", "steam_descriptions"],
    ["2026-10-05T08:30:00+08:00", "nintendo_daily"],
    ["2026-10-05T12:17:00+08:00", "frontend_insights"],
    ["2026-10-05T12:17:00+08:00", "frontend_watchdog"],
    ["2026-10-05T04:30:00+08:00", "storage_retention"],
    ["2026-10-05T03:30:00+08:00", "twitch_archive"],
    ["2026-10-05T05:30:00+08:00", "credentials_frontend"],
    ["2026-10-05T05:30:00+08:00", "credentials_backend"],
    ["2026-10-05T05:30:00+08:00", "credentials_twitch"],
    ["2026-10-05T05:30:00+08:00", "credentials_content"],
    ["2026-10-05T12:30:00+08:00", "frontend_pages"],
  ]) {
    const api = fixture();
    assert.equal((await check(api, when, onlyJob(id))).find((item) => item.job_id === id).action, "dispatch");
    const job = SCHEDULE_JOBS.find((item) => item.id === id);
    assert.equal(api.posts().length, 1);
    assert.equal(api.posts()[0].url.pathname, `/repos/danielet087/${job.repo}/actions/workflows/${job.workflow}/dispatches`);
    assert.deepEqual(JSON.parse(api.posts()[0].options.body), { ref: "main", inputs: {
      ...(id === "steam_daily" ? { refresh_today: "true" } : {}),
      ...(id === "frontend_watchdog" ? { scheduler_token_expires_at: "none" } : {}),
      target_slot: iso(when), trigger_source: "cloudflare",
    } });
    // Only the watchdog dispatch looks up the scheduler's own token.
    assert.equal(api.requests.some(({ url }) => url.pathname === "/rate_limit"), id === "frontend_watchdog");
  }
});

test("the watchdog dispatch carries only the scheduler token's expiry date", async () => {
  for (const [index, [header, expected]] of [
    ["2026-12-01 00:00:00 UTC", "2026-12-01T00:00:00Z"],
    ["2026-12-01 08:00:00 +0800", "2026-12-01T00:00:00Z"],
    [undefined, "none"],
    ["error", "unknown"],
    ["sometime next year", "unknown"],
  ].entries()) {
    const api = fixture({ tokenExpiration: header });
    const results = await check(api, `2026-10-2${index}T12:17:00+08:00`, onlyJob("frontend_watchdog"));
    assert.equal(results.find((item) => item.job_id === "frontend_watchdog").action, "dispatch");
    const body = api.posts()[0].options.body;
    assert.equal(JSON.parse(body).inputs.scheduler_token_expires_at, expected);
    assert.ok(!body.includes("offline-fixture-only") && !body.includes("never log this body"));
  }
  assert.equal(tokenExpiryTimestamp("2026-11-30 17:00:00 -07:00"), "2026-12-01T00:00:00Z");
  assert.equal(tokenExpiryTimestamp(null), null);
});

test("a matching successful slot skips and all other completed conclusions also consume the slot", async () => {
  for (const conclusion of ["success", "failure", "cancelled", "skipped", "timed_out", null]) {
    const when = "2026-10-06T00:00:00+08:00";
    const api = fixture({ runs: [run(iso(when), { conclusion })] });
    const [result] = await check(api, when);
    assert.equal(result.reason, conclusion === "success" ? "day_already_completed" : "slot_already_attempted");
    assert.equal(api.posts().length, 0);
  }
});

test("exact slot detection accepts both supported run-name separators", async () => {
  const when = "2026-10-06T03:00:00+08:00";
  const api = fixture({ runs: [run(iso(when), { display_title: `Steam catchup · slot=${iso(when)} · source=cloudflare` })] });
  assert.equal((await check(api, when))[0].reason, "slot_completed");
  assert.equal(api.posts().length, 0);
});

test("old slot history does not suppress a new slot and similar title prefixes do not match", async () => {
  const when = "2026-10-07T00:00:00+08:00";
  const api = fixture({ runs: [run("2026-10-05T16:00:00Z"), run(iso(when), {
    id: 23457, display_title: `Scheduled task | slot=${iso(when)}extra | cloudflare`,
  })] });
  assert.equal((await check(api, when))[0].action, "dispatch");
});

for (const status of ["queued", "in_progress", "waiting", "pending", "requested"]) {
  test(`an old ${status} run prevents additional dispatch`, async () => {
    const api = fixture({ active: { [status]: [run("2026-10-01T00:00:00Z", { status })] } });
    assert.equal((await check(api, "2026-10-08T00:00:00+08:00"))[0].reason, "workflow_active");
    assert.equal(api.posts().length, 0);
  });
}

test("growth and catchup respect their shared concurrency peers and daily discovery", async () => {
  for (const [when, blocker] of [
    ["2026-10-08T01:15:00+08:00", "steam-official-daily-catchup-250.yml"],
    ["2026-10-08T03:00:00+08:00", "steam-public-growth.yml"],
    ["2026-10-08T03:00:00+08:00", "steam-two-phase.yml"],
  ]) {
    const api = fixture({ activeWorkflows: { [blocker]: { queued: [run(iso(when), { status: "queued" })] } } });
    const [result] = await check(api, when);
    assert.equal(result.reason, "workflow_active");
    assert.equal(result.blocking_workflow, blocker);
    assert.equal(api.posts().length, 0);
  }
});

test("retired one-off experiments are no longer read before Steam dispatches", async () => {
  // These workflows are disabled; the scheduler must not depend on them existing.
  const retired = ["steam-official-backlog-oneoff-20260923.yml", "steam-official-nearfirst-batch-once.yml",
    "steam-official-hour-stress-once.yml"];
  for (const job of SCHEDULE_JOBS) {
    assert.ok(!(job.blockingWorkflows || []).some((workflow) => retired.includes(workflow)), job.id);
  }
  for (const when of ["2026-10-08T03:00:00+08:00", "2026-10-08T01:15:00+08:00"]) {
    const api = fixture({ activeWorkflows: Object.fromEntries(retired.map((workflow) =>
      [workflow, { in_progress: [run(iso(when), { status: "in_progress" })] }])) });
    const [result] = await check(api, when);
    assert.equal(result.action, "dispatch");
    assert.equal(api.posts().length, 1);
  }
});

test("a run becoming active between status reads and recent history is not duplicated", async () => {
  const api = fixture({ runs: [run("2026-10-09T00:00:00Z", { status: "queued" })] });
  assert.equal((await check(api, "2026-10-09T00:00:00+08:00"))[0].reason, "workflow_active");
  assert.equal(api.posts().length, 0);
});

test("a repeated daily event in this isolate cannot dispatch or reset progress twice", async () => {
  const api = fixture();
  const when = "2026-10-10T00:00:00+08:00";
  assert.equal((await check(api, when))[0].action, "dispatch");
  assert.equal((await check(api, when))[0].reason, "slot_dispatch_attempted");
  assert.equal(api.posts().length, 1);
});

test("an ambiguous dispatch timeout consumes the local slot instead of retrying a daily reset", async () => {
  const api = fixture({ dispatchError: true });
  const when = "2026-10-11T00:00:00+08:00";
  assert.equal((await check(api, when))[0].reason, "github_dispatch_unavailable");
  assert.equal((await check(api, when))[0].reason, "slot_dispatch_attempted");
  assert.equal(api.posts().length, 1);
});

test("missing secret and non-allowlisted destination fail before any network call", async () => {
  for (const env of [{ RADAR_ENABLED_JOBS: ENV.RADAR_ENABLED_JOBS }, { ...ENV, GITHUB_OWNER: "other-owner" },
    { ...ENV, GITHUB_BRANCH: "feature/branch" }, { ...ENV, GITHUB_WORKFLOW_ID: "123456" }]) {
    const api = fixture();
    assert.equal((await check(api, "2026-10-12T00:00:00+08:00", env))[0].action, "blocked");
    assert.equal(api.requests.length, 0);
  }
});

test("permission preflight is read-only and repo 403s do not prevent Twitch collection", async () => {
  const api = fixture({ blockedRepo: "game-trend-radar-backend" });
  const probe = await probeSchedulerPermissions(ENV, { fetchImpl: api.fetchImpl });
  assert.equal(probe.find((result) => result.job_id === "twitch").action, "readable");
  assert.equal(probe.find((result) => result.job_id === "steam_daily").reason, "github_workflow_http_403");
  assert.equal(api.posts().length, 0);
  assert.equal((await check(api, "2026-10-12T12:05:00+08:00"))[0].action, "dispatch");
  assert.equal(api.posts().length, 1);
  assert.ok(api.posts()[0].url.pathname.includes("/game-trend-radar-twitch-backend/"));
});

test("an enabled job with insufficient permission remains blocked rather than assuming no history", async () => {
  const api = fixture({ blockedRepo: "game-trend-radar-backend" });
  assert.equal((await check(api, "2026-10-13T03:00:00+08:00"))[0].reason, "github_runs_http_403");
  assert.equal(api.posts().length, 0);
});

const HOUR = "2026-10-14T04:00:00Z";
const twitchRun = (overrides = {}) => run("2026-10-14T04:05:10Z", {
  display_title: `Collect Twitch | slot=${HOUR} | cloudflare`, updated_at: "2026-10-14T04:12:00Z", ...overrides });
const twitchAt = async (api, when, retryTwitch = true) =>
  (await check(api, when, ENV, when, retryTwitch)).filter((item) => item.job_id === "twitch");

test("Twitch is checked again at :15 and :30, after that minute's own jobs", async () => {
  for (const [when, due] of [["2026-10-14T12:15:00+08:00", true], ["2026-10-14T12:30:00+08:00", true],
    ["2026-10-14T12:05:00+08:00", false], ["2026-10-14T12:17:00+08:00", false], ["2026-10-14T12:00:00+08:00", false]]) {
    assert.deepEqual(twitchRetry(epoch(when)), due ? [{ job_id: "twitch", target_slot: HOUR, retry: true }] : []);
  }
  // The planned schedule is unchanged: Twitch is due at :05 only.
  assert.ok(!scheduledJobs("2026-10-14T12:15:00+08:00").some((item) => item.job_id === "twitch"));
  // The production check includes the recovery, after the tick's own jobs.
  const api = fixture();
  const results = await checkScheduledJobs(ENV, { now: epoch("2026-10-14T12:30:00+08:00"),
    scheduledTime: epoch("2026-10-14T12:30:00+08:00"), fetchImpl: api.fetchImpl });
  assert.deepEqual(results.map((item) => item.job_id), ["frontend_pages", "twitch"]);
  assert.equal(results.at(-1).retry, true);
});

test("a :15 or :30 tick starts an hour the :05 check never started", async () => {
  // 2026-11-26 00:05 Taipei: the :05 tick dispatched nothing and the hour went uncollected.
  for (const when of ["2026-10-14T12:15:00+08:00", "2026-10-14T12:30:00+08:00"]) {
    const api = fixture();
    assert.deepEqual(await twitchAt(api, when), [{ job_id: "twitch", target_slot: HOUR, action: "dispatch",
      reason: "missing_published_collection", attempts: 1, retry: true }]);
    assert.equal(api.posts().length, 1);
    assert.deepEqual(JSON.parse(api.posts()[0].options.body),
      { ref: "main", inputs: { target_slot: HOUR, trigger_source: "cloudflare", force: "false" } });
  }
});

test("a retry tick starts no second run after a green one and waits for a running one", async () => {
  // The :05 run finished green; its receipt reaches the site only with the :30 deploy.
  const green = fixture({ runs: [twitchRun()] });
  assert.deepEqual(await twitchAt(green, "2026-10-14T12:30:00+08:00"), [{ job_id: "twitch", target_slot: HOUR,
    action: "skip", reason: "collected_this_hour", attempts: 1, retry: true }]);
  assert.equal(green.posts().length, 0);
  const busy = fixture({ active: { in_progress: [twitchRun({ status: "in_progress", conclusion: null })] } });
  const [running] = await twitchAt(busy, "2026-10-14T12:15:00+08:00");
  assert.equal(running.reason, "workflow_active");
  assert.equal(busy.posts().length, 0);
});

test("a failed :05 run is retried once, ten minutes after it ended", async () => {
  const failed = { conclusion: "failure", updated_at: "2026-10-14T04:08:00Z" };
  const soon = fixture({ runs: [twitchRun(failed)] });
  assert.equal((await twitchAt(soon, "2026-10-14T12:15:00+08:00"))[0].reason, "retry_cooldown");
  const later = fixture({ runs: [twitchRun(failed)] });
  const [retried] = await twitchAt(later, "2026-10-14T12:30:00+08:00");
  assert.deepEqual([retried.action, retried.attempts], ["dispatch", 2]);
  const twice = fixture({ runs: [twitchRun(failed), twitchRun({ ...failed, id: 23457, created_at: "2026-10-14T04:15:20Z",
    updated_at: "2026-10-14T04:16:00Z" })] });
  assert.equal((await twitchAt(twice, "2026-10-14T12:30:00+08:00"))[0].reason, "hourly_attempt_limit");
  assert.equal(twice.posts().length, 0);
});

test("Twitch Cron delayed from minute 05 to 06 still inspects only this hour", async () => {
  const api = fixture();
  const [result] = await check(api, "2026-10-14T12:06:00+08:00", ENV, "2026-10-14T12:05:00+08:00");
  assert.equal(result.action, "dispatch");
  assert.equal(result.job_id, "twitch");
  assert.equal(result.target_slot, "2026-10-14T04:00:00Z");
  assert.equal(api.posts().length, 1);
});

test("late hourly jobs retain the original due instant within this hour", async () => {
  for (const [when, planned, id] of [
    ["2026-10-14T03:06:00+08:00", "2026-10-14T03:00:00+08:00", "steam_catchup"],
    ["2026-10-14T12:19:00+08:00", "2026-10-14T12:17:00+08:00", "frontend_insights"],
    ["2026-10-14T12:34:00+08:00", "2026-10-14T12:30:00+08:00", "frontend_pages"],
  ]) {
    const api = fixture();
    const [result] = await check(api, when, id === "frontend_pages" ? onlyJob(id) : ENV, planned);
    assert.equal(result.action, "dispatch");
    assert.equal(result.job_id, id);
    assert.equal(result.target_slot, iso(planned));
  }
});

test("late daily tasks remain valid only within the same Taiwan day", async () => {
  const api = fixture();
  const planned = "2026-10-15T00:00:00+08:00";
  const [result] = await check(api, "2026-10-15T00:06:00+08:00", ENV, planned);
  assert.equal(result.action, "dispatch");
  assert.equal(result.target_slot, iso(planned));
  const stale = fixture();
  for (const plannedTime of ["2026-10-16T00:00:00+08:00", "2026-10-16T01:15:00+08:00", "2026-10-16T19:30:00+08:00"]) {
    assert.equal((await check(stale, "2026-10-17T00:06:00+08:00", ENV, plannedTime))[0].reason, "stale_cron_delivery");
  }
  assert.equal(stale.requests.length, 0);
});

test("cross-hour Cron deliveries and non-due minutes do not backfill old observations", async () => {
  const api = fixture();
  assert.equal((await check(api, "2026-10-13T12:05:00+08:00", ENV, "2026-10-13T11:05:00+08:00"))[0].reason, "stale_cron_delivery");
  assert.equal((await check(api, "2026-10-13T12:06:00+08:00", ENV, "2026-10-13T11:17:00+08:00"))[0].reason, "stale_cron_delivery");
  assert.equal((await check(api, "2026-10-13T12:06:00+08:00", ENV, "2026-10-13T11:00:00+08:00"))[0].reason, "stale_cron_delivery");
  assert.equal((await check(api, "2026-10-13T12:06:00+08:00"))[0].reason, "no_job_due");
  assert.equal(api.requests.length, 0);
});

test("future Cron metadata never initiates an early dispatch", async () => {
  const api = fixture();
  assert.equal((await check(api, "2026-10-13T12:04:59+08:00", ENV, "2026-10-13T12:05:00+08:00"))[0].reason, "before_scheduled_slot");
  assert.equal((await check(api, "2026-10-13T12:04:59+08:00", ENV, "2026-10-13T13:05:00+08:00"))[0].reason, "future_cron_delivery");
  assert.equal(api.requests.length, 0);
});

test("read-only permission probe rejects redirects and inactive or swapped workflows", async () => {
  for (const response of [
    new Response(null, { status: 302, headers: { Location: "https://untrusted.invalid/" } }),
    json({ id: 123, state: "disabled_manually", path: ".github/workflows/collect.yml" }),
    json({ id: 123, state: "active", path: ".github/workflows/unexpected.yml" }),
  ]) {
    const api = fixture({ responder: () => response.clone() });
    const results = await probeSchedulerPermissions(ENV, { fetchImpl: api.fetchImpl });
    assert.ok(results.every((result) => result.action === "blocked"));
    assert.equal(api.posts().length, 0);
    assert.ok(api.requests.every(({ url }) => url.hostname === "api.github.com"));
  }
});

test("HTTP remains unavailable to callers even with all jobs enabled", async () => {
  assert.equal(worker.fetch(new Request("https://example.invalid/dispatch"), ENV, {}).status, 404);
});

const coverageGate = { name: "Require complete growth coverage", status: "completed", conclusion: "success" };

test("quiet workflow failures still receive their next six-hour recovery check", async () => {
  for (const [offset, id] of ["steam_daily", "steam_growth"].entries()) {
    for (const [index, hidden] of [
      { status: "completed", conclusion: "failure", steps: [coverageGate] },
      { status: "completed", conclusion: "success", steps: [coverageGate,
        { name: "Persist", status: "completed", conclusion: "failure" }] },
    ].entries()) {
      const date = `2026-12-${String(10 + offset * 2 + index).padStart(2, "0")}`;
      const first = id === "steam_daily" ? `${date}T00:00:00+08:00` : `${date}T01:15:00+08:00`;
      const retry = id === "steam_daily" ? `${date}T06:00:00+08:00` : `${date}T07:15:00+08:00`;
      const api = fixture({ runs: [run(iso(first))], responder: url =>
        url.pathname.endsWith("/jobs") ? json({ total_count: 2, jobs: [hidden,
          { status: "completed", conclusion: "success", name: "notification", steps: [] }] }) : null });
      assert.equal((await check(api, retry, onlyJob(id)))[0].action, "dispatch");
      assert.equal(api.posts().length, 1);
    }
  }
});
const onlyJob = (id) => ({ ...ENV, RADAR_ENABLED_JOBS: id });

test("daily failures retry six hours later, with the new exact slot and no extra same-slot dispatch", async () => {
  for (const [id, first, next] of [
    ["steam_daily", "2026-11-01T00:00:00+08:00", "2026-11-01T06:00:00+08:00"],
    ["steam_growth", "2026-11-01T01:15:00+08:00", "2026-11-01T07:15:00+08:00"],
  ]) {
    const api = fixture({ runs: [run(iso(first), { conclusion: "failure" })] });
    const result = (await check(api, next, onlyJob(id)))[0];
    assert.equal(result.action, "dispatch");
    assert.equal(JSON.parse(api.posts()[0].options.body).inputs.target_slot, iso(next));
    assert.equal((await check(api, next, onlyJob(id)))[0].reason, "slot_dispatch_attempted");
    assert.equal(api.posts().length, 1);
  }
});

test("any validated success earlier today skips all later checks even if a later run failed", async () => {
  for (const [id, first, later, final] of [
    ["steam_daily", "2026-11-02T00:00:00+08:00", "2026-11-02T06:00:00+08:00", "2026-11-02T18:00:00+08:00"],
    ["steam_growth", "2026-11-02T01:15:00+08:00", "2026-11-02T07:15:00+08:00", "2026-11-02T19:15:00+08:00"],
  ]) {
    const api = fixture({ runs: [run(iso(later), { id: 23457, conclusion: "failure" }), run(iso(first))], jobs: [coverageGate] });
    const result = (await check(api, final, onlyJob(id)))[0];
    assert.equal(result.reason, "day_already_completed");
    assert.equal(result.run_id, 23456);
    assert.equal(api.posts().length, 0);
    const history = api.requests.find(({ url }) => url.searchParams.has("created"));
    assert.equal(history.url.searchParams.get("created"), ">=2026-11-01T16:00:00Z");
    assert.equal(history.url.searchParams.get("branch"), "main");
  }
});

test("yesterday's success and a previous-day run ending today cannot suppress a new day", async () => {
  const when = "2026-11-03T06:00:00+08:00";
  const api = fixture({ runs: [
    run("2026-11-02T10:00:00Z"),
    run("2026-11-02T10:00:00Z", { id: 23457, updated_at: "2026-11-02T16:01:00Z" }),
  ] });
  assert.equal((await check(api, when, onlyJob("steam_daily")))[0].action, "dispatch");
});

test("an unvalidated legacy growth success retries rather than hiding partial measurements", async () => {
  for (const [index, gate] of [null, { ...coverageGate, conclusion: "failure" }, { ...coverageGate, conclusion: "skipped" }].entries()) {
    const date = `2026-11-${String(4 + index).padStart(2, "0")}`;
    const api = fixture({ runs: [run(iso(`${date}T01:15:00+08:00`))], jobs: gate ? [gate] : [] });
    assert.equal((await check(api, `${date}T07:15:00+08:00`, onlyJob("steam_growth")))[0].action, "dispatch");
  }
});

test("complete manual growth can count today while a manual candidate continuation cannot", async () => {
  const created = "2026-11-07T02:00:00Z";
  for (const id of ["steam_daily", "steam_growth"]) {
    const api = fixture({ runs: [run(created, { display_title: "Steam task | slot=manual | source=workflow_dispatch" })], jobs: [coverageGate] });
    const when = id === "steam_daily" ? "2026-11-07T12:00:00+08:00" : "2026-11-07T13:15:00+08:00";
    const result = (await check(api, when, onlyJob(id)))[0];
    assert.equal(result.action, id === "steam_growth" ? "skip" : "dispatch");
  }
});

test("daily history pagination finds an older success and refuses incomplete job coverage metadata", async () => {
  const when = "2026-11-08T19:15:00+08:00";
  const earlier = "2026-11-08T01:15:00+08:00";
  const api = fixture({ runs: [
    ...Array.from({ length: 10 }, (_, index) => run("2026-11-08T06:00:00Z", {
      id: index + 1, conclusion: "failure", display_title: "manual maintenance",
    })), run(iso(earlier)),
  ], jobs: [coverageGate] });
  assert.equal((await check(api, when, onlyJob("steam_growth")))[0].reason, "day_already_completed");
  assert.ok(api.requests.some(({ url }) => url.searchParams.get("page") === "2"));
  const invalid = fixture({ runs: [run(iso("2026-11-09T01:15:00+08:00"))], responder: (url) =>
    url.pathname.endsWith("/jobs") ? json({ total_count: 2, jobs: [{ steps: [coverageGate] }] }) : null });
  assert.equal((await check(invalid, "2026-11-09T07:15:00+08:00", onlyJob("steam_growth")))[0].reason, "github_jobs_invalid_shape");
  assert.equal(invalid.posts().length, 0);
});

test("a six-hour candidate retry takes priority over the simultaneous hourly catchup", async () => {
  const api = fixture({ runs: [run(iso("2026-11-10T00:00:00+08:00"), { conclusion: "failure" })] });
  const results = await check(api, "2026-11-10T06:00:00+08:00");
  assert.equal(results.find((item) => item.job_id === "steam_daily").action, "dispatch");
  assert.equal(results.find((item) => item.job_id === "steam_catchup").reason, "daily_refresh_priority");
  assert.equal(api.posts().length, 1);
  const complete = fixture({ runs: [run(iso("2026-11-11T00:00:00+08:00"))] });
  const afterSuccess = await check(complete, "2026-11-11T06:00:00+08:00");
  assert.equal(afterSuccess.find((item) => item.job_id === "steam_daily").reason, "day_already_completed");
  assert.equal(afterSuccess.find((item) => item.job_id === "steam_catchup").action, "dispatch");
  assert.equal(complete.posts().length, 1);
});

test("a candidate retry waits for an official collector still running from an earlier hour", async () => {
  for (const [index, blocker] of ["steam-official-daily-catchup-250.yml", "steam-public-growth.yml"].entries()) {
    const date = `2026-11-${12 + index}`;
    const api = fixture({ activeWorkflows: { [blocker]: { in_progress: [run(iso(`${date}T05:00:00+08:00`), { status: "in_progress" })] } } });
    const results = await check(api, `${date}T06:00:00+08:00`);
    const daily = results.find((item) => item.job_id === "steam_daily");
    assert.equal(daily.reason, "workflow_active");
    assert.equal(daily.blocking_workflow, blocker);
    assert.equal(results.find((item) => item.job_id === "steam_catchup").reason, "daily_refresh_priority");
    assert.equal(api.posts().length, 0);
  }
});

test("growth retries queue behind a running catchup rather than missing every six-hour retry", async () => {
  for (const [index, hour] of [7, 13, 19].entries()) {
    const date = `2026-11-${17 + index}`;
    const planned = `${date}T${String(hour).padStart(2, "0")}:15:00+08:00`;
    const api = fixture({ runs: [run(iso(`${date}T01:15:00+08:00`), { conclusion: "failure" })], activeWorkflows: {
      "steam-official-daily-catchup-250.yml": { in_progress: [run(iso(`${date}T${String(hour).padStart(2, "0")}:00:00+08:00`), { status: "in_progress" })] },
    } });
    assert.equal((await check(api, planned))[0].action, "dispatch");
    assert.equal(api.posts().length, 1);
    assert.ok(api.posts()[0].url.pathname.includes("/steam-public-growth.yml/dispatches"));
  }
});

test("growth retries do not replace pending catchups or duplicate their own active work", async () => {
  for (const [index, status] of ["queued", "waiting", "pending", "requested"].entries()) {
    const date = `2026-11-${20 + index}`;
    const api = fixture({ activeWorkflows: { "steam-official-daily-catchup-250.yml": {
      in_progress: [run(iso(`${date}T07:00:00+08:00`), { status: "in_progress" })],
      [status]: [run(iso(`${date}T07:01:00+08:00`), { id: 23457, status })],
    } } });
    assert.equal((await check(api, `${date}T07:15:00+08:00`))[0].reason, "workflow_active");
    assert.equal(api.posts().length, 0);
  }
  const own = fixture({ activeWorkflows: {
    "steam-public-growth.yml": { queued: [run("2026-11-24T23:00:00Z", { status: "queued" })] },
    "steam-official-daily-catchup-250.yml": { in_progress: [run("2026-11-24T23:00:00Z", { status: "in_progress" })] },
  } });
  assert.equal((await check(own, "2026-11-25T07:15:00+08:00"))[0].blocking_workflow, "steam-public-growth.yml");
  assert.equal(own.posts().length, 0);
});

// Official Followers' :15 recovery (2026-11-26 15:00 Taipei: the :00 tick dispatched nothing).
const followersAt = async (api, when, env = onlyJob("steam_catchup")) =>
  (await check(api, when, env, when, false, true)).filter((item) => item.job_id === "steam_catchup");

test("Followers is checked again at :15 of its 03:00–23:00 hours, with that hour's :00 slot", async () => {
  for (const [when, due] of [["2026-12-01T15:15:00+08:00", true], ["2026-12-01T03:15:00+08:00", true],
    ["2026-12-01T23:15:00+08:00", true], ["2026-12-01T02:15:00+08:00", false], ["2026-12-01T15:30:00+08:00", false],
    ["2026-12-01T15:00:00+08:00", false], ["2026-12-01T15:05:00+08:00", false]]) {
    assert.deepEqual(catchupRetry(epoch(when)), due
      ? [{ job_id: "steam_catchup", target_slot: iso(when.replace(":15:00", ":00:00")), retry: true }] : []);
  }
  // The planned schedule is unchanged: Followers is due at :00 only.
  assert.ok(!scheduledJobs("2026-12-01T15:15:00+08:00").some((item) => item.job_id === "steam_catchup"));
  // The production check includes both recoveries, after the tick's own jobs.
  const api = fixture();
  const results = await checkScheduledJobs(ENV, { now: epoch("2026-12-01T15:15:00+08:00"),
    scheduledTime: epoch("2026-12-01T15:15:00+08:00"), fetchImpl: api.fetchImpl });
  assert.deepEqual(results.map((item) => [item.job_id, item.retry]), [["twitch", true], ["steam_catchup", true]]);
});

test("a :15 tick starts the Followers hour its :00 tick never started", async () => {
  const api = fixture();
  assert.deepEqual(await followersAt(api, "2026-12-02T15:15:00+08:00"), [{ job_id: "steam_catchup",
    target_slot: "2026-12-02T07:00:00Z", action: "dispatch", reason: "scheduled_slot_due", retry: true }]);
  assert.equal(api.posts().length, 1);
  assert.ok(api.posts()[0].url.pathname.endsWith("/steam-official-daily-catchup-250.yml/dispatches"));
  assert.deepEqual(JSON.parse(api.posts()[0].options.body),
    { ref: "main", inputs: { target_slot: "2026-12-02T07:00:00Z", trigger_source: "cloudflare" } });
});

test("a :15 Followers check starts no second run for its hour and waits for running Steam work", async () => {
  for (const [index, conclusion] of ["success", "failure", "cancelled"].entries()) {
    const date = `2026-12-0${3 + index}`;
    const api = fixture({ runs: [run(iso(`${date}T15:00:00+08:00`), { created_at: iso(`${date}T15:00:20+08:00`),
      updated_at: iso(`${date}T15:06:00+08:00`), conclusion })] });
    const [result] = await followersAt(api, `${date}T15:15:00+08:00`);
    assert.deepEqual([result.action, result.reason, result.retry],
      ["skip", conclusion === "success" ? "slot_completed" : "slot_already_attempted", true]);
    assert.equal(api.posts().length, 0);
  }
  for (const [index, blocker] of ["steam-official-daily-catchup-250.yml", "steam-two-phase.yml", "steam-public-growth.yml"].entries()) {
    const date = `2026-12-0${6 + index}`;
    const api = fixture({ activeWorkflows: { [blocker]: { in_progress: [run(iso(`${date}T06:00:00+08:00`),
      { status: "in_progress", conclusion: null })] } } });
    const [result] = await followersAt(api, `${date}T06:15:00+08:00`);
    assert.deepEqual([result.action, result.reason, result.blocking_workflow], ["wait", "workflow_active", blocker]);
    assert.equal(api.posts().length, 0);
  }
});

test("growth goes first at 07:15/13:15/19:15; Followers starts only when growth is done for the day", async () => {
  for (const [index, hour] of [7, 13, 19].entries()) {
    const at = (date, minute = "15") => `${date}T${String(hour).padStart(2, "0")}:${minute}:00+08:00`;
    const pending = fixture();
    const results = await check(pending, at(`2026-12-${10 + index}`), ENV, undefined, false, true);
    assert.deepEqual(results.map((item) => [item.job_id, item.action, item.reason]),
      [["steam_growth", "dispatch", "scheduled_slot_due"], ["steam_catchup", "wait", "growth_priority"]]);
    assert.equal(pending.posts().length, 1);
    assert.ok(pending.posts()[0].url.pathname.includes("/steam-public-growth.yml/dispatches"));
    const date = `2026-12-${20 + index}`;
    const done = fixture({ runs: [run(iso(`${date}T01:15:00+08:00`))], jobs: [coverageGate] });
    const after = await check(done, at(date), ENV, undefined, false, true);
    assert.deepEqual(after.map((item) => [item.job_id, item.action, item.reason]),
      [["steam_growth", "skip", "day_already_completed"], ["steam_catchup", "dispatch", "scheduled_slot_due"]]);
    assert.equal(done.posts().length, 1);
    assert.equal(JSON.parse(done.posts()[0].options.body).inputs.target_slot, iso(at(date, "00")));
  }
});

test("a :00 Followers dispatch lost in this isolate is decided at :15 by GitHub's run list", async () => {
  const lost = fixture({ dispatchError: true });
  assert.equal((await check(lost, "2026-12-15T15:00:00+08:00", onlyJob("steam_catchup")))[0].reason,
    "github_dispatch_unavailable");
  const later = fixture();
  const [retried] = await followersAt(later, "2026-12-15T15:15:00+08:00");
  assert.deepEqual([retried.action, retried.reason, retried.retry], ["dispatch", "scheduled_slot_due", true]);
  assert.equal(later.posts().length, 1);
  // An attempt less than ten minutes earlier (a late :00 delivery) still holds the slot.
  await check(fixture({ dispatchError: true }), "2026-12-16T15:06:00+08:00", onlyJob("steam_catchup"),
    "2026-12-16T15:00:00+08:00");
  const held = fixture();
  assert.equal((await followersAt(held, "2026-12-16T15:15:00+08:00"))[0].reason, "slot_dispatch_attempted");
  assert.equal(held.posts().length, 0);
});

test("the site deploys every hour at :30 and a delivery late into the next hour is skipped", async () => {
  for (let hour = 0; hour < 24; hour += 1) {
    const at = (minute) => scheduledJobs(`2026-10-03T${String(hour).padStart(2, "0")}:${minute}:00+08:00`).map((item) => item.job_id);
    assert.ok(at("30").includes("frontend_pages"));
    for (const minute of ["00", "05", "15", "17"]) assert.ok(!at(minute).includes("frontend_pages"));
  }
  const api = fixture();
  const late = await check(api, "2026-10-14T13:01:00+08:00", onlyJob("frontend_pages"), "2026-10-14T12:30:00+08:00");
  assert.deepEqual(late.find((item) => item.job_id === "frontend_pages"),
    { job_id: "frontend_pages", target_slot: "2026-10-14T04:30:00Z", action: "skip", reason: "stale_cron_delivery" });
  assert.equal(api.posts().length, 0);
  // A deploy already running (a code push) is not replaced; the next hour deploys.
  const busy = fixture({ activeWorkflows: { "pages-deploy.yml": { in_progress: [run("2026-10-14T04:28:00Z", { status: "in_progress" })] } } });
  const [result] = await check(busy, "2026-10-14T12:30:00+08:00", onlyJob("frontend_pages"));
  assert.equal(result.reason, "workflow_active");
  assert.equal(busy.posts().length, 0);
});

test("maintenance jobs run once at their own slot and never on the shared 30-minute content slots", () => {
  for (let hour = 0; hour < 24; hour += 1) {
    const at = (minute) => scheduledJobs(`2026-10-03T${String(hour).padStart(2, "0")}:${minute}:00+08:00`).map((item) => item.job_id);
    assert.ok(at("17").includes("frontend_watchdog"));
    assert.equal(at("30").includes("storage_retention"), hour === 4);
    assert.equal(at("30").includes("twitch_archive"), hour === 3);
    const credentials = ["credentials_frontend", "credentials_backend", "credentials_twitch", "credentials_content"];
    assert.equal(credentials.filter((id) => at("30").includes(id)).length, hour === 5 ? 4 : 0);
    for (const minute of ["00", "05", "15"]) {
      assert.ok(!at(minute).some((id) => ["frontend_watchdog", "storage_retention", "twitch_archive", ...credentials].includes(id)));
    }
  }
});
