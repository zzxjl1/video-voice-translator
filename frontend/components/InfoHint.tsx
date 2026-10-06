import React, { useLayoutEffect, useRef, useState } from 'react';

/**
 * A small circled "i" that explains one control.
 *
 * Replaces the native `title` attribute, which was the wrong tool: it waits
 * 1–2 s, cannot be styled, and is unreachable by touch — so on a phone every
 * explanation in the style panel simply did not exist. It was also attached to
 * the range inputs, so dragging a slider made the bubble re-appear and follow
 * the pointer.
 *
 * Opens on hover, focus and CLICK. The click matters: without it the hint stays
 * unreachable on touch, which is where `title` failed first.
 *
 * ---------------------------------------------------------------------------
 * USE THIS SPARINGLY. Explanations come in two kinds and they need OPPOSITE
 * treatment:
 *
 *   - A DEFINITION ("bilingual means both in one line") may be hidden behind an
 *     icon. Nothing is broken while it is collapsed.
 *   - A REASON something is unavailable or will be ignored must NOT be hidden.
 *     A control that will not do what it says, and does not say why, reads as
 *     broken. Those stay INLINE — see SeparationModeSelector, which prints why
 *     a backend is greyed out rather than tucking it into a hover. (The style
 *     panel used to carry the same kind of banner and no longer does: see the
 *     note at the top of SubtitleStylePanel.)
 *
 * Only the first kind gets an icon, and only on the rows where the label alone
 * genuinely misleads. An "i" on every row is noise, not thoroughness.
 * ---------------------------------------------------------------------------
 */

/**
 * The bubble's width in px. MUST match the `w-44` class below.
 *
 * A constant rather than a measurement, because measuring the bubble does not
 * work here: `useLayoutEffect` runs synchronously after the DOM update, while
 * the stylesheet (Tailwind, applied asynchronously) has not reached the freshly
 * inserted node yet. Measuring then returns the width of an UNSTYLED inline
 * span — small — so the clamp concludes there is nothing to move and the bubble
 * overhangs anyway. Verified: with the measurement in place the code reported a
 * shift of 0 for a bubble that reached 16 px outside its container.
 */
const BUBBLE_WIDTH = 176;

/** Room the bubble needs above/below, in px, to fit without being cut. */
const BUBBLE_ROOM = 64;

/** Inset kept from the clipping edge. */
const MARGIN = 4;

interface ClipBox {
  top: number;
  bottom: number;
  left: number;
  right: number;
}

/**
 * The nearest ancestor that clips, in viewport coordinates.
 *
 * The style panel scrolls (`overflow-y: auto`, which makes `overflow-x`
 * compute to `auto` too), so anything reaching past it is CUT OFF — and
 * because an absolutely positioned child adds no scroll size, it cannot be
 * scrolled into view either. Bubbles have to stay inside this box.
 */
function clipBox(from: HTMLElement): ClipBox {
  for (let el = from.parentElement; el; el = el.parentElement) {
    const s = getComputedStyle(el);
    if (s.overflowX !== 'visible' || s.overflowY !== 'visible') {
      const r = el.getBoundingClientRect();
      return {
        top: r.top + MARGIN,
        bottom: r.bottom - MARGIN,
        left: r.left + MARGIN,
        right: r.right - MARGIN,
      };
    }
  }
  return {
    top: MARGIN,
    bottom: window.innerHeight - MARGIN,
    left: MARGIN,
    right: window.innerWidth - MARGIN,
  };
}

interface InfoHintProps {
  text: string;
  /**
   * Swallow the click. Set when this sits INSIDE an option button — otherwise
   * tapping the icon would also choose that option.
   */
  swallowClick?: boolean;
  /**
   * Act as a tab stop. Leave OFF when nested in a button: the button already
   * owns that tab stop and a nested one adds a second, confusing stop for the
   * same control.
   */
  standalone?: boolean;
}

const InfoHint: React.FC<InfoHintProps> = ({ text, swallowClick, standalone }) => {
  const [open, setOpen] = useState(false);
  const [dropBelow, setDropBelow] = useState(false);
  const [shiftX, setShiftX] = useState(0);
  const anchor = useRef<HTMLSpanElement>(null);

  const show = () => {
    const a = anchor.current;
    if (a) {
      /*
       * Compare against the CLIPPING BOX, not the viewport. The panel can be
       * scrolled so that a row near the top of the content sits in the middle
       * of the screen; a viewport-based test would then happily place the
       * bubble above it, outside the scroll container, where it is invisible.
       */
      setDropBelow(a.getBoundingClientRect().top - clipBox(a).top < BUBBLE_ROOM);
    }
    setOpen(true);
  };

  /* Horizontal clamp: centring on the icon alone overhangs the container for
   * the leftmost option (measured, not guessed). Runs after the bubble is in
   * the DOM so the nudge is applied before the first paint. */
  useLayoutEffect(() => {
    if (!open) return;
    const a = anchor.current;
    if (!a) return;

    const box = clipBox(a);
    const ar = a.getBoundingClientRect();
    const centre = ar.left + ar.width / 2;
    const half = BUBBLE_WIDTH / 2;

    if (centre - half < box.left) setShiftX(box.left - (centre - half));
    else if (centre + half > box.right) setShiftX(box.right - (centre + half));
    else setShiftX(0);
  }, [open]);

  return (
    <span
      ref={anchor}
      className="relative inline-flex items-center shrink-0"
      onMouseEnter={show}
      onMouseLeave={() => setOpen(false)}
    >
      <span
        role="button"
        // Not a tab stop when nested in a button; the button already is one.
        tabIndex={standalone ? 0 : -1}
        aria-label={text}
        onClick={e => {
          if (swallowClick) {
            e.preventDefault();
            e.stopPropagation();
          }
          setOpen(v => !v);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onKeyDown={e => {
          if (e.key === 'Escape') setOpen(false);
        }}
        /*
         * Low contrast by default: a dozen of these at full strength would
         * speckle the panel. They read as texture until you look for one.
         */
        className={[
          'flex items-center justify-center w-[13px] h-[13px] rounded-full border',
          'text-[8px] font-bold leading-none cursor-help select-none transition-colors',
          open
            ? 'border-gray-400 text-gray-600 bg-gray-200'
            : 'border-gray-300 text-gray-400 bg-white',
        ].join(' ')}
      >
        i
      </span>

      {open && (
        <span
          role="tooltip"
          /*
           * Centred on the icon, then nudged back inside if it would overhang.
           * `pointer-events-none` so the bubble cannot steal the mouseleave
           * that closes it.
           */
          style={{ transform: `translateX(calc(-50% + ${shiftX}px))` }}
          className={[
            'absolute left-1/2 z-30 w-44 px-2 py-1.5 rounded-lg',
            'bg-gray-900/95 text-white text-[10px] font-normal leading-snug text-left',
            'shadow-lg pointer-events-none',
            dropBelow ? 'top-full mt-1.5' : 'bottom-full mb-1.5',
          ].join(' ')}
        >
          {text}
        </span>
      )}
    </span>
  );
};

export default InfoHint;
