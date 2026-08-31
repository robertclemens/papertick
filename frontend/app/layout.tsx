import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "PaperTick",
  description: "Paper trading, backtesting and wealth-management simulation",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body>{children}</body>
    </html>
  );
}
