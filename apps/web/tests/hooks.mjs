/**
 * Module hooks so `node --test` can load React components directly.
 *
 * TEST TOOLING ONLY. Two gaps have to be filled, and both are filled minimally:
 *
 *   1. **JSX.** Node's built-in TypeScript support strips type annotations but does
 *      not transform JSX, so a `.tsx` component cannot be imported. `tsx` (an
 *      esbuild-based loader) handles that, and is registered by `--import tsx`
 *      alongside these hooks.
 *
 *   2. **CSS modules.** A bundler resolves `styles.foo` to a hashed class name;
 *      Node cannot import a `.css` file at all. These hooks stub CSS imports with a
 *      Proxy that returns the property name, so `styles.panel` becomes `"panel"`.
 *      That is not a cosmetic shortcut: it keeps class names readable in the DOM,
 *      which is exactly what a test asserting structure wants.
 *
 * The alternative was a second test runner with its own config and its own
 * transform pipeline. One 40-line hook file keeps the whole repository on
 * `node --test`, which is worth more than the convenience.
 */

const CSS_PATTERN = /\.(css|scss|sass)$/;

/** Route CSS specifiers to a resolvable stub URL this loader recognises. */
export async function resolve(specifier, context, nextResolve) {
  if (CSS_PATTERN.test(specifier)) {
    return {
      url: new URL(specifier, context.parentURL ?? import.meta.url).href,
      format: "module",
      shortCircuit: true,
    };
  }
  return nextResolve(specifier, context);
}

/** Serve CSS imports as a class-name Proxy instead of reading the file. */
export async function load(url, context, nextLoad) {
  if (CSS_PATTERN.test(url)) {
    return {
      format: "module",
      shortCircuit: true,
      source: [
        "const styles = new Proxy(",
        "  {},",
        "  {",
        // Return the requested key, so `styles.panel` === "panel" and the
        // rendered markup carries meaningful class names.
        "    get: (_target, key) => (typeof key === 'string' ? key : undefined),",
        "  },",
        ");",
        "export default styles;",
      ].join("\n"),
    };
  }
  return nextLoad(url, context);
}
