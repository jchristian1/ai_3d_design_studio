"use client";

/**
 * Signing in to ChatGPT, from the browser.
 *
 * This drives the OFFICIAL `codex login` flow through the backend and shows the user
 * exactly what to do. It deliberately does NOT:
 *
 * - ask for a ChatGPT password (it never sees one),
 * - implement OAuth, or talk to auth.openai.com itself,
 * - offer an API key field — this studio uses your ChatGPT allowance.
 *
 * The link it shows comes from the Codex CLI, so it is whatever OpenAI's own flow
 * produced. In the default mode the CLI also opens your browser itself, so this panel
 * is the fallback for when that does not happen. In device-code mode it shows the
 * verification page and the one-time code to type there.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiFailure } from "../lib/api/errors.ts";
import type { ConnectionStatusView, LoginSessionView, WorkspaceClient } from "../lib/api/workspace.ts";
import styles from "./workspace.module.css";

export interface AstraConnectProps {
  client: WorkspaceClient;
  status: ConnectionStatusView | null;
  /** Called once sign-in completes, so the workspace can refresh. */
  onConnected(): void;
  /** How often to check whether authorisation finished. */
  pollIntervalMs?: number;
}

export function AstraConnect({
  client,
  status,
  onConnected,
  pollIntervalMs = 1500,
}: AstraConnectProps) {
  const [session, setSession] = useState<LoginSessionView | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const connectedRef = useRef(false);

  const connected = status?.connected ?? false;
  const needsLogin = !connected && status?.state === "login_required";
  const waiting = session?.waiting ?? false;

  // While the user is authorising in another tab, poll until Codex reports success.
  useEffect(() => {
    if (!waiting) return;
    let cancelled = false;

    const timer = setInterval(async () => {
      try {
        const next = await client.getAstraLogin();
        if (cancelled) return;
        setSession(next);
        if (next.state === "complete" || next.status?.connected) {
          clearInterval(timer);
          if (!connectedRef.current) {
            connectedRef.current = true;
            onConnected();
          }
        }
        if (next.state === "failed" && next.detail) setError(next.detail);
      } catch {
        // A dropped poll is not a failure: the CLI is still waiting either way.
      }
    }, pollIntervalMs);

    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [client, waiting, onConnected, pollIntervalMs]);

  const start = useCallback(
    async (deviceAuth: boolean) => {
      setStarting(true);
      setError(null);
      connectedRef.current = false;
      try {
        const next = await client.beginAstraLogin(deviceAuth);
        setSession(next);
        if (next.state === "complete" || next.status?.connected) onConnected();
      } catch (caught) {
        setError(
          caught instanceof ApiFailure
            ? caught.message
            : "Codex could not start the sign-in.",
        );
      } finally {
        setStarting(false);
      }
    },
    [client, onConnected],
  );

  const cancel = useCallback(async () => {
    try {
      const next = await client.cancelAstraLogin();
      setSession(next);
    } catch {
      setSession(null);
    }
  }, [client]);

  const copyCode = useCallback(async () => {
    if (!session?.user_code) return;
    try {
      await navigator.clipboard?.writeText(session.user_code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  }, [session?.user_code]);

  if (connected) return null;

  return (
    <section className={styles.connectPanel} aria-labelledby="astra-connect-heading">
      <div className={styles.connectHeader}>
        <h2 id="astra-connect-heading" className={styles.connectTitle}>
          Connect Astra
        </h2>
        <p className={styles.connectMessage}>
          {status?.message ?? "Checking whether Codex is signed in…"}
        </p>
      </div>

      {/* Codex is missing or too old: the only useful thing is the exact command. */}
      {status && (status.state === "not_installed" || status.state === "update_required") ? (
        <p className={styles.connectBody}>
          Run this in a terminal, then reload:{" "}
          <code className={styles.connectCode}>{status.action}</code>
        </p>
      ) : null}

      {needsLogin && !waiting && session?.state !== "complete" ? (
        <div className={styles.connectActions}>
          <button
            type="button"
            className={styles.sendButton}
            disabled={starting}
            onClick={() => start(false)}
          >
            {starting ? "Starting…" : "Sign in with ChatGPT"}
          </button>
          <button
            type="button"
            className={styles.smallButton}
            disabled={starting}
            onClick={() => start(true)}
          >
            Use a device code instead
          </button>
        </div>
      ) : null}

      {waiting ? (
        <div className={styles.connectWaiting} role="status">
          {session?.user_code ? (
            <>
              <p className={styles.connectBody}>
                1. Open{" "}
                <a
                  className={styles.connectLink}
                  href={session.verification_url ?? undefined}
                  target="_blank"
                  rel="noreferrer noopener"
                >
                  {session.verification_url}
                </a>
              </p>
              <p className={styles.connectBody}>2. Enter this one-time code:</p>
              <div className={styles.codeRow}>
                <code className={styles.oneTimeCode}>{session.user_code}</code>
                <button type="button" className={styles.smallButton} onClick={copyCode}>
                  {copied ? "Copied" : "Copy"}
                </button>
              </div>
            </>
          ) : (
            <p className={styles.connectBody}>
              A ChatGPT sign-in page should have opened. If it did not,{" "}
              <a
                className={styles.connectLink}
                href={session?.verification_url ?? undefined}
                target="_blank"
                rel="noreferrer noopener"
              >
                open it here
              </a>
              .
            </p>
          )}

          <p className={styles.connectHint}>
            Waiting for you to authorise… this page updates itself when you are done.
          </p>
          <button type="button" className={styles.smallButton} onClick={cancel}>
            Cancel
          </button>
        </div>
      ) : null}

      {session?.state === "cancelled" ? (
        <p className={styles.connectHint}>Sign-in cancelled.</p>
      ) : null}

      {error ? (
        <p className={styles.connectError} role="alert">
          {error}
        </p>
      ) : null}

      <p className={styles.connectHint}>
        This studio uses your ChatGPT sign-in through the Codex client. It never asks for
        your password and never uses an API key.
      </p>
    </section>
  );
}
