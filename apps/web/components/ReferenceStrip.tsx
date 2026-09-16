"use client";

/**
 * The files in this project, above the conversation.
 *
 * Compact on purpose: a thumbnail, a name, and whether it is attached to the next
 * message. Attachment is explicit because it costs the user their ChatGPT allowance — an
 * attached image is actually sent to Astra — and a freshly uploaded file starts attached
 * because you uploaded it in order to have it looked at.
 *
 * Collapses to a single line when you have a lot of files, so the transcript keeps the
 * height.
 */

import { useState } from "react";

import type { ReferenceView } from "../lib/api/workspace.ts";
import type { UploadState } from "../lib/workspace/types.ts";
import styles from "./workspace.module.css";

export interface ReferenceStripProps {
  references: ReferenceView[];
  attachedIds: string[];
  uploads: UploadState[];
  onToggleAttachment(referenceId: string): void;
  onRemove(referenceId: string): void;
  referenceUrl(referenceId: string): string;
  onAddClick(): void;
}

export function ReferenceStrip({
  references,
  attachedIds,
  uploads,
  onToggleAttachment,
  onRemove,
  referenceUrl,
  onAddClick,
}: ReferenceStripProps) {
  const [open, setOpen] = useState(true);
  // A finished upload becomes a reference, so the only uploads worth showing are the ones
  // still in flight or the ones that failed.
  const active = uploads;
  const empty = references.length === 0 && active.length === 0;

  return (
    <section className={styles.strip} aria-labelledby="files-heading">
      <header className={styles.stripHeader}>
        <h2 id="files-heading" className={styles.stripTitle}>
          Files
          {references.length ? (
            <span className={styles.count}>{references.length}</span>
          ) : null}
        </h2>
        <div className={styles.stripActions}>
          <button type="button" className={styles.ghostButton} onClick={onAddClick}>
            Add files
          </button>
          {references.length ? (
            <button
              type="button"
              className={styles.ghostButton}
              aria-expanded={open}
              onClick={() => setOpen((value) => !value)}
            >
              {open ? "Hide" : "Show"}
            </button>
          ) : null}
        </div>
      </header>

      {empty ? (
        <p className={styles.stripHint}>
          Drag a floor plan or a photo anywhere onto this conversation.
        </p>
      ) : null}

      {open && !empty ? (
        <ul className={styles.stripList} aria-label="Files in this project">
          {active.map((upload) => (
            <li key={upload.id} className={styles.stripItem}>
              <span className={styles.thumbPlaceholder} aria-hidden="true" />
              <span className={styles.stripName}>{upload.name}</span>
              <span className={styles.stripState}>
                {upload.status === "failed" ? upload.message ?? "Failed" : "Uploading…"}
              </span>
            </li>
          ))}

          {references.map((reference) => {
            const attached = attachedIds.includes(reference.reference_id);
            return (
              <li key={reference.reference_id} className={styles.stripItem}>
                {reference.is_image ? (
                  <img
                    className={styles.thumb}
                    src={referenceUrl(reference.reference_id)}
                    alt=""
                  />
                ) : (
                  <span className={styles.thumbPlaceholder} aria-hidden="true">
                    {reference.kind === "pdf" ? "PDF" : "TXT"}
                  </span>
                )}

                <span className={styles.stripName} title={reference.display_name}>
                  {reference.display_name}
                  {reference.page_count ? (
                    <span className={styles.stripMeta}>{reference.page_count} pages</span>
                  ) : null}
                </span>

                <label className={styles.attachToggle}>
                  <input
                    type="checkbox"
                    checked={attached}
                    onChange={() => onToggleAttachment(reference.reference_id)}
                  />
                  <span>Attach</span>
                </label>

                <button
                  type="button"
                  className={styles.iconButton}
                  onClick={() => onRemove(reference.reference_id)}
                  aria-label={`Remove ${reference.display_name}`}
                  title="Remove"
                >
                  ×
                </button>
              </li>
            );
          })}
        </ul>
      ) : null}
    </section>
  );
}
