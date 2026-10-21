# CloudEdge / Meari — Intégration Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Une intégration personnalisée Home Assistant pour les caméras Wi-Fi sur
batterie basées sur la plateforme cloud Meari.

Meari est la plateforme cloud et matérielle sous-jacente ; CloudEdge est l'une
de ses principales applications grand public. L'intégration communique
directement avec la plateforme et les caméras via le protocole vidéo P2P
VVP / PPStrong — aucun pont tiers, aucun plugin Frigate et aucun conteneur
supplémentaire ne sont nécessaires.

> ⚠️ **Non officiel.** Sans affiliation avec CloudEdge, Meari ni aucun
> revendeur. À utiliser à vos risques : les API cloud peuvent changer sans
> préavis.

---

## Applications prises en charge

Le profil du compte doit correspondre à l'application utilisée pour créer ce
compte :

- CloudEdge
- CloudPlus / CloudHome
- ANRAN
- Arenti
- BoifunCam
- ieGeek

Ces applications reposent sur la même plateforme Meari, mais le serveur exige
l'identité propre à chaque application lors de la connexion.

---

## Points forts

- **Flux MPEG-TS en direct** servi par Home Assistant lui-même — la vidéo est
  **copiée** depuis la caméra sans transcodage, le flux transporte donc ce que
  la caméra produit nativement :
  - **H.264** sur les profils bas (généralement `SD`)
  - **HEVC / H.265** sur les profils hauts (généralement `HD`, `QHD`, `AUTO`)

  L'audio (G.711 µ-law) est la seule chose réencodée, en AAC, pour obtenir un
  MPEG-TS propre que Frigate, go2rtc, le composant `stream` natif de HA,
  les clients ONVIF et la plupart des NVR acceptent directement.
- **Flux au repos pour les caméras sur batterie ("snap")** — pendant que la
  caméra dort, l'intégration continue de publier un MPEG-TS d'image fixe avec
  le même codec, la même résolution et le même nombre d'images annoncés que le
  flux en direct. Le consommateur (Frigate, go2rtc, l'application mobile, …)
  **ne voit jamais la source disparaître**, et dès que la caméra se réveille le
  flux passe en direct sans reconnexion ni événement "flux perdu".
- **Alertes mouvement / IA via MQTT** — les caméras sur batterie poussent les
  événements PIR, personne, animal, colis et visiteur en temps réel via le
  broker IoT de Meari.
- **Contrôle du réveil des caméras sur batterie** — bouton de réveil manuel,
  interrupteur de réveil automatique sur mouvement, et durée de réveil
  configurable.
- **Toute la surface de réglages** — la plupart des options IoT des caméras
  (LED, PIR, sirènes, jour/nuit, enregistrement, anti-scintillement, ONVIF,
  HomeKit, …) apparaissent comme entités HA natives et écrivent sur la caméra.
- **PTZ** — service `cloudplus.ptz` pour les caméras qui le supportent.
- **Plusieurs caméras et plusieurs comptes** — une entrée de configuration par
  compte, un appareil par caméra, tous créés automatiquement après la
  connexion.

---

## Prérequis

- Home Assistant **2024.1+**
- Un compte d'une application compatible Meari avec au moins une caméra déjà
  appairée dans l'application officielle
- `ffmpeg` (fourni dans HAOS, HA Container et HA Supervised)
- Un accès Internet sortant vers le cloud Meari (HTTPS + MQTT/TLS) et UDP/TCP
  vers la caméra (réseau local ou relayé par TURN)

`pycryptodome` et `paho-mqtt` sont installés automatiquement par HACS / pip.

---

## Installation

### HACS (recommandé)

1. Ouvrez **HACS → Intégrations**.
2. Menu à trois points → **Dépôts personnalisés** → collez l'URL de ce dépôt →
   catégorie **Integration**.
3. Cherchez *CloudEdge / CloudPlus / Meari*, installez, puis **redémarrez HA**.

### Manuelle

1. Copiez [custom_components/cloudplus/](custom_components/cloudplus/) dans
   `<config>/custom_components/`.
2. Redémarrez Home Assistant.

---

## Configuration

1. **Paramètres → Appareils et services → Ajouter une intégration**.
2. Choisissez *CloudEdge / CloudPlus / Meari*.
3. Saisissez les identifiants du compte :

   | Champ             | Remarques |
   |-------------------|-----------|
   | Email             | La même adresse que celle utilisée dans l'application mobile. |
   | Mot de passe      | Le mot de passe du compte. |
   | Code pays         | par ex. `FR`, `US`, `DE` — correspond à la région de votre application. |
   | Indicatif téléphonique | L'indicatif international (par ex. `33` pour la France). |
   | Profil d'application | Choisissez le profil correspondant dans [Applications prises en charge](#applications-prises-en-charge). |

4. Toutes les caméras du compte sont découvertes et ajoutées comme appareils
   individuels.

> 💡 **Utilisez un compte secondaire.** Le broker MQTT de Meari n'autorise
> qu'une seule session active par compte à la fois. Si Home Assistant se
> connecte avec le même compte que votre téléphone, les événements de
> mouvement peuvent cesser d'arriver sur le téléphone (et inversement). Créez
> un second compte cloud et partagez la ou les caméras avec lui (ou ajoutez-le
> à la "famille / maison") avant de l'ajouter à HA.

### Options par caméra

Ouvrez n'importe quel appareil caméra → **Configurer** :

| Option              | Description |
|---------------------|-------------|
| Mot de passe vidéo  | Si vous avez activé le **chiffrement vidéo / E2EE** dans l'application, saisissez ce code à 6 chiffres ici pour que le flux puisse être déchiffré. Laissez vide sinon. |

### Options par compte

Ouvrez l'appareil du compte → **Configurer** — modifiez le mot de passe, le
pays, l'indicatif ou le profil d'application sur place. Les changements sont
propagés automatiquement à toutes les entrées de caméra.

---

## Entités

Chaque caméra expose un ensemble fixe d'entités de base, plus un ensemble
dynamique découvert à partir des valeurs du modèle IoT de la caméra.

### Entités de base

| Entité              | Plateforme      | Rôle |
|---------------------|-----------------|---------|
| Camera              | `camera`        | Aperçu en direct + `stream_source` pour go2rtc / Frigate. |
| Motion              | `binary_sensor` | Alerte PIR / mouvement / IA (personne, animal, colis, etc.). |
| Camera Awake        | `binary_sensor` | Vrai pendant que la caméra diffuse activement. |
| Charging            | `binary_sensor` | État de la charge USB (caméras sur batterie). |
| Battery             | `sensor`        | Pourcentage de charge (caméras sur batterie). |
| Charge Status       | `sensor`        | Statut textuel Décharge / En charge / Plein. |
| Wake Camera         | `button`        | Réveille la caméra manuellement. |
| Wake on Motion      | `switch`        | Active/désactive le réveil automatique sur événement de mouvement. |
| Motion Timeout      | `number`        | Durée pendant laquelle la caméra reste réveillée après un mouvement (10–600 s). |
| Stream Host Mode    | `select`        | `IP Address` (par défaut) ou `Docker Hostname` pour l'URL du flux — voir [Notes sur la diffusion](#notes-sur-la-diffusion). |
| Stream Quality      | `select`        | `AUTO`, `SD`, `HD`, `QHD`, … — les profils annoncés, ou le repli HD/SD natif des caméras plus anciennes. |

### Entités IoT (selon le modèle)

L'intégration lit les codes de fonctionnalités IoT supportés par chaque caméra
à la première configuration et n'expose que ceux qui existent. Attendez-vous à
un sous-ensemble de :

- **Interrupteurs** : LED de statut, détection de mouvement, détection de
  personne, suivi humain, détection de son, détection de pleurs, ONVIF,
  enregistrement SD, capteur PIR, liaison projecteur / sirène, reconnaissance
  faciale, mode veille, lumière RGB, anti-brouillage, patrouille PTZ, jouet
  laser, alarme animal, HomeKit, mise à jour automatique, filigrane OSD, logo
  de marque, son, lampe.
- **Nombres** (curseurs) : sensibilité mouvement / son / humain / PIR, intervalle
  PIR, luminosité et durée du projecteur, volume du haut-parleur, luminosité de
  la lumière chaude.
- **Sélecteurs** : type d'enregistrement SD, mode jour/nuit, fréquence d'alarme,
  type d'alarme sonore/lumineuse, anti-scintillement (50/60 Hz), mode couleur
  complète.
- **Capteurs** : température, humidité (sur les caméras qui les remontent).

Une caméra qui n'expose pas une fonctionnalité n'affichera simplement pas son
entité.

### Services

| Service          | Description |
|------------------|-------------|
| `cloudplus.ptz`  | Déplace (`left`/`right`/`up`/`down`) ou arrête une caméra PTZ. Cible une entité `camera`. Voir [services.yaml](custom_components/cloudplus/services.yaml). |

---

## Notes sur la diffusion

### Ce que l'entité caméra expose

L'entité caméra expose son flux natif comme une source MPEG-TS gérée par HA.

- **La vidéo** est copiée depuis la caméra telle quelle. Selon la
  `Stream Quality` choisie, le codec sous-jacent est **H.264** (plus léger,
  plus robuste — généralement `SD`) ou **HEVC / H.265** (meilleure qualité,
  plus fragile sur les caméras sur batterie — généralement `HD`, `QHD`,
  `AUTO`). Les deux codecs sont livrés aux consommateurs sans modification, la
  qualité est donc exactement celle que produit la caméra.
- **L'audio** (G.711 µ-law de la caméra) est réencodé en AAC pour que la sortie
  soit un MPEG-TS totalement standard.
- Le résultat est accessible à Frigate, go2rtc, le composant `stream` natif de
  HA, `media_player.play_media`, l'application mobile HA, les clients ONVIF /
  RTSP et la plupart des modules de type NVR.

### Flux au repos (caméras sur batterie / snap)

Les caméras sur batterie passent la plupart de leur vie en veille et ne
diffusent réellement que pendant qu'un événement PIR ou de mouvement les garde
éveillées. Sans aide, des consommateurs comme Frigate verraient la source
disparaître toutes les une ou deux minutes et déclencheraient un événement
"flux perdu".

L'intégration résout cela en **publiant en continu un flux au repos** pendant
que la caméra dort :

- Même codec, même résolution et même nombre d'images annoncés que le profil en
  direct.
- Image fixe répétée, initialisée à partir de la dernière image clé capturée
  pendant que la caméra était éveillée (elle représente donc visuellement la
  scène réelle).
- Coût de rafraîchissement faible — pas de réencodage à haute fréquence.
- Quand la caméra se réveille, les images en direct prennent le relais **sur la
  même socket TCP**, sans reconnexion, sans renégociation PAT/PMT, et sans
  qu'aucun client ne voie la source passer hors ligne.

C'est la différence entre Frigate qui "fonctionne simplement" avec ces caméras
et Frigate pratiquement inutilisable avec elles.

Certains carillons Meari sont annoncés par l'API comme `doorbell` /
`pictureDoorBell` plutôt que `snap`, même si leur carte de capacités annonce
`bat`. Le coordinateur traite ces catégories de carillons compatibles batterie
comme des caméras sur batterie, elles utilisent donc le même cycle de vie
réveil / flux au repos.

### Stream Host Mode

L'entité sélecteur `Stream Host Mode` contrôle la façon dont l'URL publique du
serveur MPEG-TS est annoncée :

- `IP Address` — utilise l'IP locale de l'hôte HA (par ex.
  `tcp://192.168.1.10:36059`). Idéal pour HAOS / HA Container avec réseau
  hôte, ou tout ce qui joint HA directement par IP.
- `Docker Hostname` — utilise plutôt le nom d'hôte du conteneur. À choisir si
  Frigate / go2rtc tourne dans le **même réseau Docker** que HA et résout les
  conteneurs par leur nom.

### Utiliser le flux dans Frigate / go2rtc

Le serveur MPEG-TS est interne à Home Assistant : ce n'est pas une URL RTSP
figée à coller dans la configuration de Frigate. Il faut lire l'URL réelle dans
l'entité `camera` active de HA et la transmettre à go2rtc / Frigate à chaque
ouverture du flux.

La mise en place la plus simple est l'intégration
[`hass-expose-camera-stream-source`](https://github.com/felipecrs/hass-expose-camera-stream-source),
qui expose le `stream_source` actif de n'importe quelle entité caméra de HA via
un simple point d'entrée HTTP :

```
GET http://<ha-host>:8123/api/camera_stream_source/<camera_entity_id>
Authorization: Bearer <long-lived-access-token>
```

Le corps de la réponse est l'URL du flux courante (par ex.
`tcp://192.168.1.10:36059`). go2rtc peut appeler ce point d'entrée à chaque
ouverture de flux grâce à sa source `echo:`, qui utilise la sortie standard
d'une commande arbitraire comme URL :

1. **Installez** `hass-expose-camera-stream-source` (HACS, intégration). Aucune
   entrée YAML ni configuration n'est nécessaire — cela active simplement le
   point d'entrée.
2. **Créez un jeton d'accès de longue durée** dans HA (menu du profil → bas de
   la page → *Jetons d'accès de longue durée*).
3. **Pointez go2rtc vers ce point d'entrée**. Exemple pour le go2rtc embarqué
   dans Frigate, avec une entité caméra nommée `camera.cloudplus_camera` :

   ```yaml
   # frigate config.yml
   go2rtc:
     streams:
       cloudedge-camera:
         - 'echo:curl -fsSL http://<ha-host>:8123/api/camera_stream_source/camera.cloudplus_camera
             -H "Authorization: Bearer <ha-long-lived-access-token>"'

   cameras:
     cloudedge-camera:
       ffmpeg:
         inputs:
           - path: rtsp://127.0.0.1:8554/cloudedge-camera
             roles: [detect, record]
   ```

   Un go2rtc autonome prend exactement la même entrée `echo:curl …` dans sa
   propre section `streams:`.
4. Frigate parle ensuite à go2rtc en RTSP, go2rtc résout de nouveau le point
   d'entrée HA à chaque connexion d'un client, et vous obtenez un flux
   H.264 / HEVC propre qui survit à la mise en veille de la caméra (le flux au
   repos comble l'intervalle) et enregistre / détecte normalement.

> 💡 Si vous préférez ne pas utiliser l'intégration d'aide, vous pouvez lire
> directement l'attribut `stream_source` de l'entité caméra via l'API WebSocket
> ou REST de HA et l'injecter dans go2rtc comme vous voulez. L'aide se contente
> d'emballer cela dans un seul GET protégé par jeton.

### Détails techniques approfondis

Pour les mécanismes sous-jacents (récupération KCP, routage TURN/relais, blocages
HEVC, réémission proactive de `START_LIVE`, récupération après source inactive),
voir [docs/streaming.md](docs/streaming.md) et
[docs/protocol.md](docs/protocol.md).

---

## Dépannage

Un outil en ligne de commande autonome (`debug.py`) réutilise exactement le
chemin de code de l'intégration, vous pouvez donc reproduire les problèmes en
dehors de Home Assistant :

```bash
# Copiez .env.example ou créez votre propre .env avec EMAIL/PASSWORD/COUNTRY_CODE
python debug.py list
python debug.py stream --device-id <id> --duration 60 --quality QHD
python debug.py --debug stream --device-id <id> --duration 30  # logs verbeux
```

Problèmes courants :

- **Aucun événement de mouvement** — un autre appareil occupe probablement la
  session MQTT Meari. Utilisez un compte cloud dédié à HA (voir
  [Configuration](#configuration)).
- **Flux qui se bloque / "peu d'images" en QHD** — le HEVC à haut débit est
  fragile sur les caméras sur batterie ; passer en SD (qui est du H.264) est un
  repli utile. Voir [docs/diagnosis.md](docs/diagnosis.md).
- **La caméra ne se réveille jamais** — les réveils de dormance profonde sont
  réessayés automatiquement pendant environ 45 s ; si cela échoue de façon
  répétée, votre compte n'a peut-être pas le droit de réveil (repassez par un
  partage depuis l'application).
- **Frigate perd la source** — basculez *Stream Host Mode* entre `IP
  Address` et `Docker Hostname` selon la topologie de votre réseau.

---

## Fonctionnement (vue d'ensemble)

1. **Authentification** auprès de l'API HTTP de Meari (famille d'hôtes selon le
   profil d'application).
2. **Abonnement** au broker MQTT IoT de Meari (TLS) pour les alertes mouvement /
   IA.
3. **Découverte** des serveurs de signalisation via le protocole racine UDP
   natif.
4. **Signalisation** d'une session de type WebRTC via TCP MsgSvr
   (offer/answer, ICE).
5. **Relais** des données via une allocation TURN UDP (ou directement sur le
   réseau local).
6. **Tunnel** avec KCP (ARQ fiable sur UDP) et la couche de trames IVA.
7. **Contrôle** de la caméra avec les commandes VVP (`START_LIVE`, heartbeat,
   etc.).
8. **Déchiffrement** des médias — vidéo H.264 *ou* HEVC (3DES-ECB quand la
   caméra a activé l'E2EE) et audio G.711 µ-law.
9. **Multiplexage** en MPEG-TS — la vidéo est copiée (quel que soit le codec
   envoyé par la caméra), l'audio est encodé en AAC — puis diffusion à tous les
   consommateurs HA / Frigate / go2rtc via une seule socket TCP partagée. La
   même socket transporte le flux d'image fixe au repos pendant que la caméra
   dort.

Chaque couche est documentée en détail dans [docs/](docs/).

---

## Carte de la documentation

| Fichier | Public | Contenu |
|------|----------|---------------|
| [README.md](README.md) | Utilisateurs | Ce fichier — installation, configuration, entités, services. |
| [AGENTS.md](AGENTS.md) | Agents de code IA | Points d'entrée du projet, commandes, conventions, permissions. |
| [docs/architecture.md](docs/architecture.md) | Contributeurs | Organisation du code (custom_components, p2p_streamer, coordinator, debug_tools). |
| [docs/protocol.md](docs/protocol.md) | Spécialistes du protocole | Découverte, signalisation, ICE/TURN, KCP, VVP, trames média. |
| [docs/streaming.md](docs/streaming.md) | Contributeurs | Schémas de démarrage en direct, récupération après source inactive, diffusion MPEG-TS. |
| [docs/motion-events.md](docs/motion-events.md) | Contributeurs | Topics MQTT Meari, formes des charges utiles, API de notification de repli. |
| [docs/diagnosis.md](docs/diagnosis.md) | Contributeurs | Usage de `debug.py`, signaux de log, comment trier les blocages. |

---

## Contribuer

Les PR sont bienvenues. Si vous êtes un assistant IA travaillant dans ce dépôt,
commencez par [AGENTS.md](AGENTS.md). Si vous êtes un contributeur humain, ce
même fichier est aussi le moyen le plus rapide de s'orienter.

Avant d'ouvrir une PR, merci de :

- lancer `python debug.py list` et `python debug.py stream …` sur votre propre
  caméra pour confirmer que le changement ne casse pas le chemin de diffusion,
- garder les comportements conditionnés à des preuves réelles issues de
  captures locales de l'application officielle quand c'est possible,
- éviter les IP, régions ou points de signalisation codés en dur — la
  découverte est la source de vérité.

---

## Versions

Les envois normaux sur `main` ne créent pas de version. Les mainteneurs créent
les versions manuellement depuis **Actions → Release Tags → Run workflow** et
choisissent :

- `prerelease` — crée la version `vX.Y.Z-dev.N` suivante.
- `release` — crée la version stable `vX.Y.Z`.

La version dans `custom_components/cloudplus/manifest.json` est la source de
vérité. Après avoir publié une version stable, incrémentez cette version avant
de commencer le cycle de préversion suivant.

---

## Licence

[MIT](LICENSE) — voir le fichier pour les détails. Les noms de produits
utilisés dans ce README sont des marques de leurs propriétaires respectifs ; ce
projet n'est approuvé par aucun d'entre eux.
