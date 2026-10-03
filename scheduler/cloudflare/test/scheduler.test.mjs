import test from "node:test";
import assert from "node:assert/strict";
import worker, { checkScheduledJobs, probeSchedulerPermissions, scheduledJobs, SCHEDULE_JOBS } from "../src/worker.mjs";

const ENV = { GITHUB_ACTIONS_TOKEN: "offline-fixture-only",
  RADAR_ENABLED_JOBS: "steam_daily,steam_catchup,steam_growth,steam_content,frontend_insights,nintendo_daily" };
const json = (value, status = 200) => new Response(JSON.stringify(value), { status });
const epoch = (value) => Date.parse(value);
const iso = (value) => new Date(epoch(value)).toISOString().replace(".000Z", "Z");

function run(slot, overrides = {}) {
  return { id: 23456, status: "completed", conclusion: "success", run_attempt: 1,
    display_title: `Scheduled task | slot=${slot} | cloudflare`, created_at: slot, updated_at: slot, ...overrides };
}

function fixture({ runs = [], active = {}, activeWorkflows = {}, blockedRepo, dispatchError, responder } = {}) {
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
    if (url.hostname === "raw.githubusercontent.com") {
      assert.equal(options.headers.Authorization, undefined);
      return new Response(null, { status: 404 });
    }
    assert.equal(url.hostname, "api.github.com");
    assert.equal(options.headers.Authorization, "Bearer offline-fixture-only");
    assert.ok(SCHEDULE_JOBS.some((job) => url.pathname.startsWith(`/repos/danielet087/${job.repo}/actions/workflows/`)));
    if (blockedRepo && url.pathname.includes(`/repos/danielet087/${blockedRepo}/`)) return new Response("never log this body", { status: 403 });
    if (url.pathname.endsWith("/dispatches")) {
      assert.equal(options.method, "POST");
      if (dispatchError) throw new Error("private upstream details");
      return new Response(null, { status: 204 });
    }
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

function check(api, when, env = ENV, scheduled = when) {
  return checkScheduledJobs(env, { now: epoch(when), scheduledTime: epoch(scheduled), fetchImpl: api.fetchImpl });
}

test("Taiwan midnight, year boundaries and daily times produce exact UTC minute slots", () => {
  for (const [when, id, slot] of [
    ["2026-10-03T00:00:59+08:00", "steam_daily", "2026-10-02T16:00:00Z"],
    ["2027-01-01T00:00:01+08:00", "steam_daily", "2026-12-31T16:00:00Z"],
    ["2026-10-03T01:15:00+08:00", "steam_growth", "2026-10-02T17:15:00Z"],
    ["2026-10-03T07:30:00+08:00", "steam_content", "2026-10-02T23:30:00Z"],
    ["2026-10-03T19:30:00+08:00", "steam_content", "2026-10-03T11:30:00Z"],
    ["2026-10-03T08:30:00+08:00", "nintendo_daily", "2026-10-03T00:30:00Z"],
    ["2027-01-01T08:30:00+08:00", "nintendo_daily", "2027-01-01T00:30:00Z"],
    ["2026-10-03T12:17:00+08:00", "frontend_insights", "2026-10-03T04:17:00Z"],
    ["2026-10-03T12:05:00+08:00", "twitch", "2026-10-03T04:00:00Z"],
  ]) assert.deepEqual(scheduledJobs(when), [{ job_id: id, target_slot: slot }]);
  assert.throws(() => scheduledJobs("invalid"), /invalid_clock/);
});

test("Followers catchup is due only at Taiwan 03 through 23 inclusive", () => {
  for (let hour = 0; hour < 24; hour += 1) {
    const due = scheduledJobs(`2026-10-03T${String(hour).padStart(2, "0")}:00:00+08:00`);
    assert.equal(due.some((item) => item.job_id === "steam_catchup"), hour >= 3);
    assert.equal(due.some((item) => item.job_id === "steam_daily"), hour === 0);
  }
  assert.deepEqual(scheduledJobs("2026-10-03T02:15:00+08:00"), []);
  assert.deepEqual(scheduledJobs("2026-10-03T12:30:00+08:00"), []);
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
    ["2026-10-05T08:30:00+08:00", "nintendo_daily"],
    ["2026-10-05T12:17:00+08:00", "frontend_insights"],
  ]) {
    const api = fixture();
    assert.equal((await check(api, when))[0].action, "dispatch");
    const job = SCHEDULE_JOBS.find((item) => item.id === id);
    assert.equal(api.posts().length, 1);
    assert.equal(api.posts()[0].url.pathname, `/repos/danielet087/${job.repo}/actions/workflows/${job.workflow}/dispatches`);
    assert.deepEqual(JSON.parse(api.posts()[0].options.body), { ref: "main", inputs: {
      ...(id === "steam_daily" ? { refresh_today: "true" } : {}), target_slot: iso(when), trigger_source: "cloudflare",
    } });
  }
});

test("a matching successful slot skips and all other completed conclusions also consume the slot", async () => {
  for (const conclusion of ["success", "failure", "cancelled", "skipped", "timed_out", null]) {
    const when = "2026-10-06T00:00:00+08:00";
    const api = fixture({ runs: [run(iso(when), { conclusion })] });
    const [result] = await check(api, when);
    assert.equal(result.reason, conclusion === "success" ? "slot_completed" : "slot_already_attempted");
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
    ["2026-10-08T03:00:00+08:00", "steam-official-backlog-oneoff-20260923.yml"],
    ["2026-10-08T01:15:00+08:00", "steam-official-nearfirst-batch-once.yml"],
    ["2026-10-08T03:00:00+08:00", "steam-official-hour-stress-once.yml"],
  ]) {
    const api = fixture({ activeWorkflows: { [blocker]: { queued: [run(iso(when), { status: "queued" })] } } });
    const [result] = await check(api, when);
    assert.equal(result.reason, "workflow_active");
    assert.equal(result.blocking_workflow, blocker);
    assert.equal(api.posts().length, 0);
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
  ]) {
    const api = fixture();
    const [result] = await check(api, when, ENV, planned);
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
