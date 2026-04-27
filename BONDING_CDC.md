# DIY Network Bonding — Cahier des charges

> Document de travail, vivant. À relire / éditer à chaque décision structurante.
> Statut : **ébauche** — à compléter / trancher avec Laurent au fil des semaines.

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
- **Fibre 10 Gb/s symétrique à la maison** → le backhaul domicile n'est pas le
  goulot d'étranglement (même après agrégation, les uplinks mobiles cumulés
  restent très en dessous).
- **Infra Pi déjà en place** (Pi 2B avec pi-hole) → la mécanique d'auto-hébergement
  est déjà familière, on capitalise dessus.
- **Envie d'apprendre** → monter cette infra est un excellent terrain de jeu
  (réseau, VPN, routage, supervision), pas juste un objectif utilitaire.

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
 │   ┌────────────┐   Wi-Fi ≈100 M  │              │                           │
 │   │  iPhone    │ ─────────────┐  │              │                           │
 │   │  (op. 1)   │              │  │              │                           │
 │   └────────────┘              │  │              │                           │
 │                           ┌───▼──┴──┐    WG1    │   ┌───────────────┐       │
 │   ┌────────────┐  USB/WiFi│         │───UDP────►│   │  Raspberry Pi  │       │
 │   │ TP-Link 5G │──────────│ MacBook │───UDP────►│   │       4        │──────►│──► Internet
 │   │  (op. 2)   │          │   M4    │    WG2    │   │   (concentrateur)│      │  (via fibre 10G)
 │   └────────────┘          └─────────┘           │   └───────────────┘       │
 │                                                  │           │                │
 │  2+ interfaces physiques                         │   (routage NAT / WG)       │
 │  1 interface virtuelle utun                      │                            │
 └──────────────────────────────────────────────────┴────────────────────────────┘
```

- **Côté MacBook** : une Network Extension présente une `utun` virtuelle ; tous
  les paquets sortants y entrent et sont dispatchés vers les interfaces
  physiques via plusieurs tunnels UDP parallèles (un tunnel WireGuard par
  interface).
- **Côté Pi** : reçoit les N tunnels, réassemble l'ordre, NAT vers la fibre,
  gère la route de retour symétrique.

## 5. Matériel

### À acquérir

| Item | Modèle recommandé | Prix indicatif | Justification |
|---|---|---|---|
| Raspberry Pi 4 | Pi 4 Model B 4 GB | 55-70 € | Gigabit Ethernet réel + 4 GB RAM = tu ne seras jamais contraint par le matériel |
| microSD | Samsung Evo Plus 32 ou 64 Go (classe 10, A2) | 10 € | Ne pas rogner ici — la SD est la 1re cause de panne d'un Pi |
| Boîtier ventilé | Argon NEO ou équivalent alu | 15-25 € | Dissipation passive suffisante pour charge constante |
| Alim officielle 3 A USB-C | Officielle Raspberry Pi | 10 € | Alim low-cost = sous-tension = pannes aléatoires |

Total estimé : **90-115 €**.

### Déjà disponible

- Fibre 10 Gb/s symétrique
- Pi 2B (reste dédié à pi-hole, non touché par ce projet)
- Switch / routeur domestique (à identifier : supporte-t-il la QoS ? port libre ?)

## 6. Stack logicielle

### Pi (serveur concentrateur)

| Couche | Choix retenu | Alternative |
|---|---|---|
| OS | Raspberry Pi OS Lite 64-bit (Debian 12) | Ubuntu Server 24.04 |
| Tunnel VPN | WireGuard (kernel-mode) | OpenVPN (plus lent) |
| Bonding | **engarde** ou **mptcpd** | Script `pf`/`iptables` maison |
| Supervision | `node_exporter` + Grafana Cloud (gratuit) | Netdata |
| Durcissement | `fail2ban`, `ufw`, SSH clé uniquement | `nftables` à la main |
| DNS dynamique | `ddclient` vers DuckDNS | IP fixe FAI si dispo |

### MacBook (client)

| Couche | Choix retenu | Alternative |
|---|---|---|
| Intégration OS | Network Extension (`NEPacketTunnelProvider`) | `pf` local + tunneling user-space |
| Langage | Swift + PyObjC (Swift pour la Network Extension, Python pour monitoring) | Tout Swift |
| UI | NetHealth multi-interfaces (étendu) | Nouvelle app dédiée |
| Bonding client | Cohérent avec le choix Pi (engarde ou WG+custom) | Speedify comme comparatif |

## 7. Jalons

### Phase 0 — Préparation (1-2 semaines)

- [ ] Acheter le Pi 4 + accessoires
- [ ] Flasher Raspberry Pi OS Lite 64
- [ ] Durcir SSH (clé only, port non-standard, fail2ban)
- [ ] Configurer firewall minimal (accepter seulement SSH + UDP WireGuard)
- [ ] Mettre en place DDNS (DuckDNS) et vérifier la résolution
- [ ] Tester le port forwarding UDP sur la box

### Phase 1 — Tunnel simple WireGuard (1 semaine)

Objectif : avoir **une seule** connexion VPN MacBook ↔ Pi, qui marche depuis
l'extérieur. Sert de base saine avant d'empiler le bonding.

- [ ] Installer WireGuard côté Pi, générer clés + conf
- [ ] Installer WireGuard côté Mac (app officielle ou CLI)
- [ ] Vérifier le ping bidirectionnel et la traversée NAT
- [ ] Mesurer latence + débit au travers (baseline)

### Phase 2 — Bonding expérimental (2-4 semaines)

- [ ] POC `engarde` avec 2 uplinks simulés (Wi-Fi domicile + smartphone en partage)
- [ ] Mesurer gain vs tunnel simple (débit, latence, stabilité à la reprise)
- [ ] Si pas satisfaisant : tester alternatives (`mptcpd`, script maison)
- [ ] Trancher la stack finale

### Phase 3 — Intégration NetHealth (1-2 semaines)

- [ ] Faire dialoguer NetHealth (monitoring) avec le bonding (contrôle)
- [ ] Bouton menu « Activer / désactiver bonding »
- [ ] Affichage en temps réel : quel % du trafic passe par chaque uplink

### Phase 4 — Durcissement & auto (1-2 semaines)

- [ ] `systemd` services pour relancer auto après crash
- [ ] Rotation des logs
- [ ] Alertes si le Pi n'est plus joignable (vers un canal Pushover / Telegram)
- [ ] Documentation d'exploitation

## 8. Décisions à prendre

Questions ouvertes à trancher avant d'attaquer vraiment Phase 2 :

1. **engarde vs mptcpd vs maison ?** — engarde est le plus simple à déployer
   aujourd'hui, mptcpd est plus « standard » mais moins pragmatique sur macOS.
2. **UI client : Network Extension dédiée ou WG standard avec logique de
   split côté Mac ?** — la Network Extension est plus propre mais demande un
   dev Swift important.
3. **Priorité de routage** : round-robin bête, ou basé sur la qualité mesurée
   (santé de chaque uplink) ? Le 2e est plus intelligent mais nécessite un
   feedback loop depuis NetHealth.

## 9. Risques identifiés

| Risque | Probabilité | Impact | Mitigation |
|---|---|---|---|
| Le Pi n'est pas joignable depuis l'extérieur | Moyenne | Critique (tout tombe) | Monitoring externe + alerte + redondance Pi2B en secours |
| CG bloque les ports UDP chez un opérateur mobile | Moyenne | Élevé | Fallback TCP (dégradé), ou port 443 |
| Latence ajoutée trop élevée pour visio | Faible | Moyen | Profil par app (visio = uplink direct, reste = bondé) |
| Apple casse Network Extension dans une maj macOS | Faible | Élevé | Plan B : tout en user-space SOCKS |
| Pi HS (SD corrompue) | Moyen (Pi 2B a 8 ans) | Moyen | Backups img + Pi neuf en secours |

## 10. Évolutions futures

- **Bascule sur Mac mini** : quand le nouveau Mac mini arrive, migrer le
  concentrateur bonding dessus (plus de CPU, pas de soucis de SD) et
  redéployer le Pi 4 sur un autre usage.
- **Multi-concentrateur** : plusieurs Pi à différents endroits (ex. un au bureau,
  un à la maison) avec bascule auto selon la latence.
- **Exposer le bonding à d'autres appareils** : iPhone, iPad, tablette, via
  profil WireGuard partagé.
- **Intégrer la mesure NetHealth** directement pour piloter le routage intelligent.

## 11. Liens utiles

- [`engarde`](https://github.com/porech/engarde) — bonding UDP multi-uplink en Go
- [WireGuard](https://www.wireguard.com/) — VPN moderne, ultra-léger
- [`mptcpd`](https://github.com/multipath-tcp/mptcpd) — daemon MPTCP
- [Apple Network Extension](https://developer.apple.com/documentation/networkextension) — API officielle Apple
- [DuckDNS](https://www.duckdns.org/) — DDNS gratuit
- [Raspberry Pi 4 docs](https://www.raspberrypi.com/documentation/) — base officielle

---

_Dernière mise à jour : à la création du document. À jour à chaque avancée._
