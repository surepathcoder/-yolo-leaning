/**
 * VisionGuard design tokens.
 *
 * The palette is dark-first. A security operations screen is watched for hours
 * in a back office, often at night; a bright interface is genuinely tiring and
 * makes a camera feed harder to read. The light theme exists for managers who
 * work in daylight and for printed reports.
 *
 * Severity colours are deliberately not the framework defaults. They form an
 * ordered ramp -- slate → sky → amber → orange → red -- so an operator reads
 * urgency from hue before they read the label, and the two highest levels stay
 * distinguishable for the most common forms of colour blindness (they differ
 * in lightness as well as hue).
 */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Neutral surfaces. Not pure black: a true #000 against a bright video
        // feed produces uncomfortable contrast over a long shift.
        canvas: {
          DEFAULT: "#0b1017",
          raised: "#111823",
          overlay: "#18202d",
          border: "#233043",
        },
        brand: {
          50: "#eff9ff",
          100: "#def2ff",
          200: "#b6e8ff",
          300: "#75d8ff",
          400: "#2cc4ff",
          500: "#06aef0",
          600: "#008bcd",
          700: "#016fa6",
          800: "#065d88",
          900: "#0b4d70",
        },
        severity: {
          info: "#64748b",
          low: "#38bdf8",
          medium: "#fbbf24",
          high: "#fb923c",
          critical: "#f43f5e",
        },
        status: {
          online: "#34d399",
          offline: "#64748b",
          connecting: "#fbbf24",
          error: "#f43f5e",
          disabled: "#475569",
        },
      },
      fontFamily: {
        sans: [
          "Inter",
          "system-ui",
          "-apple-system",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
        mono: ["JetBrains Mono", "SFMono-Regular", "Consolas", "monospace"],
      },
      fontSize: {
        // A dense operations table needs a step below text-xs that is still
        // legible at arm's length on a wall-mounted screen.
        "2xs": ["0.6875rem", { lineHeight: "1rem" }],
      },
      animation: {
        "pulse-slow": "pulse 3s cubic-bezier(0.4, 0, 0.6, 1) infinite",
        "fade-in": "fade-in 200ms ease-out",
        "slide-in": "slide-in 220ms cubic-bezier(0.16, 1, 0.3, 1)",
      },
      keyframes: {
        "fade-in": {
          from: { opacity: "0" },
          to: { opacity: "1" },
        },
        "slide-in": {
          from: { opacity: "0", transform: "translateY(-6px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
    },
  },
  plugins: [],
};
