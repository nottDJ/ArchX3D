"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { jobStreamUrl } from "@/lib/api";
import {
  FAILED,
  FIRST_STEP,
  isTerminalStatus,
  parseJobEvent,
  type GenerationStep,
  type JobStatus,
} from "@/lib/generation";

/** Give up (and surface a failure) after this many consecutive failed attempts. */
const MAX_RECONNECT_ATTEMPTS = 5;

/** Hard cap on retained log lines so a chatty pipeline cannot exhaust memory. */
const MAX_LOG_LINES = 500;

/** Lifecycle of the underlying `EventSource`, surfaced for the UI. */
export type ConnectionState = "connecting" | "open" | "reconnecting" | "closed";

/** Severity used to colour a line in the terminal. */
export type LogLevel = "info" | "success" | "error" | "system";

export interface LogEntry {
  /** Monotonic, unique for the lifetime of the hook — safe as a React key. */
  readonly id: number;
  readonly at: Date;
  readonly message: string;
  readonly level: LogLevel;
  /** Absent for locally-generated system lines (connection notices, etc). */
  readonly status?: JobStatus;
}

interface StreamState {
  status: JobStatus;
  /**
   * The last status on the happy path. Retained across a failure so the
   * timeline can show *where* the pipeline broke rather than resetting.
   */
  currentStep: GenerationStep;
  logs: LogEntry[];
  connection: ConnectionState;
  /** Failure message, if the job failed or the stream was unrecoverable. */
  error: string | null;
}

export interface JobStream extends Readonly<StreamState> {
  /** Tear down and re-open the stream from scratch. Powers "Try again". */
  reconnect: () => void;
}

function createInitialState(): StreamState {
  return {
    status: FIRST_STEP,
    currentStep: FIRST_STEP,
    logs: [],
    connection: "connecting",
    error: null,
  };
}

/** Append a line while enforcing the retention cap. */
function withLog(logs: LogEntry[], entry: LogEntry): LogEntry[] {
  const next = [...logs, entry];
  return next.length > MAX_LOG_LINES ? next.slice(-MAX_LOG_LINES) : next;
}

/**
 * Subscribe to the backend's Server-Sent Events stream for one job.
 *
 * Strict Mode safety: the connection lives entirely inside the effect and is
 * closed in the cleanup, and the effect *resets* state on setup. React's
 * development double-mount therefore produces one discarded connection and one
 * live connection whose log has no duplicated lines.
 */
export function useJobStream(jobId: string): JobStream {
  const [state, setState] = useState<StreamState>(createInitialState);

  /** Bumping this re-runs the effect, which is exactly what a retry needs. */
  const [attempt, setAttempt] = useState(0);

  /** Log ids must stay unique across reconnects, so the counter outlives them. */
  const nextLogId = useRef(0);

  const reconnect = useCallback(() => setAttempt((n) => n + 1), []);

  useEffect(() => {
    if (!jobId) return;

    // A fresh connection means a fresh replay of the job's history.
    setState(createInitialState());

    let disposed = false;
    let failedAttempts = 0;

    const source = new EventSource(jobStreamUrl(jobId));

    /** Push a locally-authored line (not from the backend) into the console. */
    const pushSystemLog = (message: string, level: LogLevel = "system") => {
      if (disposed) return;
      setState((prev) => ({
        ...prev,
        logs: withLog(prev.logs, {
          id: nextLogId.current++,
          at: new Date(),
          message,
          level,
        }),
      }));
    };

    /** Close the socket and mark the stream finished. */
    const finish = (failure?: string) => {
      source.close();
      if (disposed) return;
      setState((prev) => ({
        ...prev,
        connection: "closed",
        status: failure ? FAILED : prev.status,
        error: failure ?? prev.error,
      }));
    };

    source.onopen = () => {
      if (disposed) return;
      failedAttempts = 0;
      setState((prev) => ({ ...prev, connection: "open" }));
    };

    source.onmessage = (event: MessageEvent<string>) => {
      if (disposed) return;

      const parsed = parseJobEvent(event.data);

      // Unrecognised payload: show it verbatim rather than silently dropping it.
      if (!parsed) {
        pushSystemLog(`Unrecognised payload: ${event.data}`);
        return;
      }

      const level: LogLevel =
        parsed.status === FAILED
          ? "error"
          : parsed.status === "COMPLETED"
            ? "success"
            : "info";

      setState((prev) => ({
        ...prev,
        status: parsed.status,
        // FAILED is off the happy path — keep the step we died on.
        currentStep:
          parsed.status === FAILED ? prev.currentStep : parsed.status,
        error: parsed.status === FAILED ? parsed.message || "Generation failed." : prev.error,
        logs: withLog(prev.logs, {
          id: nextLogId.current++,
          at: new Date(),
          message: parsed.message,
          level,
          status: parsed.status,
        }),
      }));

      // The contract guarantees nothing follows a terminal status.
      if (isTerminalStatus(parsed.status)) {
        source.close();
        if (!disposed) {
          setState((prev) => ({ ...prev, connection: "closed" }));
        }
      }
    };

    source.onerror = () => {
      if (disposed) return;

      // `CLOSED` means the browser will not retry (bad URL, CORS, 4xx).
      if (source.readyState === EventSource.CLOSED) {
        finish("Lost connection to the generation service.");
        return;
      }

      // Otherwise the browser is already backing off and will retry for us.
      failedAttempts += 1;

      if (failedAttempts >= MAX_RECONNECT_ATTEMPTS) {
        finish(
          `Unable to reach the generation service after ${MAX_RECONNECT_ATTEMPTS} attempts.`,
        );
        return;
      }

      setState((prev) => ({ ...prev, connection: "reconnecting" }));
      pushSystemLog(
        `Connection interrupted — retrying (${failedAttempts}/${MAX_RECONNECT_ATTEMPTS})…`,
      );
    };

    return () => {
      disposed = true;
      source.close();
    };
  }, [jobId, attempt]);

  return { ...state, reconnect };
}
