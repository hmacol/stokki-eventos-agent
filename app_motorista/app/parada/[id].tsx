// Registro do resultado da parada: Entregue / Parcial / Insucesso, com o
// checklist configurável (checklist_modelo), fotos (câmera), assinatura
// na tela e motivo (motivos_ocorrencia). Tudo entra na fila offline.
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
import { Botao, Cartao, Carregando } from '../../src/componentes';
import { cores } from '../../src/tema';
import type { CampoChecklist, Checklist, Parada, SituacaoParada } from '../../src/tipos';

type Fluxo = 'ENTREGUE' | 'PARCIAL' | 'NAO_ENTREGUE';
const CHAVE_CHECKLIST = 'motorista.checklist.v1';

// Fallback se a API do checklist não estiver alcançável na 1ª vez
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
  const [chegou, setChegou] = useState(false);

  const carregar = useCallback(async () => {
    const r = await carregarRotas();
    const rota = r.rotas.find((x) => x.id === rotaId);
    setParada(rota?.paradas.find((p) => p.id === paradaId) ?? null);
    setChecklist(await carregarChecklist());
  }, [rotaId, paradaId]);
  useEffect(() => { void carregar(); }, [carregar]);

  if (!parada || !checklist) return <Carregando />;

  const registrarChegada = async () => {
    const pos = await gps.posicaoAtual();
    await local.marcarParada(parada.id, 'EM_ROTA');
    await fila.enfileirar({ uuid: fila.novoUuid(), tipo: 'EVENTO_PARADA', paradaId: parada.id, corpo: { tipo: 'CHEGADA', ocorrido_em: fila.agoraIso(), ...pos }, criadoEm: fila.agoraIso(), tentativas: 0 });
    setChegou(true);
  };

  const tirarFoto = async (chave: string) => {
    const perm = await ImagePicker.requestCameraPermissionsAsync();
    if (!perm.granted) return Alert.alert('Câmera', 'Permita o uso da câmera pra fotografar o comprovante.');
    const r = await ImagePicker.launchCameraAsync({ quality: 0.7, allowsEditing: false, exif: false });
    if (!r.canceled && r.assets[0]) setFotos((f) => ({ ...f, [chave]: r.assets[0].uri }));
  };

  const campos: CampoChecklist[] = fluxo ? (checklist.fluxos[fluxo] ?? CHECKLIST_PADRAO.fluxos[fluxo]) : [];
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

  const confirmar = async () => {
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

  return (
    <ScrollView style={s.tela} contentContainerStyle={{ padding: 16, paddingBottom: 60 }}>
      <Cartao>
        <Text style={s.titulo}>{parada.ordem}. {parada.destinatario_nome || parada.titulo}</Text>
        <Text style={s.sub}>{parada.endereco}</Text>
        <Text style={s.sub}>{parada.codigo}{parada.volume_caixas ? ` · ${parada.volume_caixas} caixa(s)` : ''}</Text>
        {!chegou && (parada.situacao === 'PENDENTE' || parada.situacao === 'EM_DESLOCAMENTO') ? <Botao titulo="Cheguei no local" tipo="secundario" onPress={registrarChegada} estilo={{ marginTop: 10 }} /> : null}
      </Cartao>

      {!fluxo ? (
        <View style={{ gap: 10 }}>
          <Botao titulo="✅  Entregue" onPress={() => setFluxo('ENTREGUE')} />
          <Botao titulo="◐  Entrega parcial (com devolução)" tipo="alerta" onPress={() => setFluxo('PARCIAL')} />
          <Botao titulo="✖  Não entregue" tipo="perigo" onPress={() => setFluxo('NAO_ENTREGUE')} />
        </View>
      ) : (
        <>
          <Pressable onPress={() => setFluxo(null)}><Text style={s.trocar}>← trocar resultado</Text></Pressable>
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
                  <TextInput style={s.campo} placeholder="Número do documento (RG/CPF)" value={respostas[c.chave] ?? ''} onChangeText={(v) => setRespostas((r) => ({ ...r, [c.chave]: v }))} />
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
          <Botao titulo="Confirmar registro" onPress={confirmar} carregando={enviando} estilo={{ marginTop: 8 }} />
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
  trocar: { color: cores.info, fontWeight: '600', marginBottom: 10 },
  rotulo: { fontWeight: '700', color: cores.texto, marginBottom: 8, fontSize: 15 },
  aviso: { color: cores.alerta, fontSize: 12, marginBottom: 8 },
  campo: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 10, padding: 12, fontSize: 16 },
  opcoes: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  opcao: { paddingVertical: 10, paddingHorizontal: 14, borderRadius: 999, backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda },
  opcaoAtiva: { backgroundColor: cores.primaria, borderColor: cores.primaria },
  opcaoTexto: { color: cores.texto, fontWeight: '600' },
  foto: { width: '100%', height: 200, borderRadius: 10, marginBottom: 10, backgroundColor: cores.borda },
});
