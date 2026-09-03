"""
Trading Bot TUI — tui.py
Drop in repo root alongside main.py.
Run: python tui.py
Mock data lives in MockDataProvider — swap for real data hooks later.
"""

from __future__ import annotations

import random
from collections import deque
from datetime import datetime, timedelta
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.reactive import reactive
from textual.widgets import (
    DataTable, Footer, Header, Label, Static, TabbedContent, TabPane
)
from textual.timer import Timer
from rich.text import Text
from rich.table import Table
from rich.panel import Panel
from rich import box


# ─────────────────────────────────────────────
# MOCK DATA PROVIDER
# Swap these functions/classes for real data hooks later.
# ─────────────────────────────────────────────

class MockDataProvider:
    """
    All mock state lives here.
    To hook real data: replace each method's return value with
    a read from your BotCoordinator's shared state object.
    """

    PIPELINE_STAGES = [
        "Scanner", "Tier Reg", "Priority", "GT Router",
        "Gap Detect", "Confidence", "Executor", "Decay Mon"
    ]

    GT_SOURCES = [
        ("ESPN",       "ok"),
        ("Yahoo Fin",  "ok"),
        ("FRED",       "stale"),
        ("SportsLive", "ok"),
        ("KalshiWS",   "ok"),
        ("Polymarket", "ok"),
    ]

    INITIAL_POSITIONS = [
        {"market": "NBA: BOS ML",    "side": "YES", "entry": 0.62, "current": 0.67, "size": 10.0, "conf": 74},
        {"market": "CPI >3.2%",      "side": "NO",  "entry": 0.41, "current": 0.38, "size": 10.0, "conf": 68},
        {"market": "MLB: LAD -1.5",  "side": "YES", "entry": 0.55, "current": 0.52, "size": 10.0, "conf": 61},
        {"market": "AAPL >$195",     "side": "YES", "entry": 0.48, "current": 0.51, "size": 10.0, "conf": 66},
        {"market": "NFP >200k",      "side": "NO",  "entry": 0.39, "current": 0.44, "size": 10.0, "conf": 59},
        {"market": "FED +25bps",     "side": "YES", "entry": 0.71, "current": 0.73, "size": 8.0,  "conf": 78},
    ]

    CLOSED_TRADES = [
        {"market": "NBA: MIA ML",   "side": "YES", "entry": 0.58, "exit": 0.72, "size": 10.0, "result": "WIN",  "pnl":  1.40},
        {"market": "CPI <3.0%",     "side": "NO",  "entry": 0.44, "exit": 0.31, "size": 10.0, "result": "WIN",  "pnl":  1.30},
        {"market": "S&P >5200",     "side": "YES", "entry": 0.60, "exit": 0.45, "size": 10.0, "result": "LOSS", "pnl": -1.50},
        {"market": "MLB: NYY -1.5", "side": "NO",  "entry": 0.38, "exit": 0.50, "size": 10.0, "result": "LOSS", "pnl": -1.20},
        {"market": "NFP >180k",     "side": "YES", "entry": 0.52, "exit": 0.68, "size": 10.0, "result": "WIN",  "pnl":  1.60},
        {"market": "NBA: LAL ML",   "side": "YES", "entry": 0.45, "exit": 0.55, "size": 10.0, "result": "WIN",  "pnl":  1.00},
        {"market": "AAPL >$190",    "side": "YES", "entry": 0.61, "exit": 0.58, "size": 10.0, "result": "LOSS", "pnl": -0.30},
        {"market": "FED hold",      "side": "YES", "entry": 0.77, "exit": 0.88, "size": 10.0, "result": "WIN",  "pnl":  1.10},
    ]

    FEED_EVENTS = deque([
        ("14:33:01", "SIGNAL", "NBA: BOS ML — gap detected 62 vs 67 implied · conf 74%"),
        ("14:32:44", "GT",     "FRED data age check: CPI last updated 14d ago — staleness flag raised"),
        ("14:31:58", "EXEC",   "Ghost fill: CPI >3.2% NO @ 0.41 · $10.00 · book depth OK"),
        ("14:30:12", "DECAY",  "MLB: LAD -1.5 confidence decayed 66→61% · monitoring"),
        ("14:28:03", "WARN",   "NFP >200k orderbook thin — mid unreliable · hard stop risk"),
        ("14:25:41", "SIGNAL", "FED +25bps YES — gap detected 71 vs 78 implied · conf 78%"),
        ("14:24:19", "EXEC",   "Ghost fill: FED +25bps YES @ 0.71 · $8.00 · book depth OK"),
        ("14:22:05", "GT",     "SportsLive: ping 44ms — OK"),
        ("14:18:33", "SIGNAL", "AAPL >$195 YES — gap detected 48 vs 55 implied · conf 66%"),
        ("14:15:09", "DECAY",  "CPI >3.2% NO confidence stable at 68%"),
        ("14:10:44", "WARN",   "KalshiWS reconnect — orderbook stream interrupted 2s"),
        ("14:07:22", "GT",     "FRED: prior-period data detected on CPI market — skipping"),
        ("14:05:01", "SIGNAL", "MLB: LAD -1.5 YES — gap detected 55 vs 62 implied · conf 63%"),
        ("14:01:38", "EXEC",   "Ghost fill: NBA: BOS ML YES @ 0.62 · $10.00 · book depth OK"),
    ], maxlen=200)

    def __init__(self):
        self.positions = [dict(p) for p in self.INITIAL_POSITIONS]
        self._pipeline_stage = 4
        self._tick = 0
        self.session_start = datetime.now() - timedelta(hours=4, minutes=12)
        self.bankroll = 312.58
        self.realized_pnl = 14.82
        self.fees_paid = 1.44
        self.win_rate = 63
        self.signals_this_week = 14

    def tick(self):
        """Called every second. Drift prices slightly, advance pipeline."""
        self._tick += 1
        for p in self.positions:
            drift = random.uniform(-0.005, 0.005)
            p["current"] = round(max(0.01, min(0.99, p["current"] + drift)), 3)

        if self._tick % 12 == 0:
            self._pipeline_stage = (self._pipeline_stage + 1) % len(self.PIPELINE_STAGES)

        if self._tick % 20 == 0:
            self._inject_feed_event()

    def _inject_feed_event(self):
        tags = ["SIGNAL", "EXEC", "DECAY", "GT", "WARN"]
        messages = [
            ("SIGNAL", "Scanner found new gap on NBA market · evaluating"),
            ("GT",     "Yahoo Fin: equity prices refreshed OK"),
            ("DECAY",  "Confidence monitor: all positions stable"),
            ("EXEC",   "Ghost fill evaluated — below min confidence threshold, skipped"),
            ("WARN",   "Thin orderbook detected on macro market · mid flagged"),
            ("GT",     "KalshiWS: heartbeat OK · 12ms latency"),
            ("SIGNAL", "FRED macro signal — staleness check pending"),
        ]
        ts = datetime.now().strftime("%H:%M:%S")
        _, (tag, msg) = random.choice(list(enumerate(messages)))
        self.FEED_EVENTS.appendleft((ts, tag, msg))

    def get_unrealized_pnl(self) -> float:
        total = 0.0
        for p in self.positions:
            sign = 1 if p["side"] == "YES" else -1
            total += sign * (p["current"] - p["entry"]) * p["size"]
        return round(total, 2)

    def get_allocated(self) -> float:
        return round(sum(p["size"] for p in self.positions), 2)

    def get_pipeline_stage(self) -> int:
        return self._pipeline_stage

    def get_uptime(self) -> str:
        delta = datetime.now() - self.session_start
        h, rem = divmod(int(delta.total_seconds()), 3600)
        m = rem // 60
        return f"{h}h {m:02d}m"


# ─────────────────────────────────────────────
# WIDGETS
# ─────────────────────────────────────────────

class StatusBar(Static):
    """Top bar: mode badge, bot name, uptime, clock."""

    is_live: reactive[bool] = reactive(False)

    def __init__(self, provider: MockDataProvider, **kwargs):
        super().__init__(**kwargs)
        self.provider = provider

    def render(self) -> Text:
        mode = "● LIVE" if self.is_live else "○ GHOST"
        mode_style = "bold red" if self.is_live else "bold green"
        uptime = self.provider.get_uptime()
        clock = datetime.now().strftime("%a %b %d  %H:%M:%S")

        t = Text()
        t.append("  KALSHI-BOT v0.4  ", style="bold white")
        t.append(" ")
        t.append(f" {mode} ", style=f"{mode_style} on {'dark_red' if self.is_live else 'dark_green'}")
        t.append(f"  uptime {uptime}", style="dim")
        t.append("  " + "─" * 30 + "  ", style="dim")
        t.append(clock, style="dim")
        return t


class PipelineWidget(Static):
    """Pipeline stage progress bar."""

    active_stage: reactive[int] = reactive(0)

    def __init__(self, provider: MockDataProvider, **kwargs):
        super().__init__(**kwargs)
        self.provider = provider

    def render(self) -> Text:
        stages = MockDataProvider.PIPELINE_STAGES
        active = self.active_stage
        t = Text()
        for i, stage in enumerate(stages):
            if i < active:
                t.append(f" {stage} ", style="bold #00FF00 on #001400")
                t.append(" › ", style="#00FF00")
            elif i == active:
                t.append(f" {stage} ", style="bold #FFA028 on #1a0e00")
                if i < len(stages) - 1:
                    t.append(" › ", style="#FFA028")
            else:
                t.append(f" {stage} ", style="#555555 on #000000")
                if i < len(stages) - 1:
                    t.append(" › ", style="#333333")
        return t


class StatPanel(Static):
    """Renders a labeled stat block."""

    def __init__(self, title: str, rows: list[tuple], **kwargs):
        super().__init__(**kwargs)
        self._title = title
        self._rows = rows

    def update_rows(self, rows: list[tuple]):
        self._rows = rows
        self.refresh()

    def render(self) -> Panel:
        t = Text()
        for label, val, style in self._rows:
            t.append(f"  {label:<18}", style="#FFA028")
            t.append(f"{val}\n", style=style)
        return Panel(t, title=f"[bold #FFA028]{self._title}[/]", border_style="#555555", padding=(0, 0))


class FeedWidget(Static):
    """Live event feed."""

    TAG_STYLES = {
        "SIGNAL": ("#F9FF00", "#141400"),
        "EXEC":   ("#00FF00", "#001400"),
        "DECAY":  ("#FFA028", "#100800"),
        "GT":     ("#FFA028", "#100800"),
        "WARN":   ("#FF0000", "#140000"),
    }

    def __init__(self, provider: MockDataProvider, max_rows: int = 8, **kwargs):
        super().__init__(**kwargs)
        self.provider = provider
        self.max_rows = max_rows

    def render(self) -> Text:
        t = Text()
        for ts, tag, msg in list(self.provider.FEED_EVENTS)[:self.max_rows]:
            fg, bg = self.TAG_STYLES.get(tag, ("#FFFFFF", "#000000"))
            t.append(f" {ts} ", style="#555555")
            t.append(f" {tag:<6} ", style=f"bold {fg} on {bg}")
            t.append(f"  {msg}\n", style="#FFFFFF")
        return t


class GTSourcesWidget(Static):
    """Ground truth source health dots."""

    STATUS_STYLES = {
        "ok":    ("●", "bold #00FF00"),
        "stale": ("●", "bold #F9FF00"),
        "down":  ("●", "bold #FF0000"),
    }

    def render(self) -> Text:
        t = Text()
        for name, status in MockDataProvider.GT_SOURCES:
            dot, style = self.STATUS_STYLES.get(status, ("●", "#FFA028"))
            t.append(f"  {name:<12}", style="#FFA028")
            label = status.upper()
            t.append(f"{dot} {label}\n", style=style)
        return t


class PositionsTable(Static):
    """Open positions table."""

    def __init__(self, provider: MockDataProvider, **kwargs):
        super().__init__(**kwargs)
        self.provider = provider

    def render(self) -> Table:
        tbl = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style="bold #FFA028",
            padding=(0, 1),
            expand=True,
        )
        tbl.add_column("Market",   style="#FFA028",  no_wrap=True)
        tbl.add_column("Side",     justify="center")
        tbl.add_column("Entry",    justify="right", style="#555555")
        tbl.add_column("Current",  justify="right")
        tbl.add_column("Unr. P&L", justify="right")
        tbl.add_column("Conf",     justify="right")

        for p in self.provider.positions:
            sign = 1 if p["side"] == "YES" else -1
            unr = sign * (p["current"] - p["entry"]) * p["size"]
            unr_str = f"+${unr:.2f}" if unr >= 0 else f"-${abs(unr):.2f}"
            unr_style = "#00FF00" if unr >= 0 else "#FF0000"
            side_style = "bold #00FF00" if p["side"] == "YES" else "bold #FF0000"
            conf = p["conf"]
            if conf >= 70:
                conf_style = "#00FF00"
            elif conf >= 60:
                conf_style = "#FFFFFF"
            else:
                conf_style = "#FF0000"

            tbl.add_row(
                p["market"],
                Text(p["side"], style=side_style),
                f"{p['entry']:.3f}",
                f"{p['current']:.3f}",
                Text(unr_str, style=unr_style),
                Text(f"{conf}%", style=conf_style),
            )
        return tbl


class PnLTable(Static):
    """Closed trades P&L history."""

    def __init__(self, provider: MockDataProvider, **kwargs):
        super().__init__(**kwargs)
        self.provider = provider

    def render(self) -> Table:
        tbl = Table(
            box=box.SIMPLE,
            show_header=True,
            header_style="bold #FFA028",
            padding=(0, 1),
            expand=True,
        )
        tbl.add_column("Market",  style="#FFA028", no_wrap=True)
        tbl.add_column("Side",    justify="center")
        tbl.add_column("Entry",   justify="right", style="#555555")
        tbl.add_column("Exit",    justify="right", style="#555555")
        tbl.add_column("Size",    justify="right", style="#555555")
        tbl.add_column("Result",  justify="center")
        tbl.add_column("P&L",     justify="right")

        cumulative = 0.0
        for t in self.provider.CLOSED_TRADES:
            cumulative += t["pnl"]
            result_style = "bold #00FF00" if t["result"] == "WIN" else "bold #FF0000"
            pnl_str = f"+${t['pnl']:.2f}" if t["pnl"] >= 0 else f"-${abs(t['pnl']):.2f}"
            pnl_style = "#00FF00" if t["pnl"] >= 0 else "#FF0000"
            tbl.add_row(
                t["market"],
                Text(t["side"], style="#00FF00" if t["side"] == "YES" else "#FF0000"),
                f"{t['entry']:.3f}",
                f"{t['exit']:.3f}",
                f"${t['size']:.0f}",
                Text(t["result"], style=result_style),
                Text(pnl_str, style=pnl_style),
            )

        return tbl


# ─────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────

CSS = """
Screen {
    background: #000000;
    color: #FFFFFF;
}

#statusbar {
    background: #000000;
    color: #FFFFFF;
    height: 1;
    padding: 0 1;
    border-bottom: tall #555555;
}

#pipeline-container {
    background: #000000;
    height: 2;
    padding: 0 1;
    border-bottom: tall #555555;
    content-align: left middle;
}

#pipeline-label {
    color: #FFA028;
    width: auto;
    padding: 0 1;
}

TabbedContent {
    height: 1fr;
}

TabPane {
    padding: 0 1;
    margin: 0;
    background: #000000;
}

.panel-row {
    height: auto;
    margin: 0;
}

.stat-col {
    width: 1fr;
    margin: 0;
    border-right: tall #555555;
}

.stat-col:last-child {
    border-right: none;
}

.feed-full {
    height: 1fr;
    overflow-y: auto;
    background: #000000;
}

.section-label {
    color: #FFA028;
    text-style: bold;
    margin: 0;
    padding: 0 0;
}

#hotkey-bar {
    background: #000000;
    height: 1;
    border-top: tall #555555;
    padding: 0 1;
}

.hk {
    color: #555555;
    margin-right: 1;
}

.hk-label {
    color: #FFA028;
    margin-right: 2;
}

.hk-key {
    color: #FFA028;
}

.hk-kill {
    color: #FF0000;
}

.hk-pause {
    color: #F9FF00;
}

Tab {
    background: #000000;
    color: #555555;
    padding: 0 1;
}

Tab:hover {
    background: #000000;
    color: #FFA028;
}

Tab.-active {
    background: #000000;
    color: #FFA028;
    text-style: bold;
}

ContentSwitcher {
    background: #000000;
}
"""


class TradingBotTUI(App):
    CSS = CSS

    BINDINGS = [
        Binding("f7", "toggle_mode",  "Toggle Ghost/Live"),
        Binding("f8", "pause_trading","Pause"),
        Binding("f9", "kill_bot",     "Kill"),
        Binding("q",  "quit",         "Quit"),
    ]

    is_live:   reactive[bool] = reactive(False)
    is_paused: reactive[bool] = reactive(False)

    def __init__(self):
        super().__init__()
        self.provider = MockDataProvider()
        self._timer: Timer | None = None

    # ── Layout ──────────────────────────────

    def compose(self) -> ComposeResult:
        p = self.provider

        yield Static("", id="statusbar")

        with Horizontal(id="pipeline-container"):
            yield Static("PIPELINE  ", id="pipeline-label")
            yield PipelineWidget(p, id="pipeline")

        with TabbedContent(
            "Overview", "Positions", "P&L", "Live Feed", "Graphs",
            id="tabs"
        ):
            # ── Overview ──
            with TabPane("Overview", id="tab-overview"):
                with Horizontal(classes="panel-row"):
                    yield StatPanel("Session P&L", self._pnl_rows(), id="sp-pnl",      classes="stat-col")
                    yield StatPanel("Account",     self._acct_rows(), id="sp-acct",     classes="stat-col")
                    yield StatPanel("Performance", self._perf_rows(), id="sp-perf",     classes="stat-col")
                    yield GTSourcesWidget(id="sp-gt",  classes="stat-col")

                yield Static("OPEN POSITIONS", classes="section-label")
                yield PositionsTable(p, id="ov-positions")

                yield Static("LIVE FEED", classes="section-label")
                yield FeedWidget(p, max_rows=6, id="ov-feed")

            # ── Positions ──
            with TabPane("Positions", id="tab-positions"):
                yield Static("OPEN POSITIONS", classes="section-label")
                yield PositionsTable(p, id="pos-table")

            # ── P&L ──
            with TabPane("P&L", id="tab-pnl"):
                yield Static("CLOSED TRADES", classes="section-label")
                yield PnLTable(p, id="pnl-table")
                yield Static("", id="pnl-summary")

            # ── Live Feed ──
            with TabPane("Live Feed", id="tab-feed"):
                with ScrollableContainer(classes="feed-full"):
                    yield FeedWidget(p, max_rows=100, id="full-feed")

            # ── Graphs ──
            with TabPane("Graphs", id="tab-graphs"):
                yield Static(
                    "\n\n  [#555555]Graphs coming in next phase.[/]\n"
                    "  [#555555]Planned: rolling win rate · session P&L curve · confidence vs outcome · edge by category[/]",
                    markup=True
                )

        with Horizontal(id="hotkey-bar"):
            yield Static("SUGGESTED:", classes="hk hk-label")
            yield Static("F7", classes="hk hk-key")
            yield Static("MODE", classes="hk")
            yield Static("F8", classes="hk hk-pause")
            yield Static("PAUSE", classes="hk")
            yield Static("F9", classes="hk hk-kill")
            yield Static("KILL", classes="hk")
            yield Static("Q", classes="hk hk-key")
            yield Static("QUIT", classes="hk")

    # ── Lifecycle ───────────────────────────

    def on_mount(self) -> None:
        self._timer = self.set_interval(1.0, self._tick)
        self._refresh_all()

    def _tick(self) -> None:
        self.provider.tick()
        self._refresh_all()

    def _refresh_all(self) -> None:
        p = self.provider

        # statusbar
        mode = "● LIVE" if self.is_live else "○ GHOST"
        mode_markup = f"[bold #FF0000 on #1a0000] {mode} [/]" if self.is_live else f"[bold #00FF00 on #001400] {mode} [/]"
        paused = "  [bold #F9FF00]⏸ PAUSED[/]" if self.is_paused else ""
        self.query_one("#statusbar", Static).update(
            f"[bold #FFA028]  KALSHI-BOT[/][bold #F9FF00] <Equity>[/]  {mode_markup}{paused}"
            f"  [#555555]uptime {p.get_uptime()}[/]"
            f"  [#333333]{'─' * 20}[/]"
            f"  [#FFFFFF]{datetime.now().strftime('%a %b %d  %H:%M:%S')}[/]"
        )

        # pipeline
        self.query_one("#pipeline", PipelineWidget).active_stage = p.get_pipeline_stage()

        # stat panels
        self.query_one("#sp-pnl", StatPanel).update_rows(self._pnl_rows())
        self.query_one("#sp-acct", StatPanel).update_rows(self._acct_rows())
        self.query_one("#sp-perf", StatPanel).update_rows(self._perf_rows())

        # positions
        for wid in self.query(PositionsTable):
            wid.refresh()

        # feed widgets
        for wid in self.query(FeedWidget):
            wid.refresh()

        # pnl summary
        wins  = sum(1 for t in p.CLOSED_TRADES if t["result"] == "WIN")
        total = len(p.CLOSED_TRADES)
        net   = sum(t["pnl"] for t in p.CLOSED_TRADES)
        wr    = round(wins / total * 100) if total else 0
        net_s = f"+${net:.2f}" if net >= 0 else f"-${abs(net):.2f}"
        net_color = "#00FF00" if net >= 0 else "#FF0000"
        wr_color = "bold #00FF00" if wr >= 60 else ("bold #F9FF00" if wr >= 50 else "bold #FF0000")
        try:
            self.query_one("#pnl-summary", Static).update(
                f"\n  [#FFA028]Closed trades:[/] [#FFFFFF]{total}[/]  "
                f"[#FFA028]Win rate:[/] [{wr_color}]{wr}%[/]  "
                f"[#FFA028]Net P&L:[/] [bold {net_color}]{net_s}[/]"
            )
        except Exception:
            pass

    # ── Helpers ─────────────────────────────

    def _pnl_rows(self):
        p = self.provider
        unr = p.get_unrealized_pnl()
        net = p.realized_pnl + unr - p.fees_paid
        net_pct = net / p.bankroll * 100

        realized_str = f"+${p.realized_pnl:.2f}" if p.realized_pnl >= 0 else f"-${abs(p.realized_pnl):.2f}"
        realized_color = "bold #00FF00" if p.realized_pnl >= 0 else "bold #FF0000"
        unr_str = f"+${unr:.2f}" if unr >= 0 else f"-${abs(unr):.2f}"
        net_str = f"+{net_pct:.1f}%" if net_pct >= 0 else f"{net_pct:.1f}%"
        net_color = "bold #00FF00" if net >= 0 else "bold #FF0000"

        return [
            ("Realized",    realized_str, realized_color),
            ("Unrealized",  unr_str, "#00FF00" if unr >= 0 else "#FF0000"),
            ("Fees paid",   f"-${p.fees_paid:.2f}", "#FF0000"),
            ("Net edge",    net_str, net_color),
        ]

    def _acct_rows(self):
        p = self.provider
        alloc = p.get_allocated()
        free  = p.bankroll - alloc
        exp   = alloc / p.bankroll * 100
        if exp < 50:
            exp_color = "#FFFFFF"
        elif exp < 80:
            exp_color = "#F9FF00"
        else:
            exp_color = "#FF0000"
        return [
            ("Bankroll",  f"${p.bankroll:.2f}", "bold #FFFFFF"),
            ("Allocated", f"${alloc:.2f}",      "#FFFFFF"),
            ("Free cash", f"${free:.2f}",       "#FFFFFF"),
            ("Exposure",  f"{exp:.1f}%",        exp_color),
        ]

    def _perf_rows(self):
        p = self.provider
        wr = p.win_rate
        if wr >= 60:
            wr_color = "bold #00FF00"
        elif wr >= 50:
            wr_color = "bold #F9FF00"
        else:
            wr_color = "bold #FF0000"

        avg_conf = round(sum(x['conf'] for x in p.positions) / len(p.positions))
        if avg_conf >= 70:
            conf_color = "#00FF00"
        elif avg_conf >= 60:
            conf_color = "#FFFFFF"
        else:
            conf_color = "#FF0000"

        return [
            ("Win rate (30d)",   f"{wr}%",                     wr_color),
            ("Signals/week",     str(p.signals_this_week),     "#FFFFFF"),
            ("Open positions",   str(len(p.positions)),        "#FFFFFF"),
            ("Avg confidence",   f"{avg_conf}%",               conf_color),
        ]

    # ── Actions ─────────────────────────────

    def action_toggle_mode(self) -> None:
        self.is_live = not self.is_live
        # TODO: hook into BotCoordinator.set_live_mode(self.is_live)

    def action_pause_trading(self) -> None:
        self.is_paused = not self.is_paused
        # TODO: hook into BotCoordinator.set_paused(self.is_paused)

    def action_kill_bot(self) -> None:
        # TODO: hook into BotCoordinator.shutdown() before exit
        self.exit()


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    TradingBotTUI().run()
