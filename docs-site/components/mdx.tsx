import defaultMdxComponents from 'fumadocs-ui/mdx';
import { Tab, Tabs } from 'fumadocs-ui/components/tabs';
import { Accordion, Accordions } from 'fumadocs-ui/components/accordion';
import { Step, Steps } from 'fumadocs-ui/components/steps';
import { Callout } from 'fumadocs-ui/components/callout';
import { Card, Cards } from 'fumadocs-ui/components/card';
import { TypeTable } from 'fumadocs-ui/components/type-table';
import { File, Files, Folder } from 'fumadocs-ui/components/files';
import UncontrolledZoom from 'react-medium-image-zoom';
import 'react-medium-image-zoom/dist/styles.css';
import type { MDXComponents } from 'mdx/types';
import type { DetailedHTMLProps, ImgHTMLAttributes } from 'react';
import { Mermaid } from '@/components/mdx/mermaid';
import { Video } from '@/components/mdx/video';

// markdown ![]() 自动包裹 react-medium-image-zoom,支持点击放大。
// 直接用原生 <img> 不经过 Next/Image,这样 SVG 也能正常显示和缩放
// (Next/Image 默认拒绝 SVG,会导致 src 变空图渲染不出)。
function ZoomableImg({
  src,
  alt,
  ...rest
}: DetailedHTMLProps<ImgHTMLAttributes<HTMLImageElement>, HTMLImageElement>) {
  // src 可能是 string 或 StaticImport 对象;统一转成字符串
  const srcStr =
    typeof src === 'string'
      ? src
      : src && typeof src === 'object' && 'src' in src
        ? String(src.src)
        : '';
  return (
    <UncontrolledZoom zoomMargin={20} wrapElement="span">
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img {...rest} src={srcStr} alt={alt ?? ''} loading="lazy" />
    </UncontrolledZoom>
  );
}

export function getMDXComponents(components?: MDXComponents) {
  return {
    ...defaultMdxComponents,
    Tab,
    Tabs,
    Accordion,
    Accordions,
    Step,
    Steps,
    Callout,
    Card,
    Cards,
    TypeTable,
    File,
    Files,
    Folder,
    Mermaid,
    Video,
    img: ZoomableImg,
    ...components,
  } satisfies MDXComponents;
}

export const useMDXComponents = getMDXComponents;

declare global {
  type MDXProvidedComponents = ReturnType<typeof getMDXComponents>;
}
