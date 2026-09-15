"use client";

import { useEffect, useRef } from "react";

import type { ChatMessage } from "../lib/session/types.ts";
import styles from "./ChatPanel.module.css";

/**
 * The conversation transcript.
 *
 * Two accessibility decisions worth stating:
 *
 *   - The list is a `<ol>`: the messages are an ordered sequence, and a screen
 *     reader announcing "list, 4 items" is genuinely useful here.
 *   - Studio replies live in an `aria-live="polite"` region, so a status change is
 *     announced without the user having to go looking for it. Polite rather than
 *     assertive: progress updates should not interrupt what someone is typing.
 */
export function ChatPanel({ messages }: { messages: ChatMessage[] }) {
  const endRef = useRef<HTMLDivElement>(null);

  // Follow the conversation as it grows. `block: "nearest"` avoids yanking the
  // whole page around when the panel is not the scroll container.
  //
  // Guarded because `scrollIntoView` is an optional convenience, not a guarantee:
  // it is absent in jsdom and in some embedded webviews, and an auto-scroll is
  // never worth throwing from an effect and tearing down the transcript.
  useEffect(() => {
    const end = endRef.current;
    if (end && typeof end.scrollIntoView === "function") {
      end.scrollIntoView({ block: "nearest" });
    }
  }, [messages.length]);

  return (
    <section className={styles.panel} aria-labelledby="chat-heading">
      <h2 id="chat-heading" className={styles.heading}>
        Design conversation
      </h2>

      <div className={styles.scroll}>
        {messages.length === 0 ? (
          <p className={styles.empty}>
            Describe a change to the design. For example:{" "}
            <span className={styles.example}>
              &ldquo;Move Cube 50 cm to the right.&rdquo;
            </span>
          </p>
        ) : (
          <ol className={styles.list}>
            {messages.map((message) => (
              <li
                key={message.id}
                className={`${styles.message} ${
                  message.author === "user" ? styles.user : styles.studio
                } ${styles[message.tone] ?? ""}`}
              >
                <span className={styles.author}>
                  {message.author === "user" ? "You" : "Studio"}
                </span>
                <p className={styles.text}>{message.text}</p>
              </li>
            ))}
          </ol>
        )}

        {/*
          Live region for studio replies. It carries only the latest studio line,
          so a screen reader announces the change rather than re-reading history.
        */}
        <div className="visuallyHidden" aria-live="polite" aria-atomic="true">
          {latestStudioText(messages)}
        </div>

        <div ref={endRef} />
      </div>
    </section>
  );
}

function latestStudioText(messages: ChatMessage[]): string {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message && message.author === "studio") return message.text;
  }
  return "";
}
