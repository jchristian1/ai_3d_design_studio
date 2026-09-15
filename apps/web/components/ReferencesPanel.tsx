"use client";

/**
 * Project references: drag-and-drop or pick files, see thumbnails, attach them to the
 * next message, remove them.
 *
 * "Attached" is explicit and visible because it costs the user money: an attached image
 * is sent to Astra, and Astra runs on a finite ChatGPT allowance. A freshly uploaded
 * file is attached automatically (you uploaded it because you want it looked at), and
 * sending clears the attachments so the same images are never silently re-sent.
 */

import { useCallback, useRef, useState } from "react";

import type { ReferenceView } from "../lib/api/workspace.ts";
import type { UploadState } from "../lib/workspace/types.ts";
import styles from "./workspace.module.css";

export interface ReferencesPanelProps {
  references: ReferenceView[];
  attachedIds: string[];
  uploads: UploadState[];
  onUpload(files: FileList | File[]): void;
  onToggleAttachment(referenceId: string): void;
  onRemove(referenceId: string): void;
  referenceUrl(referenceId: string): string;
  accept: string;
}

export function ReferencesPanel({
  references,
  attachedIds,
  uploads,
  onUpload,
  onToggleAttachment,
  onRemove,
  referenceUrl,
  accept,
}: ReferencesPanelProps) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [dragging, setDragging] = useState(false);

  const handleDrop = useCallback(
    (event: React.DragEvent<HTMLDivElement>) => {
      event.preventDefault();
      setDragging(false);
      if (event.dataTransfer?.files?.length) onUpload(event.dataTransfer.files);
    },
    [onUpload],
  );

  return (
    <section className={styles.sidePanel} aria-labelledby="references-heading">
      <header className={styles.sideHeader}>
        <h2 id="references-heading" className={styles.sideTitle}>
          References
        </h2>
        <button
          type="button"
          className={styles.smallButton}
          onClick={() => inputRef.current?.click()}
        >
          Add files
        </button>
      </header>

      <div
        className={`${styles.dropZone} ${dragging ? styles.dropZoneActive : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
      >
        <p className={styles.dropHint}>
          Drop floor plans, photos or notes here
        </p>
        <label className="visuallyHidden" htmlFor="reference-upload">
          Upload project references
        </label>
        <input
          ref={inputRef}
          id="reference-upload"
          className={styles.fileInput}
          type="file"
          multiple
          accept={accept}
          onChange={(event) => {
            if (event.target.files?.length) onUpload(event.target.files);
            event.target.value = "";
          }}
        />
      </div>

      {uploads.length > 0 ? (
        <ul className={styles.uploadList} aria-live="polite">
          {uploads.map((upload) => (
            <li key={upload.id} className={styles.uploadItem}>
              <span className={styles.uploadName}>{upload.name}</span>
              <span
                className={
                  upload.status === "failed" ? styles.uploadFailed : styles.uploadProgress
                }
              >
                {upload.status === "failed" ? upload.message ?? "Failed" : "Uploading…"}
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      {references.length === 0 ? (
        <p className={styles.emptyNote}>
          Nothing yet. Astra can work from a floor plan, a photo of a room, or a short
          written brief.
        </p>
      ) : (
        <ul className={styles.referenceList}>
          {references.map((reference) => {
            const attached = attachedIds.includes(reference.reference_id);
            const thumbnailId = reference.is_image
              ? reference.reference_id
              : reference.pages?.[0]?.reference_id;
            return (
              <li key={reference.reference_id} className={styles.referenceItem}>
                <label className={styles.referenceLabel}>
                  <input
                    type="checkbox"
                    checked={attached}
                    onChange={() => onToggleAttachment(reference.reference_id)}
                  />
                  <span className="visuallyHidden">
                    Use {reference.display_name} in the next message
                  </span>
                  {thumbnailId ? (
                    <img
                      className={styles.thumbnail}
                      src={referenceUrl(thumbnailId)}
                      alt={`Thumbnail of ${reference.display_name}`}
                      loading="lazy"
                    />
                  ) : (
                    <span className={styles.documentIcon} aria-hidden="true">
                      TXT
                    </span>
                  )}
                  <span className={styles.referenceMeta}>
                    <span className={styles.referenceName}>{reference.display_name}</span>
                    <span className={styles.referenceDetail}>
                      {describe(reference)}
                    </span>
                  </span>
                </label>
                <button
                  type="button"
                  className={styles.removeButton}
                  onClick={() => onRemove(reference.reference_id)}
                  aria-label={`Remove ${reference.display_name}`}
                >
                  ×
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

function describe(reference: ReferenceView): string {
  if (reference.kind === "pdf") {
    const pages = reference.page_count ?? reference.pages?.length ?? 0;
    return pages === 1 ? "PDF · 1 page" : `PDF · ${pages} pages`;
  }
  if (reference.kind === "document") return "Text";
  if (reference.width && reference.height) {
    return `Image · ${reference.width}×${reference.height}`;
  }
  return "Image";
}
