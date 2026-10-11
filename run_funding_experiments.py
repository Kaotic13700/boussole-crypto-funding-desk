"""Continuously capture real funding-window evidence without placing orders."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import sys
import threading
import time

from funding_engine import collect_feed
from funding_experiments import (
    candidate_routes,
    connect_database,
    database_status,
    due_snapshots,
    experiments_needing_settlements,
    now_ms,
    record_collector_run,
    register_experiments,
    store_book_snapshot,
    store_settlements,
)
from paper_market_data import fetch_confirmed_settlements, fetch_order_books


DEFAULT_DATABASE = Path(__file__).resolve().parent / "data" / "funding_experiments.sqlite3"
DEFAULT_PID_FILE = Path(__file__).resolve().parent / "data" / "funding_experiments.pid"


def _process_exists(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except (OSError, ValueError):
        return False
    return True


def _acquire_pid_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            process_id = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            process_id = 0
        if process_id and _process_exists(process_id):
            raise RuntimeError(f"Collecteur déjà actif (PID {process_id})")
    path.write_text(str(os.getpid()), encoding="utf-8")


def _release_pid_file(path: Path) -> None:
    try:
        if path.exists() and path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


def _capture_due(connection, observed_at: int) -> int:
    due = due_snapshots(connection, observed_at)
    if not due:
        return 0
    captured = 0
    requests = []
    for experiment, offset in due:
        route = json.loads(experiment["route_json"])
        requests.append((experiment, offset, route))

    def fetch(item):
        experiment, offset, route = item
        books = fetch_order_books(
            {
                "funding": (
                    route["funding_exchange"], route["funding_symbol"],
                    route["funding_contract_value"],
                ),
                "hedge": (
                    route["hedge_exchange"], route["hedge_symbol"],
                    route["hedge_contract_value"],
                ),
            }
        )
        return experiment, offset, books

    with ThreadPoolExecutor(max_workers=min(12, len(requests))) as pool:
        pending = {pool.submit(fetch, item): item for item in requests}
        for future in as_completed(pending):
            experiment, offset, _route = pending[future]
            try:
                fetched_experiment, fetched_offset, books = future.result()
                book_time = max(int(book["timestamp"]) for book in books.values())
                captured += int(
                    store_book_snapshot(
                        connection,
                        fetched_experiment["experiment_id"],
                        fetched_experiment["deadline_ms"],
                        fetched_offset,
                        book_time,
                        books,
                    )
                )
            except Exception as error:
                print(
                    f"snapshot {experiment['experiment_id']} {offset:+d}s: {error}",
                    flush=True,
                )
    return captured


def _collect_settlements(connection, observed_at: int) -> int:
    stored = 0
    for experiment in experiments_needing_settlements(connection, observed_at):
        route = json.loads(experiment["route_json"])
        for leg in ("funding", "hedge"):
            deadline = int(route["next_funding_at"] if leg == "funding" else route["hedge_next_funding_at"])
            if not (experiment["created_at_ms"] < deadline <= observed_at):
                continue
            exchange = route[f"{leg}_exchange"]
            if exchange in {"Kraken", "Hyperliquid"}:
                continue
            already_stored = connection.execute(
                "SELECT 1 FROM settlements WHERE experiment_id = ? AND leg = ? LIMIT 1",
                (experiment["experiment_id"], leg),
            ).fetchone()
            if already_stored:
                continue
            try:
                events = fetch_confirmed_settlements(
                    exchange, route[f"{leg}_symbol"], deadline - 90_000, deadline + 90_000
                )
                stored += store_settlements(
                    connection, experiment["experiment_id"], leg, events
                )
            except Exception as error:
                print(f"settlement {experiment['experiment_id']} {leg}: {error}", flush=True)
    return stored


def run_cycle(database: str | Path, capture_due: bool = True) -> dict:
    observed_at = now_ms()
    with connect_database(database) as connection:
        try:
            feed = collect_feed()
            live_markets = [
                market for market in feed["markets"] if market.get("data_status") == "live"
            ]
            candidates = candidate_routes(live_markets, observed_at)
            register_experiments(connection, candidates, observed_at)
            captured = _capture_due(connection, now_ms()) if capture_due else 0
            settlements = _collect_settlements(connection, now_ms())
            record_collector_run(
                connection, observed_at, len(live_markets), len(candidates),
                captured, settlements,
            )
            status = database_status(connection)
            print(
                f"{observed_at}: {len(live_markets)} marchés live, "
                f"{len(candidates)} expériences actives, {captured} carnets, "
                f"{settlements} règlements",
                flush=True,
            )
            return status
        except Exception as error:
            record_collector_run(connection, observed_at, 0, 0, 0, 0, str(error))
            raise


def _scanner_loop(
    database: str | Path, stop_event: threading.Event, interval_seconds: int
) -> None:
    """Refresh the broad market universe without blocking precise book ticks."""
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            run_cycle(database, capture_due=False)
        except Exception as error:
            print(f"collector scan: {error}", file=sys.stderr, flush=True)
        stop_event.wait(max(2.0, float(interval_seconds) - (time.monotonic() - started)))


def _book_clock_loop(database: str | Path, stop_event: threading.Event) -> None:
    """Check due offsets every two seconds independently of the full scan."""
    while not stop_event.is_set():
        started = time.monotonic()
        try:
            with connect_database(database) as connection:
                captured = _capture_due(connection, now_ms())
                if captured:
                    print(f"clock: {captured} carnet(s) enregistré(s)", flush=True)
        except Exception as error:
            print(f"collector clock: {error}", file=sys.stderr, flush=True)
        stop_event.wait(max(0.25, 2.0 - (time.monotonic() - started)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=os.environ.get("EXPERIMENT_DATABASE", str(DEFAULT_DATABASE)))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=12)
    args = parser.parse_args()
    if args.once:
        run_cycle(args.database)
        return 0
    _acquire_pid_file(DEFAULT_PID_FILE)
    stop_event = threading.Event()
    try:
        scanner = threading.Thread(
            target=_scanner_loop, args=(args.database, stop_event, args.interval),
            name="funding-market-scan", daemon=True,
        )
        clock = threading.Thread(
            target=_book_clock_loop, args=(args.database, stop_event),
            name="funding-book-clock", daemon=True,
        )
        scanner.start()
        clock.start()
        while scanner.is_alive() and clock.is_alive():
            scanner.join(timeout=1)
        return 1
    except KeyboardInterrupt:
        stop_event.set()
        return 0
    finally:
        stop_event.set()
        _release_pid_file(DEFAULT_PID_FILE)


if __name__ == "__main__":
    raise SystemExit(main())
