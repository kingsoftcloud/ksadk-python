import defaultMdxComponents from 'fumadocs-ui/mdx';
import { Tab, Tabs } from 'fumadocs-ui/components/tabs';
import { Accordion, Accordions } from 'fumadocs-ui/components/accordion';
import { Step, Steps } from 'fumadocs-ui/components/steps';
import { Callout } from 'fumadocs-ui/components/callout';
import { Card, Cards } from 'fumadocs-ui/components/card';
import { TypeTable } from 'fumadocs-ui/components/type-table';
import { File, Files, Folder } from 'fumadocs-ui/components/files';
import { ImageZoom } from 'fumadocs-ui/components/image-zoom';
import type { MDXComponents } from 'mdx/types';
import type { DetailedHTMLProps, ImgHTMLAttributes } from 'react';
import { Mermaid } from '@/components/mdx/mermaid';
import { Video } from '@/components/mdx/video';

// markdown ![]() 自动用 ImageZoom 包裹,支持点击放大
function ZoomableImg({
  src,
  alt,
  ...rest
}: DetailedHTMLProps<ImgHTMLAttributes<HTMLImageElement>, HTMLImageElement>) {
  return (
    <ImageZoom
      src={typeof src === 'string' ? src : ''}
      alt={alt ?? ''}
      {...rest}
    />
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
