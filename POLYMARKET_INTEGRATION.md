# Polymarket ↔ Pinnacle

Comparaison pré-match NFL et NCAA en lecture seule. Le rapport mobile est publié dans le résumé GitHub Actions. L’artefact contient la couverture par rencontre, les deux prix Pinnacle, les carnets Polymarket, les horaires et les décisions par sélection.

## Exécution

```sh
python -m unittest discover -s tests -p 'test_polymarket*.py' -v
python polymarket_pinnacle_desk.py --board-dir output \
  --source "$(git rev-parse HEAD)" --output-dir results
```

Le workflow lit les deux boards Pinnacle déjà publiés sur `main` au SHA indiqué dans le rapport. Il n’utilise ni OddsPapi ni clé Pinnacle. Les modifications du comparateur déclenchent une exécution sur la branche de travail et la PR. Le cron de 30 minutes et le lancement manuel nécessitent le workflow sur la branche par défaut. Leur activation attend la validation de la PR. Le cron relit la dernière publication disponible, dont la fréquence détermine la couverture des EV synchrones.

## Identité, prix et règlement

- Découverte paginée par séries NFL et College Football Gamma, horizon de dix jours. Les événements annexes de même match sont rattachés à la rencontre principale quand le kickoff concorde. Appariement strict des deux équipes normalisées et de l’horaire à 60 minutes près. Les ambiguïtés bloquent l’appariement et les écarts d’horaires figurent dans le CSV.
- Appariement au niveau de la rencontre, période, type, équipe et ligne. Les spreads sont normalisés sur le côté home Pinnacle. Les deux cotes doivent provenir du même marché et du même instant de collecte. Les lignes entières, props et marchés dont le règlement courant ne peut être confirmé sont écartés. Les moneylines NFL et de périodes sont écartées à cause du traitement des égalités.
- Les règles vérifiées couvrent le match terminé normalement. Les clauses d’annulation, de report et d’abandon peuvent payer 0,50 pUSD chez Polymarket alors que Pinnacle peut rendre la mise. L’EV affichée est conditionnelle au règlement normal et demeure un signal de revue. Les règles détaillées sont attachées au contrat Polymarket.
- Le prix vérifié est le meilleur ask du carnet CLOB, avec identifiant du token et de la condition, heure de récupération et horodatage du carnet. Les filtres EV exigent une profondeur d’au moins 10 pUSD au meilleur ask, la taille minimale CLOB, un spread bid/ask de 0,10 maximum, un carnet de deux minutes maximum et des captures Pinnacle/Polymarket espacées de cinq minutes maximum avant le coup d’envoi.
- Fair Pinnacle = inverse de la cote divisé par la somme des deux inverses. Coût net par part = ask + taux taker du contrat × ask × (1 − ask). EV = fair / coût net − 1. Le BUY PRICE est un ask maximal, arrondi au tick inférieur, pour viser +2 % d’EV. Les observations désynchronisées ont des colonnes historiques distinctes et ne peuvent produire de signal courant.

[Documentation Polymarket sur les frais](https://docs.polymarket.com/trading/fees) · [Marchés Pinnacle](https://www.pinnacle.com/en/future/bets-sports-offered)

## Audit du prototype

Le [run #31](https://github.com/Ritchy-jpg/NFL-Desk-Snapshots/actions/runs/42923531472) a produit 352 lignes Polymarket sur Kent State–Bowling Green et aucune comparaison. Le comparateur manuel attendait un CSV absent, indexé par identifiants Polymarket. Le second comparateur utilisait l’intersection GP/Pinnacle, où un total exact n’est conservé que si Golden Palace a publié la sélection correspondante. Sur l’entrée du prototype, seul 44,5 figurait dans l’intersection GP/Pinnacle pour ce match. L’artefact Polymarket contenait 41,5, 43,5 et 45,5, qui figurent tous dans le board Pinnacle. La découverte se limitait aux noms Kent State et Bowling Green avec pagination globale. Le workflow acceptait les résultats vides.

Les résultats distinguent maintenant les rencontres communes, les marchés exacts, les asks vérifiés et les EV synchrones. Le job échoue si une ligue avec un board pré-match ne produit aucun ask vérifié ou si la collecte CLOB échoue. Une exécution sans EV synchrone peut encore contenir de vrais appariements et des prix historiques vérifiés.
