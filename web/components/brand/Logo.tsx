import * as React from "react";

/**
 * Campfire logo — JWST 18-segment mirror (open centre) + ember flame.
 *
 * Colours are driven entirely by CSS custom properties so ONE component
 * adapts to light mode, dark mode, and the always-dark header bar.
 * See globals.css for the `--logo-*` variable definitions.
 *
 *   --logo-flame           flame fill  (accent)
 *   --logo-halo            flame outline — pin to the surface BEHIND the mark
 *   --logo-segment-stroke  mirror segment outline
 *   --logo-segment-fill    mirror segment fill (translucent)
 */

const SEGMENTS: string[] = [
  "M31.81,8L40.91,13.25L40.91,23.75L31.81,29L22.72,23.75L22.72,13.25L31.81,8Z",
  "M50,8L59.09,13.25L59.09,23.75L50,29L40.91,23.75L40.91,13.25L50,8Z",
  "M68.19,8L77.28,13.25L77.28,23.75L68.19,29L59.09,23.75L59.09,13.25L68.19,8Z",
  "M22.72,23.75L31.81,29L31.81,39.5L22.72,44.75L13.63,39.5L13.63,29L22.72,23.75Z",
  "M40.91,23.75L50,29L50,39.5L40.91,44.75L31.81,39.5L31.81,29L40.91,23.75Z",
  "M59.09,23.75L68.19,29L68.19,39.5L59.09,44.75L50,39.5L50,29L59.09,23.75Z",
  "M77.28,23.75L86.37,29L86.37,39.5L77.28,44.75L68.19,39.5L68.19,29L77.28,23.75Z",
  "M13.63,39.5L22.72,44.75L22.72,55.25L13.63,60.5L4.53,55.25L4.53,44.75L13.63,39.5Z",
  "M31.81,39.5L40.91,44.75L40.91,55.25L31.81,60.5L22.72,55.25L22.72,44.75L31.81,39.5Z",
  "M68.19,39.5L77.28,44.75L77.28,55.25L68.19,60.5L59.09,55.25L59.09,44.75L68.19,39.5Z",
  "M86.37,39.5L95.47,44.75L95.47,55.25L86.37,60.5L77.28,55.25L77.28,44.75L86.37,39.5Z",
  "M22.72,55.25L31.81,60.5L31.81,71L22.72,76.25L13.63,71L13.63,60.5L22.72,55.25Z",
  "M40.91,55.25L50,60.5L50,71L40.91,76.25L31.81,71L31.81,60.5L40.91,55.25Z",
  "M59.09,55.25L68.19,60.5L68.19,71L59.09,76.25L50,71L50,60.5L59.09,55.25Z",
  "M77.28,55.25L86.37,60.5L86.37,71L77.28,76.25L68.19,71L68.19,60.5L77.28,55.25Z",
  "M31.81,71L40.91,76.25L40.91,86.75L31.81,92L22.72,86.75L22.72,76.25L31.81,71Z",
  "M50,71L59.09,76.25L59.09,86.75L50,92L40.91,86.75L40.91,76.25L50,71Z",
  "M68.19,71L77.28,76.25L77.28,86.75L68.19,92L59.09,86.75L59.09,76.25L68.19,71Z",
];

const FLAME =
  "M62,12C64,33 86,41 86,62C86,81 73,94 57,94C41,94 30,81 30,65C30,54 36,47 44,44C44,52 49,57 55,55C48,47 51,30 62,12Z";

export interface LogoProps extends React.SVGProps<SVGSVGElement> {
  /** px size (width & height). Default 32. */
  size?: number | string;
  /** Accessible label; set to "" for a decorative mark (aria-hidden). */
  title?: string;
}

export function Logo({ size = 32, title = "Campfire", ...rest }: LogoProps) {
  const decorative = title === "";
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 512 512"
      xmlns="http://www.w3.org/2000/svg"
      role={decorative ? undefined : "img"}
      aria-label={decorative ? undefined : title}
      aria-hidden={decorative ? true : undefined}
      {...rest}
    >
      {!decorative && <title>{title}</title>}
      {/* mirror */}
      <g
        transform="matrix(5.12,0,0,5.12,0,0)"
        fill="var(--logo-segment-fill)"
        stroke="var(--logo-segment-stroke)"
        strokeWidth={1.5}
        strokeLinejoin="round"
      >
        {SEGMENTS.map((d, i) => (
          <path key={i} d={d} fillRule="nonzero" />
        ))}
      </g>
      {/* flame */}
      <g transform="matrix(5.12,0,0,5.12,27.6332,14.3245)">
        <path
          d={FLAME}
          fill="var(--logo-flame)"
          stroke="var(--logo-halo)"
          strokeWidth={1.5}
          strokeLinejoin="round"
          fillRule="nonzero"
        />
      </g>
    </svg>
  );
}

export default Logo;
