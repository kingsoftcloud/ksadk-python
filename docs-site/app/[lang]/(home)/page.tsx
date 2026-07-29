import Link from 'next/link';
import { ServerCodeBlock } from 'fumadocs-ui/components/codeblock.rsc';

const copy = {
  cn: {
    title: 'Kingsoft Cloud Agent Development Kit',
    subtitle: '金山云智能体开发套件',
    desc: '构建、部署、调试、观测企业级 AI 智能体的一站式云原生框架。兼容 Google ADK、LangGraph、LangChain 与 DeepAgents；0.8 新增 Codex Managed Runtime、A2A 1.0 数据面、HarnessApp 与 AG-UI/A2UI 事件轨道。',
    cta: '阅读文档',
    ctaSecondary: 'GitHub 仓库',
    installLabel: '安装',
  },
  en: {
    title: 'Kingsoft Cloud Agent Development Kit',
    subtitle: 'Agent development kit for Kingsoft Cloud',
    desc: 'A cloud-native framework to build, deploy, debug, and observe enterprise AI agents. It works with Google ADK, LangGraph, LangChain, and DeepAgents; 0.8 adds Codex Managed Runtime, an A2A 1.0 data plane, HarnessApp, and the AG-UI/A2UI event path.',
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
    <main className="relative flex flex-1 flex-col items-center justify-center text-center px-4 py-24 overflow-hidden">
      <div className="ksadk-hero-bg" aria-hidden="true" />
      <h1 className="text-5xl md:text-6xl font-bold mb-3 ksadk-hero-gradient relative">
        {t.title}
      </h1>
      <p className="text-xl font-semibold text-fd-foreground mb-2 relative">{t.subtitle}</p>
      <p className="max-w-2xl text-fd-muted-foreground mb-8 relative">{t.desc}</p>
      <div className="flex flex-wrap items-center justify-center gap-3 mb-8 relative">
        <Link
          href={docsHref}
          className="rounded-full px-6 py-2.5 font-medium text-white hover:opacity-90 transition shadow-lg"
          style={{ background: 'linear-gradient(135deg, #5368db 0%, #59d3d9 100%)' }}
        >
          {t.cta}
        </Link>
        <a
          href="https://github.com/kingsoftcloud/ksadk-python"
          className="rounded-full border border-fd-border px-6 py-2.5 font-medium hover:bg-fd-accent hover:text-fd-accent-foreground transition backdrop-blur-sm"
        >
          {t.ctaSecondary}
        </a>
      </div>
      <div className="w-full max-w-md relative">
        <p className="text-sm text-fd-muted-foreground mb-1.5 text-left">{t.installLabel}</p>
        <ServerCodeBlock
          lang="bash"
          code='pip install -U "ksadk[all]"'
          codeblock={{ className: 'text-left text-sm' }}
        />
      </div>
    </main>
  );
}
