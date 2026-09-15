/**
 * Deterministic colour interpretation — TypeScript representation.
 *
 * Spec 002, Task 2.
 *
 * Canonical colour: **linear sRGB, RGBA, finite floats in [0, 1]**.
 *
 * What "linear sRGB" means here, precisely:
 *
 * sRGB **primaries** and white point, with the sRGB **transfer function decoded**,
 * so each of R, G and B is a linear-light value. It is NOT an encoded/"gamma" sRGB
 * value of the kind written as `#E6D2B5` or `rgb(230 210 181)` in CSS. Those are
 * transfer-encoded; calling them linear is the exact ambiguity this module exists to
 * remove.
 *
 * Alpha is a plain unitless [0, 1] scalar. It carries no transfer function and is
 * never gamma transformed — decoding it would be a category error.
 *
 * Linear is canonical because it is what Blender's Principled BSDF `base_color`
 * expects, so no colour-space conversion happens at the Blender boundary — the same
 * reasoning that makes METRES and RADIANS canonical.
 *
 * Scope and deliberate non-goals:
 *   - Pure arithmetic and one small table. No Blender, no bpy, no I/O.
 *   - No prose parsing: `resolveNamedColor` matches a normalized NAME token, it does
 *     not read "a sort of warm sandy colour".
 *   - The named palette is a CONVENIENCE above the canonical numeric
 *     representation, for offline determinism and fallback interpretation. It is NOT
 *     a restriction: a real provider may propose any schema-valid explicit RGBA,
 *     which is why `validateMaterialColor` exists independently of the palette and is
 *     the function the mutation path will actually depend on.
 *
 * Canonical schema: material-color.schema.json
 */

import type { ChatError } from "@studio/contracts";
import type { MaterialColor } from "@studio/types";

/** Channel names, in canonical order. */
export const COLOR_CHANNELS: readonly string[] = ["r", "g", "b", "a"] as const;

/**
 * The three channels that carry a transfer function. Alpha is excluded on purpose:
 * it is a unitless coverage scalar, not a light intensity.
 */
export const TRANSFER_CHANNELS: readonly string[] = ["r", "g", "b"] as const;

// ---------------------------------------------------------------------------
// sRGB transfer function — the SINGLE conversion site
// ---------------------------------------------------------------------------

/** Piecewise threshold of the sRGB EOTF, on the ENCODED side. */
export const SRGB_LINEAR_THRESHOLD = 0.04045;

/** Slope of the near-black linear segment. */
export const SRGB_LINEAR_SLOPE = 12.92;

/** Offset and scale of the power segment. */
export const SRGB_ALPHA = 0.055;
export const SRGB_SCALE = 1.055;

/** Exponent of the power segment. */
export const SRGB_EXPONENT = 2.4;

/** Maximum value of an 8-bit channel, for the `#RRGGBB` helper. */
export const EIGHT_BIT_MAX = 255;

export const COLOR_MESSAGES = {
  NOT_A_COLOR: "colour must be an object with finite r, g, b and a channels in [0, 1]",
  MISSING_CHANNEL: "colour is missing one or more of the channels r, g, b, a",
  NON_FINITE_CHANNEL: "colour channels must be finite numbers",
  CHANNEL_OUT_OF_RANGE: "colour channels must be within [0, 1]",
  UNKNOWN_COLOR_NAME:
    "unknown colour name; the platform interprets only a small deterministic " +
    "palette, and any other colour must be supplied as explicit numeric " +
    "linear sRGB channels",
  ENCODED_OUT_OF_RANGE: "encoded sRGB channel must be within [0, 1]",
} as const;

function isRealNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Normalize negative zero to positive zero, as everywhere else. */
function normalizeZero(value: number): number {
  return value === 0 ? 0 : value;
}

/**
 * Decode ONE encoded sRGB channel in [0, 1] to linear light.
 *
 * The standard sRGB EOTF:
 *
 *     e <= 0.04045 :  e / 12.92
 *     otherwise    :  ((e + 0.055) / 1.055) ** 2.4
 *
 * THIS IS THE ONLY sRGB transfer conversion in the repository. Dividing an 8-bit
 * value by 255 and calling the result linear is wrong by roughly a factor of two in
 * the midtones (0.5 encoded is 0.214 linear, not 0.5), which is why the naive form
 * is forbidden by the source guard rather than merely discouraged.
 *
 * Returns `undefined` for a non-finite or out-of-range input rather than clamping:
 * silently clamping would turn a caller's mistake into a plausible colour.
 *
 * Note on cross-language parity: the power segment uses `pow`, which is not
 * guaranteed bit-identical across libm implementations. Observed identical between
 * Node and CPython on the development machine, and the parity corpus compares
 * transfer-function output within a documented tolerance rather than asserting bit
 * equality. The PALETTE itself does not depend on this: it is authored in canonical
 * linear literals (below), so palette lookups are exactly equal in both languages
 * regardless of `pow`.
 */
export function srgbEncodedChannelToLinear(encoded: unknown): number | undefined {
  if (!isRealNumber(encoded)) return undefined;
  if (encoded < 0 || encoded > 1) return undefined;
  if (encoded <= SRGB_LINEAR_THRESHOLD) {
    return encoded / SRGB_LINEAR_SLOPE;
  }
  return Math.pow((encoded + SRGB_ALPHA) / SRGB_SCALE, SRGB_EXPONENT);
}

/**
 * Encode ONE linear-light channel in [0, 1] back to sRGB encoding.
 *
 * The exact inverse of `srgbEncodedChannelToLinear`. Provided for diagnostics and
 * for the round-trip test that proves the pair really are inverses; nothing on the
 * mutation path needs it, because Blender wants linear.
 */
export function linearChannelToSrgbEncoded(linear: unknown): number | undefined {
  if (!isRealNumber(linear)) return undefined;
  if (linear < 0 || linear > 1) return undefined;
  if (linear <= SRGB_LINEAR_THRESHOLD / SRGB_LINEAR_SLOPE) {
    return linear * SRGB_LINEAR_SLOPE;
  }
  return SRGB_SCALE * Math.pow(linear, 1 / SRGB_EXPONENT) - SRGB_ALPHA;
}

/**
 * Decode one 8-bit encoded channel (0-255) to linear light.
 *
 * A thin convenience over `srgbEncodedChannelToLinear` used to DOCUMENT and TEST the
 * provenance of the palette below. It divides by 255 to reach the encoded [0, 1]
 * domain and then applies the transfer function — the division alone is explicitly
 * not the conversion.
 */
export function srgb8BitToLinear(value: unknown): number | undefined {
  if (!isRealNumber(value)) return undefined;
  return srgbEncodedChannelToLinear(value / EIGHT_BIT_MAX);
}

// ---------------------------------------------------------------------------
// Named palette — small, closed, authored in CANONICAL LINEAR values
// ---------------------------------------------------------------------------

/**
 * The encoded sRGB hex each palette entry was derived from. PROVENANCE ONLY: these
 * values are never used at runtime. Storing them makes the artistic choice
 * reviewable, and colors.test.ts asserts that decoding them reproduces the canonical
 * linear literals below, so the two forms cannot drift apart.
 *
 * The canonical representation is the linear one; this is documentation with a test
 * attached, not a second source of truth.
 */
export const PALETTE_PROVENANCE_HEX: Readonly<Record<string, string>> = {
  "warm beige": "#E6D2B5",
  beige: "#F5F5DC",
  white: "#FFFFFF",
  black: "#000000",
  grey: "#808080",
};

/**
 * Deliberately TINY deterministic vocabulary. Not a colour-name database: it exists
 * so offline tests and the rule-based fallback have stable colours, and it places no
 * limit on what a real provider may propose numerically.
 *
 * Values are canonical LINEAR sRGB with alpha 1.0 (fully opaque), written as
 * literals so both languages parse identical doubles.
 */
export const NAMED_COLORS: Readonly<Record<string, MaterialColor>> = {
  // #E6D2B5 — a warm, slightly deepened beige: clearly beige, clearly warmer than
  // CSS "beige", and stable forever because it is pinned by tests.
  "warm beige": {
    r: 0.7912979403326302,
    g: 0.6444796819705821,
    b: 0.4620769996544071,
    a: 1.0,
  },
  // #F5F5DC — CSS "beige", so the plain name matches the widely understood colour.
  beige: {
    r: 0.9130986517934192,
    g: 0.9130986517934192,
    b: 0.7156935005064807,
    a: 1.0,
  },
  white: { r: 1.0, g: 1.0, b: 1.0, a: 1.0 },
  black: { r: 0.0, g: 0.0, b: 0.0, a: 1.0 },
  // #808080 — mid grey by ENCODING, which is 0.2159 linear, not 0.5. The gap is the
  // whole reason the transfer function is not optional.
  grey: {
    r: 0.21586050011389926,
    g: 0.21586050011389926,
    b: 0.21586050011389926,
    a: 1.0,
  },
};

/**
 * Spelling aliases only, never new colours. "off white" is deliberately absent: it
 * is a different colour claim, not another way to spell one of these.
 */
export const COLOR_NAME_ALIASES: Readonly<Record<string, string>> = {
  gray: "grey",
};

export type ColorResult =
  | { ok: true; color: MaterialColor }
  | { ok: false; error: ChatError };

function invalid(
  message: string,
  code: ChatError["code"] = "VALIDATION_ERROR",
): ColorResult {
  return { ok: false, error: { code, message } };
}

/**
 * Normalize a colour NAME token: trim, lowercase, collapse inner whitespace.
 *
 * Token normalization, not prose parsing: "  Warm   Beige " resolves, "something
 * warm and beige-ish" does not.
 */
export function normalizeColorName(name: unknown): string | undefined {
  if (typeof name !== "string") return undefined;
  const collapsed = name.trim().split(/\s+/).join(" ").toLowerCase();
  if (collapsed === "") return undefined;
  return COLOR_NAME_ALIASES[collapsed] ?? collapsed;
}

/** True when `name` is in the deterministic palette. */
export function isNamedColor(name: unknown): boolean {
  const normalized = normalizeColorName(name);
  return normalized !== undefined && normalized in NAMED_COLORS;
}

/**
 * Resolve a palette name to its canonical linear sRGB colour.
 *
 * An unknown name is UNSUPPORTED_INSTRUCTION, not VALIDATION_ERROR: the request was
 * well formed, the platform simply cannot interpret that phrase deterministically.
 * Once a real provider exists it will usually have proposed explicit numbers
 * already, and beige-like phrasing must NOT be forced back into this palette.
 */
export function resolveNamedColor(name: unknown): ColorResult {
  const normalized = normalizeColorName(name);
  const found = normalized === undefined ? undefined : NAMED_COLORS[normalized];
  if (found === undefined) {
    return invalid(COLOR_MESSAGES.UNKNOWN_COLOR_NAME, "UNSUPPORTED_INSTRUCTION");
  }
  return { ok: true, color: found };
}

/**
 * Validate an ARBITRARY explicit canonical colour.
 *
 * This is the function the mutation path depends on, and it is deliberately
 * independent of the palette: a real model must be free to propose any schema-valid
 * explicit colour, and the platform must be able to accept it without that colour
 * having a name.
 *
 * Rejects a missing channel, a non-numeric or non-finite channel, and any channel
 * outside [0, 1]. Never clamps: a caller who sent 1.5 was wrong about the range, and
 * quietly turning that into white would hide the mistake.
 */
export function validateMaterialColor(value: unknown): ColorResult {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return invalid(COLOR_MESSAGES.NOT_A_COLOR);
  }
  const source = value as Record<string, unknown>;
  const unknown = Object.keys(source).filter(
    (key) => !(COLOR_CHANNELS as readonly string[]).includes(key),
  );
  if (unknown.length > 0) {
    return invalid(COLOR_MESSAGES.NOT_A_COLOR);
  }
  for (const key of COLOR_CHANNELS) {
    if (source[key] === undefined || source[key] === null) {
      return invalid(COLOR_MESSAGES.MISSING_CHANNEL);
    }
  }
  for (const key of COLOR_CHANNELS) {
    if (!isRealNumber(source[key])) {
      return invalid(COLOR_MESSAGES.NON_FINITE_CHANNEL);
    }
  }
  for (const key of COLOR_CHANNELS) {
    const channel = source[key] as number;
    if (channel < 0 || channel > 1) {
      return invalid(COLOR_MESSAGES.CHANNEL_OUT_OF_RANGE);
    }
  }
  return {
    ok: true,
    color: {
      r: normalizeZero(source.r as number),
      g: normalizeZero(source.g as number),
      b: normalizeZero(source.b as number),
      a: normalizeZero(source.a as number),
    },
  };
}

/**
 * Build a canonical colour from 8-bit ENCODED sRGB channels.
 *
 * The one supported way to bring a familiar `#RRGGBB` value into the canonical
 * representation, so no caller is tempted to divide by 255 and stop there. Alpha is
 * taken as an already-linear [0, 1] scalar and is NOT transfer-decoded.
 */
export function colorFromSrgb8Bit(
  r: unknown,
  g: unknown,
  b: unknown,
  a = 1.0,
): ColorResult {
  const decoded = [r, g, b].map(srgb8BitToLinear);
  if (decoded.some((channel) => channel === undefined)) {
    return invalid(COLOR_MESSAGES.ENCODED_OUT_OF_RANGE);
  }
  return validateMaterialColor({
    r: decoded[0],
    g: decoded[1],
    b: decoded[2],
    a,
  });
}
