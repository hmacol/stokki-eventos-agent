// Pinos do Mapa da rota: circulo com o numero da parada (ordem planejada)
// e o pino da base (CD). Marker com view propria no Android vira imagem:
// com tracksViewChanges ligado ele redesenha o tempo todo (pisca e gasta
// CPU), entao fica ligado so um instante depois de cada mudanca visual.
import React, { useEffect, useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { Marker } from 'react-native-maps';
import { Ionicons } from '@expo/vector-icons';
import { cores } from '../tema';
import { ehPendente, emAndamento } from '../paradaPasso';
import type { Parada } from '../tipos';

type Coordenada = { latitude: number; longitude: number };

/** Liga o redesenho do marcador por um instante sempre que a aparencia muda. */
function useRedesenho(aparencia: string): boolean {
  const [ligado, setLigado] = useState(true);
  useEffect(() => {
    setLigado(true);
    const t = setTimeout(() => setLigado(false), 600);
    return () => clearTimeout(t);
  }, [aparencia]);
  return ligado;
}

function corDoPino(p: Parada): string {
  if (p.situacao === 'ENTREGUE' || p.situacao === 'PARCIAL') return cores.sucesso;
  if (p.situacao === 'INSUCESSO') return cores.perigo;
  return cores.primaria;
}

const SELO_ALTURA = 16;

export function PinoParada({ parada, coordenada, selecionado, onPress }: {
  parada: Parada;
  coordenada: Coordenada;
  selecionado: boolean;
  onPress: () => void;
}) {
  const cor = corDoPino(parada);
  const andamento = emAndamento(parada);
  const tamanho = andamento ? 40 : 30;
  // Reagendada ainda por fazer: o numero segue a ordem planejada, o selo avisa.
  const volta = !!parada.reagendado_para && ehPendente(parada);
  const redesenhar = useRedesenho(`${parada.situacao}|${selecionado}|${volta}`);
  return (
    <Marker
      coordinate={coordenada}
      onPress={onPress}
      anchor={{ x: 0.5, y: 0.5 }}
      tracksViewChanges={redesenhar}
      zIndex={selecionado ? 3 : andamento ? 2 : 1}
    >
      <View style={s.coluna}>
        {/* espaco igual ao selo em cima: o centro do circulo fica no centro da view (anchor 0.5) */}
        {volta ? <View style={{ height: SELO_ALTURA }} /> : null}
        <View
          style={[
            s.circulo,
            { width: tamanho, height: tamanho, borderRadius: tamanho / 2, backgroundColor: cor },
            andamento && { borderColor: cores.acentoLogo, borderWidth: 3 },
            selecionado && { borderColor: cores.alerta, borderWidth: 4 },
          ]}
        >
          <Text style={[s.numero, andamento && { fontSize: 16 }]}>{parada.ordem}</Text>
        </View>
        {volta ? <Text style={s.selo}>volta depois</Text> : null}
      </View>
    </Marker>
  );
}

export function PinoBase({ coordenada }: { coordenada: Coordenada }) {
  const redesenhar = useRedesenho('base');
  return (
    <Marker coordinate={coordenada} anchor={{ x: 0.5, y: 0.5 }} tracksViewChanges={redesenhar} zIndex={0}>
      <View style={s.base}>
        <Ionicons name="business" size={18} color="#fff" />
      </View>
    </Marker>
  );
}

const s = StyleSheet.create({
  coluna: { alignItems: 'center', padding: 2 },
  circulo: { alignItems: 'center', justifyContent: 'center', borderWidth: 2, borderColor: '#fff' },
  numero: { color: '#fff', fontWeight: '800', fontSize: 13 },
  selo: {
    height: SELO_ALTURA, paddingHorizontal: 5, borderRadius: 8, overflow: 'hidden',
    backgroundColor: cores.alerta, color: '#fff', fontSize: 10, fontWeight: '800', lineHeight: SELO_ALTURA,
  },
  base: {
    width: 34, height: 34, borderRadius: 8, backgroundColor: cores.primariaEscura, borderWidth: 2, borderColor: '#fff',
    alignItems: 'center', justifyContent: 'center',
  },
});
