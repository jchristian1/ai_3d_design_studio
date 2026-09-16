"use client";

/**
 * The transcript, and the approval card.
 *
 * One user message owns exactly one Studio reply, which updates in place as work
 * progresses — a plan of nine walls is one reply that counts up, not nine bubbles. The
 * list scrolls inside the chat column and sticks to the newest message.
 *
 * The approval card is the one place model-authored code is shown, deliberately and in
 * full: seeing exactly what would run is the entire point of asking.
 */

import { useEffect, useRef } from "react";

import type { ApprovalView } from "../lib/api/workspace.ts";
import type { TranscriptEntry } from "../lib/workspace/types.ts";
import styles from "./workspace.module.css";

export interface ConversationPanelProps {
  entries: TranscriptEntry[];
  onDecide(approvalId: string, approved: boolean): void;
  deciding: boolean;
}

export function ConversationPanel({ entries, onDecide, deciding }: ConversationPanelProps) {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [entries.length]);

  const latestStudio = [...entries].reverse().find((entry) => entry.author === "studio");

  return (
    <div className={styles.conversation} aria-label="Transcript">
      {/* Screen readers hear only the latest Studio line, not the whole history. */}
      <div className="visuallyHidden" aria-live="polite" aria-atomic="true">
        {latestStudio ? progressText(latestStudio) : ""}
      </div>

      <ol className={styles.entryList} aria-label="Messages">
        {entries.length === 0 ? (
          <li className={styles.cardHint}>
            Describe what you want built, or drag a floor plan in here and ask about it.
          </li>
        ) : null}

        {entries.map((entry) => (
          <li
            key={entry.id}
            className={`${styles.entry} ${entry.author === "user" ? styles.entryUser : ""}`}
          >
            <span className={styles.entryMeta}>{entry.author === "user" ? "You" : "Astra"}</span>

            <div
              className={`${styles.bubble} ${
                entry.author === "user"
                  ? styles.bubbleUser
                  : entry.tone === "error"
                    ? styles.bubbleError
                    : ""
              }`}
            >
              {entry.progress ? (
                <p className={styles.progress}>
                  <span className={styles.spinner} aria-hidden="true" />
                  {progressText(entry)}
                </p>
              ) : (
                <p style={{ margin: 0 }}>{entry.text}</p>
              )}

              {entry.assumptions?.length ? (
                <>
                  <p className={styles.entryMeta} style={{ marginBottom: 0 }}>
                    I assumed the following — tell me if any of it is wrong:
                  </p>
                  <ul className={styles.assumptions}>
                    {entry.assumptions.map((assumption) => (
                      <li key={assumption}>{assumption}</li>
                    ))}
                  </ul>
                </>
              ) : null}

              {entry.approval ? (
                <ApprovalCard approval={entry.approval} busy={deciding} onDecide={onDecide} />
              ) : null}
            </div>
          </li>
        ))}
        <div ref={endRef} />
      </ol>
    </div>
  );
}

export function ApprovalCard({
  approval,
  busy,
  onDecide,
}: {
  approval: ApprovalView;
  busy: boolean;
  onDecide(approvalId: string, approved: boolean): void;
}) {
  const headingId = `approval-${approval.approval_id}`;
  return (
    <div className={styles.approval} role="group" aria-labelledby={headingId}>
      <p id={headingId} className={styles.approvalTitle}>
        This step needs your approval
      </p>
      <ul className={styles.approvalReasons}>
        {approval.reasons.map((reason) => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>
      <pre className={styles.code}>
        <code>{approval.code}</code>
      </pre>
      <p className={styles.entryMeta}>
        Nothing runs until you decide. Approving allows this exact code, once.
      </p>
      <div className={styles.approvalActions}>
        <button
          type="button"
          className={styles.primaryButton}
          disabled={busy}
          onClick={() => onDecide(approval.approval_id, true)}
        >
          Approve and run
        </button>
        <button
          type="button"
          className={styles.ghostButton}
          disabled={busy}
          onClick={() => onDecide(approval.approval_id, false)}
        >
          Reject
        </button>
      </div>
    </div>
  );
}

/** A running step's label replaces the reply text, so one line tells the whole story. */
function progressText(entry: TranscriptEntry): string {
  if (!entry.progress) return entry.text;
  const { stepIndex, stepCount, label } = entry.progress;
  if (stepCount > 1) return `${label} (${stepIndex + 1} of ${stepCount})`;
  return label;
}
