"use client";

import { useEffect, useState } from "react";

import type { PreviewView } from "../lib/api/index.ts";
import styles from "./PreviewPanel.module.css";

export interface PreviewPanelProps {
  /** Absolute image URL, already built from the configured API base. */
  src: string | null;
  preview: PreviewView | null;
  /** Showing the previous image while a new change is applied. */
  stale: boolean;
  /** A change succeeded but its preview is unavailable. */
  warning: string | null;
  busy: boolean;
}

/**
 * The design preview.
 *
 * Four states, all of which really happen:
 *
 *   1. **Nothing yet** — no change has been made, so there is no image. An empty
 *      state, not an error.
 *   2. **Loading** — the image is being fetched. If a previous image exists it stays
 *      on screen dimmed, because blanking the panel on every change makes the app
 *      feel like it is losing the user's work.
 *   3. **Showing** — the current preview.
 *   4. **Unavailable** — the change was applied and saved but produced no image. A
 *      warning, never an error: saying the change failed would be false.
 *
 * A plain `<img>` rather than `next/image`: the source is a runtime API URL on
 * another origin, so none of the optimisation `next/image` provides applies, and it
 * would need remote-pattern configuration to permit what is already a simple fetch.
 */
export function PreviewPanel({
  src,
  preview,
  stale,
  warning,
  busy,
}: PreviewPanelProps) {
  // `loaded` is keyed on the src so a new image shows its own loading state
  // instead of inheriting the previous one's.
  const [loadedSrc, setLoadedSrc] = useState<string | null>(null);
  const [failedSrc, setFailedSrc] = useState<string | null>(null);

  useEffect(() => {
    if (src === null) {
      setLoadedSrc(null);
      setFailedSrc(null);
    }
  }, [src]);

  const isLoading = src !== null && loadedSrc !== src && failedSrc !== src;
  const isBroken = src !== null && failedSrc === src;

  return (
    <section className={styles.panel} aria-labelledby="preview-heading">
      <header className={styles.header}>
        <h2 id="preview-heading" className={styles.heading}>
          Preview
        </h2>
        {preview ? (
          <p className={styles.meta}>
            {preview.width} &times; {preview.height}
          </p>
        ) : null}
      </header>

      <div className={styles.stage}>
        {src === null ? (
          <EmptyState busy={busy} />
        ) : isBroken ? (
          <p className={styles.notice} role="status">
            The preview image could not be loaded. The change itself was saved.
          </p>
        ) : (
          <figure className={styles.figure}>
            <img
              className={`${styles.image} ${stale || isLoading ? styles.dimmed : ""}`}
              src={src}
              // Describes what the image SHOWS, not that it is an image. A change
              // in position is the meaningful content here.
              alt={
                preview
                  ? `Current design preview, ${preview.width} by ${preview.height} pixels`
                  : "Current design preview"
              }
              width={preview?.width}
              height={preview?.height}
              onLoad={() => setLoadedSrc(src)}
              onError={() => setFailedSrc(src)}
            />
            {isLoading || stale ? (
              <figcaption className={styles.overlay} role="status">
                {stale ? "Updating preview\u2026" : "Loading preview\u2026"}
              </figcaption>
            ) : null}
          </figure>
        )}
      </div>

      {warning ? (
        <p className={styles.warning} role="status">
          {warning}
        </p>
      ) : null}
    </section>
  );
}

function EmptyState({ busy }: { busy: boolean }) {
  return (
    <div className={styles.empty}>
      <p className={styles.emptyTitle}>
        {busy ? "Preparing your first preview\u2026" : "No preview yet"}
      </p>
      {busy ? null : (
        <p className={styles.emptyBody}>
          Send a design instruction to begin.
        </p>
      )}
    </div>
  );
}
