// Cliente HTTP da API do motorista (nucleo/api_motorista.py).
//
// - Base configurável em app.json -> extra.apiUrl (produção:
//   https://app.freshhub.com.br/motorista/api). Em desenvolvimento aponte
//   pro IP da máquina rodando `py -3.11 nucleo/api_motorista.py --porta 8073`.
// - Token de acesso no header; 401 dispara UMA renovação via refresh e
//   repete a chamada. Falhou a renovação -> sessão cai (ouvintes avisados).
// - Sem rede/timeout -> ErroRede (a fila offline usa isso pra decidir
//   se guarda pra depois).
import { Platform } from 'react-native';
import Constants from 'expo-constants';
import * as FileSystem from 'expo-file-system/legacy';
import * as segredo from './segredo';
import { postarArquivo } from './envioArquivo';

// PWA (motorista.freshhub.com.br): a API responde no MESMO domínio, em /api. É o que mantém
// o token do motorista numa origem só dele (sem painel/portal por perto).
export const API_URL: string = Platform.OS === 'web'
  ? `${window.location.origin}/api`
  : (Constants.expoConfig?.extra as { apiUrl?: string } | undefined)?.apiUrl ?? 'http://10.0.2.2:8073/api';

const CHAVE_ACESSO = 'motorista.acesso';
const CHAVE_REFRESH = 'motorista.refresh';
const TIMEOUT_MS = 20000;

export class ErroApi extends Error {
  status: number;
  /** Corpo JSON da resposta (ex.: falhas do carregamento), quando veio. */
  dados: unknown;
  constructor(status: number, mensagem: string, dados: unknown = null) {
    super(mensagem);
    this.status = status;
    this.dados = dados;
  }
}
export class ErroRede extends Error {}

let acesso: string | null = null;
let refresh: string | null = null;
let ouvintesSessaoCaiu: (() => void)[] = [];
let renovando: Promise<boolean> | null = null;

export function aoCairSessao(fn: () => void): () => void {
  ouvintesSessaoCaiu.push(fn);
  return () => {
    ouvintesSessaoCaiu = ouvintesSessaoCaiu.filter((f) => f !== fn);
  };
}

export async function carregarTokens(): Promise<boolean> {
  acesso = await segredo.ler(CHAVE_ACESSO);
  refresh = await segredo.ler(CHAVE_REFRESH);
  return !!acesso;
}

export async function guardarTokens(t: { acesso: string; refresh: string }) {
  acesso = t.acesso;
  refresh = t.refresh;
  await segredo.gravar(CHAVE_ACESSO, t.acesso);
  await segredo.gravar(CHAVE_REFRESH, t.refresh);
}

export async function limparTokens() {
  acesso = null;
  refresh = null;
  await segredo.apagar(CHAVE_ACESSO);
  await segredo.apagar(CHAVE_REFRESH);
}

export function temSessao(): boolean {
  return !!acesso;
}

async function fetchComTimeout(url: string, init: RequestInit): Promise<Response> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    return await fetch(url, { ...init, signal: ctrl.signal });
  } catch (e) {
    throw new ErroRede((e as Error).message || 'sem rede');
  } finally {
    clearTimeout(timer);
  }
}

async function tentarRenovar(): Promise<boolean> {
  if (!refresh) return false;
  if (!renovando) {
    renovando = (async () => {
      try {
        const r = await fetchComTimeout(`${API_URL}/refresh`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh }),
        });
        if (!r.ok) return false;
        const t = (await r.json()) as { acesso: string; refresh: string };
        await guardarTokens(t);
        return true;
      } catch {
        return false;
      } finally {
        renovando = null;
      }
    })();
  }
  return renovando;
}

type Opcoes = { metodo?: 'GET' | 'POST' | 'PUT'; corpo?: unknown; semAuth?: boolean };

export async function chamar<T>(caminho: string, op: Opcoes = {}, repetiu = false): Promise<T> {
  // X-App-Plataforma: 'web' (PWA) é avisado por WhatsApp, não por push (nucleo/avisos_app_whatsapp.py).
  const headers: Record<string, string> = { 'X-App-Versao': Constants.expoConfig?.version ?? '', 'X-App-Plataforma': Platform.OS };
  if (!op.semAuth && acesso) headers.Authorization = `Bearer ${acesso}`;
  let body: BodyInit | undefined;
  if (op.corpo !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(op.corpo);
  }
  const r = await fetchComTimeout(`${API_URL}${caminho}`, { method: op.metodo ?? 'GET', headers, body });

  if (r.status === 401 && !op.semAuth && !repetiu) {
    if (await tentarRenovar()) return chamar<T>(caminho, op, true);
    await limparTokens();
    ouvintesSessaoCaiu.forEach((f) => f());
  }
  let dados: unknown = null;
  try {
    dados = await r.json();
  } catch {
    dados = null;
  }
  if (!r.ok) {
    const msg = (dados as { erro?: string } | null)?.erro ?? `Erro ${r.status}`;
    throw new ErroApi(r.status, msg, dados);
  }
  return dados as T;
}

// ── chamadas de alto nível ────────────────────────────────────────────────

import type { Ajuste, CadastroCriado, Desmarcado, RespostaCarregamento, CadastroSituacao, Checklist, EstadoAtendimento, Extrato, MeusDados, Motorista, MudancaCadastro, NovoCadastro, Oferta, Pedagio, RespostaChamado, ResultadoValidacao, Rota, TipoDespesa, Trajeto } from './tipos';

export async function login(cpf: string, pin: string): Promise<Motorista> {
  const r = await chamar<{ acesso: string; refresh: string; motorista: Motorista }>('/login', {
    metodo: 'POST',
    corpo: { cpf, pin },
    semAuth: true,
  });
  await guardarTokens(r);
  return r.motorista;
}

/** Troca de PIN pelo motorista. A API devolve tokens novos (o hash muda e derruba a sessão antiga). */
export async function trocarPin(pinAtual: string, pinNovo: string): Promise<Motorista> {
  const r = await chamar<{ acesso: string; refresh: string; motorista: Motorista }>('/trocar-pin', {
    metodo: 'POST',
    corpo: { pin_atual: pinAtual, pin_novo: pinNovo },
  });
  await guardarTokens(r);
  return r.motorista;
}

/** Versao atual do APK (sem login; semAuth evita header e fluxo de sessao caindo). */
export const appVersao = () =>
  chamar<{ versao_atual: string | null; link: string }>('/app-versao', { semAuth: true });
export const eu = () => chamar<Motorista>('/eu');
/** Diagnóstico (17/11): a API loga o caminho de cada chamada; o erro de envio
 * de foto vai junto pra aparecer no log do servidor (antes só no Perfil). */
export const diagnostico = (texto: string) =>
  chamar<Motorista>(`/eu?diag=${encodeURIComponent(texto.slice(0, 300))}`).then(() => undefined, () => undefined);
/** Meus dados (Hugo, 08/10): telefone, e-mail, PIX e placa valem na hora. */
export const atualizarMeusDados = (dados: MeusDados) =>
  chamar<{ motorista: Motorista; mudancas: MudancaCadastro[] }>('/eu', { metodo: 'PUT', corpo: dados });
/** Auto-cadastro, sem login. A chave_envio autoriza só o envio dos documentos deste cadastro. */
export const criarCadastro = (dados: NovoCadastro) =>
  chamar<CadastroCriado>('/cadastro', { metodo: 'POST', corpo: dados, semAuth: true });
export const enviarDocumentoCadastro = (id: number, chave: string, tipo: 'cnh' | 'crlv', uri: string) =>
  enviarArquivo<CadastroSituacao>(`/cadastro/${id}/documentos`, uri, { tipo, chave_envio: chave }, true, 'arquivo');
export const rotas = (de?: string, ate?: string) =>
  chamar<{ rotas: Rota[] }>(`/rotas${de ? `?de=${de}&ate=${ate ?? de}` : ''}`).then((r) => r.rotas);
export const rota = (id: number) => chamar<Rota>(`/rotas/${id}`);
/** Linha do trajeto pro Mapa da rota (Google calculado e guardado no servidor). */
export const trajeto = (rotaId: number) => chamar<Trajeto>(`/rotas/${rotaId}/trajeto`);
export const aceitarRota = (id: number, corpo: object) => chamar<Rota>(`/rotas/${id}/aceitar`, { metodo: 'POST', corpo });
export const recusarRota = (id: number, motivo: string) => chamar<Rota>(`/rotas/${id}/recusar`, { metodo: 'POST', corpo: { motivo } });
export const iniciarRota = (id: number, corpo: object) => chamar<Rota>(`/rotas/${id}/iniciar`, { metodo: 'POST', corpo });
export const finalizarRota = (id: number, corpo: object) => chamar<Rota>(`/rotas/${id}/finalizar`, { metodo: 'POST', corpo });
export const eventoParada = (paradaId: number, corpo: object) =>
  chamar<{ ja_registrado: boolean; rota_status: string }>(`/paradas/${paradaId}/eventos`, { metodo: 'POST', corpo });
/** Baixa em grupo: `itens` = um {parada_id, uuid} por pedido; o resto é o corpo do evento de parada. */
export const eventoLote = (corpo: object) =>
  chamar<{ ja_registrado: boolean; rota_status: string }>('/paradas/lote/eventos', { metodo: 'POST', corpo });
export const enviarGps = (pontos: object[]) => chamar<{ novos: number }>('/gps', { metodo: 'POST', corpo: { pontos } });
export const checklist = () => chamar<Checklist>('/checklist');
export const ofertas = () => chamar<{ abertas: Oferta[]; minhas: Oferta[] }>('/ofertas');
export const escolherOferta = (id: number) => chamar<{ abertas: Oferta[]; minhas: Oferta[] }>(`/ofertas/${id}/escolher`, { metodo: 'POST' });
export const cancelarOferta = (id: number) => chamar<{ abertas: Oferta[]; minhas: Oferta[] }>(`/ofertas/${id}/cancelar`, { metodo: 'POST' });
export const financeiro = (de: string, ate: string) => chamar<Extrato>(`/financeiro?de=${de}&ate=${ate}`);
export const disponibilidade = (de: string, ate: string) => chamar<{ ajustes: Ajuste[] }>(`/disponibilidade?de=${de}&ate=${ate}`).then((r) => r.ajustes);
export const definirDisponibilidade = (corpo: object) => chamar<{ dias: number; ajustes: Ajuste[] }>('/disponibilidade', { metodo: 'PUT', corpo });
export const registrarPushToken = (token: string) => chamar<{ ok: boolean }>('/push-token', { metodo: 'POST', corpo: { token } });
/** Desiste de um pedágio já enviado (só enquanto PENDENTE; 409 se o painel já revisou). */
export const cancelarPedagio = (rotaId: number, pedagioId: number) =>
  chamar<{ pedagios: Pedagio[] }>(`/rotas/${rotaId}/pedagios/${pedagioId}/cancelar`, { metodo: 'POST' }).then((r) => r.pedagios);

// Upload de foto (11/09): pelo uploader NATIVO do expo-file-system, não
// pelo fetch + FormData do RN. Motivo: no teste do Hugo nenhuma foto
// (canhoto/pedágio) chegou ao servidor, e como a fila é sequencial a
// foto travada segurava as chegadas/entregas atrás dela. O uploader
// nativo lê o arquivo direto do disco, sem timeout de JS, e devolve o
// status HTTP como qualquer chamada.
async function enviarArquivo<T>(caminho: string, uri: string, campos: Record<string, string>, repetiu = false, campoArquivo = 'arquivo'): Promise<T> {
  let r: { status: number; body: string } | null;
  try {
    r = await postarArquivo(`${API_URL}${caminho}`, uri, campoArquivo, campos, acesso ? { Authorization: `Bearer ${acesso}` } : {});
  } catch (e) {
    throw new ErroRede((e as Error).message || 'falha no envio da foto');
  }
  if (!r) {
    // Foto sumiu do aparelho: não adianta insistir (vira "recusado" na fila)
    throw new ErroApi(410, 'A foto não está mais no aparelho -- registre de novo.');
  }
  if (r.status === 401 && !repetiu) {
    if (await tentarRenovar()) return enviarArquivo<T>(caminho, uri, campos, true, campoArquivo);
    await limparTokens();
    ouvintesSessaoCaiu.forEach((f) => f());
  }
  let dados: unknown = null;
  try {
    dados = JSON.parse(r.body);
  } catch {
    dados = null;
  }
  if (r.status < 200 || r.status >= 300) {
    const msg = (dados as { erro?: string } | null)?.erro ?? `Erro ${r.status}`;
    throw new ErroApi(r.status, msg, dados);
  }
  return dados as T;
}

export async function enviarComprovante(paradaId: number, uri: string, tipo: string, uuid: string, capturadoEm: string, nf?: string | null) {
  return enviarArquivo<{ id: number; ja_registrado: boolean; gcs: boolean }>(`/paradas/${paradaId}/comprovantes`, uri, {
    tipo, uuid, capturado_em: capturadoEm, ...(nf ? { nf } : {}),
  });
}

/** Foto única do grupo: o servidor grava uma vez e liga a todos os pedidos de `itens`. */
export async function enviarComprovanteLote(uri: string, tipo: string, uuid: string, capturadoEm: string, itens: { parada_id: number; uuid: string }[]) {
  return enviarArquivo<{ ja_registrado: boolean; gcs: boolean }>('/paradas/lote/comprovantes', uri, {
    tipo, uuid, capturado_em: capturadoEm, itens: JSON.stringify(itens),
  });
}

/** Confere a foto ANTES de enviar, com o motorista ainda no cliente
 * (Hugo, 12/09): nitidez + legibilidade + número da NF no canhoto.
 * A foto NÃO é gravada aqui -- o envio de verdade segue pela fila. Com a
 * validação desligada no servidor volta NAO_VERIFICADO/pode_seguir. */
export async function validarFoto(
  uri: string,
  campos: { tipo: 'CANHOTO' | 'PEDAGIO' | 'CARREGAMENTO'; paradaId?: number; rotaId?: number; nf?: string | null; valor?: number; tentativa: number },
): Promise<ResultadoValidacao> {
  return enviarArquivo<ResultadoValidacao>('/fotos/validar', uri, {
    tipo: campos.tipo,
    tentativa: String(campos.tentativa),
    ...(campos.paradaId ? { parada_id: String(campos.paradaId) } : {}),
    ...(campos.rotaId ? { rota_id: String(campos.rotaId) } : {}),
    ...(campos.nf ? { nf: campos.nf } : {}),
    ...(campos.valor !== undefined ? { valor: String(campos.valor) } : {}),
  });
}

/** Checklist de carregamento (antes de iniciar a rota): foto do baú + quem carregou +
 * pedidos desmarcados. NÃO usa a fila offline: tirar o pedido da Vuupt é síncrono, então
 * precisa de rede. 409 vem como ErroApi com `dados` (falhas / veredito da foto). */
export async function confirmarCarregamento(rotaId: number, uri: string, carregador: string, uuid: string, desmarcados: Desmarcado[], tentativa: number) {
  return enviarArquivo<RespostaCarregamento>(`/rotas/${rotaId}/carregamento`, uri, {
    carregador, uuid, desmarcados: JSON.stringify(desmarcados), tentativa: String(tentativa),
  });
}

/** Pedágio / despesa da rota: valor + foto do recibo (Hugo, 11/09 e 14/09). Fica pendente até o painel aprovar.
 *  `extra`: tipo (sem = pedágio), descricao (Outros) e paradaId (Estacionamento/Descarga). */
export async function enviarPedagio(rotaId: number, uri: string, valor: number, uuid: string, capturadoEm: string,
  extra: { tipo?: TipoDespesa; descricao?: string | null; paradaId?: number | null; origem?: 'camera' | 'galeria' } = {}) {
  const campos: Record<string, string> = { valor: String(valor), uuid, capturado_em: capturadoEm };
  if (extra.origem) campos.origem = extra.origem;
  if (extra.tipo) campos.tipo = extra.tipo;
  if (extra.descricao) campos.descricao = extra.descricao;
  if (extra.paradaId) campos.parada_id = String(extra.paradaId);
  return enviarArquivo<{ id: number; ja_registrado: boolean; gcs: boolean; pedagios: Pedagio[] }>(`/rotas/${rotaId}/pedagios`, uri, campos);
}

// ── Atendimento (aba Ajuda) ────────────────────────────────────────────────
// Texto vai em JSON; mensagem COM foto vai pelo uploader nativo (o FormData
// do React Native não é confiável -- ver src/fila.ts).

export const atendimentoEstado = (chamadoId?: number) =>
  chamar<EstadoAtendimento>(`/atendimento/estado${chamadoId ? `?chamado=${chamadoId}` : ''}`);
export const atendimentoNaoLidas = () => chamar<{ nao_lidas: number }>('/atendimento/nao-lidas');
// Com paradaId: "Solicitar ajuda para este pedido" -- a conversa já nasce no pedido.
export const iniciarConversa = (paradaId?: number) =>
  chamar<RespostaChamado>('/atendimento/conversas', { metodo: 'POST', ...(paradaId ? { corpo: { parada_id: paradaId } } : {}) });
export const verChamado = (id: number, desde = 0) =>
  chamar<RespostaChamado>(`/atendimento/chamados/${id}?desde=${desde}`);
export const enviarMensagemChamado = (id: number, texto: string, chip?: string | null) =>
  chamar<RespostaChamado>(`/atendimento/chamados/${id}/mensagens`, { metodo: 'POST', corpo: { texto, chip: chip ?? '' } });
export const enviarFotoChamado = (id: number, uri: string, texto: string) =>
  enviarArquivo<RespostaChamado>(`/atendimento/chamados/${id}/mensagens`, uri, { texto }, false, 'anexos');
// Áudio (Hugo, 08/10): só quando a conversa já está com a logística -- a API
// recusa (409) enquanto o assistente atende.
export const enviarAudioChamado = (id: number, uri: string) =>
  enviarArquivo<RespostaChamado>(`/atendimento/chamados/${id}/mensagens`, uri, { texto: 'Áudio' }, false, 'anexos');
/** Baixa um anexo da conversa pro cache (a URL exige o token, então o player
 * não consegue buscar direto). Devolve o caminho local. */
export async function baixarAnexoChamado(chamadoId: number, arquivo: string): Promise<string> {
  const destino = `${FileSystem.cacheDirectory}chamado-${chamadoId}-${arquivo}`;
  const info = await FileSystem.getInfoAsync(destino).catch(() => ({ exists: false }));
  if (info.exists) return destino;
  let r: FileSystem.FileSystemDownloadResult;
  try {
    r = await FileSystem.downloadAsync(`${API_URL}/atendimento/chamados/${chamadoId}/anexos/${encodeURIComponent(arquivo)}`, destino, {
      headers: acesso ? { Authorization: `Bearer ${acesso}` } : {},
    });
  } catch (e) {
    throw new ErroRede((e as Error).message || 'falha ao baixar o áudio');
  }
  if (r.status < 200 || r.status >= 300) {
    await FileSystem.deleteAsync(destino, { idempotent: true }).catch(() => {});
    throw new ErroApi(r.status, `Erro ${r.status} ao baixar o áudio`);
  }
  return destino;
}
export const acaoChamado = (id: number, tipo: 'atendente' | 'resolvido') =>
  chamar<RespostaChamado>(`/atendimento/chamados/${id}/acao`, { metodo: 'POST', corpo: { tipo } });
