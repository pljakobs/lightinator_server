#!/usr/bin/env python3
"""
Lightinator firmware updater — a Textual TUI.

Workflow:
    1. Discover controllers on the LAN via mDNS (_lightinator-api._tcp) and/or
       add one manually by IP / hostname.
    2. Pick a target controller — its current firmware, SOC and build type are
       read from GET /info?V=2.
    3. Pick a firmware version from the lightinator.de version API, filtered by
       the controller's SOC / branch / type.
    4. Confirm, provide the OTA password (prompt or RGBWW_API_PASSWORD env var),
       and trigger the update via POST /update. Progress is polled from
       GET /update until the controller reboots.

Dependencies (see requirements-tui.txt):
    textual, httpx, zeroconf, websockets (optional, for live OTA broadcasts)

Run:
    RGBWW_API_PASSWORD=... python3 firmware_updater.py
    python3 firmware_updater.py --api http://lightinator.de/api
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx

try:  # optional — enables live OTA broadcast streaming
    import websockets
except ImportError:  # pragma: no cover
    websockets = None

from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    Static,
)

DEFAULT_API_BASE = os.environ.get("LIGHTINATOR_API", "http://lightinator.de/api")
MDNS_SERVICE = "_lightinator-api._tcp.local."
HTTP_TIMEOUT = 8.0

# OTASTATUS enum from include/otaupdate.h
OTA_STATUS = {
    0: "idle",
    1: "processing",
    2: "success (rebooting)",
    3: "success",
    4: "failed",
    5: "part update 1",
    6: "part update done",
}
OTA_TERMINAL_OK = {2, 3}
OTA_TERMINAL_FAIL = {4}


def _branch_from_version(version: str) -> str:
    """Best-effort branch extraction from a firmware git-describe string.

    e.g. "V5.0-944-experimental" -> "experimental"
         "V5.0-944-experimental-externalui" -> "experimental-externalui"
    """
    parts = version.strip().split("-")
    if len(parts) >= 3:
        return "-".join(parts[2:])
    return ""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Controller:
    name: str
    ip: str
    port: int = 80
    soc: str = ""
    source: str = "mdns"  # "mdns" | "manual"
    version: str = ""
    branch: str = ""
    build_type: str = ""
    info_error: str = ""

    @property
    def key(self) -> str:
        return f"{self.ip}:{self.port}"

    @property
    def base_url(self) -> str:
        if self.port == 80:
            return f"http://{self.ip}"
        return f"http://{self.ip}:{self.port}"

    @property
    def ws_url(self) -> str:
        if self.port == 80:
            return f"ws://{self.ip}/ws"
        return f"ws://{self.ip}:{self.port}/ws"


# ---------------------------------------------------------------------------
# HTTP clients
# ---------------------------------------------------------------------------

class VersionAPI:
    """Client for the lightinator.de firmware version API."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")

    async def _get(self, path: str, params: Optional[dict] = None):
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(f"{self.base}{path}", params=params or {})
            resp.raise_for_status()
            return resp.json()

    async def branches(self, soc: str) -> list[str]:
        return await self._get("/firmware/branches", {"soc": soc})

    async def types(self, soc: str, branch: str) -> list[str]:
        return await self._get("/firmware/types", {"soc": soc, "branch": branch})

    async def versions(self, soc: str, branch: str, ftype: str) -> list[dict]:
        return await self._get(
            "/firmware/versions", {"soc": soc, "branch": branch, "type": ftype}
        )


class ControllerAPI:
    """Client for a single controller's HTTP API."""

    @staticmethod
    async def info(base_url: str) -> dict:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(f"{base_url}/info", params={"V": "2"})
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    async def start_update(base_url: str, rom_url: str, password: str) -> dict:
        # Controllers with apiSecured=false accept /update without auth, so only
        # attach Basic auth when a password was actually provided.
        auth = httpx.BasicAuth(username="admin", password=password) if password else None
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, auth=auth) as client:
            resp = await client.post(
                f"{base_url}/update",
                json={"rom": {"url": rom_url}},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    async def update_status(base_url: str) -> dict:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(f"{base_url}/update")
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    async def restart(base_url: str) -> dict:
        # POST /system {"cmd":"restart"} — the controller reboots after ~1.5 s.
        # The firmware's /system endpoint is not authenticated.
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(
                f"{base_url}/system",
                json={"cmd": "restart"},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            return resp.json()


# ---------------------------------------------------------------------------
# mDNS discovery
# ---------------------------------------------------------------------------

class Discovery:
    def __init__(self, on_found: Callable[[Controller], "asyncio.Future"]):
        self._on_found = on_found
        self._azc: Optional[AsyncZeroconf] = None
        self._browser: Optional[AsyncServiceBrowser] = None

    async def start(self) -> None:
        self._azc = AsyncZeroconf()
        self._browser = AsyncServiceBrowser(
            self._azc.zeroconf, MDNS_SERVICE, handlers=[self._on_change]
        )

    def _on_change(self, zeroconf, service_type, name, state_change) -> None:
        if state_change in (ServiceStateChange.Added, ServiceStateChange.Updated):
            asyncio.ensure_future(self._resolve(service_type, name))

    async def _resolve(self, service_type: str, name: str) -> None:
        assert self._azc is not None
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(self._azc.zeroconf, 3000):
            return
        addresses = info.parsed_addresses()
        if not addresses:
            return
        props = {}
        for k, v in (info.properties or {}).items():
            key = k.decode() if isinstance(k, bytes) else str(k)
            val = v.decode() if isinstance(v, bytes) else v
            props[key] = val
        controller = Controller(
            name=props.get("fn") or name.split(".")[0],
            ip=addresses[0],
            port=info.port or 80,
            soc=props.get("mo", ""),
            source="mdns",
        )
        await self._on_found(controller)

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.async_cancel()
        if self._azc is not None:
            await self._azc.async_close()


# ---------------------------------------------------------------------------
# Screen 1: controller selection
# ---------------------------------------------------------------------------

class ControllerScreen(Screen):
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "rescan", "Rescan"),
        ("x", "reset", "Reset"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._controllers: dict[str, Controller] = {}
        self._discovery: Optional[Discovery] = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Select a controller to update", id="title")
        yield DataTable(id="controllers", cursor_type="row")
        with Horizontal(id="manual-bar"):
            yield Input(placeholder="Add controller by IP or hostname…", id="manual-ip")
            yield Button("Add", id="add-btn", variant="primary")
        yield Static("Scanning for controllers via mDNS…", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#controllers", DataTable)
        table.add_column("Name", key="name")
        table.add_column("Address", key="addr")
        table.add_column("SOC", key="soc")
        table.add_column("Firmware", key="fw", width=36)
        table.add_column("Branch", key="branch", width=20)
        table.add_column("Source", key="source")
        self.start_discovery()

    @work(exclusive=True)
    async def start_discovery(self) -> None:
        self._discovery = Discovery(self._on_found)
        try:
            await self._discovery.start()
        except Exception as exc:  # noqa: BLE001
            self.query_one("#status", Static).update(f"mDNS unavailable: {exc}")

    async def _on_found(self, controller: Controller) -> None:
        if controller.key in self._controllers:
            return
        self._controllers[controller.key] = controller
        table = self.query_one("#controllers", DataTable)
        table.add_row(
            controller.name,
            controller.base_url,
            controller.soc or "?",
            controller.version or "…",
            controller.branch or "…",
            controller.source,
            key=controller.key,
        )
        self.query_one("#status", Static).update(
            f"{len(self._controllers)} controller(s) found. "
            f"Enter selects, r rescans, x resets."
        )
        self.enrich(controller)

    @work
    async def enrich(self, controller: Controller) -> None:
        """Fill in current firmware / SOC / build type from /info."""
        try:
            info = await ControllerAPI.info(controller.base_url)
            controller.soc = (info.get("device", {}).get("soc") or controller.soc)
            controller.version = info.get("app", {}).get("git_version", "")
            controller.build_type = info.get("app", {}).get("build_type", "")
            controller.branch = _branch_from_version(controller.version)
        except Exception as exc:  # noqa: BLE001
            controller.info_error = str(exc)
            controller.version = "unreachable"
        table = self.query_one("#controllers", DataTable)
        if controller.key in self._controllers:
            try:
                table.update_cell(controller.key, "soc", controller.soc or "?")
                table.update_cell(controller.key, "fw", controller.version or "?")
                table.update_cell(controller.key, "branch", controller.branch or "?")
            except Exception:  # noqa: BLE001 — row may have been cleared
                pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "add-btn":
            self._add_manual()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "manual-ip":
            self._add_manual()

    def _add_manual(self) -> None:
        field_widget = self.query_one("#manual-ip", Input)
        target = field_widget.value.strip()
        if not target:
            return
        host, _, port_s = target.partition(":")
        port = int(port_s) if port_s.isdigit() else 80
        controller = Controller(name=host, ip=host, port=port, source="manual")
        field_widget.value = ""
        # Fire and forget — _on_found dedupes and enriches.
        self.run_worker(self._on_found(controller))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        controller = self._controllers.get(key)
        if controller is None:
            return
        if controller.version in ("unreachable", ""):
            self.query_one("#status", Static).update(
                f"{controller.base_url} is not reachable yet — retrying…"
            )
            self.enrich(controller)
            return
        self.app.push_screen(FirmwareScreen(controller))

    def action_rescan(self) -> None:
        self.query_one("#status", Static).update("Rescanning…")
        self.start_discovery()

    def _highlighted_controller(self) -> Optional[Controller]:
        table = self.query_one("#controllers", DataTable)
        if table.row_count == 0:
            return None
        try:
            cell_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0))
            key = cell_key.row_key.value
        except Exception:  # noqa: BLE001
            return None
        return self._controllers.get(key)

    def action_reset(self) -> None:
        controller = self._highlighted_controller()
        if controller is None:
            self.query_one("#status", Static).update(
                "No controller highlighted to reset."
            )
            return
        self.app.push_screen(ResetScreen(controller))

    async def action_quit(self) -> None:
        if self._discovery is not None:
            await self._discovery.stop()
        self.app.exit()


# ---------------------------------------------------------------------------
# Screen 2: firmware version selection
# ---------------------------------------------------------------------------

class FirmwareScreen(Screen):
    BINDINGS = [
        ("escape", "back", "Back"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, controller: Controller) -> None:
        super().__init__()
        self.controller = controller
        self._versions: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            f"[b]{self.controller.name}[/b]  {self.controller.base_url}\n"
            f"SOC: {self.controller.soc or '?'}   "
            f"Current: {self.controller.version or '?'}   "
            f"Build: {self.controller.build_type or '?'}",
            id="ctrl-summary",
        )
        with Horizontal(id="filter-bar"):
            yield Label("Branch:")
            yield Select([], id="branch-select", allow_blank=True)
            yield Label("Type:")
            yield Select([], id="type-select", allow_blank=True)
        yield DataTable(id="versions", cursor_type="row")
        yield Static("Loading firmware branches…", id="fw-status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#versions", DataTable)
        table.add_columns("Version", "Branch", "Type", "Comment")
        self.load_filters()

    @work(exclusive=True)
    async def load_filters(self) -> None:
        api: VersionAPI = self.app.version_api
        soc = self.controller.soc
        status = self.query_one("#fw-status", Static)
        try:
            branches = await api.branches(soc)
        except Exception as exc:  # noqa: BLE001
            status.update(f"Failed to load branches for {soc}: {exc}")
            return
        if not branches:
            status.update(f"No firmware found for SOC '{soc}'.")
            return

        branch_select = self.query_one("#branch-select", Select)
        branch_select.set_options([(b, b) for b in branches])
        default_branch = self._guess_branch(branches)
        branch_select.value = default_branch
        await self._load_types(default_branch)

    def _guess_branch(self, branches: list[str]) -> str:
        if self.controller.branch and self.controller.branch in branches:
            return self.controller.branch
        cur = self.controller.version.lower()
        for b in branches:
            if b.lower() in cur:
                return b
        return branches[0]

    async def _load_types(self, branch: str) -> None:
        api: VersionAPI = self.app.version_api
        status = self.query_one("#fw-status", Static)
        try:
            types = await api.types(self.controller.soc, branch)
        except Exception as exc:  # noqa: BLE001
            status.update(f"Failed to load types: {exc}")
            return
        type_select = self.query_one("#type-select", Select)
        type_select.set_options([(t, t) for t in types])
        if types:
            default_type = (
                self.controller.build_type
                if self.controller.build_type in types
                else types[0]
            )
            type_select.value = default_type
            self.load_versions()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.value is Select.BLANK:
            return
        if event.select.id == "branch-select":
            self.run_worker(self._load_types(event.value))
        elif event.select.id == "type-select":
            self.load_versions()

    @work(exclusive=True)
    async def load_versions(self) -> None:
        api: VersionAPI = self.app.version_api
        status = self.query_one("#fw-status", Static)
        branch = self.query_one("#branch-select", Select).value
        ftype = self.query_one("#type-select", Select).value
        if branch is Select.BLANK or ftype is Select.BLANK:
            return
        try:
            versions = await api.versions(self.controller.soc, branch, ftype)
        except Exception as exc:  # noqa: BLE001
            status.update(f"Failed to load versions: {exc}")
            return
        self._versions = versions
        table = self.query_one("#versions", DataTable)
        table.clear()
        for idx, entry in enumerate(versions):
            marker = "  ← current" if entry.get("version") == self.controller.version else ""
            table.add_row(
                (entry.get("version", "") + marker),
                entry.get("branch", ""),
                entry.get("type", ""),
                entry.get("comment", ""),
                key=str(idx),
            )
        if versions:
            status.update(f"{len(versions)} version(s). Enter selects a version to flash.")
        else:
            status.update("No versions for this filter combination.")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        try:
            idx = int(event.row_key.value)
        except (TypeError, ValueError):
            return
        entry = self._versions[idx]
        if not entry.get("url"):
            self.query_one("#fw-status", Static).update(
                "Selected version has no ROM url — cannot flash."
            )
            return
        self.app.push_screen(ConfirmScreen(self.controller, entry))

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()


# ---------------------------------------------------------------------------
# Screen 3: confirm + run update
# ---------------------------------------------------------------------------

class ConfirmScreen(Screen):
    BINDINGS = [
        ("escape", "back", "Back"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, controller: Controller, entry: dict) -> None:
        super().__init__()
        self.controller = controller
        self.entry = entry
        self._updating = False
        self._ws_worker = None
        self._update_worker = None

    def on_mount(self) -> None:
        # Defensive: a freshly pushed screen must never inherit a stuck state.
        self._updating = False
        self._update_worker = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            f"Update [b]{self.controller.name}[/b] ({self.controller.base_url})\n"
            f"  {self.controller.version or '?'}  →  "
            f"[b]{self.entry.get('version', '?')}[/b]  "
            f"({self.entry.get('branch', '')}/{self.entry.get('type', '')})\n"
            f"  ROM: {self.entry.get('url', '')}",
            id="confirm-summary",
        )
        with Horizontal(id="pw-bar"):
            yield Label("OTA password:")
            yield Input(
                password=True,
                id="password",
                value=os.environ.get("RGBWW_API_PASSWORD", ""),
            )
        with Horizontal(id="action-bar"):
            yield Button("Start update", id="start-btn", variant="success")
            yield Button("Cancel", id="cancel-btn", variant="error")
        yield RichLog(id="log", highlight=False, markup=True)
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.app.pop_screen()
        elif event.button.id == "start-btn":
            self._start()

    def _start(self) -> None:
        log = self.query_one("#log", RichLog)
        # Only block if the previous update worker is genuinely still running.
        worker = self._update_worker
        if worker is not None and worker.is_running:
            log.write("[yellow]Update already running — ignoring.[/yellow]")
            return
        password = self.query_one("#password", Input).value
        # An empty password is valid for controllers that don't have apiSecured
        # enabled; the request is then sent without Basic auth. If the controller
        # does require one, it replies 401 and run_update reports that.
        if not password:
            log.write("[dim]No password entered — trying without authentication.[/dim]")
        self._updating = True
        self.query_one("#start-btn", Button).disabled = True
        log.write("[cyan]── Start update requested ──[/cyan]")
        log.write(f"Target : {self.controller.name} ({self.controller.base_url})")
        log.write(f"From   : {self.controller.version or '?'}")
        log.write(f"To     : {self.entry.get('version', '?')} "
                  f"({self.entry.get('branch', '')}/{self.entry.get('type', '')})")
        log.write(f"ROM    : {self.entry.get('url', '')}")
        # Best-effort live broadcast streaming from the controller websocket.
        self._ws_worker = self.stream_ws()
        self._update_worker = self.run_update(password)

    @work(exclusive=False)
    async def stream_ws(self) -> None:
        """Connect to the controller /ws and stream OTA broadcasts into the log."""
        log = self.query_one("#log", RichLog)
        if websockets is None:
            log.write("[dim]ws: streaming unavailable (pip install websockets)[/dim]")
            return
        url = self.controller.ws_url
        log.write(f"[dim]ws: connecting {url}[/dim]")
        try:
            async with websockets.connect(url, open_timeout=5) as sock:
                log.write("[dim]ws: connected[/dim]")
                async for raw in sock:
                    self._log_ws_frame(log, raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # A dropped connection is expected once the controller reboots.
            log.write(f"[dim]ws: closed ({exc})[/dim]")

    def _log_ws_frame(self, log: RichLog, raw) -> None:
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            return
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "ota_status":
            status = params.get("status")
            text = params.get("message", "")
            label = OTA_STATUS.get(status, status)
            line = f"[magenta]ws ota_status[/magenta]: {label}"
            if text:
                line += f" — {text}"
            log.write(line)
        elif method == "notification":
            text = params.get("message", params)
            log.write(f"[magenta]ws notification[/magenta]: {text}")
        else:
            log.write(f"[dim]ws {method}: {params}[/dim]")

    @work(exclusive=True)
    async def run_update(self, password: str) -> None:
        log = self.query_one("#log", RichLog)
        base = self.controller.base_url
        rom_url = self.entry.get("url", "")

        log.write(f"[cyan]POST {base}/update[/cyan]"
                  + (" (auth user=admin)" if password else " (no auth)"))
        log.write(f'[dim]body: {{"rom": {{"url": "{rom_url}"}}}}[/dim]')
        try:
            result = await ControllerAPI.start_update(base, rom_url, password)
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 401:
                if password:
                    log.write("[red]Authentication failed (wrong OTA password).[/red]")
                else:
                    log.write("[red]This controller requires a password — "
                              "enter it above and try again.[/red]")
            elif code == 409:
                log.write("[yellow]An update is already in progress.[/yellow]")
            else:
                log.write(f"[red]Update rejected (HTTP {code}).[/red]")
            self._finish(False)
            return
        except Exception as exc:  # noqa: BLE001
            log.write(f"[red]Could not reach controller: {exc}[/red]")
            self._finish(False)
            return

        log.write(f"[dim]response: {result}[/dim]")
        if result.get("code") not in (0, None):
            log.write(f"[red]Controller error: {result}[/red]")
            self._finish(False)
            return

        log.write("[green]Update accepted. Downloading & flashing…[/green]")
        await self._poll(log, base)

    async def _poll(self, log: RichLog, base: str) -> None:
        last = None
        unreachable = 0
        for i in range(120):  # ~2 min watchdog window
            await asyncio.sleep(2)
            try:
                status = await ControllerAPI.update_status(base)
            except Exception:  # noqa: BLE001
                unreachable += 1
                log.write(f"[dim]poll {i + 1}: no response ({unreachable}/3)[/dim]")
                if unreachable >= 3:
                    log.write(
                        "[green]Controller stopped responding — most likely "
                        "rebooting into the new firmware.[/green]"
                    )
                    self._finish(True)
                    return
                continue
            unreachable = 0
            code = status.get("status")
            if code != last:
                log.write(f"status: {OTA_STATUS.get(code, code)}")
                last = code
            else:
                log.write(f"[dim]poll {i + 1}: {OTA_STATUS.get(code, code)}[/dim]")
            if code in OTA_TERMINAL_OK:
                log.write("[green]Update succeeded — controller is rebooting.[/green]")
                self._finish(True)
                return
            if code in OTA_TERMINAL_FAIL:
                log.write("[red]Update failed on the controller.[/red]")
                self._finish(False)
                return
        log.write("[yellow]Timed out waiting for completion.[/yellow]")
        self._finish(False)

    def _finish(self, ok: bool) -> None:
        self._updating = False
        self._update_worker = None
        if self._ws_worker is not None:
            try:
                self._ws_worker.cancel()
            except Exception:  # noqa: BLE001
                pass
            self._ws_worker = None
        try:
            self.query_one("#start-btn", Button).disabled = False
        except Exception:  # noqa: BLE001
            pass

    def action_back(self) -> None:
        if not self._updating:
            self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()


# ---------------------------------------------------------------------------
# Screen: confirm + run controller reset
# ---------------------------------------------------------------------------

class ResetScreen(Screen):
    BINDINGS = [
        ("escape", "back", "Back"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, controller: Controller) -> None:
        super().__init__()
        self.controller = controller
        self._busy = False
        self._worker = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            f"Reset [b]{self.controller.name}[/b] ({self.controller.base_url})\n"
            f"  Firmware: {self.controller.version or '?'}\n\n"
            f"This reboots the controller. Its outputs go dark for a few "
            f"seconds while it restarts.",
            id="confirm-summary",
        )
        with Horizontal(id="action-bar"):
            yield Button("Reset controller", id="reset-btn", variant="warning")
            yield Button("Cancel", id="cancel-btn", variant="error")
        yield RichLog(id="log", highlight=False, markup=True)
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel-btn":
            self.app.pop_screen()
        elif event.button.id == "reset-btn":
            self._start()

    def _start(self) -> None:
        worker = self._worker
        if worker is not None and worker.is_running:
            return
        self._busy = True
        self.query_one("#reset-btn", Button).disabled = True
        self._worker = self.run_reset()

    @work(exclusive=True)
    async def run_reset(self) -> None:
        log = self.query_one("#log", RichLog)
        base = self.controller.base_url
        log.write("[cyan]── Reset requested ──[/cyan]")
        log.write(f"[cyan]POST {base}/system[/cyan]")
        log.write('[dim]body: {"cmd": "restart"}[/dim]')
        try:
            result = await ControllerAPI.restart(base)
        except Exception as exc:  # noqa: BLE001
            log.write(f"[red]Could not reach controller: {exc}[/red]")
            self._finish()
            return
        log.write(f"[dim]response: {result}[/dim]")
        log.write("[green]Restart accepted — controller reboots in ~1.5 s.[/green]")
        await self._await_reboot(log, base)

    async def _await_reboot(self, log: RichLog, base: str) -> None:
        """Watch the controller drop off (rebooting) and come back online."""
        went_down = False
        for i in range(30):  # ~60 s window
            await asyncio.sleep(2)
            try:
                await ControllerAPI.info(base)
                reachable = True
            except Exception:  # noqa: BLE001
                reachable = False
            if not reachable and not went_down:
                went_down = True
                log.write("[green]Controller went offline — rebooting…[/green]")
            elif reachable and went_down:
                log.write("[green]Controller is back online.[/green]")
                self._finish()
                return
        if went_down:
            log.write("[yellow]Rebooted; not back online yet (still booting?).[/yellow]")
        else:
            log.write(
                "[yellow]Controller never went offline — reset may not have "
                "taken effect.[/yellow]"
            )
        self._finish()

    def _finish(self) -> None:
        self._busy = False
        self._worker = None
        try:
            self.query_one("#reset-btn", Button).disabled = False
        except Exception:  # noqa: BLE001
            pass

    def action_back(self) -> None:
        if not self._busy:
            self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class UpdaterApp(App):
    CSS = """
    Screen { background: #1e1e1e; }
    #title, #ctrl-summary, #confirm-summary { padding: 0 1; color: #e0e0e0; }
    #status, #fw-status { padding: 0 1; color: #9aa0a6; }
    #manual-bar, #filter-bar, #pw-bar, #action-bar { height: auto; padding: 0 1; }
    #manual-ip { width: 1fr; }
    #controllers, #versions { height: 1fr; border: solid #3c3c3c; }
    #log { height: 12; border: solid #3c3c3c; }
    Select { width: 24; }
    Label { padding: 1 1 0 0; }
    """
    TITLE = "Lightinator Firmware Updater"

    def __init__(self, api_base: str) -> None:
        super().__init__()
        self.version_api = VersionAPI(api_base)

    def on_mount(self) -> None:
        self.push_screen(ControllerScreen())


def main() -> None:
    parser = argparse.ArgumentParser(description="Lightinator firmware updater TUI")
    parser.add_argument(
        "--api",
        default=DEFAULT_API_BASE,
        help=f"Version API base URL (default: {DEFAULT_API_BASE})",
    )
    args = parser.parse_args()
    UpdaterApp(args.api).run()


if __name__ == "__main__":
    main()
