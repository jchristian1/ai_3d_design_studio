"use client";

import type { ConnectionState } from "../hooks/useDesignSession.ts";
import styles from "./ConnectionIndicator.module.css";

/**
 * The development connection indicator.
 *
 * Reports two INDEPENDENT facts, because they genuinely are independent: the
 * control plane can be perfectly healthy while no design machine is connected, in
 * which case commands will fail. Collapsing them into one green light would tell
 * the user everything is fine right up until they try to change something.
 *
 * Nothing sensitive is shown: no worker id, no token, no host, no path, no version.
 * Just whether each side is up.
 */
export function ConnectionIndicator({
  connection,
}: {
  connection: ConnectionState;
}) {
  const apiState = !connection.checked
    ? "unknown"
    : connection.apiReachable
      ? "ok"
      : "down";

  const machineState = !connection.checked
    ? "unknown"
    : !connection.apiReachable
      ? "unknown"
      : connection.designMachineReady
        ? "ok"
        : "down";

  return (
    <div className={styles.indicator}>
      <Light label="Service" state={apiState} />
      <Light label="Design machine" state={machineState} />
    </div>
  );
}

const STATE_TEXT: Record<string, string> = {
  ok: "connected",
  down: "unavailable",
  unknown: "checking",
};

function Light({
  label,
  state,
}: {
  label: string;
  state: "ok" | "down" | "unknown";
}) {
  return (
    <span className={styles.item}>
      <span
        className={`${styles.dot} ${styles[state]}`}
        // The dot is decorative; the accessible text comes from the label below.
        aria-hidden="true"
      />
      <span className={styles.label}>{label}</span>
      {/* Screen readers get the actual state, which colour alone cannot convey. */}
      <span className="visuallyHidden">{`: ${STATE_TEXT[state]}`}</span>
    </span>
  );
}
