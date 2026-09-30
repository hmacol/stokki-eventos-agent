// Lançamento de pedágio e despesas (Hugo, 11/09 -> 14/09): reembolso à
// parte, valor + foto do recibo, um por recibo, enviado pela fila offline e
// aprovado no painel. Fica na aba Financeiro, com a ROTA escolhida no próprio
// cartão -- antes era um cartão dentro da tela da rota.
//
// Despesas adicionais (14/09): o tipo sai de um menu -- Pedágio,
// Estacionamento, Descarga ou Outros. Outros exige descrição; Estacionamento
// e Descarga exigem o pedido de referência (parada da rota escolhida).
import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Image, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import * as ImagePicker from 'expo-image-picker';
import * as api from './api';
import * as fila from './fila';
import { Botao, Cartao } from './componentes';
import { cores, formatarData, formatarReal } from './tema';
import { nomeExibicao } from './textos';
import type { Parada, Rota, TipoDespesa } from './tipos';

const STATUS_ROTULO: Record<string, string> = { PENDENTE: 'aguardando aprovação', APROVADO: 'aprovado', REJEITADO: 'rejeitado', CANCELADO: 'cancelado por você' };
const STATUS_COR: Record<string, string> = { PENDENTE: cores.alerta, APROVADO: cores.sucesso, REJEITADO: cores.perigo, CANCELADO: cores.textoSuave };

export const TIPOS_DESPESA: { valor: TipoDespesa; rotulo: string }[] = [
  { valor: 'PEDAGIO', rotulo: 'Pedágio' },
  { valor: 'ESTACIONAMENTO', rotulo: 'Estacionamento' },
  { valor: 'DESCARGA', rotulo: 'Descarga' },
  { valor: 'OUTROS', rotulo: 'Outros' },
];
const rotuloTipo = (t?: TipoDespesa | null) => TIPOS_DESPESA.find((x) => x.valor === (t ?? 'PEDAGIO'))?.rotulo ?? 'Pedágio';
const exigePedido = (t: TipoDespesa | null) => t === 'ESTACIONAMENTO' || t === 'DESCARGA';

/** Rotas em que o servidor aceita pedágio/despesa: do app, em andamento ou concluída. */
export const aceitaPedagio = (r: Rota) => r.editavel && (r.status === 'EM_ROTA' || r.status === 'CONCLUIDA');

const real = (v: number) => `R$ ${v.toFixed(2).replace('.', ',')}`;
const codigoLimpo = (c?: string | null) => (c ?? '').replace(/^#/, '');
const rotuloParada = (p: Parada) => `${p.ordem} · ${nomeExibicao(p)}${p.codigo ? ` · ${codigoLimpo(p.codigo)}` : ''}`;

/** Menu suspenso simples: o campo mostra a escolha; o toque abre a lista logo abaixo. */
function Seletor<T extends string | number>({ rotulo, placeholder, valor, opcoes, aoEscolher, mostrarSub = false, estilo }: {
  rotulo: string; placeholder: string; valor: T | null;
  opcoes: { valor: T; rotulo: string; sub?: string }[]; aoEscolher: (v: T) => void;
  mostrarSub?: boolean; estilo?: object;
}) {
  const [aberto, setAberto] = useState(false);
  const atual = opcoes.find((o) => o.valor === valor);
  return (
    <View style={[{ marginTop: 10 }, estilo]}>
      <Text style={s.rotulo}>{rotulo}</Text>
      <Pressable onPress={() => setAberto((a) => !a)} style={[s.seletor, aberto && { borderColor: cores.primaria }]}
        accessibilityRole="button" accessibilityLabel={rotulo} accessibilityState={{ expanded: aberto }}>
        <View style={{ flex: 1, paddingVertical: 8 }}>
          <Text style={[s.seletorTexto, !atual && { color: '#9CA3AF', fontWeight: '500' }]} numberOfLines={1}>{atual?.rotulo ?? placeholder}</Text>
          {atual?.sub && mostrarSub ? <Text style={s.opcaoSub} numberOfLines={1}>{atual.sub}</Text> : null}
        </View>
        <Ionicons name={aberto ? 'chevron-up' : 'chevron-down'} size={18} color={cores.textoSuave} />
      </Pressable>
      {aberto ? (
        <View style={s.lista}>
          {opcoes.map((o) => (
            <Pressable key={String(o.valor)} onPress={() => { aoEscolher(o.valor); setAberto(false); }}
              style={({ pressed }) => [s.opcao, o.valor === valor && s.opcaoAtiva, pressed && { backgroundColor: cores.fundo }]}>
              <View style={{ flex: 1 }}>
                <Text style={[s.opcaoTexto, o.valor === valor && { color: cores.primaria }]} numberOfLines={1}>{o.rotulo}</Text>
                {o.sub ? <Text style={s.opcaoSub} numberOfLines={1}>{o.sub}</Text> : null}
              </View>
              {o.valor === valor ? <Ionicons name="checkmark" size={18} color={cores.primaria} /> : null}
            </Pressable>
          ))}
        </View>
      ) : null}
    </View>
  );
}

export function LancarPedagio({ rotas, rotaInicial, aoMudar }: { rotas: Rota[]; rotaInicial?: number | null; aoMudar: () => void | Promise<void> }) {
  const [rotaId, setRotaId] = useState<number | null>(null);
  const [aberto, setAberto] = useState(false);
  const [tipo, setTipo] = useState<TipoDespesa | null>(null);
  const [descricao, setDescricao] = useState('');
  const [paradaId, setParadaId] = useState<number | null>(null);
  const [valor, setValor] = useState('');
  const [foto, setFoto] = useState<string | null>(null);
  const [naFila, setNaFila] = useState<Awaited<ReturnType<typeof fila.pedagiosNaFila>>>([]);
  const [ocupado, setOcupado] = useState(false);
  // Conferência automática do recibo (Hugo, 12/09) -- só roda se o
  // servidor disser que está ligada, e só sabe ler recibo de PEDÁGIO.
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
  const paradasDaRota = (rota?.paradas ?? []).filter((p) => p.situacao !== 'CANCELADA');

  const limpar = () => {
    setAberto(false); setTipo(null); setDescricao(''); setParadaId(null);
    setValor(''); setFoto(null); setAviso(''); setTentativas(0);
  };

  const trocarRota = (id: number) => {
    if (id === rotaId) return;
    setRotaId(id);
    limpar();
  };

  const trocarTipo = (t: TipoDespesa) => {
    setTipo(t);
    if (!exigePedido(t)) setParadaId(null);
    if (t !== 'OUTROS') setDescricao('');
    // A foto conferida como pedágio não vale pra outro tipo e vice-versa.
    setAviso('');
  };

  const fotografar = async () => {
    if (!rota) return;
    const perm = await ImagePicker.requestCameraPermissionsAsync();
    if (!perm.granted) return Alert.alert('Câmera', 'Permita o uso da câmera pra fotografar o recibo.');
    const r = await ImagePicker.launchCameraAsync({ quality: 0.6, allowsEditing: false, exif: false });
    if (r.canceled || !r.assets[0]) return;
    const uri = r.assets[0].uri;
    if (!validacaoAtiva || tipo !== 'PEDAGIO') return setFoto(uri);

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
      Alert.alert('Ainda não ficou boa', `${motivo}\n\nVocê pode tentar de novo ou seguir assim — vai pra conferência manual.`, [
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
    if (!tipo) return Alert.alert('Escolha o tipo de despesa.');
    if (tipo === 'OUTROS' && !descricao.trim()) return Alert.alert('Falta a descrição', 'Descreva a despesa (ex.: balsa, lavagem).');
    if (exigePedido(tipo) && !paradaId) return Alert.alert('Falta o pedido', `${rotuloTipo(tipo)} precisa do pedido de referência.`);
    const v = Number(valor.replace(',', '.'));
    if (!valor.trim() || !Number.isFinite(v) || v <= 0) return Alert.alert('Informe o valor.');
    if (!foto) return Alert.alert('Falta a foto', 'Fotografe o recibo -- sem foto não dá pra aprovar.');
    const agora = fila.agoraIso();
    await fila.enfileirar({
      uuid: fila.novoUuid(), tipo: 'PEDAGIO', rotaId: rota.id, uri: foto, valor: v, capturadoEm: agora, criadoEm: agora, tentativas: 0,
      tipoDespesa: tipo, descricao: tipo === 'OUTROS' ? descricao.trim() : null, paradaId: exigePedido(tipo) ? paradaId : null,
    });
    limpar();
    await lerFila();
    await aoMudar();
  };

  // Ainda na fila -> só tira da fila; já no servidor e PENDENTE -> pede pro
  // servidor cancelar (precisa de sinal; aprovado/rejeitado não dá).
  const cancelarNaFila = (uuid: string, v: number) =>
    Alert.alert('Cancelar este lançamento?', `${real(v)} ainda não foi enviado. Ele será descartado.`, [
      { text: 'Voltar', style: 'cancel' },
      { text: 'Cancelar lançamento', style: 'destructive', onPress: async () => { await fila.descartarItem(uuid); await lerFila(); } },
    ]);

  const cancelarEnviado = (pedagioId: number, v: number) => {
    if (!rota) return;
    Alert.alert('Cancelar este lançamento?', `${real(v)} vai sair da fila de aprovação e não será reembolsado.`, [
      { text: 'Voltar', style: 'cancel' },
      { text: 'Cancelar lançamento', style: 'destructive', onPress: async () => {
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
  const detalheParada = (id?: number | null) => {
    const p = id ? paradasDaRota.find((x) => x.id === id) : null;
    return p ? `Pedido ${codigoLimpo(p.codigo)} · ${nomeExibicao(p)}` : null;
  };

  return (
    <Cartao>
      <Text style={s.titulo}>Pedágios e despesas</Text>
      <Text style={s.sub}>Pagou pedágio, estacionamento, descarga ou outra despesa? Escolha a rota, o tipo, informe o valor e fotografe o recibo. O reembolso entra no extrato depois de aprovado.</Text>

      {rotas.length === 0 ? (
        <Text style={s.vazio}>Nenhuma rota em andamento ou concluída nos últimos dias pra lançar despesa.</Text>
      ) : (
        <>
          {/* Rota num menu suspenso, igual ao tipo de despesa (Hugo, 14/09). */}
          <Seletor<number>
            rotulo="Rota"
            placeholder="Escolha a rota"
            valor={rotaId}
            opcoes={rotas.map((r) => ({
              valor: r.id,
              rotulo: `${formatarData(r.data_rota)} · ${r.nome ?? `Rota #${r.id}`}`,
              sub: [r.status === 'EM_ROTA' ? 'em andamento' : 'concluída', `${r.paradas.filter((p) => p.situacao !== 'CANCELADA').length} pedidos`].join(' · '),
            }))}
            aoEscolher={trocarRota}
            mostrarSub
            estilo={{ marginTop: 12 }}
          />

          {enviados.map((p) => {
            const extra = [p.pedido_codigo ? `Pedido ${codigoLimpo(p.pedido_codigo)}${p.pedido_nome ? ` · ${p.pedido_nome}` : ''}` : null, p.descricao].filter(Boolean).join(' · ');
            return (
              <View key={p.id} style={[s.linha, p.status === 'CANCELADO' && { opacity: 0.55 }]}>
                <Text style={s.tipo}>{p.tipo_rotulo ?? rotuloTipo(p.tipo)}</Text>
                <Text style={[s.valor, p.status === 'CANCELADO' && { textDecorationLine: 'line-through' }]}>{real(p.valor_informado)}</Text>
                <Text style={[s.status, { color: STATUS_COR[p.status] }]}>
                  {STATUS_ROTULO[p.status]}{p.status === 'APROVADO' && p.valor_aprovado !== null && p.valor_aprovado !== p.valor_informado ? ` (${formatarReal(p.valor_aprovado)})` : ''}
                </Text>
                {p.status === 'PENDENTE' ? (
                  <Pressable onPress={() => cancelarEnviado(p.id, p.valor_informado)} disabled={ocupado} hitSlop={8} style={s.cancelar}>
                    <Text style={s.cancelarTexto}>✕ Cancelar</Text>
                  </Pressable>
                ) : null}
                {extra ? <Text style={s.obs}>{extra}</Text> : null}
                {p.observacao_revisao && p.status !== 'CANCELADO' ? <Text style={s.obs}>{p.observacao_revisao}</Text> : null}
              </View>
            );
          })}
          {pendentesDaRota.map((f) => {
            const extra = [detalheParada(f.paradaId), f.descricao].filter(Boolean).join(' · ');
            return (
              <View key={f.uuid} style={s.linha}>
                <Text style={s.tipo}>{rotuloTipo(f.tipoDespesa)}</Text>
                <Text style={s.valor}>{real(f.valor)}</Text>
                <Text style={[s.status, { color: cores.textoSuave }]}>na fila de envio</Text>
                <Pressable onPress={() => cancelarNaFila(f.uuid, f.valor)} hitSlop={8} style={s.cancelar}>
                  <Text style={s.cancelarTexto}>✕ Cancelar</Text>
                </Pressable>
                {extra ? <Text style={s.obs}>{extra}</Text> : null}
              </View>
            );
          })}

          {aberto ? (
            <View style={{ marginTop: 4 }}>
              <Seletor<TipoDespesa>
                rotulo="Tipo de despesa"
                placeholder="Escolha o tipo"
                valor={tipo}
                opcoes={TIPOS_DESPESA.map((t) => ({ valor: t.valor, rotulo: t.rotulo }))}
                aoEscolher={trocarTipo}
              />
              {tipo === 'OUTROS' ? (
                <View style={{ marginTop: 10 }}>
                  <Text style={s.rotulo}>Descrição</Text>
                  <TextInput style={s.campoTexto} placeholder="Ex.: balsa, lavagem do baú" placeholderTextColor="#9CA3AF"
                    value={descricao} onChangeText={setDescricao} maxLength={200} />
                </View>
              ) : null}
              {exigePedido(tipo) ? (
                paradasDaRota.length ? (
                  <Seletor<number>
                    rotulo="Pedido de referência"
                    placeholder="Escolha o pedido"
                    valor={paradaId}
                    opcoes={paradasDaRota.map((p) => ({ valor: p.id, rotulo: rotuloParada(p), sub: p.endereco ?? undefined }))}
                    aoEscolher={setParadaId}
                  />
                ) : <Text style={[s.vazio, { color: cores.perigo }]}>Esta rota não tem pedidos pra referenciar.</Text>
              ) : null}
              {tipo ? (
                <>
                  <Text style={[s.rotulo, { marginTop: 10 }]}>Valor</Text>
                  <TextInput style={s.campo} placeholder="R$ 0,00" placeholderTextColor="#9CA3AF" keyboardType="decimal-pad" value={valor} onChangeText={setValor} />
                  {foto ? <Image source={{ uri: foto }} style={s.foto} /> : null}
                  {aviso ? <Text style={[s.sub, { color: cores.alerta, fontWeight: '600' }]}>{aviso}</Text> : null}
                  <Botao
                    titulo={conferindo ? 'Conferindo a foto…' : foto ? 'Tirar outra foto' : '📷  Fotografar recibo'}
                    tipo={foto ? 'secundario' : 'primario'}
                    carregando={conferindo}
                    desabilitado={conferindo}
                    onPress={() => void fotografar()}
                    estilo={{ marginTop: 10 }}
                  />
                </>
              ) : null}
              <View style={{ flexDirection: 'row', gap: 8, marginTop: 8 }}>
                <Botao titulo="Cancelar" tipo="secundario" onPress={limpar} estilo={{ flex: 1 }} />
                <Botao titulo="Enviar" onPress={() => void enviar()} desabilitado={!tipo} estilo={{ flex: 1 }} />
              </View>
            </View>
          ) : (
            <Botao titulo="+ Lançar pedágio ou despesa" tipo="secundario" onPress={() => setAberto(true)} desabilitado={!rota}
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
  rotulo: { fontSize: 10.5, color: cores.textoSuave, fontWeight: '700', textTransform: 'uppercase', letterSpacing: 0.7, marginBottom: 6 },
  seletor: { flexDirection: 'row', alignItems: 'center', gap: 8, backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 10, paddingHorizontal: 12, minHeight: 48 },
  seletorTexto: { flex: 1, color: cores.texto, fontSize: 15, fontWeight: '600' },
  lista: { marginTop: 4, borderWidth: 1, borderColor: cores.borda, borderRadius: 10, backgroundColor: '#fff', overflow: 'hidden' },
  opcao: { flexDirection: 'row', alignItems: 'center', gap: 8, paddingHorizontal: 12, paddingVertical: 11, borderTopWidth: StyleSheet.hairlineWidth, borderColor: cores.borda, minHeight: 46 },
  opcaoAtiva: { backgroundColor: '#F3F6FA' },
  opcaoTexto: { color: cores.texto, fontSize: 14.5, fontWeight: '600' },
  opcaoSub: { color: cores.textoSuave, fontSize: 12, marginTop: 1 },
  linha: { flexDirection: 'row', flexWrap: 'wrap', alignItems: 'center', gap: 8, paddingVertical: 8, borderTopWidth: StyleSheet.hairlineWidth, borderColor: cores.borda, marginTop: 8 },
  tipo: { fontSize: 11, fontWeight: '800', color: cores.primaria, backgroundColor: cores.fundo, borderRadius: 999, paddingHorizontal: 8, paddingVertical: 2, overflow: 'hidden' },
  valor: { fontWeight: '800', color: cores.texto, fontSize: 15 },
  status: { fontWeight: '600', fontSize: 13 },
  cancelar: { marginLeft: 'auto', paddingVertical: 6, paddingHorizontal: 10, borderRadius: 8, borderWidth: 1, borderColor: cores.perigo },
  cancelarTexto: { color: cores.perigo, fontWeight: '700', fontSize: 12 },
  obs: { color: cores.textoSuave, fontSize: 12, marginTop: 2, width: '100%' },
  campo: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 10, padding: 12, color: cores.texto, fontSize: 18, fontWeight: '700' },
  campoTexto: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 10, padding: 12, color: cores.texto, fontSize: 15 },
  foto: { width: '100%', height: 180, borderRadius: 10, marginTop: 8, backgroundColor: cores.borda },
});
