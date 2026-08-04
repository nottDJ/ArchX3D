"use client";

import { useCallback, useId, useRef, useState } from "react";

import { UploadIcon } from "./icons";

export interface DropZoneProps {
  /** `accept` attribute and the extension filter applied to dropped files. */
  accept: string;
  multiple?: boolean;
  disabled?: boolean;
  title: string;
  hint: string;
  onFiles: (files: File[]) => void;
}

/**
 * Drag-and-drop plus multi-select file input.
 *
 * Drag events fire for every nested element, so a plain `dragleave` handler
 * flickers the highlight as the cursor crosses children. A depth counter is
 * the standard fix and keeps the affordance steady.
 */
export function DropZone({
  accept,
  multiple = false,
  disabled = false,
  title,
  hint,
  onFiles,
}: DropZoneProps) {
  const inputId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const dragDepth = useRef(0);
  const [active, setActive] = useState(false);

  const extensions = accept
    .split(",")
    .map((value) => value.trim().toLowerCase())
    .filter((value) => value.startsWith("."));

  const filter = useCallback(
    (files: File[]) => {
      if (extensions.length === 0) return files;
      return files.filter((file) =>
        extensions.some((extension) => file.name.toLowerCase().endsWith(extension)),
      );
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [accept],
  );

  const handleDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      dragDepth.current = 0;
      setActive(false);
      if (disabled) return;

      const dropped = filter(Array.from(event.dataTransfer.files));
      if (dropped.length > 0) onFiles(multiple ? dropped : dropped.slice(0, 1));
    },
    [disabled, filter, multiple, onFiles],
  );

  return (
    <div
      onDragEnter={(event) => {
        event.preventDefault();
        dragDepth.current += 1;
        if (!disabled) setActive(true);
      }}
      onDragOver={(event) => event.preventDefault()}
      onDragLeave={(event) => {
        event.preventDefault();
        dragDepth.current -= 1;
        if (dragDepth.current <= 0) setActive(false);
      }}
      onDrop={handleDrop}
      className={[
        "relative rounded-2xl border border-dashed p-8 text-center transition-colors duration-200",
        disabled
          ? "cursor-not-allowed border-line-subtle bg-surface opacity-50"
          : active
            ? "border-accent-border bg-accent-surface"
            : "border-white/15 bg-surface hover:border-line-strong",
      ].join(" ")}
    >
      <input
        ref={inputRef}
        id={inputId}
        type="file"
        accept={accept}
        multiple={multiple}
        disabled={disabled}
        className="sr-only"
        onChange={(event) => {
          const chosen = Array.from(event.target.files ?? []);
          if (chosen.length > 0) onFiles(chosen);
          // Reset so selecting the same file twice still fires a change.
          event.target.value = "";
        }}
      />

      <div className="flex flex-col items-center gap-3">
        <span
          className={[
            "flex h-11 w-11 items-center justify-center rounded-xl border transition-colors",
            active
              ? "border-accent-border bg-accent-surface text-accent-text"
              : "border-line bg-surface text-secondary",
          ].join(" ")}
        >
          <UploadIcon className="h-5 w-5" />
        </span>

        <div>
          <p className="text-sm font-medium text-primary">{title}</p>
          <p className="mt-1 text-xs text-tertiary">{hint}</p>
        </div>

        <label
          htmlFor={inputId}
          className={[
            "mt-1 rounded-lg px-4 py-2 text-sm font-medium transition-transform",
            disabled
              ? "cursor-not-allowed bg-surface-active text-tertiary"
              : "cursor-pointer bg-white text-on-solid hover:-translate-y-px active:translate-y-0",
          ].join(" ")}
        >
          {multiple ? "Choose files" : "Choose file"}
        </label>

        <p className="text-[11px] text-tertiary">
          or drag {multiple ? "them" : "it"} here
        </p>
      </div>
    </div>
  );
}
