"""OpenClaw Desktop — native GUI for OpenClaw.

Window 1: Control UI (chat, settings, agents, devices, plugins, models...) in
          Edge WebView2 — never Chrome, so proxy extensions cannot break it.
Window 2: Toolkit (gateway control, doctor, backup, update, config editor,
          log viewer, CLI runner) — full management without a shell.

Auth strategy against the "enter token every launch" bug:
  * persistent WebView2 profile (private_mode=False + storage_path) so the
    durable device credential survives app restarts;
  * a fresh single-use pairing URL is fetched on every launch anyway;
  * if the page still lands on the Gateway-secret login gate, it is detected
    after load and automatically repaired with another fresh pairing URL.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import webbrowser

import webview
from webview.menu import Menu, MenuAction, MenuSeparator

import gw
from toolkit import ToolkitApi, create_toolkit

WINDOW_TITLE = "OpenClaw Desktop"
PROFILE_DIR = gw.Path.home() / ".openclaw" / "desktop-profile"

_state: dict = {"url": gw.GATEWAY_HTTP + "/", "window": None, "toolkit": None, "exiting": False}


# --------------------------------------------------------------------------- #
# pywebview noise patch (Control UI sends {type:"ready"} to chrome.webview)
# --------------------------------------------------------------------------- #
def _patch_host_message_noise() -> None:
    import webview.platforms.edgechromium as ec

    for name in dir(ec):
        obj = getattr(ec, name)
        if isinstance(obj, type) and hasattr(obj, "on_script_notify"):
            cls = obj
            orig = cls.on_script_notify

            def make(orig_fn):
                def patched(self, _, args):
                    try:
                        parsed = json.loads(args.get_WebMessageAsJson())
                    except Exception:
                        return None
                    if not (isinstance(parsed, list) and len(parsed) == 3):
                        return None
                    return orig_fn(self, _, args)

                return patched

            cls.on_script_notify = make(orig)
            break


# --------------------------------------------------------------------------- #
# JS bridge for the Control UI window
# --------------------------------------------------------------------------- #
class Api:
    def gateway_status(self) -> dict:
        alive = gw.gateway_alive()
        return {"alive": alive, "url": gw.GATEWAY_HTTP, "version": gw.gateway_version() if alive else ""}

    def restart_gateway(self) -> dict:
        return {"alive": gw.restart_gateway()}

    def open_external(self, url: str) -> None:
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            try:
                webbrowser.open(url)
            except Exception:
                pass

    def pair_url(self) -> str:
        """Fresh single-use pairing URL (used by the login-gate auto-repair)."""
        return gw.fresh_url()

    def open_toolkit(self) -> None:
        show_toolkit()


# --------------------------------------------------------------------------- #
# Window actions
# --------------------------------------------------------------------------- #
def load_ui() -> None:
    window = _state["window"]
    if window is None:
        return
    url = gw.fresh_url()
    _state["url"] = url
    try:
        window.load_url(url)
    except Exception:
        pass


def reload_ui() -> None:
    window = _state["window"]
    if window is None:
        return
    try:
        window.load_url(_state["url"])
    except Exception:
        pass


def do_start_gateway() -> None:
    threading.Thread(target=gw.ensure_gateway, daemon=True).start()


def _hide_console() -> None:
    """Hide the console window once the GUI is up (pythonw => no-op).

    Lets `python app.py` / .bat launches look native while still showing the
    console if the app dies before any window appears.
    """
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass


def do_restart_gateway() -> None:
    def _work():
        gw.restart_gateway()
        time.sleep(1)
        load_ui()

    threading.Thread(target=_work, daemon=True).start()


def show_toolkit() -> None:
    tk = _state.get("toolkit")
    if tk is None:
        return
    try:
        if tk.events.closed.is_set():
            return
        tk.show()
        tk.restore()
        tk.shown = True
    except Exception:
        pass


def open_logs() -> None:
    gw.open_explorer("logs")


def open_config() -> None:
    gw.open_explorer("config")


def build_menu() -> list[Menu]:
    return [
        Menu(
            "Control",
            [
                MenuAction("Toolkit (quản trị)      Ctrl+T", show_toolkit),
                MenuSeparator(),
                MenuAction("Pair again (fresh token)", load_ui),
                MenuAction("Reload", reload_ui),
                MenuSeparator(),
                MenuAction("Start Gateway", do_start_gateway),
                MenuAction("Restart Gateway", do_restart_gateway),
                MenuSeparator(),
            ],
        ),
        Menu(
            "System",
            [
                MenuAction("Open logs folder", open_logs),
                MenuAction("Open config file", open_config),
            ],
        ),
    ]


# --------------------------------------------------------------------------- #
# Injected JS
# --------------------------------------------------------------------------- #
INJECT_JS = r"""
window.__ocStatus = function (s) {
  try { document.title = (s.alive ? '' : 'offline - ') + 'OpenClaw Desktop'; } catch (e) {}
};
document.addEventListener('click', function (e) {
  var a = e.target && e.target.closest ? e.target.closest('a') : null;
  if (a && a.target === '_blank') {
    e.preventDefault();
    if (window.pywebview && window.pywebview.api) window.pywebview.api.open_external(a.href);
  }
}, true);

/* Ctrl+T => open Toolkit (pywebview menus have no real accelerators) */
document.addEventListener('keydown', function (e) {
  if (e.ctrlKey && !e.shiftKey && !e.altKey && (e.key === 't' || e.key === 'T')) {
    e.preventDefault();
    if (window.pywebview && window.pywebview.api) window.pywebview.api.open_toolkit();
  }
}, true);

/* --- login-gate auto repair ------------------------------------------- */
window.__ocGateTries = window.__ocGateTries || 0;
window.__ocGateCheck = function () {
  try {
    if (!document.body) return;
    var gate =
      document.querySelector('input[placeholder*="Paste the token" i]') ||
      document.querySelector('input[placeholder*="type the password" i]');
    var title = (document.querySelector('h1,h2') || {}).textContent || '';
    var isGate = !!gate && /expects its (token|password)/i.test(title + ' ' + document.body.innerText.slice(0, 400));
    if (isGate && window.__ocGateTries < 3 && window.pywebview && window.pywebview.api) {
      window.__ocGateTries++;
      window.pywebview.api.pair_url().then(function (u) {
        if (u) { window.location.replace(u); }
      });
    }
  } catch (e) {}
};
setTimeout(function () { window.__ocGateCheck(); }, 2500);
true;
"""


def _status_loop(window: webview.Window) -> None:
    while True:
        time.sleep(10)
        try:
            if _state["window"] is None or window.events.closed.is_set():
                return
            alive = gw.gateway_alive()
            window.evaluate_js(
                "window.__ocStatus && window.__ocStatus({alive: %s, url: '%s'})"
                % (str(alive).lower(), gw.GATEWAY_HTTP)
            )
        except Exception:
            if window.events.closed.is_set():
                return


def _on_loaded(window: webview.Window) -> None:
    try:
        window.evaluate_js(INJECT_JS)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    try:
        _patch_host_message_noise()
    except Exception:
        pass

    gw.ensure_gateway()
    threading.Thread(target=gw.ensure_omniroute, daemon=True).start()

    url = gw.fresh_url()
    _state["url"] = url

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    # --- Toolkit window (created hidden) ---
    toolkit_api = ToolkitApi()

    def _open_main_from_toolkit():
        w = _state["window"]
        if w is not None:
            try:
                w.show()
                w.restore()
            except Exception:
                pass

    toolkit = create_toolkit(toolkit_api, _open_main_from_toolkit)
    _state["toolkit"] = toolkit

    # close (X) on toolkit = hide, not destroy (unless app is exiting)
    def _tk_closing():
        if _state["exiting"]:
            return True  # allow real close during app shutdown
        try:
            toolkit.hide()
        except Exception:
            pass
        return False

    toolkit.events.closing += _tk_closing

    # --- Main Control UI window ---
    api = Api()
    window = webview.create_window(
        title=WINDOW_TITLE,
        url=url,
        width=1280,
        height=840,
        min_size=(720, 480),
        background_color="#0e1015",
        text_select=True,
        easy_drag=False,
        js_api=api,
        menu=build_menu(),
    )
    _state["window"] = window
    window.events.loaded += lambda: _on_loaded(window)
    window.events.shown += _hide_console

    def _main_closed():
        # closing the main window exits the app
        _state["exiting"] = True
        try:
            toolkit.events.closing -= _tk_closing
        except Exception:
            pass
        try:
            toolkit.destroy()
        except Exception:
            pass

    window.events.closed += _main_closed

    threading.Thread(target=_status_loop, args=(window,), daemon=True).start()

    # private_mode=False + storage_path => durable device identity & credentials
    webview.start(
        gui="edgechromium",
        debug=False,
        http_server=False,
        private_mode=False,
        storage_path=str(PROFILE_DIR),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
