// Browser-only contribution; Core loads client.js through the DSH client
// extension contract and does not need a server-side Cordis service.
export const name = 'dsh-channels-client'

// DSH still expects every server-side bundle entrypoint to expose apply;
// the actual workspace contribution is registered by client.js.
export function apply() {}
