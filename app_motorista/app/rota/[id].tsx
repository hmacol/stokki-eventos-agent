import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Linking, Pressable, RefreshControl, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { useFocusEffect, useLocalSearchParams, useRouter } from 'expo-router';
import * as api from '../../src/api';
import * as fila from '../../src/fila';
import * as gps from '../../src/gps';
import * as local from '../../src/local';
import { carregarRotas } from '../../src/rotasStore';
import { Botao, Cartao, Carregando, Etiqueta } from '../../src/componentes';
import { cores, formatarData, situacaoCor, situacaoRotulo, statusRotaRotulo } from '../../src/tema';
import type { Parada, Rota } from '../../src/tipos';

function abrirNavegacao(p: Parada) {
  const destino = p.latitude !== null && p.longitude !== null ? `${p.latitude},${p.longitude}` : encodeURIComponent(p.endereco ?? '');
  Alert.alert('Navegar até a parada', undefined, [
    { text: 'Google Maps', onPress: () => void Linking.openURL(`https://www.google.com/maps/dir/?api=1&destination=${destino}&travelmode=driving`) },
    { text: 'Waze', onPress: () => void Linking.openURL(p.latitude !== null ? `https://waze.com/ul?ll=${destino}&navigate=yes` : `https://waze.com/ul?q=${destino}&navigate=yes`) },
    { text: 'Cancelar', style: 'cancel' },
  ]);
}

export default function DetalheRota() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const rotaId = Number(id);
  const router = useRouter();
  const [rota, setRota] = useState<Rota | null>(null);
  const [offline, setOffline] = useState(false);
  const [ocupado, setOcupado] = useState(false);
  const [motivoRecusa, setMotivoRecusa] = useState('');
  const [recusando, setRecusando] = useState(false);

  const carregar = useCallback(async () => {
    try {
      const r = await carregarRotas();
      setOffline(r.offline);
      setRota(r.rotas.find((x) => x.id === rotaId) ?? null);
    } catch (e) {
      Alert.alert('Erro', (e as Error).message);
    }
  }, [rotaId]);
  useFocusEffect(useCallback(() => { void carregar(); }, [carregar]));

  useEffect(() => {
    if (rota?.status === 'EM_ROTA' && rota.editavel) void gps.iniciar(rota.id);
    if (rota && rota.status !== 'EM_ROTA' && gps.rotaRastreada() === rota.id) void gps.parar();
  }, [rota]);

  if (!rota) return <Carregando />;

  const aceitar = async () => {
    setOcupado(true);
    try {
      const pos = await gps.posicaoAtual();
      if (rota.editavel) {
        await local.marcarRota(rota.id, 'ACEITA');
        await fila.enfileirar({ uuid: fila.novoUuid(), tipo: 'ROTA', rotaId: rota.id, acao: 'aceitar', corpo: { ...pos, ocorrido_em: fila.agoraIso() }, criadoEm: fila.agoraIso(), tentativas: 0 });
      } else {
        await api.aceitarRota(rota.id, { ...pos, ocorrido_em: fila.agoraIso() });
      }
      await carregar();
    } catch (e) {
      Alert.alert('Não deu', e instanceof api.ErroRede ? 'Sem conexão. Tente de novo com sinal.' : (e as Error).message);
    } finally {
      setOcupado(false);
    }
  };

  const recusar = async () => {
    if (!motivoRecusa.trim()) return Alert.alert('Informe o motivo da recusa.');
    setOcupado(true);
    try {
      await api.recusarRota(rota.id, motivoRecusa.trim());
      setRecusando(false);
      await carregar();
    } catch (e) {
      Alert.alert('Não deu', e instanceof api.ErroRede ? 'Sem conexão. Recusa precisa de sinal.' : (e as Error).message);
    } finally {
      setOcupado(false);
    }
  };

  const iniciar = async () => {
    setOcupado(true);
    try {
      const pos = await gps.posicaoAtual();
      await local.marcarRota(rota.id, 'EM_ROTA');
      await fila.enfileirar({ uuid: fila.novoUuid(), tipo: 'ROTA', rotaId: rota.id, acao: 'iniciar', corpo: { ...pos, ocorrido_em: fila.agoraIso() }, criadoEm: fila.agoraIso(), tentativas: 0 });
      await gps.iniciar(rota.id);
      await carregar();
    } finally {
      setOcupado(false);
    }
  };

  const finalizar = () => {
    const pendentes = rota.paradas.filter((p) => p.situacao === 'PENDENTE' || p.situacao === 'EM_ROTA').length;
    if (pendentes > 0) return Alert.alert('Ainda faltam paradas', `${pendentes} parada(s) sem resultado.`);
    Alert.prompt?.('Pedágio (R$)', 'Informe o total de pedágio da rota, ou deixe em branco.', async (valor) => {
      await concluir(valor ? Number(String(valor).replace(',', '.')) : null);
    }) ?? void concluir(null);
  };

  const concluir = async (pedagio: number | null) => {
    setOcupado(true);
    try {
      await gps.parar();
      await local.marcarRota(rota.id, 'CONCLUIDA');
      await fila.enfileirar({ uuid: fila.novoUuid(), tipo: 'ROTA', rotaId: rota.id, acao: 'finalizar', corpo: { pedagio, ocorrido_em: fila.agoraIso() }, criadoEm: fila.agoraIso(), tentativas: 0 });
      await carregar();
      Alert.alert('Rota concluída', 'Obrigado! O extrato atualiza assim que os registros forem enviados.');
    } finally {
      setOcupado(false);
    }
  };

  const feitas = rota.paradas.filter((p) => ['ENTREGUE', 'PARCIAL', 'INSUCESSO'].includes(p.situacao)).length;
  const ativas = rota.paradas.filter((p) => p.situacao !== 'CANCELADA');

  return (
    <ScrollView style={s.tela} contentContainerStyle={{ padding: 16, paddingBottom: 48 }} refreshControl={<RefreshControl refreshing={false} onRefresh={carregar} />}>
      {offline ? <Text style={s.aviso}>Sem conexão — trabalhando com a última versão salva.</Text> : null}
      <Cartao>
        <Text style={s.nome}>{rota.nome ?? `Rota #${rota.id}`}</Text>
        <Text style={s.sub}>{formatarData(rota.data_rota)}{rota.start_at ? ` · saída ${rota.start_at.slice(11, 16)}` : ''} · {statusRotaRotulo[rota.status]}</Text>
        <View style={s.barraFundo}><View style={[s.barra, { width: `${ativas.length ? (feitas / ativas.length) * 100 : 0}%` }]} /></View>
        <Text style={s.sub}>{feitas} de {ativas.length} paradas com resultado{rota.km_estimado ? ` · ~${rota.km_estimado.toFixed(0)} km` : ''}</Text>

        {rota.status === 'PLANEJADA' && !recusando ? (
          <View style={{ gap: 8, marginTop: 12 }}>
            <Botao titulo="Aceitar rota" onPress={aceitar} carregando={ocupado} />
            <Botao titulo="Recusar" tipo="secundario" onPress={() => setRecusando(true)} />
          </View>
        ) : null}
        {recusando ? (
          <View style={{ marginTop: 12 }}>
            <TextInput style={s.campo} placeholder="Motivo da recusa" value={motivoRecusa} onChangeText={setMotivoRecusa} multiline />
            <View style={{ flexDirection: 'row', gap: 8, marginTop: 8 }}>
              <Botao titulo="Voltar" tipo="secundario" onPress={() => setRecusando(false)} estilo={{ flex: 1 }} />
              <Botao titulo="Confirmar recusa" tipo="perigo" onPress={recusar} carregando={ocupado} estilo={{ flex: 1 }} />
            </View>
          </View>
        ) : null}
        {rota.status === 'ACEITA' && rota.editavel ? <Botao titulo="Iniciar rota" onPress={iniciar} carregando={ocupado} estilo={{ marginTop: 12 }} /> : null}
        {rota.status === 'EM_ROTA' && rota.editavel ? <Botao titulo="Finalizar rota" tipo="alerta" onPress={finalizar} carregando={ocupado} estilo={{ marginTop: 12 }} /> : null}
        {!rota.editavel ? <Text style={s.vuupt}>Esta rota é operada pelo app da VUUPT. Aqui você só aceita/recusa e consulta.</Text> : null}
        {rota.confirmacao ? <Text style={s.sub}>Confirmação: {rota.confirmacao.status.toLowerCase()}</Text> : null}
      </Cartao>

      {rota.paradas.map((p) => (
        <Pressable key={p.id} onPress={() => (rota.editavel && rota.status === 'EM_ROTA' ? router.push(`/parada/${p.id}?rota=${rota.id}`) : undefined)}>
          <Cartao estilo={{ opacity: p.situacao === 'CANCELADA' ? 0.5 : 1 }}>
            <View style={s.paradaCab}>
              <View style={s.ordem}><Text style={s.ordemTexto}>{p.ordem}</Text></View>
              <View style={{ flex: 1 }}>
                <Text style={s.paradaTitulo} numberOfLines={2}>{p.destinatario_nome || p.titulo || p.codigo}</Text>
                <Text style={s.paradaEnd} numberOfLines={2}>{p.endereco}</Text>
              </View>
              <Etiqueta texto={situacaoRotulo[p.situacao]} cor={situacaoCor[p.situacao]} />
            </View>
            <Text style={s.paradaMeta}>
              {p.codigo}{p.remetente_nome ? ` · ${p.remetente_nome}` : ''}{p.volume_caixas ? ` · ${p.volume_caixas} cx` : ''}
              {p.janela_inicio ? ` · ${p.janela_inicio}–${p.janela_fim}` : ''}{p.nivel_dificuldade && p.nivel_dificuldade >= 3 ? ' · ⚠ entrega demorada' : ''}
            </Text>
            {p.motivo_texto ? <Text style={[s.paradaMeta, { color: cores.perigo }]}>Motivo: {p.motivo_texto}</Text> : null}
            <View style={{ flexDirection: 'row', gap: 8, marginTop: 10 }}>
              <Botao titulo="Navegar" tipo="secundario" onPress={() => abrirNavegacao(p)} estilo={{ flex: 1, minHeight: 44, paddingVertical: 10 }} />
              {p.telefone ? <Botao titulo="Ligar" tipo="secundario" onPress={() => void Linking.openURL(`tel:${p.telefone}`)} estilo={{ flex: 1, minHeight: 44, paddingVertical: 10 }} /> : null}
              {rota.editavel && rota.status === 'EM_ROTA' && (p.situacao === 'PENDENTE' || p.situacao === 'EM_ROTA') ? (
                <Botao titulo="Registrar" onPress={() => router.push(`/parada/${p.id}?rota=${rota.id}`)} estilo={{ flex: 1, minHeight: 44, paddingVertical: 10 }} />
              ) : null}
            </View>
          </Cartao>
        </Pressable>
      ))}
    </ScrollView>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.fundo },
  aviso: { backgroundColor: cores.alerta, color: '#fff', padding: 8, borderRadius: 8, textAlign: 'center', marginBottom: 12, fontWeight: '600' },
  nome: { fontSize: 19, fontWeight: '800', color: cores.texto },
  sub: { color: cores.textoSuave, marginTop: 4 },
  barraFundo: { height: 8, backgroundColor: cores.borda, borderRadius: 4, marginTop: 10, overflow: 'hidden' },
  barra: { height: 8, backgroundColor: cores.primaria },
  vuupt: { color: cores.info, marginTop: 10 },
  campo: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 10, padding: 12, minHeight: 60 },
  paradaCab: { flexDirection: 'row', gap: 10, alignItems: 'flex-start' },
  ordem: { width: 32, height: 32, borderRadius: 16, backgroundColor: cores.texto, alignItems: 'center', justifyContent: 'center' },
  ordemTexto: { color: '#fff', fontWeight: '800' },
  paradaTitulo: { fontWeight: '700', color: cores.texto, fontSize: 15 },
  paradaEnd: { color: cores.textoSuave, fontSize: 13 },
  paradaMeta: { color: cores.textoSuave, fontSize: 12, marginTop: 6 },
});
