// This Worker only monitors and dispatches. Python collection stays on GitHub.
const API_ROOT = "https://api.github.com";
const RAW_ROOT = "https://raw.githubusercontent.com";
const ACTIVE_STATUSES = ["queued", "in_progress", "waiting", "pending", "requested"];
const HOUR_MS = 3_600_000;
const REQUEST_TIMEOUT_MS = 15_000;
const MAX_RUN_PAGES = 5;
const RUNS_PER_PAGE = 10;
const MAX_ATTEMPTS = 2;
const COOLDOWN_MS = 10 * 60_000;
const FIRST_MINUTE = 5;
const CLOCK_TOLERANCE_MS = 120_000;

class WatchdogError extends Error {
  constructor(code) {
    super(code);
    this.code = code;
  }
}

export function hourSlot(time) {
  const epoch = typeof time === "number" ? time : Date.parse(time);
  if (!Number.isFinite(epoch)) throw new WatchdogError("invalid_clock");
  return new Date(Math.floor(epoch / HOUR_MS) * HOUR_MS).toISOString().replace(".000Z", "Z");
}

function timestamp(value) {
  if (typeof value !== "string" || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,3})?Z$/.test(value)) {
    throw new WatchdogError("invalid_timestamp");
  }
  const epoch = Date.parse(value);
  if (!Number.isFinite(epoch)) throw new WatchdogError("invalid_timestamp");
  if (new Date(epoch).toISOString().slice(0, 19) !== value.slice(0, 19)) throw new WatchdogError("invalid_timestamp");
  return epoch;
}

function config(env) {
  const owner = env.GITHUB_OWNER || "danielet087";
  const repo = env.GITHUB_REPO || "game-trend-radar-twitch-backend";
  const frontendRepo = env.FRONTEND_REPO || "game-trend-radar";
  const workflow = env.GITHUB_WORKFLOW_ID || "369223512";
  const branch = env.GITHUB_BRANCH || "main";
  const token = env.GITHUB_ACTIONS_TOKEN;
  // Hosts are fixed; neither configuration nor API pagination can redirect the secret.
  if (![owner, repo, frontendRepo].every((value) => /^[A-Za-z0-9_.-]+$/.test(value)) ||
      !/^\d+$/.test(workflow) || !/^[A-Za-z0-9_./-]+$/.test(branch) ||
      typeof token !== "string" || !token.trim()) {
    throw new WatchdogError("invalid_configuration");
  }
  return { owner, repo, frontendRepo, workflow, branch, token };
}

async function request(fetchImpl, url, options, label, allowMissing = false) {
  let response;
  try {
    response = await fetchImpl(url, {
      ...options,
      // workerd rejects redirect: "error" while Node's fetch accepts it.
      // Inspect redirects ourselves so credentials can never follow a Location.
      redirect: "manual",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch {
    throw new WatchdogError(`${label}_unavailable`);
  }
  if (response.status >= 300 && response.status < 400) {
    throw new WatchdogError(`${label}_redirect_rejected`);
  }
  if (allowMissing && response.status === 404) return null;
  if (!response.ok) throw new WatchdogError(`${label}_http_${response.status}`);
  return response;
}

async function readJson(response, label) {
  const maxBytes = label === "receipt" ? 16_384 : 524_288;
  try {
    if (Number(response.headers.get("Content-Length")) > maxBytes) {
      throw new WatchdogError(`${label}_too_large`);
    }
    const reader = response.body?.getReader();
    if (!reader) throw new WatchdogError(`${label}_invalid_json`);
    const decoder = new TextDecoder();
    let size = 0;
    let text = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > maxBytes) {
        await reader.cancel();
        throw new WatchdogError(`${label}_too_large`);
      }
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
    return JSON.parse(text);
  } catch (error) {
    if (error instanceof WatchdogError) throw error;
    throw new WatchdogError(`${label}_invalid_json`);
  }
}

function apiOptions(token) {
  return {
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "X-GitHub-Api-Version": "2026-03-10",
      "User-Agent": "game-trend-radar-twitch-watchdog",
      "Cache-Control": "no-cache",
    },
  };
}

function runsUrl(settings, query = {}) {
  const url = new URL(`${API_ROOT}/repos/${settings.owner}/${settings.repo}/actions/workflows/${settings.workflow}/runs`);
  for (const [key, value] of Object.entries(query)) url.searchParams.set(key, String(value));
  return url;
}

async function fetchRuns(fetchImpl, settings, query) {
  const response = await request(fetchImpl, runsUrl(settings, query), apiOptions(settings.token), "github_runs");
  const result = await readJson(response, "github_runs");
  if (!result || !Number.isInteger(result.total_count) || result.total_count < 0 ||
      !Array.isArray(result.workflow_runs) || result.workflow_runs.length > result.total_count ||
      (result.total_count > 0 && result.workflow_runs.length === 0)) {
    throw new WatchdogError("github_runs_invalid_shape");
  }
  for (const run of result.workflow_runs) {
    if (!run || !Number.isSafeInteger(run.id) || run.id <= 0 ||
        ![...ACTIVE_STATUSES, "completed"].includes(run.status)) {
      throw new WatchdogError("github_run_invalid_shape");
    }
  }
  return result;
}

async function readReceipt(fetchImpl, settings, now) {
  const url = new URL(`${RAW_ROOT}/${settings.owner}/${settings.frontendRepo}/${encodeURIComponent(settings.branch)}/data/twitch_collection_status.json`);
  url.searchParams.set("watchdog", String(now));
  // This public request deliberately has no Authorization header.
  const response = await request(fetchImpl, url, { headers: { Accept: "application/json", "Cache-Control": "no-cache" } }, "receipt", true);
  if (response === null) return null;
  const receipt = await readJson(response, "receipt");
  if (!receipt || receipt.schema_version !== 1 || typeof receipt.collection_complete !== "boolean") {
    throw new WatchdogError("receipt_invalid_shape");
  }
  const started = timestamp(receipt.collection_started_at);
  const completed = timestamp(receipt.completed_at);
  const generated = timestamp(receipt.generated_at);
  const observed = timestamp(receipt.observed_slot);
  const target = timestamp(receipt.target_slot);
  const historyPath = `data/twitch_history/${new Date(started + 8 * HOUR_MS).toISOString().slice(0, 10)}.json`;
  if (hourSlot(started) !== receipt.observed_slot || hourSlot(observed) !== receipt.observed_slot ||
      hourSlot(target) !== receipt.target_slot || target > started || completed < started || generated < started ||
      receipt.generated_at !== receipt.completed_at || completed > now + CLOCK_TOLERANCE_MS ||
      !/^[1-9]\d*$/.test(String(receipt.run_id || "")) ||
      receipt.history_path !== historyPath) {
    throw new WatchdogError("receipt_invalid_metadata");
  }
  return receipt;
}

async function activeRun(fetchImpl, settings) {
  // Filtered queries find an old blocked run even if many newer completed runs exist.
  const checks = await Promise.allSettled(ACTIVE_STATUSES.map((status) =>
    fetchRuns(fetchImpl, settings, { status, per_page: 1, page: 1 })));
  const failed = checks.find((result) => result.status === "rejected");
  if (failed) throw failed.reason;
  for (const check of checks) {
    if (check.value.total_count > 0) return check.value.workflow_runs[0];
  }
  return null;
}

async function recentRuns(fetchImpl, settings, slot, wholeDay = false) {
  const since = wholeDay ? new Date(`${taipeiDay(slot)}T00:00:00+08:00`).toISOString().replace(".000Z", "Z") :
    new Date(timestamp(slot) - 2 * HOUR_MS).toISOString().replace(".000Z", "Z");
  const runs = new Map();
  for (let page = 1; page <= MAX_RUN_PAGES; page += 1) {
    const result = await fetchRuns(fetchImpl, settings, { created: `>=${since}`, branch: settings.branch, per_page: RUNS_PER_PAGE, page });
    for (const run of result.workflow_runs) {
      if (!Number.isSafeInteger(run.id) || run.id <= 0 || typeof run.status !== "string") {
        throw new WatchdogError("github_run_invalid_shape");
      }
      runs.set(run.id, run);
    }
    if (runs.size >= result.total_count) return [...runs.values()];
    if (result.workflow_runs.length < RUNS_PER_PAGE) throw new WatchdogError("github_runs_incomplete_page");
  }
  // Do not infer 'no job' from an incomplete or unexpectedly busy result set.
  throw new WatchdogError("github_runs_page_limit");
}

function hourAttempts(runs, slot, now) {
  const start = timestamp(slot);
  const matching = [];
  for (const run of runs) {
    const created = timestamp(run.created_at);
    const updated = timestamp(run.updated_at);
    if (created > now + CLOCK_TOLERANCE_MS || updated > now + CLOCK_TOLERANCE_MS) {
      throw new WatchdogError("github_run_future_timestamp");
    }
    const namedForSlot = typeof run.display_title === "string" && run.display_title.includes(`slot=${slot}`);
    if ((created >= start && created < start + HOUR_MS) || namedForSlot) {
      if (!Number.isInteger(run.run_attempt) || run.run_attempt < 1) throw new WatchdogError("github_run_invalid_attempt");
      matching.push({ ...run, created, updated });
    }
  }
  return matching;
}

export async function checkAndDispatch(env, { now = Date.now(), fetchImpl = fetch } = {}) {
  const settings = config(env);
  const slot = hourSlot(now);
  const base = { target_slot: slot };
  if (now - timestamp(slot) < FIRST_MINUTE * 60_000) return { ...base, action: "wait", reason: "before_collection_window" };

  const receipt = await readReceipt(fetchImpl, settings, now);
  if (receipt?.collection_complete === true && receipt.observed_slot === slot) {
    return { ...base, action: "skip", reason: "already_published", run_id: receipt.run_id };
  }
  const active = await activeRun(fetchImpl, settings);
  if (active) return { ...base, action: "wait", reason: "workflow_active", run_id: active.id };

  const runs = await recentRuns(fetchImpl, settings, slot);
  // Catch a run that appeared after the status-specific queries.
  const justStarted = runs.find((run) => run.status !== "completed");
  if (justStarted) return { ...base, action: "wait", reason: "workflow_active", run_id: justStarted.id };
  const attempts = hourAttempts(runs, slot, now);
  const attemptCount = attempts.reduce((count, run) => count + run.run_attempt, 0);
  if (attemptCount >= MAX_ATTEMPTS) return { ...base, action: "wait", reason: "hourly_attempt_limit", attempts: attemptCount };
  // A successful Actions run without a published receipt is not collection success.
  // Give both failures and delayed publication ten minutes before trying again.
  const mostRecent = Math.max(0, ...attempts.map((run) => Math.max(run.created, run.updated)));
  if (mostRecent && now - mostRecent < COOLDOWN_MS) {
    return { ...base, action: "wait", reason: "retry_cooldown", attempts: attemptCount };
  }

  const url = `${API_ROOT}/repos/${settings.owner}/${settings.repo}/actions/workflows/${settings.workflow}/dispatches`;
  const options = apiOptions(settings.token);
  await request(fetchImpl, url, {
    ...options,
    method: "POST",
    headers: { ...options.headers, "Content-Type": "application/json" },
    body: JSON.stringify({ ref: settings.branch, inputs: { target_slot: slot, trigger_source: "cloudflare", force: "false" } }),
  }, "github_dispatch");
  return { ...base, action: "dispatch", reason: "missing_published_collection", attempts: attemptCount + 1 };
}

// New jobs are opt-in through RADAR_ENABLED_JOBS; production enables all six.
// An absent or empty list retains the Twitch-only monitor for rollback.
// Destinations cannot be configured by callers or redirected by an API response.
export const SCHEDULE_JOBS = Object.freeze([
  { id: "twitch", repo: "game-trend-radar-twitch-backend", workflow: "369223512", enabled: true, minute: 5 },
  { id: "steam_daily", repo: "game-trend-radar-backend", workflow: "steam-two-phase.yml", enabled: false,
    minute: 0, taipeiHours: [0, 6, 12, 18], oncePerDay: true, inputs: { refresh_today: "true" } },
  { id: "steam_catchup", repo: "game-trend-radar-backend", workflow: "steam-official-daily-catchup-250.yml", enabled: false,
    minute: 0, taipeiHours: Array.from({ length: 21 }, (_, index) => index + 3),
    blockingWorkflows: ["steam-public-growth.yml", "steam-two-phase.yml", "steam-official-backlog-oneoff-20260923.yml",
      "steam-official-nearfirst-batch-once.yml", "steam-official-hour-stress-once.yml"] },
  { id: "steam_growth", repo: "game-trend-radar-backend", workflow: "steam-public-growth.yml", enabled: false,
    minute: 15, taipeiHours: [1, 7, 13, 19], oncePerDay: true, completionStep: "Require complete growth coverage",
    blockingWorkflows: ["steam-official-daily-catchup-250.yml", "steam-official-backlog-oneoff-20260923.yml",
      "steam-official-nearfirst-batch-once.yml", "steam-official-hour-stress-once.yml"] },
  { id: "steam_content", repo: "game-trend-radar-content-backend", workflow: "steam-catalog-reconcile.yml", enabled: false,
    minute: 30, taipeiHours: [7, 19] },
  // Keep credential use in the existing Twitch runner; Nintendo code and data
  // remain in their own repository. This reuses the existing 30-minute tick.
  { id: "nintendo_daily", repo: "game-trend-radar-twitch-backend", workflow: "collect-nintendo.yml", enabled: false,
    minute: 30, taipeiHours: [8] },
  { id: "frontend_insights", repo: "game-trend-radar", workflow: "radar-insights.yml", enabled: false, minute: 17 },
].map((job) => Object.freeze({ ...job,
  ...(job.taipeiHours ? { taipeiHours: Object.freeze(job.taipeiHours) } : {}),
  ...(job.blockingWorkflows ? { blockingWorkflows: Object.freeze(job.blockingWorkflows) } : {}),
  ...(job.inputs ? { inputs: Object.freeze(job.inputs) } : {}),
})));

function clockEpoch(time) {
  const epoch = typeof time === "number" ? time : Date.parse(time);
  if (!Number.isFinite(epoch) || !Number.isFinite(new Date(epoch).getTime())) throw new WatchdogError("invalid_clock");
  return epoch;
}

function minuteSlot(time) {
  return new Date(Math.floor(clockEpoch(time) / 60_000) * 60_000).toISOString().replace(".000Z", "Z");
}

function taipeiDay(time) {
  return new Date(clockEpoch(time) + 8 * HOUR_MS).toISOString().slice(0, 10);
}

export function scheduledJobs(time) {
  const epoch = clockEpoch(time);
  const minute = new Date(epoch).getUTCMinutes();
  const taipeiHour = new Date(epoch + 8 * HOUR_MS).getUTCHours();
  return SCHEDULE_JOBS.filter((job) => job.minute === minute &&
    (!job.taipeiHours || job.taipeiHours.includes(taipeiHour)))
    .map((job) => ({ job_id: job.id, target_slot: job.id === "twitch" ? hourSlot(epoch) : minuteSlot(epoch) }));
}

function enabledJobIds(env) {
  const value = env.RADAR_ENABLED_JOBS ?? "";
  if (typeof value !== "string") throw new WatchdogError("invalid_enabled_jobs");
  const ids = value.trim() ? value.split(",").map((id) => id.trim()) : [];
  if (new Set(ids).size !== ids.length || ids.some((id) =>
    !SCHEDULE_JOBS.some((job) => job.id === id && !job.enabled))) {
    throw new WatchdogError("invalid_enabled_jobs");
  }
  return new Set(["twitch", ...ids]);
}

function schedulerConfig(env) {
  const settings = config(env);
  if (settings.owner !== "danielet087" || settings.repo !== "game-trend-radar-twitch-backend" ||
      settings.frontendRepo !== "game-trend-radar" || settings.workflow !== "369223512" || settings.branch !== "main") {
    throw new WatchdogError("destination_not_allowed");
  }
  return settings;
}

function settingsForJob(settings, job) {
  return { ...settings, repo: job.repo, workflow: job.workflow };
}

function safeReason(error) {
  return error instanceof WatchdogError ? error.code : "unexpected_failure";
}

function namedForSlot(run, slot) {
  return typeof run.display_title === "string" &&
    run.display_title.split(/\s*[|·]\s*/).includes(`slot=${slot}`);
}

function dailySuccessCandidates(job, runs, slot, now) {
  const day = taipeiDay(slot);
  return runs.filter((run) => {
    if (run.status !== "completed" || run.conclusion !== "success") return false;
    const created = timestamp(run.created_at), completed = timestamp(run.updated_at);
    if (created > now + CLOCK_TOLERANCE_MS || completed > now + CLOCK_TOLERANCE_MS || completed < created) {
      throw new WatchdogError("github_run_invalid_completion_time");
    }
    if (taipeiDay(created) !== day || taipeiDay(completed) !== day) return false;
    const scheduled = job.taipeiHours.some((hour) => namedForSlot(run,
      new Date(`${day}T${String(hour).padStart(2, "0")}:${String(job.minute).padStart(2, "0")}:00+08:00`)
        .toISOString().replace(".000Z", "Z")));
    // Daily discovery's manual runs can advance one batch with refresh_today=false;
    // their successful conclusion does not prove today's daily refresh completed.
    const manualGrowth = job.id === "steam_growth" && typeof run.display_title === "string" &&
      run.display_title.split(/\s*[|·]\s*/).includes("slot=manual");
    return scheduled || manualGrowth;
  });
}

async function validatedDailySuccess(job, runs, settings, slot, fetchImpl, now) {
  for (const run of dailySuccessCandidates(job, runs, slot, now)) {
    if (!job.completionStep) return run;
    // Older growth workflows reported success even after partial/429 collection.
    // Require the explicit coverage gate, after publication, in this exact run.
    const response = await request(fetchImpl,
      `${API_ROOT}/repos/${settings.owner}/${settings.repo}/actions/runs/${run.id}/jobs?per_page=100`,
      apiOptions(settings.token), "github_jobs");
    const result = await readJson(response, "github_jobs");
    if (!result || !Number.isInteger(result.total_count) || result.total_count < 1 ||
        !Array.isArray(result.jobs) || result.jobs.length !== result.total_count) {
      throw new WatchdogError("github_jobs_invalid_shape");
    }
    if (result.jobs.some((entry) => Array.isArray(entry.steps) && entry.steps.some((step) =>
      step.name === job.completionStep && step.status === "completed" && step.conclusion === "success"))) return run;
  }
  return null;
}

// These maps close duplicate delivery races within one Worker isolate only.
// Durable protection remains the workflow's concurrency-held slot guard.
const inflightJobs = new Set();
const attemptedSlots = new Map();
const SLOT_MEMORY_MS = 48 * HOUR_MS;

async function checkAdditionalJob(job, settings, slot, fetchImpl, now) {
  const base = { job_id: job.id, target_slot: slot };
  const key = `${job.id}:${slot}`;
  for (const [oldKey, attemptedAt] of attemptedSlots) {
    if (now - attemptedAt > SLOT_MEMORY_MS) attemptedSlots.delete(oldKey);
  }
  if (attemptedSlots.has(key)) return { ...base, action: "wait", reason: "slot_dispatch_attempted" };
  if (inflightJobs.has(job.id)) return { ...base, action: "wait", reason: "controller_check_active" };
  inflightJobs.add(job.id);
  try {
    const jobSettings = settingsForJob(settings, job);
    const dailyRuns = job.oncePerDay ? await recentRuns(fetchImpl, jobSettings, slot, true) : null;
    if (dailyRuns) {
      const success = await validatedDailySuccess(job, dailyRuns, jobSettings, slot, fetchImpl, now);
      if (success) return { ...base, action: "skip", reason: "day_already_completed", run_id: success.id };
    }
    // GitHub concurrency retains just one pending run per group. Inspect known
    // group peers before dispatch so a new hourly run does not replace a pending
    // growth run. Catchup also waits for daily discovery to finish updating data.
    const blockingChecks = await Promise.allSettled([job.workflow, ...(job.blockingWorkflows || [])].map(async (workflow) => ({
      workflow, active: await activeRun(fetchImpl, { ...jobSettings, workflow }),
    })));
    const failedCheck = blockingChecks.find((result) => result.status === "rejected");
    if (failedCheck) throw failedCheck.reason;
    const activeCheck = blockingChecks.find((result) => result.value.active);
    if (activeCheck) return { ...base, action: "wait", reason: "workflow_active",
      run_id: activeCheck.value.active.id, blocking_workflow: activeCheck.value.workflow };
    const runs = dailyRuns || await recentRuns(fetchImpl, jobSettings, slot);
    const justStarted = runs.find((run) => run.status !== "completed");
    if (justStarted) return { ...base, action: "wait", reason: "workflow_active", run_id: justStarted.id };
    const matching = runs.filter((run) => namedForSlot(run, slot));
    if (matching.length) {
      const success = !job.oncePerDay && matching.find((run) => run.conclusion === "success");
      return { ...base, action: "skip", reason: success ? "slot_completed" : "slot_already_attempted",
        run_id: (success || matching[0]).id };
    }
    const options = apiOptions(jobSettings.token);
    // Mark before POST: a timeout can mean GitHub accepted the dispatch but its
    // response was lost. Do not blindly repeat a daily progress reset.
    attemptedSlots.set(key, now);
    await request(fetchImpl,
      `${API_ROOT}/repos/${jobSettings.owner}/${jobSettings.repo}/actions/workflows/${jobSettings.workflow}/dispatches`, {
        ...options,
        method: "POST",
        headers: { ...options.headers, "Content-Type": "application/json" },
        body: JSON.stringify({ ref: "main", inputs: { ...job.inputs, target_slot: slot, trigger_source: "cloudflare" } }),
      }, "github_dispatch");
    return { ...base, action: "dispatch", reason: "scheduled_slot_due" };
  } finally {
    inflightJobs.delete(job.id);
  }
}

export async function checkScheduledJobs(env, {
  now = Date.now(), scheduledTime = now, fetchImpl = fetch,
} = {}) {
  const epoch = clockEpoch(now);
  const scheduledEpoch = clockEpoch(scheduledTime);
  if (scheduledEpoch > epoch + CLOCK_TOLERANCE_MS) {
    return [{ action: "blocked", reason: "future_cron_delivery" }];
  }
  if (scheduledEpoch > epoch) return [{ action: "wait", reason: "before_scheduled_slot" }];
  // Select only the original Cron minute's jobs, even when delivery is a little
  // late. Hourly observations cannot cross an hour; daily tasks cannot cross
  // their Taiwan date. Preserve the original due instant in workflow inputs.
  const due = scheduledJobs(scheduledEpoch);
  if (!due.length) return [{ action: "skip", reason: "no_job_due" }];
  const enabled = enabledJobIds(env);
  let settings;
  try { settings = schedulerConfig(env); }
  catch (error) {
    return due.map((item) => ({ ...item, action: enabled.has(item.job_id) ? "blocked" : "skip",
      reason: enabled.has(item.job_id) ? safeReason(error) : "job_staged" }));
  }
  // Isolate failures: an inaccessible Steam or frontend repo cannot disable the
  // working Twitch monitor. Every network request retains timeout/redirect rules.
  const checkDue = async ({ job_id, target_slot }) => {
    if (!enabled.has(job_id)) return { job_id, target_slot, action: "skip", reason: "job_staged" };
    const hourly = ["twitch", "steam_catchup", "frontend_insights"].includes(job_id);
    if (hourly ? hourSlot(epoch) !== hourSlot(scheduledEpoch) : taipeiDay(epoch) !== taipeiDay(scheduledEpoch)) {
      return { job_id, target_slot, action: "skip", reason: "stale_cron_delivery" };
    }
    try {
      if (job_id === "twitch") {
        return { job_id, ...await checkAndDispatch(env, { now: epoch, fetchImpl }) };
      }
      const job = SCHEDULE_JOBS.find((item) => item.id === job_id);
      return await checkAdditionalJob(job, settings, target_slot, fetchImpl, epoch);
    } catch (error) {
      return { job_id, target_slot, action: "blocked", reason: safeReason(error) };
    }
  };
  // Daily retry slots share the minute with hourly Followers catchup. Check daily
  // first so catchup cannot race its state refresh before GitHub exposes the run.
  const dailyDue = due.find((item) => item.job_id === "steam_daily" && enabled.has(item.job_id));
  if (!dailyDue) return Promise.all(due.map(checkDue));
  const dailyResult = await checkDue(dailyDue);
  const remaining = await Promise.all(due.filter((item) => item !== dailyDue).map((item) => {
    if (item.job_id === "steam_catchup" && dailyResult.action !== "skip") {
      return { ...item, action: "wait", reason: "daily_refresh_priority", blocking_workflow: "steam-two-phase.yml" };
    }
    return checkDue(item);
  }));
  return [dailyResult, ...remaining];
}

export async function probeSchedulerPermissions(env, { fetchImpl = fetch } = {}) {
  // Explicit, read-only preflight. Never infer Actions write access from GET:
  // only a real approved dispatch can establish that permission conclusively.
  const settings = schedulerConfig(env);
  return Promise.all(SCHEDULE_JOBS.map(async (job) => {
    try {
      const response = await request(fetchImpl,
        `${API_ROOT}/repos/${settings.owner}/${job.repo}/actions/workflows/${job.workflow}`,
        apiOptions(settings.token), "github_workflow");
      const workflow = await readJson(response, "github_workflow");
      if (!workflow || !Number.isSafeInteger(workflow.id) || workflow.id <= 0 ||
          typeof workflow.path !== "string" || workflow.state !== "active") {
        throw new WatchdogError("github_workflow_not_ready");
      }
      const expectedPath = job.id === "twitch" ? ".github/workflows/collect.yml" : `.github/workflows/${job.workflow}`;
      if (workflow.path !== expectedPath) throw new WatchdogError("github_workflow_path_mismatch");
      return { job_id: job.id, repo: job.repo, workflow: job.workflow,
        action: "readable", reason: "actions_write_unverified" };
    } catch (error) {
      return { job_id: job.id, repo: job.repo, workflow: job.workflow, action: "blocked", reason: safeReason(error) };
    }
  }));
}

export default {
  async scheduled(controller, env, _ctx) {
    let results;
    try {
      const now = Date.now();
      results = await checkScheduledJobs(env, { now, scheduledTime: controller?.scheduledTime ?? now });
    } catch (error) {
      results = [{ action: "blocked", reason: safeReason(error) }];
    }
    for (const result of results) {
      // Do not log fetch errors, response bodies, headers, tokens or environment values.
      const output = JSON.stringify({ component: "radar_scheduler", ...result });
      if (result.action === "blocked") console.error(output);
      else console.log(output);
    }
    if (results.some((result) => result.action === "blocked")) throw new Error("scheduler_job_blocked");
    return results;
  },
  fetch() {
    // Even if a route is accidentally enabled, HTTP traffic cannot trigger a job.
    return new Response("Not found", { status: 404 });
  },
};
