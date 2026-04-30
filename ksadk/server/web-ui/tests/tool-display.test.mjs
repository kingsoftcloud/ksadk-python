import test from 'node:test';
import assert from 'node:assert/strict';

async function loadToolDisplayUtils() {
  return import('../src/utils/tool-display.js').catch(() => null);
}

test('tool display formats nested JSON strings and decodes Chinese escapes', async () => {
  const toolDisplay = await loadToolDisplayUtils();

  assert.ok(toolDisplay, 'expected tool display helpers to exist');
  const formatted = toolDisplay.formatToolPayload(
    '[{"type":"input_text","text":"{\\"success\\":true,\\"analysis\\":\\"\\\\u6839\\\\u636e\\\\u622a\\\\u56fe\\\\u663e\\\\u793a\\"}"}]',
  );

  assert.equal(
    formatted,
    '[\n  {\n    "type": "input_text",\n    "text": {\n      "success": true,\n      "analysis": "根据截图显示"\n    }\n  }\n]',
  );
});
