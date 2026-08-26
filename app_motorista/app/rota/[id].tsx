// Tela da rota (Hugo, 26/08): TODA ação principal é por ARRASTAR pra
// direita. Rota: aceitar -> iniciar -> finalizar. Cada card de parada tem
// UM controle que evolui com o estado dela:
//   Iniciar deslocamento >>>  Cheguei no local >>>  Finalizar entrega >>>
// e o último abre a tela de resultado (Entregue / Parcial / Não entregue /
// Reagendar). Só uma parada "em andamento" por vez -- os outros cards
// ficam travados até ela ser resolvida ou reagendada.
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
const emAndamento = (p: Parada) => p.situacao === 'EM_DESLOCAMENTO' || p.situacao === 'EM_ROTA';

function abrirNavegacao(p: Parada) {
  const destino = p.latitude !== null && p.longitude !== null ? `${p.latitude},${p.longitude}` : encodeURIComponent(p.endereco ?? '');
  Alert.alert('Navegar até a parada', undefined, [
    { text: 'Google Maps', onPress: () => void Linking.openURL(`https://www.google.com/maps/dir/?api=1&destination=${destino}&travelmode=driving`) },
    { text: 'Waze', onPress: () => void Linking.openURL(p.latitude !== null ? `https://waze.com/ul?ll=${destino}&navigate=yes` : `https://waze.com/ul?q=${destino}&navigate=yes`) },
    { text: 'Cancelar', style: 'cancel' },
  ]);
}

function rotuloRetorno(p: Parada): string | null {
  if (!p.reagendado_para) return null;
  if (p.reagendado_para === 'FIM') return 'Reagendada: depois das outras';
  return `Reagendada para ${p.reagendado_para.slice(11, 16) || p.reagendado_para}`;
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

  const pendentes = rota.paradas.filter(ehPendente);
  const ativa = rota.paradas.find(emAndamento) ?? null;       // a parada em andamento (no máximo uma)
  const feitas = rota.paradas.filter((p) => ['ENTREGUE', 'PARCIAL', 'INSUCESSO'].includes(p.situacao)).length;
  const ativas = rota.paradas.filter((p) => p.situacao !== 'CANCELADA');
  const operavel = rota.editavel && rota.status === 'EM_ROTA';

  // Ordem de exibição: sequência da rota, com as reagendadas "depois das
  // outras" no fim e as reagendadas com horário no fim ordenadas por horário.
  const paradasOrdenadas = [...rota.paradas].sort((a, b) => {
    const ka = a.reagendado_para && ehPendente(a) ? (a.reagendado_para === 'FIM' ? 2 : 1) : 0;
    const kb = b.reagendado_para && ehPendente(b) ? (b.reagendado_para === 'FIM' ? 2 : 1) : 0;
    if (ka !== kb) return ka - kb;
    if (ka === 1) return (a.reagendado_para ?? '').localeCompare(b.reagendado_para ?? '');
    return a.ordem - b.ordem;
  });

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

  /** O único controle do card, conforme o estado da parada. */
  const controleParada = (p: Parada) => {
    if (!operavel || !ehPendente(p)) return null;
    const travada = ativa !== null && ativa.id !== p.id;
    if (travada) {
      return <Text style={s.travada}>Finalize a parada {ativa.ordem} pra liberar esta.</Text>;
    }
    if (p.situacao === 'PENDENTE') {
      return <Deslizar titulo={p.tentativas > 0 ? 'Voltar: iniciar deslocamento' : 'Iniciar deslocamento'} icone="navigate" onConfirmar={() => passoParada(p, 'DESLOCAMENTO')} />;
    }
    if (p.situacao === 'EM_DESLOCAMENTO') {
      return <Deslizar titulo="Cheguei no local" icone="location" cor={cores.info} onConfirmar={() => passoParada(p, 'CHEGADA')} />;
    }
    return <Deslizar titulo="Finalizar entrega" icone="checkmark-done" cor={cores.primaria} onConfirmar={() => router.push(`/parada/${p.id}?rota=${rota.id}`)} />;
  };

  return (
    <ScrollView style={s.tela} contentContainerStyle={{ padding: 16, paddingBottom: 48 }} refreshControl={<RefreshControl refreshing={false} onRefresh={carregar} />}>
      {offline ? <Text style={s.aviso}>Sem conexão — trabalhando com a última versão salva.</Text> : null}

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

      {paradasOrdenadas.map((p) => {
        const destaque = ativa?.id === p.id;
        const retorno = rotuloRetorno(p);
        return (
          <Cartao key={p.id} estilo={{ opacity: p.situacao === 'CANCELADA' ? 0.5 : 1, borderColor: destaque ? cores.acento : cores.borda, borderWidth: destaque ? 2 : 1 }}>
            <View style={s.paradaCab}>
              <View style={[s.ordem, destaque && { backgroundColor: cores.acento }]}><Text style={s.ordemTexto}>{p.ordem}</Text></View>
              <View style={{ flex: 1 }}>
                <Text style={s.paradaTitulo} numberOfLines={2}>{p.destinatario_nome || p.titulo || p.codigo}</Text>
                <Text style={s.paradaEnd} numberOfLines={3}>{p.endereco}</Text>
              </View>
              <Etiqueta texto={situacaoRotulo[p.situacao]} cor={situacaoCor[p.situacao]} />
            </View>
            <Text style={s.paradaMeta}>
              {p.codigo}{p.remetente_nome ? ` · ${p.remetente_nome}` : ''}{p.volume_caixas ? ` · ${p.volume_caixas} cx` : ''}
              {p.janela_inicio ? ` · ${p.janela_inicio}–${p.janela_fim}` : ''}{p.nivel_dificuldade && p.nivel_dificuldade >= 3 ? ' · ⚠ entrega demorada' : ''}
            </Text>
            {retorno && ehPendente(p) ? <Text style={[s.paradaMeta, { color: cores.alerta, fontWeight: '700' }]}>{retorno}{p.tentativas > 0 ? ` · ${p.tentativas}ª tentativa feita` : ''}</Text> : null}
            {p.motivo_texto ? <Text style={[s.paradaMeta, { color: cores.perigo }]}>Motivo: {p.motivo_texto}</Text> : null}
            {ehPendente(p) ? (
              <View style={{ flexDirection: 'row', gap: 8, marginTop: 10 }}>
                <Botao titulo="Navegar" tipo="secundario" onPress={() => abrirNavegacao(p)} estilo={{ flex: 1, minHeight: 42, paddingVertical: 8 }} />
                {p.telefone ? <Botao titulo="Ligar" tipo="secundario" onPress={() => void Linking.openURL(`tel:${p.telefone}`)} estilo={{ flex: 1, minHeight: 42, paddingVertical: 8 }} /> : null}
              </View>
            ) : null}
            <View style={{ marginTop: 10 }}>{controleParada(p)}</View>
          </Cartao>
        );
      })}
    </ScrollView>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.fundo },
  aviso: { backgroundColor: cores.alerta, color: '#fff', padding: 8, borderRadius: 8, textAlign: 'center', marginBottom: 12, fontWeight: '600' },
  nome: { fontSize: 19, fontWeight: '800', color: cores.texto },
  sub: { color: cores.textoSuave, marginTop: 4 },
  link: { color: cores.info, fontWeight: '700', textAlign: 'center' },
  travada: { color: cores.textoSuave, fontStyle: 'italic', textAlign: 'center', paddingVertical: 6 },
  barraFundo: { height: 8, backgroundColor: cores.borda, borderRadius: 4, marginTop: 10, overflow: 'hidden' },
  barra: { height: 8, backgroundColor: cores.acento },
  vuupt: { color: cores.info, marginTop: 10 },
  campo: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 10, padding: 12, minHeight: 60, color: cores.texto },
  paradaCab: { flexDirection: 'row', gap: 10, alignItems: 'flex-start' },
  ordem: { width: 32, height: 32, borderRadius: 16, backgroundColor: cores.primaria, alignItems: 'center', justifyContent: 'center' },
  ordemTexto: { color: '#fff', fontWeight: '800' },
  paradaTitulo: { fontWeight: '700', color: cores.texto, fontSize: 15 },
  paradaEnd: { color: cores.textoSuave, fontSize: 13 },
  paradaMeta: { color: cores.textoSuave, fontSize: 12, marginTop: 6 },
});
