"use client";

/**
 * The interactive 3D workspace: a GLB viewer with orbit, zoom, pan and click selection.
 *
 * Three behaviours worth naming, because each is a promise to the user:
 *
 * - **The previous model stays visible while the next one loads.** A reconstruction can
 *   take a minute; blanking the viewport for that long feels like a crash.
 * - **The camera survives a refresh.** After "make this wall taller" you should be
 *   looking at the same wall from the same angle, not thrown back to a default view.
 * - **Clicking an object selects it** by the stable `studio_object_id` that Blender's
 *   glTF exporter carries through as custom properties, so "make this taller" resolves
 *   to the thing you clicked rather than to a guess.
 *
 * Three.js is imported dynamically. It is a large dependency that needs a real WebGL
 * context, so loading it lazily keeps it out of the first paint and lets this component
 * degrade to an honest message where WebGL is unavailable (including under jsdom, which
 * is how the tests exercise the fallback rather than mocking it away).
 */

import { useCallback, useEffect, useRef, useState } from "react";

import styles from "./ModelViewer.module.css";

export interface ModelViewerProps {
  /** Absolute URL of the GLB to display, or null when nothing has been modelled. */
  modelUrl: string | null;
  /** Fallback still image, shown when the 3D view cannot run. */
  previewUrl?: string | null;
  selectedObjectId?: string | null;
  onSelect?: (objectId: string | null) => void;
  /** True while a change is being applied, so the viewer can dim the stale model. */
  busy?: boolean;
}

type ViewerStatus = "idle" | "loading" | "ready" | "unsupported" | "error";

interface SceneHandle {
  dispose(): void;
  load(url: string): Promise<void>;
  setSelection(objectId: string | null): void;
}

export function ModelViewer({
  modelUrl,
  previewUrl = null,
  selectedObjectId = null,
  onSelect,
  busy = false,
}: ModelViewerProps) {
  const mountRef = useRef<HTMLDivElement | null>(null);
  const handleRef = useRef<SceneHandle | null>(null);
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;

  const [status, setStatus] = useState<ViewerStatus>("idle");
  const [message, setMessage] = useState<string | null>(null);
  const [loadedUrl, setLoadedUrl] = useState<string | null>(null);

  // --- build the scene once ----------------------------------------------
  useEffect(() => {
    let cancelled = false;
    const mount = mountRef.current;
    if (!mount) return;

    async function build() {
      let handle: SceneHandle;
      try {
        handle = await createScene(mount!, (objectId) => selectRef.current?.(objectId));
      } catch (error) {
        if (cancelled) return;
        setStatus("unsupported");
        setMessage(
          error instanceof Error && error.message
            ? error.message
            : "This browser cannot show the interactive 3D view.",
        );
        return;
      }
      if (cancelled) {
        handle.dispose();
        return;
      }
      handleRef.current = handle;
      setStatus((current) => (current === "idle" ? "ready" : current));
    }

    void build();
    return () => {
      cancelled = true;
      handleRef.current?.dispose();
      handleRef.current = null;
    };
  }, []);

  // --- load a model whenever the URL changes -----------------------------
  useEffect(() => {
    if (!modelUrl || modelUrl === loadedUrl) return;
    const handle = handleRef.current;
    if (!handle) return;

    let cancelled = false;
    setStatus("loading");
    setMessage(null);

    handle
      .load(modelUrl)
      .then(() => {
        if (cancelled) return;
        setLoadedUrl(modelUrl);
        setStatus("ready");
      })
      .catch(() => {
        if (cancelled) return;
        // The previous model is deliberately left in place.
        setStatus("error");
        setMessage("The 3D model could not be loaded. Showing the last one that worked.");
      });

    return () => {
      cancelled = true;
    };
  }, [modelUrl, loadedUrl, status]);

  useEffect(() => {
    handleRef.current?.setSelection(selectedObjectId ?? null);
  }, [selectedObjectId, loadedUrl]);

  const clearSelection = useCallback(() => selectRef.current?.(null), []);

  const showFallback = status === "unsupported" || (!loadedUrl && status !== "loading");

  return (
    <section className={styles.panel} aria-labelledby="model-heading">
      <h2 id="model-heading" className="visuallyHidden">
        3D model
      </h2>

      <div
        ref={mountRef}
        className={`${styles.canvas} ${busy ? styles.dimmed : ""}`}
        data-testid="model-canvas"
      />

      {showFallback ? (
        <div className={styles.overlay} role="status">
          {status === "unsupported" ? (
            <>
              <p className={styles.overlayTitle}>Interactive 3D is unavailable</p>
              <p className={styles.overlayBody}>{message}</p>
              {previewUrl ? (
                <img
                  className={styles.fallbackImage}
                  src={previewUrl}
                  alt="Still preview of the current design"
                />
              ) : null}
            </>
          ) : (
            <>
              <p className={styles.overlayTitle}>No model yet</p>
              <p className={styles.overlayBody}>
                Upload a plan and ask Astra to build it, and your model appears here.
              </p>
              {previewUrl ? (
                <img
                  className={styles.fallbackImage}
                  src={previewUrl}
                  alt="Still preview of the current design"
                />
              ) : null}
            </>
          )}
        </div>
      ) : null}

      {status === "loading" ? (
        <p className={styles.badge} role="status">
          Loading the 3D model…
        </p>
      ) : null}

      {status === "error" && message ? (
        <p className={`${styles.badge} ${styles.warning}`} role="status">
          {message}
        </p>
      ) : null}

      {selectedObjectId ? (
        <button type="button" className={styles.clear} onClick={clearSelection}>
          Clear selection
        </button>
      ) : null}
    </section>
  );
}

/**
 * The size to render at.
 *
 * A viewer that renders at 0×0 is indistinguishable from a broken one, and the container's
 * size depends on CSS that lives somewhere else — which is exactly how a good model ended up
 * in an invisible canvas. So the size is taken from the mount if it has one, otherwise from
 * the nearest ancestor that does, and only then from a default. Belt and braces on purpose:
 * the CSS is also fixed, and a stylesheet test guards it, but neither should be the only
 * thing standing between the user and a blank screen.
 */
export function measureForTest(mount: HTMLElement): { width: number; height: number } {
  return measure(mount);
}

function measure(mount: HTMLElement): { width: number; height: number } {
  let element: HTMLElement | null = mount;
  for (let depth = 0; element && depth < 6; depth += 1) {
    const width = element.clientWidth;
    const height = element.clientHeight;
    if (width >= 2 && height >= 2) return { width, height };
    element = element.parentElement;
  }
  return { width: 800, height: 600 };
}

/**
 * Create the WebGL scene. Throws when Three.js or WebGL is unavailable, which is what
 * drives the honest fallback above.
 */
async function createScene(
  mount: HTMLDivElement,
  onSelect: (objectId: string | null) => void,
): Promise<SceneHandle> {
  const THREE = await import("three");
  const { OrbitControls } = await import("three/examples/jsm/controls/OrbitControls.js");
  const { GLTFLoader } = await import("three/examples/jsm/loaders/GLTFLoader.js");

  let renderer: import("three").WebGLRenderer;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  } catch {
    throw new Error("WebGL is not available in this browser.");
  }
  const initial = measure(mount);
  renderer.setPixelRatio(Math.min(globalThis.devicePixelRatio ?? 1, 2));
  renderer.setSize(initial.width, initial.height);
  mount.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x14161a);

  const camera = new THREE.PerspectiveCamera(
    50,
    initial.width / initial.height,
    0.05,
    500,
  );
  camera.position.set(8, -8, 6);

  // Blender is Z-up and the glTF exporter writes Y-up, so the viewer stays Y-up and
  // the camera is simply placed to give a familiar three-quarter architectural view.
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.target.set(0, 1, 0);

  scene.add(new THREE.HemisphereLight(0xffffff, 0x333844, 2.0));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(5, 10, 7);
  scene.add(sun);

  const grid = new THREE.GridHelper(40, 40, 0x2a2f36, 0x1e2227);
  scene.add(grid);

  let root: import("three").Object3D | null = null;
  let hasFramed = false;
  const originalMaterials = new Map<string, unknown>();
  const raycaster = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  const highlight = new THREE.MeshStandardMaterial({
    color: 0x4c9ffe,
    emissive: 0x1b4f8a,
    roughness: 0.4,
  });

  let running = true;
  function animate() {
    if (!running) return;
    controls.update();
    renderer.render(scene, camera);
    requestAnimationFrame(animate);
  }
  animate();

  const resize = () => {
    const { width, height } = measure(mount);
    // A zero measurement happens while the panel is being laid out, and while a collapsed
    // column animates. `measure` never returns zero, and the renderer is only resized when
    // the size actually changed, so a stream of observations costs nothing.
    const current = renderer.getSize(new THREE.Vector2());
    if (Math.abs(current.x - width) < 1 && Math.abs(current.y - height) < 1) return;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height);
  };
  const observer =
    typeof ResizeObserver === "undefined" ? null : new ResizeObserver(resize);
  observer?.observe(mount);
  globalThis.addEventListener?.("resize", resize);

  /** Find the studio id on a mesh or any ancestor: extras land on the object node. */
  function studioIdOf(object: import("three").Object3D | null): string | null {
    let current: import("three").Object3D | null = object;
    while (current) {
      const extras = current.userData as Record<string, unknown> | undefined;
      const id = extras?.["studio_object_id"];
      if (typeof id === "string" && id) return id;
      current = current.parent;
    }
    return null;
  }

  function onClick(event: MouseEvent) {
    if (!root) return;
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera(pointer, camera);
    const hits = raycaster.intersectObject(root, true);
    onSelect(hits.length ? studioIdOf(hits[0]!.object) : null);
  }
  renderer.domElement.addEventListener("click", onClick);

  /**
   * Free everything the GPU is holding for a material: the material itself and every
   * texture it references. Three.js does NOT dispose textures when a material is disposed,
   * so a viewer that reloads models (every reconstruction does) leaks texture memory until
   * the WebGL context is lost and the tab dies. This is the belt that stops that.
   */
  function disposeMaterial(material: unknown) {
    if (!material) return;
    // Never dispose the shared highlight material here — it is reused across loads and is
    // only released in the handle's dispose(). A selected mesh carries it as its material.
    if (material === highlight) return;
    const mat = material as Record<string, unknown> & { dispose?: () => void };
    for (const value of Object.values(mat)) {
      // A texture is any material property that owns a GPU resource with dispose().
      if (
        value &&
        typeof value === "object" &&
        (value as { isTexture?: boolean }).isTexture === true
      ) {
        (value as { dispose?: () => void }).dispose?.();
      }
    }
    mat.dispose?.();
  }

  function disposeRoot() {
    if (!root) return;
    scene.remove(root);
    root.traverse((child) => {
      const mesh = child as import("three").Mesh;
      mesh.geometry?.dispose?.();
      // Dispose the ORIGINAL material we recorded on load, not whatever is currently
      // assigned — a selected mesh has been swapped to the shared highlight material, and
      // disposing that here would break every future selection.
      const original = originalMaterials.get(mesh.uuid) ?? mesh.material;
      if (Array.isArray(original)) {
        for (const entry of original) disposeMaterial(entry);
      } else {
        disposeMaterial(original);
      }
    });
    root = null;
    originalMaterials.clear();
  }

  return {
    async load(url: string) {
      const loader = new GLTFLoader();
      const gltf = await loader.loadAsync(url);
      disposeRoot();
      root = gltf.scene;
      scene.add(root);

      root.traverse((child) => {
        const mesh = child as import("three").Mesh;
        if (mesh.isMesh) originalMaterials.set(mesh.uuid, mesh.material);
      });

      // Frame the model on FIRST load only, so a later refresh keeps the camera the
      // user had positioned.
      if (!hasFramed) {
        const box = new THREE.Box3().setFromObject(root);
        if (!box.isEmpty()) {
          const size = box.getSize(new THREE.Vector3());
          const centre = box.getCenter(new THREE.Vector3());
          const span = Math.max(size.x, size.y, size.z) || 1;
          controls.target.copy(centre);
          camera.position.set(centre.x + span, centre.y + span * 0.8, centre.z + span);
          camera.near = Math.max(span / 1000, 0.01);
          camera.far = span * 100;
          camera.updateProjectionMatrix();
        }
        hasFramed = true;
      }
      controls.update();
    },

    setSelection(objectId: string | null) {
      if (!root) return;
      root.traverse((child) => {
        const mesh = child as import("three").Mesh;
        if (!mesh.isMesh) return;
        const original = originalMaterials.get(mesh.uuid);
        const isSelected = objectId !== null && studioIdOf(mesh) === objectId;
        if (isSelected) {
          mesh.material = highlight;
        } else if (original) {
          mesh.material = original as import("three").Material;
        }
      });
    },

    dispose() {
      running = false;
      renderer.domElement.removeEventListener("click", onClick);
      observer?.disconnect();
      globalThis.removeEventListener?.("resize", resize);
      disposeRoot();
      controls.dispose();
      highlight.dispose();
      renderer.dispose();
      // dispose() frees GPU objects but leaves the WebGL context itself alive; browsers
      // cap the number of live contexts (~16) and silently drop the oldest. Forcing the
      // loss here releases it immediately so repeated mounts can't exhaust the cap.
      renderer.forceContextLoss?.();
      renderer.domElement.remove();
    },
  };
}
