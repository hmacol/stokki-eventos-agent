import React, { useState } from 'react';
import { Image, KeyboardAvoidingView, Platform, StyleSheet, Text, TextInput, View } from 'react-native';
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
          <View style={s.logoCaixa}>
            <Image source={require('../assets/logo_freshlog.png')} style={s.logo} resizeMode="contain" accessibilityLabel="FreshLog" />
          </View>
          <Text style={s.sub}>Motorista</Text>
        </View>
        <View style={s.form}>
          <Text style={s.rotulo}>CPF</Text>
          <TextInput
            style={s.campo} value={cpf} onChangeText={(v) => setCpf(mascaraCpf(v))} keyboardType="number-pad"
            placeholder="000.000.000-00" placeholderTextColor="#9CA3AF" autoComplete="off" textContentType="none" returnKeyType="next"
          />
          <Text style={s.rotulo}>PIN (6 dígitos)</Text>
          <TextInput
            style={s.campo} value={pin} onChangeText={(v) => setPin(v.replace(/\D/g, '').slice(0, 6))}
            keyboardType="number-pad" secureTextEntry placeholder="••••••" placeholderTextColor="#9CA3AF" onSubmitEditing={enviar} returnKeyType="go"
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
  topo: { alignItems: 'center', paddingTop: 40, paddingBottom: 28 },
  logoCaixa: { backgroundColor: '#fff', borderRadius: 20, paddingVertical: 18, paddingHorizontal: 28 },
  logo: { width: 210, height: 70 },
  sub: { fontSize: 18, color: cores.acentoLogo, marginTop: 14, fontWeight: '700', letterSpacing: 2, textTransform: 'uppercase' },
  form: { flex: 1, backgroundColor: cores.fundo, borderTopLeftRadius: 28, borderTopRightRadius: 28, padding: 24 },
  rotulo: { color: cores.textoSuave, marginTop: 16, marginBottom: 6, fontWeight: '600' },
  campo: { backgroundColor: '#fff', borderWidth: 1, borderColor: cores.borda, borderRadius: 12, padding: 16, fontSize: 20, letterSpacing: 1, color: cores.texto },
  erro: { color: cores.perigo, marginTop: 12, fontWeight: '600' },
  ajuda: { color: cores.textoSuave, textAlign: 'center', marginTop: 24 },
});
