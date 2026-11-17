"""Exercise the real plugin event loop with a failing publisher, without host config."""

from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node.js required for the OpenCode plugin test")
class OpenCodeTests(unittest.TestCase):
    def test_failed_release_does_not_end_event_observation_or_disposal_cleanup(self):
        plugin = Path(__file__).resolve().parents[1] / "integrations/opencode/index.js"
        result = subprocess.run(["node", "--input-type=module", "-", str(plugin)], input=r"""
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
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
const source = readFileSync(process.argv[2], "utf8")
  .replace(/^import .*;\r?\n/gm, "")
  .replaceAll("import.meta.url", JSON.stringify(pathToFileURL(process.argv[2]).href))
  .replace("export default", "const { execFile, readFileSync, randomUUID } = globalThis.joroTest;\nexport default");
const plugin = (await import("data:text/javascript;base64," + Buffer.from(source).toString("base64"))).default;
let finished;
const end = new Promise(resolve => { finished = resolve; });
let finishIteration;
const iterationDone = new Promise(resolve => { finishIteration = resolve; });
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
""", capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
