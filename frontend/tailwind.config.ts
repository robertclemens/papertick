import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // validated chart palette (dark-surface steps) — see dataviz palette notes
        series1: "#3987e5",
        series2: "#d95926",
        series3: "#199e70",
        series4: "#c98500",
        series5: "#d55181",
        statusGood: "#0ca30c",
        statusCritical: "#d03b3b",
        statusWarning: "#fab219",
      },
      fontFamily: {
        sans: ["system-ui", "-apple-system", "Segoe UI", "sans-serif"],
      },
    },
  },
  plugins: [],
};
export default config;
