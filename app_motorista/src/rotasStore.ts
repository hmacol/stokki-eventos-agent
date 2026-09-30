// Carregamento das rotas do motorista com cache offline + mescla do
// estado local. Uma função só, usada pelas telas de lista e detalhe.
import * as api from './api';
import * as local from './local';
import { hoje, somarDias } from './tema';
import type { Rota } from './tipos';

export async function carregarRotas(): Promise<{ rotas: Rota[]; offline: boolean }> {
  try {
    const rotas = await api.rotas(somarDias(hoje(), -1), somarDias(hoje(), 1));
    await local.guardarCacheRotas(rotas);
    return { rotas: await local.mesclar(rotas), offline: false };
  } catch (e) {
    if (e instanceof api.ErroRede) {
      const cache = (await local.cacheRotas()) ?? [];
      return { rotas: await local.mesclar(cache), offline: true };
    }
    throw e;
  }
}
