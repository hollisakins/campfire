'use client';

import React, { useState, useRef, useEffect } from 'react';
import { EmojiPicker } from 'frimousse';

interface ListEmojiPickerProps {
  value: string | null;
  onChange: (emoji: string | null) => void;
}

export function ListEmojiPicker({ value, onChange }: ListEmojiPickerProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function handleClickOutside(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [open]);

  return (
    <div ref={containerRef} className="relative">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => setOpen(!open)}
          className="w-10 h-10 rounded-md border border-border dark:border-slate-600 bg-background dark:bg-slate-700 flex items-center justify-center text-lg hover:bg-gray-50 dark:hover:bg-slate-600 transition-colors"
        >
          {value || <span className="text-text-secondary dark:text-slate-500 text-sm">+</span>}
        </button>
        {value && (
          <button
            type="button"
            onClick={() => onChange(null)}
            className="text-[11px] text-text-secondary dark:text-slate-400 hover:text-red-500 dark:hover:text-red-400"
          >
            Clear
          </button>
        )}
      </div>

      {open && (
        <div className="absolute z-50 top-12 left-0 w-[320px] h-[360px] rounded-lg border border-border dark:border-slate-600 bg-card dark:bg-slate-800 shadow-lg overflow-hidden">
          <EmojiPicker.Root
            onEmojiSelect={(emoji) => {
              onChange(emoji.emoji);
              setOpen(false);
            }}
            className="flex flex-col h-full"
          >
            <EmojiPicker.Search
              className="w-full px-3 py-2 text-sm border-b border-border dark:border-slate-600 bg-background dark:bg-slate-700 text-text-primary dark:text-slate-100 outline-none placeholder:text-text-secondary dark:placeholder:text-slate-500"
              placeholder="Search emoji..."
              autoFocus
            />
            <EmojiPicker.Viewport className="flex-1 overflow-y-auto p-1">
              <EmojiPicker.Loading>
                <div className="flex items-center justify-center h-full text-xs text-text-secondary dark:text-slate-400">
                  Loading...
                </div>
              </EmojiPicker.Loading>
              <EmojiPicker.Empty>
                <div className="flex items-center justify-center h-full text-xs text-text-secondary dark:text-slate-400">
                  No emoji found
                </div>
              </EmojiPicker.Empty>
              <EmojiPicker.List
                className="select-none"
                components={{
                  CategoryHeader: ({ category, ...props }) => (
                    <div {...props} className="px-1 py-1.5 text-[10px] font-semibold text-text-secondary dark:text-slate-400 uppercase tracking-wider">
                      {category.label}
                    </div>
                  ),
                  Row: ({ children, ...props }) => (
                    <div {...props} className="flex">{children}</div>
                  ),
                  Emoji: ({ emoji, ...props }) => (
                    <button
                      type="button"
                      className={`w-8 h-8 flex items-center justify-center rounded cursor-pointer text-lg ${
                        emoji.isActive ? 'bg-gray-200 dark:bg-slate-500' : 'hover:bg-gray-100 dark:hover:bg-slate-600'
                      }`}
                      {...props}
                    >
                      {emoji.emoji}
                    </button>
                  ),
                }}
              />
            </EmojiPicker.Viewport>
          </EmojiPicker.Root>
        </div>
      )}
    </div>
  );
}
