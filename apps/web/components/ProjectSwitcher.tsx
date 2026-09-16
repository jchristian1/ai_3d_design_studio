"use client";

/**
 * The project name in the top bar, and the menu behind it.
 *
 * Switching project, renaming this one, or going back to the project screen — the three
 * things you want from a title. Closes on Escape and on a click outside, because a menu
 * that traps you is worse than no menu.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import type { ProjectSummaryView } from "../lib/api/workspace.ts";
import { describe, relativeTime } from "./ProjectHome.tsx";
import styles from "./workspace.module.css";

export interface ProjectSwitcherProps {
  current: ProjectSummaryView | null;
  projects: ProjectSummaryView[];
  onOpen(projectId: string): void;
  onRename(projectId: string, displayName: string): void;
  onClose(): void;
  fallbackName?: string;
}

export function ProjectSwitcher({
  current,
  projects,
  onOpen,
  onRename,
  onClose,
  fallbackName = "Project",
}: ProjectSwitcherProps) {
  const [open, setOpen] = useState(false);
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(current?.display_name ?? "");
  const container = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setDraft(current?.display_name ?? "");
  }, [current?.display_name]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    const onClick = (event: MouseEvent) => {
      if (!container.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
  }, [open]);

  const submitRename = useCallback(
    (event: React.FormEvent) => {
      event.preventDefault();
      const name = draft.trim();
      if (current && name && name !== current.display_name) {
        onRename(current.project_id, name);
      }
      setRenaming(false);
      setOpen(false);
    },
    [current, draft, onRename],
  );

  const name = current?.display_name ?? fallbackName;
  const others = projects.filter((project) => project.project_id !== current?.project_id);

  if (renaming && current) {
    return (
      <form className={styles.switcher} onSubmit={submitRename}>
        <label className="visuallyHidden" htmlFor="project-rename">
          Project name
        </label>
        <input
          id="project-rename"
          className={styles.factInput}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          autoFocus
        />
        <button type="submit" className={styles.ghostButton}>
          Save
        </button>
        <button
          type="button"
          className={styles.ghostButton}
          onClick={() => {
            setDraft(current.display_name);
            setRenaming(false);
          }}
        >
          Cancel
        </button>
      </form>
    );
  }

  return (
    <div className={styles.switcher} ref={container}>
      <button
        type="button"
        className={styles.switcherButton}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => setOpen((value) => !value)}
      >
        <span className={styles.projectName}>{name}</span>
        <span className={styles.caret} aria-hidden="true">
          ▾
        </span>
        <span className="visuallyHidden">Switch project</span>
      </button>

      {open ? (
        <div className={styles.menu} role="menu">
          <button
            type="button"
            role="menuitem"
            className={styles.menuItem}
            onClick={() => {
              setRenaming(true);
              setOpen(false);
            }}
          >
            Rename this project
          </button>
          <button
            type="button"
            role="menuitem"
            className={styles.menuItem}
            onClick={() => {
              setOpen(false);
              onClose();
            }}
          >
            All projects…
            <span className={styles.menuMeta}>Open another, or start a new one</span>
          </button>

          {others.length ? (
            <>
              <div className={styles.menuDivider} />
              <p className={styles.menuLabel}>Switch to</p>
              {others.slice(0, 8).map((project) => (
                <button
                  key={project.project_id}
                  type="button"
                  role="menuitem"
                  className={styles.menuItem}
                  onClick={() => {
                    setOpen(false);
                    onOpen(project.project_id);
                  }}
                >
                  {project.display_name}
                  <span className={styles.menuMeta}>
                    {describe(project)}
                    {project.last_opened_at
                      ? ` · opened ${relativeTime(project.last_opened_at)}`
                      : ""}
                  </span>
                </button>
              ))}
            </>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
