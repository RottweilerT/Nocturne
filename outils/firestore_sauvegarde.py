#!/usr/bin/env python3
"""Sauvegarde, vérification et restauration de la base Firestore de l'appli Nocturne (projet nocturne-rottweilert).

Utilisé par les actions GitHub « Sauvegarde de la base », « Vérifier la sauvegarde » et « Restaurer la base ».

Pourquoi un compte de service : dans Nocturne, seul le compte Google connecté peut lire ses nuits (règles Firestore).
Pour copier la base, le robot s'identifie avec un compte de service Firebase
(secret du dépôt FIREBASE_COMPTE_SERVICE, fichier JSON téléchargé dans la console Firebase),
qui passe outre les règles. Aucune bibliothèque à installer : la signature se fait avec openssl.

Commandes :
  lire FICHIER                 copie toute la base (toutes les collections et sous-collections) dans FICHIER
  verifier FICHIER             contrôle une copie déchiffrée et la compare à la base actuelle, SANS rien écrire
  restaurer FICHIER [PREFIXE]  réécrit les documents de la copie (seulement ceux dont le chemin commence par PREFIXE)

Le journal n'affiche que des nombres : le dépôt est public.
"""
import base64, json, os, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request

API = os.environ.get("FIRESTORE_API", "https://firestore.googleapis.com/v1")  # remplaçable pour les tests
ATTENDU = ["users/*/nights", "users/*/clips", "users/*/meta"]  # parties connues de la base


def stop(msg):
    print("::error::" + msg)
    sys.exit(1)


def compte_service():
    brut = os.environ.get("FIREBASE_COMPTE_SERVICE", "").strip()
    if not brut:
        stop("Le secret FIREBASE_COMPTE_SERVICE manque dans les réglages du dépôt (clé JSON du compte de service Firebase)")
    try:
        cs = json.loads(brut)
        assert cs.get("private_key") and cs.get("client_email") and cs.get("project_id")
    except Exception:
        stop("Le secret FIREBASE_COMPTE_SERVICE n'est pas une clé JSON de compte de service valide")
    return cs


def jeton(cs):
    """Jeton d'accès Google (OAuth2, flux JWT du compte de service), signé avec openssl."""
    if os.environ.get("JETON_TEST"):
        return os.environ["JETON_TEST"]
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
    now = int(time.time())
    tete = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    corps = b64(json.dumps({"iss": cs["client_email"], "scope": "https://www.googleapis.com/auth/datastore",
                            "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600}).encode())
    signe = tete + b"." + corps
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write(cs["private_key"]); cle = f.name
    try:
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", cle], input=signe, capture_output=True, check=True).stdout
    finally:
        os.unlink(cle)
    data = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                   "assertion": (signe + b"." + b64(sig)).decode()}).encode()
    try:
        return json.load(urllib.request.urlopen("https://oauth2.googleapis.com/token", data))["access_token"]
    except urllib.error.HTTPError as e:
        stop(f"Google refuse le compte de service ({e.code}) : clé révoquée ou mal copiée dans FIREBASE_COMPTE_SERVICE")


class Base:
    def __init__(self, projet, tok):
        self.racine = f"{API}/projects/{projet}/databases/(default)/documents"
        self.hdr = {"Authorization": "Bearer " + tok, "Content-Type": "application/json"}

    def appel(self, url, corps=None, methode=None):
        req = urllib.request.Request(url, data=None if corps is None else json.dumps(corps).encode(), headers=self.hdr, method=methode)
        for essai in range(4):
            try:
                return json.load(urllib.request.urlopen(req, timeout=60))
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 503) and essai < 3:
                    time.sleep(2 ** essai); continue
                raise

    def sous_collections(self, chemin_doc):
        url = f"{self.racine}/{chemin_doc}:listCollectionIds" if chemin_doc else f"{self.racine}:listCollectionIds"
        ids, token = [], ""
        while True:
            r = self.appel(url, {"pageSize": 300, **({"pageToken": token} if token else {})})
            ids += r.get("collectionIds", [])
            token = r.get("nextPageToken")
            if not token:
                return ids

    def documents(self, chemin_col):
        """Documents d'une collection, y compris les « fantômes » (sans contenu mais avec des sous-collections)."""
        token = ""
        while True:
            q = {"pageSize": 300, "showMissing": "true"}
            if token: q["pageToken"] = token
            r = self.appel(f"{self.racine}/{chemin_col}?" + urllib.parse.urlencode(q))
            for d in r.get("documents", []):
                chemin = d["name"].split("/documents/", 1)[1]
                yield chemin, (d.get("fields", {}) if "createTime" in d else None)
            token = r.get("nextPageToken")
            if not token:
                return

    def tout_lire(self):
        docs = {}
        def parcourir(chemin_doc):
            for col in self.sous_collections(chemin_doc):
                chemin_col = f"{chemin_doc}/{col}" if chemin_doc else col
                for chemin, champs in self.documents(chemin_col):
                    if champs is not None:
                        docs[chemin] = champs
                    parcourir(chemin)
        parcourir("")
        return docs

    def ecrire(self, chemin, champs):
        self.appel(f"{self.racine}/{chemin}", {"fields": champs}, "PATCH")


def groupe(chemin):
    """users/abc/sessions/x -> users/*/sessions (on ne montre jamais d'identifiant dans le journal)."""
    p = chemin.split("/")
    return "/".join(p[i] if i % 2 == 0 else "*" for i in range(len(p) - 1))


def compter(docs):
    c = {}
    for k in docs:
        c[groupe(k)] = c.get(groupe(k), 0) + 1
    return c


def resume(titre, lignes):
    f = os.environ.get("GITHUB_STEP_SUMMARY")
    if f:
        with open(f, "a", encoding="utf-8") as o:
            o.write(titre + "\n" + "\n".join(lignes) + "\n")


def cmd_lire(sortie):
    cs = compte_service()
    base = Base(cs["project_id"], jeton(cs))
    try:
        docs = base.tout_lire()
    except urllib.error.HTTPError as e:
        stop(f"Lecture de la base impossible ({e.code}) : vérifie que le compte de service a bien accès à Firestore")
    json.dump({"projet": cs["project_id"], "format": 1, "documents": docs}, open(sortie, "w"),
              ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    c = compter(docs)
    detail = ", ".join(f"{k} {v}" for k, v in sorted(c.items()))
    print(f"::notice::Sauvegarde : {len(docs)} documents lus ({detail or 'base vide'})")
    resume("## Sauvegarde de la base\n", [f"- Documents lus : **{len(docs)}**"] + [f"- {k} : {v}" for k, v in sorted(c.items())])


def charger(fichier):
    try:
        b = json.load(open(fichier, encoding="utf-8"))
        assert b.get("format") == 1 and b.get("projet") and isinstance(b.get("documents"), dict)
    except Exception:
        stop("Format inattendu : ce n'est pas une sauvegarde de l'appli Nocturne")
    return b


def cmd_verifier(fichier):
    b = charger(fichier)
    erreurs, prets, trop_gros = [], 0, 0
    for chemin, champs in b["documents"].items():
        if not isinstance(champs, dict) or chemin.count("/") % 2 != 1:
            erreurs.append("document mal formé"); continue
        if len(json.dumps({"fields": champs}).encode()) > 1_000_000:
            trop_gros += 1
        prets += 1
    if trop_gros:
        erreurs.append(f"{trop_gros} document(s) dépassent 1 Mo")
    if b["documents"] and not any(groupe(k) == "users/*/nights" for k in b["documents"]):
        erreurs.append("aucune nuit dans la sauvegarde")
    cs = compte_service()
    if cs["project_id"] != b["projet"]:
        erreurs.append("la sauvegarde ne vient pas du même projet Firebase que le compte de service")
    base = Base(b["projet"], jeton(cs))
    try:
        actuel = base.tout_lire()
    except urllib.error.HTTPError as e:
        stop(f"Lecture de la base actuelle impossible ({e.code})")
    s = b["documents"]
    lignes = [f"- Déchiffrement : **réussi** (la clé est la bonne)", f"- Documents prêts à être restaurés : **{prets}**", "",
              "| Partie de la base | Dans la sauvegarde | Dans la base actuelle | Identiques | Modifiés depuis | Ajoutés depuis | Disparus depuis |",
              "|---|---|---|---|---|---|---|"]
    for g in sorted(set(ATTENDU) | {groupe(k) for k in s} | {groupe(k) for k in actuel}):
        sg = {k: v for k, v in s.items() if groupe(k) == g}
        ag = {k: v for k, v in actuel.items() if groupe(k) == g}
        ident = sum(1 for k in sg if k in ag and ag[k] == sg[k])
        modif = sum(1 for k in sg if k in ag and ag[k] != sg[k])
        ajout = sum(1 for k in ag if k not in sg)
        disp = sum(1 for k in sg if k not in ag)
        lignes.append(f"| {g} | {len(sg)} | {len(ag)} | {ident} | {modif} | {ajout} | {disp} |")
        print(f"{g} : sauvegarde {len(sg)} · base {len(ag)} · identiques {ident} · modifiés {modif} · ajoutés {ajout} · disparus {disp}")
    # Les séances sont-elles bien fermées au public ? (lecture sans connexion, avec la clé web de l'appli)
    ferme = None
    cle_web = os.environ.get("CLE_WEB", "")
    if cle_web and API.startswith("https://firestore.googleapis.com"):
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"{base.racine}/users?pageSize=1&key={cle_web}", headers={"Referer": "https://rottweilert.github.io/Nocturne/"})).read()
            ferme = False
        except urllib.error.HTTPError as e:
            ferme = e.code in (401, 403)
    if ferme is not None:
        lignes.append(f"\n- Nuits fermées au public (lecture sans connexion refusée) : **{'oui' if ferme else 'NON, les nuits sont lisibles par tous'}**")
        print("Nuits fermées au public :", "oui" if ferme else "NON")
        if ferme is False:
            erreurs.append("les nuits sont lisibles sans connexion : vérifie les règles Firestore")
    lignes.append("\n« Modifiés / ajoutés / disparus depuis » = changements faits dans l'appli après la sauvegarde : c'est normal.")
    if erreurs:
        lignes.append("\n**Problèmes :** " + " ; ".join(erreurs))
    else:
        lignes.append("\n**Résultat : la sauvegarde est complète et restaurable.**")
    resume("## Vérification de la sauvegarde (aucune écriture)\n", lignes)
    for e in erreurs:
        print("::error::" + e)
    if erreurs:
        sys.exit(1)
    print(f"::notice::Sauvegarde complète et restaurable : {prets} documents prêts, aucune écriture effectuée")


def cmd_restaurer(fichier, prefixe=""):
    b = charger(fichier)
    cs = compte_service()
    if cs["project_id"] != b["projet"]:
        stop("La sauvegarde ne vient pas du même projet Firebase que le compte de service : restauration annulée")
    prefixe = prefixe.strip().strip("/")
    choix = {k: v for k, v in b["documents"].items() if not prefixe or k == prefixe or k.startswith(prefixe + "/")}
    if not choix:
        stop("Aucun document de la sauvegarde ne correspond à ce chemin")
    base = Base(b["projet"], jeton(cs))
    ok = err = 0
    for chemin, champs in sorted(choix.items()):
        try:
            base.ecrire(chemin, champs); ok += 1
        except urllib.error.HTTPError as e:
            err += 1; print(f"Échec d'écriture ({e.code}) dans {groupe(chemin)}")
    print(f"::notice::Restauration : {ok} documents réécrits, {err} échecs" + (" (partie choisie seulement)" if prefixe else ""))
    resume("## Restauration de la base\n", [f"- Documents réécrits : **{ok}**", f"- Échecs : **{err}**"] + ([f"- Partie restaurée : seulement les chemins choisis"] if prefixe else []))
    sys.exit(1 if err else 0)


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 2 and a[0] == "lire": cmd_lire(a[1])
    elif len(a) >= 2 and a[0] == "verifier": cmd_verifier(a[1])
    elif len(a) >= 2 and a[0] == "restaurer": cmd_restaurer(a[1], a[2] if len(a) > 2 else "")
    else: stop("usage : lire|verifier|restaurer FICHIER [PREFIXE]")
