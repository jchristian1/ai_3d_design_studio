"use client";

/**
 * The conversation column, on the left.
 *
 *   ┌ files you have attached (collapsible) ─┐
 *   │ transcript, scrolling, newest at the   │
 *   │ bottom                                 │
 *   ├────────────────────────────────────────┤
 *   └ composer, pinned to the bottom ────────┘
 *
 * The WHOLE column is a drop target: dragging a floor plan anywhere over the
 * conversation uploads it and attaches it to the next message, which is what people
 * expect from a chat. The file picker in the composer does the same thing for anyone who
 * would rather click.
 *
 * Drag tracking uses a counter rather than a boolean because dragenter/dragleave fire for
 * every child element; a boolean flickers the whole time you move the pointer.
 */

import { useCallback, useRef, useState } from "react";

import type { ReferenceView } from "../lib/api/workspace.ts";
import type { TranscriptEntry, UploadState } from "../lib/workspace/types.ts";
import { Composer } from "./Composer.tsx";
import { ConversationPanel } from "./ConversationPanel.tsx";
import { ReferenceStrip } from "./ReferenceStrip.tsx";
import styles from "./workspace.module.css";

export interface ChatColumnProps {
  entries: TranscriptEntry[];
  references: ReferenceView[];
  attachedIds: string[];
  attached: ReferenceView[];
  uploads: UploadState[];
  accept: string;
  busy: boolean;
  selectedLabel: string | null;
  hint?: string;
  canSubmit(draft: string): boolean;
  onSubmit(message: string): void;
  onUpload(files: FileList | File[]): void;
  onToggleAttachment(referenceId: string): void;
  onRemoveReference(referenceId: string): void;
  referenceUrl(referenceId: string): string;
  onDecide(approvalId: string, approved: boolean): void;
  onClearSelection(): void;
}

export function ChatColumn({
  entries,
  references,
  attachedIds,
  attached,
  uploads,
  accept,
  busy,
  selectedLabel,
  hint,
  canSubmit,
  onSubmit,
  onUpload,
  onToggleAttachment,
  onRemoveReference,
  referenceUrl,
  onDecide,
  onClearSelection,
}: ChatColumnProps) {
  const [dragDepth, setDragDepth] = useState(0);
  const fileInput = useRef<HTMLInputElement | null>(null);

  const carriesFiles = (event: React.DragEvent) =>
    Array.from(event.dataTransfer?.types ?? []).includes("Files");

  const onDragEnter = useCallback((event: React.DragEvent) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    setDragDepth((depth) => depth + 1);
  }, []);

  const onDragOver = useCallback((event: React.DragEvent) => {
    if (!carriesFiles(event)) return;
    // Without this the browser navigates to the dropped file instead of telling us.
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
  }, []);

  const onDragLeave = useCallback(() => {
    setDragDepth((depth) => Math.max(0, depth - 1));
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      if (!carriesFiles(event)) return;
      event.preventDefault();
      setDragDepth(0);
      const files = event.dataTransfer?.files;
      if (files?.length) onUpload(files);
    },
    [onUpload],
  );

  const pickFiles = useCallback(() => fileInput.current?.click(), []);

  return (
    <section
      className={`${styles.chatColumn} ${dragDepth > 0 ? styles.dropping : ""}`}
      aria-label="Conversation"
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      <ReferenceStrip
        references={references}
        attachedIds={attachedIds}
        uploads={uploads}
        onToggleAttachment={onToggleAttachment}
        onRemove={onRemoveReference}
        referenceUrl={referenceUrl}
        onAddClick={pickFiles}
      />

      <ConversationPanel entries={entries} onDecide={onDecide} deciding={busy} />

      <Composer
        canSubmit={canSubmit}
        onSubmit={onSubmit}
        onAttachClick={pickFiles}
        attached={attached}
        onDetach={onToggleAttachment}
        selectedLabel={selectedLabel}
        onClearSelection={onClearSelection}
        busy={busy}
        hint={hint}
      />

      {/* One hidden input for the whole column: the strip's "Add" and the composer's
          paperclip both open it, so there is a single upload path. */}
      <input
        ref={fileInput}
        id="reference-upload"
        className="visuallyHidden"
        type="file"
        multiple
        accept={accept}
        aria-label="Add floor plans, photos or notes"
        onChange={(event) => {
          if (event.target.files?.length) onUpload(event.target.files);
          event.target.value = "";
        }}
      />

      {dragDepth > 0 ? (
        <div className={styles.dropOverlay} aria-hidden="true">
          <span>Drop to add to this project</span>
        </div>
      ) : null}
    </section>
  );
}
