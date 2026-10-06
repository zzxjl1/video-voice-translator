/** @type {import('tailwindcss').Config} */
export default {
  /*
   * Scanned at BUILD time to decide which utilities to emit. This is the whole
   * point of the move away from the Play CDN, which generated CSS on the fly by
   * watching the DOM: it shipped the entire engine (~400 KB) to every visitor,
   * restyled the page after first paint, and required the public internet to be
   * reachable before the app looked like anything.
   *
   * Anything not listed here has no styles. A new folder of components must be
   * added, or its classes will silently render unstyled.
   */
  content: [
    './index.html',
    './*.{ts,tsx}',
    './{components,services,utils,hooks,pages,contexts}/**/*.{js,ts,jsx,tsx}',
  ],
  theme: {
    // `extend`, not a replacement: the default palette and scales stay, which
    // matters because the app leans on stock utilities (gray-*, rounded-*).
    extend: {
      fontFamily: {
        /*
         * Replaces Tailwind's default sans stack outright, matching the inline
         * `tailwind.config` that used to sit in index.html. It is deliberately
         * not a longer fallback chain: the webfonts are loaded explicitly in
         * index.html, and any difference here changes every piece of text in
         * the app at once.
         */
        serif: ['Merriweather', 'serif'],
        sans: ['Inter', 'sans-serif'],
      },
      colors: {
        claude: {
          bg: '#FDFBF9',
          paper: '#F2F0E9',
          text: '#2D2D2D',
          accent: '#DA7756',
          accentHover: '#C56645',
          border: '#E6E4DD',
          charcoal: '#383838',
        },
      },
    },
  },
  plugins: [],
};
