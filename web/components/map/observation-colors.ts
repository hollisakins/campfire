// Per-observation color palette for map shutters and filter chips.
// Extracted to a standalone file so it can be imported without pulling in Leaflet.

export const OBSERVATION_COLORS = [
  '#00ff00', // lime green
  '#00ccff', // cyan
  '#ff6600', // orange
  '#ff00ff', // magenta
  '#ffff00', // yellow
  '#00ffcc', // teal
  '#ff3399', // pink
  '#66ff33', // chartreuse
];

export function getObservationColor(observation: string, observations: string[]): string {
  const idx = observations.indexOf(observation);
  return OBSERVATION_COLORS[idx % OBSERVATION_COLORS.length];
}
