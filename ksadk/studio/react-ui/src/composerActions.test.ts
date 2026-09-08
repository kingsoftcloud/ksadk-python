import { describe, expect, it } from "vitest";
import {
  buildResponsesInput,
  encodedComposerAttachmentsBytes,
  MAX_COMPOSER_ATTACHMENT_BYTES,
  parseComposerSubmission,
  visibleComposerCommands,
  type ComposerAttachment,
} from "./composerActions";

describe("composer actions", () => {
  it("parses real plan and goal controls without creating fake chat messages", () => {
    expect(parseComposerSubmission("/plan")).toEqual({ kind: "toggle-plan" });
    expect(parseComposerSubmission("/default")).toEqual({ kind: "set-default" });
    expect(parseComposerSubmission("/goal 完成会话体验重构")).toEqual({
      kind: "goal",
      objective: "完成会话体验重构",
    });
    expect(parseComposerSubmission("普通消息")).toEqual({ kind: "message", text: "普通消息" });
  });

  it("filters the shared slash command catalog", () => {
    expect(visibleComposerCommands("/").map(item => item.slash)).toEqual([
      "/plan",
      "/goal",
      "/default",
    ]);
    expect(visibleComposerCommands("/go").map(item => item.slash)).toEqual(["/goal"]);
    expect(visibleComposerCommands("/goal ")).toEqual([]);
  });

  it("builds one Responses user message with text, image and text attachments", () => {
    const attachments: ComposerAttachment[] = [
      {
        id: "image-1",
        kind: "image",
        name: "screen.png",
        mimeType: "image/png",
        size: 4,
        dataUrl: "data:image/png;base64,AAAA",
      },
      {
        id: "text-1",
        kind: "text",
        name: "notes.md",
        mimeType: "text/markdown",
        size: 5,
        text: "hello",
      },
    ];

    expect(buildResponsesInput("请分析附件", attachments)).toEqual([{
      role: "user",
      content: [
        { type: "input_text", text: "请分析附件" },
        { type: "input_image", image_url: "data:image/png;base64,AAAA", filename: "screen.png" },
        { type: "input_text", text: "\n\n<attachment name=\"notes.md\">\nhello\n</attachment>" },
      ],
    }]);
  });

  it("measures the encoded attachment payload after base64 and UTF-8 expansion", () => {
    const boundaryImage: ComposerAttachment = {
      id: "image-boundary",
      kind: "image",
      name: "large.png",
      mimeType: "image/png",
      size: 1_120_000,
      dataUrl: `data:image/png;base64,${"A".repeat(MAX_COMPOSER_ATTACHMENT_BYTES)}`,
    };
    const unicodeText: ComposerAttachment = {
      id: "text-unicode",
      kind: "text",
      name: "中文.md",
      mimeType: "text/markdown",
      size: 2,
      text: "界面",
    };

    expect(encodedComposerAttachmentsBytes([boundaryImage])).toBeGreaterThan(
      MAX_COMPOSER_ATTACHMENT_BYTES,
    );
    expect(encodedComposerAttachmentsBytes([unicodeText])).toBeGreaterThan(
      JSON.stringify(buildResponsesInput("", [unicodeText])).length,
    );
  });
});
