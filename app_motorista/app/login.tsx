import React, { useState } from 'react';
import { KeyboardAvoidingView, Platform, StyleSheet, Text, TextInput, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { useSessao } from '../src/sessao';
import { Botao } from '../src/componentes';
import { ErroApi, ErroRede } from '../src/api';
import { cores } from '../src/tema';

function mascaraCpf(v: string): string {
  const d = v.replace(/\D/g, '').slice(0, 11);
  return d.replace(/(\d{3})(\d)/, '$1.$2').replace(/(\d{3})(\d)/, '$1.$2').replace(/(\d{3})(\d{1,2})$/, '$1-$2');
}

export default function Login() {
  const { entrar } = useSessao();
  const [cpf, setCpf] = useState('');
  const [pin, setPin] = useState('');
  const [erro, setErro] = useState<string | null>(null);
  const [carregando, setCarregando] = useState(false);

  const enviar = async () => {
    setErro(null);
    if (cpf.replace(/\D/g, '').length !== 11) return setErro('Digite o CPF completo.');
    if (pin.length !== 6) return setErro('O PIN tem 6 dígitos.');
    setCarregando(true);
    try {
      await entrar(cpf, pin);
    } catch (e) {
      if (e instanceof ErroRede) setErro('Sem conexão. Tente de novo quando tiver sinal.');
      else if (e instanceof ErroApi) setErro(e.message);
      else setErro('Não foi possível entrar.');
    } finally {
      setCarregando(false);
    }
  };

  return (
    <SafeAreaView style={s.tela}>
      <KeyboardAvoidingView behavior={Platform.OS === 'ios' ? 'padding' : undefined} style={{ flex: 1 }}>
        <View style={s.topo}>
          <Text style={s.marca}>FreshLog</Text>
          <Text style={s.sub}>Motorista</Text>
        </View>
        <View style={s.form}>
          <Text style={s.rotulo}>CPF</Text>
          <TextInput
            style={s.campo} value={cpf} onChangeText={(v) => setCpf(mascaraCpf(v))} keyboardType="number-pad"
            placeholder="000.000.000-00" autoComplete="off" textContentType="none" returnKeyType="next"
          />
          <Text style={s.rotulo}>PIN (6 dígitos)</Text>
          <TextInput
            style={s.campo} value={pin} onChangeText={(v) => setPin(v.replace(/\D/g, '').slice(0, 6))}
            keyboardType="number-pad" secureTextEntry placeholder="••••••" onSubmitEditing={enviar} returnKeyType="go"
          />
          {erro ? <Text style={s.erro}>{erro}</Text> : null}
          <Botao titulo="Entrar" onPress={enviar} carregando={carregando} estilo={{ marginTop: 16 }} />
          <Text style={s.ajuda}>Não tem PIN? Fale com a operação da FreshLog.</Text>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const s = StyleSheet.create({
  tela: { flex: 1, backgroundColor: cores.primaria },
  topo: { alignItems: 'center', paddingVertical: 48 },
  marca: { fontSize: 40, fontWeight: '900', color: '#fff', letterSpacing: 1 },
  sub: { fontSize: 18, color: '#DFF3E8', marginTop: 4 },
  form: { flex: 1, backgroundColor: cores.fundo, borderTopLeftRadius: 28, borderTopRightRadius: 28, padding: 24 },
  rotulo: { color: cores.textoSuave, marginTop: 16, marginBottom: 6, fontWeight: '600' },
  campo: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 12, padding: 16, fontSize: 20, letterSpacing: 1 },
  erro: { color: cores.perigo, marginTop: 12, fontWeight: '600' },
  ajuda: { color: cores.textoSuave, textAlign: 'center', marginTop: 24 },
});
