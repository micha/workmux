// Test the installed resource without a Pi session, model calls, or real terminal output.
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const { default: extension } = await import(pathToFileURL(process.argv[2]).href);
const handlers = new Map();
const commands = new Map();
extension({
  on: (name, handler) => handlers.set(name, handler),
  registerCommand: (name, definition) => commands.set(name, definition),
});
const originalWrite = process.stdout.write;
const ttyDescriptor = Object.getOwnPropertyDescriptor(process.stdout, "isTTY");
const originalTmux = process.env.TMUX;
const originalTimeout = globalThis.setTimeout;
let output = "";
let checks = 0;
const ctx = { mode: "tui", cwd: "/work/repos/example", ui: { notify() {} } };
const fire = (name, event = {}, context = ctx) => handlers.get(name)(event, context);

try {
  process.stdout.write = (chunk) => { output += chunk; return true; };
  Object.defineProperty(process.stdout, "isTTY", { value: true, configurable: true });
  delete process.env.TMUX;
  fire("agent_start");
  fire("agent_settled");
  assert.equal(output, "\x07\x1b]9;Pi (example): Finished — ready for input\x07"); checks++;

  output = "";
  process.env.TMUX = "/tmp/test,1,0";
  fire("ui_prompt_start", { title: "Approve deployment?" });
  assert.equal(output, "\x07\x1bPtmux;\x1b\x1b]9;Pi (example): Input needed — Approve deployment?\x07\x1b\\"); checks++;

  output = "";
  fire("ui_prompt_start", { kind: "custom" });
  assert.ok(output.includes("Input needed")); checks++;

  output = "";
  fire("ui_prompt_start", { title: "Bad\x1b]9;injected\x07\n title" });
  assert.equal((output.match(/\x1b/g) || []).length, 4);
  assert.equal((output.match(/\x07/g) || []).length, 2); checks++;

  for (const mode of ["rpc", "json", "print"]) {
    output = "";
    fire("agent_settled", {}, { ...ctx, mode });
    fire("ui_prompt_start", {}, { ...ctx, mode });
    await commands.get("workmux-notify-test").handler("", { ...ctx, mode });
    assert.equal(output, ""); checks++;
  }

  Object.defineProperty(process.stdout, "isTTY", { value: false, configurable: true });
  output = "";
  fire("agent_settled");
  fire("ui_prompt_start");
  assert.equal(output, ""); checks++;
  Object.defineProperty(process.stdout, "isTTY", { value: true, configurable: true });

  for (const [outcome, expected] of [["error", "stopped with an error"], ["aborted", "interrupted"], ["completed", "Finished"]]) {
    output = "";
    fire("agent_before_settle", { outcome });
    assert.equal(output, "");
    fire("agent_settled");
    assert.ok(output.includes(expected)); checks++;
  }

  fire("agent_before_settle", { outcome: "error" });
  fire("agent_start");
  output = "";
  fire("agent_settled");
  assert.ok(output.includes("Finished")); checks++;

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
}
console.log(`PASS: ${checks} extension checks`);
