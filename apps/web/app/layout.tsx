import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import "./globals.css";
import { Providers } from "@/components/Providers";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });

export const metadata: Metadata = {
  title: "KromaRAG — Knowledge & Role-Based Vector Architecture",
  description: "RAG-enabled Agentic Assistant with RBAC",
  applicationName: "KromaRAG",
  icons: {
    icon: [{ url: "/icon.png", type: "image/png", sizes: "512x512" }, "/favicon.ico"],
    shortcut: "/favicon.ico",
    apple: [{ url: "/apple-icon.png", sizes: "180x180", type: "image/png" }],
  },
};

export const viewport: Viewport = {
  themeColor: "#1c2a7d",
};

const themeInit = `(function(){try{var t=JSON.parse(localStorage.getItem("kromarag.theme")||'"dark"');if(t==="light"){document.documentElement.classList.remove("dark")}else{document.documentElement.classList.add("dark")}}catch(e){document.documentElement.classList.add("dark")}})();`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeInit }} />
      </head>
      <body className={`${inter.variable} font-sans min-h-screen antialiased`}>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
