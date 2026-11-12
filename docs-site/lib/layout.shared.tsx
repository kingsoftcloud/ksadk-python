import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';
import { appName, gitConfig } from './shared';

export function baseOptions(locale: string): BaseLayoutProps {
  return {
    nav: {
      title: (
        <span className="font-bold ksadk-hero-gradient">
          {appName}
        </span>
      ),
      url: `/${locale}`,
    },
    githubUrl: `https://github.com/${gitConfig.user}/${gitConfig.repo}`,
  };
}
