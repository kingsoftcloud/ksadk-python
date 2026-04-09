import test from 'node:test';
import assert from 'node:assert/strict';

async function loadMobileLayoutUtils() {
  return import('../src/utils/mobile-layout.js').catch(() => null);
}

test('mobile layout utils expose a 768px chat breakpoint', async () => {
  const mobileLayout = await loadMobileLayoutUtils();

  assert.ok(mobileLayout, 'expected mobile layout helpers to exist');
  assert.equal(mobileLayout.MOBILE_BREAKPOINT, 768);
  assert.equal(mobileLayout.isMobileViewportWidth(768), true);
  assert.equal(mobileLayout.isMobileViewportWidth(769), false);
});

test('sidebar visibility switches from desktop rail to mobile drawer', async () => {
  const mobileLayout = await loadMobileLayoutUtils();

  assert.ok(mobileLayout, 'expected mobile layout helpers to exist');
  assert.deepEqual(
    mobileLayout.resolveSidebarVisibility({
      isMobile: true,
      desktopSidebarOpen: true,
      mobileSidebarOpen: true,
    }),
    {
      desktopSidebarVisible: false,
      mobileDrawerVisible: true,
    },
  );
  assert.deepEqual(
    mobileLayout.resolveSidebarVisibility({
      isMobile: false,
      desktopSidebarOpen: true,
      mobileSidebarOpen: true,
    }),
    {
      desktopSidebarVisible: true,
      mobileDrawerVisible: false,
    },
  );
});

test('composer max height scales relative to mobile viewport height', async () => {
  const mobileLayout = await loadMobileLayoutUtils();

  assert.ok(mobileLayout, 'expected mobile layout helpers to exist');
  assert.equal(
    mobileLayout.resolveComposerMaxHeight({ isMobile: true, viewportHeight: 480 }),
    134,
  );
  assert.equal(
    mobileLayout.resolveComposerMaxHeight({ isMobile: true, viewportHeight: 900 }),
    160,
  );
  assert.equal(
    mobileLayout.resolveComposerMaxHeight({ isMobile: false, viewportHeight: 480 }),
    160,
  );
});
