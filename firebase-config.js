/* Configuration Firebase de Nocturne (projet nocturne-rottweilert).
   Ces valeurs ne sont pas secrètes : l'accès aux données est protégé par les règles Firestore (firestore.rules),
   qui n'autorisent chaque compte qu'à lire et écrire ses propres données. */
window.NOCTURNE_FIREBASE = {
  apiKey: "AIzaSyBATmJUjE07QwaMoUARVRpyRFf7SxQyQXI",
  authDomain: "nocturne-rottweilert.firebaseapp.com",
  projectId: "nocturne-rottweilert",
  storageBucket: "nocturne-rottweilert.firebasestorage.app",
  messagingSenderId: "141592994926",
  appId: "1:141592994926:web:aa80e8b2432209ef23727f",
  // Reconnexion automatique (Google One Tap) : « ID client Web » visible dans
  // Authentication → Méthode de connexion → Google → Configuration du SDK Web.
  googleClientId: "141592994926-q886d5nbe3cmqlrd8qo0t1j1nptln73c.apps.googleusercontent.com"
};
