"use client";

/**
 * The design workspace.
 *
 *   ┌ toolbar: project · Astra · Blender · model status ─────────────────┐
 *   ├ references │            3D model            │ inspector ──────────┤
 *   ├ conversation, expanding upward, independently scrollable ──────────┤
 *   └ composer, anchored to the bottom ──────────────────────────────────┘
 *
 * The model keeps the largest share of the screen and stays visible while you talk,
 * because looking at the design is the point of the tool. Both sidebars collapse, and
 * the centre takes the space they give up.
 */

import { useCallback, useRef, useState } from "react";

import { SUPPORTED_UPLOAD_ACCEPT } from "../lib/config.ts";
import { useWorkspace, type UseWorkspaceOptions } from "../hooks/useWorkspace.ts";
import { pendingApproval, selectedObject, attachedReferences } from "../lib/workspace/types.ts";
import { AstraConnect } from "./AstraConnect.tsx";
import { Composer } from "./Composer.tsx";
import { ConversationPanel } from "./ConversationPanel.tsx";
import { InspectorPanel, friendlyName } from "./InspectorPanel.tsx";
import { ModelViewer } from "./ModelViewer.tsx";
import { ReferencesPanel } from "./ReferencesPanel.tsx";
import styles from "./workspace.module.css";

export interface WorkspaceShellProps {
  /** Injected by tests to supply stub clients; unused in the app. */
  sessionOptions?: UseWorkspaceOptions;
}

export function WorkspaceShell({ sessionOptions }: WorkspaceShellProps) {
  const session = useWorkspace(sessionOptions ?? {});
  const { state } = session;

  const [referencesOpen, setReferencesOpen] = useState(true);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [historyCollapsed, setHistoryCollapsed] = useState(false);
  const uploadTrigger = useRef<HTMLDivElement | null>(null);

  const selected = selectedObject(state);
  const approval = pendingApproval(state);

  const requestAttach = useCallback(() => {
    // The file input lives in the references panel, which owns upload UI. Opening the
    // panel and clicking through keeps one implementation rather than two.
    setReferencesOpen(true);
    const input = uploadTrigger.current?.querySelector<HTMLInputElement>(
      "#reference-upload",
    );
    input?.click();
  }, []);

  const modelStatus = describeModelStatus(state.phase, Boolean(state.model));

  return (
    <div
      className={`${styles.shell} ${referencesOpen ? "" : styles.noLeft} ${
        inspectorOpen ? "" : styles.noRight
      }`}
    >
      <header className={styles.toolbar}>
        <div className={styles.toolbarGroup}>
          <button
            type="button"
            className={styles.toggle}
            aria-pressed={referencesOpen}
            onClick={() => setReferencesOpen((open) => !open)}
          >
            References
          </button>
          <h1 className={styles.projectName}>{state.displayName}</h1>
        </div>

        <div className={styles.toolbarGroup}>
          <StatusPill
            label={state.astra?.label ?? "Astra via Codex"}
            message={state.astra?.message ?? "Checking…"}
            connected={state.astra?.connected ?? false}
            action={state.astra?.action ?? null}
          />
          <StatusPill
            label="Blender"
            message={state.blender?.message ?? "Checking…"}
            connected={state.blender?.connected ?? false}
            action={null}
          />
          <span className={styles.modelStatus}>{modelStatus}</span>
          <button
            type="button"
            className={styles.toggle}
            aria-pressed={inspectorOpen}
            onClick={() => setInspectorOpen((open) => !open)}
          >
            Inspector
          </button>
        </div>
      </header>

      {state.notice ? (
        <div className={styles.notice} role="status">
          <span>{state.notice}</span>
          <button type="button" className={styles.smallButton} onClick={session.dismissNotice}>
            Dismiss
          </button>
        </div>
      ) : null}

      {state.astra && !state.astra.connected ? (
        <AstraConnect
          client={session.client}
          status={state.astra}
          onConnected={() => {
            void session.refreshStatus();
            void session.refresh();
          }}
        />
      ) : null}

      <main className={styles.main}>
        {referencesOpen ? (
          <div className={styles.left} ref={uploadTrigger}>
            <ReferencesPanel
              references={state.references}
              attachedIds={state.attachedReferenceIds}
              uploads={state.uploads}
              onUpload={session.upload}
              onToggleAttachment={session.toggleAttachment}
              onRemove={session.removeReference}
              referenceUrl={session.referenceUrl}
              accept={SUPPORTED_UPLOAD_ACCEPT}
            />
          </div>
        ) : null}

        <div className={styles.centre}>
          <ModelViewer
            modelUrl={session.modelUrl}
            previewUrl={session.previewUrl}
            selectedObjectId={state.selectedObjectId}
            onSelect={session.selectObject}
            busy={session.busy}
          />
        </div>

        {inspectorOpen ? (
          <div className={styles.right}>
            <InspectorPanel
              scene={state.scene}
              selected={selected}
              facts={state.facts}
              onSaveFact={session.saveFact}
            />
          </div>
        ) : null}
      </main>

      <div className={styles.bottom}>
        <ConversationPanel
          entries={state.entries}
          collapsed={historyCollapsed}
          onToggleCollapsed={() => setHistoryCollapsed((collapsed) => !collapsed)}
          onDecide={session.decide}
          deciding={session.busy}
        />
        <Composer
          canSubmit={session.canSubmit}
          onSubmit={session.send}
          onAttachClick={requestAttach}
          attached={attachedReferences(state)}
          onDetach={session.toggleAttachment}
          selectedLabel={selected ? friendlyName(selected.name) : null}
          onClearSelection={() => session.selectObject(null)}
          busy={session.busy}
          hint={composerHint(state.astra?.connected, state.blender?.connected, approval !== null)}
        />
      </div>
    </div>
  );
}

function StatusPill({
  label,
  message,
  connected,
  action,
}: {
  label: string;
  message: string;
  connected: boolean;
  action: string | null;
}) {
  return (
    <span className={styles.statusPill} title={action ? `${message} (${action})` : message}>
      <span
        className={`${styles.dot} ${connected ? styles.dotOk : styles.dotDown}`}
        aria-hidden="true"
      />
      <span className={styles.statusLabel}>{label}</span>
      <span className="visuallyHidden">
        {message}
        {action ? ` Run: ${action}` : ""}
      </span>
      {!connected && action ? <code className={styles.statusAction}>{action}</code> : null}
    </span>
  );
}

function describeModelStatus(phase: string, hasModel: boolean): string {
  if (phase === "working") return "Updating the model…";
  if (phase === "sending") return "Thinking…";
  return hasModel ? "Model up to date" : "No model yet";
}

function composerHint(
  astraConnected: boolean | undefined,
  blenderConnected: boolean | undefined,
  awaitingApproval: boolean,
): string | undefined {
  if (awaitingApproval) return "A step above is waiting for your approval.";
  if (astraConnected === false) {
    return "Astra is not connected yet — uploads still work, and the toolbar shows what to run.";
  }
  if (blenderConnected === false) {
    return "Blender is not connected, so Astra can discuss your plans but not model them yet.";
  }
  return "Enter sends · Shift+Enter for a new line";
}
