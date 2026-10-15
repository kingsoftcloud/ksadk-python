const path = require('node:path')

const version =
  process.env.STUDIO_APP_VERSION ||
  require('child_process')
    .execSync('python3 -c "from ksadk.version import VERSION; print(VERSION)"', { encoding: 'utf-8' })
    .trim()
  || '0.0.0'

const managedResources = [
  { from: 'build-resources/runtime', to: 'runtime' },
  { from: 'build-resources/node', to: 'node' },
  { from: 'build-resources/plugin-toolchains', to: 'plugin-toolchains' },
]

module.exports = {
  appId: 'com.kingsoftcloud.agentkit.studio',
  productName: 'AgentKit Studio',
  executableName: 'AgentKitStudio',
  compression: 'normal',
  directories: {
    buildResources: 'build-resources',
    output: 'dist/studio-app',
  },
  files: [
    'electron-main.js',
    'desktop-runtime.js',
    'preload.js',
    'package.json',
    'node_modules/**/*',
  ],
  asar: true,
  asarUnpack: ['**/*.{node,dylib,so,dll}'],
  extraResources: [
    ...managedResources,
    { from: 'build-resources/manifest.json', to: 'manifest.json' },
    { from: 'scripts/studio_icon.ico', to: 'AgentKitStudio.ico', filter: false },
  ],
  extraMetadata: {
    version,
    main: 'electron-main.js',
  },
  mac: {
    artifactName: 'AgentKitStudio-\${version}-macos-\${arch}.\${ext}',
    category: 'public.app-category.developer-tools',
    hardenedRuntime: true,
    entitlements: path.resolve(__dirname, 'scripts/studio.entitlements'),
    entitlementsInherit: path.resolve(__dirname, 'scripts/studio.entitlements'),
    icon: 'build-resources/AgentKitStudio.icns',
    // electron-builder skips the managed resources (Python/Node/DSH hold
    // hundreds of Mach-O files osx-sign cannot scan reliably); the afterSign
    // hook signs them with the proper entitlements, then notarizes.
    signIgnore: managedResources.map(r => '/Contents/Resources/' + r.to + '/'),
    // Notarization is owned by the afterSign hook (Apple ID or API-key flow);
    // without this electron-builder auto-enables its own when it sees APPLE_ID
    // and fails on its expected APPLE_APP_SPECIFIC_PASSWORD variable name.
    notarize: false,
    target: ['dmg', 'zip'],
    extendInfo: {
      NSMicrophoneUsageDescription:
        'AgentKit Studio uses the microphone for voice features.',
    },
  },
  afterSign: path.resolve(__dirname, 'scripts/after-sign.cjs'),
  dmg: {
    sign: false,
    writeUpdateInfo: false,
  },
  win: {
    artifactName: 'AgentKitStudio-\${version}-windows-\${arch}-setup.\${ext}',
    icon: 'build-resources/AgentKitStudio.ico',
    target: ['nsis'],
  },
  nsis: {
    oneClick: false,
    allowElevation: false,
    allowToChangeInstallationDirectory: false,
    createDesktopShortcut: true,
    createStartMenuShortcut: true,
  },
  publish: {
    provider: 'github',
    owner: 'kingsoftcloud',
    repo: 'ksadk-python',
  },
}
