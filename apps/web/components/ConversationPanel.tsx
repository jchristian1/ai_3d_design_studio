"use client";

/**
 * The conversation, and the approval card.
 *
 * One user message owns exactly one Studio reply, which updates in place as work
 * progresses. The transcript expands upward from the composer and is independently
 * scrollable, so the model stays visible while you talk.
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
  collapsed: boolean;
  onToggleCollapsed(): void;
  onDecide(approvalId: string, approved: boolean): void;
  deciding: boolean;
}

export function ConversationPanel({
  entries,
  collapsed,
  onToggleCollapsed,
  onDecide,
  deciding,
}: ConversationPanelProps) {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: "nearest" });
  }, [entries.length]);

  const latestStudio = [...entries].reverse().find((entry) => entry.author === "studio");

  return (
    <section className={styles.conversation} aria-labelledby="conversation-heading">
      <header className={styles.conversationHeader}>
        <h2 id="conversation-heading" className={styles.sideTitle}>
          Conversation
        </h2>
        <button type="button" className={styles.smallButton} onClick={onToggleCollapsed}>
          {collapsed ? "Show history" : "Hide history"}
        </button>
      </header>

      {/* Screen readers hear only the latest Studio line, not the whole history. */}
      <div className="visuallyHidden" aria-live="polite" aria-atomic="true">
        {latestStudio ? progressText(latestStudio) : ""}
      </div>

      {collapsed ? null : (
        <ol className={styles.entryList}>
          {entries.length === 0 ? (
            <li className={styles.emptyNote}>
              Ask Astra what it sees in your plans, or describe what you want built.
            </li>
          ) : null}

          {entries.map((entry) => (
            <li key={entry.id} className={`${styles.entry} ${styles[entry.author]}`}>
              <span className={styles.entryAuthor}>
                {entry.author === "user" ? "You" : "Astra"}
              </span>
              <div className={`${styles.entryBody} ${styles[entry.tone]}`}>
                <p className={styles.entryText}>{progressText(entry)}</p>

                {entry.progress && entry.progress.stepCount > 1 ? (
                  <progress
                    className={styles.progressBar}
                    value={entry.progress.stepIndex + 1}
                    max={entry.progress.stepCount}
                  />
                ) : null}

                {entry.assumptions?.length ? (
                  <div className={styles.assumptions}>
                    <p className={styles.assumptionsTitle}>
                      I assumed the following — tell me if any of it is wrong:
                    </p>
                    <ul>
                      {entry.assumptions.map((assumption) => (
                        <li key={assumption}>{assumption}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}

                {entry.approval ? (
                  <ApprovalCard
                    approval={entry.approval}
                    busy={deciding}
                    onDecide={onDecide}
                  />
                ) : null}
              </div>
            </li>
          ))}
          <div ref={endRef} />
        </ol>
      )}
    </section>
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
    <div className={styles.approvalCard} role="group" aria-labelledby={headingId}>
      <p id={headingId} className={styles.approvalTitle}>
        This step needs your approval
      </p>
      <ul className={styles.approvalReasons}>
        {approval.reasons.map((reason) => (
          <li key={reason}>{reason}</li>
        ))}
      </ul>
      <pre className={styles.approvalCode}>
        <code>{approval.code}</code>
      </pre>
      <p className={styles.approvalNote}>
        Nothing runs until you decide. Approving allows this exact code, once.
      </p>
      <div className={styles.approvalActions}>
        <button
          type="button"
          className={styles.approveButton}
          disabled={busy}
          onClick={() => onDecide(approval.approval_id, true)}
        >
          Approve and run
        </button>
        <button
          type="button"
          className={styles.rejectButton}
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
