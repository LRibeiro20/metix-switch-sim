import type { Metadata } from "next";

import { AstryxProvider } from "@/components/AstryxProvider";
import "./globals.css";

export const metadata: Metadata = {
  title: "METIX Switch — SIMOrede ISO 8583 Instant Payment Simulator",
  description:
    "Fault-tolerant ISO 8583 & REST Instant Payment Switch modeled on Mozambique METIX / SIMOrede (Banco de Moçambique Notice No. 1/GBM/2026).",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" data-theme="light" data-astryx-theme="neutral">
      <body>
        <AstryxProvider>{children}</AstryxProvider>
      </body>
    </html>
  );
}
