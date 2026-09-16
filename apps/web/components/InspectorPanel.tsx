"use client";

/**
 * The inspector: what is selected, what is in the scene, and what the project knows.
 *
 * Three stacked cards rather than one long list, because they answer different questions
 * and get read at different moments.
 *
 * Layout note, learned the hard way: every row here is a two-line block (label above
 * value) instead of a label/value grid with fixed columns. A long fact key like
 * "coffee table decor" and a long value like "Simple modern setup, large beige floor
 * plane…" cannot be made to fit side by side in a narrow panel, and the previous grid
 * let them overlap. Stacking wraps instead of colliding, at any width.
 *
 * Measurements are shown in the unit a designer thinks in (centimetres under a metre,
 * metres above), while everything crossing a boundary stays canonical metres.
 */

import { useEffect, useState } from "react";

import { linearChannelToSrgbEncoded } from "@studio/spatial";

import type { FactView, SceneObjectView, SceneView } from "../lib/api/workspace.ts";
import styles from "./workspace.module.css";

export interface InspectorPanelProps {
  scene: SceneView | null;
  selected: SceneObjectView | null;
  facts: FactView[];
  onSaveFact(key: string, value: string): void;
  /** Selecting from the scene list, for anyone who would rather not click in 3D. */
  onSelect?(objectId: string | null): void;
}

export function InspectorPanel({
  scene,
  selected,
  facts,
  onSaveFact,
  onSelect,
}: InspectorPanelProps) {
  return (
    <section className={styles.inspector} aria-labelledby="inspector-heading">
      <header className={styles.panelHeader}>
        <h2 id="inspector-heading" className={styles.panelTitle}>
          Inspector
        </h2>
      </header>

      <div className={styles.panelScroll}>
        <Card title={selected ? friendlyName(selected.name) : "Selection"}>
          {selected ? (
            <SelectedObject selected={selected} onClear={() => onSelect?.(null)} />
          ) : (
            <p className={styles.cardHint}>
              {scene && scene.objects.length
                ? "Click something in the 3D view, or pick it from the scene below."
                : "Nothing in the model yet."}
            </p>
          )}
        </Card>

        {scene && scene.objects.length ? (
          <Card title="Scene" badge={`${scene.objects.length}`}>
            <ul className={styles.objectList}>
              {scene.objects.map((object) => {
                const id = object.studio_object_id;
                const isSelected =
                  selected != null &&
                  ((id != null && id === selected.studio_object_id) ||
                    object.name === selected.name);
                return (
                  <li key={object.name}>
                    <button
                      type="button"
                      className={`${styles.objectRow} ${isSelected ? styles.objectRowOn : ""}`}
                      aria-pressed={isSelected}
                      disabled={!onSelect || !id}
                      onClick={() => id && onSelect?.(id)}
                    >
                      <span className={styles.objectName}>{friendlyName(object.name)}</span>
                      <span className={styles.objectType}>{friendlyType(object)}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </Card>
        ) : null}

        <Card title="Project facts" badge={facts.length ? `${facts.length}` : undefined}>
          {facts.length === 0 ? (
            <p className={styles.cardHint}>
              Anything you confirm — a ceiling height, a material — is remembered here.
            </p>
          ) : (
            <ul className={styles.factList}>
              {facts.map((fact) => (
                <FactRow key={fact.key} fact={fact} onSave={onSaveFact} />
              ))}
            </ul>
          )}
        </Card>
      </div>
    </section>
  );
}

function Card({
  title,
  badge,
  children,
}: {
  title: string;
  badge?: string;
  children: React.ReactNode;
}) {
  return (
    <section className={styles.card}>
      <header className={styles.cardHeader}>
        <h3 className={styles.cardTitle}>{title}</h3>
        {badge ? <span className={styles.count}>{badge}</span> : null}
      </header>
      {children}
    </section>
  );
}

function SelectedObject({
  selected,
  onClear,
}: {
  selected: SceneObjectView;
  onClear(): void;
}) {
  const position = selected.world_position_meters;
  const size = selected.dimensions_meters;
  return (
    <>
      <div className={styles.rows}>
        <Row label="Type" value={friendlyType(selected)} />
        <Row
          label="Size (w × d × h)"
          value={`${formatLength(size.x)} × ${formatLength(size.y)} × ${formatLength(size.z)}`}
        />
        <Row
          label="Position"
          value={`${formatLength(position.x)} right · ${formatLength(position.y)} forward · ${formatLength(
            position.z,
          )} up`}
        />
        {selected.material?.base_color ? (
          <Row
            label="Colour"
            value={
              <span className={styles.swatchRow}>
                <span
                  className={styles.swatch}
                  style={{ background: cssColour(selected.material.base_color) }}
                  aria-hidden="true"
                />
                {cssColour(selected.material.base_color)}
              </span>
            }
          />
        ) : null}
        {selected.visible ? null : <Row label="Visibility" value="Hidden in Blender" />}
      </div>
      <button type="button" className={styles.ghostButton} onClick={onClear}>
        Clear selection
      </button>
    </>
  );
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className={styles.row}>
      <span className={styles.rowLabel}>{label}</span>
      <span className={styles.rowValue}>{value}</span>
    </div>
  );
}

function FactRow({
  fact,
  onSave,
}: {
  fact: FactView;
  onSave(key: string, value: string): void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(fact.value);

  // If the fact changes underneath (Astra learned something new), follow it rather than
  // showing a stale draft.
  useEffect(() => {
    if (!editing) setDraft(fact.value);
  }, [fact.value, editing]);

  return (
    <li className={styles.factRow}>
      <span className={styles.rowLabel}>{humaniseKey(fact.key)}</span>
      {editing ? (
        <form
          className={styles.factEdit}
          onSubmit={(event) => {
            event.preventDefault();
            onSave(fact.key, draft.trim() || fact.value);
            setEditing(false);
          }}
        >
          <input
            className={styles.factInput}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            aria-label={`${humaniseKey(fact.key)} value`}
            autoFocus
          />
          <button type="submit" className={styles.ghostButton}>
            Save
          </button>
          <button
            type="button"
            className={styles.ghostButton}
            onClick={() => {
              setDraft(fact.value);
              setEditing(false);
            }}
          >
            Cancel
          </button>
        </form>
      ) : (
        <span className={styles.factValueRow}>
          <span className={styles.rowValue}>{fact.value}</span>
          <button
            type="button"
            className={styles.ghostButton}
            onClick={() => setEditing(true)}
            aria-label={`Change ${humaniseKey(fact.key)}`}
          >
            Change
          </button>
        </span>
      )}
    </li>
  );
}

/** "Wall_North_obj_8d83f" reads as "Wall North" to a person. */
export function friendlyName(name: string): string {
  const withoutId = name.replace(/_?obj_[a-z0-9]{4,}$/i, "");
  return withoutId.replace(/[_-]+/g, " ").trim() || name;
}

function friendlyType(object: SceneObjectView): string {
  const name = object.name.toLowerCase();
  if (name.includes("wall")) return "Wall";
  if (name.includes("floor")) return "Floor";
  if (name.includes("ceiling")) return "Ceiling";
  if (name.includes("door")) return "Door";
  if (name.includes("window")) return "Window";
  if (name.includes("table")) return "Table";
  if (name.includes("camera")) return "Camera";
  if (name.includes("light") || name.includes("lamp")) return "Light";
  return object.object_type === "MESH" ? "Object" : titleCase(object.object_type);
}

/** Metres above one metre, centimetres below: how a designer reads a drawing. */
export function formatLength(metres: number): string {
  if (!Number.isFinite(metres)) return "—";
  const absolute = Math.abs(metres);
  if (absolute < 1) return `${Math.round(metres * 100)} cm`;
  return `${metres.toFixed(2)} m`;
}

function cssColour(colour: { r: number; g: number; b: number }): string {
  // Linear sRGB to encoded sRGB, for display only. The canonical value stays linear,
  // and the transfer function itself lives in @studio/spatial so the browser cannot
  // drift from the backend.
  const encode = (channel: number) => {
    const encoded = linearChannelToSrgbEncoded(Math.min(Math.max(channel, 0), 1));
    return Math.round((encoded ?? 0) * 255);
  };
  return `rgb(${encode(colour.r)}, ${encode(colour.g)}, ${encode(colour.b)})`;
}

function humaniseKey(key: string): string {
  return titleCase(key.replace(/_m$/, "").replace(/_/g, " "));
}

function titleCase(value: string): string {
  const lower = value.toLowerCase().replace(/_/g, " ");
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}
