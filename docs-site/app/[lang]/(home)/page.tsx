import Link from 'next/link';

const copy = {
  cn: {
    title: 'KsADK',
    subtitle: '一次构建 Agent，到处运行。',
    desc: 'KsADK 是面向 AI Agent 的运行时平台（Agent Runtime Platform）。继续使用 Google ADK、LangGraph、LangChain 或 DeepAgents 编写业务 Agent，再用 KsADK 获得统一的本地运行、浏览器调试、OpenAI-Compatible API、沙箱执行、部署和可观测体验。',
    cta: '阅读文档',
    ctaSecondary: 'GitHub 仓库',
    installLabel: '安装',
  },
  en: {
    title: 'KsADK',
    subtitle: 'Build agents once. Run them anywhere.',
    desc: 'KsADK is an Agent Runtime Platform. Keep writing business agents with Google ADK, LangGraph, LangChain, or DeepAgents — KsADK adds unified local run, browser debugging, OpenAI-Compatible API, sandbox execution, deployment, and observability.',
    cta: 'Read the docs',
    ctaSecondary: 'GitHub',
    installLabel: 'Install',
  },
} as const;

export default async function HomePage({ params }: { params: Promise<{ lang: string }> }) {
  const { lang } = await params;
  const t = copy[lang as keyof typeof copy] ?? copy.cn;
  const docsHref = `/${lang}/docs/framework`;

  return (
    <main className="flex flex-1 flex-col items-center justify-center text-center px-4 py-24">
      <h1 className="text-5xl md:text-6xl font-bold mb-3 bg-gradient-to-r from-blue-600 to-cyan-500 bg-clip-text text-transparent">
        {t.title}
      </h1>
      <p className="text-xl font-semibold text-fd-foreground mb-2">{t.subtitle}</p>
      <p className="max-w-2xl text-fd-muted-foreground mb-8">{t.desc}</p>
      <div className="flex flex-wrap items-center justify-center gap-3 mb-8">
        <Link
          href={docsHref}
          className="rounded-full bg-fd-primary text-fd-primary-foreground px-6 py-2.5 font-medium hover:opacity-90 transition"
        >
          {t.cta}
        </Link>
        <a
          href="https://github.com/kingsoftcloud/ksadk-python"
          className="rounded-full border border-fd-border px-6 py-2.5 font-medium hover:bg-fd-accent hover:text-fd-accent-foreground transition"
        >
          {t.ctaSecondary}
        </a>
      </div>
      <div className="w-full max-w-md">
        <p className="text-sm text-fd-muted-foreground mb-1.5 text-left">{t.installLabel}</p>
        <pre className="rounded-lg border border-fd-border bg-fd-muted/40 p-3 text-left text-sm">
          <code>pip install -U &quot;ksadk[all]&quot;</code>
        </pre>
      </div>
    </main>
  );
}
