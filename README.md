# Boussole Crypto — Funding Desk

Application Streamlit de surveillance des fundings perpétuels Bitget, Kraken EU*, BloFin, Gate.io et Hyperliquid.

## Fonctionnement

- Open interest et volume 24 h issus des API officielles, normalisés en USD.
- Variations OI/volume issues de deux snapshots officiels espacés exactement de 24 h ; aucune interpolation et aucun delta entre rafraîchissements.
- Feed public officiel des PERP 1 h, 4 h et 8 h.
- Alerte à partir de `±0,50 %` et surveillance renforcée à partir de `±0,40 %`.
- Funding courant Kraken séparé de sa prévision.
- Formules Kraken distinctes pour les contrats linéaires `PF_` et inverses `PI_`.
- Échéances Bitget/BloFin/Gate.io fournies par l’exchange ; cadences Kraken et Hyperliquid dérivées de leurs spécifications horaires avec une horloge officielle.
- Couverture présentée séparément en PERP long, PERP short, margin long et margin short.
- Conservation temporaire du dernier snapshot valide en cas de panne, marqué `PÉRIMÉ` et exclu des alertes.
- Aucun spot simple, aucune donnée de marché fictive et aucun ordre réel.


## Portefeuille paper live

- Capital fictif, mais entrées et sorties calculées sur les carnets publics réels des deux plateformes.
- Ouverture autorisée seulement à `±0,50 %` ; la zone `±0,40–0,50 %` reste une surveillance.
- Prix d’entrée au VWAP, contrôle de profondeur, frais taker configurables et marge liée au levier.
- Calcul pré-trade du coût aller-retour au carnet visible, du funding net minimum et du signal minimum requis.
- Fundings Bitget, BloFin et Gate.io crédités uniquement après confirmation de l’historique officiel et d’un prix de marque officiel.
- Funding de la couverture compté dans la fenêtre uniquement si son échéance précède ou coïncide avec celle du signal.
- Clôture des deux jambes au VWAP du carnet courant et journal net des frais.
## Lancer localement

```powershell
uv run --with-requirements requirements.txt streamlit run streamlit_app.py
```

Puis ouvrir `http://localhost:8501`.

### Lancer la validation empirique continue

```powershell
uv run --with-requirements requirements.txt python run_funding_experiments.py
```

Le collecteur enregistre les carnets réels autour des échéances dans
`data/funding_experiments.sqlite3`. Il ne passe aucun ordre. La matrice peut
être consultée dans l’onglet **Validation empirique** ou exportée avec :

```powershell
uv run --with-requirements requirements.txt python analyze_funding_experiments.py
```

La formule, les fenêtres et les critères de validation sont détaillés dans
`docs/FORWARD_TEST_PROTOCOL.md`.

## Déploiement Streamlit Community Cloud

Le point d’entrée est `streamlit_app.py`. Les dépendances sont épinglées dans `requirements.txt` et le thème se trouve dans `.streamlit/config.toml`.

### Historique OI et volume sur 24 h

Le tableau n’affiche une variation que si deux snapshots du même exchange et du même symbole existent dans deux tranches UTC de cinq minutes espacées exactement de 24 heures.

- `collect_market_history.py` effectue une collecte ponctuelle depuis les cinq API officielles.
- `render.yaml` décrit un collecteur Render exécuté toutes les cinq minutes et une base PostgreSQL persistante.
- Le collecteur et Streamlit reçoivent la chaîne de connexion uniquement via `HISTORY_DATABASE_URL`.
- Aucune clé de trading, clé privée ou donnée de portefeuille n’est utilisée.
- La rétention est limitée à 72 heures.
- Les pourcentages commencent à apparaître 24 heures après le démarrage continu de la collecte. Avant cela, l’interface affiche `—`.

## Limites importantes

- Les lectures REST sont quasi temps réel, pas tick-par-tick.
- Les métadonnées publiques margin ne garantissent pas la quantité réellement empruntable au moment d’un ordre.
- L’éligibilité réglementaire Kraken EU doit être confirmée par le compte connecté.
- Streamlit Community Cloud peut mettre une application inactive en veille.
- Le collecteur à la seconde doit donc tourner sur une machine ou un worker permanent ; le processus local s’arrête si l’ordinateur s’éteint.
- Cette version n’accepte aucune clé API et n’exécute aucun ordre.
- Le cadre de recherche et les conditions d’automatisation sont documentés dans `docs/FUNDING_STRATEGY_RESEARCH.md`.

- Le journal paper reste attaché à la session Streamlit ; une persistance multi-session exige une base et une identité utilisateur.
- La liquidation exacte et les contraintes de marge du compte ne sont pas simulées sans connecteur authentifié en lecture seule.
- Le funding réalisé Kraken n’est pas crédité sans historique de compte authentifié ; aucune approximation ne le remplace.
