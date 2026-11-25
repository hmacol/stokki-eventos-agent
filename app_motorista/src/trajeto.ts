// Trajeto da rota pro Mapa da rota. A linha pelas ruas vem do servidor
// (Google, calculado uma vez por rota); o app guarda a ultima resposta do
// Google de cada rota pra desenhar sem internet. Sem nada guardado, a tela
// liga os pinos em linha reta -- o mapa nunca mostra tela de erro por isso.
import AsyncStorage from '@react-native-async-storage/async-storage';
import * as api from './api';
import type { Trajeto } from './tipos';

const PREFIXO = 'motorista.trajeto.v1.';
// So as rotas mais recentes: o AsyncStorage do Android tem teto (~6 MB) e
// divide espaco com o cache das rotas e a fila offline.
const GUARDAR_NO_MAXIMO = 5;

/** CD da Fresh Log (regras/km_cobrado.COORD_CD): base enquanto o trajeto nao chega. */
export const BASE_CD: [number, number] = [-23.497039, -46.6605211];

async function guardar(rotaId: number, t: Trajeto) {
  try {
    await AsyncStorage.setItem(`${PREFIXO}${rotaId}`, JSON.stringify(t));
    const ids = (await AsyncStorage.getAllKeys())
      .filter((k) => k.startsWith(PREFIXO))
      .map((k) => Number(k.slice(PREFIXO.length)))
      .sort((a, b) => b - a);
    const velhas = ids.slice(GUARDAR_NO_MAXIMO).map((id) => `${PREFIXO}${id}`);
    if (velhas.length) await AsyncStorage.multiRemove(velhas);
  } catch {
    // cache e conveniencia: falhar aqui nao tira a linha da tela
  }
}

async function guardado(rotaId: number): Promise<Trajeto | null> {
  try {
    const b = await AsyncStorage.getItem(`${PREFIXO}${rotaId}`);
    return b ? (JSON.parse(b) as Trajeto) : null;
  } catch {
    return null;
  }
}

/** Trajeto da API; sem rede (ou erro do servidor) devolve o guardado ou null. Nunca lanca. */
export async function carregarTrajeto(rotaId: number): Promise<Trajeto | null> {
  try {
    const t = await api.trajeto(rotaId);
    if (t.fonte === 'GOOGLE_ROUTES') await guardar(rotaId, t);
    return t;
  } catch {
    return guardado(rotaId);
  }
}
