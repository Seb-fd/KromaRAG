import type { Metadata, Viewport } from "next";
import { Inter, Space_Grotesk, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import { Providers } from "@/components/Providers";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });
const display = Space_Grotesk({ subsets: ["latin"], variable: "--font-display", display: "swap" });
const mono = JetBrains_Mono({ subsets: ["latin"], variable: "--font-geist-mono", display: "swap" });

export const metadata: Metadata = {
  title: "KromaRAG — Knowledge & Role-Based Vector Architecture",
  description: "RAG-enabled Agentic Assistant with RBAC",
  applicationName: "KromaRAG",
  icons: {
    icon: [{ url: "/logo.webp", type: "image/webp", sizes: "512x512" }],
    shortcut: "/logo.webp",
    apple: [{ url: "/logo.webp", sizes: "180x180", type: "image/webp" }],
  },
};

export const viewport: Viewport = {
  themeColor: "#0a0a12",
};

const themeInit = `(function(){try{var t=JSON.parse(localStorage.getItem("kromarag.theme")||'"dark"');if(t==="light"){document.documentElement.classList.remove("dark")}else{document.documentElement.classList.add("dark")}}catch(e){document.documentElement.classList.add("dark")}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInit }} />
      </head>
      <body className={`${inter.variable} ${display.variable} ${mono.variable} font-sans min-h-screen antialiased`}>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
