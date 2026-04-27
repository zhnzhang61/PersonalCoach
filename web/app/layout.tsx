import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono, Lora } from "next/font/google";
import { BottomNav } from "@/components/bottom-nav";
import { Providers } from "./providers";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});
const lora = Lora({
  variable: "--font-heading-serif",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "PersonalCoach",
  description: "Training and recovery dashboard",
  appleWebApp: {
    capable: true,
    title: "PersonalCoach",
    statusBarStyle: "black-translucent",
  },
};

export const viewport: Viewport = {
  themeColor: "#0a0a0a",
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html
      lang="en"
      className={`${geistSans.variable} ${geistMono.variable} ${lora.variable} h-full antialiased`}
    >
      <body className="bg-background text-foreground min-h-full flex flex-col">
        <Providers>
          <main className="flex-1 pb-20 pt-[env(safe-area-inset-top)]">
            {children}
          </main>
          <BottomNav />
        </Providers>
      </body>
    </html>
  );
}
