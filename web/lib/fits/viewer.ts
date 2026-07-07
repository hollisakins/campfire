/**
 * Single-image WebGL2 FITS viewer (epic #261, N4).
 *
 * Renders one uncompressed 2048² SCI float32 array as an R32F texture with
 * on-the-fly min/max normalization, a selectable non-linear stretch, and a
 * colormap LUT — all in the fragment shader, so changing stretch / colormap /
 * display range never refetches or re-uploads pixel data. Pans and zooms
 * interactively. Deliberately NOT a tile-pyramid viewer (that is `fitsgl`'s
 * `FitsViewer`): a per-detector exposure fits in a single texture, so there is
 * no manifest, LOD, or tile machinery here.
 *
 * Pixel convention matches the pipeline's `origin='lower'` preview PNGs: FITS
 * data row 0 is painted at the bottom of the viewport, so existing DS9-image
 * mask coordinates stay valid when N5 ports the mask editor onto this canvas.
 */

import { createProgram, createUnitQuadVAO, createImageTexture, createColormapTexture } from './gl-util';
import { LOG_SOFTENING, ASINH_SOFTENING, STRETCH_MODE_IDS, type StretchMode } from './stretch';
import { resolveColormap, backgroundForColormap, type ColormapName } from './colormaps';
import { zscaleLimits } from './zscale';
import type { SciImage } from './fetch';

const VERT = `#version 300 es
layout(location = 0) in vec2 a_quad;   // unit quad, 0..1 (bottom-left .. top-right)
uniform vec2 u_p00;   // NDC corner at a_quad = (0, 0)
uniform vec2 u_p10;
uniform vec2 u_p01;
uniform vec2 u_p11;
out vec2 v_uv;
void main() {
  vec2 top = mix(u_p00, u_p10, a_quad.x);
  vec2 bot = mix(u_p01, u_p11, a_quad.x);
  gl_Position = vec4(mix(top, bot, a_quad.y), 0.0, 1.0);
  v_uv = a_quad;  // texture row 0 -> a_quad.y=0 -> screen bottom (origin='lower')
}`;

const FRAG = `#version 300 es
precision highp float;
uniform sampler2D u_tile;
uniform sampler2D u_colormap;
uniform int u_stretchMode;   // 0 linear, 1 log, 2 asinh, 4 sqrt
uniform float u_min;
uniform float u_max;
uniform vec3 u_bg;
in vec2 v_uv;
out vec4 outColor;
const float LOG_A = ${LOG_SOFTENING.toFixed(1)};
const float ASINH_A = ${ASINH_SOFTENING};
float applyStretch(float norm) {
  if (u_stretchMode == 1) return log(LOG_A * norm + 1.0) / log(LOG_A + 1.0);
  else if (u_stretchMode == 2) return asinh(norm / ASINH_A) / asinh(1.0 / ASINH_A);
  else if (u_stretchMode == 4) return sqrt(norm);
  return norm;
}
void main() {
  float v = texture(u_tile, v_uv).r;
  if (isnan(v)) { outColor = vec4(u_bg, 1.0); return; }
  float norm = clamp((v - u_min) / (u_max - u_min), 0.0, 1.0);
  float s = applyStretch(norm);
  outColor = vec4(texture(u_colormap, vec2(s, 0.5)).rgb, 1.0);
}`;

export interface PixelReadout {
  /** 1-indexed DS9 image coordinates (origin='lower'). */
  x: number;
  y: number;
  /** SCI value at that pixel, or NaN. */
  value: number;
}

export class FitsImageViewer {
  private gl: WebGL2RenderingContext;
  private program: WebGLProgram;
  private quad: { vao: WebGLVertexArrayObject; buffer: WebGLBuffer };
  private tileTex: WebGLTexture | null = null;
  private cmapTex: WebGLTexture;
  private loc: Record<string, WebGLUniformLocation | null> = {};

  private image: SciImage | null = null;
  private stretchMode: StretchMode = 'linear';
  private colormap: ColormapName = 'gray';
  private bg: [number, number, number] = [0, 0, 0];
  private vmin = 0;
  private vmax = 1;

  // camera: image-space centre at the viewport centre + zoom (1 = fit)
  private zoom = 1;
  private cx = 0;
  private cy = 0;

  private rafHandle = 0;
  private disposed = false;

  // interactive (default): internal pan/zoom camera, backing store = CSS×dpr.
  // static: backing store = the image's native pixel size and pan/zoom are
  // no-ops — the host CSS-transforms the canvas (so an SVG mask overlay drawn in
  // the same transformed container stays pixel-aligned; epic #261, N5).
  private interactive: boolean;

  constructor(private canvas: HTMLCanvasElement, opts?: { interactive?: boolean }) {
    this.interactive = opts?.interactive ?? true;
    const gl = canvas.getContext('webgl2', { antialias: false, premultipliedAlpha: false });
    if (!gl) throw new Error('WebGL2 is not available in this browser');
    // Sampling an R32F texture with NEAREST is core WebGL2 — no float-buffer
    // extension needed (that is only required to render *to* an R32F target).
    this.gl = gl;
    this.program = createProgram(gl, VERT, FRAG);
    this.quad = createUnitQuadVAO(gl);
    for (const name of ['u_p00', 'u_p10', 'u_p01', 'u_p11', 'u_tile', 'u_colormap', 'u_stretchMode', 'u_min', 'u_max', 'u_bg']) {
      this.loc[name] = gl.getUniformLocation(this.program, name);
    }
    const { rgba, size } = resolveColormap(this.colormap);
    this.cmapTex = createColormapTexture(gl, size, rgba);
    this.bg = backgroundForColormap(this.colormap);
    this.scheduleDraw();
  }

  setImage(image: SciImage): void {
    const gl = this.gl;
    if (this.tileTex) gl.deleteTexture(this.tileTex);
    this.tileTex = createImageTexture(gl, image.width, image.height, image.data);
    this.image = image;
    this.resetView();
    this.autoStretch();
  }

  setStretchMode(mode: StretchMode): void {
    this.stretchMode = mode;
    this.scheduleDraw();
  }

  getStretchMode(): StretchMode {
    return this.stretchMode;
  }

  setColormap(name: ColormapName): void {
    const gl = this.gl;
    this.colormap = name;
    const { rgba, size } = resolveColormap(name);
    gl.deleteTexture(this.cmapTex);
    this.cmapTex = createColormapTexture(gl, size, rgba);
    this.bg = backgroundForColormap(name);
    this.scheduleDraw();
  }

  getColormap(): ColormapName {
    return this.colormap;
  }

  setDisplayRange(vmin: number, vmax: number): void {
    this.vmin = vmin;
    this.vmax = vmax;
    this.scheduleDraw();
  }

  getDisplayRange(): [number, number] {
    return [this.vmin, this.vmax];
  }

  /** Recompute ZScale limits over the current image (matches the PNG preview). */
  autoStretch(): void {
    if (!this.image) return;
    const limits = zscaleLimits(this.image.data);
    if (limits) this.setDisplayRange(limits[0], limits[1]);
  }

  resetView(): void {
    if (this.image) {
      this.cx = this.image.width / 2;
      this.cy = this.image.height / 2;
    }
    this.zoom = 1;
    this.scheduleDraw();
  }

  // ---- interaction ---------------------------------------------------------

  private backingSize(): { w: number; h: number; s: number } {
    const w = this.canvas.width;
    const h = this.canvas.height;
    const img = this.image;
    if (!img) return { w, h, s: 1 };
    const fit = Math.min(w / img.width, h / img.height);
    return { w, h, s: fit * this.zoom };
  }

  /** Pan by a screen delta in CSS pixels (drag). No-op in static mode. */
  panByCss(dxCss: number, dyCss: number): void {
    if (!this.interactive) return;
    const dpr = this.canvas.width / Math.max(1, this.canvas.clientWidth);
    const { s } = this.backingSize();
    if (s <= 0) return;
    this.cx -= (dxCss * dpr) / s;
    this.cy += (dyCss * dpr) / s; // screen-down (dy>0) reveals lower image (origin='lower')
    this.scheduleDraw();
  }

  /** Zoom by `factor` about a cursor position given in CSS pixels within the canvas. */
  zoomAt(factor: number, cssX: number, cssY: number): void {
    if (!this.interactive || !this.image) return;
    const dpr = this.canvas.width / Math.max(1, this.canvas.clientWidth);
    const { w, h, s } = this.backingSize();
    if (s <= 0) return;
    const mx = cssX * dpr;
    const my = cssY * dpr;
    const ix = this.cx + (mx - w / 2) / s;
    const iy = this.cy + (h / 2 - my) / s;
    this.zoom = Math.min(64, Math.max(0.05, this.zoom * factor));
    const s2 = this.backingSize().s;
    this.cx = ix - (mx - w / 2) / s2;
    this.cy = iy - (h / 2 - my) / s2;
    this.scheduleDraw();
  }

  /** DS9-image coordinate + value under a cursor position (CSS px in-canvas), or null. */
  readoutAt(cssX: number, cssY: number): PixelReadout | null {
    const img = this.image;
    if (!img) return null;
    const dpr = this.canvas.width / Math.max(1, this.canvas.clientWidth);
    const { w, h, s } = this.backingSize();
    if (s <= 0) return null;
    const mx = cssX * dpr;
    const my = cssY * dpr;
    const ix = this.cx + (mx - w / 2) / s; // image x, left->right
    const iy = this.cy + (h / 2 - my) / s; // image y, bottom->top (origin='lower')
    const col = Math.floor(ix);
    const row = Math.floor(iy);
    if (col < 0 || col >= img.width || row < 0 || row >= img.height) return null;
    const value = img.data[row * img.width + col]!;
    return { x: col + 1, y: row + 1, value };
  }

  // ---- rendering -----------------------------------------------------------

  private scheduleDraw(): void {
    if (this.disposed || this.rafHandle) return;
    this.rafHandle = requestAnimationFrame(() => {
      this.rafHandle = 0;
      this.draw();
    });
  }

  private syncBackingStore(): void {
    // Static mode: the backing store is the image's native pixel grid, so one
    // texel maps to one canvas pixel and the host's CSS transform scales it (a
    // fit/zoom of 1 with the camera centred makes the quad fill the canvas).
    if (!this.interactive) {
      if (this.image) {
        if (this.canvas.width !== this.image.width || this.canvas.height !== this.image.height) {
          this.canvas.width = this.image.width;
          this.canvas.height = this.image.height;
        }
      }
      return;
    }
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const cssW = Math.max(1, this.canvas.clientWidth);
    const cssH = Math.max(1, this.canvas.clientHeight);
    const w = Math.round(cssW * dpr);
    const h = Math.round(cssH * dpr);
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
    }
  }

  private draw(): void {
    if (this.disposed) return;
    const gl = this.gl;
    this.syncBackingStore();
    gl.viewport(0, 0, this.canvas.width, this.canvas.height);
    gl.clearColor(this.bg[0], this.bg[1], this.bg[2], 1);
    gl.clear(gl.COLOR_BUFFER_BIT);
    if (!this.image || !this.tileTex) return;

    const { w, h, s } = this.backingSize();
    const ndc = (ix: number, iy: number): [number, number] => [
      ((ix - this.cx) * s * 2) / w,
      ((iy - this.cy) * s * 2) / h,
    ];
    const W = this.image.width;
    const H = this.image.height;

    gl.useProgram(this.program);
    gl.uniform2fv(this.loc.u_p00!, ndc(0, 0));
    gl.uniform2fv(this.loc.u_p10!, ndc(W, 0));
    gl.uniform2fv(this.loc.u_p01!, ndc(0, H));
    gl.uniform2fv(this.loc.u_p11!, ndc(W, H));
    gl.uniform1i(this.loc.u_stretchMode!, STRETCH_MODE_IDS[this.stretchMode]);
    gl.uniform1f(this.loc.u_min!, this.vmin);
    gl.uniform1f(this.loc.u_max!, this.vmax);
    gl.uniform3fv(this.loc.u_bg!, this.bg);

    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.tileTex);
    gl.uniform1i(this.loc.u_tile!, 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.cmapTex);
    gl.uniform1i(this.loc.u_colormap!, 1);

    gl.bindVertexArray(this.quad.vao);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    gl.bindVertexArray(null);
  }

  /** Force a redraw (e.g. after the container resized). */
  invalidate(): void {
    this.scheduleDraw();
  }

  destroy(): void {
    if (this.disposed) return;
    this.disposed = true;
    if (this.rafHandle) cancelAnimationFrame(this.rafHandle);
    const gl = this.gl;
    if (this.tileTex) gl.deleteTexture(this.tileTex);
    gl.deleteTexture(this.cmapTex);
    gl.deleteBuffer(this.quad.buffer);
    gl.deleteVertexArray(this.quad.vao);
    gl.deleteProgram(this.program);
  }
}
