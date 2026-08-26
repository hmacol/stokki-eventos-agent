// Tela da rota (Hugo, 26/08): ações principais por ARRASTAR pra direita
// (aceitar, iniciar rota, finalizar) e cada parada com os passos
// "Iniciar deslocamento" -> "Cheguei no local" -> registrar resultado.
// Os passos alimentam started_at / arrived_at / completed_at da parada
// (tempo de deslocamento e tempo no local viram métrica no núcleo).
import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Linking, Pressable, RefreshControl, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { useFocusEffect, useLocalSearchParams, useRouter } from 'expo-router';
import * as api from '../../src/api';
import * as fila from '../../src/fila';
import * as gps from '../../src/gps';
import * as local from '../../src/local';
import { carregarRotas } from '../../src/rotasStore';
import { Deslizar } from '../../src/deslizar';
import { Botao, Cartao, Carregando, Etiqueta } from '../../src/componentes';
import { cores, formatarData, situacaoCor, situacaoRotulo, statusRotaRotulo } from '../../src/tema';
import type { Parada, Rota, SituacaoParada } from '../../src/tipos';

const FINAIS: SituacaoParada[] = ['ENTREGUE', 'PARCIAL', 'INSUCESSO', 'CANCELADA'];
const ehPendente = (p: Parada) => !FINAIS.includes(p.situacao);

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
  const [atualId, setAtualId] = useState<number | null>(null);

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

  // Parada atual: a que já está a caminho/no local; senão a escolhida; senão a 1ª pendente na ordem.
  const pendentes = rota.paradas.filter(ehPendente);
  const emAndamento = rota.paradas.find((p) => p.situacao === 'EM_DESLOCAMENTO' || p.situacao === 'EM_ROTA');
  const atual = emAndamento ?? pendentes.find((p) => p.id === atualId) ?? pendentes[0] ?? null;
  const feitas = rota.paradas.filter((p) => ['ENTREGUE', 'PARCIAL', 'INSUCESSO'].includes(p.situacao)).length;
  const ativas = rota.paradas.filter((p) => p.situacao !== 'CANCELADA');
  const operavel = rota.editavel && rota.status === 'EM_ROTA';

  const acaoRota = async (acao: 'aceitar' | 'iniciar' | 'finalizar', status: Rota['status'], extra: Record<string, unknown> = {}) => {
    setOcupado(true);
    try {
      const pos = await gps.posicaoAtual();
      const corpo = { ...pos, ...extra, ocorrido_em: fila.agoraIso() };
      if (rota.editavel) {
        await local.marcarRota(rota.id, status);
        await fila.enfileirar({ uuid: fila.novoUuid(), tipo: 'ROTA', rotaId: rota.id, acao, corpo, criadoEm: fila.agoraIso(), tentativas: 0 });
      } else {
        await api.aceitarRota(rota.id, corpo);
      }
      if (acao === 'iniciar') await gps.iniciar(rota.id);
      if (acao === 'finalizar') await gps.parar();
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

  const passoParada = async (p: Parada, tipo: 'DESLOCAMENTO' | 'CHEGADA') => {
    const pos = await gps.posicaoAtual();
    await local.marcarParada(p.id, tipo === 'DESLOCAMENTO' ? 'EM_DESLOCAMENTO' : 'EM_ROTA');
    await fila.enfileirar({
      uuid: fila.novoUuid(), tipo: 'EVENTO_PARADA', paradaId: p.id, criadoEm: fila.agoraIso(), tentativas: 0,
      corpo: { tipo, ocorrido_em: fila.agoraIso(), ...pos },
    });
    await carregar();
  };

  const finalizar = () => {
    if (pendentes.length > 0) return Alert.alert('Ainda faltam paradas', `${pendentes.length} parada(s) sem resultado.`);
    Alert.prompt?.('Pedágio (R$)', 'Informe o total de pedágio da rota, ou deixe em branco.', (valor) => {
      void acaoRota('finalizar', 'CONCLUIDA', { pedagio: valor ? Number(String(valor).replace(',', '.')) : null });
    }) ?? void acaoRota('finalizar', 'CONCLUIDA', { pedagio: null });
  };

  return (
    <ScrollView style={s.tela} contentContainerStyle={{ padding: 16, paddingBottom: 48 }} refreshControl={<RefreshControl refreshing={false} onRefresh={carregar} />}>
      {offline ? <Text style={s.aviso}>Sem conexão — trabalhando com a última versão salva.</Text> : null}

      {/* Cabeçalho da rota + ação principal por gesto */}
      <Cartao>
        <Text style={s.nome}>{rota.nome ?? `Rota #${rota.id}`}</Text>
        <Text style={s.sub}>{formatarData(rota.data_rota)}{rota.start_at ? ` · saída ${rota.start_at.slice(11, 16)}` : ''} · {statusRotaRotulo[rota.status]}</Text>
        <View style={s.barraFundo}><View style={[s.barra, { width: `${ativas.length ? (feitas / ativas.length) * 100 : 0}%` }]} /></View>
        <Text style={s.sub}>{feitas} de {ativas.length} paradas com resultado{rota.km_estimado ? ` · ~${rota.km_estimado.toFixed(0)} km` : ''}</Text>

        {rota.status === 'PLANEJADA' && !recusando ? (
          <View style={{ marginTop: 14 }}>
            <Deslizar titulo="Aceitar rota" icone="checkmark" onConfirmar={() => acaoRota('aceitar', 'ACEITA')} desabilitado={ocupado} />
            <Pressable onPress={() => setRecusando(true)} style={{ marginTop: 10 }}><Text style={s.link}>Não posso fazer esta rota (recusar)</Text></Pressable>
          </View>
        ) : null}
        {recusando ? (
          <View style={{ marginTop: 12 }}>
            <TextInput style={s.campo} placeholder="Motivo da recusa" placeholderTextColor="#9CA3AF" value={motivoRecusa} onChangeText={setMotivoRecusa} multiline />
            <View style={{ flexDirection: 'row', gap: 8, marginTop: 8 }}>
              <Botao titulo="Voltar" tipo="secundario" onPress={() => setRecusando(false)} estilo={{ flex: 1 }} />
              <Botao titulo="Confirmar recusa" tipo="perigo" onPress={recusar} carregando={ocupado} estilo={{ flex: 1 }} />
            </View>
          </View>
        ) : null}
        {rota.status === 'ACEITA' && rota.editavel ? (
          <View style={{ marginTop: 14 }}>
            <Deslizar titulo="Iniciar rota" icone="play" onConfirmar={() => acaoRota('iniciar', 'EM_ROTA')} desabilitado={ocupado} />
          </View>
        ) : null}
        {operavel && pendentes.length === 0 ? (
          <View style={{ marginTop: 14 }}>
            <Deslizar titulo="Finalizar rota" icone="flag" cor={cores.alerta} onConfirmar={finalizar} desabilitado={ocupado} />
          </View>
        ) : null}
        {!rota.editavel ? <Text style={s.vuupt}>Esta rota é operada pelo app da VUUPT. Aqui você só aceita/recusa e consulta.</Text> : null}
        {rota.confirmacao ? <Text style={s.sub}>Confirmação: {rota.confirmacao.status.toLowerCase()}</Text> : null}
      </Cartao>

      {/* Parada atual: passos por gesto */}
      {operavel && atual ? (
        <Cartao estilo={{ borderColor: cores.acento, borderWidth: 2 }}>
          <Text style={s.secao}>PARADA ATUAL · {atual.ordem} de {ativas.length}</Text>
          <Text style={s.paradaTituloGrande}>{atual.destinatario_nome || atual.titulo || atual.codigo}</Text>
          <Text style={s.paradaEndGrande}>{atual.endereco}</Text>
          <Text style={s.paradaMeta}>
            {atual.codigo}{atual.remetente_nome ? ` · ${atual.remetente_nome}` : ''}{atual.volume_caixas ? ` · ${atual.volume_caixas} cx` : ''}
            {atual.janela_inicio ? ` · ${atual.janela_inicio}–${atual.janela_fim}` : ''}
          </Text>
          <View style={{ flexDirection: 'row', gap: 8, marginTop: 10 }}>
            <Botao titulo="Navegar" tipo="secundario" onPress={() => abrirNavegacao(atual)} estilo={{ flex: 1, minHeight: 44, paddingVertical: 10 }} />
            {atual.telefone ? <Botao titulo="Ligar" tipo="secundario" onPress={() => void Linking.openURL(`tel:${atual.telefone}`)} estilo={{ flex: 1, minHeight: 44, paddingVertical: 10 }} /> : null}
          </View>

          <View style={s.passos}>
            <Passo numero={1} rotulo="Deslocamento" feito={atual.situacao !== 'PENDENTE'} ativo={atual.situacao === 'PENDENTE'} />
            <Passo numero={2} rotulo="No local" feito={atual.situacao === 'EM_ROTA'} ativo={atual.situacao === 'EM_DESLOCAMENTO'} />
            <Passo numero={3} rotulo="Resultado" feito={false} ativo={atual.situacao === 'EM_ROTA'} />
          </View>

          {atual.situacao === 'PENDENTE' ? (
            <Deslizar titulo="Iniciar deslocamento" icone="navigate" onConfirmar={() => passoParada(atual, 'DESLOCAMENTO')} />
          ) : null}
          {atual.situacao === 'EM_DESLOCAMENTO' ? (
            <Deslizar titulo="Cheguei no local" icone="location" cor={cores.info} onConfirmar={() => passoParada(atual, 'CHEGADA')} />
          ) : null}
          {atual.situacao === 'EM_ROTA' ? (
            <Botao titulo="Registrar resultado da entrega" onPress={() => router.push(`/parada/${atual.id}?rota=${rota.id}`)} />
          ) : null}
        </Cartao>
      ) : null}

      {/* Lista completa */}
      <Text style={s.secao}>TODAS AS PARADAS</Text>
      {rota.paradas.map((p) => {
        const ehAtual = atual?.id === p.id;
        return (
          <Cartao key={p.id} estilo={{ opacity: p.situacao === 'CANCELADA' ? 0.5 : 1, borderColor: ehAtual ? cores.acento : cores.borda }}>
            <View style={s.paradaCab}>
              <View style={[s.ordem, ehAtual && { backgroundColor: cores.acento }]}><Text style={s.ordemTexto}>{p.ordem}</Text></View>
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
            {operavel && ehPendente(p) && !ehAtual && !emAndamento ? (
              <Pressable onPress={() => setAtualId(p.id)} style={{ marginTop: 8 }}><Text style={s.link}>Atender esta parada agora →</Text></Pressable>
            ) : null}
          </Cartao>
        );
      })}
    </ScrollView>
  );
}

function Passo({ numero, rotulo, feito, ativo }: { numero: number; rotulo: string; feito: boolean; ativo: boolean }) {
  const cor = feito ? cores.acento : ativo ? cores.primaria : cores.borda;
  return (
    <View style={s.passo}>
      <View style={[s.passoBolinha, { backgroundColor: cor }]}>
        <Text style={s.passoNum}>{feito ? '✓' : numero}</Text>
      </View>
      <Text style={[s.passoRotulo, (feito || ativo) && { color: cores.texto, fontWeight: '700' }]}>{rotulo}</Text>
    </View>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.fundo },
  aviso: { backgroundColor: cores.alerta, color: '#fff', padding: 8, borderRadius: 8, textAlign: 'center', marginBottom: 12, fontWeight: '600' },
  nome: { fontSize: 19, fontWeight: '800', color: cores.texto },
  sub: { color: cores.textoSuave, marginTop: 4 },
  secao: { fontSize: 13, fontWeight: '800', color: cores.textoSuave, marginBottom: 8, marginTop: 4, letterSpacing: 0.5 },
  link: { color: cores.info, fontWeight: '700', textAlign: 'center' },
  barraFundo: { height: 8, backgroundColor: cores.borda, borderRadius: 4, marginTop: 10, overflow: 'hidden' },
  barra: { height: 8, backgroundColor: cores.acento },
  vuupt: { color: cores.info, marginTop: 10 },
  campo: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 10, padding: 12, minHeight: 60, color: cores.texto },
  paradaTituloGrande: { fontSize: 20, fontWeight: '800', color: cores.texto },
  paradaEndGrande: { fontSize: 15, color: cores.texto, marginTop: 4 },
  passos: { flexDirection: 'row', justifyContent: 'space-between', marginVertical: 14, paddingHorizontal: 8 },
  passo: { alignItems: 'center', flex: 1 },
  passoBolinha: { width: 30, height: 30, borderRadius: 15, alignItems: 'center', justifyContent: 'center' },
  passoNum: { color: '#fff', fontWeight: '800' },
  passoRotulo: { fontSize: 12, color: cores.textoSuave, marginTop: 4 },
  paradaCab: { flexDirection: 'row', gap: 10, alignItems: 'flex-start' },
  ordem: { width: 32, height: 32, borderRadius: 16, backgroundColor: cores.primaria, alignItems: 'center', justifyContent: 'center' },
  ordemTexto: { color: '#fff', fontWeight: '800' },
  paradaTitulo: { fontWeight: '700', color: cores.texto, fontSize: 15 },
  paradaEnd: { color: cores.textoSuave, fontSize: 13 },
  paradaMeta: { color: cores.textoSuave, fontSize: 12, marginTop: 6 },
});
