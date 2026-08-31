import type { ReactNode } from "react";
import "../global.css";

// The locale picker at `/` has its own root layout. Locale pages use the
// dynamic root layout under `app/[lang]`, so their initial HTML language is
// correct even before client hydration.
export default function RootPickerLayout({
  children,
}: {
  children: ReactNode;
}) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <body className="flex flex-col min-h-screen">{children}</body>
    </html>
  );
}
