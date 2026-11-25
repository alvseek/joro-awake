import { execFile } from "node:child_process";
import { readFileSync } from "node:fs";
import { randomUUID } from "node:crypto";

const RELEASE_RETRY_MS = 1000;

export default {
  id: "joro-keep-awake",
  async setup(ctx) {
    const config = JSON.parse(readFileSync(new URL("./config.json", import.meta.url), "utf8"));
    const producer = randomUUID();
    const owned = new Set();
    const sessions = new Map();
    const pending = new Map();
    const jobs = new Map();
    const handoffs = new Map();
    const staged = new Set();
    const releaseRetries = new Map();
    let stopped = false;
    let writes = Promise.resolve();
    const publish = (work, active) => new Promise((resolve, reject) => {
      const child = execFile(config.python, [config.helper, "event"], { windowsHide: true }, (error, _out, err) => {
        if (error) reject(new Error(err || "Joro activity update failed")); else resolve();
      });
      child.stdin.on("error", reject);
      child.stdin.end(JSON.stringify({ client: "opencode", producer, work, pid: process.pid, active }));
    });
    const queueWrite = (write) => {
      const job = writes.catch(() => {}).then(write);
      writes = job;
      return job;
    };
    const emit = (work, active) => queueWrite(() => publish(work, active));
    const set = async (work, active) => {
      if (stopped && active) return;
      await emit(work, active);
      if (stopped && active) { await emit(work, false); return; }
      if (active) owned.add(work); else owned.delete(work);
    };
    const done = async (id) => {
      sessions.delete(id);
      if (!handoffs.has(id)) pending.delete(id);
      await set("session:" + id, false);
    };
    const clearReleaseRetry = (id) => {
      clearTimeout(releaseRetries.get(id));
      releaseRetries.delete(id);
    };
    const retryRelease = (id) => {
      if (stopped || releaseRetries.get(id)) return;
      const timer = setTimeout(() => {
        // Keep the failed-release marker until a publication reconciles it.
        releaseRetries.set(id, null);
        void syncHandoff(id).catch((error) => console.warn("Joro activity update failed:", error.message));
      }, RELEASE_RETRY_MS);
      releaseRetries.set(id, timer);
      timer.unref();
    };
    const syncHandoff = (id) => queueWrite(async () => {
      if (stopped) return;
      const work = "handoff:" + id;
      // The intent and ownership must be read at the head of the queue, not
      // when a retry is scheduled. A new shell stays claimed until we settle
      // any release already in flight and (re)publish the parent claim.
      const active = handoffs.has(id) && !pending.get(id)?.size;
      if (active !== owned.has(work) || (active && releaseRetries.has(id))) {
        try {
          await publish(work, active);
        } catch (error) {
          if (active) throw error; // Never drop the shell on a failed acquisition.
          retryRelease(id);
          console.warn("Joro handoff release failed; retrying:", error.message);
          return;
        }
        if (active) owned.add(work); else owned.delete(work);
      }
      clearReleaseRetry(id);
    });
    const finishShell = async (job) => {
      if (!job.exited || !job.settled) return;
      // The job's terminal update follows result admission (if any). Keep the
      // shell claim if publishing the parent's claim fails.
      await syncHandoff(job.parent);
      await set("shell:" + job.shell, false);
      jobs.delete(job.shell);
    };
    const closeHandoffs = async (id, matches = () => true) => {
      const inbox = handoffs.get(id);
      if (!inbox) return;
      const closed = [];
      for (const [inboxID, job] of inbox) {
        if (!matches(inboxID, job)) continue;
        inbox.delete(inboxID);
        closed.push(job);
      }
      if (!inbox.size) {
        handoffs.delete(id);
        if (!sessions.has(id)) pending.delete(id);
      }
      await syncHandoff(id);
      for (const job of closed) if (jobs.get(job.shell) === job) await finishShell(job);
    };
    const started = async (request) => {
      if (stopped) return;
      const id = request.sessionID;
      if (!pending.get(id)?.size) await set("session:" + id, true);
      if (stopped) return;
      if (!sessions.has(id)) {
        const token = {};
        sessions.set(id, token);
        void ctx.session.wait({ sessionID: id }).finally(async () => {
          if (sessions.get(id) === token) await done(id);
        }).catch((error) => console.warn("Joro:", error.message));
      }
    };
    const guardedStart = async (request) => {
      try { await started(request); }
      catch (error) { console.warn("Joro monitoring failed:", error.message); }
    };
    const contextHook = await ctx.session.hook("context", guardedStart);
    const compactionHook = await ctx.session.hook("compaction", guardedStart);
    const controller = new AbortController();
    const observer = (async () => {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          try {
            const d = event.data ?? {};
            if (event.type === "shell.created") await set("shell:" + d.info.id, true);
            else if (["shell.exited", "shell.deleted"].includes(event.type)) {
              const job = jobs.get(d.id);
              if (job) {
                job.exited = true;
                await finishShell(job);
              } else if (owned.has("shell:" + d.id)) await set("shell:" + d.id, false);
            } else if (event.type === "experimental.job.updated" && d.info?.kind === "shell") {
              const { id, sessionID, shellID, status } = d.info;
              if (status === "running" && !jobs.has(shellID)) {
                jobs.set(shellID, { id, parent: sessionID, shell: shellID });
              } else if (["completed", "error", "cancelled"].includes(status)) {
                const job = jobs.get(shellID);
                if (job?.id === id && job.parent === sessionID) {
                  job.settled = true;
                  await finishShell(job);
                }
              }
            } else if (event.type === "session.inbox.enqueued" && d.item?.type === "synthetic") {
              const meta = d.item.payload?.metadata;
              const job = jobs.get(meta?.shellID);
              if (job && job.id === meta.jobID && job.parent === d.sessionID && !staged.has(job.parent)) {
                const inbox = handoffs.get(job.parent) ?? new Map();
                inbox.set(d.inboxID, job);
                handoffs.set(job.parent, inbox);
                await syncHandoff(job.parent);
              }
            } else if (event.type === "session.inbox.delivered") {
              const job = handoffs.get(d.sessionID)?.get(d.inboxID);
              if (job) job.delivered = true;
            } else if (event.type === "session.inbox.cancelled") {
              await closeHandoffs(d.sessionID, (id) => id === d.inboxID);
            } else if (event.type === "session.execution.succeeded") {
              // If the result is still queued, this success did not consume it.
              await closeHandoffs(d.sessionID, (_id, job) => job.delivered);
            } else if (["session.execution.failed", "session.execution.interrupted"].includes(event.type)) {
              await closeHandoffs(d.sessionID);
            } else if (event.type === "session.revert.staged") {
              staged.add(d.sessionID);
              await closeHandoffs(d.sessionID);
            } else if (["session.revert.cleared", "session.revert.committed", "session.deleted"].includes(event.type)) {
              staged.delete(d.sessionID);
              if (event.type === "session.deleted") {
                await closeHandoffs(d.sessionID);
                for (const job of jobs.values()) {
                  if (job.parent !== d.sessionID) continue;
                  job.settled = true;
                  await finishShell(job);
                }
              }
            } else if (["permission.asked", "form.created", "question.asked"].includes(event.type)) {
              const id = d.sessionID ?? d.form?.sessionID;
              if (sessions.has(id) || handoffs.has(id)) {
                const waiting = pending.get(id) ?? new Set();
                waiting.add(d.id ?? d.form?.id);
                pending.set(id, waiting);
                await Promise.all([set("session:" + id, false), syncHandoff(id)]);
              }
            } else if (["permission.replied", "form.replied", "form.cancelled", "question.replied", "question.rejected"].includes(event.type)) {
              const id = d.sessionID ?? d.form?.sessionID;
              const waiting = pending.get(id);
              waiting?.delete(d.requestID ?? d.id ?? d.form?.id);
              if (waiting?.size === 0) {
                pending.delete(id);
                await Promise.all([sessions.has(id) && set("session:" + id, true), syncHandoff(id)]);
              }
            }
          } catch (error) {
            // A failed publication must not end observation of every later job.
            console.warn("Joro activity update failed:", error.message);
          }
        }
      } catch (error) {
        if (!controller.signal.aborted) console.warn("Joro monitoring failed:", error.message);
      }
    })();
    return async () => {
      stopped = true;
      for (const id of releaseRetries.keys()) clearReleaseRetry(id);
      controller.abort();
      await observer;
      await contextHook.dispose();
      await compactionHook.dispose();
      await writes.catch(() => {});
      sessions.clear();
      pending.clear();
      handoffs.clear();
      jobs.clear();
      staged.clear();
      await Promise.all([...owned].map((work) => emit(work, false)));
    };
  },
};
