/**
 * MessageMarkdown 的预处理层只做“稳态修复”：
 * - 兼容模型输出的 inline heading / list 粘连
 * - 只修明显的“单行表格 blob”
 * - 不再对所有 `|` 做全局替换，避免把合法 GFM table 反向拆坏
 */

function normalizeBrokenThinkBlocks(text) {
  return text.replace(
    /<think>([\s\S]*?)(?:<\/think>|$)/gi,
    (_match, thoughtText) => {
      const thoughtLines = String(thoughtText || '')
        .trim()
        .split('\n')
        .filter(Boolean)
        .map((line) => `> ${line}`);
      return `\n> 💭 **思考过程**\n>\n${thoughtLines.join('\n')}\n\n`;
    },
  );
}

function isSeparatorRow(line) {
  return /^\s*\|(?:\s*[:\-]+\s*\|){2,}\s*$/.test(line);
}

function isTableRow(line) {
  const trimmed = line.trim();
  if (!trimmed.startsWith('|')) {
    return false;
  }
  return trimmed.split('|').length >= 4;
}

function isInlineTableBlob(line) {
  return isSeparatorRow(line)
    || /\|\|/.test(line)
    || (/\|(?:\s*[:\-]+\s*\|){2,}/.test(line) && /\|(?:[^|\n]*\|){2,}/.test(line));
}

function splitInlineTableBlob(line) {
  if (!isInlineTableBlob(line)) {
    return [line];
  }
  return line
    .replace(/\|\s*\|/g, '|\n|')
    .split('\n')
    .map((segment) => segment.trimEnd())
    .filter((segment) => segment.trim().length > 0);
}

function normalizeTableLineArtifacts(line) {
  if (!isTableRow(line)) {
    return line;
  }

  // Streaming responses occasionally leave a dangling bold marker after a
  // packed table row. Keep inline cell formatting intact, only remove markers
  // that appear after the final pipe.
  return line.replace(/\|\s*(?:\*{1,2}|_{1,2})\s*$/u, '|');
}

function tableColumnCount(line) {
  const trimmed = line.trim();
  if (!isTableRow(trimmed)) {
    return 0;
  }
  return trimmed
    .split('|')
    .slice(1, -1)
    .length;
}

function buildSeparatorRow(line) {
  const indentation = line.match(/^(\s*)/)?.[1] || '';
  const columns = tableColumnCount(line);
  if (columns < 2) {
    return '';
  }
  return `${indentation}| ${Array.from({ length: columns }, () => '---').join(' | ')} |`;
}

function normalizeTableGroups(block) {
  const normalizedLines = [];

  for (const line of block.split('\n')) {
    const singlePipeProse = line.match(/^(\s*)\|\s*([^|]+)$/);
    if (singlePipeProse) {
      normalizedLines.push(`${singlePipeProse[1]}${singlePipeProse[2].trim()}`);
      continue;
    }
    normalizedLines.push(...splitInlineTableBlob(line).map(normalizeTableLineArtifacts));
  }

  const repairedLines = [];
  for (let index = 0; index < normalizedLines.length; index += 1) {
    const line = normalizedLines[index];
    const nextLine = normalizedLines[index + 1] || '';
    repairedLines.push(line);
    if (
      isTableRow(line) &&
      isTableRow(nextLine) &&
      !isSeparatorRow(nextLine) &&
      tableColumnCount(line) === tableColumnCount(nextLine)
    ) {
      const previousLine = normalizedLines[index - 1] || '';
      const previousIsTable = isTableRow(previousLine);
      const lineIndentation = line.match(/^(\s*)/)?.[1] || '';
      if (!previousIsTable && lineIndentation.length === 0) {
        const separator = buildSeparatorRow(line);
        if (separator) {
          repairedLines.push(separator);
        }
      }
    }
  }

  const withSpacing = [];
  for (let index = 0; index < repairedLines.length; index += 1) {
    const line = repairedLines[index];
    const nextLine = repairedLines[index + 1] || '';
    const previousLine = withSpacing[withSpacing.length - 1] || '';
    const startsTableGroup = isTableRow(line) && (
      previousLine.trim().length === 0 || !isTableRow(previousLine)
    );
    const endsTableGroup = isTableRow(line) && (
      nextLine.trim().length === 0 || !isTableRow(nextLine)
    );
    const indentation = line.match(/^(\s*)/)?.[1] || '';

    // 顶层 prose 后面直接接表格时，补一个空行让 remark-gfm 更稳定识别。
    if (startsTableGroup && indentation.length === 0 && previousLine.trim().length > 0) {
      withSpacing.push('');
    }

    withSpacing.push(line);

    if (endsTableGroup && indentation.length === 0 && nextLine.trim().length > 0) {
      withSpacing.push('');
    }
  }

  return withSpacing.join('\n');
}

function normalizeHeadingLine(line) {
  const cleanedLine = line.replace(/^[\u200B-\u200D\uFEFF]+/u, '');
  const match = cleanedLine.match(/^([ \t\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]*)([＃#]{1,6})([ \t\u00A0\u1680\u2000-\u200A\u202F\u205F\u3000]*)(\S.*)?$/u);
  if (!match) {
    return cleanedLine;
  }

  const [, indentation, hashes, , rest = ''] = match;
  const normalizedIndentation = indentation.replace(/[^\t ]/gu, ' ');
  const normalizedHashes = hashes.replace(/＃/gu, '#');
  if (!rest) {
    return `${normalizedIndentation}${normalizedHashes}`;
  }
  return `${normalizedIndentation}${normalizedHashes} ${rest}`;
}

function normalizeProseBlock(block) {
  let normalized = block;

  normalized = normalized
    .split('\n')
    .map((line) => normalizeHeadingLine(line))
    .join('\n');

  // 标题与正文/表格粘连时补开。
  normalized = normalized.replace(/^[ \t]+(#{1,6}\s*)/gm, '$1');
  // 这里只处理“正文后面直接跟标题”的粘连，不要把 `##` / `###` 自己拆成两段。
  normalized = normalized.replace(/([^\n#])\s*(#{1,6}\s*\S)/g, '$1\n\n$2');
  normalized = normalized.replace(/^(#{1,6}\s*[^\n]+)\n(?!\n)/gm, '$1\n\n');
  normalized = normalized.replace(/^(#{1,6}[^\n|]+?)\s*(\|)/gm, '$1\n$2');

  // 列表经常被模型压成一行，这里只做轻量拆分。
  normalized = normalized.replace(/([。；;：:])\s*(\d+\.\s*)/g, '$1\n$2');
  normalized = normalized.replace(/([^\n\s])(\d+\.\s+)/g, '$1\n$2');
  normalized = normalized.replace(/([。；;：:\.])\s*-(?!-)\s*/g, '$1\n- ');
  normalized = normalized.replace(/([^\n\s-])-\s+/g, '$1\n- ');
  normalized = normalized.replace(/^-(?!-)(\S)/gm, '- $1');

  normalized = normalizeTableGroups(normalized);
  normalized = normalized.replace(/\n{3,}/g, '\n\n');
  return normalized.trim();
}

function normalizeFenceAdjacentProse(parts) {
  return parts.map((part, index) => {
    if (index % 2 === 1) {
      return part;
    }

    let prose = normalizeProseBlock(part);
    const hasPreviousFence = index > 0;
    const hasNextFence = index < parts.length - 1;

    // 模型经常把 ```go 粘在粗体/正文后面，或者把 ``` 和下一段正文挤在一行。
    // 这里不猜 fenced block 的语义，只保证围栏前后各自独立成块。
    if (!prose) {
      if (hasPreviousFence && hasNextFence) {
        return '\n\n';
      }
      if (hasPreviousFence || hasNextFence) {
        return '\n';
      }
      return prose;
    }

    if (hasPreviousFence && !prose.startsWith('\n')) {
      prose = `\n${prose}`;
    }
    if (hasNextFence && !prose.endsWith('\n')) {
      prose = `${prose}\n`;
    }
    return prose;
  });
}

export function preprocessMarkdown(text) {
  if (!text) {
    return text;
  }

  let normalized = String(text).replace(/\r\n/g, '\n').replace(/\r/g, '\n').trim();
  normalized = normalizeBrokenThinkBlocks(normalized);

  // 若代码块围栏数量为奇数，自动补一个闭合围栏，避免后续正文被整个吞掉。
  if ((normalized.match(/```/g) || []).length % 2 === 1) {
    normalized = `${normalized}\n\`\`\``;
  }

  const parts = normalized.split('```');
  const normalizedParts = normalizeFenceAdjacentProse(parts);
  return normalizedParts.join('```').trim();
}
