// Auto-cadastro de motorista novo (Hugo, 08/10): sem login. Dados + tipo
// de veículo + zonas e dias + foto da CNH e do CRLV. Vai pra fila do
// painel; o Hugo aprova escolhendo o agente Vuupt e manda o PIN no
// WhatsApp do motorista.
import React, { useState } from 'react';
import { Alert, Image, KeyboardAvoidingView, Pressable, ScrollView, StyleSheet, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import * as ImagePicker from 'expo-image-picker';
import * as api from '../src/api';
import { Botao } from '../src/componentes';
import { conferirPermissao } from '../src/permissoes';
import { DIAS, TIPOS_VEICULO, ZONAS, mascaraCpf, mascaraTelefone, placaValida } from '../src/cadastroOpcoes';
import { cores } from '../src/tema';

type Doc = 'cnh' | 'crlv';

function Chip({ rotulo, ativo, onPress }: { rotulo: string; ativo: boolean; onPress: () => void }) {
  return (
    <Pressable onPress={onPress} style={[s.chip, ativo && s.chipAtivo]} accessibilityRole="button" accessibilityState={{ selected: ativo }}>
      <Text style={[s.chipTexto, ativo && { color: '#fff' }]}>{rotulo}</Text>
    </Pressable>
  );
}

export default function Cadastro() {
  const router = useRouter();
  const [nome, setNome] = useState('');
  const [cpf, setCpf] = useState('');
  const [telefone, setTelefone] = useState('');
  const [email, setEmail] = useState('');
  const [pix, setPix] = useState('');
  const [placa, setPlaca] = useState('');
  const [tipo, setTipo] = useState('FIORINO');
  const [zonas, setZonas] = useState<string[]>([]);
  const [dias, setDias] = useState<string[]>(['SEGUNDA', 'TERCA', 'QUARTA', 'QUINTA', 'SEXTA']);
  const [viagens, setViagens] = useState(false);
  const [dono, setDono] = useState(true);
  const [fotos, setFotos] = useState<Record<Doc, string | null>>({ cnh: null, crlv: null });
  const [erro, setErro] = useState<string | null>(null);
  const [enviando, setEnviando] = useState(false);
  const [criado, setCriado] = useState<{ id: number; chave: string } | null>(null);
  const [feito, setFeito] = useState(false);

  const alternar = (lista: string[], valor: string, set: (v: string[]) => void) =>
    set(lista.includes(valor) ? lista.filter((x) => x !== valor) : [...lista, valor]);

  const pegarFoto = (doc: Doc) => {
    const guardar = (r: ImagePicker.ImagePickerResult) => {
      if (!r.canceled && r.assets[0]) setFotos((f) => ({ ...f, [doc]: r.assets[0].uri }));
    };
    Alert.alert(doc === 'cnh' ? 'Foto da CNH' : 'Foto do CRLV', 'De onde?', [
      { text: 'Câmera', onPress: async () => {
        if (!conferirPermissao(await ImagePicker.requestCameraPermissionsAsync(), 'Câmera', 'Permita a câmera pra fotografar o documento.')) return;
        guardar(await ImagePicker.launchCameraAsync({ quality: 0.7, allowsEditing: false, exif: false }));
      } },
      { text: 'Galeria', onPress: async () => guardar(await ImagePicker.launchImageLibraryAsync({ quality: 0.7, mediaTypes: ['images'], exif: false })) },
      { text: 'Cancelar', style: 'cancel' },
    ]);
  };

  const enviarDocumentos = async (id: number, chave: string) => {
    for (const doc of ['cnh', 'crlv'] as Doc[]) {
      const uri = fotos[doc];
      if (uri) await api.enviarDocumentoCadastro(id, chave, doc, uri);
    }
  };

  const enviar = async () => {
    setErro(null);
    if (nome.trim().split(/\s+/).length < 2) return setErro('Escreva o nome completo.');
    if (cpf.replace(/\D/g, '').length !== 11) return setErro('CPF incompleto.');
    const tel = telefone.replace(/\D/g, '');
    if (tel.length < 10) return setErro('Telefone com DDD (é por ele que mandamos o PIN).');
    if (email.trim() && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim())) return setErro('E-mail inválido.');
    if (!pix.trim()) return setErro('Informe a chave PIX pra receber.');
    if (!placaValida(placa)) return setErro('Placa no formato ABC1234 ou ABC1D23.');
    if (!zonas.length) return setErro('Marque pelo menos uma zona que você atende.');
    if (!dias.length) return setErro('Marque pelo menos um dia.');
    if (!fotos.cnh || !fotos.crlv) return setErro('Falta a foto da CNH ou do CRLV.');
    setEnviando(true);
    try {
      let atual = criado;
      if (!atual) {
        const r = await api.criarCadastro({
          cpf: cpf.replace(/\D/g, ''), nome: nome.trim(), telefone: tel, email: email.trim(), chave_pix: pix.trim(),
          placa: placa.toUpperCase(), tipo_veiculo: tipo, zonas, dias, aceita_viagens: viagens, dono_veiculo: dono,
        });
        atual = { id: r.id, chave: r.chave_envio };
        setCriado(atual);
      }
      await enviarDocumentos(atual.id, atual.chave);
      setFeito(true);
    } catch (e) {
      if (e instanceof api.ErroRede) setErro('Sem conexão. Toque em Enviar de novo quando tiver sinal.');
      else setErro((e as Error).message);
    } finally {
      setEnviando(false);
    }
  };

  if (feito) {
    return (
      <SafeAreaView style={s.tela}>
        <View style={{ padding: 24, gap: 14 }}>
          <Text style={s.titulo}>Recebemos seu cadastro</Text>
          <Text style={s.texto}>A Fresh Log vai avaliar e, se aprovar, manda o PIN de acesso no seu WhatsApp ({mascaraTelefone(telefone)}).</Text>
          <Botao titulo="Voltar pro início" onPress={() => router.replace('/login')} />
        </View>
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={s.tela}>
      <KeyboardAvoidingView behavior="padding" style={{ flex: 1 }}>
        <ScrollView contentContainerStyle={{ padding: 20, paddingBottom: 40 }} keyboardShouldPersistTaps="handled">
          <Text style={s.titulo}>Quero ser motorista da Fresh Log</Text>
          <Text style={s.texto}>Preencha e mande a foto da CNH e do documento do veículo (CRLV). A operação avalia e libera seu acesso.</Text>

          <Text style={s.rotulo}>Nome completo</Text>
          <TextInput style={s.campo} value={nome} onChangeText={setNome} autoCapitalize="words" placeholder="Como está na CNH" placeholderTextColor="#9CA3AF" />
          <Text style={s.rotulo}>CPF</Text>
          <TextInput style={s.campo} value={cpf} onChangeText={(v) => setCpf(mascaraCpf(v))} keyboardType="number-pad" placeholder="000.000.000-00" placeholderTextColor="#9CA3AF" />
          <Text style={s.rotulo}>Telefone (WhatsApp)</Text>
          <TextInput style={s.campo} value={telefone} onChangeText={(v) => setTelefone(mascaraTelefone(v))} keyboardType="phone-pad" placeholder="(11) 90000-0000" placeholderTextColor="#9CA3AF" />
          <Text style={s.rotulo}>E-mail (opcional)</Text>
          <TextInput style={s.campo} value={email} onChangeText={setEmail} keyboardType="email-address" autoCapitalize="none" placeholderTextColor="#9CA3AF" />
          <Text style={s.rotulo}>Chave PIX</Text>
          <TextInput style={s.campo} value={pix} onChangeText={setPix} autoCapitalize="none" placeholder="CPF, telefone, e-mail ou chave aleatória" placeholderTextColor="#9CA3AF" />
          <Text style={s.rotulo}>Placa do veículo</Text>
          <TextInput style={s.campo} value={placa} onChangeText={(v) => setPlaca(v.toUpperCase().slice(0, 8))} autoCapitalize="characters" placeholder="ABC1D23" placeholderTextColor="#9CA3AF" />

          <Text style={s.rotulo}>O veículo é seu?</Text>
          <View style={s.chips}>
            <Chip rotulo="Sim, é meu" ativo={dono} onPress={() => setDono(true)} />
            <Chip rotulo="Não, dirijo pra outra pessoa" ativo={!dono} onPress={() => setDono(false)} />
          </View>
          <Text style={s.rotulo}>Tipo de veículo</Text>
          <View style={s.chips}>{TIPOS_VEICULO.map((t) => <Chip key={t.codigo} rotulo={t.nome} ativo={tipo === t.codigo} onPress={() => setTipo(t.codigo)} />)}</View>
          <Text style={s.rotulo}>Zonas que você atende</Text>
          <View style={s.chips}>{ZONAS.map((z) => <Chip key={z} rotulo={z} ativo={zonas.includes(z)} onPress={() => alternar(zonas, z, setZonas)} />)}</View>
          <Text style={s.rotulo}>Dias que você roda</Text>
          <View style={s.chips}>{DIAS.map((d) => <Chip key={d.codigo} rotulo={d.rotulo} ativo={dias.includes(d.codigo)} onPress={() => alternar(dias, d.codigo, setDias)} />)}</View>
          <View style={[s.chips, { marginTop: 10 }]}><Chip rotulo={viagens ? '✓ Aceito viagens fora de SP' : 'Aceito viagens fora de SP'} ativo={viagens} onPress={() => setViagens(!viagens)} /></View>

          <Text style={s.rotulo}>Documentos</Text>
          <View style={s.docs}>
            {(['cnh', 'crlv'] as Doc[]).map((doc) => (
              <Pressable key={doc} onPress={() => pegarFoto(doc)} style={s.doc}>
                {fotos[doc] ? <Image source={{ uri: fotos[doc]! }} style={s.docFoto} /> : <Text style={s.docMais}>＋</Text>}
                <Text style={s.docTexto}>{doc === 'cnh' ? 'Foto da CNH' : 'Foto do CRLV'}</Text>
              </Pressable>
            ))}
          </View>

          {erro ? <Text style={s.erro}>{erro}</Text> : null}
          <Botao titulo={criado ? 'Enviar documentos' : 'Enviar cadastro'} onPress={() => void enviar()} carregando={enviando} estilo={{ marginTop: 20 }} />
          <Botao titulo="Já tenho PIN" tipo="secundario" onPress={() => router.replace('/login')} estilo={{ marginTop: 10 }} />
        </ScrollView>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.fundo },
  titulo: { fontSize: 22, fontWeight: '800', color: cores.texto },
  texto: { color: cores.textoSuave, marginTop: 8, lineHeight: 20 },
  rotulo: { color: cores.textoSuave, marginTop: 16, marginBottom: 6, fontWeight: '600' },
  campo: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 12, padding: 14, fontSize: 17, color: cores.texto },
  chips: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  chip: { borderWidth: 1, borderColor: cores.borda, backgroundColor: cores.cartao, borderRadius: 999, paddingHorizontal: 14, paddingVertical: 10 },
  chipAtivo: { backgroundColor: cores.primaria, borderColor: cores.primaria },
  chipTexto: { color: cores.texto, fontWeight: '600', fontSize: 14 },
  docs: { flexDirection: 'row', gap: 12 },
  doc: { flex: 1, minHeight: 120, borderWidth: 1, borderColor: cores.borda, borderRadius: 12, backgroundColor: cores.cartao, alignItems: 'center', justifyContent: 'center', overflow: 'hidden', gap: 6, padding: 8 },
  docFoto: { width: '100%', height: 90, borderRadius: 8 },
  docMais: { fontSize: 34, color: cores.acento, fontWeight: '800' },
  docTexto: { color: cores.texto, fontWeight: '600', fontSize: 13 },
  erro: { color: cores.perigo, marginTop: 12, fontWeight: '600' },
});
