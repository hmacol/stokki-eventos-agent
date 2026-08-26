// Resultado da parada (Hugo, 26/08): quatro ações, todas por ARRASTAR pra
// direita -- Entregue / Entregue parcial / Não entregue / Reagendar. As
// três de entrega perguntam "tem certeza?" antes de abrir o checklist
// (checklist_modelo); fotos pela câmera, assinatura na tela, motivo do
// motivos_ocorrencia. Reagendar marca um horário de retorno pra mesma
// entrega (a parada volta pra pendente). Tudo entra na fila offline.
import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Image, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import * as ImagePicker from 'expo-image-picker';
import { File, Paths } from 'expo-file-system';
import AsyncStorage from '@react-native-async-storage/async-storage';
import * as api from '../../src/api';
import * as fila from '../../src/fila';
import * as gps from '../../src/gps';
import * as local from '../../src/local';
import { carregarRotas } from '../../src/rotasStore';
import { ModalAssinatura } from '../../src/assinatura';
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

async function salvarBase64(dataUrl: string, nome: string): Promise<string> {
  const base64 = dataUrl.split(',')[1] ?? dataUrl;
  const arquivo = new File(Paths.cache, nome);
  arquivo.write(base64ParaBytes(base64));
  return arquivo.uri;
}

function base64ParaBytes(b64: string): Uint8Array {
  const alfabeto = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
  const limpo = b64.replace(/[^A-Za-z0-9+/]/g, '');
  const saida = new Uint8Array(Math.floor((limpo.length * 3) / 4));
  let acumulado = 0, bits = 0, i = 0;
  for (const ch of limpo) {
    acumulado = (acumulado << 6) | alfabeto.indexOf(ch);
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      saida[i++] = (acumulado >> bits) & 0xff;
    }
  }
  return saida.slice(0, i);
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
  const [fotos, setFotos] = useState<Record<string, string>>({});
  const [assinatura, setAssinatura] = useState<string | null>(null);
  const [motivoId, setMotivoId] = useState<number | null>(null);
  const [assinando, setAssinando] = useState(false);
  const [enviando, setEnviando] = useState(false);
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

  const tirarFoto = async (chave: string) => {
    const perm = await ImagePicker.requestCameraPermissionsAsync();
    if (!perm.granted) return Alert.alert('Câmera', 'Permita o uso da câmera pra fotografar o comprovante.');
    const r = await ImagePicker.launchCameraAsync({ quality: 0.7, allowsEditing: false, exif: false });
    if (!r.canceled && r.assets[0]) setFotos((f) => ({ ...f, [chave]: r.assets[0].uri }));
  };

  const campos: CampoChecklist[] = fluxo && fluxo !== 'REAGENDAR' ? (checklist.fluxos[fluxo] ?? CHECKLIST_PADRAO.fluxos[fluxo]) : [];
  const exigeMotivo = fluxo === 'NAO_ENTREGUE' || fluxo === 'PARCIAL';
  const motivos = checklist.motivos;

  const validar = (): string | null => {
    for (const c of campos) {
      if (!c.obrigatorio) continue;
      if (c.tipo === 'FOTO' && !fotos[c.chave]) return `Falta: ${c.rotulo}`;
      if (c.tipo !== 'FOTO' && c.tipo !== 'DOCUMENTO' && !(respostas[c.chave] ?? '').trim()) return `Falta: ${c.rotulo}`;
    }
    if (respostas.vinculo === 'Outro' && !(respostas.vinculo_outro ?? '').trim()) return 'Descreva quem recebeu.';
    if (exigeMotivo && motivos.length > 0 && motivoId === null) return 'Escolha o motivo.';
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
      await fila.enfileirar({
        uuid: fila.novoUuid(), tipo: 'EVENTO_PARADA', paradaId: parada.id, criadoEm: agora, tentativas: 0,
        corpo: { tipo: tipoEvento, ocorrido_em: agora, ...pos, motivo_id: motivoId, observacoes: respostas.observacoes ?? null,
                 checklist: { ...respostas, fotos: Object.keys(fotos), assinatura: !!assinatura } },
      });
      for (const [chave, uri] of Object.entries(fotos)) {
        await fila.enfileirar({ uuid: fila.novoUuid(), tipo: 'COMPROVANTE', paradaId: parada.id, uri, tipoComprovante: TIPO_COMPROVANTE[chave] ?? 'CANHOTO', capturadoEm: agora, criadoEm: agora, tentativas: 0 });
      }
      if (assinatura) {
        const uri = await salvarBase64(assinatura, `assinatura_${parada.id}_${Date.now()}.png`);
        await fila.enfileirar({ uuid: fila.novoUuid(), tipo: 'COMPROVANTE', paradaId: parada.id, uri, tipoComprovante: 'ASSINATURA', capturadoEm: agora, criadoEm: agora, tentativas: 0 });
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
              <Text style={s.rotulo}>Motivo *</Text>
              <View style={s.opcoes}>
                {motivos.map((m) => (
                  <Pressable key={m.id} onPress={() => setMotivoId(m.id)} style={[s.opcao, motivoId === m.id && s.opcaoAtiva]}>
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
              {c.tipo === 'SELECAO' && !c.opcoes && c.chave.startsWith('motivo') ? <Text style={s.sub}>(usa o motivo escolhido acima)</Text> : null}
              {c.tipo === 'TEXTO' || c.tipo === 'NUMERO' ? (
                <TextInput style={s.campo} value={respostas[c.chave] ?? ''} onChangeText={(v) => setRespostas((r) => ({ ...r, [c.chave]: v }))}
                  keyboardType={c.tipo === 'NUMERO' ? 'numeric' : 'default'} multiline={c.chave === 'observacoes'} />
              ) : null}
              {c.tipo === 'DOCUMENTO' ? (
                <>
                  <TextInput style={s.campo} placeholder="Número do documento (RG/CPF)" placeholderTextColor="#9CA3AF" value={respostas[c.chave] ?? ''} onChangeText={(v) => setRespostas((r) => ({ ...r, [c.chave]: v }))} />
                  <Botao titulo={fotos[c.chave] ? 'Foto do documento ✓ (refazer)' : 'Fotografar documento'} tipo="secundario" onPress={() => tirarFoto(c.chave)} estilo={{ marginTop: 8 }} />
                </>
              ) : null}
              {c.tipo === 'FOTO' ? (
                <>
                  {fotos[c.chave] ? <Image source={{ uri: fotos[c.chave] }} style={s.foto} /> : null}
                  <Botao titulo={fotos[c.chave] ? 'Tirar outra' : '📷  Tirar foto'} tipo={fotos[c.chave] ? 'secundario' : 'primario'} onPress={() => tirarFoto(c.chave)} />
                </>
              ) : null}
            </Cartao>
          ))}
          {fluxo !== 'NAO_ENTREGUE' ? (
            <Cartao>
              <Text style={s.rotulo}>Assinatura de quem recebeu</Text>
              {assinatura ? <Image source={{ uri: assinatura }} style={[s.foto, { height: 120, backgroundColor: '#fff' }]} resizeMode="contain" /> : null}
              <Botao titulo={assinatura ? 'Assinar de novo' : '✍  Coletar assinatura'} tipo="secundario" onPress={() => setAssinando(true)} />
            </Cartao>
          ) : null}
          <Deslizar titulo={`Confirmar: ${TITULO_FLUXO[fluxo]}`} icone="checkmark-done" cor={fluxo === 'NAO_ENTREGUE' ? cores.perigo : fluxo === 'PARCIAL' ? cores.alerta : cores.acento} onConfirmar={confirmarEntrega} desabilitado={enviando} />
        </>
      )}
      <ModalAssinatura visivel={assinando} onFechar={() => setAssinando(false)} onAssinou={(d) => { setAssinatura(d); setAssinando(false); }} />
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
});
