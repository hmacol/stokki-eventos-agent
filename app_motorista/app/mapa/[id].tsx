// Mapa da rota (Hugo, 18/11 -- como o "Mapa da Rota" da Vuupt): todas as
// paradas numeradas no mapa, a linha do trajeto pelas ruas e um cartao
// embaixo com a parada e os mesmos passos da lista da tela da rota.
// Spec: docs/superpowers/specs/2026-11-18-mapa-da-rota-app-motorista-design.md
//
// Sem internet: rota do cache (carregarRotas) e trajeto guardado; sem
// trajeto guardado, linha reta base -> paradas (ordem) -> base. Nada aqui
// mostra tela de erro por falta de rede ou de coordenada.
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Alert, Pressable, StyleSheet, Text, View } from 'react-native';
import { useFocusEffect, useLocalSearchParams, useRouter } from 'expo-router';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { Ionicons } from '@expo/vector-icons';
import MapView, { Polyline, PROVIDER_GOOGLE } from 'react-native-maps';
import * as gps from '../../src/gps';
import { carregarRotas } from '../../src/rotasStore';
import { BASE_CD, carregarTrajeto } from '../../src/trajeto';
import { ordenarParadas, paradaDaVez, registrarPasso } from '../../src/paradaPasso';
import { CartaoParada, temCoordenada } from '../../src/mapa/CartaoParada';
import { PinoBase, PinoParada } from '../../src/mapa/PinoParada';
import { Carregando } from '../../src/componentes';
import { cores } from '../../src/tema';
import type { Parada, Rota, Trajeto } from '../../src/tipos';

type Coordenada = { latitude: number; longitude: number };

const ZOOM_PARADA = 0.012;   // ~1 km de altura ao centralizar um pino
const MARGEM_ENQUADRAR = { top: 70, right: 50, bottom: 50, left: 50 };

const coordDe = (p: Parada): Coordenada => ({ latitude: p.latitude as number, longitude: p.longitude as number });

export default function MapaRota() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const rotaId = Number(id);
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const mapa = useRef<MapView>(null);
  const [rota, setRota] = useState<Rota | null>(null);
  const [offline, setOffline] = useState(false);
  const [trajeto, setTrajeto] = useState<Trajeto | null>(null);
  const [selecionada, setSelecionada] = useState<number | null>(null);   // id da parada do cartao
  const selecionadaRef = useRef<number | null>(null);
  const [mapaPronto, setMapaPronto] = useState(false);
  const enquadrou = useRef(false);

  const centralizar = useCallback((p: Parada | undefined) => {
    if (p && temCoordenada(p)) mapa.current?.animateToRegion({ ...coordDe(p), latitudeDelta: ZOOM_PARADA, longitudeDelta: ZOOM_PARADA }, 400);
  }, []);

  const selecionar = useCallback((p: Parada | undefined, centrar: boolean) => {
    if (!p) return;
    selecionadaRef.current = p.id;
    setSelecionada(p.id);
    if (centrar) centralizar(p);
  }, [centralizar]);

  // Recarrega ao abrir e ao voltar da tela da parada: o cartao vai pra
  // parada da vez (a em andamento ou a 1a pendente). Sem parada da vez
  // (rota por aceitar ou concluida) fica onde estava, ou na 1a.
  const carregar = useCallback(async () => {
    try {
      const r = await carregarRotas();
      const achada = r.rotas.find((x) => x.id === rotaId) ?? null;
      setOffline(r.offline);
      setRota(achada);
      if (!achada) return;
      const cartoes = ordenarParadas(achada.paradas).filter((p) => p.situacao !== 'CANCELADA');
      const vez = paradaDaVez(achada);
      const atual = cartoes.find((p) => p.id === selecionadaRef.current);
      const alvo = vez ?? atual ?? cartoes[0];
      // Na 1a carga quem posiciona o mapa e o enquadramento da rota inteira.
      selecionar(alvo, enquadrou.current && alvo?.id !== selecionadaRef.current);
    } catch (e) {
      Alert.alert('Erro', (e as Error).message);
    }
  }, [rotaId, selecionar]);
  useFocusEffect(useCallback(() => { void carregar(); }, [carregar]));

  useEffect(() => {
    let vivo = true;
    void carregarTrajeto(rotaId).then((t) => { if (vivo) setTrajeto(t); });
    return () => { vivo = false; };
  }, [rotaId]);

  // Cartoes na ordem da lista (reagendadas no fim); cancelada fica de fora do mapa.
  const cartoes = useMemo(() => (rota ? ordenarParadas(rota.paradas).filter((p) => p.situacao !== 'CANCELADA') : []), [rota]);
  const comPino = useMemo(() => cartoes.filter(temCoordenada), [cartoes]);
  const base: Coordenada = useMemo(() => {
    const [latitude, longitude] = trajeto?.base ?? BASE_CD;
    return { latitude, longitude };
  }, [trajeto]);

  // Linha pelas ruas (servidor/guardada) ou, sem ela, reta na ordem planejada.
  const linha = useMemo<Coordenada[]>(() => {
    if (trajeto && trajeto.pontos.length >= 2) return trajeto.pontos.map(([latitude, longitude]) => ({ latitude, longitude }));
    const porOrdem = [...comPino].sort((a, b) => a.ordem - b.ordem).map(coordDe);
    return porOrdem.length ? [base, ...porOrdem, base] : [];
  }, [trajeto, comPino, base]);

  const enquadrar = useCallback(() => {
    const pontos = [base, ...comPino.map(coordDe)];
    if (pontos.length === 1) {
      mapa.current?.animateToRegion({ ...base, latitudeDelta: 0.05, longitudeDelta: 0.05 }, 400);
      return;
    }
    mapa.current?.fitToCoordinates(pontos, { edgePadding: MARGEM_ENQUADRAR, animated: true });
  }, [base, comPino]);

  useEffect(() => {
    if (mapaPronto && rota && !enquadrou.current) {
      enquadrou.current = true;
      enquadrar();
    }
  }, [mapaPronto, rota, enquadrar]);

  const minhaLocalizacao = async () => {
    const pos = await gps.posicaoAtual();
    if (!pos) return Alert.alert('Localização', 'Não deu pra pegar sua localização. Confira se o GPS está ligado e se o app tem permissão.');
    mapa.current?.animateToRegion({ latitude: pos.latitude, longitude: pos.longitude, latitudeDelta: ZOOM_PARADA, longitudeDelta: ZOOM_PARADA }, 400);
  };

  if (!rota) return <Carregando />;

  const indice = Math.max(0, cartoes.findIndex((p) => p.id === selecionada));
  const parada = cartoes[indice];

  const passo = async (p: Parada, tipo: 'DESLOCAMENTO' | 'CHEGADA') => {
    await registrarPasso(p, tipo);
    await carregar();
  };
  const finalizar = (p: Parada) => {
    router.push(`/parada/${p.id}?rota=${rota.id}`);
  };

  return (
    <View style={s.tela}>
      <View style={s.cabecalho}>
        <View style={{ flex: 1 }}>
          <Text style={s.posicao}>{cartoes.length ? `${indice + 1} de ${cartoes.length}` : '0 paradas'}</Text>
          <Text style={s.nome} numberOfLines={1}>{rota.nome ?? `Rota #${rota.id}`}</Text>
        </View>
        <Pressable style={s.botaoMapa} onPress={() => void minhaLocalizacao()} accessibilityRole="button" accessibilityLabel="Minha localização">
          <Ionicons name="locate" size={22} color={cores.primaria} />
        </Pressable>
        <Pressable style={s.botaoMapa} onPress={enquadrar} accessibilityRole="button" accessibilityLabel="Ver rota inteira">
          <Ionicons name="expand" size={22} color={cores.primaria} />
        </Pressable>
      </View>
      {offline ? <Text style={s.aviso}>Sem conexão — mostrando a última versão salva.</Text> : null}

      <MapView
        ref={mapa}
        style={s.mapa}
        provider={PROVIDER_GOOGLE}
        showsUserLocation
        showsMyLocationButton={false}
        toolbarEnabled={false}
        initialRegion={{ latitude: BASE_CD[0], longitude: BASE_CD[1], latitudeDelta: 0.3, longitudeDelta: 0.3 }}
        onMapReady={() => setMapaPronto(true)}
      >
        {linha.length >= 2 ? <Polyline coordinates={linha} strokeColor={cores.info} strokeWidth={4} /> : null}
        <PinoBase coordenada={base} />
        {comPino.map((p) => (
          <PinoParada key={p.id} parada={p} coordenada={coordDe(p)} selecionado={p.id === parada?.id} onPress={() => selecionar(p, true)} />
        ))}
      </MapView>

      <View style={{ paddingBottom: insets.bottom, backgroundColor: cores.cartao }}>
        {parada ? (
          <CartaoParada
            parada={parada}
            rota={rota}
            total={cartoes.length}
            indice={indice}
            onAnterior={() => selecionar(cartoes[indice - 1], true)}
            onProxima={() => selecionar(cartoes[indice + 1], true)}
            onPasso={passo}
            onFinalizar={finalizar}
          />
        ) : (
          <Text style={s.vazio}>Esta rota não tem paradas.</Text>
        )}
      </View>
    </View>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.fundo },
  cabecalho: { flexDirection: 'row', alignItems: 'center', gap: 8, paddingHorizontal: 12, paddingVertical: 6, backgroundColor: cores.cartao, borderBottomWidth: StyleSheet.hairlineWidth, borderColor: cores.borda },
  posicao: { fontSize: 17, fontWeight: '800', color: cores.texto },
  nome: { fontSize: 12.5, color: cores.textoSuave },
  botaoMapa: { width: 44, height: 44, borderRadius: 12, backgroundColor: cores.fundo, alignItems: 'center', justifyContent: 'center' },
  aviso: { backgroundColor: cores.alerta, color: '#fff', padding: 6, textAlign: 'center', fontWeight: '600' },
  mapa: { flex: 1 },
  vazio: { color: cores.textoSuave, textAlign: 'center', padding: 20 },
});
