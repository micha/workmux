// Test the installed resource without a Pi session, model calls, or real terminal output.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const { default: extension } = await import(pathToFileURL(process.argv[2]).href);
const handlers = new Map();
const commands = new Map();
const calls = [];
let failExec = false;
let executing = 0;
let maxExecuting = 0;
extension({
  on: (name, handler) => handlers.set(name, handler),
  registerCommand: (name, definition) => commands.set(name, definition),
  exec: async (command, args, options) => {
    calls.push({ command, args, options });
    executing++;
    maxExecuting = Math.max(maxExecuting, executing);
    try {
      await Promise.resolve();
      if (failExec) throw new Error("workmux not installed");
      return { code: 0, stdout: "", stderr: "", killed: false };
    } finally {
      executing--;
    }
  },
});
const originalWrite = process.stdout.write;
const ttyDescriptor = Object.getOwnPropertyDescriptor(process.stdout, "isTTY");
const originalTmux = process.env.TMUX;
const originalPane = process.env.TMUX_PANE;
const originalTimeout = globalThis.setTimeout;
let output = "";
let checks = 0;
const ctx = { mode: "tui", cwd: "/work/repos/example", isIdle: () => true, ui: { notify() {} } };
const fire = (name, event = {}, context = ctx) => handlers.get(name)(event, context);
const state = () => calls.at(-1).args[2];

try {
  process.stdout.write = (chunk) => { output += chunk; return true; };
  Object.defineProperty(process.stdout, "isTTY", { value: true, configurable: true });
  delete process.env.TMUX;
  await fire("agent_start");
  await fire("agent_settled");
  assert.equal(output, "\x07\x1b]9;Pi (example): Finished — ready for input\x07");
  assert.equal(calls.length, 0); checks++;

  output = "";
  process.env.TMUX = "/tmp/test,1,0";
  process.env.TMUX_PANE = "%42";
  await fire("ui_prompt_start", { title: "Approve deployment?" });
  assert.equal(output, "\x07\x1bPtmux;\x1b\x1b]9;Pi (example): Input needed — Approve deployment?\x07\x1b\\"); checks++;

  output = "";
  await fire("ui_prompt_start", { kind: "custom" });
  assert.ok(output.includes("Input needed")); checks++;

  output = "";
  await fire("ui_prompt_start", { title: "Bad\x1b]9;injected\x07\n title" });
  assert.equal((output.match(/\x1b/g) || []).length, 4);
  assert.equal((output.match(/\x07/g) || []).length, 2); checks++;

  for (const mode of ["rpc", "json", "print"]) {
    const before = calls.length;
    output = "";
    await fire("agent_start", {}, { ...ctx, mode });
    await fire("ui_prompt_start", {}, { ...ctx, mode });
    await fire("ui_prompt_end", {}, { ...ctx, mode });
    await fire("agent_settled", {}, { ...ctx, mode });
    await fire("session_shutdown", {}, { ...ctx, mode });
    await commands.get("workmux-notify-test").handler("", { ...ctx, mode });
    assert.equal(output, "");
    assert.equal(calls.length, before); checks++;
  }

  Object.defineProperty(process.stdout, "isTTY", { value: false, configurable: true });
  const beforeNoTty = calls.length;
  output = "";
  await fire("agent_start");
  await fire("ui_prompt_start");
  await fire("agent_settled");
  assert.equal(output, "");
  assert.equal(calls.length, beforeNoTty); checks++;
  Object.defineProperty(process.stdout, "isTTY", { value: true, configurable: true });

  for (const [outcome, expected, expectedState] of [["error", "stopped with an error", "attention"],
                                                  ["aborted", "interrupted", "attention"],
                                                  ["completed", "Finished", "finished"]]) {
    output = "";
    await fire("agent_before_settle", { outcome });
    assert.equal(output, "");
    await fire("agent_settled");
    assert.ok(output.includes(expected));
    assert.equal(state(), expectedState); checks++;
  }

  await fire("agent_before_settle", { outcome: "error" });
  await fire("agent_start");
  output = "";
  await fire("agent_settled");
  assert.ok(output.includes("Finished"));
  assert.equal(state(), "finished"); checks++;

  await fire("session_start");
  assert.equal(state(), "idle");
  await fire("session_start", {}, { ...ctx, isIdle: () => false });
  assert.equal(state(), "working"); checks++;

  const start = calls.length;
  // Real Pi UI events can overlap. Verify order without awaiting each handler.
  const pending = [fire("agent_start"), fire("ui_prompt_start", { title: "Question" }),
                   fire("ui_prompt_end"), fire("agent_settled")];
  await Promise.all(pending);
  assert.deepEqual(calls.slice(start).map((call) => call.args[2]), ["working", "attention", "working", "finished"]);
  assert.equal(maxExecuting, 1);
  for (const call of calls.slice(start)) {
    assert.equal(call.command, "workmux");
    assert.deepEqual(call.args.slice(3), ["--pane", "%42", "--pid", String(process.pid)]);
    assert.equal(call.options.timeout, 2000);
  }
  checks++;

  const beforeMenu = calls.length;
  await fire("ui_prompt_start", { title: "Manual menu while idle" });
  await fire("ui_prompt_end");
  assert.equal(calls.length, beforeMenu); checks++;

  failExec = true;
  await fire("agent_start"); // Missing executable must not break the run.
  failExec = false;
  await fire("agent_settled");
  assert.equal(state(), "finished"); checks++;

  await fire("session_shutdown");
  assert.equal(state(), "idle"); checks++;
  delete process.env.TMUX_PANE;
  const beforeNoPane = calls.length;
  await fire("agent_start");
  await fire("agent_settled");
  assert.equal(calls.length, beforeNoPane); checks++;

  output = "";
  globalThis.setTimeout = (callback, milliseconds) => {
    assert.equal(milliseconds, 3000);
    queueMicrotask(callback);
  };
  await commands.get("workmux-notify-test").handler("", ctx);
  assert.ok(output.includes("Notification test")); checks++;
  assert.equal(handlers.has("agent_end"), false); checks++;
} finally {
  process.stdout.write = originalWrite;
  globalThis.setTimeout = originalTimeout;
  if (ttyDescriptor) Object.defineProperty(process.stdout, "isTTY", ttyDescriptor);
  else delete process.stdout.isTTY;
  if (originalTmux === undefined) delete process.env.TMUX;
  else process.env.TMUX = originalTmux;
  if (originalPane === undefined) delete process.env.TMUX_PANE;
  else process.env.TMUX_PANE = originalPane;
}
console.log(`PASS: ${checks} extension checks`);
