import type { Metadata, Viewport } from "next";
import { Inter, JetBrains_Mono } from "next/font/google";

import { Providers } from "@/components/shell/Providers";
import { THEME_SCRIPT } from "@/lib/theme";
import "./globals.css";

/**
 * UI typeface.
 *
 * `display: "swap"` renders immediately in the system font and swaps when
 * Inter arrives; blocking on the font leaves the interface invisible for as
 * long as the download takes, which on a slow connection *is* the perceived
 * load time.
 *
 * `adjustFontFallback` scales the fallback's metrics to Inter's so the swap
 * does not reflow. Without it every text block shifts a pixel or two when the
 * font lands — the visible "flash" people associate with web fonts.
 */
const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
  adjustFontFallback: true,
});

/** Console, code, and every figure that must not change width as it updates. */
const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains",
  display: "swap",
  // Only the two weights the UI renders. The full family is ~180 kB of faces
  // nothing uses.
  weight: ["400", "500"],
});

export const metadata: Metadata = {
  title: {
    default: "ArchX3D",
    template: "%s · ArchX3D",
  },
  description:
    "Turn 2D DXF floor plans and reference photographs into explorable 3D models.",
  applicationName: "ArchX3D",
};

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  // Zoom stays enabled. Disabling it fails WCAG 1.4.4 and is actively hostile
  // in a viewer, where inspecting detail is the point.
  maximumScale: 5,
  themeColor: [
    { media: "(prefers-color-scheme: light)", color: "#fcfcfd" },
    { media: "(prefers-color-scheme: dark)", color: "#0a0b0e" },
  ],
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    // `suppressHydrationWarning`: the pre-paint script below sets `data-theme`
    // on this element, so the client markup deliberately differs from the
    // server's.
    <html lang="en" suppressHydrationWarning>
      <head>
        {/*
          Resolves the theme before first paint so a dark-mode user never sees
          a white flash. Must be inline and must run here — a deferred module
          would execute after the paint it exists to prevent.
        */}
        <script dangerouslySetInnerHTML={{ __html: THEME_SCRIPT }} />
      </head>
      <body className={`${inter.variable} ${jetbrainsMono.variable}`}>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
