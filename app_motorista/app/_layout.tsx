import React, { useEffect } from 'react';
import { AppState } from 'react-native';
import { Stack, useRouter, useSegments } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import * as Notifications from 'expo-notifications';
import * as Device from 'expo-device';
import Constants from 'expo-constants';
import { ProvedorSessao, useSessao } from '../src/sessao';
import * as api from '../src/api';
import * as fila from '../src/fila';
import { cores } from '../src/tema';

Notifications.setNotificationHandler({
  handleNotification: async () => ({ shouldShowBanner: true, shouldShowList: true, shouldPlaySound: true, shouldSetBadge: false }),
});

async function registrarPush() {
  try {
    if (!Device.isDevice) return;
    const { status } = await Notifications.requestPermissionsAsync();
    if (status !== 'granted') return;
    const projectId = (Constants.expoConfig?.extra as { eas?: { projectId?: string } } | undefined)?.eas?.projectId;
    if (!projectId) return; // só depois de `eas init` (Fase B, contas das lojas)
    const token = (await Notifications.getExpoPushTokenAsync({ projectId })).data;
    await api.registrarPushToken(token);
  } catch {
    // push é conveniência; nunca bloqueia o app
  }
}

function Guarda() {
  const { pronto, motorista } = useSessao();
  const router = useRouter();
  const segments = useSegments();

  useEffect(() => {
    if (!pronto) return;
    const emLogin = segments[0] === 'login';
    if (!motorista && !emLogin) router.replace('/login');
    if (motorista && emLogin) router.replace('/(tabs)/rotas');
  }, [pronto, motorista, segments, router]);

  useEffect(() => {
    if (!motorista) return;
    void fila.processar();
    void registrarPush();
    const sub = AppState.addEventListener('change', (st) => {
      if (st === 'active') void fila.processar();
    });
    const timer = setInterval(() => void fila.processar(), 60000);
    return () => {
      sub.remove();
      clearInterval(timer);
    };
  }, [motorista]);

  return (
    <Stack screenOptions={{ headerStyle: { backgroundColor: cores.primaria }, headerTintColor: '#fff', headerTitleStyle: { fontWeight: '700' } }}>
      <Stack.Screen name="index" options={{ headerShown: false }} />
      <Stack.Screen name="login" options={{ headerShown: false }} />
      <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
      <Stack.Screen name="rota/[id]" options={{ title: 'Rota' }} />
      <Stack.Screen name="parada/[id]" options={{ title: 'Parada' }} />
    </Stack>
  );
}

export default function Layout() {
  return (
    <ProvedorSessao>
      <StatusBar style="light" />
      <Guarda />
    </ProvedorSessao>
  );
}
