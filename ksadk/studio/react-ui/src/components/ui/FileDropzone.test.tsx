import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { FileDropzone } from "./FileDropzone";

function Harness({ onError = () => undefined }: { onError?: (message: string) => void }) {
  const [file, setFile] = useState<File | null>(null);
  return (
    <FileDropzone
      ariaLabel="选择 Python 文件"
      accept={{ "text/x-python": [".py"] }}
      maxSize={1024}
      file={file}
      onFile={setFile}
      onError={onError}
    />
  );
}

describe("FileDropzone", () => {
  it("shows an accepted file and supports removal", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const input = screen.getByLabelText("选择 Python 文件");
    await user.upload(input, new File(["def run():\n    pass\n"], "tool.py", { type: "text/x-python" }));
    expect(screen.getByText("tool.py")).toBeVisible();
    expect(screen.getByText(/B/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "移除 tool.py" }));
    expect(screen.queryByText("tool.py")).not.toBeInTheDocument();
  });

  it("reports rejected file types", async () => {
    const onError = vi.fn();
    render(<Harness onError={onError} />);
    fireEvent.change(screen.getByLabelText("选择 Python 文件"), {
      target: { files: [new File(["plain"], "notes.txt", { type: "text/plain" })] },
    });
    await waitFor(() => expect(onError).toHaveBeenCalledWith(expect.stringContaining("文件类型")));
  });
});
