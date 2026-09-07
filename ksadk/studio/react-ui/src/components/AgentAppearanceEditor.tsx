import { useEffect, useRef, useState } from "react";
import Cropper, { type Area } from "react-easy-crop";
import { Bot, Code2, ImagePlus, Network, Search, Sparkles, Trash2 } from "lucide-react";
import { apiFetch } from "../api";
import { AgentAvatar, type AgentAppearance } from "./AgentAvatar";
import { StudioDialog } from "./ui/StudioDialog";

const ICONS = [
  { id: "bot", label: "Bot", icon: Bot },
  { id: "sparkles", label: "Sparkles", icon: Sparkles },
  { id: "search", label: "Search", icon: Search },
  { id: "code", label: "Code", icon: Code2 },
  { id: "workflow", label: "Workflow", icon: Network },
] as const;

const COLORS = [
  { value: "#426ea8", label: "云蓝" },
  { value: "#7c5cc4", label: "紫罗兰" },
  { value: "#2d7c68", label: "松石绿" },
  { value: "#a86d32", label: "琥珀" },
  { value: "#a55267", label: "玫瑰" },
  { value: "#526173", label: "石墨" },
] as const;

function normalizeAppearance(appearance?: AgentAppearance): Required<AgentAppearance> {
  return {
    icon: appearance?.icon || "bot",
    color: appearance?.color || "#426ea8",
    imageUrl: appearance?.imageUrl || null,
  };
}

async function loadImage(source: string): Promise<HTMLImageElement> {
  const image = new Image();
  image.decoding = "async";
  image.src = source;
  await image.decode();
  return image;
}

async function cropToWebp(source: string, area: Area): Promise<Blob> {
  const image = await loadImage(source);
  const canvas = document.createElement("canvas");
  canvas.width = 512;
  canvas.height = 512;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("当前浏览器无法创建头像画布");
  context.imageSmoothingEnabled = true;
  context.imageSmoothingQuality = "high";
  context.drawImage(
    image,
    area.x,
    area.y,
    area.width,
    area.height,
    0,
    0,
    canvas.width,
    canvas.height,
  );
  const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, "image/webp", 0.9));
  if (!blob) throw new Error("头像裁剪失败，请更换图片后重试");
  return blob;
}

export function AgentAppearanceEditor({
  name,
  appearance,
  disabled = false,
  onSave,
}: {
  name: string;
  appearance?: AgentAppearance;
  disabled?: boolean;
  onSave: (appearance: Required<AgentAppearance>) => Promise<void>;
}) {
  const [draft, setDraft] = useState(() => normalizeAppearance(appearance));
  const [sourceUrl, setSourceUrl] = useState<string | null>(null);
  const [crop, setCrop] = useState({ x: 0, y: 0 });
  const [zoom, setZoom] = useState(1);
  const [cropArea, setCropArea] = useState<Area | null>(null);
  const [uploading, setUploading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => setDraft(normalizeAppearance(appearance)), [appearance]);
  useEffect(() => () => {
    if (sourceUrl) URL.revokeObjectURL(sourceUrl);
  }, [sourceUrl]);

  const persisted = normalizeAppearance(appearance);
  const dirty = draft.icon !== persisted.icon
    || draft.color !== persisted.color
    || draft.imageUrl !== persisted.imageUrl;

  function selectFile(file?: File) {
    setError("");
    if (!file) return;
    if (!(["image/png", "image/webp"] as string[]).includes(file.type)) {
      setError("仅支持 PNG 或 WebP 图片");
      return;
    }
    if (file.size > 2 * 1024 * 1024) {
      setError("头像文件不能超过 2 MiB");
      return;
    }
    setCrop({ x: 0, y: 0 });
    setZoom(1);
    setCropArea(null);
    setSourceUrl(URL.createObjectURL(file));
  }

  async function applyCrop() {
    if (!sourceUrl || !cropArea || uploading) return;
    setUploading(true);
    setError("");
    try {
      const blob = await cropToWebp(sourceUrl, cropArea);
      const response = await apiFetch("/api/v1/assets/agent-avatars", {
        method: "POST",
        headers: { "Content-Type": blob.type },
        body: blob,
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error(payload?.error?.message || `头像上传失败（${response.status}）`);
      setDraft(current => ({ ...current, imageUrl: payload.url }));
      setSourceUrl(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "头像上传失败");
    } finally {
      setUploading(false);
    }
  }

  async function saveAppearance() {
    if (!dirty || saving || disabled) return;
    setSaving(true);
    setError("");
    try {
      await onSave(draft);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "外观保存失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="agent-appearance-editor" aria-label="Agent 外观">
      <div className="agent-appearance-preview">
        <AgentAvatar name={name} appearance={draft} size="lg" />
        <div><strong>Agent 外观</strong><span>用于列表、会话和 Trace；不会写入模型提示词。</span></div>
      </div>
      <div className="agent-appearance-controls">
        <div className="appearance-choice-group" role="group" aria-label="头像图标">
          <span>图标</span>
          <div>
            {ICONS.map(item => (
              <button
                key={item.id}
                className={draft.icon === item.id && !draft.imageUrl ? "active" : ""}
                type="button"
                aria-label={`使用 ${item.label} 图标`}
                aria-pressed={draft.icon === item.id && !draft.imageUrl}
                disabled={disabled}
                onClick={() => setDraft(current => ({ ...current, icon: item.id, imageUrl: null }))}
              >
                <item.icon size={16} />
              </button>
            ))}
          </div>
        </div>
        <div className="appearance-choice-group color" role="group" aria-label="头像配色">
          <span>配色</span>
          <div>
            {COLORS.map(item => (
              <button
                key={item.value}
                className={draft.color === item.value ? "active" : ""}
                type="button"
                aria-label={`使用${item.label}配色`}
                aria-pressed={draft.color === item.value}
                disabled={disabled}
                style={{ "--appearance-swatch": item.value } as React.CSSProperties}
                onClick={() => setDraft(current => ({ ...current, color: item.value }))}
              />
            ))}
          </div>
        </div>
      </div>
      <div className="agent-appearance-actions">
        <input
          ref={inputRef}
          className="sr-only"
          type="file"
          accept="image/png,image/webp"
          aria-label="选择 Agent 头像图片"
          disabled={disabled}
          onChange={event => { selectFile(event.target.files?.[0]); event.target.value = ""; }}
        />
        <button className="button tertiary small" type="button" disabled={disabled} onClick={() => inputRef.current?.click()}>
          <ImagePlus size={14} /><span>上传图片</span>
        </button>
        {draft.imageUrl ? (
          <button className="button tertiary small" type="button" disabled={disabled} onClick={() => setDraft(current => ({ ...current, imageUrl: null }))}>
            <Trash2 size={14} /><span>移除图片</span>
          </button>
        ) : null}
        <button className="button secondary small" type="button" disabled={disabled || saving || !dirty} onClick={saveAppearance}>
          {saving ? "正在保存" : "保存外观"}
        </button>
      </div>
      {error ? <p className="studio-field-error" role="alert">{error}</p> : null}

      <StudioDialog
        open={Boolean(sourceUrl)}
        onOpenChange={open => { if (!open && !uploading) setSourceUrl(null); }}
        title="调整 Agent 头像"
        description="拖动画面并缩放，保存后会生成 512 × 512 WebP。"
        closeDisabled={uploading}
        className="avatar-crop-dialog"
        footer={(
          <>
            <button className="button tertiary" type="button" disabled={uploading} onClick={() => setSourceUrl(null)}>取消</button>
            <button className="button accent" type="button" disabled={uploading || !cropArea} onClick={applyCrop}>{uploading ? "正在上传" : "使用裁剪"}</button>
          </>
        )}
      >
        {sourceUrl ? (
          <>
            <div className="avatar-crop-stage">
              <Cropper
                image={sourceUrl}
                crop={crop}
                zoom={zoom}
                aspect={1}
                showGrid={false}
                onCropChange={setCrop}
                onZoomChange={setZoom}
                onCropComplete={(_, pixels) => setCropArea(pixels)}
              />
            </div>
            <label className="avatar-zoom-control">
              <span>缩放</span>
              <input type="range" min={1} max={3} step={0.05} value={zoom} onChange={event => setZoom(Number(event.target.value))} />
            </label>
          </>
        ) : null}
      </StudioDialog>
    </section>
  );
}
