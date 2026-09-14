// Fila offline: tudo que o motorista faz em campo (evento de parada,
// foto, GPS, iniciar/finalizar rota) entra aqui PRIMEIRO e é enviado
// depois. Cada item carrega um uuid gerado no aparelho -- a API ignora
// repetido, então reenviar nunca duplica.
//
// Persistida em AsyncStorage; processada em ordem (FIFO) com backoff
// simples; item que a API rejeita com erro de regra (4xx que não seja
// 401/408/429) é descartado e registrado em `descartados` pra o app
// mostrar ao motorista (ex.: "insucesso exige motivo").
import AsyncStorage from '@react-native-async-storage/async-storage';
import * as api from './api';
import { ErroApi, ErroRede } from './api';

const CHAVE = 'motorista.fila.v1';
const CHAVE_DESCARTADOS = 'motorista.fila.descartados.v1';
const CHAVE_ULTIMO_ERRO = 'motorista.fila.ultimoerro.v1';

// Itens que NÃO precisam manter a ordem: se falharem por rede/servidor
// vão pro fim da fila e os eventos de parada atrás deles seguem (11/09:
// uma foto de pedágio travada segurou todas as chegadas do teste do Hugo).
const TIPOS_REORDENAVEIS = new Set<ItemFila['tipo']>(['COMPROVANTE', 'PEDAGIO', 'GPS']);

export type UltimoErro = { em: string; tipo: ItemFila['tipo']; uuid: string; mensagem: string; status?: number };

export type ItemFila =
  | { uuid: string; tipo: 'EVENTO_PARADA'; paradaId: number; corpo: Record<string, unknown>; criadoEm: string; tentativas: number }
  | { uuid: string; tipo: 'COMPROVANTE'; paradaId: number; uri: string; tipoComprovante: string; capturadoEm: string; criadoEm: string; tentativas: number; nf?: string | null }
  | { uuid: string; tipo: 'PEDAGIO'; rotaId: number; uri: string; valor: number; capturadoEm: string; criadoEm: string; tentativas: number }
  | { uuid: string; tipo: 'GPS'; pontos: object[]; criadoEm: string; tentativas: number }
  | { uuid: string; tipo: 'ROTA'; rotaId: number; acao: 'aceitar' | 'iniciar' | 'finalizar'; corpo: Record<string, unknown>; criadoEm: string; tentativas: number };

let processando = false;
let ouvintes: ((tamanho: number) => void)[] = [];

export function novoUuid(): string {
  // UUID v4 simples (sem dependência): suficiente como chave de idempotência
  const hex = '0123456789abcdef';
  let s = '';
  for (let i = 0; i < 36; i++) {
    if (i === 8 || i === 13 || i === 18 || i === 23) s += '-';
    else if (i === 14) s += '4';
    else if (i === 19) s += hex[(Math.random() * 4) | 8];
    else s += hex[(Math.random() * 16) | 0];
  }
  return s;
}

export function agoraIso(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function aoMudar(fn: (tamanho: number) => void): () => void {
  ouvintes.push(fn);
  return () => {
    ouvintes = ouvintes.filter((f) => f !== fn);
  };
}

async function ler(): Promise<ItemFila[]> {
  try {
    const bruto = await AsyncStorage.getItem(CHAVE);
    return bruto ? (JSON.parse(bruto) as ItemFila[]) : [];
  } catch {
    return [];
  }
}

async function gravar(itens: ItemFila[]) {
  await AsyncStorage.setItem(CHAVE, JSON.stringify(itens));
  ouvintes.forEach((f) => f(itens.length));
}

export async function tamanho(): Promise<number> {
  return (await ler()).length;
}

export async function enfileirar(item: ItemFila): Promise<void> {
  const itens = await ler();
  itens.push(item);
  await gravar(itens);
  void processar();
}

export async function descartados(): Promise<{ uuid: string; motivo: string; item: ItemFila; em: string }[]> {
  try {
    const bruto = await AsyncStorage.getItem(CHAVE_DESCARTADOS);
    return bruto ? JSON.parse(bruto) : [];
  } catch {
    return [];
  }
}

async function registrarDescartado(item: ItemFila, motivo: string) {
  const lista = await descartados();
  lista.unshift({ uuid: item.uuid, motivo, item, em: agoraIso() });
  await AsyncStorage.setItem(CHAVE_DESCARTADOS, JSON.stringify(lista.slice(0, 50)));
}

/** Último erro de envio (pra tela de Perfil mostrar o que está travando). */
export async function ultimoErro(): Promise<UltimoErro | null> {
  try {
    const b = await AsyncStorage.getItem(CHAVE_ULTIMO_ERRO);
    return b ? (JSON.parse(b) as UltimoErro) : null;
  } catch {
    return null;
  }
}

async function registrarUltimoErro(item: ItemFila, e: unknown) {
  const erro: UltimoErro = {
    em: agoraIso(), tipo: item.tipo, uuid: item.uuid,
    mensagem: e instanceof ErroRede ? `Rede: ${e.message}` : e instanceof ErroApi ? `HTTP ${e.status}: ${e.message}` : e instanceof Error ? e.message : String(e),
    status: e instanceof ErroApi ? e.status : undefined,
  };
  try {
    await AsyncStorage.setItem(CHAVE_ULTIMO_ERRO, JSON.stringify(erro));
  } catch {
    // diagnóstico é best-effort
  }
}

/** Resumo dos itens na fila (Perfil). */
export async function listar(): Promise<{ uuid: string; tipo: ItemFila['tipo']; criadoEm: string; tentativas: number; detalhe: string }[]> {
  return (await ler()).map((i) => ({
    uuid: i.uuid, tipo: i.tipo, criadoEm: i.criadoEm, tentativas: i.tentativas ?? 0,
    detalhe: i.tipo === 'EVENTO_PARADA' ? `${String(i.corpo.tipo ?? '')} parada ${i.paradaId}`
      : i.tipo === 'COMPROVANTE' ? `${i.tipoComprovante}${i.nf ? ` NF ${i.nf}` : ''} parada ${i.paradaId}`
      : i.tipo === 'PEDAGIO' ? `R$ ${i.valor.toFixed(2).replace('.', ',')} rota ${i.rotaId}`
      : i.tipo === 'GPS' ? `${i.pontos.length} ponto(s)`
      : `${i.acao} rota ${i.rotaId}`,
  }));
}

/** Pedágios ainda não enviados (Financeiro mostra junto dos já enviados). */
export async function pedagiosNaFila(): Promise<{ uuid: string; rotaId: number; valor: number }[]> {
  return (await ler()).flatMap((i) => (i.tipo === 'PEDAGIO' ? [{ uuid: i.uuid, rotaId: i.rotaId, valor: i.valor }] : []));
}

/** Tira um item da fila na mão (motorista/teste decidiu desistir dele). */
export async function descartarItem(uuid: string): Promise<void> {
  const itens = await ler();
  const item = itens.find((i) => i.uuid === uuid);
  if (!item) return;
  await registrarDescartado(item, 'Descartado pelo usuário');
  await gravar(itens.filter((i) => i.uuid !== uuid));
}

async function enviar(item: ItemFila): Promise<void> {
  switch (item.tipo) {
    case 'EVENTO_PARADA':
      await api.eventoParada(item.paradaId, { ...item.corpo, uuid: item.uuid });
      return;
    case 'COMPROVANTE':
      await api.enviarComprovante(item.paradaId, item.uri, item.tipoComprovante, item.uuid, item.capturadoEm, item.nf);
      return;
    case 'PEDAGIO':
      await api.enviarPedagio(item.rotaId, item.uri, item.valor, item.uuid, item.capturadoEm);
      return;
    case 'GPS':
      await api.enviarGps(item.pontos);
      return;
    case 'ROTA':
      if (item.acao === 'aceitar') await api.aceitarRota(item.rotaId, { ...item.corpo, uuid: item.uuid });
      else if (item.acao === 'iniciar') await api.iniciarRota(item.rotaId, { ...item.corpo, uuid: item.uuid });
      else await api.finalizarRota(item.rotaId, { ...item.corpo, uuid: item.uuid });
      return;
  }
}

/** Tenta esvaziar a fila. Eventos de parada/rota mantêm a ordem (param no
 * 1º erro de rede); fotos/GPS que falham vão pro fim e o resto segue. */
export async function processar(): Promise<{ enviados: number; pendentes: number }> {
  if (processando || !api.temSessao()) return { enviados: 0, pendentes: await tamanho() };
  processando = true;
  let enviados = 0;
  try {
    let itens = await ler();
    let reordenados = 0;
    const limiteReordenacoes = itens.length; // cada item vai pro fim no máximo 1x por passada
    while (itens.length > 0) {
      const item = itens[0];
      try {
        await enviar(item);
        enviados++;
        itens = itens.slice(1);
        await gravar(itens);
      } catch (e) {
        const temporario = e instanceof ErroRede
          || (e instanceof ErroApi && (e.status === 401 || e.status === 408 || e.status === 429 || e.status >= 500));
        if (temporario) {
          await registrarUltimoErro(item, e);
          item.tentativas = (item.tentativas ?? 0) + 1;
          if (TIPOS_REORDENAVEIS.has(item.tipo) && reordenados < limiteReordenacoes && itens.length > 1) {
            reordenados++;
            itens = [...itens.slice(1), item];
            await gravar(itens);
            continue;
          }
          await gravar(itens);
          break;
        }
        // Erro de regra: não adianta insistir
        await registrarUltimoErro(item, e);
        await registrarDescartado(item, e instanceof Error ? e.message : String(e));
        itens = itens.slice(1);
        await gravar(itens);
      }
    }
    return { enviados, pendentes: itens.length };
  } finally {
    processando = false;
  }
}
