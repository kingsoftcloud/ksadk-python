// afterSign hook: delegate to shell script to avoid Node.js async issues
// with spawnSync inside electron-builder's async event emitter.
const { execFileSync } = require('node:child_process')
const fs = require('node:fs')
const path = require('node:path')

module.exports = async function afterSign(context) {
  const identity = process.env.STUDIO_APP_CODESIGN_IDENTITY
  if (!identity) {
    console.log('[after-sign] STUDIO_APP_CODESIGN_IDENTITY not set; skipping')
    return
  }

  const appOutDir = context.appOutDir
  const appDirName = fs.readdirSync(appOutDir).find((f) => f.endsWith('.app'))
  if (!appDirName) {
    console.log(`[after-sign] no .app found in ${appOutDir}; skipping`)
    return
  }
  const appPath = path.join(appOutDir, appDirName)
  const entitlements = path.resolve(__dirname, 'studio.entitlements')
  const nodeEntitlements = path.resolve(__dirname, 'node.entitlements')
  const script = path.resolve(__dirname, 'after-sign.sh')

  execFileSync('sh', [script, appPath, context.outDir, entitlements, nodeEntitlements], {
    stdio: 'inherit',
    env: process.env,
  })
}
