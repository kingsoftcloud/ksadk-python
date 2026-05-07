export function shouldStopReadingRunStream(actions = []) {
  return actions.some((action) => {
    if (action?.type !== 'terminal') {
      return false;
    }
    const status = String(action.status || '').toLowerCase();
    return status === 'failed' || status === 'incomplete';
  });
}
