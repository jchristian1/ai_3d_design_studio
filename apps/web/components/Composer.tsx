"use client";

/**
 * The composer, anchored at the bottom of the screen.
 *
 * Enter sends, Shift+Enter makes a new line, and an IME composition never submits
 * mid-word. The button is disabled while busy but the textarea is not, so you can keep
 * typing your next thought while a reconstruction runs.
 *
 * Attachment chips and the selected-object chip sit inside the composer because they
 * change what the next message means, and that has to be visible at the moment of
 * sending rather than somewhere off to the side.
 */

import { useCallback, useRef, useState } from "react";

import type { ReferenceView } from "../lib/api/workspace.ts";
import styles from "./workspace.module.css";

export interface ComposerProps {
  canSubmit(draft: string): boolean;
  onSubmit(message: string): void;
  onAttachClick(): void;
  attached: ReferenceView[];
  onDetach(referenceId: string): void;
  selectedLabel: string | null;
  onClearSelection(): void;
  busy: boolean;
  placeholder?: string;
  hint?: string;
}

export function Composer({
  canSubmit,
  onSubmit,
  onAttachClick,
  attached,
  onDetach,
  selectedLabel,
  onClearSelection,
  busy,
  placeholder = "Ask Astra about your design…",
  hint,
}: ComposerProps) {
  const [draft, setDraft] = useState("");
  const composingRef = useRef(false);

  const submit = useCallback(() => {
    if (!canSubmit(draft)) return;
    onSubmit(draft);
    setDraft("");
  }, [canSubmit, draft, onSubmit]);

  return (
    <form
      className={styles.composer}
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      {attached.length > 0 || selectedLabel ? (
        <div className={styles.chips}>
          {selectedLabel ? (
            <span className={`${styles.chip} ${styles.chipText}`}>
              Editing: {selectedLabel}
              <button
                type="button"
                className={styles.chipButton}
                onClick={onClearSelection}
                aria-label="Clear the selected object"
              >
                ×
              </button>
            </span>
          ) : null}
          {attached.map((reference) => (
            <span key={reference.reference_id} className={styles.chip}>
              {reference.display_name}
              <button
                type="button"
                className={styles.chipButton}
                onClick={() => onDetach(reference.reference_id)}
                aria-label={`Do not send ${reference.display_name}`}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      ) : null}

      <div className={styles.composerRow}>
        <button
          type="button"
          className={styles.attachButton}
          onClick={onAttachClick}
          aria-label="Attach references"
          title="Attach references"
        >
          +
        </button>

        <label className="visuallyHidden" htmlFor="composer-input">
          Message Astra
        </label>
        <textarea
          id="composer-input"
          className={styles.input}
          value={draft}
          rows={1}
          placeholder={placeholder}
          aria-describedby={hint ? "composer-hint" : undefined}
          onChange={(event) => setDraft(event.target.value)}
          onCompositionStart={() => {
            composingRef.current = true;
          }}
          onCompositionEnd={() => {
            composingRef.current = false;
          }}
          onKeyDown={(event) => {
            if (event.key !== "Enter" || event.shiftKey || composingRef.current) return;
            event.preventDefault();
            submit();
          }}
        />

        <button type="submit" className={styles.sendButton} disabled={!canSubmit(draft)}>
          {busy ? "Working…" : "Send"}
        </button>
      </div>

      {hint ? (
        <p id="composer-hint" className={styles.hint}>
          {hint}
        </p>
      ) : null}
    </form>
  );
}
