"use client";

/**
 * The inspector: what is selected, and what the project knows.
 *
 * Measurements are shown in the unit a designer thinks in (centimetres under a metre,
 * metres above), while everything crossing a boundary stays canonical metres. The stable
 * `studio_object_id` is available but tucked into a developer detail, because it is
 * plumbing rather than something a designer should have to read.
 */

import { useState } from "react";

import { linearChannelToSrgbEncoded } from "@studio/spatial";

import type { FactView, SceneObjectView, SceneView } from "../lib/api/workspace.ts";
import styles from "./workspace.module.css";

export interface InspectorPanelProps {
  scene: SceneView | null;
  selected: SceneObjectView | null;
  facts: FactView[];
  onSaveFact(key: string, value: string): void;
}

export function InspectorPanel({ scene, selected, facts, onSaveFact }: InspectorPanelProps) {
  return (
    <section className={styles.sidePanel} aria-labelledby="inspector-heading">
      <header className={styles.sideHeader}>
        <h2 id="inspector-heading" className={styles.sideTitle}>
          Inspector
        </h2>
      </header>

      {selected ? (
        <dl className={styles.propertyList}>
          <div className={styles.property}>
            <dt>Name</dt>
            <dd>{friendlyName(selected.name)}</dd>
          </div>
          <div className={styles.property}>
            <dt>Type</dt>
            <dd>{friendlyType(selected)}</dd>
          </div>
          <div className={styles.property}>
            <dt>Size</dt>
            <dd>
              {formatLength(selected.dimensions_meters.x)} ×{" "}
              {formatLength(selected.dimensions_meters.y)} ×{" "}
              {formatLength(selected.dimensions_meters.z)}
            </dd>
          </div>
          <div className={styles.property}>
            <dt>Position</dt>
            <dd>
              {formatLength(selected.world_position_meters.x)},{" "}
              {formatLength(selected.world_position_meters.y)},{" "}
              {formatLength(selected.world_position_meters.z)}
            </dd>
          </div>
          {selected.material?.base_color ? (
            <div className={styles.property}>
              <dt>Colour</dt>
              <dd className={styles.colourRow}>
                <span
                  className={styles.swatch}
                  style={{ background: cssColour(selected.material.base_color) }}
                  aria-hidden="true"
                />
                {describeColour(selected.material.base_color)}
              </dd>
            </div>
          ) : null}
          {selected.studio_object_id ? (
            <details className={styles.developerDetails}>
              <summary>Developer details</summary>
              <p className={styles.monospace}>{selected.studio_object_id}</p>
              <p className={styles.developerNote}>Blender name: {selected.name}</p>
            </details>
          ) : null}
        </dl>
      ) : (
        <p className={styles.emptyNote}>
          {scene && scene.objects.length > 0
            ? "Click something in the model to see its measurements, then ask Astra to change it."
            : "Nothing in the model yet."}
        </p>
      )}

      <h3 className={styles.sideSubtitle}>Project facts</h3>
      {facts.length === 0 ? (
        <p className={styles.emptyNote}>
          Astra records confirmed measurements here, like ceiling height, so it never
          asks twice.
        </p>
      ) : (
        <ul className={styles.factList}>
          {facts.map((fact) => (
            <FactRow key={fact.key} fact={fact} onSave={onSaveFact} />
          ))}
        </ul>
      )}
    </section>
  );
}

function FactRow({ fact, onSave }: { fact: FactView; onSave(key: string, value: string): void }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(fact.value);
  const inputId = `fact-${fact.key}`;

  if (!editing) {
    return (
      <li className={styles.factItem}>
        <span className={styles.factKey}>{humaniseKey(fact.key)}</span>
        <span className={styles.factValue}>{fact.value}</span>
        <button
          type="button"
          className={styles.linkButton}
          onClick={() => {
            setDraft(fact.value);
            setEditing(true);
          }}
        >
          Change
        </button>
      </li>
    );
  }

  return (
    <li className={styles.factItem}>
      <label className={styles.factKey} htmlFor={inputId}>
        {humaniseKey(fact.key)}
      </label>
      <input
        id={inputId}
        className={styles.factInput}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            onSave(fact.key, draft.trim());
            setEditing(false);
          }
          if (event.key === "Escape") setEditing(false);
        }}
        autoFocus
      />
      <button
        type="button"
        className={styles.linkButton}
        onClick={() => {
          onSave(fact.key, draft.trim());
          setEditing(false);
        }}
      >
        Save
      </button>
    </li>
  );
}

/** Strip the stable-id suffix Blender names carry, so "Wall_North" reads as written. */
export function friendlyName(name: string): string {
  return name.replace(/_obj_[0-9a-f]{4,}$/i, "").replace(/_/g, " ");
}

function friendlyType(object: SceneObjectView): string {
  const name = object.name.toLowerCase();
  if (name.includes("wall")) return "Wall";
  if (name.includes("floor")) return "Floor";
  if (name.includes("ceiling")) return "Ceiling";
  if (name.includes("door")) return "Door";
  if (name.includes("window")) return "Window";
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

function describeColour(colour: { r: number; g: number; b: number }): string {
  return cssColour(colour);
}

function humaniseKey(key: string): string {
  return titleCase(key.replace(/_m$/, "").replace(/_/g, " "));
}

function titleCase(value: string): string {
  const lower = value.toLowerCase().replace(/_/g, " ");
  return lower.charAt(0).toUpperCase() + lower.slice(1);
}
