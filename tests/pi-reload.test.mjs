// Exercise the Pi extension's reload mailbox and team registration on a private tmux server.
// Pi's lifecycle and command dispatch are stubbed: no model or real TUI needed.
import assert from "node:assert/strict";
import { execFile, execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";

const { default: extension } = await import(pathToFileURL(process.argv[2]).href);
const exec = promisify(execFile);
const directory = mkdtempSync(join(tmpdir(), "wmux-pi-"));
const socket = join(directory, "reload ,;$.sock");
const tokenOption = "@workmux-pi-reload-token";
const requestOption = "@workmux-pi-reload-request";
const resultOption = "@workmux-pi-reload-result";
const expectedLaunchOption = "@workmux_team_expected_launch";
const teamRegistrationOption = "@workmux_team_registration";
const launch = "a".repeat(32);
const startup = { reason: "startup" };
const commandName = "workmux-internal-reload";
const tmux = (...args) => execFileSync("tmux", ["-S", socket, ...args], {
  encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: 2500,
  env: { ...process.env, HOME: directory, XDG_CONFIG_HOME: directory, XDG_RUNTIME_DIR: directory,
         ENV: "/dev/null", BASH_ENV: "/dev/null" },
}).replace(/\n$/, "");

let pane;
const get = (option) => tmux("display-message", "-p", "-t", pane, `#{${option}}`);
const set = (option, value) => tmux("set-option", "-p", "-t", pane, option, value);
const unset = (option) => tmux("set-option", "-pqu", "-t", pane, option);
const signal = (token) => tmux("wait-for", "-S", `workmux-pi-reload-${token}`);
let idCounter = 0;
const nextId = () => (++idCounter).toString(16).padStart(32, "0");
const expire = () => Date.now() + 5000;
const request = ({ token = get(tokenOption), id = nextId(), deadline = expire() } = {}) => {
  unset(resultOption);
  set(requestOption, `${token}:${id}:${deadline}`);
  signal(get(tokenOption));
  return id;
};

async function until(read, predicate, label) {
  for (let i = 0; i < 200; i++) {
    const value = read();
    if (predicate(value)) return value;
    await delay(15);
  }
  throw new Error(`Timed out waiting for ${label}; got ${read()}`);
}
const resultIs = (expected) => until(() => get(resultOption), (value) => value === expected, expected);

let runtime;
let waiters = 0;
let waitStarts = 0;
let idle = true;
let queued = false;
let draft = "";
let reloads = 0;
let failReload = false;
let failRegistration = false;
let registrationAttempts = 0;
let deferDispatch = false;
let deferred;
let beforeDispatch = () => {};
let afterClaim = () => {};
let dispatch = Promise.resolve();
const messages = [];
const ctx = {
  mode: "tui", hasUI: true, cwd: "/work/repo", isIdle: () => idle, hasPendingMessages: () => queued,
  ui: { getEditorText: () => draft },
  reload: async () => {
    reloads++;
    if (failReload) throw new Error("Reload failed");
    await runtime.fire("session_shutdown", { reason: "reload" });
    runtime = load();
    await runtime.fire("session_start", { reason: "reload" });
  },
};

function load() {
  const handlers = new Map();
  const commands = new Map();
  let commandAvailable = true;
  extension({
    on: (name, handler) => handlers.set(name, handler),
    registerCommand: (name, command) => commands.set(name, command),
    getCommands: () => commandAvailable
      ? [...commands].map(([name, { description }]) => ({ source: "extension", name, description }))
      : [],
    async exec(command, args, options) {
      if (command === "workmux") return { code: 0, stdout: "" };
      if (args.at(-1).includes(teamRegistrationOption)) {
        registrationAttempts++;
        if (failRegistration) throw new Error("tmux not available");
      }
      assert.equal(command, "tmux");
      assert.deepEqual(args.slice(0, 2), ["-S", socket]);
      const waiting = args[2] === "wait-for";
      if (waiting) {
        waitStarts++;
        waiters++;
      }
      try {
        const { stdout } = await exec(command, args, options);
        if (stdout === "accepted\n" && args.at(-1).includes(":reloading:")) afterClaim();
        return { code: 0, stdout };
      } catch (error) {
        if (error.code === "ABORT_ERR") return { code: 143, stdout: "" };
        throw error;
      } finally {
        if (waiting) waiters--;
      }
    },
    sendUserMessage(text, options) {
      assert.deepEqual(options, { expandPromptTemplates: true });
      assert.equal(text, `/${commandName}`);
      messages.push(text);
      beforeDispatch();
      const invoke = () => commands.get(commandName).handler("", ctx);
      if (deferDispatch) deferred = invoke;
      else dispatch = invoke();
    },
  });
  return {
    fire: (name, event = {}, context = ctx) => handlers.get(name)(event, context),
    invoke: (args) => commands.get(commandName).handler(args, ctx),
    hideCommand: () => { commandAvailable = false; },
  };
}

const realWrite = process.stdout.write;
const terminalWrites = [];
const ttyDescriptor = Object.getOwnPropertyDescriptor(process.stdout, "isTTY");
try {
  tmux("-f", "/dev/null", "new-session", "-d", "-s", "pi-reload-test", "sleep 60");
  pane = tmux("display-message", "-p", "#{pane_id}");
  process.env.TMUX = `${socket},123,0`;
  process.env.TMUX_PANE = pane;
  Object.defineProperty(process.stdout, "isTTY", { value: true, configurable: true });
  process.stdout.write = (chunk) => { terminalWrites.push(chunk); return true; };

  runtime = load();
  process.env.WORKMUX_TEAM_LAUNCH_ID = launch;
  set(expectedLaunchOption, launch);
  for (const mode of ["rpc", "print", "json"]) {
    await runtime.fire("session_start", startup, { ...ctx, mode });
    assert.equal(get(tokenOption), "");
    assert.equal(get(teamRegistrationOption), "");
  }
  await runtime.fire("session_start", startup, { ...ctx, hasUI: false });
  Object.defineProperty(process.stdout, "isTTY", { value: false, configurable: true });
  await runtime.fire("session_start", startup);
  Object.defineProperty(process.stdout, "isTTY", { value: true, configurable: true });
  assert.equal(get(tokenOption), "");
  assert.equal(get(teamRegistrationOption), "");
  assert.equal(waiters, 0);

  delete process.env.WORKMUX_TEAM_LAUNCH_ID;
  await runtime.fire("session_start", startup);
  const token = get(tokenOption);
  assert.match(token, /^[0-9a-f]{32}$/);
  assert.equal(waiters, 1);
  assert.equal(get(teamRegistrationOption), "");

  process.env.WORKMUX_TEAM_LAUNCH_ID = launch;
  for (const reason of ["reload", "new", "resume", "fork"]) {
    await runtime.fire("session_start", { reason });
    assert.equal(get(teamRegistrationOption), "");
  }
  unset(expectedLaunchOption);
  await runtime.fire("session_start", startup);
  assert.equal(get(teamRegistrationOption), "");
  set(expectedLaunchOption, "b".repeat(32));
  await runtime.fire("session_start", startup);
  assert.equal(get(teamRegistrationOption), "");

  set(expectedLaunchOption, launch);
  const beforeMalformed = registrationAttempts;
  for (const [variable, value] of [
    ["WORKMUX_TEAM_LAUNCH_ID", `${launch}\n`],
    ["WORKMUX_TEAM_LAUNCH_ID", `${launch} ; display-message`],
    ["TMUX_PANE", `${pane}\n`],
    ["TMUX_PANE", `${pane} ; display-message`],
    ["TMUX", `${socket},123,0\n`],
  ]) {
    const saved = process.env[variable];
    process.env[variable] = value;
    await runtime.fire("session_start", startup);
    process.env[variable] = saved;
    assert.equal(get(teamRegistrationOption), "");
  }
  assert.equal(registrationAttempts, beforeMalformed);
  failRegistration = true;
  await runtime.fire("session_start", startup);
  failRegistration = false;
  assert.equal(get(teamRegistrationOption), "");
  await runtime.fire("session_start", startup);
  assert.equal(get(teamRegistrationOption), `${launch}:${process.pid}`);
  const firstRegistration = `${launch}:${process.pid + 1}`;
  set(teamRegistrationOption, firstRegistration);
  await runtime.fire("session_start", startup);
  assert.equal(get(teamRegistrationOption), firstRegistration);
  assert.equal(get(tokenOption), token);
  assert.equal(waiters, 1);
  assert.deepEqual(terminalWrites, []);

  await runtime.invoke(""); // A direct command is not a mailbox request.
  assert.equal(reloads, 0);
  const text = `/${commandName}`;
  assert.deepEqual(await runtime.fire("input", { source: "extension", text }), { action: "handled" });
  assert.equal(await runtime.fire("input", { source: "extension", text: "normal input" }), undefined);

  idle = false;
  await resultIs(`${request()}:busy`);
  idle = true;
  queued = true;
  await resultIs(`${request()}:busy`);
  queued = false;
  draft = " ";
  await resultIs(`${request()}:draft`);
  assert.equal(draft, " ");
  draft = "";
  await runtime.fire("ui_prompt_start", { title: "A blocking prompt" });
  await resultIs(`${request()}:prompt`);
  await runtime.fire("ui_prompt_end");
  assert.equal(messages.length, 0);

  // Each invalid wake gets its own round trip before the mailbox is replaced.
  const wrongToken = (token[0] === "0" ? "1" : "0") + token.slice(1);
  for (const invalid of [
    `${wrongToken}:${nextId()}:${expire()}`,
    `${token}:${nextId()}:0`,
    `${token}:${nextId()}:${expire()}; arbitrary prompt`,
  ]) {
    unset(resultOption);
    set(requestOption, invalid);
    const previous = waitStarts;
    signal(token);
    await until(() => waitStarts, (value) => value > previous, "listener rearmed after invalid request");
    assert.equal(get(resultOption), "");
  }
  assert.equal(messages.length, 0);

  deferDispatch = true;
  await resultIs(`${request()}:busy`);
  deferDispatch = false;
  await deferred();
  assert.equal(reloads, 0);

  beforeDispatch = () => { draft = "started typing"; };
  await resultIs(`${request()}:draft`);
  beforeDispatch = () => {};
  draft = "";
  afterClaim = () => { draft = "typed while marking the mailbox"; };
  await resultIs(`${request()}:draft`);
  afterClaim = () => {};
  assert.equal(reloads, 0);
  draft = "";

  failReload = true;
  await resultIs(`${request()}:failed`);
  await dispatch;
  assert.equal(reloads, 1);
  assert.equal(get(tokenOption), token);
  failReload = false;

  // Runtime replacement is the only way to claim success; a later session
  // start must not accept another process's marker.
  await runtime.fire("session_shutdown", { reason: "new" });
  assert.equal(get(tokenOption), "");
  assert.equal(get(teamRegistrationOption), firstRegistration);
  unset(teamRegistrationOption);
  const fakeId = nextId();
  set(requestOption, `${token}:${fakeId}:${expire()}`);
  set(resultOption, `${fakeId}:reloading:${process.pid + 1}`);
  runtime = load();
  await runtime.fire("session_start", { reason: "reload" });
  assert.equal(get(resultOption), `${fakeId}:reloading:${process.pid + 1}`);
  const old = get(tokenOption);
  assert.notEqual(old, token);
  await resultIs(`${request()}:applied`);
  await dispatch;
  assert.equal(reloads, 2);
  assert.equal(get(teamRegistrationOption), "");
  const fresh = get(tokenOption);
  assert.match(fresh, /^[0-9a-f]{32}$/);
  assert.notEqual(fresh, old);
  await until(() => waiters, (value) => value === 1, "one live listener after reload");

  runtime.hideCommand();
  await resultIs(`${request()}:failed`);
  assert.equal(reloads, 2);
  const replacement = "b".repeat(32);
  set(tokenOption, replacement);
  await runtime.fire("session_shutdown", { reason: "quit" });
  assert.equal(get(tokenOption), replacement);
  await until(() => waiters, (value) => value === 0, "waiter aborted on shutdown");
} finally {
  process.stdout.write = realWrite;
  if (ttyDescriptor) Object.defineProperty(process.stdout, "isTTY", ttyDescriptor);
  else delete process.stdout.isTTY;
  try { tmux("kill-server"); } catch { /* already stopped */ }
  rmSync(directory, { recursive: true, force: true });
}
console.log("PASS: Pi reload mailbox, lifecycle, and team registration checks");
