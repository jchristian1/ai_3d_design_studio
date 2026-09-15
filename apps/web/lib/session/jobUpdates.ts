/**
 * Following a job to completion.
 *
 * The UI needs a stream of job states. Polling is how Spec 001 gets one, but it is
 * deliberately hidden behind `JobUpdateSource`:
 *
 *     useDesignSession
 *         v
 *     JobUpdateSource            <- the seam
 *      ├── createPollingJobUpdates   (Spec 001)
 *      └── (later) SSE / WebSocket
 *
 * Swapping to server-pushed updates means writing one more implementation of this
 * interface. The hook, the reducer, and every component stay untouched, because
 * none of them know how an update arrived.
 *
 * Properties this implementation guarantees, each of which is a bug people
 * actually ship:
 *
 *   - **Stops at a terminal status.** No polling after `succeeded`/`failed`.
 *   - **No overlapping loops.** One request in flight at a time, scheduled only
 *     after the previous settles. A fixed `setInterval` would stack requests when
 *     the API is slow.
 *   - **Cancellable.** `stop()` prevents any further callback, so a component that
 *     unmounts mid-poll cannot dispatch into a dead tree.
 *   - **Bounded.** A job that never terminates times out instead of polling
 *     forever.
 */

import type { ApiClient, JobStatusView } from "../api/index.ts";
import { ApiFailure, isTerminalStatus } from "../api/index.ts";

/** A live subscription to one job's progress. */
export interface JobSubscription {
  /** Stop receiving updates. Safe to call more than once. */
  stop(): void;
}

export interface JobUpdateHandlers {
  /** Every observed state, including the terminal one. */
  onUpdate(status: JobStatusView): void;
  /** The job reached a terminal state. Called once. */
  onSettled(status: JobStatusView): void;
  /** The job could not be followed. Called once. */
  onError(failure: ApiFailure): void;
  /** The job did not finish within the allotted time. Called once. */
  onTimeout(): void;
}

export interface JobUpdateSource {
  subscribe(
    projectId: string,
    jobId: string,
    handlers: JobUpdateHandlers,
  ): JobSubscription;
}

/** Injected so tests drive time instead of waiting for it. */
export interface Scheduler {
  setTimeout(callback: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
  now(): number;
}

export const realScheduler: Scheduler = {
  setTimeout: (callback, ms) => setTimeout(callback, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
  now: () => Date.now(),
};

export interface PollingOptions {
  intervalMs: number;
  timeoutMs: number;
  scheduler?: Scheduler;
}

export function createPollingJobUpdates(
  client: ApiClient,
  options: PollingOptions,
): JobUpdateSource {
  const scheduler = options.scheduler ?? realScheduler;

  return {
    subscribe(projectId, jobId, handlers) {
      let stopped = false;
      let timer: unknown = null;
      const deadline = scheduler.now() + options.timeoutMs;

      const finish = () => {
        stopped = true;
        if (timer !== null) {
          scheduler.clearTimeout(timer);
          timer = null;
        }
      };

      const scheduleNext = () => {
        if (stopped) return;
        timer = scheduler.setTimeout(() => {
          timer = null;
          void tick();
        }, options.intervalMs);
      };

      const tick = async () => {
        if (stopped) return;

        if (scheduler.now() > deadline) {
          finish();
          handlers.onTimeout();
          return;
        }

        let status: JobStatusView;
        try {
          status = await client.getJobStatus(projectId, jobId);
        } catch (cause) {
          // A single transient failure should not abandon a job that is probably
          // still running, so a reachability problem is retried; anything the API
          // answered definitively (a 404, a rejected request) is final.
          const failure =
            cause instanceof ApiFailure
              ? cause
              : new ApiFailure("Something went wrong.", "unknown");
          if (failure.kind === "offline" || failure.kind === "timeout") {
            scheduleNext();
            return;
          }
          finish();
          handlers.onError(failure);
          return;
        }

        // The subscription may have been stopped while awaiting the response.
        if (stopped) return;

        handlers.onUpdate(status);

        if (isTerminalStatus(status.job_status)) {
          finish();
          handlers.onSettled(status);
          return;
        }

        scheduleNext();
      };

      // Ask immediately: a fast job may already be finished, and waiting a full
      // interval before the first check would make the UI feel slower than it is.
      void tick();

      return {
        stop() {
          finish();
        },
      };
    },
  };
}
