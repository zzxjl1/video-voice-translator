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
    './{components,services,utils,hooks,pages,contexts,features,store,net}/**/*.{js,ts,jsx,tsx}',
    // streamdown 的组件自带 Tailwind 类名，不扫它就只有裸 HTML 的样子
    './node_modules/streamdown/dist/*.js',
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
        /*
         * streamdown（AI 回复的 Markdown 渲染器）用 shadcn 的一套语义色名。
         * 这里把它们映射到本项目的暖色盘，而不是逐元素写覆盖样式 —— 组件内部
         * 有十几处 bg-muted / border-border / text-muted-foreground，逐个覆盖
         * 既做不到完备，也会和它自己的结构耦合。
         */
        background: '#FFFFFF',
        foreground: '#2D2D2D',
        border: '#E6E4DD',
        muted: '#F2F0E9',
        'muted-foreground': '#6B7280',
        primary: '#DA7756',
        sidebar: '#FBFAF7',
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
