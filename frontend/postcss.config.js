/*
 * Vite picks this up automatically (no vite.config change needed).
 *
 * autoprefixer is not decoration here: the Play CDN applied vendor prefixes at
 * runtime, so dropping it would quietly change rendering in older browsers.
 */
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
};
