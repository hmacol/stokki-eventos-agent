// Rascunho do checklist da parada (17/11): o Android pode fechar o app
// enquanto a câmera está aberta (pouca memória). Sem isto o motorista
// voltava pra lista da rota com tudo em branco e a foto perdida. A tela
// da parada grava aqui o que já foi preenchido; ao reabrir, o app volta
// pra parada e recupera a foto (ImagePicker.getPendingResultAsync).
import AsyncStorage from '@react-native-async-storage/async-storage';
import * as FileSystem from 'expo-file-system/legacy';

const CHAVE = 'motorista.rascunho_parada.v1';
const VALIDADE_MS = 12 * 60 * 60 * 1000;

export type RascunhoParada = {
  paradaId: number;
  rotaId: number;
  fluxo: string | null;
  respostas: Record<string, string>;
  fotos: Record<string, string[]>;
  motivoId: number | null;
  /** Campo que estava com a câmera aberta quando o rascunho foi salvo. */
  cameraAberta: { chave: string; substituir: boolean } | null;
  salvoEm: number;
};

export async function salvar(r: Omit<RascunhoParada, 'salvoEm'>): Promise<void> {
  await AsyncStorage.setItem(CHAVE, JSON.stringify({ ...r, salvoEm: Date.now() }));
}

export async function ler(): Promise<RascunhoParada | null> {
  try {
    const bruto = await AsyncStorage.getItem(CHAVE);
    if (!bruto) return null;
    const r = JSON.parse(bruto) as RascunhoParada;
    return Date.now() - r.salvoEm < VALIDADE_MS ? r : null;
  } catch {
    return null;
  }
}

/** Foto que a câmera gravou depois de `desdeMs` (17/11, Renato): quando o
 * Android mata o app com a câmera aberta, o getPendingResultAsync volta
 * vazio, mas o arquivo que a câmera preencheu continua na pasta de cache
 * do ImagePicker. Devolve o mais novo com conteúdo, ou null. */
export async function fotoDaCamera(desdeMs: number): Promise<string | null> {
  try {
    const pasta = `${FileSystem.cacheDirectory}ImagePicker/`;
    let melhor: { uri: string; em: number } | null = null;
    for (const nome of await FileSystem.readDirectoryAsync(pasta)) {
      const info = await FileSystem.getInfoAsync(pasta + nome);
      if (!info.exists || info.isDirectory || info.size === 0) continue;
      const em = info.modificationTime * 1000;
      if (em >= desdeMs - 5000 && (!melhor || em > melhor.em)) melhor = { uri: info.uri, em };
    }
    return melhor?.uri ?? null;
  } catch {
    return null;
  }
}

export async function limpar(): Promise<void> {
  await AsyncStorage.removeItem(CHAVE);
}
