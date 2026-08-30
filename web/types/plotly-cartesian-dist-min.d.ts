// The partial dist bundles ship no types; they expose the same API surface
// as the full plotly.js package, so reuse its type definitions.
declare module 'plotly.js-cartesian-dist-min' {
  import * as Plotly from 'plotly.js';
  export default Plotly;
}
