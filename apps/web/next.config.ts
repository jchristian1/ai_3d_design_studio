import type { NextConfig } from "next";

/**
 * Next.js configuration for the Spec 001 browser interface.
 *
 * `transpilePackages` is required because the monorepo's shared packages are
 * published as raw TypeScript (`main: "src/index.ts"`), which Node runs natively
 * but a bundler must be told to compile. Reusing `@studio/types` is the point:
 * the browser and the backend then share ONE definition of the canonical
 * vocabulary instead of two that can drift. `@studio/spatial` is listed for the same
 * reason: unit and colour conversions in the browser must be the ones the backend
 * uses, never a second copy of the arithmetic. `@studio/validation` and
 * `@studio/contracts` appear because `@studio/spatial` imports them.
 */
const nextConfig: NextConfig = {
  transpilePackages: [
    "@studio/types",
    "@studio/spatial",
    "@studio/validation",
    "@studio/contracts",
  ],

  // The API is a separate origin in development (127.0.0.1:8000), reached with
  // CORS. No rewrite/proxy is configured on purpose: a proxy would hide the
  // real cross-origin setup that production will also have, and would make the
  // backend's CORS configuration untested locally.

  typescript: {
    // Type errors must fail the build. The point of TypeScript here is to catch
    // a contract mismatch with the API before it reaches a user.
    ignoreBuildErrors: false,
  },
};

export default nextConfig;
