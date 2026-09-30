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

async function recentRuns(fetchImpl, settings, slot) {
  const since = new Date(timestamp(slot) - 2 * HOUR_MS).toISOString().replace(".000Z", "Z");
  const runs = new Map();
  for (let page = 1; page <= MAX_RUN_PAGES; page += 1) {
    const result = await fetchRuns(fetchImpl, settings, { created: `>=${since}`, per_page: RUNS_PER_PAGE, page });
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

export default {
  async scheduled(_controller, env, _ctx) {
    try {
      // Use actual execution time: a delayed Cron delivery must not backdate live data.
      const result = await checkAndDispatch(env);
      console.log(JSON.stringify({ component: "twitch_watchdog", ...result }));
      return result;
    } catch (error) {
      // Do not log fetch errors, response bodies, headers, tokens or environment values.
      const reason = error instanceof WatchdogError ? error.code : "unexpected_failure";
      console.error(JSON.stringify({ component: "twitch_watchdog", action: "blocked", reason }));
      throw new Error(reason);
    }
  },
  fetch() {
    // Even if a route is accidentally enabled, HTTP traffic cannot trigger a job.
    return new Response("Not found", { status: 404 });
  },
};
