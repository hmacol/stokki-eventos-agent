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

type Opcoes = { metodo?: 'GET' | 'POST' | 'PUT'; corpo?: unknown; form?: FormData; semAuth?: boolean };

export async function chamar<T>(caminho: string, op: Opcoes = {}, repetiu = false): Promise<T> {
  const headers: Record<string, string> = {};
  if (!op.semAuth && acesso) headers.Authorization = `Bearer ${acesso}`;
  let body: BodyInit | undefined;
  if (op.form) {
    body = op.form;
  } else if (op.corpo !== undefined) {
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

import type { Ajuste, Checklist, Extrato, Motorista, Oferta, Pedagio, Rota } from './tipos';

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

function formComFoto(uri: string, campos: Record<string, string>): FormData {
  const form = new FormData();
  const nome = uri.split('/').pop() ?? 'foto.jpg';
  const ext = (nome.split('.').pop() ?? 'jpg').toLowerCase();
  // React Native aceita {uri, name, type} como arquivo em FormData
  form.append('arquivo', { uri, name: nome, type: ext === 'png' ? 'image/png' : 'image/jpeg' } as unknown as Blob);
  for (const [k, v] of Object.entries(campos)) form.append(k, v);
  return form;
}

export async function enviarComprovante(paradaId: number, uri: string, tipo: string, uuid: string, capturadoEm: string) {
  const form = formComFoto(uri, { tipo, uuid, capturado_em: capturadoEm });
  return chamar<{ id: number; ja_registrado: boolean; gcs: boolean }>(`/paradas/${paradaId}/comprovantes`, { metodo: 'POST', form });
}

/** Pedágio da rota: valor + foto do recibo (Hugo, 11/09). Fica pendente até o painel aprovar. */
export async function enviarPedagio(rotaId: number, uri: string, valor: number, uuid: string, capturadoEm: string) {
  const form = formComFoto(uri, { valor: String(valor), uuid, capturado_em: capturadoEm });
  return chamar<{ id: number; ja_registrado: boolean; gcs: boolean; pedagios: Pedagio[] }>(`/rotas/${rotaId}/pedagios`, { metodo: 'POST', form });
}
