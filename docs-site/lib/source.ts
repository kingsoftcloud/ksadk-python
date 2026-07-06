import { createElement } from 'react';
import { icons } from 'lucide-react';
import { docs } from 'collections/server';
import { loader } from 'fumadocs-core/source';
import { statusBadgesPlugin } from 'fumadocs-core/source/plugins/status-badges';
import { i18n } from './i18n';
import { docsContentRoute, docsImageRoute, docsRoute, assetPath } from './shared';

// See https://fumadocs.dev/docs/headless/source-api for more info
export const source = loader({
  baseUrl: docsRoute,
  i18n,
  // Resolve `icon` strings in meta.json (root tabs, groups) to lucide icons.
  icon(name) {
    if (name && name in icons) {
      return createElement(icons[name as keyof typeof icons]);
    }
  },
  source: docs.toFumadocsSource(),
  // Render a sidebar badge for pages whose frontmatter sets `status` (e.g. `status: new`).
  plugins: [
    statusBadgesPlugin({
      renderBadge: (status) =>
        createElement(
          'span',
          {
            className:
              'ms-auto shrink-0 rounded-full bg-fd-primary/10 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-fd-primary',
          },
          status,
        ),
    }),
  ],
});

export function getPageImage(page: (typeof source)['$inferPage']) {
  const segments = [...page.slugs, 'image.png'];

  return {
    segments,
    url: `${docsImageRoute}/${segments.join('/')}`,
  };
}

export function getPageMarkdownUrl(page: (typeof source)['$inferPage']) {
  const segments = [...page.slugs, 'content.md'];

  return {
    segments,
    // GitHub Pages 在 /<repo> 子路径,markdown URL 需带 basePath,否则复制出来 404。
    url: assetPath(`${docsContentRoute}/${segments.join('/')}`),
  };
}

export async function getLLMText(page: (typeof source)['$inferPage']) {
  const processed = await page.data.getText('processed');

  return `# ${page.data.title} (${page.url})

${cleanForLLM(processed)}`;
}

// 剥离 Fumadocs JSX 组件标签和锚点语法,输出干净的 markdown 给 LLM/llms.mdx。
// - <Callout>...</Callout> / <Tabs>..</Tabs> / <Card>..</Card> 等:保留内部文本,去掉标签
// - 自闭合 <Files/> <Folder/> <File/> 等:去掉
// - 标题锚点 ## 标题 [#锚点]:去掉 [#...] 部分
// - ```mermaid 代码块原样保留
function cleanForLLM(md: string): string {
  return md
    // 去掉标题行尾的锚点 [#xxx] / (#xxx)
    .replace(/\s*\[#[^\]]+\]/g, '')
    // 自闭合组件 <X .../> 换行成空(Files/Folder/File/Cards 等容器,内容已在子节点)
    .replace(/<[A-Z][A-Za-z]*[^>]*\/>/g, '')
    // 去掉开标签 <Callout ...> <Tabs> <Tab ...> <Card ...> <Steps> <Step ...> <Accordion ...>
    .replace(/<[A-Z][A-Za-z]*[^>]*>/g, '')
    // 去掉闭标签 </Callout> </Tabs> 等
    .replace(/<\/[A-Z][A-Za-z]*>/g, '')
    // 合并多余空行(组件去掉后可能留多行空)
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}
