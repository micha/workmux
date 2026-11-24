/**
 * Pi sidebar state and OSC 9 desktop notifications, including over SSH and tmux.
 * Installed by `workmux install pi`; removed by `workmux uninstall pi`.
 * Desktop notifications require `allow-passthrough on` (or `all`) in tmux.
 * Reload with /reload. Run /workmux-notify-test and switch away from your terminal.
 */
import { randomBytes } from "node:crypto";
import { basename } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

// Never allow titles/paths supplied by tools to inject terminal controls.
function clean(text: string): string {
  return text.replace(/[\x00-\x1f\x7f-\x9f]/g, " ").replace(/\s+/g, " ").trim().slice(0, 240);
}

function notify(ctx: ExtensionContext, message: string): void {
  // Do not corrupt JSON/RPC/print output or notify from headless subagents.
  if (ctx.mode !== "tui" || !process.stdout.isTTY) return;

  const text = clean(`Pi (${basename(ctx.cwd) || ctx.cwd}): ${message}`);
  const osc = `\x1b]9;${text}\x07`;
  const sequence = process.env.TMUX
    ? `\x1bPtmux;${osc.replace(/\x1b/g, "\x1b\x1b")}\x1b\\`
    : osc;

  // The separate BEL sets tmux's window alert; BEL inside OSC only terminates it.
  process.stdout.write(`\x07${sequence}`);
}

const tokenOption = "@workmux-pi-reload-token";
const requestOption = "@workmux-pi-reload-request";
const resultOption = "@workmux-pi-reload-result";
const reloadCommand = "workmux-internal-reload";
const reloadDescription = "Internal command for workmux's tmux reload request";

type ReloadRequest = { value: string; token: string; id: string; expires: number };
type ReloadListener = {
  socket: string;
  pane: string;
  token: string;
  abort: AbortController;
  registration?: Promise<boolean>;
  pending?: ReloadRequest;
};

function parseRequest(value: string): ReloadRequest | undefined {
  const match = /^([0-9a-f]{32}):([0-9a-f]{32}):([0-9]{1,16})$/.exec(value);
  if (!match || match[0] !== value) return;
  const [, token, id, deadline] = match;
  const expires = Number(deadline);
  if (Number.isSafeInteger(expires) && expires > Date.now()) return { value, token, id, expires };
}

function setupReload(pi: ExtensionAPI, promptOpen: () => boolean) {
  let listener: ReloadListener | undefined;

  async function tmux(target: ReloadListener, args: string[], wait = false): Promise<string | undefined> {
    try {
      const result = await pi.exec("tmux", ["-S", target.socket, ...args], {
        maxBuffer: 4096,
        ...(wait ? { signal: target.abort.signal } : { timeout: 1000 }),
      });
      if (result.code === 0) return result.stdout.replace(/\n$/, "");
    } catch {
      // The server or executable may have disappeared. Reload transport is optional.
    }
  }

  const equals = (option: string, expected: string) => `#{==:#{${option}},${expected}}`;
  const owned = (target: ReloadListener) => equals(tokenOption, target.token);

  async function writeResult(target: ReloadListener, request: ReloadRequest, status: string,
                             previous = "", requireOwner = true): Promise<boolean> {
    const mailbox = `#{&&:${equals(requestOption, request.value)},${equals(resultOption, previous)}}`;
    const guard = requireOwner ? `#{&&:${owned(target)},${mailbox}}` : mailbox;
    const output = await tmux(target, ["if-shell", "-F", "-t", target.pane, guard,
      `set-option -p -t ${target.pane} ${resultOption} ${request.id}:${status} ; display-message -p accepted`]);
    return output === "accepted";
  }

  function blocked(ctx: ExtensionContext): "prompt" | "busy" | "draft" | undefined {
    if (promptOpen()) return "prompt";
    if (!ctx.isIdle() || ctx.hasPendingMessages()) return "busy";
    // Whitespace is a draft too; do not reset or replace the user's editor.
    if (ctx.ui.getEditorText() !== "") return "draft";
  }

  const hasReloadCommand = () => pi.getCommands().some((command) =>
    command.source === "extension" && command.name === reloadCommand && command.description === reloadDescription);

  // Pi's command dispatch precedes input hooks. If a dispatch stops resolving to
  // our command, consume the fixed internal command rather than send a model turn.
  const internalInput = `/${reloadCommand}`;
  pi.on("input", (event) => {
    if (event.source === "extension" && event.text === internalInput) return { action: "handled" };
  });

  pi.registerCommand(reloadCommand, {
    description: reloadDescription,
    handler: async (args, ctx) => {
      const target = listener;
      const request = target?.pending;
      if (!target || !request || args || ctx.mode !== "tui") return;
      target.pending = undefined;
      if (request.expires <= Date.now()) return;
      const refused = blocked(ctx);
      if (refused) {
        await writeResult(target, request, refused);
        return;
      }
      const handoff = `${request.id}:reloading:${process.pid}`;
      if (!await writeResult(target, request, `reloading:${process.pid}`)) return;
      // The editor or run may have changed while tmux was marking the request.
      if (target.abort.signal.aborted || request.expires <= Date.now()) return;
      const recheck = blocked(ctx);
      if (recheck) {
        await writeResult(target, request, recheck, handoff);
        return;
      }
      try {
        await ctx.reload();
      } catch {
        // Failures before runtime replacement still have a live listener below.
      }
      // After session_shutdown the old context is invalid. The replacement
      // session_start alone acknowledges an applied reload.
      if (!target.abort.signal.aborted) await writeResult(target, request, "failed", handoff);
    },
  });

  async function waitForRequests(target: ReloadListener, ctx: ExtensionContext): Promise<void> {
    const { signal } = target.abort;
    let seen = "";
    try {
      while (!signal.aborted) {
        if (await tmux(target, ["wait-for", `workmux-pi-reload-${target.token}`], true) === undefined) break;
        if (signal.aborted) break;
        const value = await tmux(target, ["display-message", "-p", "-t", target.pane, `#{${requestOption}}`]);
        const request = value && parseRequest(value);
        if (signal.aborted || !request || request.token !== target.token || request.id === seen) continue;
        seen = request.id;
        const refused = blocked(ctx);
        if (refused || !hasReloadCommand()) {
          await writeResult(target, request, refused ?? "failed");
          continue;
        }
        // A normal Pi dispatch enters the command handler inline. Pi can
        // defer dispatch during settlement even though isIdle() is true;
        // revoke the request if it wasn't consumed synchronously.
        target.pending = request;
        pi.sendUserMessage(internalInput, { expandPromptTemplates: true });
        if (target.pending === request) {
          target.pending = undefined;
          await writeResult(target, request, "busy");
        }
      }
    } finally {
      if (!signal.aborted) await stop();
    }
  }

  async function start(ctx: ExtensionContext, reason: string): Promise<void> {
    const pane = process.env.TMUX_PANE;
    const socket = /^(\/.+),[0-9]+,[0-9]+$/.exec(process.env.TMUX ?? "")?.[1];
    if (listener || ctx.mode !== "tui" || !ctx.hasUI || !process.stdout.isTTY ||
        !socket || !pane || !/^%[0-9]+$/.test(pane) || !hasReloadCommand()) return;
    const target: ReloadListener = { pane, socket, token: randomBytes(16).toString("hex"), abort: new AbortController() };
    listener = target;
    target.registration = (async () => {
      if (reason === "reload") {
        const value = await tmux(target, ["display-message", "-p", "-t", pane, `#{${requestOption}}`]);
        const request = value && parseRequest(value);
        if (request && !target.abort.signal.aborted) {
          await writeResult(target, request, "applied", `${request.id}:reloading:${process.pid}`, false);
        }
      }
      if (target.abort.signal.aborted) return false;
      return await tmux(target, ["set-option", "-p", "-t", pane, tokenOption, target.token]) !== undefined;
    })();
    if (await target.registration && !target.abort.signal.aborted) void waitForRequests(target, ctx).catch(() => {});
  }

  async function stop(): Promise<void> {
    const target = listener;
    if (!target) return;
    listener = undefined;
    target.abort.abort();
    // Do not wait for the request handler: ctx.reload() calls this shutdown
    // handler itself. Only wait for any short registration already underway.
    await target.registration;
    await tmux(target, ["if-shell", "-F", "-t", target.pane, owned(target),
      `set-option -pqu -t ${target.pane} ${tokenOption}`]);
  }

  return { start, stop };
}

export default function (pi: ExtensionAPI) {
  let outcome: "completed" | "aborted" | "error" = "completed";
  let active = false;
  let promptOpen = false;
  let reports = Promise.resolve();
  const reload = setupReload(pi, () => promptOpen);

  function report(ctx: ExtensionContext, state: "idle" | "working" | "attention" | "finished"): Promise<void> {
    const pane = process.env.TMUX_PANE;
    if (ctx.mode !== "tui" || !process.stdout.isTTY || !process.env.TMUX || !pane) return Promise.resolve();
    // Serialize reports: blocking UI events can overlap with other lifecycle events.
    // Reporting is best-effort; missing workmux, a closed pane, or a timeout must
    // never prevent the agent from answering or displaying a question.
    const args = ["agent", "state", state, "--pane", pane, "--pid", String(process.pid), "--root", ctx.cwd];
    reports = reports.then(async () => {
      await pi.exec("workmux", args, { timeout: 2000 });
    }).catch(() => {});
    return reports;
  }

  pi.on("session_start", async (event, ctx) => {
    active = !ctx.isIdle();
    await Promise.all([reload.start(ctx, event.reason).catch(() => {}), report(ctx, active ? "working" : "idle")]);
  });

  pi.on("session_shutdown", async (_event, ctx) => {
    await Promise.all([reload.stop(), report(ctx, "idle")]);
  });

  pi.on("agent_start", (_event, ctx) => {
    outcome = "completed";
    active = true;
    return report(ctx, "working");
  });

  pi.on("agent_before_settle", (event) => {
    outcome = event.outcome;
  });

  pi.on("ui_prompt_start", (event, ctx) => {
    promptOpen = true;
    notify(ctx, event.title ? `Input needed — ${event.title}` : "Input needed");
    if (active) return report(ctx, "attention");
  });

  pi.on("ui_prompt_end", (_event, ctx) => {
    promptOpen = false;
    if (active) return report(ctx, "working");
  });

  // A text question in the final reply also lands here. Don't guess completion
  // from punctuation: a settled run means Pi is ready for the user's next turn.
  pi.on("agent_settled", (_event, ctx) => {
    const message = outcome === "error"
      ? "Agent stopped with an error — attention needed"
      : outcome === "aborted"
        ? "Agent interrupted — ready for input"
        : "Finished — ready for input";
    active = false;
    notify(ctx, message);
    return report(ctx, outcome === "completed" ? "finished" : "attention");
  });

  pi.registerCommand("workmux-notify-test", {
    description: "Send a desktop notification and tmux bell after 3 seconds",
    handler: async (_args, ctx) => {
      if (ctx.mode !== "tui" || !process.stdout.isTTY) return;
      ctx.ui.notify("Switch away from your terminal; notification in 3 seconds", "info");
      await new Promise((resolve) => setTimeout(resolve, 3000));
      notify(ctx, "Notification test");
    },
  });
}
