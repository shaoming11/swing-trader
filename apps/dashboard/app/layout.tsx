import type { Metadata } from "next";
import "./globals.css";
import Link from "next/link";

export const metadata: Metadata = {
  title: "Swing Trader",
  description: "Pipeline debug & signal review dashboard",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <nav className="border-b border-border bg-card px-6 py-3 flex items-center gap-6">
          <span className="font-semibold text-white tracking-tight">Swing Trader</span>
          <Link href="/" className="text-sm text-gray-400 hover:text-white">Run</Link>
          <Link href="/runs" className="text-sm text-gray-400 hover:text-white">History</Link>
          <Link href="/eval" className="text-sm text-gray-400 hover:text-white">Eval</Link>
          <Link href="/corpus" className="text-sm text-gray-400 hover:text-white">Corpus</Link>
          <a
            href="http://localhost:3000"
            target="_blank"
            rel="noopener noreferrer"
            className="text-sm text-gray-400 hover:text-white ml-auto"
          >
            Grafana
          </a>
        </nav>
        <main className="max-w-7xl mx-auto px-6 py-8">{children}</main>
      </body>
    </html>
  );
}
