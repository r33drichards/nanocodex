import type { ReactNode } from "react";
import "@copilotkit/react-ui/styles.css";
import "./globals.css";

export const metadata = {
  title: "nanocodex — CopilotKit",
  description: "CopilotKit frontend for the nanocodex AG-UI bridge",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
