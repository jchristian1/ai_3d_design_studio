/**
 * Colour interpretation tests — TypeScript (Spec 002, Task 2).
 *
 * Mirrors test_colors.py: the transfer function is the real sRGB EOTF rather than a
 * division by 255, arbitrary explicit colours validate independently of the palette,
 * and the palette is small, closed, and authored in canonical linear literals whose
 * encoded provenance is asserted to decode back to them.
 */

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  COLOR_CHANNELS,
  COLOR_MESSAGES,
  COLOR_NAME_ALIASES,
  EIGHT_BIT_MAX,
  NAMED_COLORS,
  PALETTE_PROVENANCE_HEX,
  SRGB_LINEAR_SLOPE,
  SRGB_LINEAR_THRESHOLD,
  TRANSFER_CHANNELS,
  colorFromSrgb8Bit,
  isNamedColor,
  linearChannelToSrgbEncoded,
  normalizeColorName,
  resolveNamedColor,
  srgb8BitToLinear,
  srgbEncodedChannelToLinear,
  validateMaterialColor,
} from "./index.ts";
import { decodeColor, decodeValue, loadSpatialCases } from "./test-support.ts";
import { SCHEMA_FILES, validateAgainstSchema } from "@studio/contracts";
import type { MaterialColor } from "@studio/types";

const CASES = loadSpatialCases();

/**
 * Tolerance for anything through the power segment. Documented rather than tuned:
 * `pow` is not guaranteed bit-identical across libm implementations, so the contract
 * is "agrees to well within any perceptible difference", not "same final bit".
 */
const TRANSFER_TOLERANCE = 1e-12;

function hexTo8Bit(value: string): [number, number, number] {
  const text = value.replace("#", "");
  return [
    parseInt(text.slice(0, 2), 16),
    parseInt(text.slice(2, 4), 16),
    parseInt(text.slice(4, 6), 16),
  ];
}

function close(actual: number | undefined, expected: number, tolerance: number): void {
  assert.ok(actual !== undefined, "expected a value");
  assert.ok(
    Math.abs(actual - expected) <= tolerance,
    `${actual} !~ ${expected} (tolerance ${tolerance})`,
  );
}

// ---------------------------------------------------------------------------
// The sRGB transfer function
// ---------------------------------------------------------------------------

test("zero encoded is zero linear", () => {
  assert.equal(srgbEncodedChannelToLinear(0), 0);
});

test("one encoded is one linear", () => {
  close(srgbEncodedChannelToLinear(1), 1, 1e-15);
});

test("the known midtone vector: 0.5 encoded is NOT 0.5 linear", () => {
  const linear = srgbEncodedChannelToLinear(0.5);
  close(linear, 0.21404114048223255, TRANSFER_TOLERANCE);
  assert.ok((linear as number) < 0.25);
});

test("8-bit mid grey is not half linear", () => {
  const naive = 128 / EIGHT_BIT_MAX;
  const correct = srgb8BitToLinear(128);
  close(correct, 0.21586050011389926, TRANSFER_TOLERANCE);
  assert.ok(Math.abs(naive - (correct as number)) > 0.28);
});

test("the transfer function is monotonic", () => {
  let previous = -1;
  for (let step = 0; step <= 1000; step += 1) {
    const linear = srgbEncodedChannelToLinear(step / 1000);
    assert.ok(linear !== undefined);
    assert.ok(linear > previous, `not monotonic at ${step / 1000}`);
    previous = linear;
  }
});

test("the piecewise segments meet at the threshold", () => {
  // The standard's published constants do not join perfectly: the step is ~2.3e-9,
  // four orders of magnitude below an 8-bit quantum. Pinned so a real mistake in the
  // constants would still fail.
  const below = srgbEncodedChannelToLinear(SRGB_LINEAR_THRESHOLD - 1e-12) as number;
  const at = srgbEncodedChannelToLinear(SRGB_LINEAR_THRESHOLD) as number;
  const above = srgbEncodedChannelToLinear(SRGB_LINEAR_THRESHOLD + 1e-12) as number;
  close(at, SRGB_LINEAR_THRESHOLD / SRGB_LINEAR_SLOPE, 1e-15);
  assert.ok(below <= at && at <= above);
  assert.ok(above - below < 1e-8);
});

for (const encoded of [0, 0.01, 0.04045, 0.2, 0.5, 0.8, 0.9, 1]) {
  test(`encode/decode round-trips ${encoded}`, () => {
    const linear = srgbEncodedChannelToLinear(encoded);
    close(linearChannelToSrgbEncoded(linear), encoded, 1e-12);
  });
}

for (const encoded of [1.5, -0.1, NaN, Infinity, "0.5", null, true, [0.5]]) {
  test(`encoded ${JSON.stringify(encoded)} is refused`, () => {
    assert.equal(srgbEncodedChannelToLinear(encoded), undefined);
    assert.equal(linearChannelToSrgbEncoded(encoded), undefined);
  });
}

// ---------------------------------------------------------------------------
// Palette
// ---------------------------------------------------------------------------

test("the palette is small and closed", () => {
  assert.deepEqual(Object.keys(NAMED_COLORS).sort(), [
    "beige",
    "black",
    "grey",
    "warm beige",
    "white",
  ]);
  assert.ok(Object.keys(NAMED_COLORS).length <= 8);
});

test("warm beige is the documented stable value", () => {
  assert.deepEqual(NAMED_COLORS["warm beige"], {
    r: 0.7912979403326302,
    g: 0.6444796819705821,
    b: 0.4620769996544071,
    a: 1.0,
  });
});

test("warm beige is visibly beige and warm", () => {
  const color = NAMED_COLORS["warm beige"] as MaterialColor;
  assert.ok(color.r > color.g && color.g > color.b, "warm beige must be warm");
  assert.ok(color.b > 0.25, "must not be dark");
  assert.ok(color.r < 1, "must not be blown out to white");
  assert.equal(color.a, 1);
});

test("warm beige is warmer and deeper than plain beige", () => {
  const warm = NAMED_COLORS["warm beige"] as MaterialColor;
  const plain = NAMED_COLORS.beige as MaterialColor;
  assert.ok(warm.r - warm.b > plain.r - plain.b);
  assert.ok(warm.r < plain.r);
});

for (const [name, hex] of Object.entries(PALETTE_PROVENANCE_HEX)) {
  test(`palette provenance decodes to the linear literal: ${name}`, () => {
    const eightBit = hexTo8Bit(hex);
    const expected = NAMED_COLORS[name] as MaterialColor;
    TRANSFER_CHANNELS.forEach((channel, index) => {
      close(
        srgb8BitToLinear(eightBit[index]),
        expected[channel as "r" | "g" | "b"],
        TRANSFER_TOLERANCE,
      );
    });
  });
}

test("every palette entry has provenance", () => {
  assert.deepEqual(
    Object.keys(PALETTE_PROVENANCE_HEX).sort(),
    Object.keys(NAMED_COLORS).sort(),
  );
});

test("every palette entry is a valid canonical colour", () => {
  for (const [name, color] of Object.entries(NAMED_COLORS)) {
    assert.ok(validateMaterialColor(color).ok, name);
    assert.ok(validateAgainstSchema(SCHEMA_FILES.MaterialColor, color).valid, name);
  }
});

test("palette alpha is never transfer-decoded", () => {
  for (const color of Object.values(NAMED_COLORS)) {
    assert.equal(color.a, 1);
  }
});

for (const [raw, expected] of [
  ["warm beige", "warm beige"],
  ["Warm Beige", "warm beige"],
  ["  WARM   BEIGE  ", "warm beige"],
  ["gray", "grey"],
  ["GRAY", "grey"],
  ["beige", "beige"],
] as [string, string][]) {
  test(`name token '${raw}' normalizes to '${expected}'`, () => {
    assert.equal(normalizeColorName(raw), expected);
    assert.ok(isNamedColor(raw));
    const result = resolveNamedColor(raw);
    assert.ok(result.ok);
    assert.deepEqual(result.color, NAMED_COLORS[expected]);
  });
}

test("aliases are spellings, not new colours", () => {
  for (const [alias, target] of Object.entries(COLOR_NAME_ALIASES)) {
    assert.ok(target in NAMED_COLORS);
    assert.ok(!(alias in NAMED_COLORS));
  }
});

for (const name of [
  "burnt sienna",
  "a sort of warm beige-ish tone",
  "beigey",
  "#E6D2B5",
  "",
  "   ",
  null,
  7,
  ["beige"],
]) {
  test(`unknown name ${JSON.stringify(name)} is unsupported, not guessed`, () => {
    assert.equal(isNamedColor(name), false);
    const result = resolveNamedColor(name);
    assert.ok(!result.ok);
    assert.equal(result.error.code, "UNSUPPORTED_INSTRUCTION");
    assert.equal(result.error.message, COLOR_MESSAGES.UNKNOWN_COLOR_NAME);
  });
}

test("the palette does not restrict what a provider may propose", () => {
  const result = validateMaterialColor({ r: 0.61, g: 0.42, b: 0.13, a: 1 });
  assert.ok(result.ok);
});

// ---------------------------------------------------------------------------
// Arbitrary explicit colour validation
// ---------------------------------------------------------------------------

for (const color of [
  { r: 0, g: 0, b: 0, a: 0 },
  { r: 1, g: 1, b: 1, a: 1 },
  { r: 0, g: 1, b: 0, a: 1 },
  { r: 0.5, g: 0.25, b: 0.125, a: 0.75 },
]) {
  test(`valid explicit colour ${JSON.stringify(color)}`, () => {
    assert.ok(validateMaterialColor(color).ok);
    assert.ok(validateAgainstSchema(SCHEMA_FILES.MaterialColor, color).valid);
  });
}

for (const channel of COLOR_CHANNELS) {
  for (const value of [-0.0001, 1.0001, 2, -1]) {
    test(`channel ${channel}=${value} is out of range`, () => {
      const color = { r: 0.5, g: 0.5, b: 0.5, a: 1, [channel]: value };
      const result = validateMaterialColor(color);
      assert.ok(!result.ok);
      assert.equal(result.error.message, COLOR_MESSAGES.CHANNEL_OUT_OF_RANGE);
      assert.equal(
        validateAgainstSchema(SCHEMA_FILES.MaterialColor, color).valid,
        false,
      );
    });
  }
  for (const value of [NaN, Infinity, -Infinity]) {
    test(`channel ${channel}=${value} is not finite`, () => {
      const result = validateMaterialColor({
        r: 0.5,
        g: 0.5,
        b: 0.5,
        a: 1,
        [channel]: value,
      });
      assert.ok(!result.ok);
      assert.equal(result.error.message, COLOR_MESSAGES.NON_FINITE_CHANNEL);
    });
  }
  test(`missing channel ${channel} is refused`, () => {
    const color: Record<string, number> = { r: 0.5, g: 0.5, b: 0.5, a: 1 };
    delete color[channel];
    const result = validateMaterialColor(color);
    assert.ok(!result.ok);
    assert.equal(result.error.message, COLOR_MESSAGES.MISSING_CHANNEL);
  });
}

for (const value of [
  "warm beige",
  [1, 1, 1, 1],
  null,
  7,
  { r: 1, g: 1, b: 1, a: 1, space: "srgb" },
  { red: 1, green: 1, blue: 1, alpha: 1 },
]) {
  test(`non-colour ${JSON.stringify(value)} is refused`, () => {
    const result = validateMaterialColor(value);
    assert.ok(!result.ok);
    assert.equal(result.error.code, "VALIDATION_ERROR");
  });
}

test("negative zero channels are normalized", () => {
  const result = validateMaterialColor({ r: -0, g: 0, b: 0, a: 1 });
  assert.ok(result.ok);
  assert.ok(!Object.is(result.color.r, -0));
});

// ---------------------------------------------------------------------------
// 8-bit convenience
// ---------------------------------------------------------------------------

test("colorFromSrgb8Bit matches the palette", () => {
  const [r, g, b] = hexTo8Bit(PALETTE_PROVENANCE_HEX["warm beige"] as string);
  const result = colorFromSrgb8Bit(r, g, b);
  assert.ok(result.ok);
  const expected = NAMED_COLORS["warm beige"] as MaterialColor;
  for (const channel of TRANSFER_CHANNELS) {
    close(
      result.color[channel as "r" | "g" | "b"],
      expected[channel as "r" | "g" | "b"],
      TRANSFER_TOLERANCE,
    );
  }
});

test("colorFromSrgb8Bit does not decode alpha", () => {
  const result = colorFromSrgb8Bit(255, 255, 255, 0.5);
  assert.ok(result.ok);
  assert.equal(result.color.a, 0.5);
});

for (const bad of [-1, 256, NaN, "128", null]) {
  test(`colorFromSrgb8Bit refuses ${JSON.stringify(bad)}`, () => {
    assert.ok(!colorFromSrgb8Bit(bad, 0, 0).ok);
  });
}

test("colour conversion is deterministic", () => {
  const run = () => {
    const out: (number | undefined)[] = [];
    for (let i = 0; i < 256; i += 1) out.push(srgbEncodedChannelToLinear(i / 255));
    return out;
  };
  const first = run();
  for (let i = 0; i < 5; i += 1) assert.deepEqual(run(), first);
});

// ---------------------------------------------------------------------------
// Shared corpus
// ---------------------------------------------------------------------------

for (const c of CASES.colors.valid) {
  test(`[corpus] colour valid: ${c.name}`, () => {
    assert.ok(validateMaterialColor(decodeColor(c.color)).ok);
  });
}

for (const c of CASES.colors.invalid) {
  test(`[corpus] colour invalid: ${c.name}`, () => {
    const result = validateMaterialColor(decodeColor(c.color));
    assert.ok(!result.ok);
    assert.equal(result.error.code, c.error_code);
    assert.equal(
      result.error.message,
      COLOR_MESSAGES[c.error_key as keyof typeof COLOR_MESSAGES],
    );
  });
}

for (const c of CASES.colors.named) {
  test(`[corpus] named colour: ${c.name}`, () => {
    const result = resolveNamedColor(c.input);
    assert.ok(result.ok);
    for (const channel of COLOR_CHANNELS) {
      close(
        result.color[channel as keyof MaterialColor],
        c.color[channel] as number,
        TRANSFER_TOLERANCE,
      );
    }
  });
}

for (const c of CASES.colors.unnamed) {
  test(`[corpus] unknown name: ${c.name}`, () => {
    const result = resolveNamedColor(decodeValue(c.input));
    assert.ok(!result.ok);
    assert.equal(result.error.code, c.error_code);
    assert.equal(
      result.error.message,
      COLOR_MESSAGES[c.error_key as keyof typeof COLOR_MESSAGES],
    );
  });
}

for (const c of CASES.colors.transfer.cases) {
  test(`[corpus] transfer vector: ${c.name}`, () => {
    close(
      srgbEncodedChannelToLinear(c.encoded),
      c.linear as number,
      CASES.colors.transfer.tolerance,
    );
  });
}

for (const c of CASES.colors.transfer.rejected) {
  test(`[corpus] transfer refused: ${c.name}`, () => {
    assert.equal(srgbEncodedChannelToLinear(decodeValue(c.encoded)), undefined);
  });
}
