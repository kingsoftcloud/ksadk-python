import {
  DocsBody,
  DocsDescription,
  DocsPage,
  DocsTitle,
  MarkdownCopyButton,
  ViewOptionsPopover,
} from "fumadocs-ui/layouts/docs/page";
import { createRelativeLink } from "fumadocs-ui/mdx";
import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { getMDXComponents } from "@/components/mdx";
import { gitConfig, publicSiteUrl } from "@/lib/shared";
import { getPageImage, getPageMarkdownUrl, source } from "@/lib/source";

export default async function Page({
  params,
}: {
  params: Promise<{ lang: string; slug?: string[] }>;
}) {
  const { lang, slug } = await params;
  const page = source.getPage(slug, lang);
  if (!page) notFound();

  const MDX = page.data.body;
  const markdownUrl = getPageMarkdownUrl(page).url;

  return (
    <DocsPage
      toc={page.data.toc}
      full={page.data.full}
      footer={{ enabled: false }}
    >
      <div className="flex flex-row items-center justify-between gap-4 border-b pb-4">
        <div className="flex flex-col gap-2">
          <DocsTitle className="mb-0">{page.data.title}</DocsTitle>
          <DocsDescription className="mb-0">
            {page.data.description}
          </DocsDescription>
        </div>
        <div className="flex flex-row gap-2 items-center shrink-0">
          <MarkdownCopyButton markdownUrl={markdownUrl} />
          <ViewOptionsPopover
            markdownUrl={markdownUrl}
            githubUrl={`https://github.com/${gitConfig.user}/${gitConfig.repo}/blob/${gitConfig.branch}/docs-site/content/docs/${page.path}`}
          />
        </div>
      </div>
      <DocsBody>
        <MDX
          components={getMDXComponents({
            // this allows you to link to other pages with relative file paths
            a: createRelativeLink(source, page),
          })}
        />
      </DocsBody>
    </DocsPage>
  );
}

export async function generateStaticParams() {
  return source.generateParams();
}

export async function generateMetadata({
  params,
}: {
  params: Promise<{ lang: string; slug?: string[] }>;
}): Promise<Metadata> {
  const { lang, slug } = await params;
  const page = source.getPage(slug, lang);
  if (!page) notFound();

  const suffix = page.slugs.length > 0 ? `${page.slugs.join("/")}/` : "";
  const localizedUrl = (locale: string) =>
    `${publicSiteUrl}/${locale}/docs/${suffix}`;

  return {
    title: page.data.title,
    description: page.data.description,
    alternates: {
      canonical: localizedUrl(lang),
      languages: {
        "zh-CN": localizedUrl("cn"),
        en: localizedUrl("en"),
        "x-default": localizedUrl("cn"),
      },
    },
    openGraph: {
      url: localizedUrl(lang),
      locale: lang === "en" ? "en_US" : "zh_CN",
      images: `${publicSiteUrl}${getPageImage(page).url}`,
    },
  };
}
