"use client";

import type { SessionState } from "../lib/session/types.ts";
import { STATUS_LABEL } from "../lib/session/types.ts";
import styles from "./StatusBar.module.css";

/**
 * The bottom status strip.
 *
 * Summarises the three things a user wants at a glance: which project is open,
 * what the last change is doing, and whether a preview exists.
 *
 * Job identifiers, execution phases, worker ids, and idempotency keys are
 * deliberately absent — they are implementation detail, and there is no debug panel
 * in Spec 001 to justify surfacing them.
 */
export function StatusBar({
  projectLabel,
  state,
}: {
  projectLabel: string;
  state: SessionState;
}) {
  const jobStatus = state.submission?.jobStatus ?? null;

  const jobText =
    state.phase === "submitting"
      ? "Sending\u2026"
      : jobStatus
        ? STATUS_LABEL[jobStatus]
        : "No change yet";

  const jobTone =
    jobStatus === "succeeded"
      ? styles.ok
      : jobStatus === "failed" || state.phase === "failed"
        ? styles.error
        : state.phase === "submitting" || state.phase === "tracking"
          ? styles.busy
          : styles.idle;

  return (
    <footer className={styles.bar}>
      <span className={styles.field}>
        <span className={styles.key}>Project</span>
        <span className={styles.value}>{projectLabel}</span>
      </span>

      <span className={styles.field}>
        <span className={styles.key}>Change</span>
        <span className={`${styles.value} ${jobTone}`}>{jobText}</span>
      </span>

      <span className={styles.field}>
        <span className={styles.key}>Preview</span>
        <span className={styles.value}>
          {state.preview
            ? state.previewWarning
              ? "Outdated"
              : "Available"
            : "None"}
        </span>
      </span>
    </footer>
  );
}
