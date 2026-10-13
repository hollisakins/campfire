'use client';

import { useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { Maximize2, Minimize2 } from 'lucide-react';
import type { PlotParams } from 'react-plotly.js';
import { LazyPlot } from './LazyPlot';

/** A non-modal inline dialog promoted to the browser's top layer on demand.
 * Keeping the same DOM tree preserves Plotly's zoom and the controls' state.
 * showModal supplies focus containment and makes the page behind it inert. */
export function FullscreenPlot({
  label,
  className = '',
  children,
}: {
  label: string;
  className?: string;
  children: (fullscreen: boolean, fullscreenButton: ReactNode) => ReactNode;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const [fullscreen, setFullscreen] = useState(false);

  useLayoutEffect(() => {
    const dialog = dialogRef.current!;
    if (!fullscreen) {
      if (!dialog.open) {
        dialog.show();
        toggleRef.current?.focus({ preventScroll: true });
      }
      return;
    }

    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.close();
    dialog.showModal();
    toggleRef.current?.focus({ preventScroll: true });
    return () => {
      dialog.close();
      document.body.style.overflow = overflow;
    };
  }, [fullscreen]);

  return (
    <dialog
      ref={dialogRef}
      open
      aria-label={label}
      aria-modal={fullscreen || undefined}
      role={fullscreen ? 'dialog' : 'region'}
      onCancel={(event) => {
        event.preventDefault();
        setFullscreen(false);
      }}
      onKeyDown={(event) => {
        // Don't let Escape also dismiss an enclosing inspection overlay.
        if (fullscreen && event.key === 'Escape') event.stopPropagation();
      }}
      className={`m-0 max-w-none max-h-none p-0 text-text-primary bg-card ${fullscreen
        ? 'fixed inset-0 flex h-[100dvh] w-screen flex-col overflow-auto border-0'
        : `static block w-full border-0 ${className}`}`}
    >
      {children(fullscreen,
        <button
          ref={toggleRef}
          type="button"
          onClick={() => setFullscreen(value => !value)}
          aria-expanded={fullscreen}
          aria-label={fullscreen ? 'Exit full screen' : 'Full screen'}
          title={fullscreen ? 'Exit full screen (Esc)' : 'View plot full screen'}
          className="absolute right-3 top-2 inline-flex items-center justify-center rounded p-1.5 text-text-secondary hover:bg-card-hover hover:text-text-primary focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary"
        >
          {fullscreen ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
        </button>
      )}
    </dialog>
  );
}

/** Plotly's window resize handler doesn't notice an element changing size.
 * Observe the plot slot, including changes from wrapped controls/model panels. */
export function ResizablePlot({ className, ...props }: PlotParams & { className?: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState<{ width: number; height: number }>();

  useLayoutEffect(() => {
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) {
        setSize(previous => previous?.width === width && previous.height === height
          ? previous : { width, height });
      }
    });
    observer.observe(containerRef.current!);
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={containerRef} className={className}>
      <LazyPlot {...props} layout={{ ...props.layout, ...size }} style={{ width: '100%', height: '100%' }} />
    </div>
  );
}
