# Protocole de validation pratique — Funding Desk

## Objectif

Le moteur ne cherche pas à confirmer une intuition. Il mesure, sur des
échéances réellement survenues, si une route PERP/PERP aurait été rentable
avec la profondeur réellement visible au moment de l'entrée et de la sortie.
Il ne passe aucun ordre.

## Grille enregistrée

- Entrées : T−15 min, T−10 min, T−5 min, T−2 min, T−1 min, T−30 s.
- Sorties : T+5 s, T+30 s, T+1 min, T+2 min, T+5 min, T+15 min.
- Notionnels globaux : 1 000, 2 000, 4 000 et 8 000 USD, divisés à parts égales entre les deux jambes.
- Leviers analysés : 3×, 5×, 6× et 10×.
- Signal de recherche : à partir de ±0,01 %. Les alertes de l'interface restent inchangées à ±0,40 % et ±0,50 %.

Le seuil de recherche volontairement bas permet d'observer aussi les échecs et
de calculer le vrai point mort. Il ne constitue pas une recommandation d'entrée.

## Équation réalisée

Pour chaque jambe `j` :

```text
quantité_j       = notionnel_j / VWAP_entrée_j
PnL_marché_j     = direction_j × quantité_j × (VWAP_sortie_j − VWAP_entrée_j)
funding_j        = quantité_j × mark_règlement_j × taux_réglé_j × signe_j
frais_j          = (notionnel_entrée_j + notionnel_sortie_j) × taux_frais_j

Net_USD          = Σ PnL_marché_j + Σ funding_j − Σ frais_j
Rendement notionnel = Net_USD / notionnel_global
ROE au levier L  = Net_USD / (notionnel_global / L)
```

Le slippage n'est pas ajouté sous forme d'hypothèse : il est déjà inclus dans
les VWAP calculés niveau par niveau sur les carnets enregistrés. Le PnL marché
agrège donc spread, slippage et mouvement de la base entre les deux PERP.

Le levier ne crée aucun revenu de funding supplémentaire à notionnel constant.
Il réduit la marge immobilisée et amplifie le ROE, les pertes et le risque de
liquidation. Il n'est jamais utilisé comme condition permettant de rendre une
route déficitaire admissible.

## Funding minimum empirique

Pour une observation :

```text
funding_point_mort_% = (frais − PnL_marché) / notionnel_d'une_jambe × 100
```

Pour une cellule définie par plateforme, tranche, fenêtre d'entrée, fenêtre de
sortie et taille, le scanner calcule le 90e et le 95e percentile du point mort.
Le seuil d'entrée prudent sera donc :

```text
funding_net_attendu_% >= P95(funding_point_mort_%)
```

Cette règle signifie que le funding attendu couvre les coûts et la dégradation
de base observés dans 95 % de l'échantillon historique de cette cellule. Elle
n'est activable qu'après au moins 30 observations indépendantes et si le 10e
percentile du résultat net reste positif. Avant cela, le statut est `COLLECTE`.

## OI, volume et taille

L'open interest et le volume ne sont pas artificiellement additionnés au PnL.
Ils servent à segmenter le risque d'exécution :

```text
ratio_OI      = notionnel_d'une_jambe / min(OI_USD_des_deux_jambes)
ratio_volume  = notionnel_d'une_jambe / min(volume_24h_USD_des_deux_jambes)
```

Le seuil acceptable sera appris en comparant ces ratios aux slippages et aux
résultats réalisés. Aucun seuil universel n'est imposé avant les observations.

## Données considérées comme complètes

Une observation est calculée uniquement si :

1. les deux carnets officiels sont présents aux fenêtres d'entrée et de sortie ;
2. la profondeur couvre la quantité exacte ouverte ;
3. chaque funding survenu pendant la position possède un taux réglé officiel et
   un prix de marque officiel de référence ;
4. les quatre frais taker sont présents ;
5. aucun financement d'une jambe n'est silencieusement ignoré.

Bitget, BloFin et Gate.io permettent actuellement le rapprochement public du
taux réglé et du mark de référence. Pour Kraken et Hyperliquid, la validation du
cashflow exact exigera un accès **lecture seule** à l'historique du compte. Les
routes dont leur paiement intervient dans la fenêtre sont exclues du jeu de
validation public, sans inventer de valeur.

## Limite restante avant automatisation réelle

Les taux de frais publics utilisés constituent une hypothèse conservatrice tant
que le tier réel du compte n'est pas connecté en lecture seule. Avant tout ordre
réel, le modèle devra remplacer ces taux par les frais effectifs du compte et
ajouter les règles exactes de marge/liquidation propres à chaque plateforme.
