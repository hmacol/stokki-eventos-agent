// Passos da parada, compartilhados entre a tela da rota (app/rota/[id].tsx)
// e o mapa da rota: situacao da parada, navegacao externa, ordem de exibicao,
// "parada da vez" e o registro do passo (deslocamento / chegada) com GPS,
// estado local e fila. Movido da tela da rota sem mudar comportamento.
import { Alert, Linking } from 'react-native';
import * as fila from './fila';
import * as gps from './gps';
import * as local from './local';
import type { Parada, Rota, SituacaoParada } from './tipos';

const FINAIS: SituacaoParada[] = ['ENTREGUE', 'PARCIAL', 'INSUCESSO', 'CANCELADA'];

export const ehPendente = (p: Parada) => !FINAIS.includes(p.situacao);
export const emAndamento = (p: Parada) => p.situacao === 'EM_DESLOCAMENTO' || p.situacao === 'EM_ROTA';

export function abrirNavegacao(p: Parada) {
  const destino = p.latitude !== null && p.longitude !== null ? `${p.latitude},${p.longitude}` : encodeURIComponent(p.endereco ?? '');
  Alert.alert('Navegar até a parada', undefined, [
    { text: 'Google Maps', onPress: () => void Linking.openURL(`https://www.google.com/maps/dir/?api=1&destination=${destino}&travelmode=driving`) },
    { text: 'Waze', onPress: () => void Linking.openURL(p.latitude !== null ? `https://waze.com/ul?ll=${destino}&navigate=yes` : `https://waze.com/ul?q=${destino}&navigate=yes`) },
    { text: 'Cancelar', style: 'cancel' },
  ]);
}

// Ordem de exibição: sequência da rota, com as reagendadas "depois das
// outras" no fim e as reagendadas com horário no fim ordenadas por horário.
export function ordenarParadas(paradas: Parada[]): Parada[] {
  return [...paradas].sort((a, b) => {
    const ka = a.reagendado_para && ehPendente(a) ? (a.reagendado_para === 'FIM' ? 2 : 1) : 0;
    const kb = b.reagendado_para && ehPendente(b) ? (b.reagendado_para === 'FIM' ? 2 : 1) : 0;
    if (ka !== kb) return ka - kb;
    if (ka === 1) return (a.reagendado_para ?? '').localeCompare(b.reagendado_para ?? '');
    return a.ordem - b.ordem;
  });
}

export const rotaOperavel = (rota: Rota) => rota.editavel && rota.status === 'EM_ROTA';

// A parada da vez: a que está em andamento ou, se nenhuma estiver, a
// primeira pendente da ordem. Só faz sentido com a rota em andamento --
// rota por aceitar ou já concluída não tem parada da vez.
export function paradaDaVez(rota: Rota): Parada | null {
  const ativa = rota.paradas.find(emAndamento) ?? null;
  return rotaOperavel(rota) ? ativa ?? ordenarParadas(rota.paradas).find(ehPendente) ?? null : null;
}

export async function registrarPasso(p: Parada, tipo: 'DESLOCAMENTO' | 'CHEGADA'): Promise<void> {
  const pos = await gps.posicaoAtual();
  await local.marcarParada(p.id, tipo === 'DESLOCAMENTO' ? 'EM_DESLOCAMENTO' : 'EM_ROTA');
  await fila.enfileirar({
    uuid: fila.novoUuid(), tipo: 'EVENTO_PARADA', paradaId: p.id, criadoEm: fila.agoraIso(), tentativas: 0,
    corpo: { tipo, ocorrido_em: fila.agoraIso(), ...pos },
  });
}
