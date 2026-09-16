"use client";

/**
 * The screen you get when no project is open.
 *
 * Two things only: start something new, or carry on with something you already have.
 * A project card says enough to recognise it — its name, when it was last opened, how
 * many references and messages it has, whether there is a model to look at — and nothing
 * technical. No ids, no paths, no counts of jobs.
 */

import { useCallback, useState } from "react";

import type { ProjectSummaryView } from "../lib/api/workspace.ts";
import styles from "./home.module.css";

export interface ProjectHomeProps {
  projects: ProjectSummaryView[];
  loading: boolean;
  error: string | null;
  onOpen(projectId: string): void;
  onCreate(displayName: string): void;
  astraLabel?: string;
  astraConnected?: boolean;
  blenderConnected?: boolean;
}

export function ProjectHome({
  projects,
  loading,
  error,
  onOpen,
  onCreate,
  astraLabel = "Astra",
  astraConnected = false,
  blenderConnected = false,
}: ProjectHomeProps) {
  const [name, setName] = useState("");

  const submit = useCallback(
    (event: React.FormEvent) => {
      event.preventDefault();
      const trimmed = name.trim();
      if (!trimmed) return;
      onCreate(trimmed);
      setName("");
    },
    [name, onCreate],
  );

  return (
    <div className={styles.home}>
      <div className={styles.inner}>
        <header className={styles.header}>
          <p className={styles.eyebrow}>AI 3D Design Studio</p>
          <h1 className={styles.title}>What are we designing?</h1>
          <p className={styles.subtitle}>
            Describe a room in plain language, or upload a floor plan, and it is built in
            Blender for you.
          </p>
          <div className={styles.statusRow}>
            <StatusChip label={astraLabel} ok={astraConnected} />
            <StatusChip label="Blender" ok={blenderConnected} />
          </div>
        </header>

        <form className={styles.createCard} onSubmit={submit}>
          <label className={styles.createLabel} htmlFor="new-project-name">
            New project
          </label>
          <div className={styles.createRow}>
            <input
              id="new-project-name"
              className={styles.createInput}
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Beach house kitchen"
              autoComplete="off"
            />
            <button type="submit" className={styles.primary} disabled={!name.trim()}>
              Create project
            </button>
          </div>
          <p className={styles.createHint}>
            You can rename it later. Nothing is built until you ask for something.
          </p>
        </form>

        {error ? (
          <p className={styles.error} role="alert">
            {error}
          </p>
        ) : null}

        <section className={styles.recent} aria-labelledby="recent-heading">
          <h2 id="recent-heading" className={styles.sectionTitle}>
            Your projects
          </h2>

          {loading && projects.length === 0 ? (
            <p className={styles.muted}>Loading…</p>
          ) : projects.length === 0 ? (
            <p className={styles.muted}>
              Nothing yet. Give your first project a name above.
            </p>
          ) : (
            <ul className={styles.projectList}>
              {projects.map((project) => (
                <li key={project.project_id}>
                  <button
                    type="button"
                    className={styles.projectCard}
                    onClick={() => onOpen(project.project_id)}
                  >
                    <span className={styles.projectName}>{project.display_name}</span>
                    <span className={styles.projectMeta}>{describe(project)}</span>
                    <span className={styles.projectWhen}>
                      {project.last_opened_at
                        ? `Opened ${relativeTime(project.last_opened_at)}`
                        : `Created ${relativeTime(project.created_at)}`}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}

function StatusChip({ label, ok }: { label: string; ok: boolean }) {
  return (
    <span className={styles.statusChip}>
      <span
        className={`${styles.dot} ${ok ? styles.dotOk : styles.dotDown}`}
        aria-hidden="true"
      />
      {label}
      <span className="visuallyHidden">{ok ? " connected" : " not connected"}</span>
    </span>
  );
}

export function describe(project: ProjectSummaryView): string {
  const parts: string[] = [];
  if (project.has_model) parts.push("3D model");
  if (project.reference_count === 1) parts.push("1 reference");
  else if (project.reference_count > 1) parts.push(`${project.reference_count} references`);
  if (project.message_count === 1) parts.push("1 message");
  else if (project.message_count > 1) parts.push(`${project.message_count} messages`);
  return parts.length ? parts.join(" · ") : "Empty project";
}

/** A human sense of time. Deliberately coarse: exact timestamps are noise here. */
export function relativeTime(iso: string, now: Date = new Date()): string {
  const then = new Date(iso);
  const seconds = Math.max(0, Math.round((now.getTime() - then.getTime()) / 1000));
  if (!Number.isFinite(seconds)) return "recently";
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days} day${days === 1 ? "" : "s"} ago`;
  return then.toLocaleDateString();
}
