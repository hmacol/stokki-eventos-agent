// Aba Ajuda (Hugo, 12/09): o chat do motorista com a Fresh Log. Primeiro
// responde o assistente, que sabe a rota e as paradas dele; quando precisa
// de gente, o chamado entra na fila da logística (mesma tela /atendimento
// que atende os clientes).
//
// Feito pra quem está na rua: bolhas grandes, chips no lugar de digitar
// sempre que dá, e um aviso claro de quando a logística está no ar.
import React, { useCallback, useEffect, useRef, useState } from 'react';
import { ActivityIndicator, Alert, KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { useFocusEffect } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';
import * as ImagePicker from 'expo-image-picker';
import * as api from '../../src/api';
import { Botao, Cartao, Carregando, Vazio } from '../../src/componentes';
import { cores } from '../../src/tema';
import type { Chamado, EstadoAtendimento, MensagemChamado, OpcaoMensagem, SituacaoAtendimento } from '../../src/tipos';

const INTERVALO_MS = 6000;      // poll enquanto a tela está aberta
const COR_ESTADO: Record<string, string> = { online: cores.sucesso, ausente: cores.alerta, almoco: cores.alerta, fechado: cores.textoSuave };

function Ponto({ cor }: { cor: string }) {
  return <View style={[s.ponto, { backgroundColor: cor }]} />;
}

function Bolha({ m }: { m: MensagemChamado }) {
  if (m.origem === 'sistema') return <Text style={s.sistema}>{m.texto}</Text>;
  const meu = m.origem === 'cliente';
  const quem = meu ? 'Você' : m.origem === 'assistente' ? 'Assistente' : (m.autor || 'Fresh Log');
  return (
    <View style={[s.msg, meu && s.msgEu]}>
      <View style={[s.bolha, meu ? s.bolhaEu : m.origem === 'assistente' && s.bolhaBot]}>
        <Text style={[s.bolhaTexto, meu && { color: '#fff' }]}>{m.texto}</Text>
        {m.anexos.map((a) => (
          <Text key={a.arquivo} style={[s.anexo, meu && { color: '#D8F3E8' }]}>📎 {a.nome}</Text>
        ))}
      </View>
      <Text style={s.metaMsg}>{quem} · {m.hora}{m.canal === 'email' ? ' · por e-mail' : ''}</Text>
    </View>
  );
}

export default function Ajuda() {
  const [estado, setEstado] = useState<EstadoAtendimento | null>(null);
  const [chamado, setChamado] = useState<Chamado | null>(null);
  const [mensagens, setMensagens] = useState<MensagemChamado[]>([]);
  const [texto, setTexto] = useState('');
  const [ocupado, setOcupado] = useState(false);
  const [erro, setErro] = useState<string | null>(null);
  const [verLista, setVerLista] = useState(false);
  const rolagem = useRef<ScrollView | null>(null);
  const ultimoId = useRef(0);

  const aplicar = useCallback((c: Chamado, msgs: MensagemChamado[], substituir: boolean) => {
    setChamado(c);
    setMensagens((antes) => {
      const lista = substituir ? msgs : [...antes, ...msgs.filter((m) => !antes.some((x) => x.id === m.id))];
      ultimoId.current = lista.length ? lista[lista.length - 1].id : 0;
      return lista;
    });
  }, []);

  const carregar = useCallback(async () => {
    try {
      const e = await api.atendimentoEstado();
      setEstado(e);
      setErro(null);
      if (e.ativo) aplicar(e.ativo.chamado, e.ativo.mensagens, true);
      else { setChamado(null); setMensagens([]); ultimoId.current = 0; }
    } catch (err) {
      setErro(err instanceof api.ErroRede ? 'Sem conexão. O chat precisa de sinal.' : (err as Error).message);
    }
  }, [aplicar]);

  useFocusEffect(useCallback(() => { void carregar(); }, [carregar]));

  // Enquanto a conversa está aberta, busca só o que chegou depois da última
  // mensagem -- é o que traz a resposta da logística sem recarregar tudo.
  useEffect(() => {
    if (!chamado) return;
    const t = setInterval(async () => {
      try {
        const r = await api.verChamado(chamado.id, ultimoId.current);
        if (r.mensagens.length) aplicar(r.chamado, r.mensagens, false);
        else setChamado(r.chamado);
        if (r.situacao) setEstado((e) => (e ? { ...e, situacao: r.situacao as SituacaoAtendimento } : e));
      } catch { /* sem sinal: tenta de novo no próximo ciclo */ }
    }, INTERVALO_MS);
    return () => clearInterval(t);
  }, [chamado?.id, aplicar]);

  const comErro = async (fn: () => Promise<void>) => {
    setOcupado(true);
    try { await fn(); setErro(null); }
    catch (e) { Alert.alert('Não deu', e instanceof api.ErroRede ? 'Sem conexão. Tente de novo com sinal.' : (e as Error).message); }
    finally { setOcupado(false); }
  };

  const iniciar = () => comErro(async () => {
    const r = await api.iniciarConversa();
    aplicar(r.chamado, r.mensagens, true);
    setVerLista(false);
  });

  const abrir = (id: number) => comErro(async () => {
    const r = await api.verChamado(id);
    aplicar(r.chamado, r.mensagens, true);
    setVerLista(false);
  });

  const mandar = (valor?: string, chip?: string) => comErro(async () => {
    if (!chamado) return;
    const t = (valor ?? texto).trim();
    if (!t) return;
    setTexto('');
    const r = await api.enviarMensagemChamado(chamado.id, t, chip);
    aplicar(r.chamado, r.mensagens, false);
  });

  const tocarOpcao = (o: OpcaoMensagem) => {
    if (!chamado) return;
    if (o.acao === 'chip') return void mandar(o.rotulo, o.valor);
    if (o.acao === 'resolvido') {
      return Alert.alert('Encerrar a conversa?', 'Se precisar de novo é só mandar outra mensagem.', [
        { text: 'Voltar', style: 'cancel' },
        { text: 'Encerrar', onPress: () => void comErro(async () => { const r = await api.acaoChamado(chamado.id, 'resolvido'); aplicar(r.chamado, r.mensagens, false); }) },
      ]);
    }
    void comErro(async () => { const r = await api.acaoChamado(chamado.id, 'atendente'); aplicar(r.chamado, r.mensagens, false); });
  };

  const mandarFoto = () => comErro(async () => {
    if (!chamado) return;
    const perm = await ImagePicker.requestCameraPermissionsAsync();
    if (!perm.granted) return Alert.alert('Câmera', 'Permita o uso da câmera pra mandar uma foto.');
    const f = await ImagePicker.launchCameraAsync({ quality: 0.6, allowsEditing: false, exif: false });
    if (f.canceled || !f.assets[0]) return;
    const r = await api.enviarFotoChamado(chamado.id, f.assets[0].uri, texto.trim() || 'Foto');
    setTexto('');
    aplicar(r.chamado, r.mensagens, false);
  });

  if (!estado) return <Carregando texto="Abrindo o atendimento…" />;

  const sit = estado.situacao;
  // Botões só da última fala (aviso de sistema não conta). O servidor apaga as
  // opções antigas a cada mensagem nova, mas a cópia local não é atualizada:
  // buscar "a última com opções" deixava os chips da saudação na tela depois
  // de escolhidos, e o motorista tocava de novo (Hugo, 13/09).
  const opcoes = [...mensagens].reverse().find((m) => m.origem !== 'sistema')?.opcoes ?? [];
  const naFila = chamado && ['NA_FILA', 'AGUARDANDO_FL'].includes(chamado.status);
  const resolvido = chamado?.status === 'RESOLVIDO';

  // ── lista de conversas anteriores ──
  if (verLista || !chamado) {
    return (
      <ScrollView style={s.tela} contentContainerStyle={{ padding: 16, paddingBottom: 40 }}>
        <Cartao estilo={{ backgroundColor: cores.primaria, borderColor: cores.primaria }}>
          <View style={s.linhaStatus}>
            <Ponto cor={COR_ESTADO[sit.estado] ?? cores.textoSuave} />
            <Text style={s.statusTexto}>{sit.texto}</Text>
          </View>
          <Text style={s.statusSub}>Logística: {sit.horario}</Text>
          <Text style={s.statusSub}>Fale sobre entrega, veículo, pagamento ou o app. Fora do horário a gente responde aqui mesmo assim que voltar.</Text>
          <Botao titulo="Nova conversa" onPress={iniciar} carregando={ocupado} estilo={{ marginTop: 14 }} />
        </Cartao>
        {erro ? <Text style={s.erro}>{erro}</Text> : null}
        {estado.chamados.length === 0 ? (
          <Vazio texto="Nenhuma conversa ainda. Toque em Nova conversa quando precisar." />
        ) : (
          estado.chamados.map((c) => (
            <Pressable key={c.id} onPress={() => abrir(c.id)}>
              <Cartao>
                <View style={s.itemCab}>
                  <Text style={s.itemTitulo} numberOfLines={1}>{c.assunto || c.area_rotulo || 'Conversa'}</Text>
                  {c.nao_lidas ? <View style={s.badge}><Text style={s.badgeTexto}>{c.nao_lidas}</Text></View> : null}
                </View>
                <Text style={s.itemMeta}>#{c.id} · {c.status_rotulo} · {c.quando}</Text>
                {c.ultima_texto ? <Text style={s.itemPrevia} numberOfLines={2}>{c.ultima_texto}</Text> : null}
              </Cartao>
            </Pressable>
          ))
        )}
        {chamado ? <Botao titulo="Voltar pra conversa" tipo="secundario" onPress={() => setVerLista(false)} /> : null}
      </ScrollView>
    );
  }

  // ── conversa aberta ──
  return (
    <KeyboardAvoidingView style={s.tela} behavior={Platform.OS === 'ios' ? 'padding' : undefined} keyboardVerticalOffset={90}>
      <View style={s.barra}>
        <View style={s.linhaStatus}>
          <Ponto cor={COR_ESTADO[sit.estado] ?? cores.textoSuave} />
          <Text style={s.barraTexto} numberOfLines={1}>{sit.texto}</Text>
        </View>
        <Pressable onPress={() => setVerLista(true)} hitSlop={10}>
          <Text style={s.barraLink}>Conversas</Text>
        </Pressable>
      </View>

      <ScrollView
        ref={rolagem}
        style={{ flex: 1 }}
        contentContainerStyle={{ padding: 14, paddingBottom: 20, gap: 10 }}
        onContentSizeChange={() => rolagem.current?.scrollToEnd({ animated: true })}
      >
        {naFila ? (
          <Text style={s.aviso}>
            {sit.estado === 'online' ? 'Você está na fila da logística. Alguém assume em instantes.' : `${sit.texto}. Deixamos registrado: a equipe responde aqui.`}
          </Text>
        ) : null}
        {mensagens.map((m) => <Bolha key={m.id} m={m} />)}
        {ocupado ? <ActivityIndicator color={cores.acento} style={{ marginTop: 6 }} /> : null}
      </ScrollView>

      {opcoes.length && !resolvido ? (
        <View style={s.chips}>
          {opcoes.map((o, i) => (
            <Pressable key={`${o.rotulo}-${i}`} onPress={() => tocarOpcao(o)} disabled={ocupado}
              style={[s.chip, o.estilo === 'principal' && s.chipPrincipal, ocupado && { opacity: 0.5 }]}>
              <Text style={[s.chipTexto, o.estilo === 'principal' && { color: '#fff' }]}>{o.rotulo}</Text>
            </Pressable>
          ))}
        </View>
      ) : null}

      {resolvido ? (
        <View style={s.rodapeResolvido}>
          <Text style={s.resolvidoTexto}>Conversa encerrada. Mandar uma mensagem reabre.</Text>
          <Botao titulo="Nova conversa" tipo="secundario" onPress={iniciar} estilo={{ minHeight: 44, paddingVertical: 10 }} />
        </View>
      ) : null}

      <View style={s.compor}>
        <Pressable onPress={mandarFoto} disabled={ocupado} style={s.icone} hitSlop={8}>
          <Ionicons name="camera" size={24} color={cores.textoSuave} />
        </Pressable>
        <TextInput
          style={s.campo}
          placeholder="Escreva sua mensagem…"
          placeholderTextColor="#9CA3AF"
          value={texto}
          onChangeText={setTexto}
          multiline
        />
        <Pressable onPress={() => mandar()} disabled={ocupado || !texto.trim()}
          style={[s.enviar, (ocupado || !texto.trim()) && { opacity: 0.4 }]}>
          <Ionicons name="send" size={20} color="#fff" />
        </Pressable>
      </View>
    </KeyboardAvoidingView>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.fundo },
  ponto: { width: 9, height: 9, borderRadius: 9 },
  linhaStatus: { flexDirection: 'row', alignItems: 'center', gap: 8, flex: 1 },
  statusTexto: { color: '#fff', fontWeight: '700', fontSize: 15, flex: 1 },
  statusSub: { color: cores.textoSuaveSobrePrimaria, marginTop: 6, fontSize: 12.5, lineHeight: 18 },
  barra: { flexDirection: 'row', alignItems: 'center', gap: 10, paddingHorizontal: 14, paddingVertical: 10, backgroundColor: cores.cartao, borderBottomWidth: 1, borderColor: cores.borda },
  barraTexto: { color: cores.texto, fontWeight: '600', fontSize: 13, flex: 1 },
  barraLink: { color: cores.info, fontWeight: '700', fontSize: 13 },
  msg: { alignItems: 'flex-start', maxWidth: '88%', gap: 3 },
  msgEu: { alignSelf: 'flex-end', alignItems: 'flex-end' },
  bolha: { backgroundColor: cores.cartao, borderWidth: 1, borderColor: cores.borda, borderRadius: 14, borderTopLeftRadius: 4, paddingHorizontal: 13, paddingVertical: 10 },
  bolhaEu: { backgroundColor: cores.acento, borderColor: 'transparent', borderTopLeftRadius: 14, borderBottomRightRadius: 4 },
  bolhaBot: { borderLeftWidth: 3, borderLeftColor: cores.info },
  bolhaTexto: { color: cores.texto, fontSize: 15.5, lineHeight: 22 },
  anexo: { marginTop: 6, fontSize: 13, color: cores.info },
  metaMsg: { color: cores.textoSuave, fontSize: 11.5 },
  sistema: { alignSelf: 'center', textAlign: 'center', color: cores.textoSuave, fontSize: 12.5, backgroundColor: cores.cartao, borderWidth: 1, borderColor: cores.borda, borderRadius: 999, paddingHorizontal: 14, paddingVertical: 5, overflow: 'hidden' },
  aviso: { backgroundColor: cores.alertaBg, color: '#7A5211', borderRadius: 10, padding: 10, fontSize: 13, textAlign: 'center', fontWeight: '600' },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, paddingHorizontal: 14, paddingBottom: 6 },
  chip: { borderWidth: 1, borderColor: cores.borda, backgroundColor: cores.cartao, borderRadius: 999, paddingHorizontal: 14, paddingVertical: 10 },
  chipPrincipal: { backgroundColor: cores.primaria, borderColor: cores.primaria },
  chipTexto: { color: cores.texto, fontWeight: '600', fontSize: 14 },
  compor: { flexDirection: 'row', alignItems: 'flex-end', gap: 8, padding: 10, paddingBottom: 14, backgroundColor: cores.cartao, borderTopWidth: 1, borderColor: cores.borda },
  icone: { paddingVertical: 10, paddingHorizontal: 4 },
  campo: { flex: 1, borderWidth: 1, borderColor: cores.borda, borderRadius: 12, paddingHorizontal: 12, paddingVertical: 10, fontSize: 15.5, color: cores.texto, backgroundColor: cores.fundo, maxHeight: 110 },
  enviar: { width: 46, height: 46, borderRadius: 12, backgroundColor: cores.acento, alignItems: 'center', justifyContent: 'center' },
  rodapeResolvido: { paddingHorizontal: 14, paddingBottom: 8, gap: 8 },
  resolvidoTexto: { color: cores.textoSuave, fontSize: 13, textAlign: 'center' },
  itemCab: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  itemTitulo: { fontWeight: '700', color: cores.texto, fontSize: 15.5, flex: 1 },
  itemMeta: { color: cores.textoSuave, fontSize: 12.5, marginTop: 3 },
  itemPrevia: { color: cores.textoSuave, fontSize: 13, marginTop: 6 },
  badge: { minWidth: 22, height: 22, borderRadius: 11, backgroundColor: cores.acento, alignItems: 'center', justifyContent: 'center', paddingHorizontal: 6 },
  badgeTexto: { color: '#fff', fontWeight: '800', fontSize: 12 },
  erro: { color: '#fff', backgroundColor: cores.alerta, padding: 10, borderRadius: 8, marginBottom: 12, textAlign: 'center' },
});
