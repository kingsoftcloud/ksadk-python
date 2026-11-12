import { i18nProvider } from "fumadocs-ui/i18n";
import { RootProvider } from "fumadocs-ui/provider/next";
import type { Metadata } from "next";
import type { ReactNode } from "react";
import SearchDialog from "@/components/search";
import { i18n } from "@/lib/i18n";
import { translations } from "@/lib/i18n-ui";
import { publicSiteUrl } from "@/lib/shared";
import "../global.css";

export function generateStaticParams() {
  return i18n.languages.map((lang) => ({ lang }));
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ lang: string }>;
}): Promise<Metadata> {
  const { lang } = await params;
  return {
    metadataBase: new URL(`${publicSiteUrl}/`),
    alternates: {
      canonical: `${publicSiteUrl}/${lang}/`,
      languages: {
        "zh-CN": `${publicSiteUrl}/cn/`,
        en: `${publicSiteUrl}/en/`,
        "x-default": `${publicSiteUrl}/cn/`,
      },
    },
  };
}

export default async function LangLayout({
  params,
  children,
}: {
  params: Promise<{ lang: string }>;
  children: ReactNode;
}) {
  const { lang } = await params;
  const htmlLanguage = lang === "en" ? "en" : "zh-CN";

  return (
    <html lang={htmlLanguage} suppressHydrationWarning>
      <body className="flex flex-col min-h-screen">
        <RootProvider
          i18n={i18nProvider(translations, lang)}
          search={{ SearchDialog }}
        >
          {children}
        </RootProvider>
      </body>
    </html>
  );
}
