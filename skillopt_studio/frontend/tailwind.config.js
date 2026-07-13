/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Launchpad palette — mirrors agentcore_launchpad tokens.css 1:1.
        bg: "#0B0E0D", // page plane
        panel: "#141816", // card / panel surface (palette validated against this)
        panel2: "#191E1B", // raised surface
        line: "#232B27", // hairline
        line2: "#2E3833", // stronger borders (buttons, inputs)
        grid: "#212823", // table row hairlines
        well: "#0E1210", // input / search / code-well bg
        codebg: "#0A0D0C", // code blocks
        text: "#E9EDEA", // primary ink
        muted: "#A3ACA6", // secondary ink
        faint: "#69736C", // muted ink — labels/meta only, not body text
        amber: "#FFB000", // brand signal — chrome only, never a data series
        s1: "#3987E5", // series 1 blue / links / info
        s2: "#199E70", // series 2 aqua
        s3: "#C98500", // series 3 yellow
        s5: "#9085E9", // series 5 violet
        good: "#0CA30C",
        warn: "#FAB219",
        serious: "#EC835A",
        crit: "#D03B3B", // error fills/borders only
        critText: "#DD5252", // error TEXT on panel: 4.61:1 (AA)
      },
      fontFamily: {
        mono: [
          '"IBM Plex Mono"', "ui-monospace", "SFMono-Regular", "Menlo",
          '"Noto Sans Mono CJK SC"', "monospace",
        ],
        sans: [
          '"Archivo Variable"', '"Archivo"', "system-ui",
          '"PingFang SC"', '"Noto Sans CJK SC"', '"Microsoft YaHei"', "sans-serif",
        ],
      },
      boxShadow: {
        glow: "0 0 0 1px #FFB000, 0 12px 40px -18px rgba(255, 176, 0, 0.35)",
        card: "none",
      },
    },
  },
  plugins: [],
};
