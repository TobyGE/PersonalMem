"""PersonalMem CLI.

Subcommands:
    init                                  # write default config
    onboard                                # (re)pick LLM provider
    start                                  # start the capture daemon (background)
    stop                                   # stop the capture daemon
    status                                 # show daemon + capture status
    run                                    # replay captures into topic threads
        --since YYYY-MM-DDTHH:MM           # window start
        --until YYYY-MM-DDTHH:MM           # window end
        --limit N                          # cap number of captures
        --reset                            # wipe threads_db before run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from . import config as oc_config
from . import paths
from .capture import ax_pruner
from .pipeline import router as thread_router
from .pipeline import summarizer as thread_summarizer
from .pipeline.router import CaptureView
from .store import threads as thread_store


# ─── data fetch + coalesce ─────────────────────────────────────────────────


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s)


def open_input_db(path: Path) -> sqlite3.Connection:
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def open_replay_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    thread_store.ensure_schema(conn)
    return conn


def fetch_captures(
    conn: sqlite3.Connection,
    *,
    since: str | None,
    until: str | None,
    limit: int | None,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    args: list = []
    if since:
        clauses.append("timestamp >= ?")
        args.append(since)
    if until:
        clauses.append("timestamp <= ?")
        args.append(until)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        "SELECT id, timestamp, app_name, bundle_id, window_title, "
        "       focused_role, focused_value, visible_text, url "
        f"  FROM captures {where} "
        " ORDER BY timestamp ASC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return conn.execute(sql, args).fetchall()


_TITLE_PREFIX_NOISE = re.compile(r"^[\W_]+", flags=re.UNICODE)


def _window_key(row: sqlite3.Row, sub_context: str = "") -> tuple[str, str, str]:
    title = (row["window_title"] or "").strip()
    title = _TITLE_PREFIX_NOISE.sub("", title)
    return (row["app_name"] or "", title, sub_context)


def coalesce_runs(
    rows: list[sqlite3.Row],
    *,
    max_gap_seconds: int = 60,
    sub_context_for: dict[str, str] | None = None,
) -> tuple[list[sqlite3.Row], list[list[str]]]:
    """Sliding-window dedup keyed on (app, normalized_title, sub_context).

    For each key, the latest capture seen within ``max_gap_seconds`` of the
    previous one for that same key replaces the older entry in the kept
    list. Captures of different keys are kept independently in chronological
    order. Spinner-frame bursts and rapid-flicker app oscillations collapse
    to one rep per "phase"; genuine re-visits after a gap remain distinct.
    """
    if not rows:
        return [], []
    sub_context_for = sub_context_for or {}

    kept: list[sqlite3.Row] = []
    folded_ids: list[list[str]] = []
    pos_for_key: dict[tuple[str, str, str], int] = {}
    ts_for_key: dict[tuple[str, str, str], datetime] = {}
    tombstoned: set[int] = set()

    for row in rows:
        row_dt = parse_iso(row["timestamp"])
        key = _window_key(row, sub_context_for.get(row["id"], ""))
        prev_ts = ts_for_key.get(key)
        if prev_ts is not None and (row_dt - prev_ts).total_seconds() <= max_gap_seconds:
            old_pos = pos_for_key[key]
            tombstoned.add(old_pos)
            folded_ids[old_pos].append(kept[old_pos]["id"])
            new_pos = len(kept)
            kept.append(row)
            folded_ids.append(folded_ids[old_pos])
            folded_ids[old_pos] = []
            pos_for_key[key] = new_pos
            ts_for_key[key] = row_dt
        else:
            kept.append(row)
            folded_ids.append([])
            pos_for_key[key] = len(kept) - 1
            ts_for_key[key] = row_dt

    out_kept = [r for i, r in enumerate(kept) if i not in tombstoned]
    out_folded = [folded_ids[i] for i in range(len(kept)) if i not in tombstoned]
    return out_kept, out_folded


# ─── capture rendering ─────────────────────────────────────────────────────


def row_to_view(row: sqlite3.Row, sub_context: str = "") -> CaptureView:
    title = row["window_title"] or ""
    if sub_context:
        title = f"{title} / {sub_context}" if title else sub_context
    return CaptureView(
        id=row["id"],
        timestamp=row["timestamp"],
        app=row["app_name"] or "",
        window_title=title,
        focused_role=row["focused_role"] or "",
        focused_value=row["focused_value"] or "",
        url=row["url"] or "",
        visible_text=row["visible_text"] or "",
    )


# ─── routing ──────────────────────────────────────────────────────────────


def run_routing(
    cfg,
    *,
    in_conn: sqlite3.Connection,
    out_conn: sqlite3.Connection,
    captures: list[sqlite3.Row],
    top_k: int,
    log_path: Path,
    sub_context_for: dict[str, str] | None = None,
    buffer_dir: Path | None = None,
    folded_for_kept: dict[str, list[str]] | None = None,
) -> dict:
    sub_context_for = sub_context_for or {}
    folded_for_kept = folded_for_kept or {}
    decisions: list[dict] = []
    routed = {"continue": 0, "new": 0, "close_and_new": 0, "fallback_first": 0}

    for i, row in enumerate(captures):
        cap_sub = sub_context_for.get(row["id"], "")
        cap = row_to_view(row, cap_sub)

        open_threads = thread_store.list_recent_threads(out_conn, top_k=top_k)

        # Pull each open thread's full capture history so the router judges
        # by activity pattern, not by abstract narrative.
        thread_captures: dict[str, list[CaptureView]] = {}
        for t in open_threads:
            cap_ids = thread_store.thread_capture_ids(out_conn, t.id)
            if not cap_ids:
                thread_captures[t.id] = []
                continue
            placeholders = ",".join("?" * len(cap_ids))
            t_cap_rows = in_conn.execute(
                "SELECT id, timestamp, app_name, window_title, focused_role, "
                "       focused_value, visible_text, url "
                f"  FROM captures WHERE id IN ({placeholders}) "
                " ORDER BY timestamp ASC",
                cap_ids,
            ).fetchall()
            thread_captures[t.id] = [
                row_to_view(r, sub_context_for.get(r["id"], "")) for r in t_cap_rows
            ]

        if not open_threads:
            tid = thread_store.open_thread(
                out_conn, title=cap.app or "Untitled", opened_at=cap.timestamp,
            )
            thread_store.append_capture(
                out_conn, thread_id=tid, capture_id=cap.id, at=cap.timestamp,
            )
            routed["fallback_first"] += 1
            decisions.append({
                "i": i, "ts": cap.timestamp, "action": "first_thread",
                "thread_id": tid, "reason": "no open threads",
            })
            continue

        try:
            decision = thread_router.route(
                cfg, capture=cap, open_threads=open_threads,
                thread_captures=thread_captures,
                buffer_dir=buffer_dir,
                folded_capture_ids=tuple(folded_for_kept.get(cap.id, ())),
            )
        except Exception as e:  # noqa: BLE001
            print(f"  ! router error at {i}: {e}; routing as continue-most-recent",
                  file=sys.stderr)
            decision = thread_router.RouteDecision(
                action="continue", thread_id=open_threads[0].id,
                close_thread_ids=[], new_title=None,
                updated_title="", updated_summary="",
                reason=f"router_exception: {e}", raw="",
            )

        for close_id in decision.close_thread_ids:
            thread_store.close_thread(out_conn, thread_id=close_id, closed_at=cap.timestamp)

        if decision.action == "continue" and decision.thread_id:
            target = decision.thread_id
        else:
            target = thread_store.open_thread(
                out_conn,
                title=decision.new_title or (cap.app or "Untitled"),
                opened_at=cap.timestamp,
            )

        thread_store.append_capture(
            out_conn, thread_id=target, capture_id=cap.id, at=cap.timestamp,
        )

        if decision.updated_title:
            thread_store.update_title(
                out_conn, thread_id=target, title=decision.updated_title,
            )

        routed[decision.action] = routed.get(decision.action, 0) + 1
        decisions.append({
            "i": i, "ts": cap.timestamp, "app": cap.app,
            "title_hint": cap.window_title[:60],
            "action": decision.action, "thread_id": target,
            "close_ids": decision.close_thread_ids,
            "updated_title": decision.updated_title,
            "reason": decision.reason,
        })

        if (i + 1) % 10 == 0:
            print(f"  ... routed {i + 1}/{len(captures)}", file=sys.stderr)

    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(decisions, indent=2, ensure_ascii=False))
    return {
        "captures_routed": len(captures),
        "actions": routed,
        "decision_log": str(log_path),
    }


# ─── summarization + md rendering ──────────────────────────────────────────


def close_remaining(out_conn: sqlite3.Connection, *, closed_at: str) -> int:
    threads = thread_store.list_open_threads(out_conn)
    for t in threads:
        thread_store.close_thread(out_conn, thread_id=t.id, closed_at=closed_at)
    return len(threads)


def summarize_all(
    cfg, *, in_conn, out_conn, out_dir: Path, buffer_dir: Path,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict] = []
    rows = out_conn.execute(
        "SELECT id, title, opened_at, closed_at, last_active_at FROM threads "
        "ORDER BY opened_at ASC"
    ).fetchall()
    for r in rows:
        tid = r["id"]
        cap_ids = thread_store.thread_capture_ids(out_conn, tid)
        if not cap_ids:
            continue
        placeholders = ",".join("?" * len(cap_ids))
        cap_rows = in_conn.execute(
            "SELECT id, timestamp, app_name, bundle_id, window_title, "
            "       focused_role, focused_value, visible_text, url "
            f"  FROM captures WHERE id IN ({placeholders}) "
            " ORDER BY timestamp ASC",
            cap_ids,
        ).fetchall()
        captures = [row_to_view(cr) for cr in cap_rows]

        narrative = ""
        title = r["title"]
        outcome = ""
        key_events: list[str] = []
        try:
            summary = thread_summarizer.summarize(
                cfg, thread_id=tid, title=r["title"],
                opened_at=r["opened_at"],
                closed_at=r["closed_at"] or r["last_active_at"],
                captures=captures,
                buffer_dir=buffer_dir,
            )
            title = summary.title or r["title"]
            narrative = summary.narrative
            outcome = summary.outcome
            key_events = summary.key_events
        except Exception as e:  # noqa: BLE001
            print(f"  ! summarizer error for {tid}: {e}", file=sys.stderr)

        md = _render_thread_md(
            tid=tid, title=title,
            opened_at=r["opened_at"],
            closed_at=r["closed_at"] or r["last_active_at"],
            narrative=narrative, outcome=outcome,
            key_events=key_events, captures=captures,
            buffer_dir=buffer_dir,
        )
        (out_dir / f"{tid}.md").write_text(md)
        written.append({
            "thread_id": tid, "title": title,
            "captures": len(captures),
            "outcome": outcome,
        })
    return {"threads_written": len(written), "details": written}


def _safe_codeblock(text: str) -> str:
    if "```" in text:
        text = text.replace("```", "``​`")
    return "```\n" + text.rstrip() + "\n```"


def _render_thread_md(
    *, tid, title, opened_at, closed_at, narrative, captures,
    outcome: str = "", key_events: list[str] | None = None,
    buffer_dir: Path | None = None,
) -> str:
    key_events = key_events or []
    lines = [
        f"# {title}", "",
        f"- **id:** `{tid}`",
        f"- **opened:** {opened_at}",
        f"- **closed:** {closed_at}",
        f"- **captures:** {len(captures)}",
    ]
    if outcome:
        lines.append(f"- **outcome:** {outcome}")
    lines.append("")
    if narrative:
        lines += ["## Narrative", "", narrative.strip(), ""]
    if key_events:
        lines += ["## Key events", ""] + [f"- {ev}" for ev in key_events] + [""]
    lines += ["## Captures", ""]
    for c in captures:
        ts_short = c.timestamp[11:19] if len(c.timestamp) > 11 else c.timestamp
        bits = [ts_short, c.app or "?"]
        if c.window_title:
            bits.append(c.window_title.replace("\n", " ").strip())
        lines.append("### " + " · ".join(bits))
        lines.append("")
        if c.url:
            lines.append(f"**url:** {c.url}")
            lines.append("")
        if c.focused_role:
            lines.append(f"**focused role:** `{c.focused_role}`")
            lines.append("")
        if c.focused_value:
            lines.append("**focused input:**")
            lines.append("")
            lines.append(_safe_codeblock(c.focused_value))
            lines.append("")
        pruned = ax_pruner.load_pruned_text(c.id, buffer_dir=buffer_dir,
                                            fallback=c.visible_text or "")
        if pruned:
            lines.append("<details><summary>visible text</summary>")
            lines.append("")
            lines.append(_safe_codeblock(pruned))
            lines.append("")
            lines.append("</details>")
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


# ─── CLI ───────────────────────────────────────────────────────────────────


def cmd_run(args) -> int:
    cfg = oc_config.load(Path(args.config).expanduser() if args.config else None)
    in_path = Path(cfg.source.index_db).expanduser()
    buffer_dir = Path(cfg.source.capture_buffer_dir).expanduser()
    out_path = Path(cfg.storage.threads_db).expanduser()
    out_dir = Path(cfg.storage.out_dir).expanduser()

    if args.reset and out_path.exists():
        out_path.unlink()
        print(f"reset: removed {out_path}", file=sys.stderr)

    print(f"router model:     {cfg.model_for('thread_router').model}", file=sys.stderr)
    print(f"summarizer model: {cfg.model_for('thread_summarizer').model}", file=sys.stderr)

    in_conn = open_input_db(in_path)
    out_conn = open_replay_db(out_path)

    print("fetching captures...", file=sys.stderr)
    rows = fetch_captures(in_conn, since=args.since, until=args.until, limit=args.limit)
    raw_count = len(rows)
    print(f"fetched {raw_count} captures", file=sys.stderr)
    if not rows:
        print("nothing to do", file=sys.stderr)
        return 0

    sub_context_for: dict[str, str] = {}
    print(f"extracting sub-context for {raw_count} captures...", file=sys.stderr)
    for row in rows:
        sc = ax_pruner.load_sub_context(row["id"], buffer_dir=buffer_dir)
        if sc:
            sub_context_for[row["id"]] = sc

    kept, folded = coalesce_runs(
        rows, max_gap_seconds=cfg.coalesce.gap_seconds,
        sub_context_for=sub_context_for,
    )
    print(f"coalesce: {raw_count} → {len(kept)} ({raw_count - len(kept)} folded)",
          file=sys.stderr)
    rows = kept
    # Map kept_capture_id → list of folded capture IDs that collapsed
    # into it. Used by the router to merge OCR text across the whole
    # phase — the latest kept frame's OCR is often the sparsest, so
    # we union the OCR from all folded siblings to get the rich signal
    # (subtitles, chapter list, etc.) that flashed mid-phase.
    folded_for_kept: dict[str, list[str]] = {
        row["id"]: list(folded[i]) for i, row in enumerate(kept)
    }

    log_path = out_dir / "_decisions.json"
    t0 = time.monotonic()
    routing_stats = run_routing(
        cfg, in_conn=in_conn, out_conn=out_conn, captures=rows,
        top_k=cfg.router.top_k, log_path=log_path,
        sub_context_for=sub_context_for,
        buffer_dir=buffer_dir,
        folded_for_kept=folded_for_kept,
    )
    routing_stats["routing_seconds"] = round(time.monotonic() - t0, 1)

    last_ts = rows[-1]["timestamp"]
    closed_n = close_remaining(out_conn, closed_at=last_ts)
    routing_stats["forced_close_at_end"] = closed_n

    print("summarizing closed threads...", file=sys.stderr)
    t2 = time.monotonic()
    summary_stats = summarize_all(
        cfg, in_conn=in_conn, out_conn=out_conn,
        out_dir=out_dir, buffer_dir=buffer_dir,
    )
    summary_stats["summary_seconds"] = round(time.monotonic() - t2, 1)

    report = {
        "input_db": str(in_path),
        "threads_db": str(out_path),
        "out_dir": str(out_dir),
        "captures_window": {
            "since": rows[0]["timestamp"],
            "until": rows[-1]["timestamp"],
            "raw_count": raw_count,
            "after_coalesce": len(rows),
            "folded": raw_count - len(rows),
        },
        "routing": routing_stats,
        "summary": summary_stats,
    }
    (out_dir / "_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwrote {summary_stats['threads_written']} thread files to {out_dir}",
          file=sys.stderr)
    return 0


def cmd_init(args) -> int:
    path = Path(args.config).expanduser() if args.config else oc_config.default_config_path()
    if path.exists() and not args.force:
        print(f"config already exists at {path} (use --force to overwrite)", file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(oc_config.DEFAULT_CONFIG_TEMPLATE)
    print(f"wrote default config → {path}", file=sys.stderr)
    return 0


# ─── daemon: start / stop / status ─────────────────────────────────────────


def _read_pid() -> int | None:
    pf = paths.pid_file()
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
    except ValueError:
        return None
    # Verify process is alive
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    return pid


def cmd_start(args) -> int:
    # Auto-init: first run lays down the default config so users don't
    # have to remember `personalmem init` before `personalmem start`.
    cfg_path = Path(args.config).expanduser() if args.config else oc_config.default_config_path()
    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(oc_config.DEFAULT_CONFIG_TEMPLATE)
        print(f"first run: wrote default config → {cfg_path}", file=sys.stderr)

    # First-run onboarding (interactive only — no-ops in non-TTY shells).
    from . import onboard
    if onboard.needs_onboarding():
        try:
            onboard.run_onboarding()
        except (KeyboardInterrupt, EOFError):
            print("\nonboarding cancelled — run `personalmem onboard` to retry",
                  file=sys.stderr)
            return 1
        except Exception as e:  # noqa: BLE001
            print(f"onboarding failed: {e}", file=sys.stderr)
            return 1

    cfg = oc_config.load(cfg_path)
    pid = _read_pid()
    if pid:
        print(f"already running (pid {pid})", file=sys.stderr)
        return 1

    from . import daemon

    if args.foreground:
        print("PersonalMem capture daemon starting in foreground — Ctrl+C to stop.",
              file=sys.stderr)
        daemon.run(cfg)
        return 0

    # Background: double-fork → fully detached daemon
    if os.fork() != 0:
        print("PersonalMem started in background.", file=sys.stderr)
        print(f"Logs: {paths.logs_dir()}", file=sys.stderr)
        return 0
    os.setsid()
    if os.fork() != 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    daemon.run(cfg)
    os._exit(0)


def cmd_stop(args) -> int:
    pid = _read_pid()
    if not pid:
        print("daemon not running", file=sys.stderr)
        return 1
    os.kill(pid, signal.SIGTERM)
    print(f"sent SIGTERM to pid {pid}", file=sys.stderr)
    return 0


def cmd_onboard(args) -> int:
    from . import onboard
    ran = onboard.run_onboarding(force=True)
    if not ran:
        print("onboarding skipped (non-interactive shell)", file=sys.stderr)
        return 1
    return 0


def cmd_status(args) -> int:
    pid = _read_pid()
    print(f"  Root          {paths.root()}")
    print(f"  Daemon        {'running (pid ' + str(pid) + ')' if pid else 'stopped'}")
    buf = paths.capture_buffer_dir()
    if buf.exists():
        files = sorted(buf.glob("*.json"))
        last = files[-1].stem if files else "(none)"
        print(f"  Buffer        {len(files)} files, last: {last}")
    else:
        print(f"  Buffer        (no buffer dir yet)")
    db = paths.index_db()
    if db.exists():
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            (n_caps,) = conn.execute("SELECT COUNT(*) FROM captures").fetchone()
            try:
                (n_threads,) = conn.execute("SELECT COUNT(*) FROM threads").fetchone()
            except sqlite3.OperationalError:
                n_threads = 0
            print(f"  Captures      {n_caps}")
            print(f"  Threads       {n_threads}")
        except sqlite3.Error as e:
            print(f"  DB error      {e}")
    else:
        print(f"  Captures      (no index.db yet)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="personalmem")
    ap.add_argument("--config", default=None,
                    help="path to config.toml (default: ~/.personalmem/config.toml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp_run = sub.add_parser("run", help="replay captures into topic threads")
    sp_run.add_argument("--since", default=None)
    sp_run.add_argument("--until", default=None)
    sp_run.add_argument("--limit", type=int, default=None)
    sp_run.add_argument("--reset", action="store_true",
                        help="wipe threads_db before this run")
    sp_run.set_defaults(func=cmd_run)

    sp_init = sub.add_parser("init", help="write default config.toml")
    sp_init.add_argument("--force", action="store_true")
    sp_init.set_defaults(func=cmd_init)

    sp_start = sub.add_parser("start", help="start the capture daemon")
    sp_start.add_argument("-f", "--foreground", action="store_true",
                          help="run in this terminal instead of double-forking")
    sp_start.set_defaults(func=cmd_start)

    sp_stop = sub.add_parser("stop", help="stop the capture daemon")
    sp_stop.set_defaults(func=cmd_stop)

    sp_status = sub.add_parser("status", help="show daemon + data status")
    sp_status.set_defaults(func=cmd_status)

    sp_onb = sub.add_parser("onboard", help="(re)pick LLM provider")
    sp_onb.set_defaults(func=cmd_onboard)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
