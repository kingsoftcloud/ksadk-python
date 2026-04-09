import { useEffect, useState } from 'react';

import { MOBILE_BREAKPOINT, isMobileViewportWidth } from '../utils/mobile-layout.js';

type ResponsiveViewport = {
  isMobile: boolean;
  viewportHeight: number;
};

function getViewportHeight(): number {
  if (typeof window === 'undefined') {
    return 900;
  }
  return Math.round(window.visualViewport?.height || window.innerHeight || 900);
}

function getResponsiveSnapshot(): ResponsiveViewport {
  if (typeof window === 'undefined') {
    return {
      isMobile: false,
      viewportHeight: 900,
    };
  }

  return {
    isMobile: isMobileViewportWidth(window.innerWidth, MOBILE_BREAKPOINT),
    viewportHeight: getViewportHeight(),
  };
}

export function useResponsiveViewport(): ResponsiveViewport {
  const [state, setState] = useState<ResponsiveViewport>(() => getResponsiveSnapshot());

  useEffect(() => {
    if (typeof window === 'undefined') {
      return undefined;
    }

    const mediaQuery = window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT}px)`);
    const updateViewport = () => {
      setState(getResponsiveSnapshot());
    };

    updateViewport();

    const cleanupMediaQuery =
      typeof mediaQuery.addEventListener === 'function'
        ? (() => {
            mediaQuery.addEventListener('change', updateViewport);
            return () => mediaQuery.removeEventListener('change', updateViewport);
          })()
        : (() => {
            mediaQuery.addListener(updateViewport);
            return () => mediaQuery.removeListener(updateViewport);
          })();

    window.addEventListener('resize', updateViewport);
    window.visualViewport?.addEventListener('resize', updateViewport);

    return () => {
      cleanupMediaQuery();
      window.removeEventListener('resize', updateViewport);
      window.visualViewport?.removeEventListener('resize', updateViewport);
    };
  }, []);

  return state;
}
