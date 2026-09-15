/**
 * Canonical schema access + a dependency-free JSON Schema validator.
 *
 * Direction of truth:
 *
 *   packages/contracts/schemas/*.schema.json   <-- CANONICAL (language-neutral)
 *            |
 *            +--> TypeScript representation (this package)
 *            +--> Python representation (studio_contracts.schema)
 *
 * The TypeScript interfaces in ./index.ts are a *representation* of the
 * canonical schemas, not the source of truth. Conformance tests load the schema
 * files at runtime and verify the representation still matches.
 *
 * This validator implements only the JSON Schema subset the canonical contracts
 * use, deliberately avoiding a code-generation framework or a validation
 * dependency at this milestone. Supported keywords:
 *   $ref (sibling file), type, enum, const, required, properties,
 *   additionalProperties, items, minLength, maxLength, pattern, minimum,
 *   maximum, format ("date-time"), allOf, anyOf, oneOf, if/then/else, not
 */

import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const SCHEMA_DIR = join(HERE, "..", "schemas");

export type JsonSchema = Record<string, unknown>;

const cache = new Map<string, JsonSchema>();

/** Load a canonical schema by file name, e.g. "vec3.schema.json". */
export function loadSchema(name: string): JsonSchema {
  const cached = cache.get(name);
  if (cached) return cached;
  const raw = readFileSync(join(SCHEMA_DIR, name), "utf8");
  const parsed = JSON.parse(raw) as JsonSchema;
  cache.set(name, parsed);
  return parsed;
}

/** All canonical schema file names present in the schema directory. */
export function listSchemaNames(): string[] {
  return readdirSync(SCHEMA_DIR)
    .filter((f) => f.endsWith(".schema.json"))
    .sort();
}

/** Read the canonical `enum` list for a schema (optionally at a property path). */
export function schemaEnum(name: string, propertyPath?: string[]): string[] {
  let node: JsonSchema = loadSchema(name);
  for (const key of propertyPath ?? []) {
    const props = node.properties as Record<string, JsonSchema> | undefined;
    if (!props || !props[key]) {
      throw new Error(`no property '${key}' in ${name}`);
    }
    node = resolve(props[key]);
  }
  const values = node.enum;
  if (!Array.isArray(values)) {
    throw new Error(`schema ${name} has no enum at ${(propertyPath ?? []).join(".")}`);
  }
  return values as string[];
}

/** Read the canonical `required` list for a schema. */
export function schemaRequired(name: string): string[] {
  const req = loadSchema(name).required;
  return Array.isArray(req) ? ([...req] as string[]).sort() : [];
}

/** Read the canonical property names for a schema. */
export function schemaProperties(name: string): string[] {
  const props = loadSchema(name).properties as Record<string, unknown> | undefined;
  return props ? Object.keys(props).sort() : [];
}

function resolve(schema: JsonSchema): JsonSchema {
  const ref = schema.$ref;
  if (typeof ref === "string") {
    return resolve(loadSchema(ref));
  }
  return schema;
}

export interface SchemaViolation {
  path: string;
  message: string;
}

const DATE_TIME =
  /^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$/;

function typeMatches(type: string, value: unknown): boolean {
  switch (type) {
    case "object":
      return value !== null && typeof value === "object" && !Array.isArray(value);
    case "array":
      return Array.isArray(value);
    case "string":
      return typeof value === "string";
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    case "integer":
      return typeof value === "number" && Number.isInteger(value);
    case "boolean":
      return typeof value === "boolean";
    case "null":
      return value === null;
    default:
      throw new Error(`unsupported type keyword: ${type}`);
  }
}

function deepEqual(a: unknown, b: unknown): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function check(
  schema: JsonSchema,
  value: unknown,
  path: string,
  out: SchemaViolation[],
): void {
  const s = resolve(schema);

  if (s.type !== undefined) {
    const types = Array.isArray(s.type) ? (s.type as string[]) : [s.type as string];
    if (!types.some((t) => typeMatches(t, value))) {
      out.push({ path, message: `expected type ${types.join("|")}` });
      return;
    }
  }

  if (Array.isArray(s.enum) && !s.enum.some((v) => deepEqual(v, value))) {
    out.push({ path, message: `value not in enum` });
  }

  if (s.const !== undefined && !deepEqual(s.const, value)) {
    out.push({ path, message: `value must equal const` });
  }

  if (typeof value === "string") {
    if (typeof s.minLength === "number" && value.length < s.minLength) {
      out.push({ path, message: `shorter than minLength ${s.minLength}` });
    }
    if (typeof s.maxLength === "number" && value.length > s.maxLength) {
      out.push({ path, message: `longer than maxLength ${s.maxLength}` });
    }
    if (typeof s.pattern === "string" && !new RegExp(s.pattern, "u").test(value)) {
      out.push({ path, message: `does not match pattern ${s.pattern}` });
    }
    if (s.format === "date-time" && !DATE_TIME.test(value)) {
      out.push({ path, message: `not an ISO-8601 date-time` });
    }
  }

  if (typeof value === "number") {
    if (typeof s.minimum === "number" && value < s.minimum) {
      out.push({ path, message: `below minimum ${s.minimum}` });
    }
    if (typeof s.maximum === "number" && value > s.maximum) {
      out.push({ path, message: `above maximum ${s.maximum}` });
    }
  }

  if (value !== null && typeof value === "object" && !Array.isArray(value)) {
    const obj = value as Record<string, unknown>;
    const props = (s.properties ?? {}) as Record<string, JsonSchema>;

    for (const key of (s.required as string[] | undefined) ?? []) {
      if (!(key in obj)) {
        out.push({ path: path ? `${path}.${key}` : key, message: `required` });
      }
    }

    for (const [key, sub] of Object.entries(props)) {
      if (key in obj) {
        check(sub, obj[key], path ? `${path}.${key}` : key, out);
      }
    }

    if (s.additionalProperties === false) {
      for (const key of Object.keys(obj)) {
        if (!(key in props)) {
          out.push({
            path: path ? `${path}.${key}` : key,
            message: `additional property not allowed`,
          });
        }
      }
    }
  }

  if (Array.isArray(value) && s.items !== undefined) {
    value.forEach((item, i) => {
      check(s.items as JsonSchema, item, `${path}[${i}]`, out);
    });
  }

  for (const sub of (s.allOf as JsonSchema[] | undefined) ?? []) {
    check(sub, value, path, out);
  }

  const anyOf = s.anyOf as JsonSchema[] | undefined;
  if (anyOf && !anyOf.some((sub) => isValid(sub, value))) {
    out.push({ path, message: `does not match anyOf` });
  }

  const oneOf = s.oneOf as JsonSchema[] | undefined;
  if (oneOf && oneOf.filter((sub) => isValid(sub, value)).length !== 1) {
    out.push({ path, message: `must match exactly one of oneOf` });
  }

  if (s.not !== undefined && isValid(s.not as JsonSchema, value)) {
    out.push({ path, message: `must not match 'not' schema` });
  }

  if (s.if !== undefined) {
    const branch = isValid(s.if as JsonSchema, value) ? s.then : s.else;
    if (branch !== undefined) {
      check(branch as JsonSchema, value, path, out);
    }
  }
}

function isValid(schema: JsonSchema, value: unknown): boolean {
  const out: SchemaViolation[] = [];
  check(schema, value, "", out);
  return out.length === 0;
}

/** Validate a value against a canonical schema file. */
export function validateAgainstSchema(
  schemaName: string,
  value: unknown,
): { valid: boolean; violations: SchemaViolation[] } {
  const violations: SchemaViolation[] = [];
  check(loadSchema(schemaName), value, "", violations);
  return { valid: violations.length === 0, violations };
}
