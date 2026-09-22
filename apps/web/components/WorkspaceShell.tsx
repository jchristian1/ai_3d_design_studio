"use client";

/**
 * The design workspace.
 *
 *   ┌ top bar: project · Astra · Blender · model ────────────────────────┐
 *   │ conversation │                                    │  inspector    │
 *   │  + files     │            3D model                │  scene        │
 *   │  + composer  │                                    │  facts        │
 *   └──────────────┴────────────────────────────────────┴───────────────┘
 *
 * Chat on the LEFT, in one column, with the composer pinned to the bottom of that
 * column: talking and looking happen side by side, and the model keeps a large, stable
 * area rather than being squeezed by a transcript that grows across the whole width.
 *
 * Both side columns collapse and the model takes the space they give up.
 */

import { useCallback, useMemo, useState } from "react";

import { SUPPORTED_UPLOAD_ACCEPT } from "../lib/config.ts";
import { useWorkspace, type UseWorkspaceOptions } from "../hooks/useWorkspace.ts";
import { pendingApproval, selectedObject, attachedReferences } from "../lib/workspace/types.ts";
import { AstraConnect } from "./AstraConnect.tsx";
import { ChatColumn } from "./ChatColumn.tsx";
import { InspectorPanel, friendlyName } from "./InspectorPanel.tsx";
import { ModelViewer } from "./ModelViewer.tsx";
import { PreviewPanel } from "./PreviewPanel.tsx";
import styles from "./workspace.module.css";

export interface WorkspaceShellProps {
  /** Injected by tests to supply stub clients; unused in the app. */
  sessionOptions?: UseWorkspaceOptions;
  /** Shown in the top bar. Falls back to whatever the workspace reports. */
  projectName?: string;
  /** Rendered inside the top bar: the project switcher, when there is one. */
  projectControl?: React.ReactNode;
}

export function WorkspaceShell({
  sessionOptions,
  projectName,
  projectControl,
}: WorkspaceShellProps) {
  const session = useWorkspace(sessionOptions ?? {});
  const { state } = session;

  const [chatOpen, setChatOpen] = useState(true);
  const [inspectorOpen, setInspectorOpen] = useState(true);
  //: The rendered still is on by default. It is the only view that shows lighting, and
  //: defaulting it off is what made a working feature look broken.
  const [renderOpen, setRenderOpen] = useState(true);
  const [reloading, setReloading] = useState(false);

  /**
   * Reload the design machine's code.
   *
   * Stays busy for a few seconds rather than clearing immediately: the machine restarts
   * at its next idle moment, so refreshing the status straight away would still read
   * "connected" and the button would look as though it had done nothing. The wait is
   * about reporting honestly, not about the request taking time.
   */
  const reloadMachine = useCallback(async () => {
    if (reloading) return;
    setReloading(true);
    try {
      await session.client.reloadWorker();
    } catch {
      // Not worth interrupting the user over: the machine is still connected and still
      // working, just on its previous code.
    } finally {
      window.setTimeout(() => {
        setReloading(false);
        void session.refreshStatus();
      }, 4000);
    }
  }, [reloading, session]);

  const selected = selectedObject(state);
  const approval = pendingApproval(state);
  const attached = useMemo(() => attachedReferences(state), [state]);

  // Only give the render its row once there is something to put in it, so a project with
  // no preview yet does not lose a third of the viewport to an empty panel.
  const showRender = renderOpen && session.previewUrl !== null;

  const clearSelection = useCallback(() => session.selectObject(null), [session]);

  return (
    <div
      className={`${styles.shell} ${chatOpen ? "" : styles.noChat} ${
        inspectorOpen ? "" : styles.noInspector
      }`}
    >
      <header className={styles.topBar}>
        <div className={styles.topLeft}>
          <button
            type="button"
            className={styles.toggle}
            aria-pressed={chatOpen}
            onClick={() => setChatOpen((open) => !open)}
            title="Show or hide the conversation"
          >
            Chat
          </button>
          {projectControl ?? (
            <h1 className={styles.projectName}>{projectName ?? state.displayName}</h1>
          )}
        </div>

        <div className={styles.topRight}>
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
            // The design machine reloads itself when the studio's code changes, so this
            // is the override: press it when it has been busy, or when you want to be
            // certain rather than to wait.
            button={
              state.blender?.can_reload
                ? { label: reloading ? "Reloading…" : "Reload", onPress: reloadMachine, busy: reloading }
                : null
            }
          />
          <StatusPill
            label={state.gpu?.label ?? "GPU"}
            message={state.gpu?.message ?? "Checking…"}
            connected={state.gpu?.connected ?? false}
            action={null}
          />
          <span className={styles.modelStatus}>
            {describeModelStatus(state.phase, Boolean(state.model))}
          </span>
          <button
            type="button"
            className={styles.toggle}
            aria-pressed={renderOpen}
            onClick={() => setRenderOpen((open) => !open)}
            title="Show or hide the rendered still"
          >
            Render
          </button>
          <button
            type="button"
            className={styles.toggle}
            aria-pressed={inspectorOpen}
            onClick={() => setInspectorOpen((open) => !open)}
            title="Show or hide the inspector"
          >
            Inspector
          </button>
        </div>
      </header>

      {state.notice ? (
        <div className={styles.notice} role="status">
          <span>{state.notice}</span>
          <button type="button" className={styles.ghostButton} onClick={session.dismissNotice}>
            Dismiss
          </button>
        </div>
      ) : null}

      <div className={styles.body}>
        {chatOpen ? (
          <div className={styles.chatSlot}>
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

            <ChatColumn
              entries={state.entries}
              references={state.references}
              attachedIds={state.attachedReferenceIds}
              attached={attached}
              uploads={state.uploads}
              accept={SUPPORTED_UPLOAD_ACCEPT}
              busy={session.busy}
              selectedLabel={selected ? friendlyName(selected.name) : null}
              hint={composerHint(
                state.astra?.connected,
                state.blender?.connected,
                approval !== null,
              )}
              canSubmit={session.canSubmit}
              onSubmit={session.send}
              onUpload={session.upload}
              onToggleAttachment={session.toggleAttachment}
              onRemoveReference={session.removeReference}
              referenceUrl={session.referenceUrl}
              onDecide={session.decide}
              onClearSelection={clearSelection}
            />
          </div>
        ) : null}

        <main className={`${styles.stage} ${showRender ? styles.stageWithRender : ""}`}>
          <ModelViewer
            modelUrl={session.modelUrl}
            previewUrl={session.previewUrl}
            selectedObjectId={state.selectedObjectId}
            onSelect={session.selectObject}
            busy={session.busy}
          />
          {/* The rendered still, beneath the interactive model.
            *
            * These are two different pictures of one design and both are worth having.
            * The model is what you orbit and click; the render is what Blender actually
            * produced, and it is the ONLY place lighting appears — a GLB carries no
            * lights, so the interactive view cannot show them at all. Leaving this panel
            * out is why several rounds of lighting work were invisible in the browser
            * while being perfectly correct on disk. */}
          {showRender ? (
            <div className={styles.renderSlot}>
              <PreviewPanel
                src={session.previewUrl}
                // The workspace's artifact record carries no dimensions, so none are
                // claimed. The image still displays; only the size label is absent.
                preview={null}
                stale={session.busy}
                warning={null}
                busy={session.busy}
              />
            </div>
          ) : null}
        </main>

        {inspectorOpen ? (
          <div className={styles.inspectorSlot}>
            <InspectorPanel
              scene={state.scene}
              selected={selected}
              facts={state.facts}
              onSaveFact={session.saveFact}
              onSelect={session.selectObject}
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}

function StatusPill({
  label,
  message,
  connected,
  action,
  button = null,
}: {
  label: string;
  message: string;
  connected: boolean;
  /** A command the user runs themselves, shown as text. */
  action: string | null;
  /** Something the studio can do on their behalf, shown as a button. */
  button?: { label: string; onPress: () => void; busy?: boolean } | null;
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
      {button ? (
        <button
          type="button"
          className={styles.statusButton}
          onClick={button.onPress}
          disabled={button.busy}
          // The label alone ("Reload") does not say what is reloaded, which matters
          // most to someone reaching it by screen reader.
          aria-label={`Reload ${label}`}
        >
          {button.label}
        </button>
      ) : null}
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
    return "Astra is not connected yet — uploads still work, and the panel above shows how to sign in.";
  }
  if (blenderConnected === false) {
    return "Blender is not connected, so Astra can discuss your plans but not model them yet.";
  }
  return "Enter sends · Shift+Enter for a new line";
}
