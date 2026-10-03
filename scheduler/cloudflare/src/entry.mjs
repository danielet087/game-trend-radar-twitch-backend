import { WorkflowEntrypoint } from "cloudflare:workers";
import worker, { checkScheduledJobs } from "./worker.mjs";

// Ordinary Worker Cron events and HTTP behavior stay in the existing Worker.
export default worker;

const DAILY_CRON = "0 16 * * *";
const DAILY_JOB_ID = "steam_daily";

function logResult(result) {
  // A Workflow failure must not expose response bodies, fetch errors or secrets.
  const record = { component: "radar_workflow_scheduler", job_id: DAILY_JOB_ID };
  for (const key of ["action", "reason"]) {
    if (typeof result[key] === "string" && /^[a-z][a-z0-9_]{0,95}$/.test(result[key])) record[key] = result[key];
  }
  if (typeof result.target_slot === "string" && /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(result.target_slot)) {
    record.target_slot = result.target_slot;
  }
  if (/^[1-9]\d*$/.test(String(result.run_id || ""))) record.run_id = result.run_id;
  const output = JSON.stringify(record);
  if (result.action === "blocked") console.error(output);
  else console.log(output);
}

function blocked(reason) {
  logResult({ action: "blocked", reason });
  return new Error("steam_daily_scheduler_blocked");
}

export class SteamDailyScheduler extends WorkflowEntrypoint {
  async run(event, step) {
    const schedule = event?.schedule;
    // Manual API-created instances have no native schedule. Parameters cannot
    // select a destination or provide a replacement schedule to this adapter.
    if (!schedule || schedule.cron !== DAILY_CRON ||
        typeof schedule.scheduledTime !== "number" || !Number.isFinite(schedule.scheduledTime)) {
      throw blocked("workflow_schedule_required");
    }
    let results;
    try {
      results = await step.do("dispatch-steam-daily-slot", {
        // GitHub may accept POST even if its response is lost. Preserve the
        // controller's one-attempt policy instead of Workflow automatic retries.
        retries: { limit: 0, delay: "1 second", backoff: "constant" },
        timeout: "2 minutes",
      }, async () => checkScheduledJobs(this.env, {
        now: Date.now(),
        scheduledTime: schedule.scheduledTime,
        jobId: DAILY_JOB_ID,
      }));
    } catch {
      throw blocked("workflow_scheduler_unavailable");
    }
    if (!Array.isArray(results) || results.length !== 1 || !results[0] ||
        (results[0].job_id && results[0].job_id !== DAILY_JOB_ID)) {
      throw blocked("workflow_scheduler_invalid_result");
    }
    const result = results[0];
    logResult(result);
    if (result.action === "blocked") throw new Error("steam_daily_scheduler_blocked");
    return result;
  }
}
