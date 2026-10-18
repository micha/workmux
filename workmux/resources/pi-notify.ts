/**
 * Pi sidebar state and OSC 9 desktop notifications, including over SSH and tmux.
 * Installed by `workmux install pi`; removed by `workmux uninstall pi`.
 * Desktop notifications require `allow-passthrough on` (or `all`) in tmux.
 * Reload with /reload. Run /workmux-notify-test and switch away from your terminal.
 */
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

export default function (pi: ExtensionAPI) {
  let outcome: "completed" | "aborted" | "error" = "completed";
  let active = false;
  let reports = Promise.resolve();

  function report(ctx: ExtensionContext, state: "idle" | "working" | "attention" | "finished"): Promise<void> {
    const pane = process.env.TMUX_PANE;
    if (ctx.mode !== "tui" || !process.stdout.isTTY || !process.env.TMUX || !pane) return Promise.resolve();
    // Serialize reports: blocking UI events can overlap with other lifecycle events.
    // Reporting is best-effort; missing workmux, a closed pane, or a timeout must
    // never prevent the agent from answering or displaying a question.
    const args = ["agent", "state", state, "--pane", pane, "--pid", String(process.pid)];
    reports = reports.then(async () => {
      await pi.exec("workmux", args, { timeout: 2000 });
    }).catch(() => {});
    return reports;
  }

  pi.on("session_start", (_event, ctx) => {
    active = !ctx.isIdle();
    return report(ctx, active ? "working" : "idle");
  });

  pi.on("session_shutdown", (_event, ctx) => report(ctx, "idle"));

  pi.on("agent_start", (_event, ctx) => {
    outcome = "completed";
    active = true;
    return report(ctx, "working");
  });

  pi.on("agent_before_settle", (event) => {
    outcome = event.outcome;
  });

  pi.on("ui_prompt_start", (event, ctx) => {
    notify(ctx, event.title ? `Input needed — ${event.title}` : "Input needed");
    if (active) return report(ctx, "attention");
  });

  pi.on("ui_prompt_end", (_event, ctx) => {
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
