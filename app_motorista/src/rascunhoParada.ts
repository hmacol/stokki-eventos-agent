// Rascunho do checklist da parada (17/11): o Android pode fechar o app
// enquanto a câmera está aberta (pouca memória). Sem isto o motorista
// voltava pra lista da rota com tudo em branco e a foto perdida. A tela
// da parada grava aqui o que já foi preenchido; ao reabrir, o app volta
// pra parada e recupera a foto (ImagePicker.getPendingResultAsync).
import AsyncStorage from '@react-native-async-storage/async-storage';

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

export async function limpar(): Promise<void> {
  await AsyncStorage.removeItem(CHAVE);
}
