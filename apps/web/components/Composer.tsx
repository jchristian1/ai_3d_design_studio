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

import { useCallback, useEffect, useRef, useState } from "react";

import type { ReferenceView } from "../lib/api/workspace.ts";
import styles from "./workspace.module.css";

/** How tall the textarea may grow before it starts scrolling internally (px). */
const MAX_TEXTAREA_HEIGHT = 200;

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
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Grow the textarea to fit its content, the way ChatGPT does: measure the natural
  // scroll height and adopt it, capped so a very long paste scrolls internally rather
  // than swallowing the transcript. Runs on every draft change, including the reset to
  // "" after a send, so the box snaps back to one line.
  const resize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT)}px`;
  }, []);

  useEffect(() => {
    resize();
  }, [draft, resize]);

  const submit = useCallback(() => {
    if (!canSubmit(draft)) return;
    onSubmit(draft);
    setDraft("");
  }, [canSubmit, draft, onSubmit]);

  const sendable = canSubmit(draft);

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

      <div className={styles.inputSurface}>
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
          ref={textareaRef}
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

        <button
          type="submit"
          className={styles.sendButton}
          disabled={!sendable}
          aria-label={busy ? "Working" : "Send"}
          title={busy ? "Working…" : "Send"}
        >
          {busy ? (
            <span className={styles.sendSpinner} aria-hidden="true" />
          ) : (
            <svg
              className={styles.sendIcon}
              viewBox="0 0 24 24"
              aria-hidden="true"
              focusable="false"
            >
              <path
                d="M12 20V5M12 5l-6 6M12 5l6 6"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.2"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          )}
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
