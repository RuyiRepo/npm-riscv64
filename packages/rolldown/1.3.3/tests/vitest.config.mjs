// vitest configuration for upstream's own tests (see tests/package.json).
//
// The package under test is left external to the runner. Loading it through the
// runner's SSR transform replaces import.meta.resolve with a shim, and one of
// upstream's cases (report mode keeps parallel plugin calls that the filter of any
// worker accepts) calls import.meta.resolve through the wrapper's parallel-plugin
// loader. External means both the module graph and that call go through node,
// exactly as they do for a consumer who ran `npm i rolldown`.
//
// The regexes match the package ids only, not the path of the checkout: an
// `external: [/rolldown/]` would also match the test files themselves and try to
// load their TypeScript helpers without the transform.
export default {
  resolve: { preserveSymlinks: true },
  test: {
    include: ["**/*.test.js"],
    testTimeout: 60000,
    hookTimeout: 60000,
    server: { deps: { external: [/^(rolldown|@rolldown\/)/] } },
  },
};
