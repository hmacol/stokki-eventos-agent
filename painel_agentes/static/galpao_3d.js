// Galpao 3D: o galpao em Three.js, com os bonecos Lego encenando o que a Stokki
// e a Vuupt registraram. Porta o prototipo galpao-3d-esteiras.html (layout aprovado
// pelo Hugo) e liga ao JSON de /api/galpao-3d/estado (galpao3d/estado.py).
//
// - Bonecos vem de `pessoas`, nas vagas da area (bancadas, mesas, doca); extras ao lado.
// - Esteiras mostram min(fila, 12) caixas e a placa "fila N" com o N real.
// - A cada 60 s busca o estado; eventos novos (id > ultimoId) viram animacao.
//   Na primeira carga nada e encenado. Fila com mais de 10 animacoes anda a 3x.
// - ?exemplo=1 usa ESTADO_EXEMPLO e gera eventos sinteticos para mostrar a encenacao.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const raiz = document.getElementById('galpao3d');
const API = raiz.dataset.api;
const EXEMPLO = new URLSearchParams(location.search).get('exemplo') === '1';
const INTERVALO_MS = 60000;
const MAX_CAIXAS = 12;
const MAX_CAMINHOES = 8;
const $ = id => document.getElementById(id);

// ---------- cena ----------
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
raiz.prepend(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0e1620);
scene.fog = new THREE.Fog(0x0e1620, 70, 130);

const camera = new THREE.PerspectiveCamera(42, 1, 0.1, 300);
camera.position.set(-9, 40, 37);
const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(-1, 0, -1.5);
controls.enableDamping = true;
controls.maxPolarAngle = Math.PI * 0.47;

scene.add(new THREE.HemisphereLight(0xdfefff, 0x334455, 1.0));
const sol = new THREE.DirectionalLight(0xffffff, 1.6);
sol.position.set(18, 40, 22);
sol.castShadow = true;
sol.shadow.mapSize.set(2048, 2048);
Object.assign(sol.shadow.camera, { left: -45, right: 45, top: 45, bottom: -45, far: 120 });
scene.add(sol);

function redimensionar() {
  const w = raiz.clientWidth || 800, h = raiz.clientHeight || 500;
  camera.aspect = w / h; camera.updateProjectionMatrix();
  renderer.setSize(w, h, false);
}
new ResizeObserver(redimensionar).observe(raiz);
redimensionar();

const mat = (c, extra = {}) => new THREE.MeshStandardMaterial(Object.assign({ color: c, roughness: .75 }, extra));
function bloco(w, h, d, c, x, y, z, extra) {
  const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat(c, extra));
  m.position.set(x, y, z); m.castShadow = true; m.receiveShadow = true; scene.add(m); return m;
}
// Rotulo de uma linha que cresce com o texto (prototipo).
function textura(txt, { w = 512, h = 128, fundo = null, cor = '#fff', borda = null, fonte = 64 } = {}) {
  const cv = document.createElement('canvas'); cv.height = h;
  let g = cv.getContext('2d'); g.font = `700 ${fonte}px system-ui, sans-serif`;
  w = Math.max(w, Math.ceil(g.measureText(txt).width) + h); cv.width = w;
  g = cv.getContext('2d');
  if (fundo) { g.fillStyle = fundo; g.beginPath(); g.roundRect(4, 4, w - 8, h - 8, h / 2.4); g.fill(); }
  if (borda) { g.strokeStyle = borda; g.lineWidth = 8; g.beginPath(); g.roundRect(4, 4, w - 8, h - 8, h / 2.4); g.stroke(); }
  g.fillStyle = cor; g.font = `700 ${fonte}px system-ui, sans-serif`; g.textAlign = 'center'; g.textBaseline = 'middle';
  g.fillText(txt, w / 2, h / 2 + 2);
  const t = new THREE.CanvasTexture(cv); t.colorSpace = THREE.SRGBColorSpace; return t;
}
// Balao de duas linhas (nome em cima, tarefa embaixo), largura pelo texto mais longo.
function texturaBalao(l1, l2, borda) {
  const F1 = '700 46px system-ui, sans-serif', F2 = '600 38px system-ui, sans-serif';
  const cv = document.createElement('canvas');
  let g = cv.getContext('2d');
  g.font = F1; const w1 = g.measureText(l1).width;
  g.font = F2; const w2 = l2 ? g.measureText(l2).width : 0;
  const h = l2 ? 128 : 76, w = Math.ceil(Math.max(w1, w2, 60)) + 56;
  cv.width = w; cv.height = h; g = cv.getContext('2d');
  g.fillStyle = '#ffffff'; g.beginPath(); g.roundRect(4, 4, w - 8, h - 8, 26); g.fill();
  g.strokeStyle = borda; g.lineWidth = 8; g.beginPath(); g.roundRect(4, 4, w - 8, h - 8, 26); g.stroke();
  g.textAlign = 'center'; g.textBaseline = 'middle';
  g.fillStyle = '#1b2a3d'; g.font = F1; g.fillText(l1, w / 2, l2 ? 40 : h / 2 + 2);
  if (l2) { g.fillStyle = '#3b4d63'; g.font = F2; g.fillText(l2, w / 2, 90); }
  const t = new THREE.CanvasTexture(cv); t.colorSpace = THREE.SRGBColorSpace; return t;
}
// Troca a textura de um sprite (descartando a anterior) e ajusta a largura.
function trocarTextura(sprite, tex, altura) {
  sprite.material.map?.dispose();
  sprite.material.map = tex; sprite.material.needsUpdate = true;
  const img = tex.image; sprite.scale.set(altura * img.width / img.height, altura, 1);
}
function placa(txt, fundo, x, y, z, w = 3.6) {
  const s = new THREE.Sprite(new THREE.SpriteMaterial({ map: textura(txt, { fundo }), depthTest: false }));
  const img = s.material.map.image; s.scale.set(w / 4 * img.width / img.height, w / 4, 1); s.position.set(x, y, z); s.renderOrder = 5; scene.add(s); return s;
}
function descartar(obj) {
  scene.remove(obj);
  obj.traverse(o => {
    o.geometry?.dispose();
    if (o.material) { o.material.map?.dispose(); o.material.dispose(); }
  });
}

const KRAFT = 0xc89b5a, PEDIDO = 0x4c8dff, CONFERIDO = 0x3ddc84;

// ---------- chao e areas (layout do prototipo) ----------
const chao = new THREE.Mesh(new THREE.PlaneGeometry(53, 34), mat(0x3a4452));
chao.rotation.x = -Math.PI / 2; chao.position.set(-4, 0, 0); chao.receiveShadow = true; scene.add(chao);

const zonas = [
  ['Recebimento', 13, -16, 9, 11, 0x8a7350],
  ['Estoque (câmaras frias)', -18, -16, 18, 13, 0x3d6585],
  ['Separação', -28, 1, 14, 11, 0x4f6b5a],
  ['Conferência', 4, 1, 7, 11, 0x5b5a7a],
  ['Expedição · Doca', 13, -5, 9, 21, 0x6b6355],
];
for (const [nome, x, z, w, d, c] of zonas) {
  const p = new THREE.Mesh(new THREE.PlaneGeometry(w, d), mat(c));
  p.rotation.x = -Math.PI / 2; p.position.set(x + w / 2, 0.02, z + d / 2); p.receiveShadow = true; scene.add(p);
  const tz = textura(nome, { fundo: 'rgba(0,0,0,.45)' }), alt = Math.min(1.6, (w - 1) / (tz.image.width / tz.image.height));
  const r = new THREE.Mesh(new THREE.PlaneGeometry(alt * tz.image.width / tz.image.height, alt), new THREE.MeshBasicMaterial({ map: tz, transparent: true }));
  r.rotation.x = -Math.PI / 2; r.position.set(x + w / 2, 0.04, z + d - .9); scene.add(r);
}
// rua
const rua = new THREE.Mesh(new THREE.PlaneGeometry(6, 120), mat(0x22272e));
rua.rotation.x = -Math.PI / 2; rua.position.set(25, 0.01, 0); rua.receiveShadow = true; scene.add(rua);
for (let z = -58; z < 60; z += 4) {
  const f = new THREE.Mesh(new THREE.PlaneGeometry(.25, 2), new THREE.MeshBasicMaterial({ color: 0xe0c341 }));
  f.rotation.x = -Math.PI / 2; f.position.set(25, 0.03, z); scene.add(f);
}
bloco(53, 1.2, .3, 0x5d6b7c, -4, .6, -17);
bloco(.3, 1.2, 34, 0x5d6b7c, -30.5, .6, 0);

// ---------- recebimento: paletes ----------
for (const [x, z] of [[17, -13], [14.8, -13], [17, -8]]) {
  bloco(1.8, .2, 1.4, 0x8d6e4e, x, .1, z);
  for (let i = 0; i < 4; i++) bloco(.7, .6, .6, KRAFT, x - .4 + (i % 2) * .8, .5 + Math.floor(i / 2) * .6, z);
}

// ---------- estoque: camaras frias com prateleiras ----------
const vidro = { color: 0x9ec9ef, transparent: true, opacity: .3 };
bloco(18, 3.4, .2, 0x9ec9ef, -9, 1.7, -16, vidro);
bloco(.2, 3.4, 13, 0x9ec9ef, -18, 1.7, -9.5, vidro);
bloco(.2, 3.4, 4.5, 0x9ec9ef, 0, 1.7, -13.75, vidro);
bloco(.2, 3.4, 6.5, 0x9ec9ef, 0, 1.7, -6.25, vidro);
bloco(1, 3.4, .2, 0x9ec9ef, -17.5, 1.7, -3, vidro);
bloco(15, 3.4, .2, 0x9ec9ef, -7.5, 1.7, -3, vidro);
bloco(.2, 3.4, 8, 0x9ec9ef, -9, 1.7, -12, vidro);
bloco(18, .15, 13, 0xcfe6fa, -9, 3.45, -9.5, { transparent: true, opacity: .18 });
placa('❄ Câmara 1', '#1d4e89', -13.5, 4.4, -12);
placa('❄ Câmara 2', '#1d4e89', -4.5, 4.4, -12);
const coresProd = [0xc89b5a, 0xb98a4c, 0xd9b27c, 0xe9c46a, 0xa47148];
function prateleira(x0, x1, z) {
  const w = x1 - x0, cx = (x0 + x1) / 2;
  for (const y of [.9, 1.9, 2.9]) bloco(w, .12, 1, 0x7a8b99, cx, y, z);
  for (let x = x0; x <= x1 + .01; x += w / 3) bloco(.15, 3, 1, 0x5d6b7c, x, 1.5, z);
  for (let i = 0; i < Math.floor(w * 2.4); i++) bloco(.42, .42, .5, coresProd[i % 5], x0 + .35 + (i * .51) % (w - .6), [1.17, 2.17, 3.17][i % 3], z);
}
prateleira(-16.5, -10, -14.6);
prateleira(-8, -1, -14.6);
prateleira(-14, -11, -6);
prateleira(-7, -2, -6);

// ---------- esteiras ----------
const faixa = (() => {
  const cv = document.createElement('canvas'); cv.width = 64; cv.height = 64;
  const g = cv.getContext('2d'); g.fillStyle = '#23262b'; g.fillRect(0, 0, 64, 64);
  g.fillStyle = '#3a3f47'; for (let i = 0; i < 64; i += 16) g.fillRect(0, i, 64, 6);
  const t = new THREE.CanvasTexture(cv); t.wrapS = t.wrapT = THREE.RepeatWrapping; return t;
})();
const ALT = .95, PASSO_CAIXA = .7;
// Caixas paradas no fim da esteira = a fila real (ate MAX_CAIXAS ou o que couber).
function esteira(nome, a, b, cor) {
  const dx = b[0] - a[0], dz = b[1] - a[1], len = Math.hypot(dx, dz), ang = Math.atan2(dx, dz);
  const g = new THREE.Group(); g.position.set(a[0], 0, a[1]); g.rotation.y = ang; scene.add(g);
  const tex = faixa.clone(); tex.needsUpdate = true; tex.repeat.set(1, len);
  const lona = new THREE.Mesh(new THREE.BoxGeometry(.9, .08, len), new THREE.MeshStandardMaterial({ map: tex, roughness: .9 }));
  lona.position.set(0, ALT - .05, len / 2); lona.receiveShadow = true; g.add(lona);
  for (const lado of [-.5, .5]) {
    const borda = new THREE.Mesh(new THREE.BoxGeometry(.1, .2, len), mat(0xf4a261)); borda.position.set(lado, ALT, len / 2); borda.castShadow = true; g.add(borda);
  }
  for (let s = .3; s < len; s += 1.6) for (const lado of [-.42, .42]) {
    const perna = new THREE.Mesh(new THREE.BoxGeometry(.08, ALT - .1, .08), mat(0x555b63)); perna.position.set(lado, (ALT - .1) / 2, s); g.add(perna);
  }
  const rot = new THREE.Sprite(new THREE.SpriteMaterial({ depthTest: false, transparent: true }));
  const sPlaca = len - Math.min(2.5, len / 2);
  rot.position.set(a[0] + dx / len * sPlaca, 2.4, a[1] + dz / len * sPlaca); rot.renderOrder = 6; rot.visible = false; scene.add(rot);
  const capacidade = Math.min(MAX_CAIXAS, Math.floor((len - .3) / PASSO_CAIXA) + 1);
  const e = { nome, a, b, len, ux: dx / len, uz: dz / len, tex, cor, caixas: [], vel: 1.3, rot, capacidade, alvo: 0, fila: undefined };
  const novaCaixa = s => {
    const m = new THREE.Mesh(new THREE.BoxGeometry(.5, .42, .5), mat(e.cor)); m.castShadow = true; scene.add(m);
    const c = { m, s }; return c;
  };
  e.poe = () => { e.caixas.push(novaCaixa(0)); e.posiciona(); };
  e.posiciona = () => e.caixas.forEach(c => c.m.position.set(e.a[0] + e.ux * c.s, ALT + .23, e.a[1] + e.uz * c.s));
  // Ajusta as caixas paradas ao numero real; sem animar (chega/sai na hora).
  e.defineFila = n => {
    e.alvo = n == null ? 0 : Math.min(n, e.capacidade);
    while (e.caixas.length > e.alvo) descartar(e.caixas.pop().m);
    while (e.caixas.length < e.alvo) e.caixas.push(novaCaixa(e.len - e.caixas.length * PASSO_CAIXA));
    e.posiciona();
    if (n === e.fila) return;
    e.fila = n;
    if (n == null) {
      trocarTextura(rot, textura('sem dado', { fundo: '#4a5868', fonte: 60 }), .6); rot.visible = true;
    } else if (n >= 3) {
      trocarTextura(rot, textura(`fila ${n}`, { fundo: n >= 5 ? '#d62839' : '#e09f3e', fonte: 70 }), .65); rot.visible = true;
    } else rot.visible = false;
  };
  e.anda = dt => {
    let limite = e.len;
    for (const c of e.caixas) { c.s = Math.min(c.s + e.vel * dt, limite); c.parada = c.s >= limite - 1e-6; limite = c.s - PASSO_CAIXA; }
    // caixa encenada que passa da fila real some ao encostar no fim da fila
    while (e.caixas.length > e.alvo && e.caixas[e.caixas.length - 1].parada) descartar(e.caixas.pop().m);
    e.posiciona();
    tex.offset.y -= e.vel * dt;
  };
  return e;
}
const ESTEIRAS = {
  rec_estoque: esteira('rec→estoque', [13.2, -10.5], [-2, -10.5], KRAFT),
  estoque_sep: esteira('estoque→sep', [-16, -5.2], [-16, 3], KRAFT),
  sep_conf: esteira('sep→conf', [-14.6, 6.5], [4.8, 6.5], PEDIDO),
  conf_exp: esteira('conf→exp', [10.4, 6.5], [14.6, 6.5], CONFERIDO),
};
placa('Esteira → Estoque', '#7a4b12', 5.5, 2.1, -10.5, 3.2);
placa('Esteira → Separação', '#7a4b12', -16.9, 2.1, -.8, 3.4);
placa('Esteira → Conferência', '#7a4b12', -5, 2.1, 6.5, 3.6);
placa('Esteira → Expedição', '#7a4b12', 12.5, 2.1, 4.2, 3.4);

// ---------- separacao e conferencia: 3 bancadas e 3 mesas ----------
function mesa(x, z, w, cor, monitor) {
  bloco(w, .1, 1.1, cor, x, 1, z);
  for (const [dx, dz] of [[-w / 2 + .1, -.45], [w / 2 - .1, -.45], [-w / 2 + .1, .45], [w / 2 - .1, .45]]) bloco(.1, 1, .1, 0x555, x + dx, .5, z + dz);
  if (monitor) bloco(.6, .5, .2, 0x222831, x + w / 2 - .5, 1.3, z);
  else bloco(.45, .4, .45, KRAFT, x - w / 2 + .5, 1.25, z);
}
for (const z of [3.5, 6.8, 10.1]) mesa(-23, z, 3, 0x9fb8a8);
mesa(6, 3, 2.2, 0xa8a3c9, true); mesa(9.2, 3, 2.2, 0xa8a3c9, true); mesa(7.6, 10.4, 2.2, 0xa8a3c9, true);

// ---------- vagas por area ----------
// pos: onde o boneco fica; face: para onde olha; poe: onde larga a caixa na esteira.
const VAGAS = {
  separacao: [
    { pos: [-23, 4.5], face: [0, -1], poe: [-15.3, 5.7] },
    { pos: [-23, 7.8], face: [0, -1], poe: [-15.3, 7.3] },
    { pos: [-23, 11.1], face: [0, -1], poe: [-15.6, 6.5] },
  ],
  conferencia: [
    { pos: [6, 4], face: [0, -1], poe: [9.8, 5.6] },
    { pos: [9.2, 4], face: [0, -1], poe: [9.8, 7.4] },
    { pos: [7.6, 9.4], face: [0, 1], poe: [9.6, 6.5] },
  ],
  estoque: [{ pos: [-12.5, -7.1], face: [0, 1] }],
  recebimento_expedicao: [
    { pos: [17, -11.9], face: [0, -1] },
    { pos: [16, 7.4], face: [-1, 0] },
    { pos: [17, 0], face: [-1, 0] },
  ],
};
// Extras: em fila ao lado da area (primeiro a vaga base, depois anda em x).
const EXTRAS = {
  separacao: { pos: [-19.6, 9.2], face: [-1, 0], poe: [-15.3, 6.5], dx: 0, dz: 1.4 },
  conferencia: { pos: [5, 9.2], face: [0, -1], poe: [9.6, 6.5], dx: 1.3, dz: 0 },
  estoque: { pos: [-8, -7.1], face: [0, 1], dx: 1.4, dz: 0 },
  recebimento_expedicao: { pos: [14.4, -3.5], face: [1, 0], dx: 0, dz: 1.5 },
};
const grupoDaArea = a => (a === 'recebimento' || a === 'expedicao') ? 'recebimento_expedicao' : a;
function vagaExtra(grupo, k) {
  const b = EXTRAS[grupo] || EXTRAS.estoque;
  return { pos: [b.pos[0] + b.dx * k, b.pos[1] + b.dz * k], face: b.face, poe: b.poe };
}
const CAMISAS = {
  separacao: [0x2a9d8f, 0x219ebc, 0x52b788], conferencia: [0x7b61ff, 0x9d4edd, 0xc77dff],
  estoque: [0x48cae4], recebimento_expedicao: [0xe76f51],
};

// ---------- boneco Lego ----------
const AMARELO = 0xf6c90e, CINZA = new THREE.Color(0x7d8794);
const STATUS = { ok: 0x3ddc84, lento: 0xffd166, parado: 0xff4d6d, fora: 0x8a96a3 };
const hex = n => '#' + n.toString(16).padStart(6, '0');
function criarBoneco(camisa, calca = 0x2b3a67) {
  const g = new THREE.Group();
  const partes = [];
  const parte = (geo, c, x, y, z, pai = g) => {
    const m = new THREE.Mesh(geo, mat(c, { roughness: .45 })); m.position.set(x, y, z); m.castShadow = true;
    m.userData.cor = c; pai.add(m); partes.push(m); return m;
  };
  const pernas = [-.19, .19].map(x => { const p = new THREE.Group(); p.position.set(x, .75, 0); g.add(p); parte(new THREE.BoxGeometry(.34, .72, .42), calca, 0, -.38, 0, p); return p; });
  parte(new THREE.BoxGeometry(.76, .16, .44), calca, 0, .82, 0);
  const tronco = parte(new THREE.CylinderGeometry(.36, .5, .75, 4), camisa, 0, 1.27, 0);
  tronco.rotation.y = Math.PI / 4; tronco.scale.z = .62;
  const bracos = [-.5, .5].map(x => {
    const b = new THREE.Group(); b.position.set(x, 1.55, 0); g.add(b);
    parte(new THREE.BoxGeometry(.2, .55, .26), camisa, 0, -.27, 0, b);
    parte(new THREE.CylinderGeometry(.1, .1, .14, 10), AMARELO, 0, -.62, 0, b);
    return b;
  });
  parte(new THREE.CylinderGeometry(.08, .08, .1, 10), AMARELO, 0, 1.69, 0);
  parte(new THREE.CylinderGeometry(.26, .26, .44, 20), AMARELO, 0, 1.94, 0);
  parte(new THREE.CylinderGeometry(.13, .13, .12, 14), AMARELO, 0, 2.22, 0);
  for (const x of [-.09, .09]) parte(new THREE.SphereGeometry(.035, 8, 8), 0x111111, x, 1.99, .25);
  const sorriso = new THREE.Mesh(new THREE.TorusGeometry(.08, .015, 6, 12, Math.PI), mat(0x111111));
  sorriso.position.set(0, 1.9, .255); sorriso.rotation.z = Math.PI; g.add(sorriso);
  const caixa = new THREE.Mesh(new THREE.BoxGeometry(.55, .45, .5), mat(KRAFT)); caixa.position.set(0, 1.15, .55); caixa.castShadow = true; caixa.visible = false; g.add(caixa);
  const banco = new THREE.Mesh(new THREE.BoxGeometry(.6, .5, .5), mat(0x6b5a45)); banco.position.set(0, .5, -.05); banco.visible = false; g.add(banco);
  const diamante = new THREE.Mesh(new THREE.OctahedronGeometry(.2), new THREE.MeshStandardMaterial({ color: STATUS.ok, emissive: STATUS.ok, emissiveIntensity: .6 }));
  diamante.scale.y = 1.8; diamante.position.y = 2.95; g.add(diamante);
  const rot = new THREE.Sprite(new THREE.SpriteMaterial({ depthTest: false }));
  rot.position.y = 3.9; rot.renderOrder = 10; g.add(rot);
  scene.add(g);
  return { g, pernas, bracos, caixa, banco, diamante, rot, partes };
}
function criarCaminhao(cor) {
  const g = new THREE.Group();
  const p = (w, h, d, c, x, y, z) => { const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat(c)); m.position.set(x, y, z); m.castShadow = true; g.add(m); return m; };
  p(3.6, 2.1, 1.9, 0xeef1f4, -0.6, 1.55, 0);
  p(1.3, 1.6, 1.9, cor, 1.85, 1.3, 0);
  p(.05, .7, 1.6, 0x9ad0ff, 2.5, 1.65, 0);
  for (const [x, z] of [[-1.6, .95], [-1.6, -.95], [1.8, .95], [1.8, -.95]]) {
    const r = new THREE.Mesh(new THREE.CylinderGeometry(.42, .42, .3, 16), mat(0x111111)); r.rotation.x = Math.PI / 2; r.position.set(x, .42, z); g.add(r);
  }
  const rot = new THREE.Sprite(new THREE.SpriteMaterial({ depthTest: false }));
  rot.position.y = 3.6; rot.renderOrder = 10; g.add(rot);
  scene.add(g);
  return { g, rot };
}

// ---------- bonecos ligados a `pessoas` ----------
const bonecos = new Map();   // nome -> boneco
const virar = (g, f) => { g.rotation.y = Math.atan2(f[0], f[1]); };

function rotular(b, l1, l2, st) {
  const chave = `${l1}|${l2}|${st}`;
  if (chave === b._chave) return;
  b._chave = chave;
  trocarTextura(b.rot, texturaBalao(l1, l2, hex(STATUS[st] ?? STATUS.ok)), l2 ? 1.35 : .85);
}
function rotuloDoEstado(b) {
  const p = b.pessoa;
  const tarefa = p.status === 'fora' ? 'fora do turno' : (p.tarefa || 'sem tarefa hoje');
  rotular(b, p.nome + (p.extra ? ' (extra)' : ''), tarefa, p.status);
}
function aplicarStatus(b) {
  const st = b.pessoa.status, fora = st === 'fora';
  b.diamante.visible = !fora;
  if (!fora) { b.diamante.material.color.setHex(STATUS[st] ?? STATUS.ok); b.diamante.material.emissive.setHex(STATUS[st] ?? STATUS.ok); }
  if (fora !== b.fora) {
    b.fora = fora;
    for (const m of b.partes) {
      m.material.color.setHex(m.userData.cor);
      if (fora) m.material.color.lerp(CINZA, .75);
    }
  }
  if (!b.acao) rotuloDoEstado(b);
}

function aplicarPessoas(pessoas) {
  const vistos = new Set();
  const porGrupo = {};
  // equipe primeiro, extras depois (as vagas fixas vao para quem e da equipe)
  const ordem = [...pessoas].sort((x, y) => (x.extra ? 1 : 0) - (y.extra ? 1 : 0));
  for (const p of ordem) {
    const grupo = grupoDaArea(p.area);
    const k = porGrupo[grupo] = (porGrupo[grupo] ?? -1) + 1;
    const fixas = VAGAS[grupo] || [];
    const vaga = k < fixas.length ? fixas[k] : vagaExtra(grupo, k - fixas.length);
    let b = bonecos.get(p.nome);
    if (!b) {
      const cores = CAMISAS[grupo] || [0x8d99ae];
      b = Object.assign(criarBoneco(cores[k % cores.length]), { fase: Math.random() * 6, acao: null, fora: false });
      b.g.position.set(vaga.pos[0], 0, vaga.pos[1]); virar(b.g, vaga.face);
      bonecos.set(p.nome, b);
    } else if (!b.acao && (b.vaga?.pos[0] !== vaga.pos[0] || b.vaga?.pos[1] !== vaga.pos[1])) {
      b.acao = { passos: [{ to: vaga.pos }, { face: vaga.face }], i: 0, entrou: false };
    }
    b.vaga = vaga; b.grupo = grupo; b.pessoa = p;
    b.rot.position.y = 3.8 + (k % 2) * 1.1;   // alterna a altura para os baloes vizinhos nao se taparem
    aplicarStatus(b);
    vistos.add(p.nome);
  }
  for (const [nome, b] of bonecos) if (!vistos.has(nome)) { descartar(b.g); bonecos.delete(nome); }
}

// ---------- encenacao dos eventos ----------
// Cada evento novo vira uma animacao. Um boneco faz uma de cada vez; a fila
// anda em ordem por boneco. Mais de 10 na fila: tudo a 3x.
const filaAnim = [];
let rodizio = 0;
const ENCENA = {
  'Em Conferência': { grupo: 'separacao', esteira: 'sep_conf', cor: PEDIDO, texto: 'separado → conferência' },
  'Aguardando Transportador': { grupo: 'conferencia', esteira: 'conf_exp', cor: CONFERIDO, texto: 'conferido → expedição' },
};
function escolherBoneco(ev) {
  const regra = ENCENA[ev.para];
  const candidatos = [...bonecos.values()].filter(b => b.grupo === regra.grupo);
  const pelo = candidatos.find(b => b.pessoa.nome === ev.usuario);
  if (pelo) return pelo;
  const ativos = candidatos.filter(b => b.pessoa.status !== 'fora');
  const lista = ativos.length ? ativos : candidatos;
  return lista.length ? lista[rodizio++ % lista.length] : null;
}
function passosDaEncenacao(b, regra) {
  const v = b.vaga, poe = v.poe || [ESTEIRAS[regra.esteira].a[0] - .7, ESTEIRAS[regra.esteira].a[1]];
  const ida = regra.grupo === 'separacao' ? [{ to: [-20.5, v.pos[1]], carry: true }] : [];
  const volta = regra.grupo === 'separacao' ? [{ to: [-20.5, v.pos[1]] }] : [];
  return [
    { wait: .6, anim: 'pick', carry: true },
    ...ida,
    { to: poe, carry: true },
    { face: [1, 0], carry: true },
    { poe: ESTEIRAS[regra.esteira], wait: .5, anim: 'pick' },
    ...volta,
    { to: v.pos },
    { face: v.face },
  ];
}
function despachar() {
  const ocupados = new Set();
  for (let i = 0; i < filaAnim.length; i++) {
    const ev = filaAnim[i], regra = ENCENA[ev.para];
    const b = escolherBoneco(ev);
    if (!b) {   // ninguem da area na tela: a caixa entra direto na esteira
      ESTEIRAS[regra.esteira].poe(); filaAnim.splice(i--, 1); continue;
    }
    if (b.acao || ocupados.has(b)) { ocupados.add(b); continue; }
    filaAnim.splice(i--, 1);
    b.cor = regra.cor;
    b.caixa.material.color.setHex(regra.cor);
    rotular(b, b.pessoa.nome, `pedido ${ev.id_stokki} · ${regra.texto}`, 'ok');
    b.acao = { passos: passosDaEncenacao(b, regra), i: 0, entrou: false };
    ocupados.add(b);
  }
}
const MAX_FILA_ANIM = 30;
function enfileirar(eventos) {
  for (const ev of eventos) if (ENCENA[ev.para]) filaAnim.push(ev);
  // so as mais recentes: um atraso grande nao vira replay longo
  if (filaAnim.length > MAX_FILA_ANIM) filaAnim.splice(0, filaAnim.length - MAX_FILA_ANIM);
}
// Aba escondida: o loop de desenho para, mas a busca continua enfileirando.
// Ao voltar, descarta o atraso (o estado ja esta aplicado, so nao reencena).
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') filaAnim.length = 0;
});

// Executa o passo atual da acao; devolve se esta andando.
function passoDaAcao(b, dt) {
  const a = b.acao, s = a.passos[a.i];
  if (!a.entrou) {
    a.entrou = true; a.t = 0;
    if (s.face) virar(b.g, s.face);
    if (s.poe) s.poe.poe();
    b.caixa.visible = !!s.carry;
  }
  let fim = true, andando = false;
  if (s.to) {
    const dx = s.to[0] - b.g.position.x, dz = s.to[1] - b.g.position.z, dist = Math.hypot(dx, dz), d = 3 * dt;
    if (dist > d) {
      b.g.position.x += dx / dist * d; b.g.position.z += dz / dist * d;
      b.g.rotation.y = Math.atan2(dx, dz); fim = false; andando = true;
    } else b.g.position.set(s.to[0], 0, s.to[1]);
  } else if (s.wait) { a.t += dt; fim = a.t >= s.wait; }
  if (fim && ++a.i >= a.passos.length) {
    b.acao = null; b.caixa.visible = false; b._chave = null; rotuloDoEstado(b);
  } else if (fim) a.entrou = false;
  return { andando, s };
}
function animarBoneco(b, dt) {
  let andando = false, s = null;
  if (b.acao) ({ andando, s } = passoDaAcao(b, dt));
  const sentado = b.fora && !b.acao;
  b.banco.visible = sentado;
  b.g.position.y = sentado ? -.25 : 0;
  b.fase += dt * (andando ? 9 : 2);
  const sw = andando ? Math.sin(b.fase) * .6 : 0;
  b.pernas.forEach((p, k) => p.rotation.x = sentado ? -Math.PI / 2 : (k ? -sw : sw));
  b.bracos.forEach((br, k) => {
    if (s?.carry || (b.acao && b.caixa.visible)) br.rotation.x = -1.25;
    else if (s?.anim === 'pick') br.rotation.x = -1.9 + Math.sin(b.fase * 3 + k) * .35;
    else if (andando) br.rotation.x = k ? sw : -sw;
    else if (sentado) br.rotation.x = -.35;
    else if (b.pessoa.status === 'parado') br.rotation.x = 0;
    else br.rotation.x = -.15 + Math.sin(b.fase + k * 2) * .1;   // respiracao, sem trabalho inventado
  });
  if (b.diamante.visible) { b.diamante.rotation.y += dt * 2; b.diamante.position.y = 2.95 + Math.sin(b.fase * .8) * .06; }
}

// ---------- rua: caminhoes da Vuupt ----------
const NA_RUA = new Set(['on_route', 'in_progress', 'started']);
const ENCERRADA = new Set(['finished', 'done', 'completed', 'canceled', 'cancelled']);
const DOCA_Z = [-2, 2.5, 7, 11.5];
let caminhoes = [];
let placaRua = null;
function aplicarRua(rua) {
  caminhoes.forEach(c => descartar(c.g)); caminhoes = [];
  if (placaRua) { descartar(placaRua); placaRua = null; }
  if (!rua) { placaRua = placa('Vuupt: sem dado', '#4a5868', 25, 3.2, -8, 3); return; }
  const ativas = (rua.rotas || []).filter(r => !ENCERRADA.has((r.status || '').toLowerCase()));
  const voltando = ativas.filter(r => NA_RUA.has(r.status) && r.insucessos > 0);
  const naRua = ativas.filter(r => NA_RUA.has(r.status) && !(r.insucessos > 0));
  const doca = ativas.filter(r => !NA_RUA.has(r.status));
  const naDoca = doca.slice(0, DOCA_Z.length);
  const rua1 = [...voltando, ...naRua].slice(0, Math.max(0, MAX_CAMINHOES - naDoca.length));
  naDoca.forEach((r, i) => {
    const c = criarCaminhao(0xf4a261); c.g.position.set(20.2, 0, DOCA_Z[i]);
    trocarTextura(c.rot, texturaBalao(r.nome || 'rota', `na doca · ${r.total} pedidos`, hex(STATUS.ok)), 1.3);
    caminhoes.push(c);
  });
  let zIda = -10, zVolta = 16;
  for (const r of rua1) {
    const volta = r.insucessos > 0;
    const c = criarCaminhao(volta ? 0xe63946 : 0x4c8dff);
    if (volta) { c.g.position.set(23.6, 0, zVolta); c.g.rotation.y = Math.PI / 2; zVolta += 7; }
    else { c.g.position.set(26.4, 0, zIda); c.g.rotation.y = Math.PI / 2; zIda -= 7; }
    const l2 = volta ? `↩ voltando · ${r.insucessos}` : `${r.motorista || ''} · ${r.entregues}/${r.total} entregues`;
    trocarTextura(c.rot, texturaBalao(r.nome || 'rota', l2, hex(volta ? STATUS.parado : STATUS.ok)), 1.3);
    caminhoes.push(c);
  }
  const fora = ativas.length - naDoca.length - rua1.length;
  const resumo = `${rua.carros_na_rua ?? 0} na rua · ${rua.entregues ?? 0} entregues` + (fora > 0 ? ` · +${fora} rotas fora da tela` : '');
  placaRua = placa(resumo, '#1d3557', 25, 4.6, -4, 3);
}

// ---------- paineis HTML ----------
const fmt = v => v == null ? '—' : Number(v).toLocaleString('pt-BR');
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const hhmm = iso => iso ? String(iso).slice(11, 16) : '—';
const mmss = seg => { if (seg == null) return "—"; const s = Math.round(seg); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const NOME_AREA = { separacao: 'Separação', conferencia: 'Conferência', estoque: 'Estoque', recebimento: 'Recebimento',
  expedicao: 'Expedição', recebimento_expedicao: 'Receb./Expedição' };
const NOME_STATUS = { ok: 'ok', lento: 'lento', parado: 'parado', fora: 'fora do turno' };

function pintarPlacar(est) {
  for (const k of ['recebendo', 'estoque', 'separacao', 'conferencia', 'expedicao', 'na_rua', 'entregues', 'voltando']) {
    $('g3-c-' + k).textContent = fmt(est.placar?.[k]);
  }
  $('g3-relogio').textContent = hhmm(est.agora);
  const id = est.idade || {}, selo = $('g3-idade');
  selo.className = 'g3-selo ' + (id.stokki_nivel || 'vermelho');
  selo.textContent = id.stokki_min == null ? 'Stokki: sem coleta' : `Stokki: dados de ${id.stokki_min} min`;
  selo.title = id.vuupt_min == null ? 'Vuupt: sem coleta' : `Vuupt: dados de ${id.vuupt_min} min`;
}
function pintarLog(eventos) {
  const ult = (eventos || []).slice(-6).reverse();
  $('g3-log').innerHTML = ult.length
    ? ult.map(e => `<div><b>${hhmm(e.quando)}</b> pedido ${esc(e.id_stokki)}: ${esc(e.de || '—')} → ${esc(e.para)}` + (e.usuario ? ` · ${esc(e.usuario)}` : '') + '</div>').join('')
    : '<span class="vazio">Nenhuma mudança de etapa registrada ainda.</span>';
}
function pintarIndicadores(est) {
  const ind = est.indicadores || {}, geral = ind.conferencia_geral || {};
  $('g3-conf-geral').textContent = geral.pedidos
    ? `Conferência geral: ${fmt(geral.pedidos)} pedidos, média ${mmss(geral.media_s_ponderada)} por pedido (ponderada pelas unidades).`
    : 'Conferência geral: nenhum pedido conferido hoje.';
  const conf = new Map((ind.conferencia_por_pessoa || []).map(c => [c.usuario, c]));
  const sep = new Map((ind.produtividade_separacao || []).map(s => [s.operador, s]));
  const linhas = (est.pessoas || []).map(p => ({ nome: p.nome, area: p.area, status: p.status }));
  for (const nome of new Set([...conf.keys(), ...sep.keys()])) if (!linhas.some(l => l.nome === nome)) linhas.push({ nome, area: null, status: null });
  $('g3-indicadores').innerHTML = linhas.length ? linhas.map(l => {
    const c = conf.get(l.nome), s = sep.get(l.nome);
    const ponto = l.status ? `<span class="g3-ponto" style="background:${hex(STATUS[l.status] ?? STATUS.ok)}"></span>${esc(NOME_STATUS[l.status] || l.status)}` : '—';
    return `<tr><td>${esc(l.nome)}</td><td>${esc(NOME_AREA[l.area] || l.area || '—')}</td><td>${ponto}</td>
      <td class="num">${c ? fmt(c.pedidos) : '—'}</td><td class="num">${c ? mmss(c.media_s_ponderada) : '—'}</td>
      <td class="num">${s ? fmt(s.pedidos) : '—'}</td><td class="num">${s ? fmt(Math.round(s.unidades_por_hora)) : '—'}</td></tr>`;
  }).join('') : '<tr><td colspan="7">Ninguém na equipe nem nos dados de hoje.</td></tr>';
}
function pintarSugestoes(sugestoes) {
  const caixa = $('g3-sugestoes');
  caixa.hidden = !(sugestoes && sugestoes.length);
  $('g3-sugestoes-lista').innerHTML = (sugestoes || []).map(s =>
    `<li><b>Mover 1 da ${esc(NOME_AREA[s.de] || s.de)} para a ${esc(NOME_AREA[s.para] || s.para)}</b>: ${esc(s.motivo)}</li>`).join('');
}

// ---------- estado ----------
let ultimoId = null;
function aplicarEstado(est) {
  pintarPlacar(est); pintarLog(est.eventos); pintarIndicadores(est); pintarSugestoes(est.sugestoes);
  aplicarPessoas(est.pessoas || []);
  for (const [k, e] of Object.entries(ESTEIRAS)) e.defineFila(est.filas?.[k] ?? null);
  aplicarRua(est.rua);
  const ids = (est.eventos || []).map(e => e.id);
  const maior = ids.length ? Math.max(...ids) : 0;
  if (ultimoId == null) { ultimoId = maior; return; }   // 1a carga: so marca, nao encena
  enfileirar((est.eventos || []).filter(e => e.id > ultimoId).sort((a, b) => a.id - b.id));
  ultimoId = Math.max(ultimoId, maior);
}
function avisarConexao(texto) {
  const selo = $('g3-conexao');
  selo.textContent = texto;
  selo.hidden = false;   // mantem o ultimo retrato
}
async function buscar() {
  let est;
  try {
    const r = await fetch(API, { credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(20000) });
    // login vencido: 401/403 ou redirecionado para a tela de login (HTML no lugar do JSON)
    const tipo = r.headers.get('content-type') || '';
    if (r.status === 401 || r.status === 403 || r.redirected || (r.ok && !tipo.includes('json'))) {
      console.warn('galpao 3d: sessao expirada', r.status, r.url);
      avisarConexao('sessão expirou — faça login de novo');
      return;
    }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    est = await r.json();
  } catch (err) {
    console.warn('galpao 3d: sem conexao', err);
    avisarConexao('sem conexão');
    return;
  }
  $('g3-conexao').hidden = true;
  aplicarEstado(est);   // erro de desenho aparece no console, nao como "sem conexao"
}

// ---------- exemplo (?exemplo=1) ----------
function isoLocal(d) {
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
const minAtras = m => isoLocal(new Date(Date.now() - m * 60000));
const ESTADO_EXEMPLO = {
  agora: minAtras(0), dia_desde: minAtras(600),
  idade: { stokki_min: 2, vuupt_min: 1, stokki_nivel: 'ok' },
  placar: { recebendo: null, estoque: null, separacao: 38, conferencia: 212, expedicao: 4, na_rua: 2, entregues: 57, voltando: 3 },
  filas: { rec_estoque: null, estoque_sep: null, sep_conf: 200, conf_exp: 4 },
  pessoas: [
    { nome: 'Carlos', area: 'recebimento_expedicao', tarefa: '', status: 'fora', extra: false },
    { nome: 'Marina', area: 'recebimento_expedicao', tarefa: '', status: 'fora', extra: false },
    { nome: 'Diego', area: 'recebimento_expedicao', tarefa: '', status: 'fora', extra: false },
    { nome: 'Joana', area: 'estoque', tarefa: '', status: 'ok', extra: false },
    { nome: 'Paulo', area: 'separacao', tarefa: 'PS-42410 · separou', status: 'ok', extra: false },
    { nome: 'Rita', area: 'separacao', tarefa: 'PS-42398 · separou', status: 'lento', extra: false },
    { nome: 'Bruno', area: 'separacao', tarefa: 'PS-42377 · separou', status: 'parado', extra: false },
    { nome: 'Lucas', area: 'conferencia', tarefa: 'PS-42401 · conferindo', status: 'ok', extra: false },
    { nome: 'Fernanda', area: 'conferencia', tarefa: 'PS-42395 · conferiu', status: 'ok', extra: false },
    { nome: 'Tiago', area: 'conferencia', tarefa: 'PS-42390 · conferiu', status: 'ok', extra: false },
    { nome: 'André', area: 'separacao', tarefa: 'PS-42412 · separou', status: 'ok', extra: true },
  ],
  eventos: [
    { id: 1001, quando: minAtras(31), usuario: 'Paulo', id_stokki: 88401, de: 'Em Separação', para: 'Em Conferência' },
    { id: 1002, quando: minAtras(27), usuario: 'Lucas', id_stokki: 88377, de: 'Em Conferência', para: 'Aguardando Transportador' },
    { id: 1003, quando: minAtras(22), usuario: 'Rita', id_stokki: 88398, de: 'Em Separação', para: 'Em Conferência' },
    { id: 1004, quando: minAtras(18), usuario: 'André', id_stokki: 88412, de: 'Aberto', para: 'Em Separação' },
    { id: 1005, quando: minAtras(14), usuario: 'Fernanda', id_stokki: 88395, de: 'Em Conferência', para: 'Aguardando Transportador' },
    { id: 1006, quando: minAtras(9), usuario: 'Paulo', id_stokki: 88410, de: 'Em Separação', para: 'Em Conferência' },
    { id: 1007, quando: minAtras(6), usuario: 'Tiago', id_stokki: 88390, de: 'Em Conferência', para: 'Aguardando Transportador' },
    { id: 1008, quando: minAtras(3), usuario: 'André', id_stokki: 88412, de: 'Em Separação', para: 'Em Conferência' },
  ],
  rua: {
    rotas: [
      { nome: 'R-04', motorista: 'Iago', status: 'on_route', total: 14, entregues: 9, insucessos: 0, pendentes: 5 },
      { nome: 'R-07', motorista: 'Watson', status: 'on_route', total: 12, entregues: 6, insucessos: 2, pendentes: 4 },
      { nome: 'R-11', motorista: 'Claudinei', status: 'assigned', total: 16, entregues: 0, insucessos: 0, pendentes: 16 },
      { nome: 'R-02', motorista: 'Rafael', status: 'finished', total: 11, entregues: 10, insucessos: 1, pendentes: 0 },
    ],
    carros_na_rua: 2, entregues: 57, insucessos: 3,
  },
  indicadores: {
    conferencia_geral: { pedidos: 41, media_s_ponderada: 222 },
    conferencia_por_pessoa: [
      { usuario: 'Fernanda', pedidos: 14, unidades: 160, media_s_ponderada: 241 },
      { usuario: 'Lucas', pedidos: 15, unidades: 180, media_s_ponderada: 205 },
      { usuario: 'Tiago', pedidos: 12, unidades: 120, media_s_ponderada: 219 },
    ],
    produtividade_separacao: [
      { operador: 'André', pedidos: 6, unidades: 140, horas: 1.2, unidades_por_hora: 116.7 },
      { operador: 'Bruno', pedidos: 9, unidades: 190, horas: 3.5, unidades_por_hora: 54.3 },
      { operador: 'Paulo', pedidos: 18, unidades: 420, horas: 3.5, unidades_por_hora: 120 },
      { operador: 'Rita', pedidos: 12, unidades: 260, horas: 3.5, unidades_por_hora: 74.3 },
    ],
  },
  sugestoes: [{ de: 'separacao', para: 'conferencia',
    motivo: '200 pedidos esperando conferência, mediana 2h10; separação com 2 na fila' }],
};
// Simula o "proximo minuto": eventos sinteticos para ver a encenacao.
function iniciarExemplo() {
  const est = structuredClone(ESTADO_EXEMPLO);
  aplicarEstado(est);
  let id = 1008, vez = 0;
  const QUEM = [['Paulo', 'Em Conferência'], ['Lucas', 'Aguardando Transportador'], ['Rita', 'Em Conferência'],
    ['Tiago', 'Aguardando Transportador'], ['André', 'Em Conferência'], ['Fernanda', 'Aguardando Transportador']];
  const rodada = n => {
    for (let k = 0; k < n; k++) {
      const [usuario, para] = QUEM[vez++ % QUEM.length];
      est.eventos.push({ id: ++id, quando: isoLocal(new Date()), usuario, id_stokki: 88420 + id % 100,
        de: para === 'Em Conferência' ? 'Em Separação' : 'Em Conferência', para });
    }
    est.eventos = est.eventos.slice(-50);
    est.agora = isoLocal(new Date());
    aplicarEstado(est);
  };
  setTimeout(() => rodada(2), 3000);
  setInterval(() => rodada(2), 9000);
}

// ---------- loop ----------
if (EXEMPLO) iniciarExemplo();
else { buscar(); setInterval(buscar, INTERVALO_MS); }

const relogio = new THREE.Clock();
renderer.setAnimationLoop(() => {
  const dtReal = Math.min(relogio.getDelta(), .05);
  despachar();
  const dt = dtReal * (filaAnim.length > 10 ? 3 : 1);
  Object.values(ESTEIRAS).forEach(e => e.anda(dt));
  bonecos.forEach(b => animarBoneco(b, dt));
  controls.update();
  renderer.render(scene, camera);
});
