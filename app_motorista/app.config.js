// A chave do Google Maps (Android) vem da variavel do EAS GOOGLE_MAPS_ANDROID_KEY; nunca vai pro git.
// Sem a chave o plugin remove o meta-data e o app quebra ao abrir o mapa: no worker do EAS o build falha.
// (expo start / expo config / eas update seguem funcionando sem a chave.)
if (process.env.EAS_BUILD === 'true' && !process.env.GOOGLE_MAPS_ANDROID_KEY) {
  throw new Error('GOOGLE_MAPS_ANDROID_KEY ausente no ambiente do EAS');
}

export default ({ config }) => ({
  ...config,
  plugins: [
    ...(config.plugins ?? []),
    ['react-native-maps', { androidGoogleMapsApiKey: process.env.GOOGLE_MAPS_ANDROID_KEY }],
  ],
});
