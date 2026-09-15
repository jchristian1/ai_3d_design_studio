"use client";

import { useState } from "react";

import styles from "./MessageInput.module.css";

export interface MessageInputProps {
  /** Whether the current draft may be sent. */
  canSubmit(draft: string): boolean;
  onSubmit(message: string): void;
  /** A mutation is in flight. */
  busy: boolean;
  /** Offered when a submission's outcome is genuinely uncertain. */
  onRetry?: (() => void) | undefined;
  retryAvailable?: boolean;
}

/**
 * The command input.
 *
 * A real `<form>` with a labelled `<textarea>`, so Enter submits and screen readers
 * announce the field — both for free, and both easy to lose by building this out of
 * divs.
 *
 * Enter sends; Shift+Enter inserts a newline. A textarea rather than an input
 * because design instructions can reasonably run long, and silently truncating the
 * visible text would be worse than a slightly taller control.
 *
 * Sending is blocked while a change is in flight. For Spec 001 that is deliberate:
 * two concurrent mutations on one project would race, and the second would be
 * planned against a scene the first is about to change.
 */
export function MessageInput({
  canSubmit,
  onSubmit,
  busy,
  onRetry,
  retryAvailable = false,
}: MessageInputProps) {
  const [draft, setDraft] = useState("");
  const sendable = canSubmit(draft);

  const submit = () => {
    if (!sendable) return;
    onSubmit(draft);
    setDraft("");
  };

  return (
    <form
      className={styles.form}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <label className={styles.label} htmlFor="design-instruction">
        Design instruction
      </label>

      <textarea
        id="design-instruction"
        name="instruction"
        className={styles.textarea}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          // Enter sends, Shift+Enter is a newline. `isComposing` guards IME input:
          // committing a candidate with Enter must not send the message.
          if (
            event.key === "Enter" &&
            !event.shiftKey &&
            !event.nativeEvent.isComposing
          ) {
            event.preventDefault();
            submit();
          }
        }}
        placeholder="Move Cube 50 cm to the right."
        rows={3}
        // Sending stays disabled while busy, but the field does not: the user can
        // compose their next instruction while the current one finishes.
        aria-describedby="instruction-hint"
      />

      <div className={styles.row}>
        <p id="instruction-hint" className={styles.hint}>
          {busy
            ? "Applying your change\u2026 you can type the next one meanwhile."
            : "Press Enter to send, Shift+Enter for a new line."}
        </p>

        <div className={styles.actions}>
          {retryAvailable && onRetry ? (
            <button
              type="button"
              className={styles.retry}
              onClick={onRetry}
              disabled={busy}
            >
              Retry
            </button>
          ) : null}

          <button type="submit" className={styles.send} disabled={!sendable}>
            {busy ? "Working\u2026" : "Send"}
          </button>
        </div>
      </div>
    </form>
  );
}
