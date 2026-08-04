"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import type { ConnectionState, LogEntry, LogLevel } from "@/hooks/useJobStream";
import { ArrowDownIcon, CopyIcon } from "./icons";

/** Distance from the bottom (px) still treated as "pinned to the bottom". */
const STICK_THRESHOLD = 24;

export interface TerminalLogProps {
  logs: readonly LogEntry[];
  connection: ConnectionState;
  /** Drives the blinking cursor — hidden once the job reaches a terminal state. */
  running: boolean;
}

const LEVEL_STYLES: Record<LogLevel, string> = {
  info: "text-secondary",
  success: "text-success-text",
  error: "text-danger-text",
  system: "text-warning-text",
};

const LEVEL_GLYPHS: Record<LogLevel, string> = {
  info: "›",
  success: "✔",
  error: "✖",
  system: "!",
};

const CONNECTION_COPY: Record<ConnectionState, { label: string; dot: string }> = {
  connecting: { label: "connecting", dot: "bg-warning-solid" },
  open: { label: "live", dot: "bg-success-solid" },
  reconnecting: { label: "reconnecting", dot: "bg-warning-solid" },
  closed: { label: "closed", dot: "bg-line-strong" },
};

function formatTime(date: Date): string {
  return date.toLocaleTimeString("en-GB", { hour12: false });
}

/**
 * A dark, monospaced console that streams pipeline messages.
 *
 * Auto-scroll follows the "tail -f" convention: it sticks to the bottom while
 * the user is already there, and stops fighting them the moment they scroll up.
 */
export function TerminalLog({ logs, connection, running }: TerminalLogProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [pinned, setPinned] = useState(true);
  const [copied, setCopied] = useState(false);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    setPinned(distance <= STICK_THRESHOLD);
  }, []);

  const scrollToBottom = useCallback((behavior: ScrollBehavior = "smooth") => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior });
  }, []);

  // Layout effect so the jump happens in the same frame the line paints,
  // which avoids a visible flicker of the previous scroll position.
  useLayoutEffect(() => {
    if (pinned) scrollToBottom("smooth");
    // `pinned` is intentionally excluded: re-pinning is handled by the button.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [logs.length, scrollToBottom]);

  // Reset the transient "Copied" affordance.
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1600);
    return () => clearTimeout(timer);
  }, [copied]);

  const handleCopy = useCallback(async () => {
    const text = logs
      .map((line) => `[${formatTime(line.at)}] ${line.message}`)
      .join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // Clipboard access can be blocked (insecure origin, permissions).
      // Silently degrade — the log is still selectable by hand.
    }
  }, [logs]);

  const status = CONNECTION_COPY[connection];

  return (
    <section className="overflow-hidden rounded-xl border border-line bg-sunken shadow-[0_1px_0_0_rgba(255,255,255,0.04)_inset]">
      {/* ---- Title bar ---------------------------------------------------- */}
      <header className="flex items-center gap-3 border-b border-line-subtle bg-surface px-4 py-2.5">
        <div className="flex gap-1.5" aria-hidden>
          <span className="h-2.5 w-2.5 rounded-full bg-danger-solid/70" />
          <span className="h-2.5 w-2.5 rounded-full bg-warning-surface" />
          <span className="h-2.5 w-2.5 rounded-full bg-success-surface" />
        </div>

        <p className="flex-1 truncate font-mono text-[11px] tracking-wide text-tertiary">
          archx3d@pipeline — job stream
        </p>

        <span className="flex items-center gap-1.5 rounded-full border border-line-subtle px-2 py-0.5 font-mono text-[10px] tracking-wide text-secondary uppercase">
          <span
            className={[
              "h-1.5 w-1.5 rounded-full",
              status.dot,
              connection === "open" || connection === "reconnecting"
                ? "animate-pulse-soft"
                : "",
            ].join(" ")}
          />
          {status.label}
        </span>

        <button
          type="button"
          onClick={handleCopy}
          className="flex items-center gap-1.5 rounded-md px-2 py-1 font-mono text-[10px] tracking-wide text-tertiary uppercase transition-colors hover:bg-surface-hover hover:text-primary focus-visible:ring-1 focus-visible:ring-focus focus-visible:outline-none"
        >
          <CopyIcon className="h-3 w-3" />
          {copied ? "Copied" : "Copy"}
        </button>
      </header>

      {/* ---- Stream ------------------------------------------------------- */}
      <div className="relative">
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          role="log"
          aria-live="polite"
          aria-relevant="additions"
          aria-label="Pipeline output"
          tabIndex={0}
          className="scroll-slim h-64 overflow-y-auto px-4 py-3 font-mono text-[12.5px] leading-6 focus-visible:outline-none sm:h-72"
        >
          {logs.length === 0 ? (
            <p className="text-tertiary">
              Waiting for the first event from the pipeline…
            </p>
          ) : (
            logs.map((line) => (
              <p
                key={line.id}
                className="animate-rise-in flex gap-2.5 break-words whitespace-pre-wrap"
              >
                <span className="shrink-0 tabular-nums text-disabled select-none">
                  {formatTime(line.at)}
                </span>
                <span
                  className={`shrink-0 select-none ${LEVEL_STYLES[line.level]}`}
                  aria-hidden
                >
                  {LEVEL_GLYPHS[line.level]}
                </span>
                <span className={`min-w-0 ${LEVEL_STYLES[line.level]}`}>
                  {line.message}
                </span>
              </p>
            ))
          )}

          {running && (
            <p className="flex gap-2.5" aria-hidden>
              <span className="shrink-0 text-disabled select-none">
                {"".padEnd(8, " ")}
              </span>
              <span className="animate-pulse-soft inline-block h-4 w-2 translate-y-1 bg-accent-solid/80" />
            </p>
          )}
        </div>

        {/* Fade the top edge so scrolled content dissolves rather than clips. */}
        <div className="pointer-events-none absolute inset-x-0 top-0 h-6 bg-gradient-to-b from-[#0a0b0d] to-transparent" />

        {/* Escape hatch, shown only when the user has scrolled away. */}
        {!pinned && logs.length > 0 && (
          <button
            type="button"
            onClick={() => {
              setPinned(true);
              scrollToBottom();
            }}
            className="animate-rise-in absolute right-4 bottom-3 flex items-center gap-1.5 rounded-full border border-line bg-raised/90 px-3 py-1.5 font-mono text-[10px] tracking-wide text-secondary uppercase shadow-lg backdrop-blur transition-colors hover:border-accent-border hover:text-primary"
          >
            <ArrowDownIcon className="h-3 w-3" />
            Follow output
          </button>
        )}
      </div>
    </section>
  );
}
