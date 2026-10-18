/**
 * Desktop notifications for Ghostty, including over SSH and tmux.
 * Installed by `workmux install pi`; removed by `workmux uninstall pi`.
 * Requires `allow-passthrough on` (or `all`) in tmux.
 * Reload with /reload. Run /workmux-notify-test and switch away from Ghostty.
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

  pi.on("agent_start", () => {
    outcome = "completed";
  });

  pi.on("agent_before_settle", (event) => {
    outcome = event.outcome;
  });

  pi.on("ui_prompt_start", (event, ctx) => {
    notify(ctx, event.title ? `Input needed — ${event.title}` : "Input needed");
  });

  // A text question in the final reply also lands here. Don't guess completion
  // from punctuation: a settled run means Pi is ready for the user's next turn.
  pi.on("agent_settled", (_event, ctx) => {
    const message = outcome === "error"
      ? "Agent stopped with an error — attention needed"
      : outcome === "aborted"
        ? "Agent interrupted — ready for input"
        : "Finished — ready for input";
    notify(ctx, message);
  });

  pi.registerCommand("workmux-notify-test", {
    description: "Send a desktop notification and tmux bell after 3 seconds",
    handler: async (_args, ctx) => {
      if (ctx.mode !== "tui" || !process.stdout.isTTY) return;
      ctx.ui.notify("Switch away from Ghostty; notification in 3 seconds", "info");
      await new Promise((resolve) => setTimeout(resolve, 3000));
      notify(ctx, "Notification test");
    },
  });
}
