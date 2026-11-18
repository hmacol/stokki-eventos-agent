// A chave do Google Maps (Android) vem da variavel do EAS GOOGLE_MAPS_ANDROID_KEY; nunca vai pro git.
export default ({ config }) => ({
  ...config,
  plugins: [
    ...(config.plugins ?? []),
    ['react-native-maps', { androidGoogleMapsApiKey: process.env.GOOGLE_MAPS_ANDROID_KEY }],
  ],
});
