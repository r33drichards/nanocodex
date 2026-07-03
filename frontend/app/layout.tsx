import type { ReactNode } from "react";
import "./globals.css";

export const metadata = {
  title: "nanocodex — assistant-ui",
  description: "assistant-ui + AG-UI frontend for nanocodex (codex threads as source of truth)",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
