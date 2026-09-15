"use client";

import { PROJECT_LABEL } from "../lib/config.ts";
import type { UseDesignSessionOptions } from "../hooks/useDesignSession.ts";
import { useDesignSession } from "../hooks/useDesignSession.ts";
import { ChatPanel } from "./ChatPanel.tsx";
import { ConnectionIndicator } from "./ConnectionIndicator.tsx";
import { MessageInput } from "./MessageInput.tsx";
import { PreviewPanel } from "./PreviewPanel.tsx";
import { StatusBar } from "./StatusBar.tsx";
import styles from "./StudioShell.module.css";

/**
 * The whole interface.
 *
 * A single client component that owns the session and passes plain data down. The
 * child components are presentational — they receive values and callbacks and hold
 * no knowledge of the API, which is what keeps backend response shapes out of the
 * component tree.
 *
 * `sessionOptions` exists so tests can inject a stub API client and a controllable
 * job-update source and drive the real component. Production passes nothing.
 */
export function StudioShell({
  sessionOptions,
}: {
  sessionOptions?: UseDesignSessionOptions;
}) {
  const session = useDesignSession(sessionOptions ?? {});
  const { state } = session;

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <div className={styles.brand}>
          <h1 className={styles.title}>AI 3D Design Studio</h1>
          <p className={styles.project}>{PROJECT_LABEL}</p>
        </div>
        <ConnectionIndicator connection={session.connection} />
      </header>

      <main className={styles.main}>
        <div className={styles.chatColumn}>
          <ChatPanel messages={state.messages} />
          <MessageInput
            canSubmit={session.canSubmit}
            onSubmit={(message) => void session.submit(message)}
            busy={session.busy}
            retryAvailable={state.retryable !== null}
            onRetry={() => void session.retry()}
          />
        </div>

        <div className={styles.previewColumn}>
          <PreviewPanel
            src={session.previewSrc}
            preview={state.preview ?? state.stalePreview}
            stale={session.previewIsStale}
            warning={state.previewWarning}
            busy={session.busy}
          />
        </div>
      </main>

      <StatusBar projectLabel={PROJECT_LABEL} state={state} />
    </div>
  );
}
