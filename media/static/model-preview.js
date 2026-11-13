/* Local GLB 2.0 preview. No network libraries or remote assets are loaded.
 * Supports embedded meshes, vertex colors, base-color textures, and scene transforms.
 * This lightweight preview intentionally leaves production PBR rendering to DCC tools. */
const IDENTITY = () => new Float32Array([1,0,0,0, 0,1,0,0, 0,0,1,0, 0,0,0,1]);
function multiply(a, b) { const out = new Float32Array(16); for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) for (let k = 0; k < 4; k++) out[c*4+r] += a[k*4+r] * b[c*4+k]; return out; }
function transform(m, x, y, z) { return [m[0]*x+m[4]*y+m[8]*z+m[12], m[1]*x+m[5]*y+m[9]*z+m[13], m[2]*x+m[6]*y+m[10]*z+m[14]]; }
function nodeMatrix(node) {
  if (node.matrix) return new Float32Array(node.matrix);
  const [x,y,z,w] = node.rotation || [0,0,0,1], s = node.scale || [1,1,1], t = node.translation || [0,0,0];
  return new Float32Array([(1-2*y*y-2*z*z)*s[0],(2*x*y+2*z*w)*s[0],(2*x*z-2*y*w)*s[0],0,(2*x*y-2*z*w)*s[1],(1-2*x*x-2*z*z)*s[1],(2*y*z+2*x*w)*s[1],0,(2*x*z+2*y*w)*s[2],(2*y*z-2*x*w)*s[2],(1-2*x*x-2*y*y)*s[2],0,...t,1]);
}
function normalize(v) { const n = Math.hypot(...v) || 1; return v.map((x) => x/n); }
function cross(a,b) { return [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]]; }
function viewMatrix(eye, target) { const z = normalize(eye.map((v,i) => v-target[i])); const x = normalize(cross([0,1,0],z)); const y = cross(z,x); const dot = (v) => -v.reduce((sum,n,i) => sum+n*eye[i],0); return new Float32Array([x[0],y[0],z[0],0,x[1],y[1],z[1],0,x[2],y[2],z[2],0,dot(x),dot(y),dot(z),1]); }
function perspective(aspect, near, far) { const f = 1/Math.tan(Math.PI/8); return new Float32Array([f/aspect,0,0,0,0,f,0,0,0,0,(far+near)/(near-far),-1,0,0,2*far*near/(near-far),0]); }
export function parseGLB(buffer) {
  const view = new DataView(buffer);
  if (buffer.byteLength < 20 || view.getUint32(0,true) !== 0x46546c67 || view.getUint32(4,true) !== 2) throw new Error('仅支持 GLB 2.0 文件');
  let json, binary;
  for (let offset = 12; offset+8 <= buffer.byteLength;) {
    const size = view.getUint32(offset,true), type = view.getUint32(offset+4,true); offset += 8;
    if (offset+size > buffer.byteLength) throw new Error('GLB 文件不完整');
    if (type === 0x4e4f534a) json = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer,offset,size)).trim());
    if (type === 0x004e4942) binary = buffer.slice(offset,offset+size);
    offset += size;
  }
  if (!json || !binary) throw new Error('GLB 缺少模型数据');
  if ((json.extensionsRequired || []).some((name) => ['KHR_draco_mesh_compression','EXT_meshopt_compression','KHR_texture_basisu'].includes(name))) throw new Error('该模型使用压缩扩展，请下载到 3D 工具查看');
  return { json, binary };
}
export function readEmbeddedTexture(data, index) {
  const texture = data.json.textures?.[index];
  // TRELLIS exports WebP textures through EXT_texture_webp, with no core source.
  const source = texture?.extensions?.EXT_texture_webp?.source ?? texture?.source;
  const image = data.json.images?.[source];
  if (!image || image.bufferView === undefined) return null;
  const view = data.json.bufferViews?.[image.bufferView];
  if (!view || (view.buffer || 0) !== 0) return null;
  const offset = view.byteOffset || 0;
  if (offset < 0 || offset + view.byteLength > data.binary.byteLength) throw new Error('模型贴图数据不完整');
  return { bytes: data.binary.slice(offset, offset + view.byteLength), mimeType: image.mimeType || 'image/png' };
}
function accessor(data, index) {
  const spec = data.json.accessors?.[index]; if (!spec || spec.sparse) throw new Error('模型包含暂不支持的顶点格式');
  const bv = data.json.bufferViews?.[spec.bufferView]; if (!bv || (bv.buffer || 0) !== 0) throw new Error('预览仅支持 GLB 内嵌模型');
  const components = { SCALAR: 1,VEC2: 2,VEC3: 3,VEC4: 4,MAT4: 16 }[spec.type];
  const types = { 5120: [1,'getInt8',127],5121: [1,'getUint8',255],5122: [2,'getInt16',32767],5123: [2,'getUint16',65535],5125: [4,'getUint32',4294967295],5126: [4,'getFloat32',1] };
  const kind = types[spec.componentType]; if (!components || !kind) throw new Error('不支持该模型的数据格式');
  const stride = bv.byteStride || components*kind[0], offset = (bv.byteOffset || 0)+(spec.byteOffset || 0);
  if (offset + (spec.count-1)*stride + components*kind[0] > data.binary.byteLength || spec.count > 15000000) throw new Error('模型数据范围异常');
  const result = new Float32Array(spec.count*components), dv = new DataView(data.binary);
  for (let n = 0; n < spec.count; n++) for (let c = 0; c < components; c++) { let value = dv[kind[1]](offset+n*stride+c*kind[0],true); if (spec.normalized && spec.componentType !== 5126) value = Math.max(-1,value/kind[2]); result[n*components+c] = value; }
  return { values: result, components, count: spec.count };
}
const vertexShader = `#version 300 es
precision highp float;
in vec3 position; in vec3 normal; in vec2 uv; in vec4 color;
uniform mat4 model; uniform mat4 camera;
out vec3 vNormal; out vec2 vUV; out vec4 vColor;
void main(){ vec4 world=model*vec4(position,1.0); vNormal=transpose(inverse(mat3(model)))*normal; vUV=uv; vColor=color; gl_Position=camera*world; }`;
const fragmentShader = `#version 300 es
precision highp float;
in vec3 vNormal; in vec2 vUV; in vec4 vColor;
uniform vec4 baseColor; uniform sampler2D map; uniform bool hasTexture; uniform bool hasNormal;
out vec4 outColor;
void main(){ vec4 c=baseColor*vColor; if(hasTexture)c*=texture(map,vUV); if(c.a<0.1)discard;
vec3 n=normalize(vNormal); if(!gl_FrontFacing)n=-n;
float lighting=hasNormal ? 0.55+0.38*max(dot(n,normalize(vec3(0.7,1.0,1.2))),0.0)+0.16*max(dot(n,normalize(vec3(-1.0,0.5,-0.5))),0.0) : 0.94;
outColor=vec4(c.rgb*lighting,1.0); }`;
export async function mountModelPreview(root, url, signal) {
  root.textContent = '';
  const loading = document.createElement('p'); loading.className = 'empty'; loading.textContent = '正在加载 3D 预览…'; root.append(loading);
  const response = await fetch(url, { credentials: 'same-origin', signal }); if (!response.ok) throw new Error(`模型文件读取失败（${response.status}）`);
  const data = parseGLB(await response.arrayBuffer()); if (signal.aborted) return () => {};
  const canvas = document.createElement('canvas'); canvas.tabIndex = 0; canvas.setAttribute('aria-label','3D 模型预览。拖动旋转，滚轮缩放；方向键旋转，加减键缩放，Home 键复位。');
  const gl = canvas.getContext('webgl2', { antialias: true, alpha: false }); if (!gl) throw new Error('浏览器未启用 WebGL 2');
  const resources = [], textures = []; let frame, disposed = false, observer;
  const cleanup = () => { if (disposed) return; disposed = true; window.removeEventListener('fantasy-theme-change',requestDraw); cancelAnimationFrame(frame); observer?.disconnect(); for (const item of resources) gl.deleteBuffer(item); for (const texture of textures) gl.deleteTexture(texture); gl.deleteProgram(program); gl.getExtension('WEBGL_lose_context')?.loseContext(); };
  function compile(type, source) { const shader = gl.createShader(type); gl.shaderSource(shader,source); gl.compileShader(shader); if (!gl.getShaderParameter(shader,gl.COMPILE_STATUS)) throw new Error('无法初始化 3D 着色器'); return shader; }
  const program = gl.createProgram(); const vs = compile(gl.VERTEX_SHADER,vertexShader), fs = compile(gl.FRAGMENT_SHADER,fragmentShader); gl.attachShader(program,vs); gl.attachShader(program,fs); gl.linkProgram(program); gl.deleteShader(vs); gl.deleteShader(fs); if (!gl.getProgramParameter(program,gl.LINK_STATUS)) throw new Error('无法初始化 3D 渲染');
  const locations = {}; for (const name of ['position','normal','uv','color']) locations[name] = gl.getAttribLocation(program,name); for (const name of ['model','camera','baseColor','map','hasTexture','hasNormal']) locations[name] = gl.getUniformLocation(program,name);
  function gpuBuffer(values, type = gl.ARRAY_BUFFER) { const result = gl.createBuffer(); resources.push(result); gl.bindBuffer(type,result); gl.bufferData(type,values,gl.STATIC_DRAW); return result; }
  const textureCache = new Map();
  async function loadTexture(index) {
    if (textureCache.has(index)) return textureCache.get(index);
    const image = readEmbeddedTexture(data,index); if (!image) return null;
    const blob = new Blob([image.bytes], { type: image.mimeType });
    const bitmap = await createImageBitmap(blob, { imageOrientation: 'none', premultiplyAlpha: 'none' });
    if (signal.aborted) { bitmap.close(); return null; }
    const texture = gl.createTexture(); textures.push(texture); gl.bindTexture(gl.TEXTURE_2D,texture); gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,bitmap); gl.generateMipmap(gl.TEXTURE_2D); gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR_MIPMAP_LINEAR); gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR); bitmap.close(); textureCache.set(index,texture); return texture;
  }
  const primitives = [], min = [Infinity,Infinity,Infinity], max = [-Infinity,-Infinity,-Infinity];
  async function visit(index, parent, depth = 0) {
    if (depth > 100) throw new Error('模型层级过深');
    const node = data.json.nodes?.[index]; if (!node) return; const matrix = multiply(parent,nodeMatrix(node));
    if (node.mesh !== undefined) for (const primitive of data.json.meshes?.[node.mesh]?.primitives || []) {
      if (primitive.mode !== undefined && primitive.mode !== 4) continue;
      if (primitive.attributes?.POSITION === undefined) continue;
      const position = accessor(data,primitive.attributes.POSITION), attrs = {};
      attrs.position = { buffer: gpuBuffer(position.values), size: 3 };
      for (let i = 0; i < position.values.length; i += 3) { const point = transform(matrix,...position.values.subarray(i,i+3)); for (let axis = 0; axis < 3; axis++) { min[axis] = Math.min(min[axis],point[axis]); max[axis] = Math.max(max[axis],point[axis]); } }
      for (const [gltf,name] of [['NORMAL','normal'],['TEXCOORD_0','uv'],['COLOR_0','color']]) if (primitive.attributes[gltf] !== undefined) { const value = accessor(data,primitive.attributes[gltf]); attrs[name] = { buffer: gpuBuffer(value.values), size: value.components }; }
      let indices = null, count = position.count;
      if (primitive.indices !== undefined) { const array = accessor(data,primitive.indices).values; indices = gpuBuffer(new Uint32Array(array),gl.ELEMENT_ARRAY_BUFFER); count = array.length; }
      const material = data.json.materials?.[primitive.material]?.pbrMetallicRoughness || {};
      let texture = null; if (material.baseColorTexture) texture = await loadTexture(material.baseColorTexture.index);
      primitives.push({ attrs, matrix, indices, count, texture, color: material.baseColorFactor || [1,1,1,1] });
    }
    for (const child of node.children || []) await visit(child,matrix,depth+1);
  }
  try {
    const roots = data.json.scenes?.[data.json.scene || 0]?.nodes || data.json.nodes?.map((_,i) => i).filter((i) => !data.json.nodes.some((node) => node.children?.includes(i))) || [];
    for (const node of roots) await visit(node,IDENTITY());
    if (signal.aborted) { cleanup(); return () => {}; }
    if (!primitives.length) throw new Error('模型中没有可预览的三角网格');
  } catch (error) { cleanup(); throw error; }
  const center = min.map((value,i) => (value+max[i])/2), radius = Math.max(Math.hypot(...max.map((value,i) => value-min[i]))/2,0.01);
  let yaw = .6, pitch = .28, distance = radius*3.1;
  const help = document.createElement('p'); help.className = 'model-viewer-help'; help.textContent = '拖动旋转 · 滚轮缩放 · 双击复位 ｜ 轻量预览，材质效果以制作工具为准'; root.replaceChildren(canvas,help);
  gl.enable(gl.DEPTH_TEST); gl.clearColor(.917,.945,.9,1); gl.useProgram(program); gl.uniform1i(locations.map,0);
  function draw() {
    if (disposed || signal.aborted) return;
    const w = Math.max(1,Math.floor(canvas.clientWidth*Math.min(devicePixelRatio || 1,2))), h = Math.max(1,Math.floor(canvas.clientHeight*Math.min(devicePixelRatio || 1,2)));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    gl.viewport(0,0,w,h); if (window.FantasyTheme?.getTheme() === 'dark') gl.clearColor(.075,.10,.13,1); else gl.clearColor(.917,.945,.9,1); gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
    const eye = [center[0]+Math.sin(yaw)*Math.cos(pitch)*distance,center[1]+Math.sin(pitch)*distance,center[2]+Math.cos(yaw)*Math.cos(pitch)*distance];
    const camera = multiply(perspective(w/h,radius*.005,radius*100),viewMatrix(eye,center)); gl.uniformMatrix4fv(locations.camera,false,camera);
    for (const primitive of primitives) {
      gl.uniformMatrix4fv(locations.model,false,primitive.matrix); gl.uniform4fv(locations.baseColor,primitive.color); gl.uniform1i(locations.hasTexture,Boolean(primitive.texture)); gl.uniform1i(locations.hasNormal,Boolean(primitive.attrs.normal));
      gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D,primitive.texture);
      for (const name of ['position','normal','uv','color']) { const attr = primitive.attrs[name], loc = locations[name]; if (attr) { gl.enableVertexAttribArray(loc); gl.bindBuffer(gl.ARRAY_BUFFER,attr.buffer); gl.vertexAttribPointer(loc,attr.size,gl.FLOAT,false,0,0); } else { gl.disableVertexAttribArray(loc); if (name === 'color') gl.vertexAttrib4f(loc,1,1,1,1); else if (name === 'normal') gl.vertexAttrib3f(loc,0,1,0); else gl.vertexAttrib2f(loc,0,0); } }
      if (primitive.indices) { gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER,primitive.indices); gl.drawElements(gl.TRIANGLES,primitive.count,gl.UNSIGNED_INT,0); } else gl.drawArrays(gl.TRIANGLES,0,primitive.count);
    }
  }
  function requestDraw() { cancelAnimationFrame(frame); frame = requestAnimationFrame(draw); }
  function reset() { yaw = .6; pitch = .28; distance = radius*3.1; requestDraw(); }
  let pointer;
  canvas.addEventListener('pointerdown',(event) => { pointer = { id: event.pointerId, x: event.clientX, y: event.clientY }; canvas.setPointerCapture(event.pointerId); });
  canvas.addEventListener('pointermove',(event) => { if (!pointer || pointer.id !== event.pointerId) return; yaw -= (event.clientX-pointer.x)*.008; pitch = Math.max(-1.45,Math.min(1.45,pitch+(event.clientY-pointer.y)*.008)); pointer.x = event.clientX; pointer.y = event.clientY; requestDraw(); });
  for (const event of ['pointerup','pointercancel','lostpointercapture']) canvas.addEventListener(event,() => { pointer = null; });
  canvas.addEventListener('wheel',(event) => { event.preventDefault(); distance = Math.max(radius*.3,Math.min(radius*15,distance*Math.exp(event.deltaY*.001))); requestDraw(); },{ passive: false });
  canvas.addEventListener('dblclick',reset);
  canvas.addEventListener('keydown',(event) => { let handled = true; if (event.key === 'ArrowLeft') yaw -= .12; else if (event.key === 'ArrowRight') yaw += .12; else if (event.key === 'ArrowUp') pitch = Math.min(1.45,pitch+.1); else if (event.key === 'ArrowDown') pitch = Math.max(-1.45,pitch-.1); else if (event.key === '+' || event.key === '=') distance = Math.max(radius*.3,distance*.9); else if (event.key === '-') distance = Math.min(radius*15,distance*1.1); else if (event.key === 'Home') reset(); else handled = false; if (handled) { event.preventDefault(); requestDraw(); } });
  canvas.addEventListener('webglcontextlost',(event) => { event.preventDefault(); help.textContent = '3D 预览资源已中断，请重新打开预览；原文件仍可下载。'; });
  window.addEventListener('fantasy-theme-change',requestDraw); observer = new ResizeObserver(requestDraw); observer.observe(canvas); signal.addEventListener('abort',cleanup,{ once: true }); requestDraw();
  return cleanup;
}
