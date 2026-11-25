"""Exercise the real plugin event loop and claim transitions without host config."""

from pathlib import Path
import shutil
import subprocess
import unittest

LOAD_ADAPTER = r"""
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const source = readFileSync(process.argv[2], "utf8")
  .replace(/^import .*;\r?\n/gm, "")
  .replaceAll("import.meta.url", JSON.stringify(pathToFileURL(process.argv[2]).href))
  .replace("export default", `
const { execFile, readFileSync, randomUUID, setTimeout = globalThis.setTimeout,
        clearTimeout = globalThis.clearTimeout } = globalThis.joroTest;
export default`);
let instance = 0;
const loadPlugin = async () => (await import(
  "data:text/javascript;base64," + Buffer.from(source).toString("base64") + "#" + ++instance
)).default;
"""


@unittest.skipUnless(shutil.which("node"), "Node.js required for the OpenCode plugin test")
class OpenCodeTests(unittest.TestCase):
    def run_adapter(self, scenario):
        plugin = Path(__file__).resolve().parents[1] / "integrations/opencode/index.js"
        result = subprocess.run(["node", "--input-type=module", "-", str(plugin)],
                                input=LOAD_ADAPTER + scenario, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_failed_release_does_not_end_event_observation_or_disposal_cleanup(self):
        self.run_adapter(r"""
const claims = new Set();
const attempts = [];
const warnings = [];
let failed = false;
globalThis.joroTest = {
  randomUUID: () => "test",
  readFileSync: () => "{}",
  execFile: (_python, _args, _options, callback) => ({
    stdin: {
      on() {},
      end(text) {
        const value = JSON.parse(text);
        attempts.push(value);
        if (!value.active && value.work === "shell:one" && !failed) {
          failed = true;
          callback(new Error("locked"), "", "locked");
        } else {
          if (value.active) claims.add(value.work); else claims.delete(value.work);
          callback(null, "", "");
        }
      },
    },
  }),
};
let finished;
const end = new Promise(resolve => { finished = resolve; });
let finishIteration;
const iterationDone = new Promise(resolve => { finishIteration = resolve; });
const plugin = await loadPlugin();
const dispose = await plugin.setup({
  session: { hook: async () => ({ dispose: async () => {} }) },
  event: {
    async *subscribe({ signal }) {
      signal.addEventListener("abort", finished, { once: true });
      for (const event of [
        { type: "shell.created", data: { info: { id: "one" } } },
        { type: "shell.exited", data: { id: "one" } },
        { type: "shell.created", data: { info: { id: "two" } } },
        { type: "shell.exited", data: { id: "two" } },
      ]) yield event;
      finishIteration();
      await end;
    },
  },
});
const originalWarn = console.warn;
console.warn = (...args) => warnings.push(args.join(" "));
try {
  await Promise.race([
    iterationDone,
    new Promise((_, reject) => setTimeout(() => reject(new Error("listener stopped early")), 2000).unref()),
  ]);
  assert.equal(failed, true);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /locked/);
  assert.ok(attempts.some(value => value.work === "shell:two" && value.active));
  assert.ok(attempts.some(value => value.work === "shell:two" && !value.active));
  assert.deepEqual([...claims], ["shell:one"]);
  await dispose();
  assert.equal(claims.size, 0);
} finally {
  console.warn = originalWarn;
}
""")

    HANDOFF_HARNESS = r"""
const parent = "ses_parent";
const shell = "sh_returning";
const other = "sh_other";
const inbox = "msg_result";
const shellWork = "shell:" + shell;
const parentWork = "handoff:" + parent;
const sessionWork = "session:" + parent;
const otherWork = "shell:" + other;
const make = (type, data = {}) => ({ type, data });
const created = id => make("shell.created", { info: { id, metadata: { sessionID: parent } } });
const exited = id => make("shell.exited", { id, status: "exited" });
const job = (status, sessionID = parent, shellID = shell) => make("experimental.job.updated", {
  info: { kind: "shell", id: shellID, shellID, sessionID, status },
});
const enqueued = (sessionID = parent, jobID = shell, shellID = shell, inboxID = inbox) => make("session.inbox.enqueued", {
  sessionID, inboxID, item: { type: "synthetic", payload: { metadata: { shellID, jobID } } },
});
const delivered = make("session.inbox.delivered", { sessionID: parent, inboxID: inbox });
const life = status => make("session.execution." + status, { sessionID: parent });

async function setup(fail = () => false, hold = () => false) {
  const claims = new Set();
  const transitions = [];
  const attempts = [];
  const timers = new Map();
  let finishWrite;
  let next;
  let acknowledged;
  let idle;
  const hooks = {};
  globalThis.joroTest = {
    readFileSync: () => "{}",
    randomUUID: () => "fixture",
    setTimeout(fn, delay) {
      assert.ok(delay > 0, "cleanup retry must be delayed");
      const timer = { unref() {} };
      timers.set(timer, fn);
      return timer;
    },
    clearTimeout(timer) { timers.delete(timer); },
    execFile: (_python, _args, _options, callback) => ({
      stdin: {
        on() {},
        end(text) {
          const { work, active } = JSON.parse(text);
          attempts.push({ work, active });
          if (fail(work, active)) return callback(new Error("fixture write failure"));
          const complete = (error) => {
            if (error) return callback(error);
            if (active) claims.add(work); else claims.delete(work);
            transitions.push([...claims].sort());
            callback(null, "", "");
          };
          if (hold(work, active)) {
            assert.equal(finishWrite, undefined, "publisher must serialize writes");
            finishWrite = complete;
          } else complete();
        },
      },
    }),
  };
  const plugin = await loadPlugin();
  const dispose = await plugin.setup({
    session: {
      hook: async (name, cb) => { hooks[name] = cb; return { dispose: async () => {} }; },
      wait: () => new Promise(resolve => { idle = resolve; }),
    },
    event: {
      async *subscribe({ signal }) {
        signal.addEventListener("abort", () => next(null), { once: true });
        for (;;) {
          const event = await new Promise(resolve => { next = resolve; acknowledged?.(); });
          if (!event) break;
          yield event;
        }
      },
    },
  });
  return {
    transitions,
    attempts,
    timers,
    releaseWrite(error) {
      const finish = finishWrite;
      finishWrite = undefined;
      assert.ok(finish, "expected an in-flight write");
      finish(error);
    },
    async retry() {
      for (const [timer, fire] of [...timers]) {
        timers.delete(timer);
        fire();
      }
      await new Promise(resolve => setImmediate(resolve));
    },
    check: (...expected) => assert.deepEqual([...claims].sort(), expected.sort()),
    dispatch: async (...items) => {
      for (const event of items) await new Promise(resolve => { acknowledged = resolve; next(event); });
    },
    start: () => hooks.context({ sessionID: parent }),
    idle: async () => { idle(); await new Promise(resolve => setImmediate(resolve)); },
    dispose,
  };
}
"""

    def test_background_shell_claim_transfers_and_cleans_up(self):
        self.run_adapter(self.HANDOFF_HARNESS + r"""
// Real 2.4.1 ordering with argument payloads omitted: the parent can be
// idle at shell exit, or already processing when the inbox result arrives.
for (const alreadyActive of [false, true]) {
  const t = await setup();
  if (alreadyActive) await t.start();
  await t.dispatch(created(shell), job("running"), exited(shell));
  t.check(...(alreadyActive ? [shellWork, sessionWork] : [shellWork]));
  await t.dispatch(enqueued());
  t.check(...(alreadyActive ? [shellWork, sessionWork, parentWork] : [shellWork, parentWork]));
  await t.dispatch(job("completed"));
  t.check(...(alreadyActive ? [sessionWork, parentWork] : [parentWork]));
  if (alreadyActive) await t.idle();
  if (!alreadyActive) await t.dispatch(life("started"));
  await t.dispatch(delivered, life("succeeded"));
  t.check();
  assert.ok(t.transitions.slice(0, -1).every(claims => claims.length > 0), t.transitions);
  await t.dispose();
}

// The job terminal can be processed before the shell exit. The old
// execution ending before delivery must not remove the new handoff.
{
  const t = await setup();
  await t.dispatch(created(shell), job("running"), enqueued(), job("completed"), life("succeeded"));
  t.check(shellWork, parentWork);
  await t.dispatch(exited(shell), delivered, life("succeeded"));
  t.check();
  assert.ok(t.transitions.slice(0, -1).every(claims => claims.length > 0), t.transitions);
  await t.dispose();
}

for (const stop of [
  life("failed"),
  life("interrupted"),
  make("session.inbox.cancelled", { sessionID: parent, inboxID: inbox }),
  make("session.deleted", { sessionID: parent }),
]) {
  const t = await setup();
  await t.dispatch(created(shell), job("running"), exited(shell), enqueued(), job("completed"));
  t.check(parentWork);
  await t.dispatch(stop);
  t.check();
  await t.dispose();
}

// Finishing one parent, or cancelling one of its inputs, cannot clear
// another parent's handoff or another queued result for the same parent.
{
  const t = await setup();
  const another = "ses_other";
  const third = "sh_third";
  await t.dispatch(created(shell), job("running"), exited(shell), enqueued(), job("completed"),
                   created(other), job("running", another, other), exited(other),
                   enqueued(another, other, other, "msg_other"), job("completed", another, other),
                   created(third), job("running", parent, third), exited(third),
                   enqueued(parent, third, third, "msg_third"), job("completed", parent, third));
  t.check(parentWork, "handoff:" + another);
  await t.dispatch(make("session.inbox.cancelled", { sessionID: parent, inboxID: inbox }));
  t.check(parentWork, "handoff:" + another);
  await t.dispatch(life("interrupted"));
  t.check("handoff:" + another);
  await t.dispose();
  t.check();
}

// Session metadata alone is not a returning job. Wrong parent/job IDs
// do not transfer; a staged revert admits the notification but won't run it.
{
  const t = await setup();
  await t.dispatch(created(other), exited(other));
  t.check();
  await t.dispatch(created(shell), job("running"), exited(shell), enqueued("ses_unrelated"),
                   enqueued(parent, "wrong-job"));
  t.check(shellWork);
  await t.dispatch(make("session.revert.staged", { sessionID: parent }), enqueued(), job("completed"));
  t.check();
  await t.dispatch(make("session.revert.cleared", { sessionID: parent }),
                   created(shell), job("running"), exited(shell), job("cancelled"));
  t.check();
  await t.dispose();
}

// A human wait suppresses both foreground contributions, not a second shell.
{
  const t = await setup();
  await t.start();
  await t.dispatch(created(shell), job("running"), exited(shell), enqueued(), job("completed"),
                   created(other), make("permission.asked", { sessionID: parent, id: "per" }),
                   make("form.created", { form: { sessionID: parent, id: "form" } }));
  t.check(otherWork);
  await t.dispatch(make("permission.replied", { sessionID: parent, requestID: "per" }));
  t.check(otherWork);
  await t.dispatch(make("form.replied", { form: { sessionID: parent, id: "form" } }));
  t.check(otherWork, parentWork, sessionWork);
  await t.dispatch(delivered, life("succeeded"));
  await t.idle();
  t.check(otherWork);
  await t.dispose();
  t.check();
}

// A failed parent write must not release the shell. A persistent failure
// still releases the retained shell when the parent is interrupted.
const warn = console.warn;
console.warn = () => {};
try {
  for (const failCount of [1, 2]) {
    let remaining = failCount;
    const t = await setup((work, active) => work === parentWork && active && remaining-- > 0);
    await t.dispatch(created(shell), job("running"), exited(shell), enqueued());
    t.check(shellWork);
    await t.dispatch(job("completed"));
    t.check(failCount === 1 ? parentWork : shellWork);
    await t.dispatch(life("interrupted"));
    t.check();
    await t.dispose();
  }
} finally {
  console.warn = warn;
}
""")

    def test_failed_final_release_retries_without_events_and_cannot_clear_new_handoff(self):
        self.run_adapter(self.HANDOFF_HARNESS + r"""
const tick = () => new Promise(resolve => setImmediate(resolve));
const release = (work, active) => work === parentWork && !active;
const close = async t => {
  await t.dispatch(created(shell), job("running"), exited(shell), enqueued(), job("completed"));
  await t.dispatch(delivered, life("succeeded"));
  t.check(parentWork);
};
const newShell = t => t.dispatch(created(other), job("running", parent, other), exited(other));
const nextResult = enqueued(parent, other, other, "msg_next");
const finishNext = t => t.dispatch(job("completed", parent, other),
  make("session.inbox.delivered", { sessionID: parent, inboxID: "msg_next" }), life("succeeded"));
const warn = console.warn;
console.warn = () => {};
try {
  // No events are emitted after the final execution event. Even repeated
  // failures must stay queued without spinning.
  for (const count of [1, 2]) {
    let failures = count;
    const t = await setup((work, active) => release(work, active) && failures-- > 0);
    await close(t);
    for (let attempt = 1; attempt < count; attempt++) {
      assert.equal(t.timers.size, 1);
      await t.retry();
      t.check(parentWork);
    }
    await t.retry();
    t.check();
    assert.equal(t.attempts.filter(({ work, active }) => release(work, active)).length, count + 1);
    assert.equal(t.timers.size, 0);
    await t.dispose();
  }

  // The result can arrive before the timer, or while a retry is queued
  // behind another publication. Neither ordering may issue an old removal.
  for (const queued of [false, true]) {
    let failures = 1;
    const t = await setup((work, active) => release(work, active) && failures-- > 0,
      (work, active) => queued && work === sessionWork && active);
    await close(t);
    await newShell(t);
    const start = queued ? t.start() : null;
    if (queued) await t.retry();
    const admit = t.dispatch(nextResult);
    await tick();
    if (queued) t.releaseWrite();
    await Promise.all([admit, start]);
    await t.retry();
    assert.equal(t.attempts.filter(({ work, active }) => release(work, active)).length, 1);
    t.check(otherWork, parentWork, ...(queued ? [sessionWork] : []));
    await finishNext(t);
    if (queued) await t.idle();
    t.check();
    await t.dispose();
  }

  // An already-started release cannot be recalled. Its write must settle
  // before reacquiring the parent, with the new shell claim covering the gap.
  {
    let failures = 1;
    let pauses = 1;
    const t = await setup((work, active) => release(work, active) && failures-- > 0,
      (work, active) => release(work, active) && pauses-- > 0);
    await close(t);
    await newShell(t);
    await t.retry();
    const admit = t.dispatch(nextResult);
    await tick();
    t.check(otherWork, parentWork);
    const first = t.transitions.length;
    t.releaseWrite();
    await admit;
    t.check(otherWork, parentWork);
    assert.deepEqual(t.transitions.slice(first), [[otherWork], [parentWork, otherWork]]);
    await finishNext(t);
    t.check();
    await t.dispose();
  }

  // Disposing cancels the timer and cleans up a claim still owned after failure.
  {
    let failures = 1;
    const t = await setup((work, active) => release(work, active) && failures-- > 0);
    await close(t);
    assert.equal(t.timers.size, 1);
    await t.dispose();
    t.check();
    assert.equal(t.timers.size, 0);
  }
  for (const failedInFlight of [false, true]) {
    let failures = 1;
    let pauses = 1;
    const t = await setup((work, active) => release(work, active) && failures-- > 0,
      (work, active) => release(work, active) && pauses-- > 0);
    await close(t);
    await t.retry();
    const disposal = t.dispose();
    await tick();
    t.check(parentWork);
    t.releaseWrite(failedInFlight ? new Error("late write failure") : undefined);
    await disposal;
    t.check();
    assert.equal(t.timers.size, 0);
    assert.equal(t.attempts.filter(({ work, active }) => release(work, active)).length,
      failedInFlight ? 3 : 2);
  }
} finally {
  console.warn = warn;
}
""")


if __name__ == "__main__":
    unittest.main()
