/** Resolve Vite's `?url` import suffix, which esbuild does not know about. */
export const urlSuffixPlugin = {
  name: 'vite-url-suffix',
  setup(build) {
    build.onResolve({ filter: /\?url$/ }, (args) => ({
      path: args.path,
      namespace: 'vite-url',
    }))
    build.onLoad({ filter: /.*/, namespace: 'vite-url' }, (args) => ({
      contents: `export default ${JSON.stringify('/assets/' + args.path.split('/').pop().replace('?url', ''))}`,
      loader: 'js',
    }))
  },
}
