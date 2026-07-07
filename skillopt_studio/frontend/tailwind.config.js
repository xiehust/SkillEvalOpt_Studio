/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // SkillOpt PPT palette — single source of truth for the studio theme
        bg: "#0E1524",
        panel: "#18212F",
        panel2: "#1E2938",
        line: "#2A3647",
        green: "#A6DB4C",
        cyan: "#56C7D6",
        amber: "#F0B43C",
        red: "#E86A50",
        purple: "#A98BE0",
        text: "#EAF0F7",
        muted: "#94A3B7",
      },
      fontFamily: {
        mono: [
          '"IBM Plex Mono"', "ui-monospace", "SFMono-Regular", "Menlo",
          '"Cascadia Mono"', '"Noto Sans Mono CJK SC"', "monospace",
        ],
        sans: [
          '"Avenir Next"', "Seravek", '"Gill Sans"', "ui-sans-serif",
          '"PingFang SC"', '"Noto Sans CJK SC"', '"Microsoft YaHei"', "sans-serif",
        ],
      },
      boxShadow: {
        glow: "0 0 12px rgba(166, 219, 76, 0.25)",
        card: "0 1px 0 rgba(255,255,255,0.03) inset, 0 8px 24px rgba(0,0,0,0.35)",
      },
    },
  },
  plugins: [],
};
