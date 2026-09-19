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
 * A fourth promise, added with the material library: **a material looks the same here as
 * in the still preview.** Both use the Khronos PBR Neutral transform, the same exposure,
 * and a key and fill light from the same elevation and azimuth. Two views of one design
 * that are graded differently make the user doubt what they are looking at — they cannot
 * tell a changed material from a changed renderer.
 *
 * The backdrop is deliberately NOT matched: the preview shows a sky, this shows a neutral
 * studio dark, because what is behind the model should not compete with it.
 *
 * Three.js is imported dynamically. It is a large dependency that needs a real WebGL
 * context, so loading it lazily keeps it out of the first paint and lets this component
 * degrade to an honest message where WebGL is unavailable (including under jsdom, which
 * is how the tests exercise the fallback rather than mocking it away).
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { degreesToRadians } from "@studio/spatial";

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
 * Lighting and grading constants, kept numerically in step with the server preview
 * (`services/preview/studio_preview/blender_scripts/render_preview.py`).
 *
 * These are not independently chosen values. They were calibrated on the server by
 * rendering known textures and comparing against their real albedo, and the whole
 * point of repeating them here is that the browser and the still image agree. If the
 * preview's lighting is retuned, these move with it.
 */
const SUN_ELEVATION_DEGREES = 38;
const SUN_AZIMUTH_DEGREES = -125;
const FILL_ELEVATION_DEGREES = 20;

/** Blender sun strength is irradiance; three's is unitless, so these are matched by eye
 *  to the same key/fill RATIO (3.0 : 1.0) rather than copied as numbers. */
const SUN_INTENSITY = 2.4;
const FILL_INTENSITY = 0.8;

/** The preview's -0.5 stops of exposure, as a linear multiplier: 2 ** -0.5. */
const EXPOSURE_MULTIPLIER = 0.7071;

/** Neutral grey used for image-based lighting, matching the preview's ambient. */
const AMBIENT_GREY = 0x808080;

/**
 * Degrees to radians through the shared converter.
 *
 * These are compile-time constants describing a light rig, so an invalid one would be
 * a programming error rather than bad input; falling back to 0 keeps the viewer
 * rendering instead of throwing inside scene construction.
 */
function radiansOf(degrees: number): number {
  const result = degreesToRadians(degrees);
  return result.ok ? result.radians : 0;
}

/**
 * A unit vector pointing at a light placed at the given elevation and azimuth.
 *
 * Converts from the server's Z-up convention to the viewer's Y-up one, so a single
 * pair of angles describes the same sun in both. Getting this wrong is subtle and
 * ugly: the model is lit correctly but from the wrong side, and the preview and the
 * 3D view disagree about where the shadows fall.
 */
function lightDirection(
  THREE: typeof import("three"),
  elevationDegrees: number,
  azimuthDegrees: number,
): import("three").Vector3 {
  // Converted through the shared implementation, never with a local `* Math.PI / 180`.
  // One conversion site keeps the browser bit-identical to the backend, and a guard
  // test enforces it — see packages/spatial/src/conversion-site-guard.test.ts.
  const elevation = radiansOf(elevationDegrees);
  const azimuth = radiansOf(azimuthDegrees);
  // Blender: x = cos(e)cos(a), y = cos(e)sin(a), z = sin(e), Z up.
  // Three:   x = same,         y = up = Blender z, z = -Blender y.
  return new THREE.Vector3(
    Math.cos(elevation) * Math.cos(azimuth),
    Math.sin(elevation),
    -Math.cos(elevation) * Math.sin(azimuth),
  ).multiplyScalar(60);
}

/**
 * A neutral dark backdrop with a subtle vertical gradient.
 *
 * This was briefly a blue sky gradient, to match the sky in the server preview. That
 * was the wrong call and it is worth recording why, because the reasoning looks
 * convincing right up until you see it: a pale sky behind a grey-and-white building
 * destroys the contrast you need to read the model, and it makes the reference grid
 * far louder than it was designed to be.
 *
 * The thing that genuinely has to match the preview is TONE MAPPING and light
 * direction, because those decide whether a material looks the same in both views.
 * The backdrop decides nothing — it is behind the model. So the viewer gets a studio
 * backdrop, and the preview keeps its sky.
 *
 * Nearly black, very slightly cool, lighter towards the bottom so the model sits in a
 * space rather than floating in a void. It only ever reaches `scene.background`;
 * lighting comes from a separate neutral environment, so these numbers cannot tint a
 * single surface.
 *
 * Deliberately generated rather than loaded: an image would be a download and an asset
 * to licence, for something the user only sees behind the model. Two pixels wide is
 * enough — it is stretched across the whole sphere and only varies vertically.
 */
function backdropTexture(
  THREE: typeof import("three"),
): import("three").DataTexture {
  const height = 64;
  const width = 2;
  const data = new Uint8Array(width * height * 4);

  // Close to the flat `0x14161a` this replaced, so the panel looks familiar; the
  // gradient is the only addition.
  const zenith = { r: 0x0e, g: 0x10, b: 0x13 };
  const horizon = { r: 0x20, g: 0x23, b: 0x28 };

  for (let row = 0; row < height; row += 1) {
    const t = row / (height - 1);
    const r = Math.round(zenith.r + (horizon.r - zenith.r) * t);
    const g = Math.round(zenith.g + (horizon.g - zenith.g) * t);
    const b = Math.round(zenith.b + (horizon.b - zenith.b) * t);
    for (let column = 0; column < width; column += 1) {
      const index = (row * width + column) * 4;
      data[index] = r;
      data[index + 1] = g;
      data[index + 2] = b;
      data[index + 3] = 255;
    }
  }

  const texture = new THREE.DataTexture(data, width, height);
  texture.mapping = THREE.EquirectangularReflectionMapping;
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.minFilter = THREE.LinearFilter;
  texture.magFilter = THREE.LinearFilter;
  texture.needsUpdate = true;
  return texture;
}

/**
 * A neutral environment map for image-based lighting.
 *
 * Built by prefiltering a scene whose background is flat grey, which gives materials
 * something to reflect and softens shading everywhere — without it, metal and
 * polished surfaces reflect nothing and read as flat paint. Neutral rather than sky
 * blue for the same reason the server's lighting is neutral: a white wall lit by a
 * blue sky renders blue, and the user cannot judge the material they chose.
 */
function neutralEnvironment(
  THREE: typeof import("three"),
  renderer: import("three").WebGLRenderer,
): { texture: import("three").Texture; dispose: () => void } {
  const generator = new THREE.PMREMGenerator(renderer);
  const source = new THREE.Scene();
  source.background = new THREE.Color(AMBIENT_GREY);
  const target = generator.fromScene(source);
  // The generator holds its own GPU resources and is not needed once the map exists.
  generator.dispose();
  return {
    texture: target.texture,
    dispose: () => target.dispose(),
  };
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

  // Tone mapping, matched DELIBERATELY to the server preview.
  //
  // The still preview renders through Blender's "Khronos PBR Neutral" transform;
  // three's NeutralToneMapping is that same Khronos transform. Using it here means
  // the PNG the user sees and the model they orbit are graded identically — without
  // this, one design looks like two, and the user cannot tell whether a material
  // changed or only the view did. The exposure is the preview's -0.5 stops
  // expressed as a linear multiplier.
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = EXPOSURE_MULTIPLIER;

  // Soft shadows. A model with no contact shadows reads as floating cardboard, and
  // shadows are most of what makes geometry look like it has mass.
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;

  mount.appendChild(renderer.domElement);

  const scene = new THREE.Scene();

  // Background and lighting come from DIFFERENT sources, the same separation the
  // preview makes with its world split. Here the backdrop is a neutral studio dark
  // and the lighting is a neutral environment, so nothing behind the model can tint
  // what is in front of it.
  const backgroundTexture = backdropTexture(THREE);
  scene.background = backgroundTexture;

  const environment = neutralEnvironment(THREE, renderer);
  scene.environment = environment.texture;

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

  // Key light and fill, placed from the SAME elevation and azimuth the server
  // preview uses, so the light falls across the model from the same side in both
  // views. Y is up here and Z is up in Blender, hence the axis swap.
  const sun = new THREE.DirectionalLight(0xffffff, SUN_INTENSITY);
  sun.position.copy(lightDirection(THREE, SUN_ELEVATION_DEGREES, SUN_AZIMUTH_DEGREES));
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  // Shadow acne on large flat surfaces (every floor) comes from depth precision;
  // a small normal bias is the cheap, artefact-free fix.
  sun.shadow.normalBias = 0.02;
  scene.add(sun);
  scene.add(sun.target);

  // Shadowless fill from the opposite side: the bounce card. Without it, faces
  // turned away from the sun go to flat silhouette.
  const fill = new THREE.DirectionalLight(0xffffff, FILL_INTENSITY);
  fill.position.copy(
    lightDirection(THREE, FILL_ELEVATION_DEGREES, SUN_AZIMUTH_DEGREES + 180),
  );
  scene.add(fill);

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

      const maxAnisotropy = renderer.capabilities.getMaxAnisotropy();

      root.traverse((child) => {
        const mesh = child as import("three").Mesh;
        if (!mesh.isMesh) return;
        originalMaterials.set(mesh.uuid, mesh.material);

        // Every surface both casts and receives: a wall shades the floor, and the
        // floor takes the shadow. Marking only one would lose half the effect.
        mesh.castShadow = true;
        mesh.receiveShadow = true;

        // Anisotropic filtering. A tiled floor is viewed at a grazing angle almost
        // by definition, and that is exactly where isotropic mipmaps smear the
        // texture into grey mush — the planks simply vanish into the distance.
        for (const material of Array.isArray(mesh.material)
          ? mesh.material
          : [mesh.material]) {
          const record = material as unknown as Record<string, unknown>;
          for (const value of Object.values(record)) {
            const texture = value as { isTexture?: boolean; anisotropy?: number } | null;
            if (texture && texture.isTexture === true) {
              texture.anisotropy = maxAnisotropy;
            }
          }
        }
      });

      const box = new THREE.Box3().setFromObject(root);

      // Fit the shadow camera to the model. A directional light's shadow is rendered
      // through an orthographic frustum, and the default one is a few units across:
      // anything larger than a small object falls outside it and simply has no
      // shadow, which looks like the feature is broken rather than mis-sized.
      if (!box.isEmpty()) {
        const size = box.getSize(new THREE.Vector3());
        const centre = box.getCenter(new THREE.Vector3());
        const radius = Math.max(size.x, size.y, size.z) * 0.75 || 1;

        sun.target.position.copy(centre);
        sun.position
          .copy(lightDirection(THREE, SUN_ELEVATION_DEGREES, SUN_AZIMUTH_DEGREES))
          .normalize()
          .multiplyScalar(radius * 4)
          .add(centre);

        const shadowCamera = sun.shadow.camera;
        shadowCamera.left = -radius * 1.6;
        shadowCamera.right = radius * 1.6;
        shadowCamera.top = radius * 1.6;
        shadowCamera.bottom = -radius * 1.6;
        shadowCamera.near = 0.05;
        shadowCamera.far = radius * 10;
        shadowCamera.updateProjectionMatrix();

        fill.position
          .copy(
            lightDirection(THREE, FILL_ELEVATION_DEGREES, SUN_AZIMUTH_DEGREES + 180),
          )
          .normalize()
          .multiplyScalar(radius * 4)
          .add(centre);
      }

      // Frame the model on FIRST load only, so a later refresh keeps the camera the
      // user had positioned.
      if (!hasFramed) {
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
      // The environment render target and the background texture are GPU resources
      // the scene owns rather than the model, so disposeRoot() never touches them.
      scene.environment = null;
      scene.background = null;
      environment.dispose();
      backgroundTexture.dispose();
      renderer.dispose();
      // dispose() frees GPU objects but leaves the WebGL context itself alive; browsers
      // cap the number of live contexts (~16) and silently drop the oldest. Forcing the
      // loss here releases it immediately so repeated mounts can't exhaust the cap.
      renderer.forceContextLoss?.();
      renderer.domElement.remove();
    },
  };
}
