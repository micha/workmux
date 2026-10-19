/* Local OpenCode TUI lifecycle reports; no configuration or dependencies needed. */
import { execFile } from "node:child_process";
import { basename } from "node:path";
import { promisify } from "node:util";

const exec = promisify(execFile);

export default async function WorkmuxPlugin({ client, directory }) {
  // Local server plugins can also load in run/serve/web/ACP processes. Never
  // send terminal payloads or pane state from those noninteractive backends.
  const headless = new Set(["run", "serve", "web", "acp", "debug"]);
  if (!process.stdout.isTTY || process.argv.slice(2).some(arg => headless.has(arg)) ||
      !process.env.TMUX || !process.env.TMUX_PANE) return {};

  const roots = new Map();
  const sessions = new Map();
  let reports = Promise.resolve();
  let reported;

  async function report(state) {
    if (state === reported) return;
    await exec("workmux", ["agent", "state", state, "--pane", process.env.TMUX_PANE,
                          "--pid", String(process.pid)], { timeout: 2000 }).catch(() => {});
    reported = state;
  }

  function notify(state) {
    const message = state === "attention" ? "Input needed" : "Finished — ready for input";
    const text = `OpenCode (${basename(directory) || directory}): ${message}`
      .replace(/[\x00-\x1f\x7f-\x9f]/g, " ").slice(0, 240);
    const osc = `\x1b]9;${text}\x07`;
    process.stdout.write(`\x07\x1bPtmux;${osc.replace(/\x1b/g, "\x1b\x1b")}\x1b\\`);
  }

  async function isRoot(id) {
    if (!roots.has(id)) {
      const result = await client.session.get({ path: { id } }).catch(() => undefined);
      if (!result?.data) return false;
      roots.set(id, !result.data.parentID);
    }
    return roots.get(id);
  }

  async function handle(event) {
    const p = event.properties || {};
    const id = p.sessionID;
    if (!id || !await isRoot(id)) return;
    const session = sessions.get(id) || { state: "idle", pending: new Set() };
    const before = session.pending.size ? "attention" : session.state;
    if (event.type === "session.status" && ["busy", "retry"].includes(p.status?.type)) {
      session.state = "working";
    } else if (event.type === "session.idle") {
      // Idle events are also emitted during startup; only finish an active run.
      if (session.state === "working") session.state = "finished";
    } else if (event.type === "session.error") {
      session.state = "attention";
    } else if (["permission.asked", "question.asked"].includes(event.type)) {
      session.pending.add(`${event.type.split(".")[0]}:${p.id}`);
    } else if (["permission.replied", "question.replied", "question.rejected"].includes(event.type)) {
      session.pending.delete(`${event.type.split(".")[0]}:${p.requestID}`);
    } else if (event.type === "session.deleted") {
      sessions.delete(id);
      roots.delete(id);
    } else {
      return;
    }
    if (event.type !== "session.deleted") sessions.set(id, session);
    const state = session.pending.size ? "attention" : session.state;
    if (state === "attention" && (before !== "attention" || event.type.endsWith(".asked"))) notify(state);
    if (state === "finished" && before !== "finished") notify(state);
    const states = [...sessions.values()].map(s => s.pending.size ? "attention" : s.state);
    await report(["attention", "working", "finished"].find(s => states.includes(s)) || "idle");
  }

  reports = reports.then(() => report("idle")).catch(() => {});
  return {
    event: ({ event }) => {
      // Root-session lookups and state reports must preserve event order even
      // though OpenCode dispatches asynchronous plugin event handlers together.
      reports = reports.then(() => handle(event)).catch(() => {});
      return reports;
    },
    dispose: () => {
      reports = reports.then(() => report("idle")).catch(() => {});
      return reports;
    },
  };
}
