# Boussole Crypto — cadre de stratégie funding

Mise à jour : 11 octobre 2026. Ce document sépare volontairement les faits
observés, les coûts mesurables et les hypothèses à éprouver en paper trading.

## 1. Conclusion opérationnelle

La stratégie de départ ne doit pas être « funding supérieur à un seuil = ordre ».
La règle robuste est :

> ouvrir uniquement si le cash-flow de funding conservateur de la route dépasse
> le coût aller-retour mesuré, le coût d'emprunt éventuel et une réserve de risque
> de base, avec deux carnets frais et assez profonds.

La route PERP/PERP est prioritaire pour la phase automatisée : elle évite
l'incertitude de disponibilité et de taux d'emprunt du spot margin. La route
PERP/spot-margin reste valable lorsqu'une API de compte confirme la quantité
empruntable et son taux réel au moment de l'ordre.

Un funding de 0,50 % appliqué à une jambe PERP de 2 000 USD produit 10 USD avant
coûts. Le levier ne transforme pas ce cash-flow en 100 USD : il réduit la marge
immobilisée. Le rendement sur marge peut donc être amplifié, mais aussi le risque
de liquidation et le besoin de réserve de collatéral.

## 2. Équation de décision

Pour deux jambes de notionnel `N` :

```text
Funding net attendu
  = N × (cash-flow funding jambe signal + cash-flow funding couverture)

Coût d'exécution
  = frais entrée des 2 jambes
  + frais sortie des 2 jambes
  + franchissement du spread et impact de carnet à l'entrée
  + franchissement du spread et impact de carnet à la sortie

Net conservateur
  = Funding net attendu
  - coût d'exécution
  - intérêt d'emprunt spot-margin
  - coût de rebalancement
  - réserve de risque de base
```

Le seuil de rentabilité affiché par l'application est désormais calculé à partir
des deux carnets réels : elle simule une ouverture puis une fermeture immédiate
avec la profondeur visible et quatre frais taker. C'est un plancher exécutable
maintenant, pas une prévision du carnet futur. Une sortie future exacte est
inconnaissable avant qu'elle n'existe.

Exemple de frais seuls : avec 0,05 % taker sur chaque plateforme, deux jambes de
2 000 USD ouvertes puis fermées coûtent environ 4 USD, soit 0,20 % du notionnel
de la jambe qui reçoit le funding. Le spread, l'impact et la base s'ajoutent.

## 3. Entrer une ou deux minutes avant l'échéance

Cette idée est une hypothèse à tester, pas la règle par défaut. Elle réduit le
temps d'exposition, mais concentre plusieurs risques :

- le taux « courant » peut encore évoluer avant le règlement selon la plateforme ;
- d'autres arbitragistes entrent au même moment, dégradant spread et profondeur ;
- la base PERP/PERP ou PERP/spot peut se détendre juste après le paiement ;
- les deux jambes peuvent ne pas être exécutées simultanément ;
- une position ouverte trop près de la borne peut ne pas être éligible au paiement
  si l'accusé d'exécution arrive après le snapshot de règlement ;
- un ordre maker évite éventuellement un frais taker mais ne garantit pas le fill.

Le test live doit comparer les couples suivants sur chaque plateforme et chaque
actif, sans extrapolation :

- entrées : T−15 min, T−10 min, T−5 min, T−2 min, T−1 min, T−30 s ;
- sorties : T+5 s, T+30 s, T+1 min, T+2 min, T+5 min, T+15 min ;
- mesures : taux finalement réglé, VWAP des deux jambes, frais réels, évolution de
  la base, slippage, exécution partielle, meilleur net et pire excursion.

La fenêtre gagnante doit être choisie par quantiles de résultat net, pas par
moyenne : médiane positive, cinquième percentile acceptable et assez
d'observations par venue/actif/tranche.

## 4. Filtres obligatoires avant toute automatisation

1. Taux, tranche et échéance provenant de l'API officielle et horodatage frais.
2. Deux carnets frais, écart d'horodatage inférieur à la limite et profondeur
   suffisante au notionnel demandé.
3. Frais exacts du compte ; un barème public n'est qu'une valeur par défaut.
4. Si spot-margin : produit listé, quantité réellement empruntable et taux
   d'emprunt confirmé par une API authentifiée en lecture seule.
5. Funding net des deux PERP : la couverture peut elle-même payer ou recevoir un
   funding pendant la fenêtre.
6. OI et volume suffisants, sans les confondre avec la profondeur réellement
   exécutable.
7. Delta en USD contrôlé après les deux fills ; un même notionnel nominal n'assure
   pas une neutralité parfaite lorsque prix, multiplicateurs ou quantos diffèrent.
8. Collatéral et marge de liquidation suffisants séparément sur chaque venue.
9. Aucun ordre si une API est périmée, si l'horloge diverge ou si une jambe échoue.
10. Sortie contrôlée : funding réglé confirmé, base sous limite, liquidité encore
    suffisante et mécanisme d'urgence testé.

## 5. Architecture d'exécution recommandée

L'automate doit suivre une machine d'état persistante :

```text
OBSERVATION → CANDIDATE → ARMED → LEG_1_PENDING → HEDGED
            → FUNDING_CONFIRMED → EXITING → CLOSED
```

Tout état intermédiaire possède un délai maximal et une action de récupération.
La première version réelle doit utiliser des clés dédiées sans retrait, des
limites de notionnel par venue, un plafond de perte, une limite de slippage, un
kill switch global et des identifiants d'ordre idempotents. La jambe la plus
liquide peut servir de hedge immédiat, mais le choix de l'ordre des jambes doit
être mesuré et non supposé.

## 6. Ce qui est déjà mesuré et ce qui manque encore

Mesuré publiquement : funding, tranche, échéance, prix, OI, volume, catalogues
PERP/margin, carnets Gate.io et Hyperliquid, VWAP, slippage d'entrée et proxy de
coût aller-retour instantané.

Encore nécessaire avant argent réel :

- frais exacts de chaque compte et éventuels rabais ;
- capacité et taux d'emprunt spot-margin en temps réel ;
- collecte persistante des carnets autour de chaque échéance ;
- accusés de fills, funding effectivement crédité et historique de compte ;
- seuil de réserve issu des distributions observées de base et de slippage ;
- règles précises d'éligibilité au funding confirmées plateforme par plateforme.

## 7. Sources principales

- Gate API v4, contrats Futures :
  https://www.gate.com/docs/developers/apiv4/en/futures/
- Hyperliquid, mécanisme de funding :
  https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding
- Hyperliquid, frais :
  https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
- Hyperliquid, API des contextes PERP :
  https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
- Bitget, taux courant et échéance :
  https://www.bitget.com/legacy-docs/uta/public/Get-Current-Funding-Rate
- He, Manela, Ross et von Wachter, *Fundamentals of Perpetual Futures* :
  https://arxiv.org/abs/2212.06888
- Ackerer, Hugonnier et Jermann, *Perpetual Futures Pricing* :
  https://arxiv.org/abs/2310.11771
- Dai, Li et Yang, *Arbitrage in Perpetual Contracts* :
  https://papers.ssrn.com/sol3/Delivery.cfm/5262988.pdf?abstractid=5262988

Les travaux théoriques établissent des bornes d'absence d'arbitrage dépendantes
des coûts de transaction. Ils ne prouvent pas qu'un funding ponctuellement élevé
est un profit exécutable ; précisément pour cette raison, le système doit mesurer
les coûts et la base autour du règlement en conditions réelles.
