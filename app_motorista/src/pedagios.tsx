// Lançamento de pedágio (Hugo, 11/09 -> 14/09): reembolso à parte, valor +
// foto do recibo, um por recibo, enviado pela fila offline e aprovado no
// painel. Fica na aba Financeiro, com a ROTA escolhida no próprio cartão --
// antes era um cartão dentro da tela da rota.
import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Image, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import * as ImagePicker from 'expo-image-picker';
import * as api from './api';
import * as fila from './fila';
import { Botao, Cartao } from './componentes';
import { cores, formatarData, formatarReal } from './tema';
import type { Rota } from './tipos';

const PEDAGIO_ROTULO: Record<string, string> = { PENDENTE: 'aguardando aprovação', APROVADO: 'aprovado', REJEITADO: 'rejeitado', CANCELADO: 'cancelado por você' };
const PEDAGIO_COR: Record<string, string> = { PENDENTE: cores.alerta, APROVADO: cores.sucesso, REJEITADO: cores.perigo, CANCELADO: cores.textoSuave };

/** Rotas em que o servidor aceita pedágio: do app, em andamento ou concluída. */
export const aceitaPedagio = (r: Rota) => r.editavel && (r.status === 'EM_ROTA' || r.status === 'CONCLUIDA');

const real = (v: number) => `R$ ${v.toFixed(2).replace('.', ',')}`;

export function LancarPedagio({ rotas, rotaInicial, aoMudar }: { rotas: Rota[]; rotaInicial?: number | null; aoMudar: () => void | Promise<void> }) {
  const [rotaId, setRotaId] = useState<number | null>(null);
  const [aberto, setAberto] = useState(false);
  const [valor, setValor] = useState('');
  const [foto, setFoto] = useState<string | null>(null);
  const [naFila, setNaFila] = useState<{ uuid: string; rotaId: number; valor: number }[]>([]);
  const [ocupado, setOcupado] = useState(false);
  // Conferência automática do recibo (Hugo, 12/09) -- só roda se o
  // servidor disser que está ligada (carregada junto do checklist).
  const [validacaoAtiva, setValidacaoAtiva] = useState(false);
  const [conferindo, setConferindo] = useState(false);
  const [tentativas, setTentativas] = useState(0);
  const [aviso, setAviso] = useState('');

  // Rota padrão: a pedida (vinda da tela da rota), senão a em andamento,
  // senão a mais recente.
  useEffect(() => {
    if (rotaInicial && rotas.some((r) => r.id === rotaInicial)) return setRotaId(rotaInicial);
    setRotaId((atual) => (atual && rotas.some((r) => r.id === atual) ? atual
      : (rotas.find((r) => r.status === 'EM_ROTA') ?? [...rotas].sort((a, b) => b.data_rota.localeCompare(a.data_rota))[0])?.id ?? null));
  }, [rotas, rotaInicial]);

  const lerFila = useCallback(async () => setNaFila(await fila.pedagiosNaFila()), []);
  useEffect(() => {
    void lerFila();
    return fila.aoMudar(() => void lerFila());
  }, [lerFila]);

  useEffect(() => {
    api.checklist().then((c) => setValidacaoAtiva(c.validacao_fotos?.ativo === true)).catch(() => setValidacaoAtiva(false));
  }, []);

  const rota = rotas.find((r) => r.id === rotaId) ?? null;

  const limpar = () => { setAberto(false); setValor(''); setFoto(null); setAviso(''); setTentativas(0); };

  const trocarRota = (id: number) => {
    if (id === rotaId) return;
    setRotaId(id);
    limpar();
  };

  const fotografar = async () => {
    if (!rota) return;
    const perm = await ImagePicker.requestCameraPermissionsAsync();
    if (!perm.granted) return Alert.alert('Câmera', 'Permita o uso da câmera pra fotografar o recibo.');
    const r = await ImagePicker.launchCameraAsync({ quality: 0.6, allowsEditing: false, exif: false });
    if (r.canceled || !r.assets[0]) return;
    const uri = r.assets[0].uri;
    if (!validacaoAtiva) return setFoto(uri);

    const tentativa = tentativas + 1;
    setTentativas(tentativa);
    setConferindo(true);
    try {
      const v = Number(valor.replace(',', '.'));
      const res = await api.validarFoto(uri, { tipo: 'PEDAGIO', rotaId: rota.id, tentativa, ...(Number.isFinite(v) && v > 0 ? { valor: v } : {}) });
      if (res.resultado !== 'REPROVADO') {
        setFoto(uri);
        setAviso(res.avisos?.includes('VALOR_DIVERGE')
          ? `O recibo mostra ${real(res.valor_lido ?? 0)} — confira o valor digitado.`
          : res.resultado === 'NAO_VERIFICADO' ? 'Foto não conferida — vai pra revisão.' : '');
        return;
      }
      const motivo = res.motivo ?? 'A foto não ficou boa.';
      if (!res.pode_seguir) return Alert.alert('Foto não serve', `${motivo}\n\nTire outra foto.`);
      Alert.alert('Ainda não ficou boa', `${motivo}\n\nVocê pode tentar de novo ou seguir assim — o pedágio vai pra conferência manual.`, [
        { text: 'Tirar de novo', style: 'cancel' },
        { text: 'Não consigo melhorar', onPress: () => { setFoto(uri); setAviso('Segue pra conferência manual.'); } },
      ]);
    } catch {
      setFoto(uri);
      setAviso('Sem sinal pra conferir agora — será conferida no envio.');
    } finally {
      setConferindo(false);
    }
  };

  const enviar = async () => {
    if (!rota) return;
    const v = Number(valor.replace(',', '.'));
    if (!valor.trim() || !Number.isFinite(v) || v <= 0) return Alert.alert('Informe o valor do pedágio.');
    if (!foto) return Alert.alert('Falta a foto', 'Fotografe o recibo do pedágio -- sem foto não dá pra aprovar.');
    const agora = fila.agoraIso();
    await fila.enfileirar({ uuid: fila.novoUuid(), tipo: 'PEDAGIO', rotaId: rota.id, uri: foto, valor: v, capturadoEm: agora, criadoEm: agora, tentativas: 0 });
    limpar();
    await lerFila();
    await aoMudar();
  };

  // Ainda na fila -> só tira da fila; já no servidor e PENDENTE -> pede pro
  // servidor cancelar (precisa de sinal; aprovado/rejeitado não dá).
  const cancelarNaFila = (uuid: string, v: number) =>
    Alert.alert('Cancelar este pedágio?', `${real(v)} ainda não foi enviado. Ele será descartado.`, [
      { text: 'Voltar', style: 'cancel' },
      { text: 'Cancelar pedágio', style: 'destructive', onPress: async () => { await fila.descartarItem(uuid); await lerFila(); } },
    ]);

  const cancelarEnviado = (pedagioId: number, v: number) => {
    if (!rota) return;
    Alert.alert('Cancelar este pedágio?', `${real(v)} vai sair da fila de aprovação e não será reembolsado.`, [
      { text: 'Voltar', style: 'cancel' },
      { text: 'Cancelar pedágio', style: 'destructive', onPress: async () => {
        setOcupado(true);
        try {
          await api.cancelarPedagio(rota.id, pedagioId);
        } catch (e) {
          Alert.alert('Não deu', e instanceof api.ErroRede ? 'Sem conexão. Cancelar precisa de sinal -- tente de novo mais tarde.' : (e as Error).message);
        } finally {
          setOcupado(false);
          await aoMudar();
        }
      } },
    ]);
  };

  const enviados = rota?.pedagios ?? [];
  const pendentesDaRota = naFila.filter((f) => f.rotaId === rota?.id && !enviados.some((p) => p.uuid === f.uuid));

  return (
    <Cartao>
      <Text style={s.titulo}>Pedágios</Text>
      <Text style={s.sub}>Pagou pedágio? Escolha a rota, informe o valor e fotografe o recibo. O reembolso entra no extrato depois de aprovado.</Text>

      {rotas.length === 0 ? (
        <Text style={s.vazio}>Nenhuma rota em andamento ou concluída nos últimos dias pra lançar pedágio.</Text>
      ) : (
        <>
          <Text style={s.rotulo}>Rota</Text>
          <ScrollView horizontal showsHorizontalScrollIndicator={false} contentContainerStyle={{ gap: 8 }}>
            {rotas.map((r) => {
              const ativa = r.id === rotaId;
              return (
                <Pressable key={r.id} onPress={() => trocarRota(r.id)} style={[s.chip, ativa && s.chipAtivo]}
                  accessibilityRole="button" accessibilityState={{ selected: ativa }}>
                  <Text style={[s.chipData, ativa && { color: '#fff' }]}>{formatarData(r.data_rota)}{r.status === 'EM_ROTA' ? ' · em andamento' : ''}</Text>
                  <Text style={[s.chipNome, ativa && { color: '#fff' }]} numberOfLines={1}>{r.nome ?? `Rota #${r.id}`}</Text>
                </Pressable>
              );
            })}
          </ScrollView>

          {enviados.map((p) => (
            <View key={p.id} style={[s.linha, p.status === 'CANCELADO' && { opacity: 0.55 }]}>
              <Text style={[s.valor, p.status === 'CANCELADO' && { textDecorationLine: 'line-through' }]}>{real(p.valor_informado)}</Text>
              <Text style={[s.status, { color: PEDAGIO_COR[p.status] }]}>
                {PEDAGIO_ROTULO[p.status]}{p.status === 'APROVADO' && p.valor_aprovado !== null && p.valor_aprovado !== p.valor_informado ? ` (${formatarReal(p.valor_aprovado)})` : ''}
              </Text>
              {p.status === 'PENDENTE' ? (
                <Pressable onPress={() => cancelarEnviado(p.id, p.valor_informado)} disabled={ocupado} hitSlop={8} style={s.cancelar}>
                  <Text style={s.cancelarTexto}>✕ Cancelar</Text>
                </Pressable>
              ) : null}
              {p.observacao_revisao && p.status !== 'CANCELADO' ? <Text style={s.obs}>{p.observacao_revisao}</Text> : null}
            </View>
          ))}
          {pendentesDaRota.map((f) => (
            <View key={f.uuid} style={s.linha}>
              <Text style={s.valor}>{real(f.valor)}</Text>
              <Text style={[s.status, { color: cores.textoSuave }]}>na fila de envio</Text>
              <Pressable onPress={() => cancelarNaFila(f.uuid, f.valor)} hitSlop={8} style={s.cancelar}>
                <Text style={s.cancelarTexto}>✕ Cancelar</Text>
              </Pressable>
            </View>
          ))}

          {aberto ? (
            <View style={{ marginTop: 12 }}>
              <TextInput style={s.campo} placeholder="Valor do pedágio (R$)" placeholderTextColor="#9CA3AF" keyboardType="decimal-pad" value={valor} onChangeText={setValor} />
              {foto ? <Image source={{ uri: foto }} style={s.foto} /> : null}
              {aviso ? <Text style={[s.sub, { color: cores.alerta, fontWeight: '600' }]}>{aviso}</Text> : null}
              <Botao
                titulo={conferindo ? 'Conferindo a foto…' : foto ? 'Tirar outra foto' : '📷  Fotografar recibo'}
                tipo={foto ? 'secundario' : 'primario'}
                carregando={conferindo}
                desabilitado={conferindo}
                onPress={() => void fotografar()}
                estilo={{ marginTop: 8 }}
              />
              <View style={{ flexDirection: 'row', gap: 8, marginTop: 8 }}>
                <Botao titulo="Cancelar" tipo="secundario" onPress={limpar} estilo={{ flex: 1 }} />
                <Botao titulo="Enviar pedágio" onPress={() => void enviar()} estilo={{ flex: 1 }} />
              </View>
            </View>
          ) : (
            <Botao titulo="+ Adicionar pedágio" tipo="secundario" onPress={() => setAberto(true)} desabilitado={!rota}
              estilo={{ marginTop: 12, minHeight: 46, paddingVertical: 10 }} />
          )}
        </>
      )}
    </Cartao>
  );
}

const s = StyleSheet.create({
  titulo: { fontWeight: '800', color: cores.texto, fontSize: 16 },
  sub: { color: cores.textoSuave, marginTop: 4 },
  vazio: { color: cores.textoSuave, marginTop: 10, fontStyle: 'italic' },
  rotulo: { fontSize: 10.5, color: cores.textoSuave, fontWeight: '700', textTransform: 'uppercase', letterSpacing: 0.7, marginTop: 12, marginBottom: 6 },
  chip: { borderWidth: 1, borderColor: cores.borda, backgroundColor: cores.fundo, borderRadius: 12, paddingHorizontal: 12, paddingVertical: 8, maxWidth: 220 },
  chipAtivo: { backgroundColor: cores.primaria, borderColor: cores.primaria },
  chipData: { fontSize: 12, fontWeight: '700', color: cores.texto },
  chipNome: { fontSize: 12, color: cores.textoSuave, marginTop: 1 },
  linha: { flexDirection: 'row', flexWrap: 'wrap', alignItems: 'center', gap: 8, paddingVertical: 8, borderTopWidth: StyleSheet.hairlineWidth, borderColor: cores.borda, marginTop: 8 },
  valor: { fontWeight: '800', color: cores.texto, fontSize: 15 },
  status: { fontWeight: '600', fontSize: 13 },
  cancelar: { marginLeft: 'auto', paddingVertical: 6, paddingHorizontal: 10, borderRadius: 8, borderWidth: 1, borderColor: cores.perigo },
  cancelarTexto: { color: cores.perigo, fontWeight: '700', fontSize: 12 },
  obs: { color: cores.textoSuave, fontSize: 12, marginTop: 6 },
  campo: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 10, padding: 12, color: cores.texto, fontSize: 18, fontWeight: '700' },
  foto: { width: '100%', height: 180, borderRadius: 10, marginTop: 8, backgroundColor: cores.borda },
});
