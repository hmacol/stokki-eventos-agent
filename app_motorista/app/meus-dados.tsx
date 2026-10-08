// "Meus dados" (Hugo, 08/10): telefone, e-mail, chave PIX e placa. Vale na
// hora; a logística recebe um aviso. Zonas, dias e veículo continuam com a
// operação (painel).
import React, { useState } from 'react';
import { Alert, KeyboardAvoidingView, ScrollView, StyleSheet, Text, TextInput } from 'react-native';
import { useRouter } from 'expo-router';
import * as api from '../src/api';
import { useSessao } from '../src/sessao';
import { Botao } from '../src/componentes';
import { mascaraTelefone, placaValida } from '../src/cadastroOpcoes';
import { cores } from '../src/tema';

export default function MeusDados() {
  const { motorista, atualizarMotorista } = useSessao();
  const router = useRouter();
  const [telefone, setTelefone] = useState(mascaraTelefone(motorista?.telefone ?? ''));
  const [email, setEmail] = useState(motorista?.email ?? '');
  const [pix, setPix] = useState(motorista?.chave_pix ?? '');
  const [placa, setPlaca] = useState(motorista?.placa ?? '');
  const [erro, setErro] = useState<string | null>(null);
  const [salvando, setSalvando] = useState(false);

  const salvar = async () => {
    setErro(null);
    const tel = telefone.replace(/\D/g, '');
    if (tel && (tel.length < 10 || tel.length > 11)) return setErro('Telefone com DDD, 10 ou 11 números.');
    if (email.trim() && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email.trim())) return setErro('E-mail inválido.');
    if (placa.trim() && !placaValida(placa)) return setErro('Placa no formato ABC1234 ou ABC1D23.');
    setSalvando(true);
    try {
      const r = await api.atualizarMeusDados({ telefone: tel, email: email.trim(), chave_pix: pix.trim(), placa: placa.trim().toUpperCase() });
      atualizarMotorista(r.motorista);
      Alert.alert('Salvo', r.mudancas.length ? `Alterado: ${r.mudancas.map((m) => m.rotulo).join(', ')}.` : 'Nada mudou.');
      router.back();
    } catch (e) {
      if (e instanceof api.ErroRede) setErro('Sem conexão. Tente de novo com sinal.');
      else setErro((e as Error).message);
    } finally {
      setSalvando(false);
    }
  };

  return (
    <KeyboardAvoidingView behavior="padding" style={s.tela}>
      <ScrollView contentContainerStyle={{ padding: 20, paddingBottom: 40 }} keyboardShouldPersistTaps="handled">
        <Text style={s.texto}>Esses dados valem na hora. Zonas, dias e tipo de veículo: fale com a operação.</Text>
        <Text style={s.rotulo}>Telefone (WhatsApp)</Text>
        <TextInput style={s.campo} value={telefone} onChangeText={(v) => setTelefone(mascaraTelefone(v))} keyboardType="phone-pad" placeholder="(11) 90000-0000" placeholderTextColor="#9CA3AF" />
        <Text style={s.rotulo}>E-mail</Text>
        <TextInput style={s.campo} value={email} onChangeText={setEmail} keyboardType="email-address" autoCapitalize="none" placeholder="opcional" placeholderTextColor="#9CA3AF" />
        <Text style={s.rotulo}>Chave PIX</Text>
        <TextInput style={s.campo} value={pix} onChangeText={setPix} autoCapitalize="none" placeholder="CPF, telefone, e-mail ou chave aleatória" placeholderTextColor="#9CA3AF" />
        <Text style={s.rotulo}>Placa do veículo</Text>
        <TextInput style={s.campo} value={placa} onChangeText={(v) => setPlaca(v.toUpperCase().slice(0, 8))} autoCapitalize="characters" placeholder="ABC1D23" placeholderTextColor="#9CA3AF" />
        {erro ? <Text style={s.erro}>{erro}</Text> : null}
        <Botao titulo="Salvar" onPress={() => void salvar()} carregando={salvando} estilo={{ marginTop: 20 }} />
        <Botao titulo="Voltar" tipo="secundario" onPress={() => router.back()} estilo={{ marginTop: 10 }} />
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.fundo },
  texto: { color: cores.textoSuave, lineHeight: 20 },
  rotulo: { color: cores.textoSuave, marginTop: 16, marginBottom: 6, fontWeight: '600' },
  campo: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 12, padding: 14, fontSize: 17, color: cores.texto },
  erro: { color: cores.perigo, marginTop: 12, fontWeight: '600' },
});
