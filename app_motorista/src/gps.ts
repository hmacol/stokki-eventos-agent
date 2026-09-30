// Rastreamento em rota (só com o app em primeiro plano no v1 -- ver
// DOC_EXECUCAO seção 3.4). Pontos vão pra fila offline em lotes; a API
// soma o km real (nucleo/operacao.calcular_km_gps) e ele entra no
// financeiro no lugar do km estimado.
import * as Location from 'expo-location';
import * as fila from './fila';

let assinatura: Location.LocationSubscription | null = null;
let rotaAtual: number | null = null;
let buffer: object[] = [];
let timer: ReturnType<typeof setInterval> | null = null;

async function despejar() {
  if (buffer.length === 0) return;
  const pontos = buffer;
  buffer = [];
  await fila.enfileirar({ uuid: fila.novoUuid(), tipo: 'GPS', pontos, criadoEm: fila.agoraIso(), tentativas: 0 });
}

export async function posicaoAtual(): Promise<{ latitude: number; longitude: number; precisao_m: number | null } | null> {
  try {
    const { status } = await Location.getForegroundPermissionsAsync();
    if (status !== 'granted') return null;
    const p = await Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.High });
    return { latitude: p.coords.latitude, longitude: p.coords.longitude, precisao_m: p.coords.accuracy ?? null };
  } catch {
    return null;
  }
}

export async function iniciar(rotaId: number): Promise<boolean> {
  if (rotaAtual === rotaId && assinatura) return true;
  await parar();
  const { status } = await Location.requestForegroundPermissionsAsync();
  if (status !== 'granted') return false;
  rotaAtual = rotaId;
  assinatura = await Location.watchPositionAsync(
    { accuracy: Location.Accuracy.High, distanceInterval: 40, timeInterval: 20000 },
    (p) => {
      buffer.push({
        uuid: fila.novoUuid(), rota_id: rotaId, ocorrido_em: fila.agoraIso(),
        latitude: p.coords.latitude, longitude: p.coords.longitude, precisao_m: p.coords.accuracy ?? null,
      });
      if (buffer.length >= 15) void despejar();
    },
  );
  timer = setInterval(() => void despejar(), 90000);
  return true;
}

export async function parar() {
  if (assinatura) assinatura.remove();
  assinatura = null;
  rotaAtual = null;
  if (timer) clearInterval(timer);
  timer = null;
  await despejar();
}

export function rotaRastreada(): number | null {
  return rotaAtual;
}
