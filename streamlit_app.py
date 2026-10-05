from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from funding_engine import collect_feed, market_state


ROOT = Path(__file__).resolve().parent
LOGO_PATH = ROOT / "boussole-crypto-logo.png"
VENUE_LABELS = {"Bitget": "Bitget", "Kraken": "Kraken EU*", "BloFin": "BloFin"}
STATE_RANK = {"PÉRIMÉ": -1, "FLUX": 0, "SURVEILLER": 1, "ALERTE": 2}

st.set_page_config(
    page_title="Funding Desk — Boussole Crypto",
    page_icon=str(LOGO_PATH),
    layout="wide",
    initial_sidebar_state="collapsed",
)


def _logo_data_uri() -> str:
    encoded = base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


LOGO_URI = _logo_data_uri()

st.markdown(
    f"""
    <style>
    :root {{
      --ink: #080a0a;
      --panel: rgba(17, 20, 19, .82);
      --panel-2: rgba(24, 27, 25, .72);
      --line: rgba(255,255,255,.085);
      --gold: #d8ad61;
      --gold-soft: #f0d59e;
      --emerald: #32d59a;
      --coral: #ff695e;
      --amber: #ffb45f;
      --muted: #8e9691;
    }}
    .stApp {{
      background:
        radial-gradient(circle at 82% 8%, rgba(216,173,97,.12), transparent 29rem),
        radial-gradient(circle at 7% 42%, rgba(23,139,104,.11), transparent 30rem),
        linear-gradient(145deg, #070909 0%, #0b0e0d 48%, #080a09 100%);
      color: #f4f4ef;
    }}
    .stApp::before {{
      content: "";
      position: fixed;
      inset: 9% -9% auto auto;
      width: min(58vw, 780px);
      aspect-ratio: 1;
      background: url('{LOGO_URI}') center/contain no-repeat;
      opacity: .035;
      filter: saturate(.7);
      pointer-events: none;
      z-index: 0;
    }}
    [data-testid="stHeader"] {{ background: transparent; }}
    [data-testid="stMainBlockContainer"] {{ max-width: 1720px; padding-top: 1.25rem; padding-bottom: 3rem; }}
    [data-testid="stAppViewContainer"] > .main {{ position: relative; z-index: 1; }}
    .bc-hero {{
      position: relative;
      overflow: hidden;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 2rem;
      min-height: 170px;
      padding: 1.25rem 1.5rem;
      border: 1px solid rgba(216,173,97,.18);
      border-radius: 22px;
      background: linear-gradient(112deg, rgba(20,23,21,.94), rgba(12,15,14,.84));
      box-shadow: 0 24px 80px rgba(0,0,0,.28), inset 0 1px rgba(255,255,255,.035);
    }}
    .bc-hero::after {{
      content: "";
      position: absolute;
      right: -5rem;
      top: -9rem;
      width: 28rem;
      height: 28rem;
      border: 1px solid rgba(216,173,97,.14);
      border-radius: 50%;
      box-shadow: 0 0 80px rgba(216,173,97,.08);
    }}
    .bc-brand {{ display:flex; align-items:center; gap:1.15rem; position:relative; z-index:2; }}
    .bc-logo {{ width:112px; height:112px; object-fit:contain; filter:drop-shadow(0 12px 24px rgba(0,0,0,.42)); }}
    .bc-eyebrow {{ color:var(--gold); font:600 .68rem/1.2 ui-monospace, monospace; letter-spacing:.2em; text-transform:uppercase; }}
    .bc-title {{ margin:.36rem 0 .35rem; font-size:clamp(1.75rem,3vw,3.2rem); line-height:1; letter-spacing:-.045em; font-weight:760; }}
    .bc-subtitle {{ margin:0; color:rgba(244,244,239,.55); font-size:.88rem; }}
    .bc-live {{ display:flex; align-items:center; gap:.6rem; padding:.62rem .82rem; border:1px solid rgba(50,213,154,.18); border-radius:999px; background:rgba(50,213,154,.055); color:#abf5d7; font:600 .7rem/1 ui-monospace, monospace; letter-spacing:.08em; position:relative; z-index:2; white-space:nowrap; }}
    .bc-live i {{ width:7px; height:7px; border-radius:50%; background:var(--emerald); box-shadow:0 0 14px var(--emerald); }}
    .bc-section-label {{ margin:1.4rem 0 .75rem; color:rgba(244,244,239,.38); font:600 .66rem/1.2 ui-monospace, monospace; letter-spacing:.16em; text-transform:uppercase; }}
    .bc-metric {{ min-height:116px; padding:1rem 1.05rem; border:1px solid var(--line); border-radius:15px; background:linear-gradient(145deg, rgba(24,27,25,.82), rgba(14,17,16,.78)); box-shadow:inset 0 1px rgba(255,255,255,.025); }}
    .bc-metric.alert {{ border-color:rgba(255,105,94,.23); background:linear-gradient(145deg,rgba(255,105,94,.08),rgba(14,17,16,.8)); }}
    .bc-metric.watch {{ border-color:rgba(255,180,95,.22); background:linear-gradient(145deg,rgba(255,180,95,.075),rgba(14,17,16,.8)); }}
    .bc-metric .label {{ color:rgba(244,244,239,.46); font-size:.72rem; }}
    .bc-metric .value {{ margin:.5rem 0 .2rem; font:700 1.62rem/1 ui-monospace, monospace; letter-spacing:-.04em; }}
    .bc-metric.alert .value {{ color:#ff9188; }} .bc-metric.watch .value {{ color:#ffc47f; }}
    .bc-metric .detail {{ color:rgba(244,244,239,.28); font-size:.66rem; }}
    .bc-sources {{ display:flex; flex-wrap:wrap; gap:.5rem; margin:.7rem 0 .1rem; }}
    .bc-source {{ display:inline-flex; align-items:center; gap:.4rem; padding:.42rem .58rem; border-radius:7px; border:1px solid var(--line); background:rgba(255,255,255,.025); font:500 .65rem/1 ui-monospace,monospace; color:rgba(244,244,239,.56); }}
    .bc-source.live {{ color:#93eac8; border-color:rgba(50,213,154,.16); }}
    .bc-source.stale {{ color:#ffc47f; border-color:rgba(255,180,95,.2); }}
    .bc-source.error {{ color:#ff9188; border-color:rgba(255,105,94,.2); }}
    .bc-dot {{ width:5px; height:5px; border-radius:50%; background:currentColor; }}
    .bc-notice {{ padding:.7rem .85rem; margin:.75rem 0; border:1px solid rgba(255,180,95,.18); border-radius:10px; background:rgba(255,180,95,.055); color:#e7c690; font-size:.76rem; }}
    .bc-detail {{ padding:1rem; border:1px solid var(--line); border-radius:14px; background:rgba(12,15,14,.72); }}
    .bc-detail strong {{ color:var(--gold-soft); }}
    div[data-testid="stDataFrame"] {{ border:1px solid var(--line); border-radius:14px; overflow:hidden; background:rgba(10,12,11,.74); }}
    div[data-testid="stSelectbox"], div[data-testid="stMultiSelect"] {{ border-radius:11px; }}
    div[data-baseweb="select"] > div {{ background:rgba(21,24,22,.94); border-color:var(--line); }}
    .stButton > button {{ border-radius:10px; border-color:rgba(216,173,97,.22); color:var(--gold-soft); background:rgba(216,173,97,.055); }}
    .stButton > button:hover {{ border-color:rgba(216,173,97,.5); color:white; }}
    .bc-footer {{ margin-top:1.6rem; padding-top:1rem; border-top:1px solid var(--line); color:rgba(244,244,239,.3); font-size:.69rem; line-height:1.65; }}
    @media (max-width: 780px) {{
      .bc-hero {{ padding:1rem; min-height:136px; }} .bc-logo {{ width:80px; height:80px; }}
      .bc-live {{ display:none; }} .bc-title {{ font-size:1.75rem; }} .bc-subtitle {{ font-size:.74rem; }}
    }}
    </style>
    <div class="bc-hero">
      <div class="bc-brand">
        <img class="bc-logo" src="{LOGO_URI}" alt="Boussole Crypto" />
        <div>
          <div class="bc-eyebrow">Boussole Crypto · Market Intelligence</div>
          <div class="bc-title">Funding Desk</div>
          <p class="bc-subtitle">Scanner PERP 1 h · 4 h · 8 h — Bitget, Kraken EU*, BloFin</p>
        </div>
      </div>
      <div class="bc-live"><i></i> FLUX OFFICIELS · QUASI TEMPS RÉEL</div>
    </div>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=2.4, max_entries=1, show_spinner=False)
def load_feed() -> dict[str, Any]:
    return collect_feed()


def age_label(age_ms: int | None) -> str:
    if age_ms is None:
        return "horodatage indisponible"
    if age_ms < 1_000:
        return "< 1 s"
    seconds = round(age_ms / 1_000)
    return f"{seconds} s"


def countdown(deadline_ms: int, current_ms: int) -> str:
    remaining = max(0, deadline_ms - current_ms) // 1_000
    hours, remainder = divmod(remaining, 3_600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def metric_card(label: str, value: str, detail: str, tone: str = "") -> None:
    st.markdown(
        f'<div class="bc-metric {tone}"><div class="label">{label}</div><div class="value">{value}</div><div class="detail">{detail}</div></div>',
        unsafe_allow_html=True,
    )


def coverage_by_asset(markets: list[dict[str, Any]]) -> dict[str, dict[str, list[str]]]:
    output: dict[str, dict[str, set[str]]] = {}
    for market in markets:
        coverage = output.setdefault(
            market["asset"],
            {"perp_long": set(), "perp_short": set(), "margin_long": set(), "margin_short": set()},
        )
        if market["data_status"] != "live":
            continue
        venue = VENUE_LABELS[market["exchange"]]
        coverage["perp_long"].add(venue)
        coverage["perp_short"].add(venue)
        if market["margin_long_available"]:
            coverage["margin_long"].add(venue)
        if market["margin_short_available"]:
            coverage["margin_short"].add(venue)
    return {
        asset: {key: sorted(values) for key, values in coverage.items()}
        for asset, coverage in output.items()
    }


def source_badges(sources: list[dict[str, Any]]) -> None:
    pieces: list[str] = []
    for source in sources:
        status = source["status"]
        age = age_label(source["age_ms"])
        market_age = (
            ""
            if source["market_data_age_ms"] is None
            else f" · marché {age_label(source['market_data_age_ms'])}"
        )
        pieces.append(
            f'<span class="bc-source {status}" title="{source.get("message") or ""}"><i class="bc-dot"></i>'
            f'{VENUE_LABELS[source["exchange"]]} · {status.upper()} · {source["markets"]} PERP · scan {age}{market_age}</span>'
        )
    st.markdown(f'<div class="bc-sources">{"".join(pieces)}</div>', unsafe_allow_html=True)


def notify_threshold_crossings(markets: list[dict[str, Any]], enabled: bool) -> None:
    current = {
        market["id"]: market_state(market["funding_pct"], market["data_status"])
        for market in markets
        if market["data_status"] == "live"
    }
    previous = st.session_state.get("previous_bands")
    st.session_state.previous_bands = current
    if not enabled:
        return
    if previous is None:
        active = sum(value == "ALERTE" for value in current.values())
        if active:
            st.toast(f"{active} alerte(s) funding active(s)", icon="🚨")
        return
    by_id = {market["id"]: market for market in markets}
    for market_id, state in current.items():
        old_state = previous.get(market_id, "FLUX")
        if STATE_RANK[state] > STATE_RANK.get(old_state, 0) and state in {"ALERTE", "SURVEILLER"}:
            market = by_id[market_id]
            st.toast(
                f"{state} · {market['asset']} · {VENUE_LABELS[market['exchange']]} · {market['funding_pct']:+.4f}%",
                icon="🚨" if state == "ALERTE" else "👁️",
            )


def table_rows(markets: list[dict[str, Any]], coverage: dict[str, dict[str, list[str]]], current_ms: int) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for market in markets:
        asset_coverage = coverage[market["asset"]]
        logo_asset = "bitcoin" if market["asset"] == "BTC" else market["asset"].lower()
        rows.append(
            {
                "Logo": f"https://assets.coincap.io/assets/icons/{logo_asset}@2x.png",
                "État": market_state(market["funding_pct"], market["data_status"]),
                "Actif": market["asset"],
                "Plateforme": VENUE_LABELS[market["exchange"]],
                "Symbole": market["symbol"],
                "Fenêtre": market["interval_hours"],
                "Funding": market["funding_pct"],
                "Prévision": market["predicted_funding_pct"],
                "Hypothèse x10": abs(market["funding_pct"]) * 10,
                "Échéance": datetime.fromtimestamp(market["next_funding_at"] / 1000, tz=timezone.utc),
                "Compte à rebours": countdown(market["next_funding_at"], current_ms),
                "PERP long": " · ".join(asset_coverage["perp_long"]) or "Non vérifié",
                "PERP short": " · ".join(asset_coverage["perp_short"]) or "Non vérifié",
                "Margin long": " · ".join(asset_coverage["margin_long"]) or "Non vérifié",
                "Margin short": " · ".join(asset_coverage["margin_short"]) or "Non vérifié",
                "Spread PERP": market["spread_bps"],
                "Statut donnée": "LIVE" if market["data_status"] == "live" else "PÉRIMÉE",
            }
        )
    return pd.DataFrame(rows)


def inspect_contract(market: dict[str, Any], coverage: dict[str, dict[str, list[str]]], current_ms: int) -> None:
    asset_coverage = coverage[market["asset"]]
    prediction = (
        "Non fournie"
        if market["predicted_funding_pct"] is None
        else f"{market['predicted_funding_pct']:+.6f}%"
    )
    market_age = (
        "Horodatage non fourni"
        if market["market_data_at"] is None
        else age_label(max(0, current_ms - market["market_data_at"]))
    )
    st.markdown(
        f"""
        <div class="bc-detail">
          <div class="bc-eyebrow">Contrat sélectionné · {market_state(market['funding_pct'], market['data_status'])}</div>
          <h3 style="margin:.6rem 0 .8rem">{market['asset']} <span style="color:#777;font-size:.72em">PERP</span></h3>
          <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.8rem;font-size:.76rem;color:#929b96">
            <div>Plateforme<br><strong>{VENUE_LABELS[market['exchange']]}</strong></div>
            <div>Fenêtre<br><strong>{market['interval_hours']} h</strong></div>
            <div>Funding courant<br><strong>{market['funding_pct']:+.6f}%</strong></div>
            <div>Funding prédit<br><strong>{prediction}</strong></div>
            <div>Hypothèse x10<br><strong>{abs(market['funding_pct']) * 10:.2f}%</strong></div>
            <div>Échéance<br><strong>{countdown(market['next_funding_at'], current_ms)}</strong></div>
            <div>Donnée marché<br><strong>{market_age}</strong></div>
            <div>Source échéance<br><strong>{'API exchange' if market['deadline_source'] == 'exchange' else 'Cadence officielle'}</strong></div>
            <div>Margin long<br><strong>{' · '.join(asset_coverage['margin_long']) or 'Non vérifié'}</strong></div>
            <div>Margin short<br><strong>{' · '.join(asset_coverage['margin_short']) or 'Non vérifié'}</strong></div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


@st.fragment(run_every="3s")
def live_dashboard() -> None:
    feed = load_feed()
    markets = feed["markets"]
    current_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    coverage = coverage_by_asset(markets)
    alert_count = sum(market_state(item["funding_pct"], item["data_status"]) == "ALERTE" for item in markets)
    watch_count = sum(market_state(item["funding_pct"], item["data_status"]) == "SURVEILLER" for item in markets)
    live_source_count = sum(source["status"] == "live" for source in feed["sources"])

    st.markdown('<div class="bc-section-label">Vue opérationnelle</div>', unsafe_allow_html=True)
    metric_columns = st.columns(4)
    with metric_columns[0]:
        metric_card("Alertes ±0,50%", str(alert_count), "Déclenchement immédiat", "alert")
    with metric_columns[1]:
        metric_card("À surveiller ±0,40%", str(watch_count), "Surveillance renforcée", "watch")
    with metric_columns[2]:
        metric_card("Contrats PERP", f"{len(markets):,}".replace(",", " "), "Fenêtres 1 h · 4 h · 8 h")
    with metric_columns[3]:
        metric_card("Flux officiels", f"{live_source_count}/3", f"Scan serveur {feed['latency_ms']} ms")

    source_badges(feed["sources"])
    if feed["mode"] == "hybrid":
        st.markdown('<div class="bc-notice">Une source est dégradée. Son dernier snapshot valide reste visible en PÉRIMÉ et ne déclenche aucune alerte.</div>', unsafe_allow_html=True)
    elif feed["mode"] == "offline":
        st.markdown('<div class="bc-notice">Les sources sont indisponibles. Les éventuelles données conservées sont périmées et non alertantes.</div>', unsafe_allow_html=True)

    st.markdown('<div class="bc-section-label">Filtres et alertes</div>', unsafe_allow_html=True)
    first_row = st.columns([1.2, 1.1, 1.1, 1.2, 1.2])
    with first_row[0]:
        selected_state = st.selectbox("État", ["Tous", "ALERTE", "SURVEILLER", "FLUX", "PÉRIMÉ"], key="state_filter")
    with first_row[1]:
        selected_direction = st.selectbox("Funding", ["Tous", "Positif", "Négatif"], key="direction_filter")
    with first_row[2]:
        selected_venue = st.selectbox("Plateforme", ["Toutes", "Bitget", "Kraken EU*", "BloFin"], key="venue_filter")
    with first_row[3]:
        selected_intervals = st.multiselect("Fenêtres", [1, 4, 8], default=[1, 4, 8], format_func=lambda value: f"{value} h", key="interval_filter")
    with first_row[4]:
        selected_coverage = st.selectbox(
            "Couverture",
            ["Toutes", "PERP 2+ plateformes", "Margin long ou short", "Margin long", "Margin short", "Sans margin"],
            key="coverage_filter",
        )

    second_row = st.columns([1.25, 1, 1, 1.4])
    with second_row[0]:
        sort_key = st.selectbox("Trier prioritairement", ["État", "Funding absolu", "Funding signé", "Hypothèse x10", "Échéance"], key="sort_key")
    with second_row[1]:
        sort_direction = st.selectbox("Ordre", ["Décroissant", "Croissant"], key="sort_direction")
    with second_row[2]:
        alerts_enabled = st.toggle("Alertes dans l’application", value=True, key="alerts_enabled")
    with second_row[3]:
        st.caption("Actualisation automatique toutes les 3 secondes · en-têtes du tableau également triables")

    notify_threshold_crossings(markets, alerts_enabled)

    filtered: list[dict[str, Any]] = []
    for market in markets:
        state = market_state(market["funding_pct"], market["data_status"])
        venue = VENUE_LABELS[market["exchange"]]
        asset_coverage = coverage[market["asset"]]
        if selected_state != "Tous" and state != selected_state:
            continue
        if selected_direction == "Positif" and market["funding_pct"] < 0:
            continue
        if selected_direction == "Négatif" and market["funding_pct"] >= 0:
            continue
        if selected_venue != "Toutes" and venue != selected_venue:
            continue
        if market["interval_hours"] not in selected_intervals:
            continue
        has_margin_long = bool(asset_coverage["margin_long"])
        has_margin_short = bool(asset_coverage["margin_short"])
        if selected_coverage == "PERP 2+ plateformes" and len(asset_coverage["perp_long"]) < 2:
            continue
        if selected_coverage == "Margin long ou short" and not (has_margin_long or has_margin_short):
            continue
        if selected_coverage == "Margin long" and not has_margin_long:
            continue
        if selected_coverage == "Margin short" and not has_margin_short:
            continue
        if selected_coverage == "Sans margin" and (has_margin_long or has_margin_short):
            continue
        filtered.append(market)

    descending = sort_direction == "Décroissant"
    if sort_key == "État":
        filtered.sort(key=lambda item: (STATE_RANK[market_state(item["funding_pct"], item["data_status"])], abs(item["funding_pct"])), reverse=descending)
    elif sort_key in {"Funding absolu", "Hypothèse x10"}:
        filtered.sort(key=lambda item: abs(item["funding_pct"]), reverse=descending)
    elif sort_key == "Funding signé":
        filtered.sort(key=lambda item: item["funding_pct"], reverse=descending)
    else:
        filtered.sort(key=lambda item: item["next_funding_at"], reverse=descending)

    st.markdown(f'<div class="bc-section-label">Marchés affichés · {len(filtered)}</div>', unsafe_allow_html=True)
    frame = table_rows(filtered, coverage, current_ms)
    if frame.empty:
        st.info("Aucun PERP 1 h, 4 h ou 8 h ne correspond aux filtres sélectionnés.")
    else:
        st.dataframe(
            frame,
            width="stretch",
            height=680,
            hide_index=True,
            column_config={
                "Logo": st.column_config.ImageColumn("", width="small"),
                "Fenêtre": st.column_config.NumberColumn("Fenêtre", format="%d h"),
                "Funding": st.column_config.NumberColumn("Funding courant", format="%+.6f%%"),
                "Prévision": st.column_config.NumberColumn("Prévision", format="%+.6f%%"),
                "Hypothèse x10": st.column_config.NumberColumn("Hypothèse x10", format="%.2f%%"),
                "Échéance": st.column_config.DatetimeColumn("Échéance UTC", format="HH:mm:ss"),
                "Spread PERP": st.column_config.NumberColumn("Spread PERP", format="%.2f bps"),
            },
        )

        contract_ids = [market["id"] for market in filtered]
        by_id = {market["id"]: market for market in filtered}
        selected_id = st.selectbox(
            "Inspecter un contrat",
            contract_ids,
            format_func=lambda identifier: (
                f"{by_id[identifier]['asset']} · {VENUE_LABELS[by_id[identifier]['exchange']]} · "
                f"{by_id[identifier]['interval_hours']} h · {by_id[identifier]['funding_pct']:+.6f}%"
            ),
            key="selected_contract",
        )
        inspect_contract(by_id[selected_id], coverage, current_ms)

    st.markdown(
        '<div class="bc-footer"><strong>Lecture des signaux :</strong> funding positif → le short PERP reçoit normalement le funding ; funding négatif → le long PERP le reçoit. '
        'L’hypothèse x10 est brute et informative, jamais une condition d’entrée. Les statuts margin proviennent du catalogue public ; la quantité réellement empruntable doit être confirmée par un compte authentifié avant exécution. '
        '* Kraken EU : catalogue public Futures, éligibilité réglementaire à confirmer selon le compte. Streamlit Community Cloud peut mettre une application inactive en veille.</div>',
        unsafe_allow_html=True,
    )


live_dashboard()
