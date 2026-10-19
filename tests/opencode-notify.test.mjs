// Exercise installed plugin events without OpenCode, providers, or tmux writes.
import assert from "node:assert/strict";
import childProcess from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { pathToFileURL } from "node:url";

const originalExec = childProcess.execFile;
const calls = [];
let fail = false, executing = 0, maxExecuting = 0;
childProcess.execFile = (command, args, options, callback) => {
  calls.push({ command, args, options });
  maxExecuting = Math.max(maxExecuting, ++executing);
  queueMicrotask(() => {
    executing--;
    callback(fail ? new Error("missing workmux") : null, "", "");
  });
};
syncBuiltinESMExports();
const { default: plugin } = await import(pathToFileURL(process.argv[2]).href);
const argv = process.argv;
const tty = Object.getOwnPropertyDescriptor(process.stdout, "isTTY");
const write = process.stdout.write;
const tmux = process.env.TMUX, pane = process.env.TMUX_PANE;
let output = "";
const lookups = [];
const client = { session: { get: async ({ path: { id } }) => {
  lookups.push(id);
  if (id === "missing") throw new Error("closed session");
  return { data: { id, parentID: id === "child" ? "root" : undefined } };
} } };
const input = { client, directory: "/repo/control\x1b\x07name" };
const state = () => calls.at(-1).args[2];
try {
  process.stdout.write = chunk => { output += chunk; return true; };
  Object.defineProperty(process.stdout, "isTTY", { value: true, configurable: true });
  process.env.TMUX = "/tmp/fake,1,0";
  process.env.TMUX_PANE = "%42";
  process.argv = ["node", "opencode"];
  const hooks = await plugin(input);
  const fire = (type, properties = {}) => hooks.event({ event: { type, properties: { sessionID: "root", ...properties } } });
  await fire("session.idle");
  assert.equal(state(), "idle");
  assert.equal(output, "");
  await fire("session.status", { status: { type: "busy" } });
  assert.equal(state(), "working");
  assert.equal(output, "");
  await fire("permission.asked", { id: "p1" });
  assert.equal(state(), "attention");
  assert.ok(output.includes("Input needed"));
  assert.equal((output.match(/\x1b/g) || []).length, 4);
  assert.equal((output.match(/\x07/g) || []).length, 2);
  assert.equal(calls.at(-1).options.timeout, 2000);
  assert.deepEqual(calls.at(-1).args.slice(3), ["--pane", "%42", "--pid", String(process.pid)]);
  output = "";
  await fire("session.status", { status: { type: "busy" } });
  assert.equal(state(), "attention");
  assert.equal(output, "");
  await fire("question.asked", { id: "q1" });
  await fire("permission.replied", { requestID: "p1" });
  assert.equal(state(), "attention");
  await fire("question.replied", { requestID: "q1" });
  assert.equal(state(), "working");
  output = "";
  await fire("session.idle");
  assert.equal(state(), "finished");
  assert.ok(output.includes("Finished"));
  output = "";
  await fire("session.idle");
  assert.equal(output, "");
  const count = calls.length;
  await fire("session.status", { sessionID: "child", status: { type: "busy" } });
  await fire("session.idle", { sessionID: "child" });
  await fire("session.error", { sessionID: "missing" });
  assert.equal(calls.length, count);
  assert.equal(lookups.filter(id => id === "root").length, 1);
  await Promise.all([
    fire("session.status", { status: { type: "busy" } }),
    fire("question.asked", { id: "q2" }),
    fire("question.rejected", { requestID: "q2" }),
    fire("session.idle"),
  ]);
  assert.deepEqual(calls.slice(-4).map(c => c.args[2]), ["working", "attention", "working", "finished"]);
  assert.equal(maxExecuting, 1);
  await fire("session.error");
  assert.equal(state(), "attention");
  await fire("session.idle");
  assert.equal(state(), "attention");
  await fire("session.status", { sessionID: "other", status: { type: "busy" } });
  assert.equal(state(), "attention");
  await fire("session.deleted");
  assert.equal(state(), "working");
  fail = true;
  await fire("session.idle", { sessionID: "other" });
  assert.equal(state(), "finished");
  await hooks.dispose();
  assert.equal(state(), "idle");
  fail = false;
  output = "";
  const before = calls.length;
  for (const command of ["run", "serve", "web", "acp", "debug"]) {
    process.argv = ["node", "opencode", "--port", "1234", command];
    assert.deepEqual(await plugin(input), {});
  }
  process.argv = ["node", "opencode"];
  Object.defineProperty(process.stdout, "isTTY", { value: false, configurable: true });
  assert.deepEqual(await plugin(input), {});
  Object.defineProperty(process.stdout, "isTTY", { value: true, configurable: true });
  delete process.env.TMUX;
  assert.deepEqual(await plugin(input), {});
  assert.equal(calls.length, before);
  assert.equal(output, "");
} finally {
  childProcess.execFile = originalExec;
  syncBuiltinESMExports();
  process.argv = argv;
  process.stdout.write = write;
  if (tty) Object.defineProperty(process.stdout, "isTTY", tty);
  else delete process.stdout.isTTY;
  if (tmux === undefined) delete process.env.TMUX; else process.env.TMUX = tmux;
  if (pane === undefined) delete process.env.TMUX_PANE; else process.env.TMUX_PANE = pane;
}
console.log("PASS OpenCode lifecycle, serialization, root filtering, and headless safety");
