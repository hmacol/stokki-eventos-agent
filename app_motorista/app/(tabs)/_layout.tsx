import React from 'react';
import type { ColorValue } from 'react-native';
import { Tabs } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';
import { cores } from '../../src/tema';

type Icone = React.ComponentProps<typeof Ionicons>['name'];
const icone = (nome: Icone) => ({ color, size }: { color: ColorValue; size: number }) => <Ionicons name={nome} color={color} size={size} />;

export default function Abas() {
  return (
    <Tabs
      screenOptions={{
        headerStyle: { backgroundColor: cores.primaria }, headerTintColor: '#fff', headerTitleStyle: { fontWeight: '700' },
        tabBarActiveTintColor: cores.acento, tabBarInactiveTintColor: cores.textoSuave,
        tabBarLabelStyle: { fontSize: 12, fontWeight: '600' }, tabBarStyle: { height: 64, paddingBottom: 8, paddingTop: 6 },
      }}
    >
      <Tabs.Screen name="rotas" options={{ title: 'Rotas', tabBarIcon: icone('map') }} />
      <Tabs.Screen name="ofertas" options={{ title: 'Ofertas', tabBarIcon: icone('hand-right') }} />
      <Tabs.Screen name="financeiro" options={{ title: 'Financeiro', tabBarIcon: icone('cash') }} />
      <Tabs.Screen name="disponibilidade" options={{ title: 'Agenda', tabBarIcon: icone('calendar') }} />
      <Tabs.Screen name="perfil" options={{ title: 'Perfil', tabBarIcon: icone('person') }} />
    </Tabs>
  );
}
