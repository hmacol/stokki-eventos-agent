// Resultado da parada (Hugo, 26/08): quatro ações, todas por ARRASTAR pra
// direita -- Entregue / Entregue parcial / Não entregue / Reagendar. As
// três de entrega perguntam "tem certeza?" antes de abrir o checklist
// (checklist_modelo); fotos pela câmera (várias por campo), motivo do
// motivos_ocorrencia. Reagendar marca um horário de retorno pra mesma
// entrega (a parada volta pra pendente). Tudo entra na fila offline.
// Assinatura na tela foi desconsiderada pelo Hugo em 11/09 (o canhoto
// fotografado já é a prova de entrega); src/assinatura.tsx fica sem uso.
import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Image, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import * as ImagePicker from 'expo-image-picker';
import AsyncStorage from '@react-native-async-storage/async-storage';
import * as api from '../../src/api';
import * as fila from '../../src/fila';
import * as gps from '../../src/gps';
import * as local from '../../src/local';
import { carregarRotas } from '../../src/rotasStore';
import { Deslizar } from '../../src/deslizar';
import { Botao, Cartao, Carregando } from '../../src/componentes';
import { cores, hoje } from '../../src/tema';
import type { CampoChecklist, Checklist, Parada, SituacaoParada } from '../../src/tipos';

type Fluxo = 'ENTREGUE' | 'PARCIAL' | 'NAO_ENTREGUE' | 'REAGENDAR';
const CHAVE_CHECKLIST = 'motorista.checklist.v1';

const CHECKLIST_PADRAO: Checklist = {
  fluxos: {
    ENTREGUE: [
      { chave: 'nome_recebedor', rotulo: 'Nome de quem recebeu', tipo: 'TEXTO', obrigatorio: true, opcoes: null, aviso: null },
      { chave: 'vinculo', rotulo: 'Quem é essa pessoa?', tipo: 'SELECAO', obrigatorio: true, opcoes: ['Próprio cliente', 'Filho', 'Porteiro', 'Zelador', 'Conferente', 'Gerente', 'Outro'], aviso: null },
      { chave: 'documento', rotulo: 'Documento da pessoa', tipo: 'DOCUMENTO', obrigatorio: false, opcoes: null, aviso: null },
      { chave: 'foto_canhoto', rotulo: 'Foto do canhoto / comprovante', tipo: 'FOTO', obrigatorio: true, opcoes: null, aviso: null },
    ],
    PARCIAL: [
      { chave: 'nome_recebedor', rotulo: 'Nome de quem recebeu', tipo: 'TEXTO', obrigatorio: true, opcoes: null, aviso: null },
      { chave: 'foto_canhoto', rotulo: 'Foto do canhoto / comprovante', tipo: 'FOTO', obrigatorio: true, opcoes: null, aviso: null },
      { chave: 'foto_nf_devolucao', rotulo: 'Foto da nota de devolução', tipo: 'FOTO', obrigatorio: true, opcoes: null, aviso: null },
      { chave: 'foto_produto_devolvido', rotulo: 'Foto do produto devolvido', tipo: 'FOTO', obrigatorio: true, opcoes: null, aviso: null },
    ],
    NAO_ENTREGUE: [
      { chave: 'observacoes', rotulo: 'Observações', tipo: 'TEXTO', obrigatorio: false, opcoes: null, aviso: null },
      { chave: 'foto_ocorrencia', rotulo: 'Foto (opcional)', tipo: 'FOTO', obrigatorio: false, opcoes: null, aviso: null },
    ],
  },
  motivos: [],
};

const TIPO_COMPROVANTE: Record<string, string> = {
  foto_canhoto: 'CANHOTO', foto_nf_devolucao: 'NF_DEVOLUCAO', foto_produto_devolvido: 'PRODUTO', foto_ocorrencia: 'OCORRENCIA', documento: 'DOCUMENTO',
};

// Um canhoto por NF (Hugo, 12/09): quando o pedido tem mais de uma nota,
// o campo foto_canhoto vira um bloco por NF. A chave da foto carrega a
// nota: "foto_canhoto::12345" -- `chaveBase` volta pro campo original.
const SEP_NF = '::';
const chaveBase = (chave: string) => chave.split(SEP_NF)[0];
const nfDaChave = (chave: string) => (chave.includes(SEP_NF) ? chave.split(SEP_NF)[1] : null);

const TITULO_FLUXO: Record<Fluxo, string> = {
  ENTREGUE: 'Entregue', PARCIAL: 'Entregue parcial', NAO_ENTREGUE: 'Não entregue', REAGENDAR: 'Reagendar',
};

async function carregarChecklist(): Promise<Checklist> {
  try {
    const c = await api.checklist();
    if (Object.keys(c.fluxos).length > 0) {
      await AsyncStorage.setItem(CHAVE_CHECKLIST, JSON.stringify(c));
      return c;
    }
  } catch {
    // usa cache/padrão
  }
  const cache = await AsyncStorage.getItem(CHAVE_CHECKLIST);
  return cache ? (JSON.parse(cache) as Checklist) : CHECKLIST_PADRAO;
}

function horaMaisMinutos(min: number): string {
  const d = new Date(Date.now() + min * 60000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export default function RegistroParada() {
  const { id, rota: rotaParam } = useLocalSearchParams<{ id: string; rota: string }>();
  const paradaId = Number(id);
  const rotaId = Number(rotaParam);
  const router = useRouter();
  const [parada, setParada] = useState<Parada | null>(null);
  const [checklist, setChecklist] = useState<Checklist | null>(null);
  const [fluxo, setFluxo] = useState<Fluxo | null>(null);
  const [respostas, setRespostas] = useState<Record<string, string>>({});
  // Várias fotos por campo (canhoto de frente e verso, mais de um
  // comprovante...): cada uma vira um COMPROVANTE separado na fila.
  const [fotos, setFotos] = useState<Record<string, string[]>>({});
  const [motivoId, setMotivoId] = useState<number | null>(null);
  const [enviando, setEnviando] = useState(false);
  // Conferência automática da foto (Hugo, 12/09). `conferindo` é a chave
  // do campo sendo conferido agora; `tentativas` conta por campo pra o
  // servidor liberar o "não consigo melhorar" depois do limite.
  const [conferindo, setConferindo] = useState<string | null>(null);
  const [tentativas, setTentativas] = useState<Record<string, number>>({});
  const [avisosFoto, setAvisosFoto] = useState<Record<string, string>>({});
  // Reagendar
  const [horaRetorno, setHoraRetorno] = useState<string | 'FIM' | null>(null);
  const [horaManual, setHoraManual] = useState('');
  const [obsRetorno, setObsRetorno] = useState('');

  const carregar = useCallback(async () => {
    const r = await carregarRotas();
    const rota = r.rotas.find((x) => x.id === rotaId);
    setParada(rota?.paradas.find((p) => p.id === paradaId) ?? null);
    setChecklist(await carregarChecklist());
  }, [rotaId, paradaId]);
  useEffect(() => { void carregar(); }, [carregar]);

  if (!parada || !checklist) return <Carregando />;

  // Conferência automática das fotos: só existe se o servidor disser que
  // está ligada (config api_motorista.validacao_fotos.ativo). Desligada,
  // a tela se comporta exatamente como antes.
  const validacaoAtiva = checklist.validacao_fotos?.ativo === true;

  const escolherComConfirmacao = (f: Fluxo) =>
    new Promise<void>((resolve) => {
      Alert.alert(
        `Finalizar como "${TITULO_FLUXO[f]}"?`,
        'Tem certeza que quer finalizar este pedido? Depois disso o checklist é aberto.',
        [
          { text: 'Voltar', style: 'cancel', onPress: () => resolve() },
          { text: 'Sim, finalizar', onPress: () => { setFluxo(f); resolve(); } },
        ],
        { cancelable: true, onDismiss: () => resolve() },
      );
    });

  const guardarFoto = (chave: string, uri: string, substituir: boolean) =>
    setFotos((f) => ({ ...f, [chave]: substituir ? [uri] : [...(f[chave] ?? []), uri] }));

  /** Pergunta ao servidor se a foto serve (nitidez, legibilidade e, no
   * canhoto, o número da NF). Reprovada trava; depois de max_tentativas
   * o servidor libera o "seguir assim" e a foto vai pra revisão humana.
   * Sem rede / validação desligada nunca trava o motorista. */
  const conferirFoto = async (chave: string, uri: string, substituir: boolean) => {
    const nf = nfDaChave(chave);
    const tentativa = (tentativas[chave] ?? 0) + 1;
    setTentativas((t) => ({ ...t, [chave]: tentativa }));
    setConferindo(chave);
    try {
      const r = await api.validarFoto(uri, { tipo: 'CANHOTO', paradaId: parada.id, nf, tentativa });
      if (r.resultado !== 'REPROVADO') {
        guardarFoto(chave, uri, substituir);
        setAvisosFoto((a) => ({ ...a, [chave]: r.resultado === 'NAO_VERIFICADO' ? 'Foto não conferida — vai pra revisão.' : '' }));
        return;
      }
      const motivo = r.motivo ?? 'A foto não ficou boa.';
      if (!r.pode_seguir) {
        Alert.alert('Foto não serve', `${motivo}\n\nTire outra foto.`);
        return;
      }
      // Tentativas esgotadas: o motorista decide seguir (revisão humana).
      Alert.alert('Ainda não ficou boa', `${motivo}\n\nVocê pode tentar de novo ou seguir assim — nesse caso a foto vai pra conferência manual.`, [
        { text: 'Tirar de novo', style: 'cancel' },
        { text: 'Não consigo melhorar', onPress: () => {
          guardarFoto(chave, uri, substituir);
          setAvisosFoto((a) => ({ ...a, [chave]: 'Segue pra conferência manual.' }));
        } },
      ]);
    } catch {
      // Sem sinal na hora: aceita a foto; o servidor confere quando ela chegar.
      guardarFoto(chave, uri, substituir);
      setAvisosFoto((a) => ({ ...a, [chave]: 'Sem sinal pra conferir agora — será conferida no envio.' }));
    } finally {
      setConferindo(null);
    }
  };

  // substituir=true (documento): a nova foto toma o lugar da anterior;
  // senão ela é acrescentada à lista do campo.
  const tirarFoto = async (chave: string, substituir = false) => {
    const perm = await ImagePicker.requestCameraPermissionsAsync();
    if (!perm.granted) return Alert.alert('Câmera', 'Permita o uso da câmera pra fotografar o comprovante.');
    const r = await ImagePicker.launchCameraAsync({ quality: 0.7, allowsEditing: false, exif: false });
    const uri = !r.canceled ? r.assets[0]?.uri : null;
    if (!uri) return;
    if (validacaoAtiva && chaveBase(chave) === 'foto_canhoto') return conferirFoto(chave, uri, substituir);
    guardarFoto(chave, uri, substituir);
  };
  const removerFoto = (chave: string, indice: number) =>
    setFotos((f) => ({ ...f, [chave]: (f[chave] ?? []).filter((_, i) => i !== indice) }));
  const fotosDe = (chave: string) => fotos[chave] ?? [];

  // O modelo do servidor (checklist_modelo) traz `vinculo_outro` como card
  // próprio; aqui ele é desenhado dentro do card de vínculo, só com "Outro".
  // Ele traz também um campo de motivo (motivo_ocorrencia / motivo_devolucao)
  // como SELEÇÃO sem opções: isso era um segundo bloco "Motivo da ocorrência"
  // impossível de preencher (e obrigatório, travando a finalização). O motivo
  // é o card do topo, vindo de motivos_ocorrencia -- aqui esse campo some.
  const camposBase: CampoChecklist[] = (fluxo && fluxo !== 'REAGENDAR' ? (checklist.fluxos[fluxo] ?? CHECKLIST_PADRAO.fluxos[fluxo]) : [])
    .filter((c) => c.chave !== 'vinculo_outro' && !(c.tipo === 'SELECAO' && !c.opcoes && c.chave.startsWith('motivo')));
  // Um canhoto por NF (Hugo, 12/09): pedido com mais de uma nota vira um
  // bloco de canhoto por nota. Com uma NF só (ou nenhuma conhecida) fica
  // o bloco único de sempre, que já aceita várias fotos.
  const nfsPedido = parada.nfs ?? [];
  const campos: CampoChecklist[] = nfsPedido.length > 1
    ? camposBase.flatMap((c) => (c.chave !== 'foto_canhoto' ? [c] : nfsPedido.map((nf) => ({
        ...c,
        chave: `foto_canhoto${SEP_NF}${nf}`,
        rotulo: `Canhoto da NF ${nf}`,
        aviso: c.aviso ?? 'Enquadre o número da nota e a assinatura de quem recebeu.',
      }))))
    : camposBase;
  const exigeMotivo = fluxo === 'NAO_ENTREGUE' || fluxo === 'PARCIAL';
  const motivos = checklist.motivos;
  const rotuloMotivo = fluxo === 'PARCIAL' ? 'Motivo da devolução parcial' : 'Motivo da não entrega';

  // O motivo vai direto pro cliente (Hugo, 12/09): confirmar antes de gravar,
  // deixando claro que não é uma anotação interna.
  const escolherMotivo = (id: number, texto: string) => {
    if (motivoId === id) return;
    Alert.alert(
      'Tem certeza do motivo?',
      `Você escolheu "${texto}".\n\nEsse motivo é enviado direto para o cliente, do jeito que está escrito. Confira se é mesmo o que aconteceu.`,
      [
        { text: 'Escolher outro', style: 'cancel' },
        { text: 'Sim, é esse', onPress: () => setMotivoId(id) },
      ],
      { cancelable: true },
    );
  };
  // "Descreva quem recebeu" só existe (e só é exigido) quando o vínculo
  // escolhido é "Outro" -- antes a validação cobrava um campo que a tela
  // nunca mostrava, travando a finalização.
  const pedeVinculoOutro = respostas.vinculo === 'Outro';

  const validar = (): string | null => {
    for (const c of campos) {
      if (!c.obrigatorio) continue;
      if (c.tipo === 'FOTO' && fotosDe(c.chave).length === 0) return `Falta: ${c.rotulo}`;
      if (c.tipo !== 'FOTO' && c.tipo !== 'DOCUMENTO' && !(respostas[c.chave] ?? '').trim()) return `Falta: ${c.rotulo}`;
    }
    if (pedeVinculoOutro && !(respostas.vinculo_outro ?? '').trim()) return 'Descreva quem recebeu.';
    if (exigeMotivo && motivos.length > 0 && motivoId === null) return `Escolha o ${rotuloMotivo.toLowerCase()}.`;
    return null;
  };

  const confirmarEntrega = async () => {
    const falta = validar();
    if (falta) return Alert.alert('Quase lá', falta);
    setEnviando(true);
    try {
      const pos = await gps.posicaoAtual();
      const agora = fila.agoraIso();
      const tipoEvento = fluxo === 'NAO_ENTREGUE' ? 'INSUCESSO' : fluxo;
      const situacao: SituacaoParada = tipoEvento === 'INSUCESSO' ? 'INSUCESSO' : tipoEvento === 'PARCIAL' ? 'PARCIAL' : 'ENTREGUE';
      await local.marcarParada(parada.id, situacao);
      const { vinculo_outro, ...demais } = respostas;
      const fotosQtd = Object.fromEntries(Object.entries(fotos).filter(([, lista]) => lista.length > 0).map(([chave, lista]) => [chave, lista.length]));
      await fila.enfileirar({
        uuid: fila.novoUuid(), tipo: 'EVENTO_PARADA', paradaId: parada.id, criadoEm: agora, tentativas: 0,
        corpo: { tipo: tipoEvento, ocorrido_em: agora, ...pos, motivo_id: motivoId, observacoes: respostas.observacoes ?? null,
                 checklist: { ...demais, ...(pedeVinculoOutro ? { vinculo_outro } : {}), fotos: Object.keys(fotosQtd), fotos_qtd: fotosQtd, assinatura: false } },
      });
      for (const [chave, lista] of Object.entries(fotos)) {
        for (const uri of lista) {
          await fila.enfileirar({ uuid: fila.novoUuid(), tipo: 'COMPROVANTE', paradaId: parada.id, uri, tipoComprovante: TIPO_COMPROVANTE[chaveBase(chave)] ?? 'CANHOTO', nf: nfDaChave(chave), capturadoEm: agora, criadoEm: agora, tentativas: 0 });
        }
      }
      router.back();
    } finally {
      setEnviando(false);
    }
  };

  const confirmarReagendamento = async () => {
    let novo: string;
    if (horaRetorno === 'FIM') novo = 'FIM';
    else {
      const hhmm = horaRetorno ?? horaManual.trim();
      if (!/^\d{2}:\d{2}$/.test(hhmm)) return Alert.alert('Horário', 'Escolha um horário (ou "Depois das outras").');
      novo = `${hoje()} ${hhmm}`;
    }
    setEnviando(true);
    try {
      const pos = await gps.posicaoAtual();
      const agora = fila.agoraIso();
      await local.marcarParada(parada.id, 'PENDENTE');
      await fila.enfileirar({
        uuid: fila.novoUuid(), tipo: 'EVENTO_PARADA', paradaId: parada.id, criadoEm: agora, tentativas: 0,
        corpo: { tipo: 'REAGENDAR', ocorrido_em: agora, ...pos, novo_horario: novo, observacoes: obsRetorno.trim() || null },
      });
      router.back();
    } finally {
      setEnviando(false);
    }
  };

  return (
    <ScrollView style={s.tela} contentContainerStyle={{ padding: 16, paddingBottom: 60 }} keyboardShouldPersistTaps="handled">
      <Cartao>
        <Text style={s.titulo}>{parada.ordem}. {parada.destinatario_nome || parada.titulo}</Text>
        <Text style={s.sub}>{parada.endereco}</Text>
        <Text style={s.sub}>{parada.codigo}{parada.volume_caixas ? ` · ${parada.volume_caixas} caixa(s)` : ''}{parada.tentativas > 0 ? ` · ${parada.tentativas}ª tentativa já feita` : ''}</Text>
      </Cartao>

      {!fluxo ? (
        <View style={{ gap: 12 }}>
          <Text style={s.instrucao}>Arraste para a direita a opção que corresponde ao resultado:</Text>
          <Deslizar titulo="Entregue" icone="checkmark" cor={cores.acento} onConfirmar={() => escolherComConfirmacao('ENTREGUE')} />
          <Deslizar titulo="Entregue parcial" icone="remove-circle" cor={cores.alerta} onConfirmar={() => escolherComConfirmacao('PARCIAL')} />
          <Deslizar titulo="Não entregue" icone="close" cor={cores.perigo} onConfirmar={() => escolherComConfirmacao('NAO_ENTREGUE')} />
          <Deslizar titulo="Reagendar (voltar depois)" icone="time" cor={cores.primaria} onConfirmar={() => { setFluxo('REAGENDAR'); }} />
        </View>
      ) : fluxo === 'REAGENDAR' ? (
        <>
          <Pressable onPress={() => setFluxo(null)}><Text style={s.trocar}>← voltar</Text></Pressable>
          <Cartao>
            <Text style={s.rotulo}>Quando você volta nesta entrega? *</Text>
            <View style={s.opcoes}>
              {[30, 60, 120, 180].map((min) => {
                const h = horaMaisMinutos(min);
                return (
                  <Pressable key={min} onPress={() => { setHoraRetorno(h); setHoraManual(''); }} style={[s.opcao, horaRetorno === h && s.opcaoAtiva]}>
                    <Text style={[s.opcaoTexto, horaRetorno === h && { color: '#fff' }]}>{min < 60 ? `em ${min} min` : `em ${min / 60}h`} ({h})</Text>
                  </Pressable>
                );
              })}
              <Pressable onPress={() => { setHoraRetorno('FIM'); setHoraManual(''); }} style={[s.opcao, horaRetorno === 'FIM' && s.opcaoAtiva]}>
                <Text style={[s.opcaoTexto, horaRetorno === 'FIM' && { color: '#fff' }]}>Depois das outras entregas</Text>
              </Pressable>
            </View>
            <Text style={[s.rotulo, { marginTop: 12 }]}>Ou digite o horário (HH:MM)</Text>
            <TextInput style={s.campo} placeholder="14:30" placeholderTextColor="#9CA3AF" keyboardType="numbers-and-punctuation" value={horaManual}
              onChangeText={(v) => { setHoraManual(v.replace(/[^\d:]/g, '').slice(0, 5)); setHoraRetorno(null); }} />
            <Text style={[s.rotulo, { marginTop: 12 }]}>Motivo / observação</Text>
            <TextInput style={s.campo} placeholder="Ex.: cliente pediu pra voltar depois do almoço" placeholderTextColor="#9CA3AF" value={obsRetorno} onChangeText={setObsRetorno} multiline />
          </Cartao>
          <Deslizar titulo="Confirmar reagendamento" icone="time" cor={cores.primaria} onConfirmar={confirmarReagendamento} desabilitado={enviando} />
        </>
      ) : (
        <>
          <Pressable onPress={() => setFluxo(null)}><Text style={s.trocar}>← trocar resultado ({TITULO_FLUXO[fluxo]})</Text></Pressable>
          {exigeMotivo && motivos.length > 0 ? (
            <Cartao>
              <Text style={s.rotulo}>{rotuloMotivo} *</Text>
              <Text style={[s.sub, { marginTop: 0, marginBottom: 8 }]}>O motivo escolhido aqui é mostrado para o cliente.</Text>
              <View style={s.opcoes}>
                {motivos.map((m) => (
                  <Pressable key={m.id} onPress={() => escolherMotivo(m.id, m.motivo_texto)} style={[s.opcao, motivoId === m.id && s.opcaoAtiva]}>
                    <Text style={[s.opcaoTexto, motivoId === m.id && { color: '#fff' }]}>{m.motivo_texto}</Text>
                  </Pressable>
                ))}
              </View>
            </Cartao>
          ) : null}
          {campos.map((c) => (
            <Cartao key={c.chave}>
              <Text style={s.rotulo}>{c.rotulo}{c.obrigatorio ? ' *' : ''}</Text>
              {c.aviso ? <Text style={s.aviso}>{c.aviso}</Text> : null}
              {c.tipo === 'SELECAO' && c.opcoes ? (
                <View style={s.opcoes}>
                  {c.opcoes.map((op) => (
                    <Pressable key={op} onPress={() => setRespostas((r) => ({ ...r, [c.chave]: op }))} style={[s.opcao, respostas[c.chave] === op && s.opcaoAtiva]}>
                      <Text style={[s.opcaoTexto, respostas[c.chave] === op && { color: '#fff' }]}>{op}</Text>
                    </Pressable>
                  ))}
                </View>
              ) : null}
              {c.chave === 'vinculo' && pedeVinculoOutro ? (
                <>
                  <Text style={[s.rotulo, { marginTop: 12 }]}>Descreva quem recebeu *</Text>
                  <TextInput style={s.campo} placeholder="Ex.: vizinho, funcionário da loja" placeholderTextColor="#9CA3AF" value={respostas.vinculo_outro ?? ''}
                    onChangeText={(v) => setRespostas((r) => ({ ...r, vinculo_outro: v }))} />
                </>
              ) : null}
              {c.tipo === 'TEXTO' || c.tipo === 'NUMERO' ? (
                <TextInput style={s.campo} value={respostas[c.chave] ?? ''} onChangeText={(v) => setRespostas((r) => ({ ...r, [c.chave]: v }))}
                  keyboardType={c.tipo === 'NUMERO' ? 'numeric' : 'default'} multiline={c.chave === 'observacoes'} />
              ) : null}
              {c.tipo === 'DOCUMENTO' ? (
                <>
                  <TextInput style={s.campo} placeholder="Número do documento (RG/CPF)" placeholderTextColor="#9CA3AF" value={respostas[c.chave] ?? ''} onChangeText={(v) => setRespostas((r) => ({ ...r, [c.chave]: v }))} />
                  <Botao titulo={fotosDe(c.chave).length > 0 ? 'Foto do documento ✓ (refazer)' : 'Fotografar documento'} tipo="secundario" onPress={() => tirarFoto(c.chave, true)} estilo={{ marginTop: 8 }} />
                </>
              ) : null}
              {c.tipo === 'FOTO' ? (
                <>
                  {fotosDe(c.chave).map((uri, i) => (
                    <View key={uri} style={s.fotoBloco}>
                      <Image source={{ uri }} style={s.foto} />
                      <Pressable onPress={() => removerFoto(c.chave, i)} style={s.fotoRemover} hitSlop={8}>
                        <Text style={s.fotoRemoverTexto}>✕ remover</Text>
                      </Pressable>
                    </View>
                  ))}
                  {fotosDe(c.chave).length > 0 ? <Text style={s.sub}>{fotosDe(c.chave).length} foto(s). Pode tirar mais (frente e verso, outro comprovante...).</Text> : null}
                  {avisosFoto[c.chave] ? <Text style={s.aviso}>{avisosFoto[c.chave]}</Text> : null}
                  <Botao
                    titulo={conferindo === c.chave ? 'Conferindo a foto…' : fotosDe(c.chave).length > 0 ? '📷  Tirar mais uma foto' : '📷  Tirar foto'}
                    tipo={fotosDe(c.chave).length > 0 ? 'secundario' : 'primario'}
                    carregando={conferindo === c.chave}
                    desabilitado={conferindo !== null}
                    onPress={() => tirarFoto(c.chave)}
                    estilo={fotosDe(c.chave).length > 0 ? { marginTop: 8 } : undefined}
                  />
                </>
              ) : null}
            </Cartao>
          ))}
          <Deslizar titulo={`Confirmar: ${TITULO_FLUXO[fluxo]}`} icone="checkmark-done" cor={fluxo === 'NAO_ENTREGUE' ? cores.perigo : fluxo === 'PARCIAL' ? cores.alerta : cores.acento} onConfirmar={confirmarEntrega} desabilitado={enviando} />
        </>
      )}
    </ScrollView>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.fundo },
  titulo: { fontSize: 18, fontWeight: '800', color: cores.texto },
  sub: { color: cores.textoSuave, marginTop: 4 },
  instrucao: { color: cores.textoSuave, marginBottom: 4, textAlign: 'center' },
  trocar: { color: cores.info, fontWeight: '600', marginBottom: 10 },
  rotulo: { fontWeight: '700', color: cores.texto, marginBottom: 8, fontSize: 15 },
  aviso: { color: cores.alerta, fontSize: 12, marginBottom: 8 },
  campo: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 10, padding: 12, fontSize: 16, color: cores.texto },
  opcoes: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  opcao: { paddingVertical: 10, paddingHorizontal: 14, borderRadius: 999, backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda },
  opcaoAtiva: { backgroundColor: cores.primaria, borderColor: cores.primaria },
  opcaoTexto: { color: cores.texto, fontWeight: '600' },
  foto: { width: '100%', height: 200, borderRadius: 10, marginBottom: 10, backgroundColor: cores.borda },
  fotoBloco: { position: 'relative' },
  fotoRemover: { position: 'absolute', top: 8, right: 8, backgroundColor: 'rgba(0,0,0,0.65)', paddingVertical: 6, paddingHorizontal: 10, borderRadius: 999 },
  fotoRemoverTexto: { color: '#fff', fontWeight: '700', fontSize: 12 },
});
