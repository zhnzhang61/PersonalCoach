"use client";

import { useState, type KeyboardEvent } from "react";
import { Send } from "lucide-react";

interface Props {
  onSubmit: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
}

export function ChatInput({ onSubmit, disabled = false, placeholder }: Props) {
  const [value, setValue] = useState("");

  const submit = () => {
    const t = value.trim();
    if (!t || disabled) return;
    setValue("");
    onSubmit(t);
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends, Shift+Enter inserts newline. Mobile keyboards send
    // an "Enter" event for the return key, so this works on phones.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="flex items-end gap-2">
      <textarea
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={placeholder ?? "ask anything…"}
        // `rows={3}` + min-h-[88px] gives the user three visible lines
        // up front — typing felt cramped at the previous 1-line height.
        // text-base (16px) is intentional: iOS Safari auto-zooms on
        // focus when an input's font-size is < 16px, and that zoom
        // also disrupts the long-press → "Paste" menu, which is the
        // surface the user uses to paste text into the textarea. So
        // 14 → 16 fixes the "can't paste" complaint in the same edit
        // that fixes the "too small" complaint.
        rows={3}
        disabled={disabled}
        className="min-h-[88px] max-h-[200px] flex-1 resize-none rounded-xl border border-border bg-background px-3 py-2 text-base leading-relaxed shadow-sm focus:outline-none focus:ring-2 focus:ring-warm-accent/40 disabled:opacity-50"
      />
      <button
        type="button"
        onClick={submit}
        disabled={disabled || !value.trim()}
        // size-11 (44px) hits the iOS HIG minimum touch target — the
        // old size-10 (40px) was slightly under. Keep it aligned to
        // the bottom via the parent's `items-end`.
        className="flex size-11 shrink-0 items-center justify-center rounded-xl bg-foreground text-background transition-colors hover:bg-foreground/90 disabled:opacity-40"
        aria-label="Send"
      >
        <Send className="size-5" />
      </button>
    </div>
  );
}
