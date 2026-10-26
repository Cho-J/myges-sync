# Limites

**API non publique et non versionnée.** La source utilisée est l'API d'une application mobile, sans documentation ni engagement de stabilité. Le flux d'authentification (OAuth implicite, jeton lu dans le fragment d'une redirection 302) et la structure de la réponse (`result`, `reservation_id`, `rooms`, `campus`…) ont été observés, pas spécifiés. Ils peuvent changer sans préavis. Le flux implicite est d'ailleurs déconseillé par OAuth 2.1 et pourrait disparaître. Les conditions d'utilisation de la source peuvent aussi restreindre ce type d'accès automatisé : à vérifier avant tout déploiement.

**Jeton et identifiants.** Le script ne garde pas le jeton : il en redemande un à chaque exécution avec l'identifiant et le mot de passe du compte. Il n'y a donc pas de jeton à renouveler à la main. La contrepartie : le mot de passe du compte source est stocké en clair dans le fichier de configuration, que seuls les droits Unix protègent. Si ce mot de passe change, la synchronisation échoue jusqu'à ce que le fichier soit mis à jour (une notification d'échec est envoyée).

**Plage de dates fixe.** `RANGE_START` et `RANGE_END` sont fixes, pour que les cours passés restent dans le calendrier. Une fois `RANGE_END` dépassée, plus rien de nouveau n'est récupéré, sans erreur ni alerte. Il faut repousser la date à la main.

**Cas non couverts, tels qu'ils ressortent du code :**

- **Calendrier non dédié.** Tout événement du calendrier dont l'UID n'existe pas à la source est supprimé, y compris un événement ajouté à la main. Le garde-fou des 50 % ne protège que les suppressions massives.
- **Petit calendrier.** En dessous de 10 événements, le garde-fou de suppression ne s'applique pas. Une réponse partielle mais non vide de la source peut alors supprimer presque tout.
- **Événements tous invalides.** Si la source renvoie des événements dont aucun n'a d'identifiant ni d'horaires, la liste normalisée est vide sans que le garde-fou n° 1 (réponse vide) se déclenche. Seul le garde-fou des 50 % s'applique ensuite.
- **Changement de format des UID.** Modifier la construction des UID ou `UID_DOMAIN` fait apparaître tous les événements comme supprimés puis recréés. Le garde-fou des 50 % bloque alors l'exécution : il faut une migration manuelle.
- **Événement passant d'un campus à plusieurs.** L'UID change, ce qui produit une suppression et une ou plusieurs créations au lieu d'une mise à jour.
- **Pas de verrou.** Deux exécutions simultanées (cron et lancement manuel) peuvent créer des doublons d'UID. Le script les signale à la lecture suivante, sans les corriger.
- **Pas de nouvel essai.** Une erreur réseau passagère fait échouer toute l'exécution, qui sera retentée au cron suivant.
- **Fuseau horaire.** Les horaires sont émis en UTC, en supposant que la source fournit de l'UTC. C'est vérifié sur la source actuelle, mais rien ne le contrôle.
- **Notification d'échec impossible** si la configuration est illisible ou si ntfy est lui-même en panne : l'erreur n'est alors visible que dans le journal.

**Tests.** Aucun test automatisé. Le script a été validé à l'usage, en production depuis la rentrée 2026, et le mode `--dry-run` sert de vérification avant chaque modification. TODO : ajouter des tests unitaires sur la normalisation, la comparaison et les garde-fous, avec une source et un calendrier simulés.
