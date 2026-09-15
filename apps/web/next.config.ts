import type { NextConfig } from "next";

/**
 * Next.js configuration for the Spec 001 browser interface.
 *
 * `transpilePackages` is required because the monorepo's shared packages are
 * published as raw TypeScript (`main: "src/index.ts"`), which Node runs natively
 * but a bundler must be told to compile. Reusing `@studio/types` is the point:
 * the browser and the backend then share ONE definition of the canonical
 * vocabulary instead of two that can drift.
 */
const nextConfig: NextConfig = {
  transpilePackages: ["@studio/types"],

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
