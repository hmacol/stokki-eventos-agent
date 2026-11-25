// Etapa de carregamento (checklist de carregamento): aparece no lugar do
// "Iniciar rota" quando o servidor exige (rota.carregamento.exigido && !feito).
// O motorista informa quem carregou, fotografa o BAÚ INTEIRO (porta aberta) e
// desmarca os pedidos que não foram carregados (Não coube / Não Encontrado /
// Outros). Desmarcar vale pela PARADA inteira.
//
// Confirmar exige rede: tirar o pedido da Vuupt é síncrono, então NÃO passa
// pela fila offline (src/fila.ts só empresta o gerador de uuid). Falha parcial
// ("este pedido não saiu da rota") reenvia com o MESMO uuid.
import React, { useEffect, useRef, useState } from 'react';
import { Alert, Image, Pressable, StyleSheet, Text, TextInput, View } from 'react-native';
import * as ImagePicker from 'expo-image-picker';
import AsyncStorage from '@react-native-async-storage/async-storage';
import { Ionicons } from '@expo/vector-icons';
import * as api from './api';
import * as fila from './fila';
import { conferirPermissao } from './permissoes';
import { Botao } from './componentes';
import { cores } from './tema';
import { codigoComNf, nomeExibicao } from './textos';
import type { Desmarcado, FalhaCarregamento, MotivoDesmarcar, Parada, ResultadoValidacao, Rota } from './tipos';

const CHAVE_CARREGADORES = 'carregadores';
const MOTIVOS: MotivoDesmarcar[] = ['Não coube', 'Não Encontrado', 'Outros'];

type FotoCarga = { uri: string; nota: string | null };
type Rascunho = { motivo: MotivoDesmarcar | null; observacao: string };

async function lerCarregadores(): Promise<string[]> {
  try {
    const bruto = await AsyncStorage.getItem(CHAVE_CARREGADORES);
    const lista = bruto ? (JSON.parse(bruto) as unknown) : [];
    return Array.isArray(lista) ? lista.filter((x): x is string => typeof x === 'string').slice(0, 5) : [];
  } catch {
    return [];
  }
}

async function guardarCarregador(nome: string) {
  const atuais = await lerCarregadores();
  const lista = [nome, ...atuais.filter((n) => n.toLowerCase() !== nome.toLowerCase())].slice(0, 5);
  await AsyncStorage.setItem(CHAVE_CARREGADORES, JSON.stringify(lista)).catch(() => {});
}

/** "PS-1, PS-2" -> ["PS-1", "PS-2"]: a parada pode juntar vários pedidos. */
const pedidosDa = (p: Parada) => (p.codigo ?? '').split(',').map((c) => c.trim()).filter(Boolean);

export function CarregamentoRota({ rota, aoConfirmar }: { rota: Rota; aoConfirmar: (rotaAtualizada: Rota) => void }) {
  // Uma vez por tela: o reenvio ("Tentar de novo") reaproveita o mesmo uuid.
  const [uuid] = useState(() => fila.novoUuid());
  const paradas = rota.paradas.filter((p) => p.situacao !== 'CANCELADA');

  const [carregador, setCarregador] = useState('');
  const [sugestoes, setSugestoes] = useState<string[]>([]);
  const [foto, setFoto] = useState<FotoCarga | null>(null);
  const [tentativa, setTentativa] = useState(0);
  const [conferindo, setConferindo] = useState(false);
  const [veredito, setVeredito] = useState<ResultadoValidacao | null>(null);
  const [msgFoto, setMsgFoto] = useState<string | null>(null);
  const [desmarcados, setDesmarcados] = useState<Record<number, Rascunho>>({});
  const [editando, setEditando] = useState<number | null>(null);
  const [rascunho, setRascunho] = useState<Rascunho>({ motivo: null, observacao: '' });
  const [enviando, setEnviando] = useState(false);
  const [erro, setErro] = useState<string | null>(null);
  const [falhas, setFalhas] = useState<FalhaCarregamento[]>([]);
  // Paradas que o servidor já tirou da rota numa tentativa anterior: não dá mais pra remarcar.
  const [jaSairam, setJaSairam] = useState<number[]>([]);
  // Guarda síncrona: dois toques seguidos entram antes de o setEnviando renderizar.
  const enviandoRef = useRef(false);

  useEffect(() => { void lerCarregadores().then(setSugestoes); }, []);

  const nomeLimpo = carregador.trim();
  const todasDesmarcadas = paradas.length > 0 && paradas.every((p) => desmarcados[p.id]);
  const podeConfirmar = nomeLimpo.length > 0 && foto !== null && !conferindo && !enviando;

  const aceitarFoto = (uri: string, nota: string | null, v: ResultadoValidacao | null) => {
    setFoto({ uri, nota });
    setVeredito(v);
    setMsgFoto(null);
    setErro(null);
  };

  /** Avalia a foto na hora. Reprovada pede outra; esgotadas as tentativas o servidor
   * devolve pode_seguir=true e a foto segue pra revisão humana. Sem rede ou
   * NAO_VERIFICADO nunca travam (o servidor reconfere ao confirmar). */
  const conferirFoto = async (uri: string, n: number) => {
    setConferindo(true);
    try {
      const r = await api.validarFoto(uri, { tipo: 'CARREGAMENTO', rotaId: rota.id, tentativa: n });
      if (r.resultado !== 'REPROVADO') {
        aceitarFoto(uri, r.resultado === 'NAO_VERIFICADO' ? 'Foto não conferida — vai pra revisão.' : null, r);
        return;
      }
      const motivo = r.motivo ?? 'A foto não mostra o baú inteiro.';
      const max = r.tentativa + r.tentativas_restantes;
      if (!r.pode_seguir) {
        setFoto(null);
        setVeredito(r);
        setMsgFoto(`${motivo} Tire outra foto, com o baú inteiro e a porta aberta (tentativa ${r.tentativa} de ${max}).`);
        return;
      }
      setFoto(null);
      setVeredito(r);
      setMsgFoto(`${motivo} Você pode tirar de novo ou seguir assim — nesse caso a foto vai pra conferência manual.`);
      Alert.alert('Ainda não ficou boa', `${motivo}\n\nVocê pode tirar de novo ou seguir assim — nesse caso a foto vai pra conferência manual.`, [
        { text: 'Tirar de novo', style: 'cancel' },
        { text: 'Não consigo melhorar', onPress: () => aceitarFoto(uri, 'Segue pra conferência manual.', r) },
      ]);
    } catch {
      aceitarFoto(uri, 'Sem sinal pra conferir agora — será conferida ao confirmar.', null);
    } finally {
      setConferindo(false);
    }
  };

  const tirarFoto = async () => {
    if (!conferirPermissao(await ImagePicker.requestCameraPermissionsAsync(), 'Câmera', 'Permita o uso da câmera pra fotografar o baú.')) return;
    const r = await ImagePicker.launchCameraAsync({ quality: 0.7, allowsEditing: false, exif: false });
    const uri = !r.canceled ? r.assets[0]?.uri : null;
    if (!uri) return;
    const n = tentativa + 1;
    setTentativa(n);
    await conferirFoto(uri, n);
  };

  const alternarParada = (p: Parada) => {
    if (enviando || jaSairam.includes(p.id)) return;
    if (desmarcados[p.id]) {
      // volta a marcar
      setDesmarcados((d) => { const { [p.id]: _, ...resto } = d; return resto; });
      setEditando((e) => (e === p.id ? null : e));
      return;
    }
    setEditando(editando === p.id ? null : p.id);
    setRascunho({ motivo: null, observacao: '' });
  };

  const confirmarMotivo = (p: Parada) => {
    if (!rascunho.motivo) return Alert.alert('Motivo', 'Escolha o motivo.');
    if (rascunho.motivo === 'Outros' && !rascunho.observacao.trim()) return Alert.alert('Motivo', 'Descreva o motivo em "Outros".');
    setDesmarcados((d) => ({ ...d, [p.id]: { motivo: rascunho.motivo, observacao: rascunho.observacao.trim() } }));
    setEditando(null);
  };

  const enviar = async () => {
    if (!foto || !nomeLimpo || enviandoRef.current) return;
    enviandoRef.current = true;
    setEnviando(true);
    setErro(null);
    setFalhas([]);
    try {
      const lista: Desmarcado[] = paradas.filter((p) => desmarcados[p.id]).map((p) => ({
        parada_id: p.id, motivo: desmarcados[p.id].motivo as MotivoDesmarcar, observacao: desmarcados[p.id].observacao,
      }));
      const r = await api.confirmarCarregamento(rota.id, foto.uri, nomeLimpo, uuid, lista, Math.max(tentativa, 1));
      await guardarCarregador(nomeLimpo);
      aoConfirmar(r.rota);
    } catch (e) {
      if (e instanceof api.ErroRede) {
        setErro('Sem conexão. Confirmar o carregamento precisa de sinal — tente de novo quando tiver rede.');
      } else if (e instanceof api.ErroApi && e.status === 409) {
        const d = (e.dados ?? {}) as { falhas?: FalhaCarregamento[]; removidos?: number[]; resultado?: string; motivo?: string | null };
        if (Array.isArray(d.falhas) && d.falhas.length > 0) {
          setFalhas(d.falhas);
          if (Array.isArray(d.removidos)) setJaSairam((atuais) => Array.from(new Set([...atuais, ...d.removidos!])));
        } else if (d.resultado === 'REPROVADO') {
          // O servidor reconferiu a foto e não aceitou: pede outra.
          setFoto(null);
          setMsgFoto(`${d.motivo ?? e.message} Tire outra foto, com o baú inteiro e a porta aberta.`);
        } else {
          setErro(e.message);
        }
      } else {
        setErro((e as Error).message);
      }
    } finally {
      enviandoRef.current = false;
      setEnviando(false);
    }
  };

  const confirmar = () => {
    if (!podeConfirmar) return;
    if (!todasDesmarcadas) return void enviar();
    Alert.alert(
      'A rota será cancelada',
      'Você desmarcou TODAS as paradas. Se confirmar, esta rota será cancelada. Tem certeza?',
      [
        { text: 'Voltar', style: 'cancel' },
        { text: 'Sim, cancelar a rota', style: 'destructive', onPress: () => void enviar() },
      ],
    );
  };

  const rotuloParada = (id: number) => {
    const p = paradas.find((x) => x.id === id);
    return p ? `${nomeExibicao(p)}${p.codigo ? ` (${codigoComNf(p)})` : ''}` : `parada ${id}`;
  };

  return (
    <View style={{ marginTop: 14 }}>
      <Text style={s.titulo}>Carregamento</Text>
      <Text style={s.sub}>Confira o que foi pro baú antes de iniciar a rota.</Text>

      <Text style={s.rotulo}>Quem carregou? *</Text>
      <TextInput
        style={s.campo}
        placeholder="Nome de quem carregou"
        placeholderTextColor="#9CA3AF"
        value={carregador}
        onChangeText={setCarregador}
        editable={!enviando}
        autoCapitalize="words"
      />
      {sugestoes.length > 0 ? (
        <View style={s.chips}>
          {sugestoes.map((n) => (
            <Pressable key={n} onPress={() => setCarregador(n)} style={[s.chip, nomeLimpo === n && s.chipAtivo]} accessibilityRole="button">
              <Text style={[s.chipTexto, nomeLimpo === n && { color: '#fff' }]}>{n}</Text>
            </Pressable>
          ))}
        </View>
      ) : null}

      <Text style={s.rotulo}>Foto do baú *</Text>
      <Text style={s.dica}>Fotografe o baú INTEIRO, com a porta aberta, mostrando toda a carga.</Text>
      {foto ? <Image source={{ uri: foto.uri }} style={s.foto} /> : null}
      {foto && veredito?.resultado === 'APROVADO' && veredito.ocupacao_pct != null ? <Text style={s.ok}>Baú inteiro · ocupação ~{Math.round(veredito.ocupacao_pct)}%</Text> : null}
      {foto && !foto.nota && veredito?.resultado === 'APROVADO' ? <Text style={s.ok}>Foto aprovada.</Text> : null}
      {foto?.nota ? <Text style={s.aviso}>{foto.nota}</Text> : null}
      {msgFoto ? <Text style={s.erro}>{msgFoto}</Text> : null}
      <Botao
        titulo={conferindo ? 'Conferindo a foto…' : foto ? '📷  Tirar outra foto' : '📷  Tirar foto do baú'}
        tipo={foto ? 'secundario' : 'primario'}
        carregando={conferindo}
        desabilitado={conferindo || enviando}
        onPress={tirarFoto}
        estilo={{ marginTop: 8 }}
      />

      <Text style={s.rotulo}>O que foi carregado</Text>
      <Text style={s.dica}>Desmarque a parada que NÃO foi pro baú (todos os pedidos dela saem da rota).</Text>
      {paradas.map((p) => {
        const d = desmarcados[p.id];
        const saiu = jaSairam.includes(p.id);
        const aberto = editando === p.id;
        const pedidos = pedidosDa(p);
        return (
          <View key={p.id} style={[s.parada, d && s.paradaOff, saiu && { opacity: 0.6 }]}>
            <Pressable
              style={s.paradaTopo}
              onPress={() => alternarParada(p)}
              disabled={enviando || saiu}
              accessibilityRole="checkbox"
              accessibilityState={{ checked: !d, disabled: saiu }}
              accessibilityLabel={`${nomeExibicao(p)}, parada ${p.ordem}`}
            >
              <Ionicons name={d ? 'square-outline' : 'checkbox'} size={26} color={d ? cores.textoSuave : cores.acento} />
              <View style={{ flex: 1 }}>
                <Text style={[s.paradaNome, d && s.riscado]} numberOfLines={2}>{p.ordem}. {nomeExibicao(p)}</Text>
                {pedidos.length > 0 ? <Text style={s.paradaPedidos}>{pedidos.join(' · ')}{p.volume_caixas ? ` · ${p.volume_caixas} cx` : ''}</Text> : null}
                {d ? <Text style={s.paradaMotivo}>{saiu ? 'Já saiu da rota: ' : 'Fora da rota: '}{d.motivo}{d.observacao ? ` — ${d.observacao}` : ''}</Text> : null}
              </View>
            </Pressable>
            {aberto ? (
              <View style={s.painel}>
                <Text style={s.rotulo}>Por que não foi carregado?</Text>
                <View style={s.chips}>
                  {MOTIVOS.map((m) => (
                    <Pressable key={m} onPress={() => setRascunho((r) => ({ ...r, motivo: m }))} style={[s.chip, rascunho.motivo === m && s.chipAtivo]} accessibilityRole="button">
                      <Text style={[s.chipTexto, rascunho.motivo === m && { color: '#fff' }]}>{m}</Text>
                    </Pressable>
                  ))}
                </View>
                {rascunho.motivo === 'Outros' ? (
                  <TextInput
                    style={[s.campo, { marginTop: 8 }]}
                    placeholder="Descreva o motivo"
                    placeholderTextColor="#9CA3AF"
                    value={rascunho.observacao}
                    onChangeText={(v) => setRascunho((r) => ({ ...r, observacao: v }))}
                  />
                ) : null}
                <View style={{ flexDirection: 'row', gap: 8, marginTop: 10 }}>
                  <Botao titulo="Voltar" tipo="secundario" onPress={() => setEditando(null)} estilo={{ flex: 1 }} />
                  <Botao titulo="Tirar da rota" tipo="perigo" onPress={() => confirmarMotivo(p)} estilo={{ flex: 1 }} />
                </View>
              </View>
            ) : null}
          </View>
        );
      })}

      {todasDesmarcadas ? <Text style={s.erro}>Todas as paradas estão desmarcadas: ao confirmar, a rota será cancelada.</Text> : null}

      {falhas.length > 0 ? (
        <View style={s.falhas}>
          <Text style={s.falhasTitulo}>Estes pedidos não saíram da rota:</Text>
          {falhas.map((f) => (
            <Text key={`${f.parada_id}-${f.codigo}`} style={s.falhasItem}>• {f.codigo || rotuloParada(f.parada_id)}: {f.erro}</Text>
          ))}
          <Text style={s.falhasItem}>Os demais já foram tirados. Toque em "Tentar de novo" pra repetir só o que falta.</Text>
        </View>
      ) : null}
      {erro ? <Text style={s.erro}>{erro}</Text> : null}

      <Botao
        titulo={falhas.length > 0 || erro ? 'Tentar de novo' : 'Confirmar carregamento'}
        tipo={todasDesmarcadas ? 'perigo' : 'primario'}
        carregando={enviando}
        desabilitado={!podeConfirmar}
        onPress={confirmar}
        estilo={{ marginTop: 14 }}
      />
      {!podeConfirmar && !enviando ? (
        <Text style={s.dica}>{!nomeLimpo ? 'Informe quem carregou.' : !foto ? 'Falta uma foto do baú aprovada.' : ''}</Text>
      ) : null}
    </View>
  );
}

const s = StyleSheet.create({
  titulo: { fontSize: 17, fontWeight: '800', color: cores.texto },
  sub: { color: cores.textoSuave, marginTop: 4 },
  rotulo: { fontWeight: '700', color: cores.texto, marginTop: 16, marginBottom: 6, fontSize: 15 },
  dica: { color: cores.textoSuave, fontSize: 12.5, marginBottom: 6 },
  campo: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 10, padding: 12, fontSize: 16, color: cores.texto },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: 8, marginTop: 8 },
  chip: { paddingVertical: 9, paddingHorizontal: 14, borderRadius: 999, backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda },
  chipAtivo: { backgroundColor: cores.primaria, borderColor: cores.primaria },
  chipTexto: { color: cores.texto, fontWeight: '600' },
  foto: { width: '100%', height: 200, borderRadius: 10, marginBottom: 8, backgroundColor: cores.borda },
  ok: { color: cores.acento, fontWeight: '700', marginBottom: 4 },
  aviso: { color: cores.alerta, fontSize: 13, marginBottom: 4 },
  erro: { color: cores.perigo, fontSize: 13.5, fontWeight: '600', marginTop: 8 },
  parada: { borderWidth: 1, borderColor: cores.borda, borderRadius: 12, backgroundColor: cores.cartao, marginBottom: 8, overflow: 'hidden' },
  paradaOff: { backgroundColor: cores.perigoBg, borderColor: cores.perigo },
  paradaTopo: { flexDirection: 'row', alignItems: 'center', gap: 12, padding: 12, minHeight: 58 },
  paradaNome: { fontSize: 15, fontWeight: '700', color: cores.texto },
  riscado: { textDecorationLine: 'line-through', color: cores.textoSuave },
  paradaPedidos: { fontSize: 12.5, color: cores.textoSuave, marginTop: 2 },
  paradaMotivo: { fontSize: 12.5, color: cores.perigo, fontWeight: '600', marginTop: 3 },
  painel: { paddingHorizontal: 12, paddingBottom: 12, borderTopWidth: StyleSheet.hairlineWidth, borderColor: cores.borda },
  falhas: { backgroundColor: cores.perigoBg, borderRadius: 10, padding: 12, marginTop: 12 },
  falhasTitulo: { color: cores.perigo, fontWeight: '800', marginBottom: 4 },
  falhasItem: { color: cores.texto, fontSize: 13.5, marginTop: 2 },
});
