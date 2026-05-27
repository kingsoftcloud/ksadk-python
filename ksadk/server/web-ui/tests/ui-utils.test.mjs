import test from 'node:test';
import assert from 'node:assert/strict';

import { estimateTextTokens, buildComposerContextIndicator } from '../src/utils/context.js';
import { copyTextToClipboard } from '../src/utils/clipboard.js';
import { preprocessMarkdown } from '../src/utils/markdown.js';

test('estimateTextTokens gives CJK text a less optimistic estimate', () => {
  assert.equal(estimateTextTokens(''), 0);
  assert.equal(estimateTextTokens('hello world'), 3);
  assert.equal(estimateTextTokens('你好世界'), 4);
  assert.equal(estimateTextTokens('Agent平台设计'), 6);
});

test('buildComposerContextIndicator shows warning near threshold and compressing when checkpoint is active', () => {
  const warningIndicator = buildComposerContextIndicator({
    messages: [
      {
        role: 'user',
        content: '你'.repeat(160),
      },
    ],
    draftInput: '',
    selectedModel: {
      id: 'glm-5.1',
      context_window_tokens: 200,
      auto_compact_threshold_percentage: 80,
    },
  });
  assert.equal(warningIndicator?.phase, 'warning');
  assert.match(warningIndicator?.label || '', /即将压缩/);
  assert.equal(warningIndicator?.usedTokens, 160);
  assert.equal(warningIndicator?.contextWindowTokens, 200);
  assert.equal(warningIndicator?.percent, 80);

  const compressingIndicator = buildComposerContextIndicator({
    messages: [
      {
        role: 'system',
        eventType: 'context_checkpoint',
        status: 'running',
        summary: 'Earlier conversation summary',
      },
    ],
    draftInput: '',
    selectedModel: {
      id: 'glm-5.1',
      context_window_tokens: 200000,
    },
  });
  assert.deepEqual(compressingIndicator, {
    label: '正在压缩上下文…',
    phase: 'compressing',
    usedTokens: 7,
    contextWindowTokens: 200000,
    percent: 0,
  });
});

test('buildComposerContextIndicator exposes token counts for compact run status', () => {
  const indicator = buildComposerContextIndicator({
    messages: [
      { role: 'user', content: '你好世界' },
      { role: 'model', content: 'hello world' },
    ],
    draftInput: 'abcde',
    selectedModel: {
      id: 'qwen3.6-plus',
      limits: {
        context_window_tokens: 200000,
      },
    },
  });

  assert.equal(indicator?.phase, 'normal');
  assert.equal(indicator?.usedTokens, 9);
  assert.equal(indicator?.contextWindowTokens, 200000);
  assert.match(indicator?.label || '', /估算上下文/);
});

test('preprocessMarkdown keeps GFM table rows on separate lines for inline table blobs', () => {
  const raw = [
    '五、与岗位匹配度',
    '| JD核心要求',
    '| 候选人匹配情况 | 匹配度 | |--------|---------| | Agent Runtime系统设计 | ✅ 主导设计Agent Runtime模块 | 完全匹配 || Tool Calling机制 | ✅ 构建Tool Registry与工具协议体系 | 完全匹配 |',
    '',
    '综合匹配度：95%',
  ].join('\n');

  const normalized = preprocessMarkdown(raw);

  assert.match(
    normalized,
    /\| 候选人匹配情况 \| 匹配度 \|\n\|--------\|---------\|\n\| Agent Runtime系统设计 \| ✅ 主导设计Agent Runtime模块 \| 完全匹配 \|\n\| Tool Calling机制 \| ✅ 构建Tool Registry与工具协议体系 \| 完全匹配 \|/,
  );
  assert.doesNotMatch(normalized, /\|\n\|--------\|---------\|\n\|\n Agent Runtime系统设计/);
  assert.match(normalized, /\n\n\| 候选人匹配情况 \| 匹配度 \|/);
});

test('preprocessMarkdown preserves list indentation before tables', () => {
  const raw = [
    '- 面试要点：',
    '  | 维度 | 评分 |',
    '  | --- | --- |',
    '  | 架构设计 | 5 |',
  ].join('\n');

  const normalized = preprocessMarkdown(raw);

  assert.match(normalized, /- 面试要点：\n\s+\| 维度 \| 评分 \|/);
  assert.match(normalized, /\n\s+\| --- \| --- \|/);
});

test('preprocessMarkdown repairs header-only pipe tables from model output', () => {
  const raw = [
    '## 三地对比',
    '| 市场 | 表现 | 日期 |',
    '| A股 | 小幅下跌（沪指 - 0.09%） | 4月30日盘中 || 港股 | 跌幅较大（恒指 - 1.34%） | 4月30日盘中 || 美股 | 小幅收涨（纳指 + 0.27%） | 4月29日收盘 |',
    '',
    '今日亚太市场整体偏弱。',
  ].join('\n');

  const normalized = preprocessMarkdown(raw);

  assert.match(normalized, /\| 市场 \| 表现 \| 日期 \|\n\| --- \| --- \| --- \|/);
  assert.match(normalized, /\| A股 \| 小幅下跌（沪指 - 0\.09%） \| 4月30日盘中 \|/);
  assert.match(normalized, /\| 港股 \| 跌幅较大（恒指 - 1\.34%） \| 4月30日盘中 \|/);
  assert.match(normalized, /\| 美股 \| 小幅收涨（纳指 \+ 0\.27%） \| 4月29日收盘 \|/);
});

test('preprocessMarkdown repairs malformed packed market tables', () => {
  const raw = [
    '| 指数 | 当前 | 涨跌 | 涨幅 |',
    '| 上证指数 | 4 111.02 | + 3.50 | + 0.09% || 深证成指 | 15 111.33 | - 9.60 | - 0.06% || 创业板指 | 3 353.40 | - 8.92 | - 0.27% | **',
    '- 数据仅供参考',
  ].join('\n');

  const normalized = preprocessMarkdown(raw);

  assert.match(normalized, /\| 指数 \| 当前 \| 涨跌 \| 涨幅 \|\n\| --- \| --- \| --- \| --- \|/);
  assert.match(normalized, /\| 上证指数 \| 4 111\.02 \| \+ 3\.50 \| \+ 0\.09% \|/);
  assert.match(normalized, /\| 深证成指 \| 15 111\.33 \| - 9\.60 \| - 0\.06% \|/);
  assert.match(normalized, /\| 创业板指 \| 3 353\.40 \| - 8\.92 \| - 0\.27% \|/);
  assert.doesNotMatch(normalized, /\|\s+\*\*/);
  assert.match(normalized, /\n\n- 数据仅供参考/);
});

test('preprocessMarkdown keeps consecutive heading markers intact', () => {
  assert.equal(preprocessMarkdown('##⏭️ 下一步'), '## ⏭️ 下一步');
  assert.equal(preprocessMarkdown('###📌 标题'), '### 📌 标题');
});

test('preprocessMarkdown normalizes full-width and invisible heading prefixes', () => {
  assert.equal(preprocessMarkdown('\u200B＃我能做什么？'), '# 我能做什么？');
  assert.equal(preprocessMarkdown('\u3000##特色技能'), '## 特色技能');
});

test('preprocessMarkdown separates fenced code blocks that are glued to prose', () => {
  const raw = [
    '关于 Agent 间工具调用的冲突问题：',
    '',
    '**方案一：工具调用互斥锁机制**```go',
    'type ToolRegistry struct {',
    '  tools map[string]Tool',
    '}',
    '```',
    '',
    '**方案二：工具调用队列**```go',
    'type ToolCallRequest struct {',
    '  AgentID string',
    '}',
    '```',
  ].join('\n');

  const normalized = preprocessMarkdown(raw);

  assert.match(normalized, /\*\*方案一：工具调用互斥锁机制\*\*\n```go/);
  assert.match(normalized, /}\n```\n\*\*方案二：工具调用队列\*\*\n```go/);
});

test('copyTextToClipboard uses navigator clipboard when available', async () => {
  let copiedText = '';
  const ok = await copyTextToClipboard('hello', {
    navigator: {
      clipboard: {
        writeText: async (value) => {
          copiedText = value;
        },
      },
    },
  });

  assert.equal(ok, true);
  assert.equal(copiedText, 'hello');
});

test('copyTextToClipboard falls back to execCommand when navigator clipboard fails', async () => {
  const appended = [];
  const removed = [];
  const textarea = {
    value: '',
    style: {},
    setAttribute() {},
    focus() {},
    select() {},
    setSelectionRange() {},
  };

  const ok = await copyTextToClipboard('fallback', {
    navigator: {
      clipboard: {
        writeText: async () => {
          throw new Error('clipboard denied');
        },
      },
    },
    document: {
      activeElement: { focus() {} },
      body: {
        appendChild(node) {
          appended.push(node);
        },
        removeChild(node) {
          removed.push(node);
        },
      },
      createElement(tagName) {
        assert.equal(tagName, 'textarea');
        return textarea;
      },
      execCommand(command) {
        assert.equal(command, 'copy');
        return true;
      },
    },
  });

  assert.equal(ok, true);
  assert.equal(appended[0], textarea);
  assert.equal(removed[0], textarea);
  assert.equal(textarea.value, 'fallback');
});
