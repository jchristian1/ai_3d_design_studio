/**
 * Identifier generation.
 *
 * `request_id` is the MUTATION IDENTITY of a submission (Task 3): the backend
 * derives `idempotency_key` from `(project_id, request_id, operation_index)`, so
 * these ids decide whether a command executes once or twice.
 *
 * That makes collisions a correctness problem, not an aesthetic one:
 *
 *   - two different commands sharing a `request_id` would be treated as the same
 *     mutation, and the second would be silently swallowed (the API answers 409
 *     when the content differs, which is a client bug, not a user error);
 *   - a retry that generated a NEW id would apply the change twice.
 *
 * `crypto.randomUUID()` is therefore used when available — it is a
 * cryptographically strong v4 UUID and is present in every browser this app
 * targets and in Node 19+. `Math.random()` is not an acceptable primary source
 * here; the fallback exists only so a non-secure context degrades instead of
 * crashing, and it still draws from `crypto.getRandomValues` when it can.
 */

/** A raw UUID v4 string. */
export function uuid(): string {
  const webCrypto: Crypto | undefined =
    typeof globalThis !== "undefined" ? globalThis.crypto : undefined;

  if (webCrypto && typeof webCrypto.randomUUID === "function") {
    return webCrypto.randomUUID();
  }

  // Fallback: build a v4 UUID from strong random bytes when they are available.
  if (webCrypto && typeof webCrypto.getRandomValues === "function") {
    const bytes = new Uint8Array(16);
    webCrypto.getRandomValues(bytes);
    bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40; // version 4
    bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80; // RFC 4122 variant
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
    return [
      hex.slice(0, 8),
      hex.slice(8, 12),
      hex.slice(12, 16),
      hex.slice(16, 20),
      hex.slice(20),
    ].join("-");
  }

  // Last resort only. Reached in no supported runtime; documented so its
  // weakness is never mistaken for an acceptable default.
  throw new Error(
    "no cryptographic random source is available, so a safe request_id cannot " +
      "be generated",
  );
}

/** A fresh submission identity. Reused verbatim only for an explicit retry. */
export function newRequestId(): string {
  return `req_${uuid().replace(/-/g, "")}`;
}

/** A browser-session identity, generated once per page session. */
export function newSessionId(): string {
  return `sess_${uuid().replace(/-/g, "")}`;
}
