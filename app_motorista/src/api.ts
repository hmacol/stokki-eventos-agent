// Cliente HTTP da API do motorista (nucleo/api_motorista.py).
//
// - Base configurável em app.json -> extra.apiUrl (produção:
//   https://app.freshhub.com.br/motorista/api). Em desenvolvimento aponte
//   pro IP da máquina rodando `py -3.11 nucleo/api_motorista.py --porta 8073`.
// - Token de acesso no header; 401 dispara UMA renovação via refresh e
//   repete a chamada. Falhou a renovação -> sessão cai (ouvintes avisados).
// - Sem rede/timeout -> ErroRede (a fila offline usa isso pra decidir
//   se guarda pra depois).
import Constants from 'expo-constants';
import * as FileSystem from 'expo-file-system/legacy';
import * as SecureStore from 'expo-secure-store';

export const API_URL: string =
  (Constants.expoConfig?.extra as { apiUrl?: string } | undefined)?.apiUrl ?? 'http://10.0.2.2:8073/api';

const CHAVE_ACESSO = 'motorista.acesso';
const CHAVE_REFRESH = 'motorista.refresh';
const TIMEOUT_MS = 20000;

export class ErroApi extends Error {
  status: number;
  constructor(status: number, mensagem: string) {
    super(mensagem);
    this.status = status;
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
  acesso = await SecureStore.getItemAsync(CHAVE_ACESSO);
  refresh = await SecureStore.getItemAsync(CHAVE_REFRESH);
  return !!acesso;
}

export async function guardarTokens(t: { acesso: string; refresh: string }) {
  acesso = t.acesso;
  refresh = t.refresh;
  await SecureStore.setItemAsync(CHAVE_ACESSO, t.acesso);
  await SecureStore.setItemAsync(CHAVE_REFRESH, t.refresh);
}

export async function limparTokens() {
  acesso = null;
  refresh = null;
  await SecureStore.deleteItemAsync(CHAVE_ACESSO);
  await SecureStore.deleteItemAsync(CHAVE_REFRESH);
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
  const headers: Record<string, string> = {};
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
    throw new ErroApi(r.status, msg);
  }
  return dados as T;
}

// ── chamadas de alto nível ────────────────────────────────────────────────

import type { Ajuste, Checklist, Extrato, Motorista, Oferta, Pedagio, ResultadoValidacao, Rota } from './tipos';

export async function login(cpf: string, pin: string): Promise<Motorista> {
  const r = await chamar<{ acesso: string; refresh: string; motorista: Motorista }>('/login', {
    metodo: 'POST',
    corpo: { cpf, pin },
    semAuth: true,
  });
  await guardarTokens(r);
  return r.motorista;
}

export const eu = () => chamar<Motorista>('/eu');
export const rotas = (de?: string, ate?: string) =>
  chamar<{ rotas: Rota[] }>(`/rotas${de ? `?de=${de}&ate=${ate ?? de}` : ''}`).then((r) => r.rotas);
export const rota = (id: number) => chamar<Rota>(`/rotas/${id}`);
export const aceitarRota = (id: number, corpo: object) => chamar<Rota>(`/rotas/${id}/aceitar`, { metodo: 'POST', corpo });
export const recusarRota = (id: number, motivo: string) => chamar<Rota>(`/rotas/${id}/recusar`, { metodo: 'POST', corpo: { motivo } });
export const iniciarRota = (id: number, corpo: object) => chamar<Rota>(`/rotas/${id}/iniciar`, { metodo: 'POST', corpo });
export const finalizarRota = (id: number, corpo: object) => chamar<Rota>(`/rotas/${id}/finalizar`, { metodo: 'POST', corpo });
export const eventoParada = (paradaId: number, corpo: object) =>
  chamar<{ ja_registrado: boolean; rota_status: string }>(`/paradas/${paradaId}/eventos`, { metodo: 'POST', corpo });
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
async function enviarArquivo<T>(caminho: string, uri: string, campos: Record<string, string>, repetiu = false): Promise<T> {
  const info = await FileSystem.getInfoAsync(uri).catch(() => ({ exists: false }));
  if (!info.exists) {
    // Foto sumiu do cache do aparelho: não adianta insistir (vira "recusado" na fila)
    throw new ErroApi(410, 'A foto não está mais no aparelho -- registre de novo.');
  }
  const nome = uri.split('/').pop() ?? 'foto.jpg';
  const ext = (nome.split('.').pop() ?? 'jpg').toLowerCase();
  let r: FileSystem.FileSystemUploadResult;
  try {
    r = await FileSystem.uploadAsync(`${API_URL}${caminho}`, uri, {
      httpMethod: 'POST',
      uploadType: FileSystem.FileSystemUploadType.MULTIPART,
      fieldName: 'arquivo',
      mimeType: ext === 'png' ? 'image/png' : 'image/jpeg',
      parameters: campos,
      headers: acesso ? { Authorization: `Bearer ${acesso}` } : {},
    });
  } catch (e) {
    throw new ErroRede((e as Error).message || 'falha no envio da foto');
  }
  if (r.status === 401 && !repetiu) {
    if (await tentarRenovar()) return enviarArquivo<T>(caminho, uri, campos, true);
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
    throw new ErroApi(r.status, msg);
  }
  return dados as T;
}

export async function enviarComprovante(paradaId: number, uri: string, tipo: string, uuid: string, capturadoEm: string, nf?: string | null) {
  return enviarArquivo<{ id: number; ja_registrado: boolean; gcs: boolean }>(`/paradas/${paradaId}/comprovantes`, uri, {
    tipo, uuid, capturado_em: capturadoEm, ...(nf ? { nf } : {}),
  });
}

/** Confere a foto ANTES de enviar, com o motorista ainda no cliente
 * (Hugo, 12/09): nitidez + legibilidade + número da NF no canhoto.
 * A foto NÃO é gravada aqui -- o envio de verdade segue pela fila. Com a
 * validação desligada no servidor volta NAO_VERIFICADO/pode_seguir. */
export async function validarFoto(
  uri: string,
  campos: { tipo: 'CANHOTO' | 'PEDAGIO'; paradaId?: number; rotaId?: number; nf?: string | null; valor?: number; tentativa: number },
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

/** Pedágio da rota: valor + foto do recibo (Hugo, 11/09). Fica pendente até o painel aprovar. */
export async function enviarPedagio(rotaId: number, uri: string, valor: number, uuid: string, capturadoEm: string) {
  return enviarArquivo<{ id: number; ja_registrado: boolean; gcs: boolean; pedagios: Pedagio[] }>(`/rotas/${rotaId}/pedagios`, uri, { valor: String(valor), uuid, capturado_em: capturadoEm });
}
