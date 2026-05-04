# DIY Network Bonding — Cahier des charges

> Document de travail, vivant. À relire / éditer à chaque décision structurante.
> **Statut au 2026-05-04** : NetHealth (v1.24) est clos / archivé. Ce doc devient
> autonome et porte désormais la Phase 2 du projet. Le couplage NetHealth ↔
> bonding (jalon "Intégration NetHealth") est retiré : le pilotage de l'agrégat
> se fera via les outils du concentrateur (Speedify console, ou Grafana sur Pi5
> en cas de stack DIY).

## 1. Vision en une phrase

Agréger plusieurs uplinks internet mobiles (iPhone + TP-Link M8550) depuis un laptop
en déplacement, vers un **concentrateur à domicile** (Raspberry Pi 4 derrière fibre
symétrique 10 Gb/s), pour obtenir une connexion **plus fiable** et **plus rapide**
qu'un uplink seul, le tout en open-source et auto-hébergé.

## 2. Contexte & motivation

- **Déplacements professionnels** en augmentation → besoin d'une connexion stable
  depuis n'importe où.
- **Deux opérateurs mobiles différents** (iPhone chez l'un, TP-Link 5G chez l'autre)
  → la probabilité que **les deux** tombent en même temps est très faible.
- **Fibre symétrique à la maison** (Bouygues + IPv6 natif derrière UCG Express 7)
  → le backhaul domicile n'est pas le goulot d'étranglement (même après
  agrégation, les uplinks mobiles cumulés restent très en dessous).
- **Pi5 déjà exposé en IPv6** + DNS wildcard Infomaniak + DDNS préfixe → un
  endpoint internet est déjà prêt, plus besoin de prévoir un Pi 4 dédié comme
  dans la 1re version de ce doc.
- **Envie d'apprendre** → monter cette infra est un excellent terrain de jeu
  (réseau, VPN, routage, supervision), pas juste un objectif utilitaire.

### 2bis. Limite matérielle Mac (mise à jour 2026-05-04)

Une décision majeure a émergé en clôturant NetHealth : **un MacBook n'a qu'une
seule radio Wi-Fi physique**, donc une seule association SSID à la fois. Il est
**impossible** de bonder simultanément deux Wi-Fi (ex. Nostromo + Lolfr's Mobile)
sans matériel additionnel. Conséquences sur l'arsenal d'uplinks :

| Uplink                         | Comment l'attacher au Mac                       |
|---|---|
| Wi-Fi domicile (Nostromo)      | Carte interne                                    |
| iPhone tethering               | USB-C, devient `enX` côté Mac                    |
| TP-Link M8550 (5G)             | **Ethernet via dock USB-LAN** (pas en Wi-Fi simultané) |
| 2e Wi-Fi externe               | Clé USB Wi-Fi (Alfa AWUS036ACM ou équivalent), optionnel |

Le M8550 sur 192.168.1.1 reste joignable depuis le subnet domicile uniquement
si on le câble. Pour la mobilité : prévoir le câble dock-LAN dans le sac.

## 3. Contraintes

| Contrainte | Détail |
|---|---|
| Pas de modification du MacBook au niveau kernel | Pas de kext, uniquement Network Extension + user-space |
| Solution signée & notarisable | Pour ne pas galérer à chaque rebuild |
| Bande passante cible en mobilité | 100-300 Mbit/s cumulés (réaliste) |
| Latence ajoutée maximale | 30 ms par rapport à la meilleure interface seule |
| CPU Pi | ≤ 60 % en pic, pour garder de la marge au pi-hole |
| Sécurité | Un seul port UDP exposé sur internet, auth par clé publique |

## 4. Architecture cible

```
 ┌──────────── MOBILITÉ ────────────┐              ┌──────── DOMICILE ─────────┐
 │                                  │              │                           │
 │   ┌────────────┐   USB-C         │              │                           │
 │   │  iPhone    │ ─────────────┐  │              │                           │
 │   │  (op. 1)   │              │  │              │                           │
 │   └────────────┘              │  │              │                           │
 │                           ┌───▼──┴──┐    WG1    │   ┌───────────────┐       │
 │   ┌────────────┐  USB-LAN │         │───UDP────►│   │  Raspberry Pi  │       │
 │   │ TP-Link 5G │──────────│ MacBook │───UDP────►│   │       5        │──────►│──► Internet
 │   │  (op. 2)   │          │         │    WG2    │   │  (concentrateur)│      │  (via fibre Bouygues)
 │   └────────────┘          │         │           │   └───────────────┘       │
 │                           │         │    WG3    │           │                │
 │   ┌────────────┐  Wi-Fi   │         │───UDP────►│   (routage NAT / WG /      │
 │   │ Wi-Fi tiers│──────────│         │           │    OpenMPTCProuter)        │
 │   │ (hôtel…)   │          └─────────┘           │                            │
 │   └────────────┘                                 │                            │
 │                                                  │                            │
 │  2-3 interfaces physiques                        │  Endpoint UDP exposé en   │
 │  1 interface virtuelle utun                      │  IPv6 (déjà en place)     │
 └──────────────────────────────────────────────────┴────────────────────────────┘
```

- **Côté MacBook** : un client de bonding présente une iface virtuelle
  (`utun`/`tun`) ; tous les paquets sortants y entrent et sont dispatchés vers
  les interfaces physiques via plusieurs tunnels UDP parallèles. Selon le
  produit : Network Extension custom (Swift), Speedify (commercial), ou client
  WireGuard standard couplé à des règles `pf` côté Mac.
- **Côté Pi5** : reçoit les N tunnels, réassemble, NAT vers la fibre, gère la
  route de retour symétrique. Pi5 est **déjà** exposé en IPv6 et a un nom
  stable (`watchtower.mylastnight.eu` pour la supervision, à compléter d'un
  enregistrement dédié bonding).

## 5. Matériel

### Déjà disponible (mise à jour 2026-05-04)

- **Pi5** opérationnel, exposé IPv6, plein de services dessus → sert de
  concentrateur. Pas besoin d'acheter un Pi 4 dédié comme prévu initialement.
- **UCG Express 7** (Bouygues fibre) → port-forwarding UDP déjà supporté.
- **Pi 2B** dédié pi-hole / DNS secondaire → laissé tranquille.
- **iPhone** (op. 1) en tethering USB-C.
- **TP-Link M8550** (op. 2) — accessible en Ethernet via dock USB-LAN.
- **MacBook M4** + dock USB-C avec port LAN.

### À envisager seulement si DIY confirmé

| Item | Quand | Coût |
|---|---|---|
| Mini-PC dédié OpenMPTCProuter | si le Pi5 est trop chargé pour porter en plus le bonding | 100-200 € |
| VPS sortie OpenMPTCProuter | si on veut une IP fixe stable hors résidentiel | 5-10 €/mois |
| Clé USB Wi-Fi (Alfa AWUS036ACM) | si on veut bonder 2 Wi-Fi en mobilité | 50 € |
| Câble Ethernet plat 1 m | obligatoire pour utiliser le M8550 en mobilité | 10 € |

## 6. Stack logicielle

> 2026-05-04 : la stack DIY initiale (engarde / mptcpd / Network Extension)
> reste valide *sur le papier*, mais on insère **avant** une phase de
> validation produit (Speedify) pour s'assurer que l'expérience ciblée vaut
> bien la complexité d'auto-héberger. Voir Phase 2A dans §7.

### Phase 2A — Validation par produit commercial (Speedify)

| Couche | Choix |
|---|---|
| Client Mac | App Speedify officielle (gratuite, free tier 2 Go/mois) |
| Concentrateur | Cloud Speedify — **rien à monter** |
| Avantage | 5 minutes de setup, 0 dette technique |
| Sortie produit | Confirmer/infirmer l'utilité réelle avant d'investir 2-3 jours en DIY |
| Limite | Tout le trafic transite chez Speedify, pas idéal pour les flux sensibles |

### Phase 2B — DIY auto-hébergé (si Speedify a validé l'usage)

#### Pi5 (concentrateur)

| Couche | Choix candidat | Alternative |
|---|---|---|
| OS | Raspberry Pi OS 64-bit (déjà en place) | — |
| Tunnel VPN | WireGuard (kernel-mode) | OpenVPN (déjà en prod, mais plus lent) |
| Bonding | **OpenMPTCProuter** ou WG+ECMP côté Pi5 | `engarde`, `mptcpd` |
| Supervision | Grafana déjà en place sur l'infra | — |
| Durcissement | Authentik / WireGuard clé only / IPv6 firewall (déjà en place) | — |
| DNS dynamique | DDNS préfixe Infomaniak (déjà en place) | — |

#### MacBook (client)

| Couche | Choix candidat | Alternative |
|---|---|---|
| Intégration OS | Client WireGuard standard + règles `pf` ciblées | Network Extension custom (Swift) — plus propre mais lourd à développer/signer/notariser |
| Routage par destination | `pf` policy-based routing | App tierce (Surge ~50 €) |
| UI / monitoring | Grafana web sur Pi5 (pas d'app Mac dédiée) — NetHealth étant clos | Mini-app menubar dédiée si besoin plus tard |

## 7. Jalons

> Refonte 2026-05-04 : phases 0/1 majoritairement réalisées par l'infra
> existante, ajout d'une Phase 2A validation produit, le couplage NetHealth
> est retiré.

### Phase 0 — Préparation ✅ (déjà fait par l'infra existante)

- [x] Concentrateur en place (Pi5 + UCG Express 7 + IPv6 natif)
- [x] DDNS / DNS wildcard (Infomaniak)
- [x] SSH durci (clé only, fail2ban / Authentik selon services)
- [ ] Vérifier que l'UCG laisse passer un port UDP supplémentaire pour WG
- [ ] Tester explicitement la traversée NAT IPv4/IPv6 (selon opérateur mobile en sortie)

### Phase 1 — Tunnel simple WireGuard Mac ↔ Pi5 (1 semaine)

Objectif : avoir **une seule** connexion VPN MacBook ↔ Pi5 qui marche depuis
l'extérieur. Sert de base saine avant d'empiler le bonding. Note : un OpenVPN
existe déjà sur Bouygues (cf. `project_vpn_troubleshoot.md`), à comparer.

- [ ] Installer WireGuard côté Pi5, générer clés + conf
- [ ] Installer WireGuard côté Mac (app officielle ou CLI)
- [ ] Vérifier le ping bidirectionnel et la traversée NAT
- [ ] Mesurer latence + débit au travers (baseline single-link)
- [ ] Décider : on garde WG côté bonding ou on réutilise OpenVPN existant

### Phase 2A — Validation par Speedify (1-2 jours)

Objectif : vérifier *avant tout DIY* que l'expérience d'avoir un lien bondé
vaut la complexité — sur du vrai usage en mobilité.

- [ ] Installer Speedify sur le Mac (free tier, 2 Go/mois)
- [ ] Tester avec iPhone tethering + Wi-Fi domicile activés en parallèle
- [ ] Tester avec iPhone tethering + M8550 (Ethernet via dock)
- [ ] Évaluer : stabilité visio, vitesse perçue, basculement automatique
- [ ] **Decision point** : l'expérience est-elle suffisamment au-dessus d'un
      simple failover natif macOS (`networksetup -setnetworkserviceorder`)
      pour justifier l'effort DIY de la Phase 2B ? Si non : on s'arrête là,
      Speedify reste l'outil de référence ou on capitule sur le bonding.

### Phase 2B — Bonding DIY auto-hébergé (si Phase 2A a convaincu) — 2-3 jours

Deux variantes possibles, à trancher au début de la phase :

**Variante 1 — OpenMPTCProuter sur Pi5** (si Pi5 a la capacité)
- [ ] Vérifier la charge CPU/RAM disponible sur Pi5 (déjà chargé en services)
- [ ] Installer OpenMPTCProuter / configurer un VPS de sortie
- [ ] Bonder iPhone + M8550 + Wi-Fi domicile en mobilité

**Variante 2 — WireGuard multi-tunnels + ECMP côté Pi5**
- [ ] N tunnels WG (un par iface Mac) sortant vers Pi5
- [ ] ECMP / load-balance par flux côté Pi5
- [ ] Plus DIY mais plus contrôlable

- [ ] Mesurer gain vs tunnel simple (débit, latence, stabilité à la reprise)
- [ ] Comparer à Speedify : la perte d'ergonomie vaut-elle l'autonomie ?

### Phase 3 — Durcissement & exploitation (1 semaine)

- [ ] `systemd` services pour relance auto après crash
- [ ] Rotation des logs
- [ ] Alertes via ntfy (`mln-infra` topic) si le Pi5 n'est plus joignable
- [ ] Doc d'exploitation dans la knowledge base homelab

## 8. Décisions à prendre

Refonte 2026-05-04 — on remonte la décision la plus haute en premier :

1. **Speedify d'abord ou DIY directement ?** — fortement biaisé "Speedify
   d'abord" : 5 minutes vs 2-3 jours, et règle la question existentielle "ai-je
   vraiment besoin de bonding ?" avant tout investissement.
2. **Si DIY : OpenMPTCProuter sur Pi5 ou WG+ECMP maison ?** — OMR est plus
   complet et déjà packagé, mais c'est une stack lourde qui veut idéalement un
   mini-PC dédié. WG+ECMP est plus léger et tient sur le Pi5 existant, au prix
   d'un peu de routage manuel.
3. **Si DIY : Network Extension Mac ou client WG standard + `pf` ?** — Network
   Extension demande Swift + signature/notarisation à chaque rebuild ;
   `pf` + WG officielle évite ce coût pour 90% du résultat.
4. **Priorité de routage** : round-robin / load-balance / par-app ? À trancher
   *après* la Phase 2A — Speedify gère ça automatiquement, ce qui donne un
   benchmark.

## 9. Risques identifiés

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| Le Pi n'est pas joignable depuis l'extérieur | Moyenne | Critique (tout tombe) | Monitoring externe + alerte + redondance Pi2B en secours |
| CG bloque les ports UDP chez un opérateur mobile | Moyenne | Élevé | Fallback TCP (dégradé), ou port 443 |
| Latence ajoutée trop élevée pour visio | Faible | Moyen | Profil par app (visio = uplink direct, reste = bondé) |
| Apple casse Network Extension dans une maj macOS | Faible | Élevé | Plan B : tout en user-space SOCKS |
| Pi HS (SD corrompue) | Moyen (Pi 2B a 8 ans) | Moyen | Backups img + Pi neuf en secours |

## 10. Évolutions futures

- **Multi-concentrateur** : plusieurs Pi à différents endroits (ex. un au
  bureau, un à la maison) avec bascule auto selon la latence.
- **Exposer le bonding à d'autres appareils** : iPhone, iPad, tablette, via
  profil WireGuard partagé.
- **Mini-PC OpenMPTCProuter dédié** : si le Pi5 sature, déporter le bonding
  sur un mini-PC fanless (ex. N100) pour 100-200 €, libérer le Pi5.
- **2e radio Wi-Fi** : ajouter une clé Alfa USB pour bonder 2 Wi-Fi simultanés
  en mobilité (utile si à la fois le M8550 ne suffit pas et qu'on n'a pas de
  port Ethernet pour le câbler).

## 11. Liens utiles

- [Speedify](https://speedify.com/) — produit commercial, free tier 2 Go/mois pour benchmarker
- [OpenMPTCProuter](https://www.openmptcprouter.com/) — distribution Linux clé en main pour bonding multipath
- [`engarde`](https://github.com/porech/engarde) — bonding UDP multi-uplink en Go (alternative Speedify, plus minimaliste)
- [WireGuard](https://www.wireguard.com/) — VPN moderne, ultra-léger
- [`mptcpd`](https://github.com/multipath-tcp/mptcpd) — daemon MPTCP (Linux, peu utile sur Mac)
- [Apple Network Extension](https://developer.apple.com/documentation/networkextension) — API officielle Apple
- [Surge for Mac](https://nssurge.com/) — proxy local Mac, routage par règles (~50 €)

---

_Dernière mise à jour : 2026-05-04 — refonte post-clôture NetHealth, ajout
Phase 2A Speedify, recentrage du concentrateur sur Pi5._
