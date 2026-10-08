from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import html
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import streamlit as st

from funding_engine import collect_feed, market_state
from paper_engine import build_perp_routes, mark_paper_position


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
      --cyan: #5ed9ff;
      --violet: #9c8cff;
      --muted: #8e9691;
    }}
    .stApp {{
      background:
        radial-gradient(circle at 82% 8%, rgba(216,173,97,.18), transparent 29rem),
        radial-gradient(circle at 7% 42%, rgba(23,139,104,.17), transparent 30rem),
        radial-gradient(circle at 55% 82%, rgba(94,217,255,.055), transparent 26rem),
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
      border: 1px solid rgba(216,173,97,.30);
      border-radius: 22px;
      background: linear-gradient(112deg, rgba(20,23,21,.94), rgba(12,15,14,.84));
      box-shadow: 0 30px 100px rgba(0,0,0,.38), 0 0 55px rgba(216,173,97,.055), inset 0 1px rgba(255,255,255,.055);
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
      box-shadow: 0 0 95px rgba(216,173,97,.14);
      animation: bc-orbit 12s ease-in-out infinite alternate;
    }}
    @keyframes bc-orbit {{ from {{ transform:scale(.96) rotate(-3deg); opacity:.72; }} to {{ transform:scale(1.05) rotate(4deg); opacity:1; }} }}
    .bc-brand {{ display:flex; align-items:center; gap:1.15rem; position:relative; z-index:2; }}
    .bc-logo {{ width:112px; height:112px; object-fit:contain; filter:drop-shadow(0 12px 24px rgba(0,0,0,.42)); }}
    .bc-eyebrow {{ color:var(--gold); font:600 .68rem/1.2 ui-monospace, monospace; letter-spacing:.2em; text-transform:uppercase; }}
    .bc-title {{ margin:.36rem 0 .35rem; font-size:clamp(1.75rem,3vw,3.2rem); line-height:1; letter-spacing:-.045em; font-weight:760; }}
    .bc-subtitle {{ margin:0; color:rgba(244,244,239,.55); font-size:.88rem; }}
    .bc-live {{ display:flex; align-items:center; gap:.6rem; padding:.62rem .82rem; border:1px solid rgba(50,213,154,.18); border-radius:999px; background:rgba(50,213,154,.055); color:#abf5d7; font:600 .7rem/1 ui-monospace, monospace; letter-spacing:.08em; position:relative; z-index:2; white-space:nowrap; }}
    .bc-live i {{ width:7px; height:7px; border-radius:50%; background:var(--emerald); box-shadow:0 0 14px var(--emerald); }}
    .bc-section-label {{ margin:1.4rem 0 .75rem; color:rgba(244,244,239,.38); font:600 .66rem/1.2 ui-monospace, monospace; letter-spacing:.16em; text-transform:uppercase; }}
    .bc-metric {{ min-height:116px; padding:1rem 1.05rem; border:1px solid var(--line); border-radius:15px; background:linear-gradient(145deg, rgba(24,27,25,.90), rgba(14,17,16,.82)); box-shadow:0 12px 34px rgba(0,0,0,.18), inset 0 1px rgba(255,255,255,.035); transition:transform .18s ease,border-color .18s ease; }}
    .bc-metric:hover {{ transform:translateY(-2px); border-color:rgba(216,173,97,.28); }}
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
    .bc-coverage-grid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:.6rem; margin:.9rem 0; }}
    .bc-coverage-card {{ min-height:82px; padding:.75rem; border:1px solid var(--line); border-radius:12px; background:linear-gradient(145deg,rgba(255,255,255,.038),rgba(255,255,255,.014)); }}
    .bc-coverage-card .k {{ color:rgba(244,244,239,.42); font-size:.65rem; text-transform:uppercase; letter-spacing:.09em; }}
    .bc-coverage-card .v {{ color:#aaf2d5; font-weight:700; font-size:.75rem; margin-top:.45rem; line-height:1.45; }}
    .bc-coverage-card.off .v {{ color:#8b938f; }}
    .bc-proof {{ margin-top:.7rem; padding:.65rem .75rem; border-left:2px solid rgba(216,173,97,.5); background:rgba(216,173,97,.045); color:#a9afa9; font-size:.69rem; line-height:1.55; }}
    .bc-paper {{ padding:1rem; border:1px solid rgba(94,217,255,.16); border-radius:16px; background:linear-gradient(135deg,rgba(94,217,255,.055),rgba(17,20,19,.88)); }}
    div[data-testid="stDataFrame"] {{ border:1px solid rgba(216,173,97,.18); border-radius:16px; overflow:hidden; background:rgba(10,12,11,.82); box-shadow:0 20px 55px rgba(0,0,0,.20); }}
    div[data-testid="stDataFrame"] [role="columnheader"] {{ background:linear-gradient(180deg,rgba(216,173,97,.13),rgba(216,173,97,.045)); color:#f3d99e; font-weight:750; }}
    button[data-baseweb="tab"] {{ letter-spacing:.04em; font-weight:700; }}
    div[data-baseweb="tab-list"] {{ gap:.45rem; border-bottom:1px solid rgba(216,173,97,.12); }}
    div[data-testid="stSelectbox"], div[data-testid="stMultiSelect"] {{ border-radius:11px; }}
    div[data-baseweb="select"] > div {{ background:rgba(21,24,22,.94); border-color:var(--line); }}
    .stButton > button {{ border-radius:10px; border-color:rgba(216,173,97,.22); color:var(--gold-soft); background:rgba(216,173,97,.055); }}
    .stButton > button:hover {{ border-color:rgba(216,173,97,.5); color:white; }}
    .bc-footer {{ margin-top:1.6rem; padding-top:1rem; border-top:1px solid var(--line); color:rgba(244,244,239,.3); font-size:.69rem; line-height:1.65; }}
    @media (max-width: 780px) {{
      .bc-hero {{ padding:1rem; min-height:136px; }} .bc-logo {{ width:80px; height:80px; }}
      .bc-live {{ display:none; }} .bc-title {{ font-size:1.75rem; }} .bc-subtitle {{ font-size:.74rem; }}
      .bc-coverage-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
      [data-testid="stMainBlockContainer"] {{ padding-left:.7rem; padding-right:.7rem; }}
      div[data-testid="stDataFrame"] {{ border-radius:11px; }}
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


@st.cache_data(ttl=3_600, max_entries=1, show_spinner=False)
def load_logo_index() -> dict[str, str]:
    """Resolve unambiguous CoinGecko logos; uncertain symbols use a local monogram."""
    endpoint = "https://api.coingecko.com/api/v3/coins/markets"

    def fetch_page(page: int) -> list[dict[str, Any]]:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            response = client.get(
                endpoint,
                params={"vs_currency": "usd", "per_page": 250, "page": page, "sparkline": "false"},
                headers={"Accept": "application/json", "User-Agent": "BoussoleFundingDesk/2.0"},
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, list) else []

    records: list[dict[str, Any]] = []
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(fetch_page, page) for page in range(1, 7)]
            for future in as_completed(futures):
                records.extend(future.result())
    except Exception:
        return {}

    candidates: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        symbol = str(record.get("symbol", "")).upper()
        image = str(record.get("image", ""))
        if symbol and image.startswith("https://"):
            candidates.setdefault(symbol, []).append(record)

    output: dict[str, str] = {}
    for symbol, items in candidates.items():
        if len(items) == 1:
            output[symbol] = str(items[0]["image"])
            continue
        exact = [
            item
            for item in items
            if "".join(character for character in str(item.get("id", "")).upper() if character.isalnum())
            == symbol
        ]
        if len(exact) == 1:
            output[symbol] = str(exact[0]["image"])
    return output


def monogram_logo(asset: str) -> str:
    digest = hashlib.sha256(asset.encode("utf-8")).hexdigest()
    palette = ["#d8ad61", "#32d59a", "#5ed9ff", "#9c8cff", "#ff7c73", "#ffb45f"]
    start = palette[int(digest[:2], 16) % len(palette)]
    end = palette[int(digest[2:4], 16) % len(palette)]
    initials = html.escape(asset[:4])
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="72" height="72" viewBox="0 0 72 72">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        f'<stop stop-color="{start}"/><stop offset="1" stop-color="{end}"/></linearGradient></defs>'
        '<circle cx="36" cy="36" r="33" fill="#101513" stroke="url(#g)" stroke-width="3"/>'
        f'<text x="36" y="41" text-anchor="middle" font-family="Arial,sans-serif" font-size="17" font-weight="800" fill="{start}">{initials}</text>'
        '</svg>'
    )
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def asset_logo(asset: str, logo_index: dict[str, str]) -> str:
    return logo_index.get(asset.upper()) or monogram_logo(asset.upper())


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
        if market.get("perp_long_available"):
            coverage["perp_long"].add(venue)
        if market.get("perp_short_available"):
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


def availability_text(platforms: list[str]) -> str:
    return "✓ " + " · ".join(platforms) if platforms else "— Indisponible"

def deadline_source_label(market: dict[str, Any]) -> str:
    if market.get("deadline_source") == "exchange":
        return f"API {VENUE_LABELS[market['exchange']]} · timestamp publié"
    if market.get("deadline_source") == "kraken-server-clock+official-hourly-boundary":
        return "Kraken · horloge serveur + borne horaire officielle"
    return "Source indisponible"



def table_rows(
    markets: list[dict[str, Any]],
    coverage: dict[str, dict[str, list[str]]],
    current_ms: int,
    logo_index: dict[str, str],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for market in markets:
        asset_coverage = coverage[market["asset"]]
        rows.append(
            {
                "Logo": asset_logo(market["asset"], logo_index),
                "État": market_state(market["funding_pct"], market["data_status"]),
                "Actif": market["asset"],
                "Funding": market["funding_pct"],
                "Tranche": market["interval_hours"],
                "Échéance": datetime.fromtimestamp(market["next_funding_at"] / 1000, tz=timezone.utc),
                "Compte à rebours": countdown(market["next_funding_at"], current_ms),
                "Source échéance": deadline_source_label(market),
                "Signal": "SHORT PERP reçoit" if market["funding_pct"] > 0 else "LONG PERP reçoit",
                "PERP long": availability_text(asset_coverage["perp_long"]),
                "PERP short": availability_text(asset_coverage["perp_short"]),
                "Margin long": availability_text(asset_coverage["margin_long"]),
                "Margin short": availability_text(asset_coverage["margin_short"]),
                "Plateforme": VENUE_LABELS[market["exchange"]],
                "Symbole": market["symbol"],
                "Prévision": market["predicted_funding_pct"],
                "Hypothèse x10": abs(market["funding_pct"]) * 10,
                "Spread PERP": market["spread_bps"],
                "Statut donnée": "LIVE" if market["data_status"] == "live" else "PÉRIMÉE",
            }
        )
    return pd.DataFrame(rows)


def styled_market_frame(frame: pd.DataFrame) -> Any:
    def state_style(value: Any) -> str:
        return {
            "ALERTE": "color:#ff8b82;background-color:rgba(255,105,94,.13);font-weight:800",
            "SURVEILLER": "color:#ffc47f;background-color:rgba(255,180,95,.11);font-weight:800",
            "FLUX": "color:#8fe8c5;background-color:rgba(50,213,154,.07);font-weight:700",
            "PÉRIMÉ": "color:#949b97;background-color:rgba(148,155,151,.08);font-weight:700",
        }.get(str(value), "")

    def funding_style(value: Any) -> str:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return ""
        color = "#67e2b0" if number >= 0 else "#ff867d"
        background = "rgba(50,213,154,.08)" if number >= 0 else "rgba(255,105,94,.08)"
        return f"color:{color};background-color:{background};font-weight:780"

    def coverage_style(value: Any) -> str:
        return (
            "color:#9be9c9;background-color:rgba(50,213,154,.055)"
            if str(value).startswith("✓")
            else "color:#7d8580;background-color:rgba(125,133,128,.045)"
        )

    styler = frame.style.map(state_style, subset=["État"]).map(funding_style, subset=["Funding"])
    for column in ("PERP long", "PERP short", "Margin long", "Margin short"):
        styler = styler.map(coverage_style, subset=[column])
    return styler


def inspect_contract(market: dict[str, Any], coverage: dict[str, dict[str, list[str]]], current_ms: int) -> None:
    asset_coverage = coverage[market["asset"]]
    prediction = (
        "— Non publiée par l’API officielle"
        if market["predicted_funding_pct"] is None
        else f"{market['predicted_funding_pct']:+.6f}%"
    )
    market_age = (
        "Horodatage non fourni"
        if market["market_data_at"] is None
        else age_label(max(0, current_ms - market["market_data_at"]))
    )
    def coverage_card(label: str, venues: list[str]) -> str:
        value = " · ".join(venues) if venues else "Indisponible"
        tone = "" if venues else " off"
        return f'<div class="bc-coverage-card{tone}"><div class="k">{label}</div><div class="v">{html.escape(value)}</div></div>'

    proof = " · ".join(
        [
            str(market.get("perp_availability_source", "")),
            str(market.get("margin_long_source", "")),
            str(market.get("margin_short_source", "")),
        ]
    )
    st.markdown(
        f"""
        <div class="bc-detail">
          <div class="bc-eyebrow">Contrat sélectionné · {market_state(market['funding_pct'], market['data_status'])}</div>
          <h3 style="margin:.6rem 0 .8rem">{market['asset']} <span style="color:#777;font-size:.72em">PERP</span></h3>
          <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.8rem;font-size:.76rem;color:#929b96">
            <div>Plateforme<br><strong>{VENUE_LABELS[market['exchange']]}</strong></div>
            <div>Tranche funding<br><strong>{market['interval_hours']} h</strong></div>
            <div>Funding courant<br><strong>{market['funding_pct']:+.6f}%</strong></div>
            <div>Funding prédit<br><strong>{prediction}</strong></div>
            <div>Hypothèse x10<br><strong>{abs(market['funding_pct']) * 10:.2f}%</strong></div>
            <div>Échéance UTC<br><strong>{datetime.fromtimestamp(market['next_funding_at'] / 1000, tz=timezone.utc).strftime('%d/%m/%Y %H:%M:%S')}</strong></div>
            <div>Compte à rebours<br><strong>{countdown(market['next_funding_at'], current_ms)}</strong></div>
            <div>Donnée marché<br><strong>{market_age}</strong></div>
            <div>Source tranche<br><strong>{html.escape(str(market.get('interval_source', 'Source indisponible')))}</strong></div>
            <div>Source échéance<br><strong>{html.escape(deadline_source_label(market))}</strong></div>
          </div>
          <div class="bc-coverage-grid">
            {coverage_card('PERP long', asset_coverage['perp_long'])}
            {coverage_card('PERP short', asset_coverage['perp_short'])}
            {coverage_card('Margin long', asset_coverage['margin_long'])}
            {coverage_card('Margin short', asset_coverage['margin_short'])}
          </div>
          <div class="bc-proof"><strong>Preuve catalogue :</strong> {html.escape(proof)}</div>
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
    logo_index = load_logo_index()
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
    frame = table_rows(filtered, coverage, current_ms, logo_index)
    if frame.empty:
        st.info("Aucun PERP 1 h, 4 h ou 8 h ne correspond aux filtres sélectionnés.")
    else:
        st.dataframe(
            styled_market_frame(frame),
            width="stretch",
            height=680,
            hide_index=True,
            column_config={
                "Logo": st.column_config.ImageColumn("", width="small"),
                "État": st.column_config.TextColumn("État", width="small"),
                "Actif": st.column_config.TextColumn("Actif", width="small"),
                "Funding": st.column_config.NumberColumn("Funding courant", format="%+.6f%%"),
                "Tranche": st.column_config.NumberColumn("Tranche funding", format="%d h"),
                "Échéance": st.column_config.DatetimeColumn("Échéance UTC", format="DD/MM/YYYY HH:mm:ss"),
                "Compte à rebours": st.column_config.TextColumn("Compte à rebours", width="medium"),
                "Source échéance": st.column_config.TextColumn("Source échéance", width="large"),
                "Signal": st.column_config.TextColumn("Sens funding", width="medium"),
                "PERP long": st.column_config.TextColumn("PERP long · plateformes", width="large"),
                "PERP short": st.column_config.TextColumn("PERP short · plateformes", width="large"),
                "Margin long": st.column_config.TextColumn("Margin long · plateformes", width="large"),
                "Margin short": st.column_config.TextColumn("Margin short · plateformes", width="large"),
                "Prévision": st.column_config.NumberColumn("Prévision", format="%+.6f%%"),
                "Hypothèse x10": st.column_config.NumberColumn("Hypothèse x10", format="%.2f%%"),
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
        'L’hypothèse x10 est brute et informative, jamais une condition d’entrée. Sur Kraken, le funding est continu : la borne affichée est la prochaine réalisation horaire, calculée depuis l’horloge serveur et la cadence officielle. '
        'Les disponibilités PERP et margin sont issues des catalogues officiels ; elles ne signifient pas qu’une quantité d’emprunt précise est garantie au moment d’un ordre. '
        '* Kraken EU : catalogue public Futures, éligibilité réglementaire à confirmer selon le compte. Streamlit Community Cloud peut mettre une application inactive en veille.</div>',
        unsafe_allow_html=True,
    )

def route_label(route: dict[str, Any]) -> str:
    funding_venue = VENUE_LABELS[route["funding_exchange"]]
    hedge_venue = VENUE_LABELS[route["hedge_exchange"]]
    return (
        f"{route['asset']} · {route['funding_pct']:+.4f}%/{route['interval_hours']} h · "
        f"{route['funding_side'].upper()} {funding_venue} ↔ {route['hedge_side'].upper()} {hedge_venue}"
    )


@st.fragment(run_every="3s")
def demo_dashboard() -> None:
    feed = load_feed()
    markets = feed["markets"]
    markets_by_id = {market["id"]: market for market in markets}
    routes = build_perp_routes(markets, threshold_pct=0.4)
    positions = st.session_state.setdefault("paper_positions", [])

    st.markdown('<div class="bc-section-label">Portefeuille de démonstration · marché réel</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="bc-paper"><strong>Mode paper live</strong><br>'
        '<span style="color:#9da7a1;font-size:.78rem">Les deux jambes utilisent exclusivement les prix PERP live du scanner. '
        'Aucun ordre, aucune clé API et aucun prix fictif. Le funding affiché reste une projection jusqu’au règlement effectif.</span></div>',
        unsafe_allow_html=True,
    )

    status_cols = st.columns(4)
    with status_cols[0]:
        metric_card("Routes live ≥ ±0,40%", str(len(routes)), "PERP / PERP vérifiées", "watch")
    with status_cols[1]:
        metric_card("Positions démo", str(len(positions)), "État de session local")
    with status_cols[2]:
        metric_card("Exécution réelle", "OFF", "Verrouillée par conception")
    with status_cols[3]:
        metric_card("Mobile", "READY", "Interface responsive · alertes in-app")

    st.markdown('<div class="bc-section-label">Prise de décision</div>', unsafe_allow_html=True)
    if not routes:
        st.info("Aucune route PERP/PERP live à deux jambes ne franchit actuellement ±0,40%.")
    else:
        route_by_id = {route["id"]: route for route in routes}
        selected_route_id = st.selectbox(
            "Route live",
            list(route_by_id),
            format_func=lambda identifier: route_label(route_by_id[identifier]),
            key="paper_route",
        )
        route = route_by_id[selected_route_id]
        controls = st.columns([1, 1, 1.25])
        with controls[0]:
            total_notional = st.number_input(
                "Notionnel global ($)", min_value=200.0, max_value=100_000.0, value=4_000.0, step=200.0
            )
        with controls[1]:
            leverage = st.select_slider("Levier indicatif", options=list(range(3, 11)), value=6)
        with controls[2]:
            st.caption("Le levier ne déclenche jamais l’entrée ; le seuil reste uniquement le funding ±0,40/0,50%.")
        if st.button("Ouvrir les deux jambes en démo live", type="primary", use_container_width=True):
            positions.append(
                {
                    **route,
                    "leg_notional": float(total_notional) / 2,
                    "total_notional": float(total_notional),
                    "leverage": int(leverage),
                    "opened_at": int(datetime.now(tz=timezone.utc).timestamp() * 1000),
                }
            )
            st.toast(f"Position démo ouverte · {route['asset']} · 2 × ${total_notional / 2:,.0f}", icon="🧭")

    if positions:
        st.markdown('<div class="bc-section-label">Positions suivies sur le flux live</div>', unsafe_allow_html=True)
        marked_rows: list[dict[str, Any]] = []
        for index, position in enumerate(positions, start=1):
            marked = mark_paper_position(position, markets_by_id)
            if marked is None:
                status = "FLUX INDISPONIBLE"
                market_pnl = projected = None
            else:
                status = "LIVE"
                market_pnl = marked["market_pnl"]
                projected = marked["projected_next_funding"]
            marked_rows.append(
                {
                    "#": index,
                    "Actif": position["asset"],
                    "Jambe funding": f"{position['funding_side'].upper()} · {VENUE_LABELS[position['funding_exchange']]}",
                    "Jambe couverture": f"{position['hedge_side'].upper()} · {VENUE_LABELS[position['hedge_exchange']]}",
                    "Notionnel": position["total_notional"],
                    "Levier": position["leverage"],
                    "PnL marché": market_pnl,
                    "Funding prochain cycle": projected,
                    "Statut": status,
                }
            )
        st.dataframe(
            pd.DataFrame(marked_rows),
            width="stretch",
            hide_index=True,
            column_config={
                "Notionnel": st.column_config.NumberColumn("Notionnel global", format="$%.2f"),
                "Levier": st.column_config.NumberColumn("Levier", format="x%d"),
                "PnL marché": st.column_config.NumberColumn("PnL marché live", format="$%+.2f"),
                "Funding prochain cycle": st.column_config.NumberColumn("Funding projeté", format="$%.2f"),
            },
        )
        if st.button("Clôturer toutes les positions démo", use_container_width=True):
            st.session_state.paper_positions = []
            st.rerun(scope="fragment")

    st.markdown('<div class="bc-section-label">Connexion portefeuille</div>', unsafe_allow_html=True)
    connection_cols = st.columns([1, 2])
    with connection_cols[0]:
        st.button("Connexion réelle verrouillée", disabled=True, use_container_width=True)
    with connection_cols[1]:
        st.caption(
            "L’interface est prête à recevoir des connecteurs d’exchange. L’activation réelle exigera une validation séparée, "
            "des clés sans droit de retrait, un chiffrement des secrets et un kill switch serveur."
        )


scanner_tab, paper_tab = st.tabs(["◈ Scanner live", "◎ Portefeuille démo live"])
with scanner_tab:
    live_dashboard()
with paper_tab:
    demo_dashboard()
