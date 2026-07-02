/**
 * Small WebGL2 helpers: shader/program compilation, a unit-quad VAO, R32F image
 * texture upload, and colormap-LUT upload. Every function takes the `gl` context
 * and performs no work at module load.
 *
 * Vendored from `fitsgl` (`fitsgl-core/src/renderer/gl-util.ts`); the "tile"
 * texture here is simply the whole single-exposure SCI array (epic #261, N4).
 */

export function createShader(
  gl: WebGL2RenderingContext,
  type: number,
  source: string,
): WebGLShader {
  const shader = gl.createShader(type);
  if (shader === null) throw new Error('gl-util: createShader returned null');
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const log = gl.getShaderInfoLog(shader) ?? '(no info log)';
    gl.deleteShader(shader);
    const kind = type === gl.VERTEX_SHADER ? 'vertex' : 'fragment';
    throw new Error(`gl-util: ${kind} shader compile failed:\n${log}`);
  }
  return shader;
}

export function createProgram(
  gl: WebGL2RenderingContext,
  vertSource: string,
  fragSource: string,
): WebGLProgram {
  const vert = createShader(gl, gl.VERTEX_SHADER, vertSource);
  const frag = createShader(gl, gl.FRAGMENT_SHADER, fragSource);
  const program = gl.createProgram();
  if (program === null) throw new Error('gl-util: createProgram returned null');
  gl.attachShader(program, vert);
  gl.attachShader(program, frag);
  gl.linkProgram(program);
  gl.deleteShader(vert);
  gl.deleteShader(frag);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    const log = gl.getProgramInfoLog(program) ?? '(no info log)';
    gl.deleteProgram(program);
    throw new Error(`gl-util: program link failed:\n${log}`);
  }
  return program;
}

/**
 * A VAO holding a unit quad (0..1) as a 4-vertex triangle strip on attribute
 * location 0. Drawn with `gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4)`. Returns the
 * backing buffer too: deleting a VAO does not delete its buffers.
 */
export function createUnitQuadVAO(gl: WebGL2RenderingContext): {
  vao: WebGLVertexArrayObject;
  buffer: WebGLBuffer;
} {
  const vao = gl.createVertexArray();
  if (vao === null) throw new Error('gl-util: createVertexArray returned null');
  gl.bindVertexArray(vao);

  const buffer = gl.createBuffer();
  if (buffer === null) throw new Error('gl-util: createBuffer returned null');
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0, 1, 0, 0, 1, 1, 1]), gl.STATIC_DRAW);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);

  gl.bindVertexArray(null);
  return { vao, buffer };
}

/**
 * Upload the decoded image as an R32F single-channel texture with NEAREST
 * filtering and clamp-to-edge wrapping. NEAREST is mandatory: R32F is not
 * filterable in core WebGL2. `data` must be exactly `width * height` floats.
 */
export function createImageTexture(
  gl: WebGL2RenderingContext,
  width: number,
  height: number,
  data: Float32Array,
): WebGLTexture {
  const texture = gl.createTexture();
  if (texture === null) throw new Error('gl-util: createTexture returned null');
  gl.bindTexture(gl.TEXTURE_2D, texture);
  // FITS float32 blocks aren't 4-byte-row-padded in any way that matters, but a
  // width that isn't a multiple of 4 still needs UNPACK_ALIGNMENT = 1 for the
  // RED/FLOAT upload to read rows correctly.
  gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, width, height, 0, gl.RED, gl.FLOAT, data);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.bindTexture(gl.TEXTURE_2D, null);
  return texture;
}

/**
 * Upload a 1-D colormap LUT as a `size × 1` RGBA8 texture with LINEAR filtering,
 * sampled in the fragment shader at `vec2(s, 0.5)`. `rgba` must be `size * 4`
 * bytes.
 */
export function createColormapTexture(
  gl: WebGL2RenderingContext,
  size: number,
  rgba: Uint8Array,
): WebGLTexture {
  const texture = gl.createTexture();
  if (texture === null) throw new Error('gl-util: createTexture returned null');
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, size, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, rgba);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.bindTexture(gl.TEXTURE_2D, null);
  return texture;
}
