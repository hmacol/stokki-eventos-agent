// Tela da rota (Hugo, 26/08): TODA ação principal é por ARRASTAR pra
// direita. Rota: aceitar -> iniciar -> finalizar. Cada card de parada tem
// UM controle que evolui com o estado dela:
//   Iniciar deslocamento >>>  Cheguei no local >>>  Finalizar entrega >>>
// e o último abre a tela de resultado (Entregue / Parcial / Não entregue /
// Reagendar). Só uma parada "em andamento" por vez -- os outros cards
// ficam travados até ela ser resolvida ou reagendada.
//
// Layout (Hugo, 12/09): UMA PARADA POR VEZ. O app já só deixa operar uma
// parada de cada vez, então a tela mostra isso -- a parada da vez ocupa um
// cartão grande no alto (nome em 22, janela com contagem regressiva,
// ações e o deslizar) e as outras viram linhas de uma altura que abrem no
// toque. Com 14 paradas a rota inteira cabe em pouco mais de uma tela.
import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Image, Linking, Pressable, RefreshControl, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { useFocusEffect, useLocalSearchParams, useRouter } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';
import * as ImagePicker from 'expo-image-picker';
import * as api from '../../src/api';
import * as fila from '../../src/fila';
import * as gps from '../../src/gps';
import * as local from '../../src/local';
import { carregarRotas } from '../../src/rotasStore';
import { Deslizar } from '../../src/deslizar';
import { Botao, Cartao, Carregando } from '../../src/componentes';
import { cores, formatarData, situacaoCor, situacaoRotulo, statusRotaRotulo } from '../../src/tema';
import { horaDe, nomeExibicao, partesEndereco, resumoJanela } from '../../src/textos';
import type { Parada, Rota, SituacaoParada } from '../../src/tipos';

const FINAIS: SituacaoParada[] = ['ENTREGUE', 'PARCIAL', 'INSUCESSO', 'CANCELADA'];
// Rótulo da etapa no alto do cartão da vez -- o motorista lê isto antes do nome.
const ETAPA_ROTULO: Record<string, string> = { PENDENTE: 'Próxima parada', EM_DESLOCAMENTO: 'A caminho', EM_ROTA: 'No local' };
// Faixa da janela: neutra com folga, âmbar faltando 2h, vermelha depois de fechar.
const TOM_JANELA = {
  neutro: { fundo: cores.fundo, forte: cores.texto, suave: cores.textoSuave, icone: cores.textoSuave },
  alerta: { fundo: cores.alertaBg, forte: '#7A5211', suave: '#9A6716', icone: '#9A6716' },
  perigo: { fundo: cores.perigoBg, forte: cores.perigo, suave: cores.perigo, icone: cores.perigo },
  ausente: { fundo: cores.fundo, forte: cores.textoSuave, suave: cores.textoSuave, icone: cores.textoSuave },
};
const PEDAGIO_ROTULO: Record<string, string> = { PENDENTE: 'aguardando aprovação', APROVADO: 'aprovado', REJEITADO: 'rejeitado', CANCELADO: 'cancelado por você' };
const PEDAGIO_COR: Record<string, string> = { PENDENTE: cores.alerta, APROVADO: cores.sucesso, REJEITADO: cores.perigo, CANCELADO: cores.textoSuave };
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

// WhatsApp exige só dígitos com DDI. Telefone cadastrado costuma vir como
// "(11) 91234-5678" ou "11912345678"; sem DDI a gente assume Brasil (55).
function numeroWhatsapp(telefone: string | null | undefined): string | null {
  const digitos = (telefone ?? '').replace(/\D/g, '');
  if (digitos.length < 10) return null;
  return digitos.startsWith('55') && digitos.length >= 12 ? digitos : `55${digitos}`;
}

function abrirWhatsapp(p: Parada) {
  const numero = numeroWhatsapp(p.telefone);
  if (!numero) return;
  const texto = encodeURIComponent(`Olá! Sou o motorista da Fresh Log e estou a caminho com a sua entrega (${p.codigo}).`);
  Linking.openURL(`https://wa.me/${numero}?text=${texto}`).catch(() => Alert.alert('WhatsApp', 'Não foi possível abrir o WhatsApp neste aparelho.'));
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
  // Lista compacta: uma linha aberta por vez (endereço inteiro + ações).
  const [aberta, setAberta] = useState<number | null>(null);
  // Relógio da contagem regressiva da janela -- de minuto em minuto basta.
  const [agora, setAgora] = useState(() => Date.now());
  // Pedágio (Hugo, 11/09): reembolso à parte -- valor + foto do recibo,
  // um por recibo, enviado pela fila offline; aprovado no painel.
  const [pedagioAberto, setPedagioAberto] = useState(false);
  const [pedagioValor, setPedagioValor] = useState('');
  const [pedagioFoto, setPedagioFoto] = useState<string | null>(null);
  const [pedagiosNaFila, setPedagiosNaFila] = useState<{ uuid: string; valor: number }[]>([]);
  // Conferência automática do recibo (Hugo, 12/09) -- só roda se o
  // servidor disser que está ligada (carregada junto do checklist).
  const [validacaoAtiva, setValidacaoAtiva] = useState(false);
  const [conferindoPedagio, setConferindoPedagio] = useState(false);
  const [tentativasPedagio, setTentativasPedagio] = useState(0);
  const [avisoPedagio, setAvisoPedagio] = useState('');

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

  useEffect(() => {
    // Sem rede a conferência fica desligada e o fluxo é o de sempre.
    api.checklist().then((c) => setValidacaoAtiva(c.validacao_fotos?.ativo === true)).catch(() => setValidacaoAtiva(false));
  }, []);

  useEffect(() => {
    const t = setInterval(() => setAgora(Date.now()), 60000);
    return () => clearInterval(t);
  }, []);

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

  // A parada da vez: a que está em andamento ou, se nenhuma estiver, a
  // primeira pendente da ordem. Só faz sentido com a rota em andamento --
  // rota por aceitar ou já concluída vira lista compacta pura.
  const atual = operavel ? ativa ?? paradasOrdenadas.find(ehPendente) ?? null : null;
  const depoisDesta = paradasOrdenadas.filter((p) => ehPendente(p) && p.id !== atual?.id);
  const jaFeitas = paradasOrdenadas.filter((p) => !ehPendente(p));

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
    void acaoRota('finalizar', 'CONCLUIDA');
  };

  /** Conferência automática do recibo (Hugo, 12/09): nitidez + "isto é
   * mesmo um recibo de pedágio?". Reprovado trava até o limite de
   * tentativas; depois o motorista pode seguir e o Hugo confere no
   * painel. Sem sinal ou com a validação desligada, aceita direto. */
  const fotografarPedagio = async () => {
    const perm = await ImagePicker.requestCameraPermissionsAsync();
    if (!perm.granted) return Alert.alert('Câmera', 'Permita o uso da câmera pra fotografar o recibo.');
    const r = await ImagePicker.launchCameraAsync({ quality: 0.6, allowsEditing: false, exif: false });
    if (r.canceled || !r.assets[0]) return;
    const uri = r.assets[0].uri;
    if (!validacaoAtiva) return setPedagioFoto(uri);

    const tentativa = tentativasPedagio + 1;
    setTentativasPedagio(tentativa);
    setConferindoPedagio(true);
    try {
      const valor = Number(pedagioValor.replace(',', '.'));
      const v = await api.validarFoto(uri, {
        tipo: 'PEDAGIO', rotaId: rota.id, tentativa,
        ...(Number.isFinite(valor) && valor > 0 ? { valor } : {}),
      });
      if (v.resultado !== 'REPROVADO') {
        setPedagioFoto(uri);
        setAvisoPedagio(v.avisos?.includes('VALOR_DIVERGE')
          ? `O recibo mostra R$ ${(v.valor_lido ?? 0).toFixed(2).replace('.', ',')} — confira o valor digitado.`
          : v.resultado === 'NAO_VERIFICADO' ? 'Foto não conferida — vai pra revisão.' : '');
        return;
      }
      const motivo = v.motivo ?? 'A foto não ficou boa.';
      if (!v.pode_seguir) return Alert.alert('Foto não serve', `${motivo}\n\nTire outra foto.`);
      Alert.alert('Ainda não ficou boa', `${motivo}\n\nVocê pode tentar de novo ou seguir assim — o pedágio vai pra conferência manual.`, [
        { text: 'Tirar de novo', style: 'cancel' },
        { text: 'Não consigo melhorar', onPress: () => { setPedagioFoto(uri); setAvisoPedagio('Segue pra conferência manual.'); } },
      ]);
    } catch {
      setPedagioFoto(uri);
      setAvisoPedagio('Sem sinal pra conferir agora — será conferida no envio.');
    } finally {
      setConferindoPedagio(false);
    }
  };

  const enviarPedagio = async () => {
    const valor = Number(pedagioValor.replace(',', '.'));
    if (!pedagioValor.trim() || !Number.isFinite(valor) || valor <= 0) return Alert.alert('Informe o valor do pedágio.');
    if (!pedagioFoto) return Alert.alert('Falta a foto', 'Fotografe o recibo do pedágio -- sem foto não dá pra aprovar.');
    const uuid = fila.novoUuid();
    const agora = fila.agoraIso();
    await fila.enfileirar({ uuid, tipo: 'PEDAGIO', rotaId: rota.id, uri: pedagioFoto, valor, capturadoEm: agora, criadoEm: agora, tentativas: 0 });
    setPedagiosNaFila((l) => [...l, { uuid, valor }]);
    setPedagioValor('');
    setPedagioFoto(null);
    setPedagioAberto(false);
    setAvisoPedagio('');
    setTentativasPedagio(0);
    await carregar();
  };

  // Cancelar um pedágio (errou o valor, foto errada, mandou 2x). Dois casos:
  // ainda na fila de envio -> só tira da fila; já no servidor e PENDENTE ->
  // pede pro servidor cancelar (precisa de sinal; aprovado/rejeitado não dá).
  const cancelarPedagioNaFila = (uuid: string, valor: number) => {
    Alert.alert('Cancelar este pedágio?', `R$ ${valor.toFixed(2).replace('.', ',')} ainda não foi enviado. Ele será descartado.`, [
      { text: 'Voltar', style: 'cancel' },
      { text: 'Cancelar pedágio', style: 'destructive', onPress: async () => {
        await fila.descartarItem(uuid);
        setPedagiosNaFila((l) => l.filter((f) => f.uuid !== uuid));
        await carregar();
      } },
    ]);
  };

  const cancelarPedagioEnviado = (pedagioId: number, valor: number) => {
    Alert.alert('Cancelar este pedágio?', `R$ ${valor.toFixed(2).replace('.', ',')} vai sair da fila de aprovação e não será reembolsado.`, [
      { text: 'Voltar', style: 'cancel' },
      { text: 'Cancelar pedágio', style: 'destructive', onPress: async () => {
        setOcupado(true);
        try {
          const pedagios = await api.cancelarPedagio(rota.id, pedagioId);
          setRota((r) => (r ? { ...r, pedagios } : r));
          await carregar();
        } catch (e) {
          Alert.alert('Não deu', e instanceof api.ErroRede ? 'Sem conexão. Cancelar precisa de sinal -- tente de novo mais tarde.' : (e as Error).message);
          await carregar();
        } finally {
          setOcupado(false);
        }
      } },
    ]);
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

  /** Navegar / Ligar / WhatsApp -- ícone + rótulo numa linha só (o rótulo
   *  "Navegar" quebrava no meio quando os três botões dividiam a largura). */
  const acoesContato = (p: Parada) => (
    <View style={s.acoes}>
      <Pressable style={s.acao} onPress={() => abrirNavegacao(p)} accessibilityRole="button" accessibilityLabel="Navegar até a parada">
        <Ionicons name="navigate" size={17} color={cores.primaria} />
        <Text style={s.acaoTexto}>Navegar</Text>
      </Pressable>
      <Pressable
        style={[s.acao, !p.telefone && s.acaoInativa]}
        disabled={!p.telefone}
        onPress={() => void Linking.openURL(`tel:${p.telefone}`)}
        accessibilityRole="button"
        accessibilityLabel="Ligar para o cliente"
      >
        <Ionicons name="call" size={17} color={cores.primaria} />
        <Text style={s.acaoTexto}>Ligar</Text>
      </Pressable>
      <Pressable
        style={[s.acao, !numeroWhatsapp(p.telefone) && s.acaoInativa]}
        disabled={!numeroWhatsapp(p.telefone)}
        onPress={() => abrirWhatsapp(p)}
        accessibilityRole="button"
        accessibilityLabel="Falar no WhatsApp"
      >
        <Ionicons name="logo-whatsapp" size={17} color={cores.primaria} />
        <Text style={s.acaoTexto}>WhatsApp</Text>
      </Pressable>
    </View>
  );

  /** "Solicitar ajuda para este pedido" (Hugo, 13/09): abre o chat da aba
   *  Ajuda já neste pedido -- o motorista não precisa achá-lo na lista. */
  const botaoAjuda = (p: Parada) => (
    <Pressable
      style={({ pressed }) => [s.ajuda, pressed && { backgroundColor: cores.fundo }]}
      onPress={() => router.push({ pathname: '/ajuda', params: { parada: String(p.id) } })}
      accessibilityRole="button"
      accessibilityLabel="Solicitar ajuda para este pedido"
    >
      <Ionicons name="chatbubble-ellipses" size={17} color={cores.info} />
      <Text style={s.ajudaTexto}>Solicitar ajuda para este pedido</Text>
    </Pressable>
  );

  /** Avisos que valem tanto no cartão da vez quanto na linha aberta. */
  const avisosParada = (p: Parada) => {
    const retorno = rotuloRetorno(p);
    return (
      <>
        {p.nivel_dificuldade && p.nivel_dificuldade >= 3 ? <Text style={[s.avisoParada, { color: cores.alerta }]}>⚠ Entrega demorada — reserve mais tempo aqui.</Text> : null}
        {retorno && ehPendente(p) ? <Text style={[s.avisoParada, { color: cores.alerta, fontWeight: '700' }]}>{retorno}{p.tentativas > 0 ? ` · ${p.tentativas}ª tentativa feita` : ''}</Text> : null}
        {p.motivo_texto ? <Text style={[s.avisoParada, { color: cores.perigo }]}>Motivo: {p.motivo_texto}</Text> : null}
      </>
    );
  };

  /** A parada da vez, em tamanho grande: dá pra ler com o celular no suporte. */
  const cartaoAtual = (p: Parada) => {
    const end = partesEndereco(p.endereco);
    const janela = resumoJanela(p, rota.data_rota, agora);
    const tom = TOM_JANELA[janela.tom];
    return (
      <Cartao estilo={s.cartaoAtual}>
        <View style={s.capa}>
          <View style={s.capaOrdem}><Text style={s.capaOrdemTexto}>{p.ordem}</Text></View>
          <Text style={s.capaRotulo}>{ETAPA_ROTULO[p.situacao] ?? 'Próxima parada'}</Text>
          <Text style={s.capaProgresso}>{feitas} de {ativas.length} feitas</Text>
        </View>
        <View style={s.corpoAtual}>
          <Text style={s.atualNome} numberOfLines={2}>{nomeExibicao(p)}</Text>
          <Text style={s.atualEnd} numberOfLines={2}>{end.rua}{end.bairro ? ` — ${end.bairro}` : ''}</Text>

          <View style={[s.faixa, { backgroundColor: tom.fundo }]}>
            <Ionicons name={janela.tom === 'ausente' ? 'time-outline' : 'time'} size={17} color={tom.icone} />
            <Text style={[s.faixaHora, { color: tom.forte }]}>{janela.texto}</Text>
            {janela.detalhe ? <Text style={[s.faixaDetalhe, { color: tom.suave }]} numberOfLines={1}>{janela.detalhe}</Text> : null}
          </View>

          <View style={[s.dados, !p.volume_caixas && !p.remetente_nome && !p.codigo && { marginTop: 0 }]}>
            {p.volume_caixas ? (
              <View><Text style={s.dadoRotulo}>Volume</Text><Text style={s.dadoValor}>{p.volume_caixas} cx</Text></View>
            ) : null}
            {p.remetente_nome ? (
              <View style={{ flexShrink: 1 }}>
                <Text style={s.dadoRotulo}>Remetente</Text>
                <Text style={s.dadoValor} numberOfLines={1}>{nomeExibicao({ destinatario_nome: p.remetente_nome })}</Text>
              </View>
            ) : null}
            {p.codigo ? (
              <View><Text style={s.dadoRotulo}>Pedido</Text><Text style={s.dadoValor}>{p.codigo}</Text></View>
            ) : null}
          </View>

          {avisosParada(p)}
          {acoesContato(p)}
          <View style={{ marginTop: 12 }}>{controleParada(p)}</View>
          {botaoAjuda(p)}
        </View>
      </Cartao>
    );
  };

  /** Uma linha por parada; o toque abre endereço inteiro, ações e o
   *  controle -- dá pra começar fora de ordem, como antes. */
  const linhaParada = (p: Parada) => {
    const end = partesEndereco(p.endereco);
    const final = !ehPendente(p);
    const hora = horaDe(p.completed_at);
    const expandida = aberta === p.id;
    const direita = final ? hora || situacaoRotulo[p.situacao] : p.janela_fim ? `até ${p.janela_fim}` : '—';
    const controle = controleParada(p);   // dá pra iniciar fora de ordem pela linha aberta
    return (
      <View key={p.id} style={[s.linha, p.situacao === 'CANCELADA' && { opacity: 0.55 }]}>
        <Pressable
          style={s.linhaToque}
          onPress={() => setAberta(expandida ? null : p.id)}
          accessibilityRole="button"
          accessibilityLabel={`${nomeExibicao(p)}, parada ${p.ordem}`}
          accessibilityHint={expandida ? 'Toque para recolher' : 'Toque para ver endereço e ações'}
        >
          <View style={[s.linhaOrdem, final && { backgroundColor: situacaoCor[p.situacao], borderColor: situacaoCor[p.situacao] }]}>
            <Text style={[s.linhaOrdemTexto, final && { color: '#fff' }]}>{p.ordem}</Text>
          </View>
          <View style={{ flex: 1 }}>
            <Text style={[s.linhaNome, final && s.linhaNomeFeita]} numberOfLines={1}>{nomeExibicao(p)}</Text>
            <Text style={s.linhaSub} numberOfLines={1}>
              {[end.bairro, p.volume_caixas ? `${p.volume_caixas} cx` : ''].filter(Boolean).join(' · ') || (p.codigo ?? '')}
            </Text>
          </View>
          <View style={{ alignItems: 'flex-end' }}>
            <Text style={[s.linhaHora, final && { color: situacaoCor[p.situacao], fontWeight: '700' }]}>{direita}</Text>
            {final && hora ? <Text style={s.linhaSituacao}>{situacaoRotulo[p.situacao]}</Text> : null}
          </View>
          <Ionicons name={expandida ? 'chevron-up' : 'chevron-down'} size={16} color={cores.textoSuave} />
        </Pressable>
        {expandida ? (
          <View style={s.linhaAberta}>
            <Text style={s.linhaEndereco}>{end.completo || 'Endereço não informado'}</Text>
            {p.remetente_nome ? <Text style={s.avisoParada}>Remetente: {nomeExibicao({ destinatario_nome: p.remetente_nome })}{p.codigo ? ` · ${p.codigo}` : ''}</Text> : null}
            {avisosParada(p)}
            {ehPendente(p) ? acoesContato(p) : null}
            {controle ? <View style={{ marginTop: 10 }}>{controle}</View> : null}
            {p.situacao !== 'CANCELADA' ? botaoAjuda(p) : null}
          </View>
        ) : null}
      </View>
    );
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

      {rota.editavel && (rota.status === 'EM_ROTA' || rota.status === 'CONCLUIDA') ? (
        <Cartao>
          <Text style={s.secao}>Pedágios</Text>
          <Text style={s.sub}>Pagou pedágio nesta rota? Informe o valor e fotografe o recibo. O reembolso entra no extrato depois de aprovado.</Text>
          {(rota.pedagios ?? []).map((p) => (
            <View key={p.id} style={[s.pedagioLinha, p.status === 'CANCELADO' && { opacity: 0.55 }]}>
              <Text style={[s.pedagioValor, p.status === 'CANCELADO' && { textDecorationLine: 'line-through' }]}>R$ {p.valor_informado.toFixed(2).replace('.', ',')}</Text>
              <Text style={[s.pedagioStatus, { color: PEDAGIO_COR[p.status] }]}>
                {PEDAGIO_ROTULO[p.status]}{p.status === 'APROVADO' && p.valor_aprovado !== null && p.valor_aprovado !== p.valor_informado ? ` (R$ ${p.valor_aprovado.toFixed(2).replace('.', ',')})` : ''}
              </Text>
              {p.status === 'PENDENTE' ? (
                <Pressable onPress={() => cancelarPedagioEnviado(p.id, p.valor_informado)} disabled={ocupado} hitSlop={8} style={s.pedagioCancelar}>
                  <Text style={s.pedagioCancelarTexto}>✕ Cancelar</Text>
                </Pressable>
              ) : null}
              {p.observacao_revisao && p.status !== 'CANCELADO' ? <Text style={s.paradaMeta}>{p.observacao_revisao}</Text> : null}
            </View>
          ))}
          {pedagiosNaFila.filter((f) => !(rota.pedagios ?? []).some((p) => p.uuid === f.uuid)).map((f) => (
            <View key={f.uuid} style={s.pedagioLinha}>
              <Text style={s.pedagioValor}>R$ {f.valor.toFixed(2).replace('.', ',')}</Text>
              <Text style={[s.pedagioStatus, { color: cores.textoSuave }]}>na fila de envio</Text>
              <Pressable onPress={() => cancelarPedagioNaFila(f.uuid, f.valor)} hitSlop={8} style={s.pedagioCancelar}>
                <Text style={s.pedagioCancelarTexto}>✕ Cancelar</Text>
              </Pressable>
            </View>
          ))}
          {pedagioAberto ? (
            <View style={{ marginTop: 12 }}>
              <TextInput style={s.campoCurto} placeholder="Valor do pedágio (R$)" placeholderTextColor="#9CA3AF" keyboardType="decimal-pad" value={pedagioValor} onChangeText={setPedagioValor} />
              {pedagioFoto ? <Image source={{ uri: pedagioFoto }} style={s.fotoPedagio} /> : null}
              {avisoPedagio ? <Text style={[s.sub, { color: cores.alerta, fontWeight: '600' }]}>{avisoPedagio}</Text> : null}
              <Botao
                titulo={conferindoPedagio ? 'Conferindo a foto…' : pedagioFoto ? 'Tirar outra foto' : '📷  Fotografar recibo'}
                tipo={pedagioFoto ? 'secundario' : 'primario'}
                carregando={conferindoPedagio}
                desabilitado={conferindoPedagio}
                onPress={() => void fotografarPedagio()}
                estilo={{ marginTop: 8 }}
              />
              <View style={{ flexDirection: 'row', gap: 8, marginTop: 8 }}>
                <Botao titulo="Cancelar" tipo="secundario" onPress={() => { setPedagioAberto(false); setPedagioFoto(null); setAvisoPedagio(''); }} estilo={{ flex: 1 }} />
                <Botao titulo="Enviar pedágio" onPress={() => void enviarPedagio()} estilo={{ flex: 1 }} />
              </View>
            </View>
          ) : (
            <Botao titulo="+ Adicionar pedágio" tipo="secundario" onPress={() => setPedagioAberto(true)} estilo={{ marginTop: 10, minHeight: 46, paddingVertical: 10 }} />
          )}
        </Cartao>
      ) : null}

      {atual ? cartaoAtual(atual) : null}

      {depoisDesta.length > 0 ? (
        <>
          <Text style={s.tituloSecao}>{atual ? 'Depois desta' : 'Paradas'}</Text>
          {depoisDesta.map((p) => linhaParada(p))}
        </>
      ) : null}

      {jaFeitas.length > 0 ? (
        <>
          <Text style={s.tituloSecao}>Já feitas</Text>
          {jaFeitas.map((p) => linhaParada(p))}
        </>
      ) : null}
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
  campoCurto: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 10, padding: 12, color: cores.texto, fontSize: 18, fontWeight: '700' },
  secao: { fontWeight: '800', color: cores.texto, fontSize: 16 },
  pedagioLinha: { flexDirection: 'row', flexWrap: 'wrap', alignItems: 'center', gap: 8, paddingVertical: 8, borderTopWidth: StyleSheet.hairlineWidth, borderColor: cores.borda, marginTop: 8 },
  pedagioValor: { fontWeight: '800', color: cores.texto, fontSize: 15 },
  pedagioStatus: { fontWeight: '600', fontSize: 13 },
  pedagioCancelar: { marginLeft: 'auto', paddingVertical: 6, paddingHorizontal: 10, borderRadius: 8, borderWidth: 1, borderColor: cores.perigo },
  pedagioCancelarTexto: { color: cores.perigo, fontWeight: '700', fontSize: 12 },
  fotoPedagio: { width: '100%', height: 180, borderRadius: 10, marginTop: 8, backgroundColor: cores.borda },
  paradaMeta: { color: cores.textoSuave, fontSize: 12, marginTop: 6 },

  // --- cartão da parada da vez ---
  cartaoAtual: { padding: 0, overflow: 'hidden', borderWidth: 2, borderColor: cores.acento },
  capa: { flexDirection: 'row', alignItems: 'center', gap: 9, backgroundColor: cores.primaria, paddingHorizontal: 14, paddingVertical: 10 },
  capaOrdem: { width: 26, height: 26, borderRadius: 13, backgroundColor: '#fff', alignItems: 'center', justifyContent: 'center' },
  capaOrdemTexto: { color: cores.primaria, fontWeight: '800', fontSize: 13 },
  capaRotulo: { color: cores.textoSobrePrimaria, fontWeight: '700', fontSize: 12, letterSpacing: 0.6, textTransform: 'uppercase' },
  capaProgresso: { color: cores.textoSuaveSobrePrimaria, fontSize: 12, marginLeft: 'auto' },
  corpoAtual: { padding: 16 },
  atualNome: { fontSize: 22, fontWeight: '800', color: cores.texto, letterSpacing: -0.3, lineHeight: 26 },
  atualEnd: { color: cores.textoSuave, fontSize: 14, marginTop: 4 },
  faixa: { flexDirection: 'row', alignItems: 'center', gap: 9, borderRadius: 10, paddingHorizontal: 12, paddingVertical: 9, marginTop: 12 },
  faixaHora: { fontWeight: '800', fontSize: 15 },
  faixaDetalhe: { marginLeft: 'auto', fontWeight: '600', fontSize: 12.5, flexShrink: 1 },
  dados: { flexDirection: 'row', flexWrap: 'wrap', gap: 16, marginTop: 12 },
  dadoRotulo: { fontSize: 10.5, color: cores.textoSuave, fontWeight: '700', textTransform: 'uppercase', letterSpacing: 0.7 },
  dadoValor: { fontSize: 14, fontWeight: '700', color: cores.texto },
  avisoParada: { color: cores.textoSuave, fontSize: 12.5, marginTop: 8 },

  // --- ações de contato ---
  acoes: { flexDirection: 'row', gap: 8, marginTop: 14 },
  acao: { flex: 1, height: 50, borderRadius: 12, backgroundColor: cores.fundo, flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 6 },
  acaoTexto: { fontSize: 13, fontWeight: '700', color: cores.texto },
  acaoInativa: { opacity: 0.4 },
  ajuda: { flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 8, marginTop: 10, minHeight: 46, borderRadius: 12, borderWidth: 1, borderColor: cores.info },
  ajudaTexto: { fontSize: 14, fontWeight: '700', color: cores.info },

  // --- lista compacta ---
  tituloSecao: { fontSize: 11, fontWeight: '800', color: cores.textoSuave, textTransform: 'uppercase', letterSpacing: 0.9, marginBottom: 8, marginTop: 4 },
  linha: { backgroundColor: cores.cartao, borderWidth: 1, borderColor: cores.borda, borderRadius: 12, marginBottom: 8 },
  linhaToque: { flexDirection: 'row', alignItems: 'center', gap: 10, paddingHorizontal: 12, paddingVertical: 11, minHeight: 58 },
  linhaOrdem: { width: 26, height: 26, borderRadius: 13, borderWidth: 1.5, borderColor: cores.borda, backgroundColor: '#fff', alignItems: 'center', justifyContent: 'center' },
  linhaOrdemTexto: { fontSize: 12, fontWeight: '800', color: cores.textoSuave },
  linhaNome: { fontSize: 14, fontWeight: '700', color: cores.texto },
  linhaNomeFeita: { color: cores.textoSuave, textDecorationLine: 'line-through' },
  linhaSub: { fontSize: 12, color: cores.textoSuave, marginTop: 1 },
  linhaHora: { fontSize: 12.5, color: cores.textoSuave },
  linhaSituacao: { fontSize: 10.5, color: cores.textoSuave, textTransform: 'uppercase', letterSpacing: 0.5, fontWeight: '700' },
  linhaAberta: { paddingHorizontal: 12, paddingBottom: 12, borderTopWidth: StyleSheet.hairlineWidth, borderColor: cores.borda, paddingTop: 10 },
  linhaEndereco: { fontSize: 13, color: cores.texto },
});
