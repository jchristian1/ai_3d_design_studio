/**
 * Registers the CSS-module hooks for `node --test`.
 *
 * Separate from hooks.mjs because module hooks must run on their own thread:
 * `register()` is what moves them there.
 */
import { register } from "node:module";

register("./hooks.mjs", import.meta.url);
