export const MOBILE_BREAKPOINT = 768;
export const DESKTOP_COMPOSER_MAX_HEIGHT = 160;
export const MOBILE_COMPOSER_MIN_HEIGHT = 120;
export const MOBILE_COMPOSER_HEIGHT_RATIO = 0.28;

export function isMobileViewportWidth(width, breakpoint = MOBILE_BREAKPOINT) {
  return Number(width) <= breakpoint;
}

export function resolveSidebarVisibility({
  isMobile,
  desktopSidebarOpen,
  mobileSidebarOpen,
}) {
  return {
    desktopSidebarVisible: !isMobile && Boolean(desktopSidebarOpen),
    mobileDrawerVisible: Boolean(isMobile && mobileSidebarOpen),
  };
}

export function resolveComposerMaxHeight({
  isMobile,
  viewportHeight,
  desktopMaxHeight = DESKTOP_COMPOSER_MAX_HEIGHT,
}) {
  if (!isMobile) {
    return desktopMaxHeight;
  }

  const numericViewportHeight = Number(viewportHeight);
  if (!Number.isFinite(numericViewportHeight) || numericViewportHeight <= 0) {
    return desktopMaxHeight;
  }

  const scaledHeight = Math.floor(numericViewportHeight * MOBILE_COMPOSER_HEIGHT_RATIO);
  return Math.max(MOBILE_COMPOSER_MIN_HEIGHT, Math.min(desktopMaxHeight, scaledHeight));
}
