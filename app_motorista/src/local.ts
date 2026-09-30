// Estado local "otimista": quando o motorista registra algo sem rede, a
// tela precisa refletir na hora (parada entregue, rota iniciada) mesmo
// antes da fila conseguir enviar. Guarda só o mínimo, por id, e a tela
// mescla por cima do que veio da API.
import AsyncStorage from '@react-native-async-storage/async-storage';
import type { Rota, SituacaoParada, StatusRota } from './tipos';

const CHAVE = 'motorista.local.v1';

type Estado = {
  paradas: Record<string, { situacao: SituacaoParada; em: string }>;
  rotas: Record<string, { status: StatusRota; em: string }>;
  cacheRotas?: Rota[];
  cacheEm?: string;
};

async function ler(): Promise<Estado> {
  try {
    const b = await AsyncStorage.getItem(CHAVE);
    return b ? (JSON.parse(b) as Estado) : { paradas: {}, rotas: {} };
  } catch {
    return { paradas: {}, rotas: {} };
  }
}

async function gravar(e: Estado) {
  await AsyncStorage.setItem(CHAVE, JSON.stringify(e));
}

export async function marcarParada(paradaId: number, situacao: SituacaoParada) {
  const e = await ler();
  e.paradas[String(paradaId)] = { situacao, em: new Date().toISOString() };
  await gravar(e);
}

export async function marcarRota(rotaId: number, status: StatusRota) {
  const e = await ler();
  e.rotas[String(rotaId)] = { status, em: new Date().toISOString() };
  await gravar(e);
}

export async function guardarCacheRotas(rotas: Rota[]) {
  const e = await ler();
  e.cacheRotas = rotas;
  e.cacheEm = new Date().toISOString();
  await gravar(e);
}

/** Mescla o que a API devolveu com o que está pendente localmente. A API
 * "vence" quando já reflete a mesma situação (aí o override é limpo). */
export async function mesclar(rotas: Rota[]): Promise<Rota[]> {
  const e = await ler();
  let mudou = false;
  const resultado = rotas.map((r) => {
    const or = e.rotas[String(r.id)];
    let status = r.status;
    if (or) {
      if (or.status === r.status || r.status === 'CONCLUIDA' || r.status === 'CANCELADA') {
        delete e.rotas[String(r.id)];
        mudou = true;
      } else status = or.status;
    }
    const paradas = r.paradas.map((p) => {
      const op = e.paradas[String(p.id)];
      if (!op) return p;
      if (op.situacao === p.situacao || p.situacao === 'CANCELADA') {
        delete e.paradas[String(p.id)];
        mudou = true;
        return p;
      }
      return { ...p, situacao: op.situacao };
    });
    const entregues = paradas.filter((p) => p.situacao === 'ENTREGUE' || p.situacao === 'PARCIAL').length;
    const insucessos = paradas.filter((p) => p.situacao === 'INSUCESSO').length;
    return { ...r, status, paradas, entregues, insucessos };
  });
  if (mudou) await gravar(e);
  return resultado;
}

export async function cacheRotas(): Promise<Rota[] | null> {
  const e = await ler();
  return e.cacheRotas ?? null;
}
