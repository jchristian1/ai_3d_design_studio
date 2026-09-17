"use client";

/**
 * Confirming the deletion of a project.
 *
 * Deletion is irreversible and there is no bin to recover from, so the gate is typing the
 * project's name. That is deliberately more work than a "yes" button:
 *
 * - it cannot happen by a stray click or a repeated key;
 * - it forces you to read WHICH project you are about to lose;
 * - and it is the only control here, so there is nothing to click through by habit.
 *
 * The dialog also says exactly what will go, counted from the project itself, because
 * "delete this project?" is not enough information to decide with.
 *
 * The server checks the typed name as well. This dialog is the courtesy; the check is the
 * guarantee.
 */

import { useEffect, useRef, useState } from "react";

import type { ProjectSummaryView } from "../lib/api/workspace.ts";
import styles from "./workspace.module.css";

export interface DeleteProjectDialogProps {
  project: ProjectSummaryView;
  busy?: boolean;
  error?: string | null;
  onCancel(): void;
  onConfirm(typedName: string): void;
}

export function DeleteProjectDialog({
  project,
  busy = false,
  error = null,
  onCancel,
  onConfirm,
}: DeleteProjectDialogProps) {
  const [typed, setTyped] = useState("");
  const field = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    field.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);

  // Whitespace is forgiven — a copy-paste often brings a trailing space — and nothing else
  // is. The server applies the same rule.
  const matches = collapse(typed) === collapse(project.display_name);

  return (
    <div className={styles.scrim} role="presentation" onMouseDown={onCancel}>
      <div
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby="delete-project-heading"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <h2 id="delete-project-heading" className={styles.dialogTitle}>
          Delete “{project.display_name}”?
        </h2>

        <p className={styles.dialogBody}>
          This cannot be undone. {describeLosses(project)} will be deleted.
        </p>

        <form
          onSubmit={(event) => {
            event.preventDefault();
            if (matches && !busy) onConfirm(typed);
          }}
        >
          <label className={styles.dialogLabel} htmlFor="delete-confirm">
            Type the project name to confirm
          </label>
          <input
            id="delete-confirm"
            ref={field}
            className={styles.dialogInput}
            value={typed}
            onChange={(event) => setTyped(event.target.value)}
            placeholder={project.display_name}
            autoComplete="off"
            spellCheck={false}
            aria-describedby="delete-confirm-hint"
          />
          <p id="delete-confirm-hint" className={styles.dialogHint}>
            {matches
              ? "That matches. Deleting is permanent."
              : `Type “${project.display_name}” exactly.`}
          </p>

          {error ? (
            <p className={styles.connectError} role="alert">
              {error}
            </p>
          ) : null}

          <div className={styles.dialogActions}>
            <button type="button" className={styles.ghostButton} onClick={onCancel}>
              Cancel
            </button>
            <button type="submit" className={styles.dangerButton} disabled={!matches || busy}>
              {busy ? "Deleting…" : "Delete project"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function collapse(value: string): string {
  return value.split(/\s+/).filter(Boolean).join(" ");
}

/** What the user is about to lose, in their terms. */
export function describeLosses(project: ProjectSummaryView): string {
  const parts: string[] = [];
  if (project.reference_count === 1) parts.push("1 uploaded file");
  else if (project.reference_count > 1) parts.push(`${project.reference_count} uploaded files`);
  if (project.message_count === 1) parts.push("1 message");
  else if (project.message_count > 1) parts.push(`${project.message_count} messages`);
  if (project.has_model) parts.push("the 3D model");
  if (parts.length === 0) return "The project";
  if (parts.length === 1) return parts[0]!;
  return `${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}`;
}
