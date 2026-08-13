import { FileCode2, FileUp, RefreshCw, X } from "lucide-react";
import { useDropzone, type Accept, type FileRejection } from "react-dropzone";

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(size < 10 * 1024 ? 1 : 0)} KiB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MiB`;
}

function rejectionMessage(rejection: FileRejection): string {
  const code = rejection.errors[0]?.code;
  if (code === "file-invalid-type") return "文件类型不受支持，请重新选择。";
  if (code === "file-too-large") return "文件超过允许大小，请重新选择。";
  if (code === "too-many-files") return "一次只能选择一个文件。";
  return rejection.errors[0]?.message || "文件无法读取，请重新选择。";
}

export function FileDropzone({
  accept,
  maxSize,
  file,
  onFile,
  onError,
  ariaLabel = "选择文件",
  hint = "拖放文件到这里，或点击选择",
}: {
  accept: Accept;
  maxSize: number;
  file: File | null;
  onFile: (file: File | null) => void;
  onError: (message: string) => void;
  ariaLabel?: string;
  hint?: string;
}) {
  const {
    getRootProps,
    getInputProps,
    isDragActive,
    isDragReject,
    open,
  } = useDropzone({
    accept,
    maxSize,
    multiple: false,
    noClick: Boolean(file),
    onDropAccepted: accepted => {
      onError("");
      onFile(accepted[0] || null);
    },
    onDropRejected: rejected => {
      onFile(null);
      onError(rejectionMessage(rejected[0]));
    },
  });

  return (
    <div
      {...getRootProps({
        className: `studio-file-dropzone${isDragActive ? " dragging" : ""}${isDragReject ? " rejected" : ""}${file ? " has-file" : ""}`,
      })}
    >
      <input {...getInputProps({ "aria-label": ariaLabel })} />
      {file ? (
        <>
          <span className="studio-file-icon"><FileCode2 size={20} /></span>
          <span className="studio-file-copy">
            <strong title={file.name}>{file.name}</strong>
            <small>{formatBytes(file.size)} · 已准备检查</small>
          </span>
          <span className="studio-file-actions">
            <button
              className="icon-button tertiary"
              type="button"
              aria-label={`替换 ${file.name}`}
              title="替换文件"
              onClick={event => { event.stopPropagation(); open(); }}
            >
              <RefreshCw size={14} />
            </button>
            <button
              className="icon-button tertiary"
              type="button"
              aria-label={`移除 ${file.name}`}
              title="移除文件"
              onClick={event => { event.stopPropagation(); onFile(null); }}
            >
              <X size={15} />
            </button>
          </span>
        </>
      ) : (
        <>
          <span className="studio-file-icon"><FileUp size={20} /></span>
          <span className="studio-file-copy">
            <strong>{isDragActive ? "松开即可选择" : hint}</strong>
            <small>文件只会先做只读检查，确认后才写入 Catalog。</small>
          </span>
        </>
      )}
    </div>
  );
}
