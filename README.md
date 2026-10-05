# Boussole Crypto — Funding Desk

Application Streamlit de surveillance des fundings perpétuels Bitget, Kraken et BloFin.

## Fonctionnement

- Feed public officiel des PERP 1 h, 4 h et 8 h.
- Alerte à partir de `±0,50 %` et surveillance renforcée à partir de `±0,40 %`.
- Funding courant Kraken séparé de sa prévision.
- Formules Kraken distinctes pour les contrats linéaires `PF_` et inverses `PI_`.
- Échéances Bitget/BloFin fournies par l’exchange ; cadence Kraken dérivée de sa spécification horaire.
- Couverture présentée séparément en PERP long, PERP short, margin long et margin short.
- Conservation temporaire du dernier snapshot valide en cas de panne, marqué `PÉRIMÉ` et exclu des alertes.
- Aucun spot simple, aucune donnée de démonstration et aucun ordre réel.

## Lancer localement

```powershell
uv run --with-requirements requirements.txt streamlit run streamlit_app.py
```

Puis ouvrir `http://localhost:8501`.

## Déploiement Streamlit Community Cloud

Le point d’entrée est `streamlit_app.py`. Les dépendances sont épinglées dans `requirements.txt` et le thème se trouve dans `.streamlit/config.toml`.

## Limites importantes

- Les lectures REST sont quasi temps réel, pas tick-par-tick.
- Les métadonnées publiques margin ne garantissent pas la quantité réellement empruntable au moment d’un ordre.
- L’éligibilité réglementaire Kraken EU doit être confirmée par le compte connecté.
- Streamlit Community Cloud peut mettre une application inactive en veille.
- Cette version n’accepte aucune clé API et n’exécute aucun ordre.

