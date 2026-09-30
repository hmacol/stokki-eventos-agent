// Widget de atendimento do portal do cliente (chat no canto) -- 09/09/2026.
// Fala com /api/atendimento/* (chamados_web.py). Estados da conversa:
//   COM_ASSISTENTE -> triagem com o assistente (chips/botões vêm do servidor)
//   NA_FILA / EM_ATENDIMENTO -> chat ao vivo com a equipe (poll rápido)
//   AGUARDANDO_FL / RESPONDIDO -> chamado assíncrono (poll lento, e-mail)
//   RESOLVIDO -> histórico; nova mensagem reabre
(function () {
  const CFG = window.ATD_CONFIG || {};
  const BASE = CFG.base || '';
  const $ = id => document.getElementById(id);
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const ICO_BOT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="8" width="18" height="12" rx="3"></rect><circle cx="9" cy="14" r="1.5"></circle><circle cx="15" cy="14" r="1.5"></circle><line x1="12" y1="4" x2="12" y2="8"></line><circle cx="12" cy="3" r="1"></circle></svg>';
  const ICO_CLIPE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"></path></svg>';
  const ICO_RELOGIO = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>';
  const ICO_MAIS = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>';

  const st = { aberto: false, aba: 'conversa', situacao: null, chamado: null, mensagens: [], chamados: [], areas: [], emails: [],
               ultimoId: 0, timer: null, enviando: false, arquivos: [], tela: 'inicio', naoLidas: 0 };

  // ── rede ──────────────────────────────────────────────────────────────────
  async function api(caminho, opts) {
    const r = await fetch(BASE + caminho, Object.assign({ credentials: 'same-origin' }, opts || {}));
    if (r.status === 401) { location.href = `${BASE}/login?proximo=${encodeURIComponent(location.pathname + location.search)}`; throw new Error('sessão'); }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.erro || 'Falha na conexão.');
    return j;
  }

  async function carregarEstado(chamadoId) {
    const j = await api('/api/atendimento/estado' + (chamadoId ? `?chamado=${chamadoId}` : ''));
    st.situacao = j.situacao; st.chamados = j.chamados || []; st.areas = j.areas || []; st.emails = j.emails || []; st.naoLidas = j.nao_lidas || 0;
    if (j.ativo) { st.chamado = j.ativo.chamado; st.mensagens = j.ativo.mensagens; st.ultimoId = ultimo(st.mensagens); st.tela = 'conversa'; }
    else if (!st.chamado) { st.tela = 'inicio'; }
    renderBalao(); if (st.aberto) render();
  }
  const ultimo = ms => ms.length ? ms[ms.length - 1].id : 0;
  // junta mensagens novas sem duplicar (o poll e o POST podem trazer a mesma)
  function juntar(novas) {
    const vistos = new Set(st.mensagens.filter(m => Number.isInteger(m.id)).map(m => m.id));
    st.mensagens = st.mensagens.filter(m => Number.isInteger(m.id)).concat((novas || []).filter(m => !vistos.has(m.id)));
    st.ultimoId = ultimo(st.mensagens);
  }

  async function abrirChamado(id) {
    const j = await api(`/api/atendimento/chamados/${id}`);
    st.chamado = j.chamado; st.mensagens = j.mensagens; st.ultimoId = ultimo(st.mensagens); st.situacao = j.situacao || st.situacao;
    st.tela = 'conversa'; st.aba = 'conversa'; render(); agendarPoll();
  }

  async function poll() {
    if (!st.aberto || !st.chamado) { if (st.aberto) render(); return; }
    try {
      const j = await api(`/api/atendimento/chamados/${st.chamado.id}?desde=${st.ultimoId}`);
      const mudouStatus = j.chamado.status !== st.chamado.status;
      st.chamado = j.chamado; st.situacao = j.situacao || st.situacao;
      if (j.mensagens.length) juntar(j.mensagens);
      if (j.mensagens.length || mudouStatus) render(); else renderStatus();
      if (mudouStatus) carregarEstadoSilencioso();
    } catch (e) { /* tenta de novo no próximo ciclo */ }
    agendarPoll();
  }
  function agendarPoll() {
    clearTimeout(st.timer);
    if (!st.aberto) { st.timer = setTimeout(() => carregarEstado().catch(() => {}).then(agendarPoll), 60000); return; }
    const s = st.chamado ? st.chamado.status : '';
    const ms = (s === 'NA_FILA' || s === 'EM_ATENDIMENTO') ? 4000 : (s === 'COM_ASSISTENTE' ? 8000 : 15000);
    st.timer = setTimeout(poll, ms);
  }

  // ── render ────────────────────────────────────────────────────────────────
  function renderBalao() {
    const b = $('atd-badge'); const n = st.naoLidas;
    b.textContent = n; b.classList.toggle('oculto', !n);
    if (st.situacao) $('atd-dica-st').textContent = st.situacao.estado === 'online' ? `Online · ${st.situacao.horario}` : st.situacao.texto;
  }
  function renderStatus() {
    const el = $('atd-status'); const s = st.situacao; if (!s) return;
    let cls = s.estado, txt = s.texto;
    if (st.chamado && st.chamado.status === 'EM_ATENDIMENTO' && st.chamado.atendente) { cls = 'online'; txt = `Online · ${st.chamado.atendente} está atendendo você`; }
    else if (s.estado === 'online') txt = `Online · ${s.horario}`;
    el.className = 'st ' + cls; el.innerHTML = `<i></i><span>${esc(txt)}</span>`;
  }
  function render() {
    renderStatus(); renderBalao();
    document.querySelectorAll('#atd-widget .atd-abas button').forEach(b => b.classList.toggle('ativa', b.dataset.aba === st.aba));
    const nl = st.chamados.reduce((n, c) => n + (c.nao_lidas || 0), 0);
    $('atd-aba-badge').textContent = nl; $('atd-aba-badge').classList.toggle('oculto', !nl);
    if (st.aba === 'chamados') renderLista();
    else if (st.tela === 'form') renderForm();
    else if (st.tela === 'conversa' && st.chamado) renderConversa();
    else renderInicio();
  }

  function bannerHorario() {
    const s = st.situacao; if (!s || s.estado === 'online') return '';
    const titulo = s.motivo === 'almoco' ? 'Nosso atendimento está em horário de almoço.' : (s.estado === 'ausente' ? 'Nossos atendentes estão ocupados no momento.' : 'Estamos fora do horário de atendimento.');
    const volta = s.volta_em ? ` Voltamos <b>${esc(s.volta_em)}</b>.` : '';
    return `<div class="atd-aviso">${ICO_RELOGIO}<div class="t"><b>${titulo}</b><span>Atendemos ${esc(s.horario)}.${volta}</span></div></div>`;
  }

  function renderInicio() {
    $('atd-pe').classList.add('oculto');
    const s = st.situacao || {};
    const chips = st.areas.map(a => `<button type="button" class="atd-chip" data-area="${esc(a.valor)}">${esc(a.rotulo)}</button>`).join('');
    let intro;
    if (s.estado === 'online') intro = 'Sou o assistente da Fresh Log. Sobre o que você precisa hoje? Se eu não resolver, chamo um atendente.';
    else intro = 'Sou o assistente da Fresh Log. Posso tentar te ajudar agora, ou você deixa um chamado e a equipe responde por e-mail e aqui no portal assim que voltar.';
    $('atd-corpo').innerHTML = bannerHorario() + `
      <div class="atd-msg"><div class="av bot">${ICO_BOT}</div><div class="col"><div class="atd-bolha">${esc(intro)}
        <div class="atd-opcoes">${chips}</div>
        <div class="atd-opcoes"><button type="button" class="atd-chip principal" data-form="1">${ICO_MAIS} Deixar um chamado</button></div></div>
        <div class="atd-meta">Assistente Fresh Log</div></div></div>`;
  }

  function renderConversa() {
    const c = st.chamado; const corpo = $('atd-corpo');
    const partes = [];
    if (c.status !== 'RESOLVIDO' && c.status !== 'EM_ATENDIMENTO' && c.status !== 'NA_FILA') partes.push(bannerHorario());
    partes.push(`<div class="atd-sistema">Chamado #${c.id}${c.assunto ? ' · ' + esc(c.assunto) : ''} · ${esc(c.status_rotulo)}</div>`);
    for (const m of st.mensagens) partes.push(bolha(m, c));
    if (st.enviando) partes.push('<div class="atd-digitando">…</div>');
    corpo.innerHTML = partes.join('');
    corpo.scrollTop = corpo.scrollHeight;
    const pe = $('atd-pe'); pe.classList.remove('oculto');
    const txt = $('atd-rodape-txt');
    const mail = st.emails.length ? st.emails[0] : 'seu e-mail';
    if (c.status === 'COM_ASSISTENTE') txt.textContent = 'Assistente automático · pode pedir um atendente a qualquer momento';
    else if (c.status === 'NA_FILA') txt.textContent = 'Aguardando um atendente assumir…';
    else if (c.status === 'EM_ATENDIMENTO') txt.textContent = `Chamado #${c.id} · o histórico vai pro seu e-mail ao encerrar`;
    else if (c.status === 'RESOLVIDO') txt.textContent = 'Chamado resolvido · escrever aqui reabre o chamado';
    else txt.textContent = `As respostas chegam aqui e em ${mail}. Você também pode responder pelo e-mail.`;
    $('atd-encerrar').classList.toggle('oculto', c.status === 'RESOLVIDO');
    $('atd-encerrar').textContent = c.status === 'COM_ASSISTENTE' ? 'Encerrar' : 'Encerrar conversa';
    $('atd-nova').classList.toggle('oculto', !['AGUARDANDO_FL', 'RESPONDIDO', 'RESOLVIDO'].includes(c.status));
    renderPendentes();
  }

  function bolha(m, c) {
    const anexos = (m.anexos || []).map(a => `<a class="atd-anexo" href="${BASE}/api/atendimento/chamados/${c.id}/anexos/${encodeURIComponent(a.arquivo)}" target="_blank" rel="noopener">${ICO_CLIPE}<span>${esc(a.nome)}</span></a>`).join(' ');
    const canal = m.canal === 'email' ? ' · por e-mail' : '';
    if (m.origem === 'sistema') return `<div class="atd-sistema">${esc(m.texto)} · ${esc(m.hora)}</div>`;
    if (m.origem === 'cliente') return `<div class="atd-msg eu"><div class="atd-bolha">${esc(m.texto)}${anexos ? '<br>' + anexos : ''}</div><div class="atd-meta"><span>Você</span><span>·</span><span>${esc(m.hora)}${canal}</span></div></div>`;
    const bot = m.origem === 'assistente';
    const opcoes = (m.opcoes || []).length ? `<div class="atd-opcoes">${m.opcoes.map(o => `<button type="button" class="atd-chip ${o.estilo === 'principal' ? 'principal' : ''}" data-acao="${esc(o.acao)}" data-valor="${esc(o.valor || '')}" data-rotulo="${esc(o.rotulo)}">${esc(o.rotulo)}</button>`).join('')}</div>` : '';
    const nome = bot ? 'Assistente Fresh Log' : `${esc(m.autor || 'Fresh Log')} · Fresh Log`;
    const av = bot ? `<div class="av bot">${ICO_BOT}</div>` : `<div class="av fl">${esc((m.autor || 'F').trim().charAt(0).toUpperCase())}</div>`;
    return `<div class="atd-msg">${av}<div class="col"><div class="atd-bolha">${esc(m.texto)}${anexos ? '<br>' + anexos : ''}${opcoes}</div><div class="atd-meta"><span>${nome}</span><span>·</span><span>${esc(m.hora)}${canal}</span></div></div></div>`;
  }

  function renderPendentes() {
    const el = $('atd-pendentes');
    el.classList.toggle('oculto', !st.arquivos.length);
    el.innerHTML = st.arquivos.map((f, i) => `<span>${esc(f.name)} <b data-rm="${i}">×</b></span>`).join('');
  }

  function renderLista() {
    $('atd-pe').classList.add('oculto');
    const itens = st.chamados.map(c => `
      <div class="atd-item ${c.nao_lidas ? 'nl' : ''}" data-abrir="${c.id}"><span class="pt"></span><div class="c">
        <div class="l1"><span class="n">#${c.id}</span><span class="a">${esc(c.assunto || c.area_rotulo || 'Conversa com o assistente')}</span><span class="q">${esc(c.quando)}</span></div>
        <div class="l2">${esc(previa(c))}</div>
        <div class="l3"><span class="atd-pill ${c.status}"><i></i>${esc(c.status_rotulo)}</span><span>${esc(c.area_rotulo || '')}</span></div></div></div>`).join('');
    $('atd-corpo').innerHTML = (itens || '<div class="atd-vazio">Você ainda não abriu nenhum chamado.</div>') +
      `<div class="atd-vazio" style="padding-top:4px">Chamados ficam guardados aqui por 12 meses.</div>
       <div style="display:flex;justify-content:center;gap:8px;padding-bottom:4px"><button type="button" class="atd-botao" data-form="1">${ICO_MAIS} Abrir chamado</button><button type="button" class="atd-botao linha" data-nova="1">Falar com o assistente</button></div>`;
  }
  function previa(c) {
    if (c.status === 'RESOLVIDO') return c.historico_enviado_em ? 'Resolvido · histórico enviado pro seu e-mail' : 'Resolvido';
    const t = (c.ultima_texto || '').replace(/\s+/g, ' ');
    const quem = c.ultima_origem_msg === 'cliente' ? 'Você: ' : (c.ultima_origem_msg === 'equipe' ? 'Fresh Log: ' : (c.ultima_origem_msg === 'assistente' ? 'Assistente: ' : ''));
    return quem + t;
  }

  function renderForm() {
    $('atd-pe').classList.add('oculto');
    const areas = st.areas.map(a => `<option value="${esc(a.valor)}">${esc(a.rotulo)}</option>`).join('');
    const mail = st.emails.length ? st.emails.join(', ') : 'seu e-mail cadastrado';
    $('atd-corpo').innerHTML = bannerHorario() + `
      <form class="atd-form" id="atd-form">
        <label>Assunto<input name="assunto" maxlength="120" required placeholder="Ex.: Comprovante da NF 45210"></label>
        <div class="g2">
          <label>Área<select name="area">${areas}</select></label>
          <label>Pedido / NF<input name="pedido" maxlength="40" placeholder="opcional"></label>
        </div>
        <label>Mensagem<textarea name="mensagem" rows="4" required placeholder="Conte o que aconteceu ou o que você precisa"></textarea></label>
        <label>Anexos<input type="file" name="anexos" multiple accept=".jpg,.jpeg,.png,.gif,.webp,.pdf,.xlsx,.xls,.csv,.txt,.xml"></label>
        <div class="erro oculto" id="atd-form-erro"></div>
        <div class="acoes"><button type="button" class="atd-botao linha" data-voltar="1">Voltar</button><span style="flex:1"></span><button type="submit" class="atd-botao" id="atd-form-enviar">Deixar chamado</button></div>
        <div style="font-size:11px;color:var(--texto-suave)">Você recebe a resposta em ${esc(mail)} e aqui no portal.</div>
      </form>`;
    $('atd-form').addEventListener('submit', enviarForm);
  }

  // ── ações ─────────────────────────────────────────────────────────────────
  async function iniciarConversa(areaValor, areaRotulo) {
    if (st.enviando) return;
    st.enviando = true; render();
    try {
      const j = await api('/api/atendimento/conversas', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
      st.chamado = j.chamado; st.mensagens = j.mensagens; st.ultimoId = ultimo(st.mensagens); st.tela = 'conversa'; st.aba = 'conversa';
      st.enviando = false;
      if (areaValor) await enviar(areaRotulo, areaValor);
      else { render(); agendarPoll(); }
    } catch (e) { st.enviando = false; alert(e.message); render(); }
  }

  async function enviar(texto, chip) {
    if (!st.chamado || st.enviando) return;
    texto = (texto || '').trim();
    if (!texto && !st.arquivos.length) return;
    const fd = new FormData();
    fd.append('texto', texto);
    if (chip) fd.append('chip', chip);
    for (const f of st.arquivos) fd.append('anexos', f, f.name);
    st.enviando = true;
    // eco otimista
    st.mensagens.push({ id: st.ultimoId + 0.5, origem: 'cliente', texto: texto || '(anexo)', hora: new Date().toTimeString().slice(0, 5), anexos: [] });
    for (const m of st.mensagens) m.opcoes = [];
    $('atd-texto').value = ''; st.arquivos = []; $('atd-arquivos').value = '';
    render();
    try {
      const j = await api(`/api/atendimento/chamados/${st.chamado.id}/mensagens`, { method: 'POST', body: fd });
      juntar(j.mensagens); st.chamado = j.chamado; st.situacao = j.situacao || st.situacao;
    } catch (e) {
      st.mensagens = st.mensagens.filter(m => Number.isInteger(m.id));
      alert(e.message);
    }
    st.enviando = false; render(); agendarPoll();
    carregarEstadoSilencioso();
  }

  async function acao(tipo) {
    if (!st.chamado || st.enviando) return;
    if (tipo === 'resolvido' && st.chamado.status !== 'COM_ASSISTENTE' && !confirm('Encerrar a conversa? O histórico completo vai pro seu e-mail.')) return;
    st.enviando = true; render();
    try {
      const j = await api(`/api/atendimento/chamados/${st.chamado.id}/acao`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ tipo }) });
      juntar(j.mensagens); st.chamado = j.chamado; st.situacao = j.situacao || st.situacao;
    } catch (e) { alert(e.message); }
    st.enviando = false; render(); agendarPoll();
    carregarEstadoSilencioso();
  }

  async function enviarForm(ev) {
    ev.preventDefault();
    const form = ev.target, erro = $('atd-form-erro'), btn = $('atd-form-enviar');
    erro.classList.add('oculto'); btn.disabled = true; btn.textContent = 'Enviando…';
    try {
      const j = await api('/api/atendimento/chamados', { method: 'POST', body: new FormData(form) });
      st.chamado = j.chamado; st.mensagens = j.mensagens; st.ultimoId = ultimo(st.mensagens); st.tela = 'conversa'; st.aba = 'conversa';
      render(); agendarPoll(); carregarEstadoSilencioso();
    } catch (e) { erro.textContent = e.message; erro.classList.remove('oculto'); btn.disabled = false; btn.textContent = 'Deixar chamado'; }
  }

  function carregarEstadoSilencioso() {
    api('/api/atendimento/estado').then(j => { st.chamados = j.chamados || []; st.naoLidas = j.nao_lidas || 0; st.situacao = j.situacao || st.situacao; renderBalao(); if (st.aba === 'chamados') render(); }).catch(() => {});
  }

  function abrir(sim) {
    st.aberto = sim;
    $('atd-widget').classList.toggle('oculto', !sim);
    $('atd-balao').classList.toggle('oculto', sim);
    try { sessionStorage.setItem('atd_aberto', sim ? '1' : '0'); } catch (e) {}
    if (sim) { render(); poll(); } else agendarPoll();
  }

  // ── eventos ───────────────────────────────────────────────────────────────
  $('atd-abrir').addEventListener('click', () => abrir(true));
  $('atd-dica').addEventListener('click', () => abrir(true));
  $('atd-minimizar').addEventListener('click', () => abrir(false));
  document.querySelectorAll('#atd-widget .atd-abas button').forEach(b => b.addEventListener('click', () => { st.aba = b.dataset.aba; if (st.aba === 'conversa' && st.tela === 'form') st.tela = st.chamado ? 'conversa' : 'inicio'; render(); if (st.aba === 'chamados') carregarEstadoSilencioso(); }));
  $('atd-corpo').addEventListener('click', e => {
    const area = e.target.closest('[data-area]'); if (area) { iniciarConversa(area.dataset.area, area.textContent.trim()); return; }
    const form = e.target.closest('[data-form]'); if (form) { st.tela = 'form'; st.aba = 'conversa'; render(); return; }
    const nova = e.target.closest('[data-nova]'); if (nova) { st.chamado = null; st.mensagens = []; st.tela = 'inicio'; st.aba = 'conversa'; render(); return; }
    const voltar = e.target.closest('[data-voltar]'); if (voltar) { st.tela = st.chamado ? 'conversa' : 'inicio'; render(); return; }
    const item = e.target.closest('[data-abrir]'); if (item) { abrirChamado(Number(item.dataset.abrir)).catch(err => alert(err.message)); return; }
    const op = e.target.closest('[data-acao]');
    if (op) {
      if (op.dataset.acao === 'chip') enviar(op.dataset.rotulo, op.dataset.valor);
      else acao(op.dataset.acao);
    }
  });
  $('atd-enviar').addEventListener('click', () => enviar($('atd-texto').value));
  $('atd-texto').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); enviar($('atd-texto').value); } });
  $('atd-texto').addEventListener('input', e => { e.target.style.height = 'auto'; e.target.style.height = Math.min(e.target.scrollHeight, 110) + 'px'; });
  $('atd-anexar').addEventListener('click', () => $('atd-arquivos').click());
  $('atd-arquivos').addEventListener('change', e => { st.arquivos = Array.from(e.target.files || []).slice(0, 5); renderPendentes(); });
  $('atd-pendentes').addEventListener('click', e => { const b = e.target.closest('[data-rm]'); if (b) { st.arquivos.splice(Number(b.dataset.rm), 1); renderPendentes(); } });
  $('atd-encerrar').addEventListener('click', () => acao('resolvido'));
  $('atd-nova').addEventListener('click', () => { st.chamado = null; st.mensagens = []; st.tela = 'inicio'; st.aba = 'conversa'; render(); });

  // ── início ────────────────────────────────────────────────────────────────
  const inicial = CFG.chamadoInicial ? Number(CFG.chamadoInicial) : 0;
  carregarEstado(inicial || undefined).then(() => {
    let abrirAgora = !!inicial;
    try { abrirAgora = abrirAgora || sessionStorage.getItem('atd_aberto') === '1'; } catch (e) {}
    if (abrirAgora) abrir(true); else agendarPoll();
  }).catch(() => { $('atd-dica-st').textContent = 'Atendimento indisponível agora'; });

  // Chamado por outra tela (aba Envios, bloqueio de área — Hugo 23/09):
  // abre o widget num chamado e, se vier texto, já manda como mensagem.
  window.atdAbrirChamado = async function (chamadoId, textoPronto) {
    try {
      await abrirChamado(Number(chamadoId));
      abrir(true);
      if (textoPronto) await enviar(textoPronto);
    } catch (e) { alert(e.message || 'Não foi possível abrir a conversa.'); }
  };
})();
