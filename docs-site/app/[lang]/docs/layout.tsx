import { DocsLayout } from "fumadocs-ui/layouts/docs";
import { BookMarked, LayoutGrid, Terminal } from "lucide-react";
import type { ReactNode } from "react";
import { baseOptions } from "@/lib/layout.shared";
import { source } from "@/lib/source";

export default async function Layout({
  params,
  children,
}: {
  params: Promise<{ lang: string }>;
  children: ReactNode;
}) {
  const { lang } = await params;
  const zh = lang === "cn";

  // Root dropdown (sidebar RootToggle): user journey / CLI / reference.
  const tabs = [
    {
      title: zh ? "开发指南" : "Development",
      description: zh ? "从入门到部署" : "From quickstart to deployment",
      url: `/${lang}/docs/framework`,
      icon: <LayoutGrid className="size-full" />,
    },
    {
      title: zh ? "命令行工具" : "CLI",
      description: zh ? "命令行参考" : "Command-line reference",
      url: `/${lang}/docs/cli`,
      icon: <Terminal className="size-full" />,
    },
    {
      title: zh ? "参考" : "Reference",
      description: zh ? "API · 贡献 · 许可" : "API · Contributing · License",
      url: `/${lang}/docs/references`,
      icon: <BookMarked className="size-full" />,
    },
  ];

  return (
    <DocsLayout
      tree={source.getPageTree(lang)}
      tabMode="auto"
      tabs={tabs}
      {...baseOptions(lang)}
    >
      {children}
    </DocsLayout>
  );
}
