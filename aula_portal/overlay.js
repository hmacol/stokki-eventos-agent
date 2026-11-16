// Camada da gravacao da aula: legenda, cursor, destaque do clique e cartao
// de titulo. Injetada em toda pagina (add_init_script); nada disso existe
// no portal de verdade. window.__aulaCfg vem antes deste arquivo.
(() => {
  const cfg = window.__aulaCfg || {};
  try {
    // guia da tela (tour) nao abre sozinho no meio do video
    for (const id of ['portal', 'cotacao', 'acompanhamento', 'envios']) localStorage.setItem('tour_visto:' + id, '1');
  } catch (e) {}
  // tela dentro da moldura de celular (aula da equipe): a camada mora so na pagina de cima
  if (window.top !== window) return;

  const CSS = `
    /* reset do popover (UA poe inset:0/margin:auto/borda): vem antes, as regras abaixo ganham */
    #aula-legenda, #aula-cursor, .aula-anel, .aula-clique, #aula-cartao { inset: auto; margin: 0; border: 0; overflow: visible; }
    #aula-cursor, .aula-clique, .aula-anel { padding: 0; background: transparent; }
    #aula-legenda { position: fixed; left: 50%; bottom: 28px; transform: translateX(-50%); max-width: 82%;
      background: rgba(20,20,40,.90); color: #fff; font: 600 23px/1.35 'Segoe UI', Arial, sans-serif;
      padding: 12px 22px; border-radius: 12px; text-align: center; z-index: 2147483600; pointer-events: none;
      box-shadow: 0 10px 30px rgba(0,0,0,.25); opacity: 0; transition: opacity .25s; }
    #aula-legenda.on { opacity: 1; }
    #aula-cursor { position: fixed; left: 640px; top: 360px; width: 26px; height: 26px; z-index: 2147483646;
      pointer-events: none; transition: left .6s cubic-bezier(.4,0,.2,1), top .6s cubic-bezier(.4,0,.2,1); }
    #aula-cursor svg { width: 26px; height: 26px; filter: drop-shadow(0 2px 3px rgba(0,0,0,.35)); }
    .aula-anel { position: fixed; z-index: 2147483500; pointer-events: none; border: 3px solid #00C896; border-radius: 10px;
      box-shadow: 0 0 0 6px rgba(0,200,150,.25); animation: aula-pulso 1.1s ease-out 2; }
    @keyframes aula-pulso { 0% { box-shadow: 0 0 0 0 rgba(0,200,150,.55); } 100% { box-shadow: 0 0 0 16px rgba(0,200,150,0); } }
    .aula-clique { position: fixed; z-index: 2147483645; pointer-events: none; width: 18px; height: 18px; margin: -9px 0 0 -9px;
      border-radius: 50%; background: rgba(0,200,150,.55); animation: aula-onda .5s ease-out forwards; }
    @keyframes aula-onda { to { transform: scale(3.2); opacity: 0; } }
    #aula-cartao { position: fixed; inset: 0; width: 100vw; height: 100vh; max-width: none; max-height: none; box-sizing: border-box; z-index: 2147483640; background: #141428; display: flex; flex-direction: column;
      justify-content: center; padding: 0 110px; overflow: hidden; opacity: 0; transition: opacity .35s; pointer-events: none; }
    #aula-cartao.on { opacity: 1; }
    #aula-cartao::after { content: ""; position: absolute; right: -140px; top: -140px; width: 560px; height: 560px; border-radius: 50%;
      background: radial-gradient(circle at 30% 30%, rgba(0,200,150,.55), rgba(0,200,150,0) 70%); }
    #aula-cartao .sub { font: 700 18px 'Segoe UI', Arial, sans-serif; letter-spacing: .16em; text-transform: uppercase; color: #00C896; }
    #aula-cartao .tit { font: 800 60px/1.08 'Segoe UI', Arial, sans-serif; color: #fff; margin-top: 14px; max-width: 900px; }
    ${cfg.semDica ? '.atd-balao .dica { display: none !important; }' : ''}
    ${cfg.legendaBaixo ? `#aula-legenda { bottom: ${cfg.legendaBaixo}px; }` : ''}
    /* video em pe (celular, 360 px): mesma camada, em escala de celular */
    @media (max-width: 500px) {
      #aula-legenda { font-size: 17px; max-width: 92%; width: max-content; padding: 10px 14px; border-radius: 10px; }
      #aula-cartao { padding: 0 28px; }
      #aula-cartao::after { right: -120px; top: -120px; width: 320px; height: 320px; }
      #aula-cartao .sub { font-size: 13px; letter-spacing: .12em; }
      #aula-cartao .tit { font-size: 38px; }
    }
  `;

  // <dialog> aberto com showModal() fica na "camada de topo", acima de qualquer
  // z-index. Popover manual tambem vai pra ela, e o ultimo aberto fica por cima:
  // reabrir a camada da aula a cada uso mantem legenda/cursor visiveis.
  function topo(e) {
    if (!e || typeof e.showPopover !== 'function') return;
    if (!e.hasAttribute('popover')) e.setAttribute('popover', 'manual');
    try { e.hidePopover(); } catch (x) {}
    try { e.showPopover(); } catch (x) {}
  }

  function montar() {
    if (document.getElementById('aula-estilo')) return;
    const st = document.createElement('style'); st.id = 'aula-estilo'; st.textContent = CSS;
    document.head.appendChild(st);
    const leg = document.createElement('div'); leg.id = 'aula-legenda'; document.body.appendChild(leg);
    const cur = document.createElement('div'); cur.id = 'aula-cursor';
    cur.innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 2l16 9-7 2-3 7z" fill="#fff" stroke="#141428" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    document.body.appendChild(cur);
    const pos = window.__aulaCursor;
    if (pos) { cur.style.transition = 'none'; cur.style.left = pos[0] + 'px'; cur.style.top = pos[1] + 'px'; }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', montar); else montar();

  function el(sel) { const e = document.querySelector(sel); if (!e) throw new Error('aula: seletor nao achado ' + sel); return e; }

  window.aula = {
    legenda(texto) {
      montar();
      const leg = document.getElementById('aula-legenda');
      leg.textContent = texto || '';
      leg.classList.toggle('on', !!texto);
      topo(leg);
    },
    // move o cursor ate o centro do elemento e desenha o anel; devolve o centro
    destacar(sel, anel = true) {
      return window.aula.destacarCaixa(el(sel).getBoundingClientRect(), anel);
    },
    // o mesmo, com o retangulo ja medido (elemento dentro da moldura de celular)
    destacarCaixa(r, anel = true) {
      montar();
      const x = Math.round(r.left + r.width / 2), y = Math.round(r.top + r.height / 2);
      const cur = document.getElementById('aula-cursor');
      cur.style.transition = ''; cur.style.left = x + 'px'; cur.style.top = y + 'px';
      window.__aulaCursor = [x, y];
      topo(cur);
      if (anel) {
        document.querySelectorAll('.aula-anel').forEach(a => a.remove());
        const a = document.createElement('div'); a.className = 'aula-anel';
        const pad = 6;
        Object.assign(a.style, { left: (r.left - pad) + 'px', top: (r.top - pad) + 'px',
                                 width: (r.width + 2 * pad) + 'px', height: (r.height + 2 * pad) + 'px' });
        document.body.appendChild(a);
        topo(a);
        topo(cur);
        setTimeout(() => a.remove(), 2600);
      }
      return [x, y];
    },
    clique(x, y) {
      const o = document.createElement('div'); o.className = 'aula-clique';
      o.style.left = x + 'px'; o.style.top = y + 'px';
      document.body.appendChild(o); topo(o); setTimeout(() => o.remove(), 600);
    },
    cartao(titulo, sub) {
      montar();
      let c = document.getElementById('aula-cartao');
      if (!c) { c = document.createElement('div'); c.id = 'aula-cartao'; document.body.appendChild(c); }
      c.innerHTML = '<div class="sub"></div><div class="tit"></div>';
      c.querySelector('.sub').textContent = sub || '';
      c.querySelector('.tit').textContent = titulo || '';
      topo(c);
      topo(document.getElementById('aula-cursor'));
      requestAnimationFrame(() => c.classList.add('on'));
      window.aula.legenda('');
    },
    // marca de sincronia (gravar.achar_marcas): quadrado da cor do passo no canto, por cima de tudo
    marca(n) {
      montar();
      const [x, y, lado] = cfg.marca || [3, 3, 16];
      const paleta = cfg.paleta || [[255, 0, 0]];
      let m = document.getElementById('aula-marca');
      if (!m) { m = document.createElement('div'); m.id = 'aula-marca'; document.body.appendChild(m); }
      const [r, g, b] = paleta[n % paleta.length];
      Object.assign(m.style, { position: 'fixed', left: x + 'px', top: y + 'px', width: lado + 'px', height: lado + 'px',
                               inset: 'auto', margin: '0', padding: '0', border: '0', zIndex: '2147483647',
                               pointerEvents: 'none', background: `rgb(${r}, ${g}, ${b})` });
      m.style.left = x + 'px'; m.style.top = y + 'px';
      topo(m);
    },
    fecharCartao() {
      const c = document.getElementById('aula-cartao');
      if (c) { c.classList.remove('on'); setTimeout(() => c.remove(), 400); }
    },
  };
})();
