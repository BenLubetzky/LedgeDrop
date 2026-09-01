import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "LedgerDrop",
  description: "Upload and track invoice PDFs.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
