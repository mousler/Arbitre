# Plan de simulation et de stabilisation Rhetora

## Objectif

Vérifier les parcours, les performances et les contrôles de sécurité de Rhetora dans un environnement local isolé, avec des acteurs reproductibles et des résultats consultables. Aucun essai de charge ne vise le serveur de production.

## Progression

- [x] Rendre le chemin SQLite configurable avec `RHETORA_DATA_DIR`.
- [x] Rendre le port configurable avec `ARBITRE_PORT`.
- [x] Valider l'initialisation SQLite dans un dossier temporaire distinct.
- [x] Réserver les transactions SQLite des routes qui allouent des identifiants, après détection d'une collision sous concurrence.
- [x] Ajouter une simulation communautaire bornée : comptes, connexions, analyses déterministes, publications, commentaires, réactions, profils et messages privés.
- [x] Faire passer les analyses par la vraie file de tâches en mode sandbox, sans appel externe.
- [x] Ajouter une console Rhetora locale avec lancement, progression, chronologie, latences et rapport.
- [x] Ajouter des durées configurables, quatre profils d'activité, un registre filtrable de 500 comptes maximum, un fil social nominatif et un arrêt demandé proprement.
- [ ] Ajouter des parcours navigateur pour vérifier les écrans et les erreurs visibles.
- [x] Étendre les contrôles défensifs : permissions entre comptes, CSRF, limites de débit, sessions et entrées invalides.
- [x] Valider un palier à 500 utilisateurs et 10 actions concurrentes, sans relever les limites de protection.
- [x] Ajouter des rôles séparés (exploitation/support, QA, performance, sécurité) produisant un rapport, sans accès à la production.
- [x] Ordonnancer les acteurs indépendamment, borner les tâches et les historiques, et ajouter des tests de non-régression.
- [x] Faire produire à chaque acteur une analyse complète, avec synthèse et arbre argumentatif déterministes.
- [x] Ajouter des sondes sécurité XSS, IDOR, injection SQL, en-têtes HTTP et fichiers protégés; afficher les recommandations sans les confondre avec des échecs.
- [x] Ajouter le journal des étapes d'agents et comparer les contrôles et p95 entre exécutions comparables.
- [ ] Mesurer CPU, mémoire et files d'attente sur des essais longs et plusieurs paliers de concurrence.

## Simulation complète actuelle (API)

Lancer depuis PowerShell à la racine du dépôt :

```powershell
python simulate.py
python simulate.py --users 25 --concurrency 5
python simulate.py --users 50 --concurrency 10 --duration-seconds 180 --mode balanced
python simulate.py --users 500 --concurrency 10 --duration-seconds 30 --pace load --seed 42 --ramp-seconds 10
python -m unittest test_simulate -v
```

Après redémarrage du serveur Rhetora, la console visuelle est disponible sur `http://localhost:8766/simulation.html`. Elle permet de choisir 5 à 500 acteurs, 30 secondes à 20 minutes d'activité, jusqu'à 50 actions concurrentes et cinq profils (écosystème mixte, communauté, conversations, lecture/réactions, créateurs). Le registre est filtrable par compte ou par profil. Le fil social montre les échanges; le tableau d'audit expose chaque contrôle et son verdict. Le bouton d'arrêt laisse finir les actions en cours, sans lancer les suivantes. La prévisualisation de validation est `http://127.0.0.1:18767/simulation.html`, avec sa propre base et son propre état temporaires.

Les rapports CLI sont enregistrés dans `simulation-results/`. Le serveur de test utilise un port libre sur `127.0.0.1`, une base temporaire supprimée à la fin, et un environnement sans clés API ni secrets hérités. Le premier compte est réservé à l'entreprise, car l'application lui attribue le rôle administrateur. Les utilisateurs simulés ont tous le rôle normal et ouvrent une session par l'API. Un utilisateur est inscrit par l'API; les autres sont des fixtures pour respecter la limite par IP. Les comptes dédiés aux audits portent le total à six inscriptions API réussies par exécution complète; une septième doit être refusée avec HTTP 429. Les comptes d'entreprise et d'audit ne sont pas inclus dans le nombre d'utilisateurs demandé.

Le texte suit la vraie route `/api/analyze-text`, le système de tâches et la chaîne de fusion; le moteur de sortie est déterministe et local. Les acteurs publient le résultat, commentent, réagissent, modifient leur profil, envoient un message et lisent la conversation entrante. Aucune clé réelle n'est transmise au serveur enfant.

Cette version ne teste **pas** YouTube, la transcription audio, les courriels ni la qualité sémantique d'un fournisseur IA réel. Les acteurs utilisent un fournisseur déterministe local. Les parcours utilisateurs traversent les API, pas les écrans de l'application. Les sondes XSS vérifient les API et l'intégrité des contenus; elles ne prouvent pas l'absence de XSS dans tous les rendus navigateur. Les audits ciblés ne constituent pas un audit de sécurité exhaustif, une validation TLS ou une certification de production. Les limites sont de 500 utilisateurs et 50 actions concurrentes; commencer à 10, examiner le rapport, puis augmenter graduellement.

## Voir la simulation dans le site

Le panneau « Rhetora en direct » de la console expose le vrai site de la sandbox pendant le scénario. Choisir un compte, puis ouvrir son fil, sa messagerie ou son profil : le site s'ouvre dans un onglet `rhetora-sandbox`, connecté sous ce compte. Le réglage « Site après le test » garde la sandbox ouverte 10 minutes, 30 minutes ou 1 heure après le rapport; « Fermer le site » l'arrête plus tôt. Lancer un nouveau scénario ferme la sandbox précédente.

Chaque interaction du fil social et chaque compte du registre sont cliquables. Le détail affiche le compte, son profil, le destinataire, la publication, l'heure et le contenu exact : message, commentaire, publication, synthèse d'analyse, titres vus dans le fil ou derniers messages lus. Les boutons ouvrent la publication, la conversation du point de vue de chaque participant ou le profil. L'historique d'un compte inclut les actions envoyées et reçues, depuis le journal `simulation-results/live-activity.jsonl`.

L'ouverture de session passe par `POST /api/simulation/view-as`, actif seulement dans le serveur sandbox. Il exige une clé aléatoire propre à l'exécution, accepte uniquement les requêtes loopback et limite les destinations à des routes internes du site. La clé reste dans l'état en direct de la console et n'est pas copiée dans le rapport. Les sessions sandbox utilisent le cookie `rh_sim_session`, distinct de `rh_session`. Un seul compte est connecté à la fois dans le navigateur : ouvrir un autre compte remplace la session sandbox précédente. Le journal est remplacé au prochain scénario; le rapport conserve les 200 derniers événements.

## Grande population et parcours complets

Une simulation peut compter jusqu'à 10 000 comptes, avec 100 actions simultanées au maximum. Tous les comptes peuvent tout faire : se connecter et se déconnecter, lire et parcourir le fil, publier une analyse, commenter, répondre, réagir à une publication ou à un commentaire, voter, s'abonner et se désabonner, écrire et lire des messages, consulter les notifications, chercher des membres, explorer, consulter le classement et les profils, et signaler un contenu. Le persona (créateur, communauté, messagerie, lecteur, polyvalent) change seulement la fréquence relative des actions; « Tous polyvalents » les rend équiprobables.

- **Cohorte initiale** : au plus 100 comptes se connectent, analysent et publient avant la phase active; les autres arrivent progressivement pendant la durée choisie.
- **Comptes qui se connectent** : part des comptes hors cohorte qui visitent le site pendant le test (5 %, 10 %, 25 %, 50 % ou 100 %). Les autres restent présents comme membres consultables, suivis ou contactés.
- **Nouvelles inscriptions** : 0 à 600 par minute via la vraie route `/api/auth/register`. Chaque nouveau compte complète son profil, s'abonne, parcourt le fil puis vit comme les autres.
- **Modération** : le compte entreprise relit la file des signalements et classe jusqu'à 5 signalements toutes les 10 s en charge, ou 30 s au rythme réaliste.
- **Adresses distinctes** : chaque compte utilise sa propre adresse loopback (`127.10.x.y`). Les limites anti-abus par IP s'appliquent comme pour des visiteurs distincts, sans être désactivées.
- **Rythme** : les délais par famille d'actions restent sous les limites de l'application (commentaires, réactions, abonnements, messages, publications, signalements).

Les connexions passent par une voie réservée au quart de la concurrence. Elles ne bloquent donc pas les comptes déjà en ligne. Le contrôle `login_capacity` échoue si des arrivées attendent depuis plus de 10 s à la fin. Le contrôle QA `population_consistency` compare la base aux créations confirmées : comptes, signalements et signalements classés.

Le registre de la console interroge `GET /api/simulation/actors` (recherche côté serveur, 100 résultats par page). L'état en direct ne transporte plus la liste complète des comptes, mais un résumé de population.

## Acteurs et équipe Rhetora

Les agents sont des automates scénarisés locaux, sans LLM ni frais externes. Dans le profil mixte, les acteurs se répartissent entre créateurs, participants communautaires, utilisateurs de messagerie et lecteurs. Chacun possède une session et un générateur de décisions distincts. Un acteur lent n'empêche pas les autres acteurs disponibles de repartir; une même session n'est jamais utilisée simultanément par deux actions de son acteur.

- `--pace realistic` : délai de 15 à 25 secondes après chaque action; `--pace load` : 1 à 3 secondes. Les écritures ont des délais supplémentaires pour respecter les quotas normaux.
- `--seed 42` : reproduit les décisions de chaque acteur. Les identifiants de sandbox, les latences et le nombre d'actions terminé dans la durée restent variables.
- `--ramp-seconds 10` : répartit le démarrage de l'activité; la rampe effective est plafonnée à la moitié de la durée active. La préparation initiale reste limitée par la concurrence.
- Les créateurs peuvent refaire des analyses et des publications durant l'activité, avec au maximum deux supplémentaires par acteur et par type. Les lecteurs et la messagerie exercent aussi déconnexion/reconnexion.
- L'entreprise consulte ses statistiques et sa file de modération, reçoit une demande de support et répond; le parcours vérifie la réception dans les deux sens.

L'équipe produit 31 contrôles par exécution sans phase active (2 exploitation/support, 4 QA, 2 performance et 23 sécurité); une phase active ajoute le contrôle de capacité des connexions. Les sondes sécurité vérifient les en-têtes HTTP, la CSP, les fichiers et chemins protégés, les XSS réfléchis et stockés, les accès IDOR aux tâches, notifications et corbeille, les injections SQL, ainsi que les contrôles déjà présents (droits, CSRF, confidentialité, sessions et limites de débit). Une recommandation comme l'absence de CSP ou l'accès à une tâche par son identifiant apparaît comme « à améliorer »; elle reste visible sans transformer un avertissement en échec. Un sandbox ne peut pas lancer une autre simulation.

La console présente le journal chronologique des étapes des agents, leur verdict et leur observation. Elle compare les contrôles à la référence précédente; les p95 ne sont comparés que si utilisateurs, concurrence, durée, profil, rythme, graine, rampe et paramètres d'arrivée correspondent. Les écarts de p95 sont signalés au-delà de 20 % et 50 ms.

## Mesures et verdict

`--p95-budget-ms 2000` fixe un budget p95 par action, hors sondes de sécurité. Un dépassement fait échouer le contrôle de performance même si toutes les réponses HTTP sont correctes. Un refus attendu, comme HTTP 403 pour un accès interdit ou HTTP 429 pour une limite, compte comme un contrôle réussi; une autorisation inattendue échoue.

Les compteurs de requêtes, de statuts HTTP et d'erreurs restent exacts. La mémoire des événements et erreurs est bornée; les latences conservent au plus 10 000 échantillons par action, puis utilisent un échantillonnage réservoir. Les p50/p95/p99 deviennent donc des estimations au-delà de cette limite; `latency_sample_count` l'indique dans le rapport. Le débit affiché est une moyenne du scénario complet, préparation et attentes comprises, pas la capacité maximale du serveur.

Les paramètres sont verrouillés pendant l'exécution; le rafraîchissement ne remplace pas une nouvelle saisie lorsque la console est à l'arrêt. Le rapport contient `checks`, `audit_team`, `audit_summary`, `agent_activity`, `comparison`, `error_count`, `request_count` et les mesures par action. Les avertissements ne changent pas le code de sortie : 0 signifie aucun contrôle en échec, 1 signale au moins un échec et 130 un arrêt demandé. L'absence de contrôles après annulation ou échec de préparation n'est pas considérée comme un audit réussi.

## Critères de passage

- Toutes les actions répondent avec le statut attendu, y compris les refus défensifs explicites.
- Tous les contrôles d'audit passent et le p95 par action respecte le budget configuré.
- Le rapport donne le nombre de succès/échecs et les latences p50, p95, p99 et maximum par action.
- Aucune clé API, aucun message réel et aucune donnée de la base `data/rhetora.db` n'est utilisé par la simulation.
- Un échec est reproductible et attribué à une action plutôt que masqué dans une moyenne globale.

## Validations récentes

- 25 acteurs, 60 secondes, profil mixte : 390 requêtes, zéro erreur; comptes et fil social visibles dans la console.
- 100 acteurs, 30 secondes, profil mixte : 1 401 requêtes, zéro erreur.
- Demande d'arrêt pendant la préparation de 250 acteurs : arrêt propre après 63 requêtes, zéro erreur.
- Version enrichie, 500 acteurs, 10 actions concurrentes, 30 secondes actives en charge : 9 638 requêtes, zéro erreur, 20/20 contrôles réussis; p95 maximal de 752,89 ms. Exécution complète : 119,12 secondes, débit moyen de 80,91 requêtes/s. Rapport : `simulation-results/run-20261027-200826-29c8ff.json`.
- Version enrichie : dix tests unitaires passent, y compris mémoire bornée, annulation d'une analyse sans faux échec, acteur lent, refus attendus et fermeture SQLite sous Windows.
- Population : 13 tests passent. 150 comptes et 30 inscriptions par minute : 4 627 requêtes, zéro échec, 21/21 contrôles. 10 000 comptes, 50 actions simultanées, 5 % de visiteurs, 30 inscriptions par minute pendant 2 minutes : 9 803 requêtes sans échec HTTP, 581 connexions, 26 inscriptions, 185 signalements, 296 abonnements, aucune connexion bloquée. Le budget p95 de 2 s est légèrement dépassé par `community.comment` (2,25 s) et `moderation.resolve` (2,04 s).
- À 100 % de visiteurs, une connexion PBKDF2 coûte environ 133 ms : la machine de validation sert environ 5 à 9 connexions/s sous charge mixte. Prévoir environ 20 minutes pour faire passer 10 000 comptes, ou réduire la part de visiteurs.
- 2026-10-31, simulation API 10 utilisateurs : 209 requêtes, 0 échec, 10/10 analyses complètes avec arbre argumentatif; 29 contrôles réussis, 2 recommandations et 0 échec. Rapport : `simulation-results/run-20261031-204214-91b429.json`.
- 2026-10-31, console navigateur : 5 utilisateurs, 30 s, 59 étapes d'agents affichées, 30 contrôles réussis, 2 recommandations, 0 échec; comparaison au rapport précédent, sans débordement horizontal à 390 px.
- Console : scénario de 50 acteurs terminé avec 20/20 contrôles, puis arrêt et relance de cinq acteurs avec paramètres personnalisés; dernier scénario : 183 requêtes, zéro erreur, 20/20 contrôles. Paramètres et refus des saisies invalides vérifiés dans le navigateur; captures desktop/mobile et absence de débordement horizontal vérifiées de 320 à 1 440 pixels.

La durée sélectionnée correspond à la phase d'interactions répétées après la préparation initiale (comptes, connexions, analyses et publications). Les audits se déroulent ensuite. Une durée active de 30 secondes n'implique donc pas une exécution totale de 30 secondes.

## Séquence proposée

1. **Parcours** : enrichir les scénarios fonctionnels, puis ajouter les tests navigateur.
2. **Analyse** : compléter le fournisseur déterministe existant par un test d'intégration réel isolé, avec consentement et contrôle des coûts.
3. **Sécurité** : étendre les sondes existantes aux téléchargements, aux pièces jointes et aux erreurs de rendu navigateur.
4. **Capacité** : mesurer CPU, mémoire, débit, erreurs SQLite et files d'analyse avant de choisir une migration ou une architecture distribuée.
5. **Équipe d'audit** : enrichir les rôles scénarisés existants; toute intégration d'agents LLM et toute remédiation automatique nécessitent une étape distincte, avec contrôle des coûts et des permissions.