"""Toolkit window — full OpenClaw management without a shell.

A native pywebview window exposing Gateway control, doctor, backup, update,
config editing, log viewer and a safe CLI runner. All commands run hidden
(no console window) and stream output into the page.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import gw

TOOLKIT_TITLE = "OpenClaw Toolkit"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ToolkitApi:
    """Exposed to the Toolkit page as window.pywebview.api.*"""

    def __init__(self):
        self._window = None
        self._open_main = None

    def open_main(self) -> bool:
        cb = self._open_main
        if cb:
            cb()
            return True
        return False

    # ---------------- Gateway ---------------- #
    def gateway_status(self) -> dict:
        alive = gw.gateway_alive()
        status: dict = {"alive": alive, "version": gw.gateway_version() if alive else ""}
        try:
            status["omniroute"] = bool(gw.omniroute_alive())
        except Exception:
            status["omniroute"] = False
        if alive:
            try:
                proc = gw.run_cli(["gateway", "status", "--json"], timeout=30)
                raw = (proc.stdout or "").strip()
                start, end = raw.find("{"), raw.rfind("}")
                if start >= 0 and end > start:
                    import json

                    data = json.loads(raw[start : end + 1])
                    status["service"] = data.get("service", {}).get("loadedText", "")
                    status["logFile"] = data.get("logFile", "")
            except Exception:
                pass
        return status

    def gw_start(self) -> dict:
        gw.run_cli(["gateway", "start", "--yes"], timeout=90)
        return self.gateway_status()

    def gw_stop(self) -> dict:
        gw.run_cli(["gateway", "stop"], timeout=60)
        return self.gateway_status()

    def gw_restart(self) -> dict:
        gw.restart_gateway()
        return self.gateway_status()

    # ---------------- Jobs (streaming CLI) ---------------- #
    def job_start(self, args: list) -> dict:
        argv = ["openclaw"] + [str(a) for a in (args or [])]
        jid = gw.start_job(argv)
        if jid is None:
            return {"job": None, "error": "refused"}
        return {"job": jid}

    def job_poll(self, jid: str, since: int) -> dict:
        return gw.poll_job(str(jid), int(since or 0))

    # ---------------- Presets ---------------- #
    def op_doctor(self, fix: bool) -> dict:
        args = ["doctor", "--json"] + (["--fix"] if fix else [])
        jid = gw.start_job(["openclaw", *args])
        return {"job": jid}

    def op_backup(self) -> dict:
        jid = gw.start_job(["openclaw", "backup", "create", "--verify", "--json"])
        return {"job": jid}

    def op_update_status(self) -> dict:
        jid = gw.start_job(["openclaw", "update", "status", "--json"])
        return {"job": jid}

    def op_update_apply(self) -> dict:
        jid = gw.start_job(["openclaw", "update", "--yes", "--no-restart"])
        return {"job": jid}

    def op_validate_config(self) -> dict:
        jid = gw.start_job(["openclaw", "config", "validate", "--json"])
        return {"job": jid}

    # ---------------- Config ---------------- #
    def config_read(self) -> dict:
        try:
            return {"ok": True, "text": gw.read_config_text(), "path": str(gw.CONFIG_PATH)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def config_write(self, text: str) -> dict:
        return gw.save_config_text(str(text))

    def config_schema(self) -> dict:
        try:
            proc = gw.run_cli(["config", "schema"], timeout=30)
            return {"ok": True, "text": (proc.stdout or "")[:500000]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---------------- Agents CRUD ---------------- #
    def agents_list(self) -> dict:
        try:
            proc = gw.run_cli(["agents", "list", "--json"], timeout=60)
            data = gw.parse_json(proc.stdout)
            if isinstance(data, list):
                return {"ok": True, "agents": data}
            return {"ok": False, "error": (proc.stdout or proc.stderr or "no output")[:1500]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def agent_add(self, name: str, workspace: str = "", model: str = "", role: str = "") -> dict:
        name = str(name or "").strip()
        if not _ID_RE.match(name):
            return {"ok": False, "error": "ID không hợp lệ (chữ, số, dấu - _ . , bắt đầu bằng chữ/số)."}
        args = ["agents", "add", name, "--non-interactive", "--json"]
        ws = str(workspace or "").strip()
        if ws:
            p = Path(ws).expanduser()
            if not p.is_absolute():
                p = Path.home() / p
            if p.exists() and (not p.is_dir() or any(p.iterdir())):
                return {"ok": False, "error": f"Thư mục '{p}' đã tồn tại và không rỗng — chọn thư mục khác."}
            ws = str(p)
        else:
            ws = str(gw.WORKSPACE / name)
        args += ["--workspace", ws]
        mdl = str(model or "").strip()
        if mdl:
            args += ["--model", mdl]
        rl = str(role or "").strip()
        if rl:
            args += ["--role", rl]
        try:
            proc = gw.run_cli(args, timeout=120)
            data = gw.parse_json(proc.stdout)
            if proc.returncode == 0 and data:
                return {"ok": True, "data": data}
            return {"ok": False, "error": ((proc.stdout or "") + (proc.stderr or "")).strip()[:1500]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def agent_delete(self, name: str) -> dict:
        name = str(name or "").strip()
        if not _ID_RE.match(name):
            return {"ok": False, "error": "ID không hợp lệ."}
        if name == "main":
            return {"ok": False, "error": "Không được xóa agent 'main'."}
        try:
            proc = gw.run_cli(["agents", "delete", name, "--force", "--json"], timeout=120)
            data = gw.parse_json(proc.stdout)
            if proc.returncode == 0 and data:
                return {"ok": True, "data": data}
            err = ((proc.stdout or "") + (proc.stderr or "")).strip()
            if "safe integer range" in err:
                return self._agent_delete_fallback(name)
            return {"ok": False, "error": err[:1500]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _agent_delete_fallback(self, name: str) -> dict:
        """Windows bug in OpenClaw: trash identity check throws when a file id
        exceeds Number.MAX_SAFE_INTEGER, so `agents delete` can never finish.
        Remove the config entry and OpenClaw-owned dirs directly instead."""
        try:
            cfg = gw.read_config()
            agents = cfg.get("agents") or {}
            entries = agents.get("entries") or {}
            entry = entries.pop(name, None)
            holders = [agents.get("defaults") or {}] + [e for e in entries.values() if isinstance(e, dict)]
            for holder in holders:
                allow = ((holder.get("subagents") or {}).get("allowAgents")) if isinstance(holder.get("subagents"), dict) else None
                if isinstance(allow, list) and name in allow:
                    allow[:] = [a for a in allow if a != name]
            res = gw.write_config(cfg)
            if not res.get("ok"):
                return res
            kept = None
            if isinstance(entry, dict):
                ad = str(entry.get("agentDir") or "")
                if ad:
                    shutil.rmtree(Path(ad).parent, ignore_errors=True)
                ws = str(entry.get("workspace") or "")
                if ws and Path(ws).resolve() == (gw.WORKSPACE / name).resolve():
                    shutil.rmtree(ws, ignore_errors=True)
                elif ws and Path(ws).exists():
                    kept = ws
            return {"ok": True, "data": {"agentId": name, "fallback": True, "keptWorkspace": kept}}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def agent_identity(self, name: str, display: str = "", emoji: str = "", theme: str = "") -> dict:
        name = str(name or "").strip()
        if not _ID_RE.match(name):
            return {"ok": False, "error": "ID không hợp lệ."}
        args = ["agents", "set-identity", "--agent", name, "--json"]
        has = False
        if str(display or "").strip():
            args += ["--name", str(display).strip()]
            has = True
        if str(emoji or "").strip():
            args += ["--emoji", str(emoji).strip()]
            has = True
        if str(theme or "").strip():
            args += ["--theme", str(theme).strip()]
            has = True
        if not has:
            return {"ok": False, "error": "Nhập ít nhất một trong: tên, emoji, theme."}
        try:
            proc = gw.run_cli(args, timeout=60)
            data = gw.parse_json(proc.stdout)
            if proc.returncode == 0 and data:
                return {"ok": True, "data": data}
            return {"ok": False, "error": ((proc.stdout or "") + (proc.stderr or "")).strip()[:1500]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def agent_set_model(self, name: str, model: str) -> dict:
        """Set default model for one agent (agents.entries.<id>.model)."""
        name = str(name or "").strip()
        model = str(model or "").strip()
        if not _ID_RE.match(name):
            return {"ok": False, "error": "ID không hợp lệ."}
        if not model:
            return {"ok": False, "error": "Chưa nhập model."}
        try:
            cfg = gw.read_config()
            entries = cfg.setdefault("agents", {}).setdefault("entries", {})
            entry = entries.get(name)
            if entry is None:
                entry = {"workspace": str(gw.WORKSPACE / name)}
                entries[name] = entry
            entry["model"] = model
            return gw.write_config(cfg)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---------------- Orchestration (phân công điều phối) ---------------- #
    @staticmethod
    def _subagents_cfg(entry) -> dict:
        sub = entry.get("subagents") if isinstance(entry, dict) else None
        return sub if isinstance(sub, dict) else {}

    @staticmethod
    def _allow_list(v) -> list | None:
        if isinstance(v, list):
            return [str(x) for x in v]
        return None

    def orch_get(self) -> dict:
        """Đọc cấu hình điều phối: agents.*.subagents + defaults (cho ma trận)."""
        try:
            cfg = gw.read_config()
            agents = cfg.get("agents") or {}
            defaults = agents.get("defaults") or {}
            entries = agents.get("entries") or {}
            dsub = self._subagents_cfg(defaults)
            d_allow = self._allow_list(dsub.get("allowAgents")) or []
            out = []
            for aid, raw in entries.items():
                e = raw if isinstance(raw, dict) else {}
                sub = self._subagents_cfg(e)
                ident = e.get("identity") if isinstance(e.get("identity"), dict) else {}
                own = self._allow_list(sub.get("allowAgents"))
                req = sub.get("requireAgentId")
                out.append({
                    "id": aid,
                    "name": str(ident.get("name") or ""),
                    "emoji": str(ident.get("emoji") or ""),
                    "model": self._model_str(e.get("model")) or self._model_str(defaults.get("model")),
                    "allow": own if own is not None else d_allow,
                    "allowRaw": own,
                    "mode": str(sub.get("delegationMode") or dsub.get("delegationMode") or ""),
                    "modeRaw": str(sub.get("delegationMode") or ""),
                    "req": req if isinstance(req, bool) else None,
                })
            req_d = dsub.get("requireAgentId")
            return {
                "ok": True,
                "ids": list(entries.keys()),
                "agents": out,
                "defaults": {
                    "allow": d_allow,
                    "mode": str(dsub.get("delegationMode") or ""),
                    "req": req_d if isinstance(req_d, bool) else None,
                },
                "systemAgent": str((((defaults.get("systemAgent") or {}).get("agentId")) or "")),
                "talkAgent": str((((cfg.get("talk") or {}).get("agentId")) or "")),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def orch_save(self, scope: str, agent_id: str, allow_json: str,
                  mode: str = "", req=None) -> dict:
        """Ghi cấu hình phân công điều phối.

        scope "defaults" → agents.defaults.subagents; scope "agent" → entry.
        allow_json: JSON mảng, vd '["*"]', '["ops-writer"]', '[]' (chỉ tự mình).
        mode: "" = bỏ trừ (kế thừa), "suggest" | "prefer".
        req: None = giữ nguyên, True/False = requireAgentId.
        """
        scope = str(scope or "").strip()
        agent_id = str(agent_id or "").strip()
        mode = str(mode or "").strip()
        if mode and mode not in ("suggest", "prefer"):
            return {"ok": False, "error": "delegationMode phải là 'suggest' hoặc 'prefer'."}
        try:
            allow = json.loads(str(allow_json or "[]"))
        except Exception as exc:
            return {"ok": False, "error": f"Danh sách không phải JSON hợp lệ: {exc}"}
        if not isinstance(allow, list) or any(not isinstance(a, str) for a in allow):
            return {"ok": False, "error": 'Danh sách phải là mảng chuỗi, vd ["ops-writer"].'}
        try:
            cfg = gw.read_config()
            agents = cfg.setdefault("agents", {})
            entries = agents.get("entries") or {}
            known = set(entries.keys())
            clean: list[str] = []
            for a in allow:
                a = a.strip()
                if not a:
                    continue
                if a == "*":
                    if "*" not in clean:
                        clean.append("*")
                    continue
                if a not in known:
                    return {"ok": False, "error": f"Agent '{a}' không tồn tại."}
                if a not in clean:
                    clean.append(a)
            if scope == "defaults":
                holder = agents.setdefault("defaults", {})
            elif scope == "agent":
                if agent_id not in known:
                    return {"ok": False, "error": f"Không có agent '{agent_id}'."}
                holder = entries[agent_id]
                if not isinstance(holder, dict):
                    holder = entries[agent_id] = {}
            else:
                return {"ok": False, "error": "scope phải là 'defaults' hoặc 'agent'."}
            sub = holder.get("subagents")
            if not isinstance(sub, dict):
                sub = {}
                holder["subagents"] = sub
            sub["allowAgents"] = clean
            if mode:
                sub["delegationMode"] = mode
            else:
                sub.pop("delegationMode", None)
            if isinstance(req, bool):
                sub["requireAgentId"] = req
            res = gw.write_config(cfg)
            if isinstance(res, dict) and res.get("ok"):
                res["applied"] = {"scope": scope, "agent": agent_id,
                                  "allow": clean, "mode": mode}
            return res
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---------------- Providers / models ---------------- #
    @staticmethod
    def _model_str(v) -> str:
        """agents.defaults.model may be a string or {primary: ..., fallbacks: [...]}."""
        if isinstance(v, str):
            return v
        if isinstance(v, dict):
            return str(v.get("primary") or v.get("model") or "")
        return ""

    def providers_get(self) -> dict:
        try:
            cfg = gw.read_config()
            models_cfg = cfg.get("models") or {}
            defaults = (cfg.get("agents") or {}).get("defaults") or {}
            return {
                "ok": True,
                "providers": models_cfg.get("providers") or {},
                "aliases": models_cfg.get("aliases") or {},
                "mode": models_cfg.get("mode", ""),
                "defaultModel": self._model_str(defaults.get("model")),
                "defaultModels": defaults.get("models") or {},
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def provider_save(self, pid: str, obj_json: str) -> dict:
        pid = str(pid or "").strip()
        if not _ID_RE.match(pid):
            return {"ok": False, "error": "Provider ID không hợp lệ."}
        try:
            obj = json.loads(str(obj_json))
        except Exception as exc:
            return {"ok": False, "error": f"JSON không hợp lệ: {exc}"}
        if not isinstance(obj, dict):
            return {"ok": False, "error": "Provider phải là object JSON."}
        base = str(obj.get("baseUrl") or "").strip()
        if not base.startswith(("http://", "https://")):
            return {"ok": False, "error": "baseUrl phải bắt đầu bằng http:// hoặc https://"}
        models_list = obj.get("models")
        if models_list is None:
            obj["models"] = []
        elif not isinstance(models_list, list):
            return {"ok": False, "error": "Trường 'models' phải là mảng []."}
        try:
            cfg = gw.read_config()
            providers = cfg.setdefault("models", {}).setdefault("providers", {})
            providers[pid] = obj
            return gw.write_config(cfg)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def provider_delete(self, pid: str) -> dict:
        pid = str(pid or "").strip()
        if not _ID_RE.match(pid):
            return {"ok": False, "error": "Provider ID không hợp lệ."}
        try:
            cfg = gw.read_config()
            providers = cfg.setdefault("models", {}).setdefault("providers", {})
            if pid not in providers:
                return {"ok": False, "error": f"Không có provider '{pid}'."}
            del providers[pid]
            return gw.write_config(cfg)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _model_agent_args(self) -> list[str]:
        """Explicit --agent scope for `models auth`/`models status`.

        With multiple agents configured, `models auth logout` (write mode)
        fails with "Multiple agents are configured, but the model command has
        no explicit owner". Scope every models auth/status call to the same
        ambient owner the plain read commands resolve, so what the UI lists
        is exactly what logout can remove.
        """
        try:
            cfg = gw.read_config()
            agents = cfg.get("agents") or {}
            entries = agents.get("entries") or {}
            if len(entries) <= 1:
                return []
            sid = str((((agents.get("defaults") or {}).get("systemAgent") or {}).get("agentId")) or "").strip()
            owner = sid if sid in entries else ("main" if "main" in entries else next(iter(entries), None))
            return ["--agent", str(owner)] if owner else []
        except Exception:
            return []

    def model_set_default(self, model: str) -> dict:
        model = str(model or "").strip()
        if not model:
            return {"ok": False, "error": "Chưa nhập model."}
        try:
            proc = gw.run_cli(["models", "set", model], timeout=60)
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            if proc.returncode == 0:
                return {"ok": True, "text": out[:1000]}
            return {"ok": False, "error": out[:1500]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def models_configured(self) -> dict:
        """List model ids available from configured providers (for dropdowns)."""
        try:
            cfg = gw.read_config()
            providers = ((cfg.get("models") or {}).get("providers")) or {}
            out = []
            for pid, obj in providers.items():
                for m in (obj or {}).get("models") or []:
                    mid = str(m.get("id") or "").strip()
                    if mid:
                        out.append(f"{pid}/{mid}")
            return {"ok": True, "models": out}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def auth_list(self) -> dict:
        try:
            proc = gw.run_cli(["models", "auth", "list", "--json"] + self._model_agent_args(), timeout=60)
            data = gw.parse_json(proc.stdout)
            if isinstance(data, dict):
                return {"ok": True, "profiles": data.get("profiles") or [], "agentId": data.get("agentId", "")}
            return {"ok": False, "error": (proc.stdout or proc.stderr or "no output")[:1500]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def auth_logout(self, profile_id: str) -> dict:
        profile_id = str(profile_id or "").strip()
        if not profile_id or ":" not in profile_id:
            return {"ok": False, "error": "Profile ID không hợp lệ (dạng provider:profile)."}
        try:
            proc = gw.run_cli(["models", "auth", "logout", profile_id, "--yes"] + self._model_agent_args(), timeout=60)
            out = ((proc.stdout or "") + (proc.stderr or "")).strip()
            if proc.returncode == 0:
                return {"ok": True, "text": out[:1000]}
            return {"ok": False, "error": out[:1500]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def models_status(self) -> dict:
        try:
            proc = gw.run_cli(["models", "status", "--json"] + self._model_agent_args(), timeout=60)
            data = gw.parse_json(proc.stdout)
            if isinstance(data, dict):
                auth = data.get("auth") or {}
                return {
                    "ok": True,
                    "defaultModel": data.get("resolvedDefault") or data.get("defaultModel") or "",
                    "fallbacks": data.get("fallbacks") or [],
                    "aliases": data.get("aliases") or {},
                    "providers": [
                        {
                            "provider": p.get("provider", ""),
                            "count": (p.get("profiles") or {}).get("count", 0),
                            "labels": (p.get("profiles") or {}).get("labels") or [],
                        }
                        for p in auth.get("providers") or []
                    ],
                }
            return {"ok": False, "error": (proc.stdout or proc.stderr or "no output")[:1500]}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---------------- Logs ---------------- #
    def logs_list(self) -> list:
        return gw.list_logs()

    def logs_read(self, name: str, lines: int) -> dict:
        return {"ok": True, "text": gw.read_log_tail(str(name), int(lines or 300))}

    # ---------------- Explorer ---------------- #
    def open_path(self, target: str) -> bool:
        return gw.open_explorer(str(target))

    # ---------------- Health ---------------- #
    def health(self) -> dict:
        alive = gw.gateway_alive()
        return {"alive": alive, "version": gw.gateway_version(), "url": gw.GATEWAY_HTTP}


TOOLKIT_HTML = r"""
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<title>OpenClaw Toolkit</title>
<style>
  :root{
    --bg:#0a0d13; --panel:#121722; --panel2:#182031; --panel3:#1f2942;
    --sidebar:#0d1119; --border:#232c40; --border2:#2e3a58;
    --text:#e9edf7; --muted:#8d97b2; --faint:#5d6784;
    --brand:#ff6a3d; --brand2:#ff9061; --indigo:#6e7dff; --indigo2:#93a0ff;
    --ok:#3ddc97; --warn:#ffc857; --err:#ff5f57;
    --mono:"Cascadia Code",Consolas,"Courier New",monospace;
    --r:12px;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%}
  body{background:var(--bg);color:var(--text);font:14px/1.45 "Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;display:flex;overflow:hidden}

  /* ---------- sidebar ---------- */
  aside{width:238px;background:linear-gradient(180deg,#0f1420 0%,var(--sidebar) 100%);border-right:1px solid var(--border);display:flex;flex-direction:column;padding:16px 12px 12px;flex-shrink:0}
  .brand{display:flex;align-items:center;gap:10px;padding:2px 8px 16px;font-weight:700;font-size:15px;letter-spacing:.2px}
  .brand .logo{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,var(--brand),var(--brand2));display:flex;align-items:center;justify-content:center;font-size:16px;box-shadow:0 4px 14px rgba(255,106,61,.35)}
  .brand .dot{width:8px;height:8px;border-radius:50%;background:var(--err);margin-left:auto;transition:.3s}
  .brand .dot.on{background:var(--ok);box-shadow:0 0 8px var(--ok)}
  nav#nav{display:flex;flex-direction:column;gap:2px;overflow-y:auto;flex:1}
  nav .g{font-size:10px;text-transform:uppercase;letter-spacing:.14em;color:var(--faint);padding:14px 10px 6px;font-weight:600}
  nav button{background:none;border:0;color:var(--muted);text-align:left;padding:9px 12px;border-radius:9px;font-size:13.5px;cursor:pointer;display:flex;align-items:center;gap:10px;transition:.15s;font-family:inherit}
  nav button i{font-style:normal;width:17px;text-align:center;opacity:.85;font-size:13px}
  nav button:hover{background:var(--panel2);color:var(--text)}
  nav button.active{background:linear-gradient(90deg,rgba(255,106,61,.18),rgba(255,106,61,0));color:#fff;font-weight:600;box-shadow:inset 2px 0 0 var(--brand)}
  nav button.active i{opacity:1}
  .side-foot{border-top:1px solid var(--border);margin-top:10px;padding:10px 8px 2px;display:flex;flex-direction:column;gap:4px}
  .side-foot .st{font-size:12px;color:var(--muted);display:flex;align-items:center;gap:7px}
  .side-foot .st b{color:var(--text);font-weight:600}
  .side-foot .ver{font-size:11px;color:var(--faint);font-family:var(--mono)}

  /* ---------- main ---------- */
  main{flex:1;padding:24px 30px 40px;overflow-y:auto}
  .hd{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px;flex-wrap:wrap}
  h2{font-size:19px;font-weight:650;letter-spacing:.2px}
  .sub{color:var(--muted);font-size:12.5px;margin-top:4px;max-width:720px}
  .section{display:none}
  .section.active{display:block;animation:fade .18s ease}
  @keyframes fade{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}

  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(175px,1fr));gap:12px;margin-bottom:18px}
  .card{background:linear-gradient(180deg,#151b2b,var(--panel));border:1px solid var(--border);border-radius:14px;padding:14px 16px;position:relative;overflow:hidden}
  .card::after{content:"";position:absolute;inset:0 0 auto;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.06),transparent)}
  .card .k{color:var(--muted);font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;font-weight:600}
  .card .v{font-size:16.5px;font-weight:650;margin-top:7px;word-break:break-all}
  .card .v.sm{font-size:13px;font-weight:600}

  .row{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:16px;align-items:center}
  .box{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:16px 18px;margin-bottom:14px}
  .box h3{font-size:13.5px;font-weight:650;margin-bottom:12px;display:flex;align-items:center;gap:8px}
  .box h3 .tag{font-size:10px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--indigo2);background:rgba(110,125,255,.14);border:1px solid rgba(110,125,255,.3);padding:2px 8px;border-radius:20px}

  /* ---------- buttons / inputs ---------- */
  button.btn{background:var(--panel2);border:1px solid var(--border2);color:var(--text);padding:8px 15px;border-radius:9px;cursor:pointer;font-size:13px;transition:.15s;font-family:inherit;display:inline-flex;align-items:center;gap:7px}
  button.btn:hover{border-color:var(--indigo);background:var(--panel3);color:#fff}
  button.btn.primary{background:linear-gradient(135deg,var(--brand),var(--brand2));border-color:transparent;color:#fff;font-weight:600;box-shadow:0 4px 14px rgba(255,106,61,.28)}
  button.btn.primary:hover{filter:brightness(1.08);border-color:transparent}
  button.btn.ghost{background:transparent}
  button.btn.danger{background:rgba(255,95,87,.12);border-color:rgba(255,95,87,.4);color:#ff8a83}
  button.btn.danger:hover{background:rgba(255,95,87,.2);border-color:var(--err);color:#ffb0aa}
  button.btn:disabled{opacity:.45;cursor:default}
  button.btn.mini{padding:5px 11px;font-size:12.5px;border-radius:8px}
  select,input[type=text],input[type=number]{background:#0e1421;border:1px solid var(--border2);color:var(--text);padding:8px 11px;border-radius:9px;font-size:13px;font-family:inherit;transition:.15s}
  select:focus,input[type=text]:focus,textarea:focus{outline:none;border-color:var(--indigo);box-shadow:0 0 0 3px rgba(110,125,255,.18)}
  input[type=text]{min-width:320px}
  label.fld{display:flex;align-items:center;gap:8px;color:var(--muted);font-size:12.5px}
  label.fld input,label.fld select{min-width:0}
  input[type=checkbox]{accent-color:var(--brand);width:14px;height:14px;cursor:pointer}

  .msg{padding:10px 14px;border-radius:10px;margin-bottom:14px;font-size:13px;display:none;border:1px solid transparent}
  .msg.ok{display:block;background:rgba(61,220,151,.09);color:var(--ok);border-color:rgba(61,220,151,.3)}
  .msg.err{display:block;background:rgba(255,95,87,.1);color:#ff9a93;border-color:rgba(255,95,87,.35)}

  .badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:650;letter-spacing:.02em}
  .badge.ok{background:rgba(61,220,151,.13);color:var(--ok)}
  .badge.err{background:rgba(255,95,87,.13);color:#ff8a83}
  .badge.warn{background:rgba(255,200,87,.12);color:var(--warn)}
  .badge.info{background:rgba(110,125,255,.14);color:var(--indigo2)}

  .list{display:flex;flex-direction:column;gap:9px}
  .item{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:11px 14px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;transition:.15s}
  .item:hover{border-color:var(--border2);background:#141a2a}
  .item .ava{width:32px;height:32px;border-radius:10px;background:var(--panel3);border:1px solid var(--border2);display:flex;align-items:center;justify-content:center;font-size:16px;flex-shrink:0}
  .item .id{font-family:var(--mono);font-weight:700;color:var(--indigo2);min-width:120px;font-size:13px}
  .item .meta{color:var(--muted);font-size:12.5px}
  .item .sp{flex:1}
  .item .em{font-size:17px}
  .chip{display:inline-block;background:var(--panel2);border:1px solid var(--border2);color:var(--muted);padding:2px 9px;border-radius:20px;font-size:11.5px;font-family:var(--mono)}
  code{font-family:var(--mono);background:var(--panel2);border:1px solid var(--border);padding:1px 6px;border-radius:6px;font-size:12px;color:var(--indigo2)}
  .hint{color:var(--faint);font-size:12px;margin-top:9px;line-height:1.5}

  pre.out{background:#080b11;border:1px solid var(--border);border-radius:12px;padding:14px 16px;font:12.5px/1.55 var(--mono);white-space:pre-wrap;word-break:break-all;max-height:55vh;overflow-y:auto;color:#c2cadd}
  pre.out:empty::before{content:"(chưa có đầu ra)";color:var(--faint)}
  textarea.cfg{width:100%;height:52vh;background:#080b11;border:1px solid var(--border);border-radius:12px;color:#c2cadd;font:12.5px/1.5 var(--mono);padding:14px 16px;resize:vertical;tab-size:2}
  ::-webkit-scrollbar{width:10px;height:10px}
  ::-webkit-scrollbar-thumb{background:#27314c;border-radius:8px;border:2px solid var(--bg)}
  ::-webkit-scrollbar-thumb:hover{background:#334066}
  ::-webkit-scrollbar-track{background:transparent}

  .cmdrow{display:flex;gap:9px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
  .cmdrow .prefix{font-family:var(--mono);color:var(--brand);font-weight:700}
  .quick{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
  .quick button{background:var(--panel2);border:1px dashed var(--border2);color:var(--muted);padding:6px 13px;border-radius:20px;font-size:12.5px;cursor:pointer;font-family:var(--mono);transition:.15s}
  .quick button:hover{color:var(--text);border-color:var(--indigo);border-style:solid}
  .spin{display:inline-block;width:13px;height:13px;border:2px solid #fff4;border-top-color:#fff;border-radius:50%;animation:sp .8s linear infinite;vertical-align:-2px}
  @keyframes sp{to{transform:rotate(360deg)}}

  /* ---------- orchestration matrix ---------- */
  .matrix-wrap{overflow-x:auto;padding-bottom:4px}
  table.matrix{border-collapse:separate;border-spacing:5px;margin:-5px}
  table.matrix th.rh{text-align:left;font-size:12.5px;font-family:var(--mono);color:var(--indigo2);font-weight:650;padding:6px 12px 6px 6px;white-space:nowrap;cursor:pointer;border-radius:8px}
  table.matrix th.rh:hover{background:var(--panel2)}
  table.matrix thead th{font-size:11px;color:var(--muted);font-weight:600;background:none;writing-mode:vertical-rl;transform:rotate(180deg);padding:8px 3px;max-width:34px;overflow:hidden;text-overflow:ellipsis;font-family:var(--mono)}
  table.matrix td.cell{text-align:center;width:44px;height:36px;background:var(--panel2);border:1px solid var(--border);border-radius:9px;font-size:14px;cursor:pointer;transition:.12s}
  table.matrix td.cell.ok{color:var(--ok);background:rgba(61,220,151,.09);border-color:rgba(61,220,151,.28)}
  table.matrix td.cell.no{color:#39415c}
  table.matrix td.cell.self{color:var(--warn);background:rgba(255,200,87,.07);border-color:rgba(255,200,87,.22)}
  table.matrix td.cell:hover{transform:scale(1.08);border-color:var(--indigo);z-index:2;position:relative}
  .legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:12px}
  .legend span{display:inline-flex;align-items:center;gap:6px}
  .legend .ok{color:var(--ok)} .legend .self{color:var(--warn)} .legend .no{color:#39415c}

  .chips{display:flex;gap:8px;flex-wrap:wrap;margin:4px 0 12px}
  .chips label{display:inline-flex;align-items:center;gap:7px;background:var(--panel2);border:1px solid var(--border2);color:var(--muted);padding:7px 13px;border-radius:20px;font-size:12.5px;cursor:pointer;transition:.12s;font-family:var(--mono);user-select:none}
  .chips label:hover{border-color:var(--indigo);color:var(--text)}
  .chips label:has(input:checked){background:rgba(255,106,61,.14);border-color:rgba(255,106,61,.55);color:#ffd0bd}
  .chips label.star{border-style:dashed}
  .chips label:has(input:disabled){opacity:.4;cursor:default}
</style>
</head>
<body>
<aside>
  <div class="brand"><span class="logo">🐾</span> OpenClaw Toolkit <span class="dot" id="gdot"></span></div>
  <nav id="nav">
    <div class="g">Hệ thống</div>
    <button data-s="overview" class="active"><i>📊</i>Tổng quan</button>
    <button data-s="gateway"><i>🌐</i>Gateway</button>
    <div class="g">Quản trị agent</div>
    <button data-s="agents"><i>🤖</i>Agents</button>
    <button data-s="orch"><i>🕸️</i>Điều phối</button>
    <button data-s="providers"><i>🧩</i>Model &amp; Provider</button>
    <div class="g">Công cụ</div>
    <button data-s="config"><i>⚙️</i>Cấu hình</button>
    <button data-s="doctor"><i>🩺</i>Doctor</button>
    <button data-s="backup"><i>💾</i>Backup &amp; Update</button>
    <button data-s="logs"><i>📜</i>Nhật ký</button>
    <button data-s="cli"><i>⌨️</i>Lệnh CLI</button>
  </nav>
  <div class="side-foot">
    <div class="st" id="foot-st"><b>…</b></div>
    <div class="ver">v<span id="ver">…</span></div>
  </div>
</aside>
<main>

<!-- OVERVIEW -->
<section class="section active" id="s-overview">
  <div class="hd">
    <div><h2>Tổng quan</h2><div class="sub">Trạng thái hệ thống OpenClaw — tự làm mới khi bạn chuyển tab.</div></div>
    <div class="row" style="margin:0">
      <button class="btn" onclick="T.gateway_status().then(renderOv)">⟳ Làm mới</button>
      <button class="btn primary" onclick="openMain()">🚀 Mở Control UI</button>
    </div>
  </div>
  <div class="cards">
    <div class="card"><div class="k">Gateway</div><div class="v" id="ov-gw"><span class="badge warn">kiểm tra…</span></div></div>
    <div class="card"><div class="k">OmniRoute</div><div class="v" id="ov-omni"><span class="badge warn">kiểm tra…</span></div></div>
    <div class="card"><div class="k">Phiên bản</div><div class="v" id="ov-ver">…</div></div>
    <div class="card"><div class="k">Service</div><div class="v" id="ov-svc">…</div></div>
    <div class="card"><div class="k">Cổng</div><div class="v">18789</div></div>
  </div>
  <div class="box">
    <h3>Truy cập nhanh</h3>
    <div class="row" style="margin:0">
      <button class="btn" onclick="T.open_path('openclaw')">📁 ~/.openclaw</button>
      <button class="btn" onclick="T.open_path('workspace')">📁 Workspace</button>
      <button class="btn" onclick="T.open_path('logs')">📁 Thư mục log</button>
      <button class="btn" onclick="T.open_path('backups')">📁 Backup</button>
    </div>
  </div>
</section>

<!-- GATEWAY -->
<section class="section" id="s-gateway">
  <div class="hd">
    <div><h2>Gateway</h2><div class="sub">Khởi động / dừng / khởi động lại Gateway service — không cần lệnh terminal.</div></div>
  </div>
  <div class="msg" id="gw-msg"></div>
  <div class="row">
    <button class="btn primary" id="btn-start" onclick="gwAct('start', this)">▶ Start</button>
    <button class="btn" id="btn-restart" onclick="gwAct('restart', this)">⟳ Restart</button>
    <button class="btn danger" id="btn-stop" onclick="gwAct('stop', this)">■ Stop</button>
    <button class="btn ghost" onclick="T.gateway_status().then(renderGw)">Kiểm tra lại</button>
  </div>
  <div class="cards">
    <div class="card"><div class="k">Trạng thái</div><div class="v" id="gw-state">…</div></div>
    <div class="card"><div class="k">Service</div><div class="v" id="gw-svc">…</div></div>
    <div class="card"><div class="k">OmniRoute</div><div class="v" id="gw-omni">…</div></div>
    <div class="card"><div class="k">Endpoint</div><div class="v sm">ws://127.0.0.1:18789</div></div>
  </div>
  <p class="hint">Start/Stop thao tác Scheduled Task của Windows (openclaw daemon). Restart áp dụng thay đổi cấu hình mới lưu.</p>
</section>

<!-- AGENTS -->
<section class="section" id="s-agents">
  <div class="hd">
    <div><h2>Agents</h2><div class="sub">Thêm / sửa / xóa agent — mọi lệnh chạy nền ẩn, không mở terminal.</div></div>
    <div class="row" style="margin:0">
      <button class="btn" onclick="agLoad()">⟳ Làm mới</button>
      <button class="btn primary" onclick="agToggleAdd()">＋ Thêm agent</button>
    </div>
  </div>
  <div class="msg" id="ag-msg"></div>

  <!-- form thêm agent -->
  <div id="ag-add" class="box" style="display:none">
    <h3>Thêm agent mới <span class="tag">agents add</span></h3>
    <div class="cmdrow">
      <label class="fld">ID <input type="text" id="ag-new-id" placeholder="vd: seo-agent" style="min-width:180px"></label>
      <label class="fld">Model <input type="text" id="ag-new-model" placeholder="vd: omniroute/new-cb" style="min-width:220px" list="ag-models"></label>
      <datalist id="ag-models"></datalist>
    </div>
    <div class="cmdrow">
      <label class="fld">Workspace <input type="text" id="ag-new-ws" placeholder="(trống = ~/.openclaw/workspace/&lt;id&gt;)" style="min-width:320px"></label>
    </div>
    <div class="cmdrow">
      <label class="fld">Role <select id="ag-new-role">
        <option value="">— (không seed role) —</option>
        <option value="coordinator">coordinator</option>
        <option value="researcher">researcher</option>
        <option value="writer">writer</option>
        <option value="reviewer">reviewer</option>
      </select></label>
      <button class="btn primary" id="ag-add-btn" onclick="agAdd(this)">✓ Tạo agent</button>
      <button class="btn ghost" onclick="document.getElementById('ag-add').style.display='none'">Hủy</button>
    </div>
    <p class="hint">Role seed sẵn identity + workspace con (team ops). ID không được chứa space.</p>
  </div>

  <!-- form sửa identity -->
  <div id="ag-edit" class="box" style="display:none">
    <h3>Sửa agent: <span id="ag-edit-id" style="color:var(--indigo2);font-family:var(--mono)"></span></h3>
    <div class="cmdrow">
      <label class="fld">Tên hiển thị <input type="text" id="ag-edit-name" placeholder="vd: Chief of Staff" style="min-width:220px"></label>
      <label class="fld">Emoji <input type="text" id="ag-edit-emoji" placeholder="vd: 🧭" style="min-width:90px"></label>
    </div>
    <div class="cmdrow">
      <label class="fld">Theme <input type="text" id="ag-edit-theme" placeholder="vd: calm, accountable coordination" style="min-width:340px"></label>
    </div>
    <div class="cmdrow">
      <label class="fld">Model <input type="text" id="ag-edit-model" placeholder="vd: omniroute/new-cb" style="min-width:240px" list="ag-models"></label>
      <button class="btn primary" id="ag-edit-btn" onclick="agSaveEdit(this)">💾 Lưu</button>
      <button class="btn ghost" onclick="document.getElementById('ag-edit').style.display='none'">Đóng</button>
    </div>
    <p class="hint">Tên/Emoji/Theme ghi qua <code>set-identity</code>; Model ghi vào <code>agents.entries.&lt;id&gt;.model</code>.</p>
  </div>

  <div id="ag-list" class="list"></div>
</section>

<!-- ORCHESTRATION -->
<section class="section" id="s-orch">
  <div class="hd">
    <div><h2>Điều phối</h2><div class="sub">Phân công quản lý giữa các agent — ai được giao việc (sessions_spawn) cho ai, qua <code>subagents.allowAgents</code> + <code>delegationMode</code>.</div></div>
    <div class="row" style="margin:0">
      <button class="btn" onclick="orLoad()">⟳ Làm mới</button>
      <button class="btn" onclick="orEditDefaults()">⚙️ Quyền mặc định</button>
    </div>
  </div>
  <div class="msg" id="or-msg"></div>

  <div class="cards">
    <div class="card"><div class="k">Quyền mặc định (defaults)</div><div class="v sm" id="or-def-sum">…</div></div>
    <div class="card"><div class="k">System agent</div><div class="v sm" id="or-sys-sum">…</div></div>
    <div class="card"><div class="k">Talk agent</div><div class="v sm" id="or-talk-sum">…</div></div>
    <div class="card"><div class="k">Số agent</div><div class="v" id="or-count">…</div></div>
  </div>

  <div class="box">
    <h3>Ma trận phân công <span class="tag">phía ngang = người giao · phía dọc = người nhận</span></h3>
    <div class="matrix-wrap" id="or-matrix"></div>
    <div class="legend">
      <span><b class="ok">●</b> được giao việc</span>
      <span><b class="self">◐</b> chỉ tự mình (luôn có)</span>
      <span><b class="no">○</b> không được giao</span>
      <span>→ bấm ô / tên agent để sửa</span>
    </div>
    <p class="hint">Agent không khai báo riêng sẽ kế thừa <b>Quyền mặc định</b>. Mặc định của OpenClaw: <code>[]</code> = chỉ tự giao việc cho chính mình; <code>["*"]</code> = mọi agent.</p>
  </div>

  <!-- form sửa phân công 1 agent -->
  <div id="or-edit" class="box" style="display:none">
    <h3>Phân công cho: <span id="or-edit-agent" style="color:var(--indigo2);font-family:var(--mono)"></span></h3>
    <p class="hint" id="or-own-hint" style="margin:0 0 10px"></p>
    <label class="fld" style="margin-bottom:6px">Agent này được GIAO VIỆC cho:</label>
    <div class="chips" id="or-chips"></div>
    <div class="cmdrow">
      <label class="fld">Chế độ ủy quyền <select id="or-mode">
        <option value="">— kế thừa mặc định —</option>
        <option value="prefer">prefer (ưu tiên delegate)</option>
        <option value="suggest">suggest (gợi ý delegate)</option>
      </select></label>
      <label class="fld">Yêu cầu agentId <select id="or-req">
        <option value="">— kế thừa —</option>
        <option value="true">bắt buộc</option>
        <option value="false">không bắt buộc</option>
      </select></label>
      <button class="btn primary" id="or-btn" onclick="orSave(this)">💾 Lưu phân công</button>
      <button class="btn ghost" onclick="document.getElementById('or-edit').style.display='none'">Đóng</button>
    </div>
  </div>

  <!-- form sửa quyền mặc định -->
  <div id="or-def" class="box" style="display:none">
    <h3>Quyền mặc định <span class="tag">agents.defaults.subagents</span></h3>
    <p class="hint" style="margin:0 0 10px">Áp dụng cho mọi agent không khai báo riêng.</p>
    <label class="fld" style="margin-bottom:6px">Mọi agent được GIAO VIỆC cho:</label>
    <div class="chips" id="or-def-chips"></div>
    <div class="cmdrow">
      <label class="fld">Chế độ ủy quyền <select id="or-def-mode">
        <option value="">— không đặt —</option>
        <option value="prefer">prefer (ưu tiên delegate)</option>
        <option value="suggest">suggest (gợi ý delegate)</option>
      </select></label>
      <label class="fld">Yêu cầu agentId <select id="or-def-req">
        <option value="">— không đặt —</option>
        <option value="true">bắt buộc</option>
        <option value="false">không bắt buộc</option>
      </select></label>
      <button class="btn primary" id="or-def-btn" onclick="orSaveDef(this)">💾 Lưu mặc định</button>
      <button class="btn ghost" onclick="document.getElementById('or-def').style.display='none'">Đóng</button>
    </div>
  </div>
</section>

<!-- PROVIDERS -->
<section class="section" id="s-providers">
  <div class="hd">
    <div><h2>Model &amp; Provider</h2><div class="sub">Cấu hình <b>models.providers</b> (baseUrl, API key, danh sách model) + model mặc định + auth profile.</div></div>
    <div class="row" style="margin:0">
      <button class="btn" onclick="pvLoad()">⟳ Làm mới</button>
      <button class="btn primary" onclick="pvToggleAdd()">＋ Thêm provider</button>
      <button class="btn" onclick="document.getElementById('pv-setdef').style.display = document.getElementById('pv-setdef').style.display==='none'?'block':'none'">Đổi model mặc định</button>
    </div>
  </div>
  <div class="msg" id="pv-msg"></div>

  <div class="cards">
    <div class="card"><div class="k">Model mặc định</div><div class="v sm" id="pv-def">…</div></div>
    <div class="card"><div class="k">Số provider</div><div class="v" id="pv-np">…</div></div>
    <div class="card"><div class="k">Auth profiles</div><div class="v" id="pv-na">…</div></div>
  </div>

  <!-- đổi model mặc định -->
  <div id="pv-setdef" class="box" style="display:none">
    <div class="cmdrow" style="margin:0">
      <label class="fld">Model <input type="text" id="pv-def-in" placeholder="vd: omniroute/new-cb" style="min-width:260px" list="pv-models"></label>
      <datalist id="pv-models"></datalist>
      <button class="btn primary" id="pv-def-btn" onclick="pvSetDefault(this)">✓ Đặt làm mặc định</button>
    </div>
  </div>

  <!-- form thêm/sửa provider -->
  <div id="pv-form" class="box" style="display:none">
    <h3 id="pv-form-title">Provider</h3>
    <div class="cmdrow">
      <label class="fld">ID <input type="text" id="pv-id" placeholder="vd: myrouter" style="min-width:160px"></label>
      <label class="fld">API type <select id="pv-api">
        <option value="openai-completions">openai-completions</option>
        <option value="openai-responses">openai-responses</option>
        <option value="anthropic-messages">anthropic-messages</option>
        <option value="google-gemini">google-gemini</option>
      </select></label>
    </div>
    <div class="cmdrow">
      <label class="fld">Base URL <input type="text" id="pv-base" placeholder="http://localhost:20128/v1" style="min-width:340px"></label>
    </div>
    <div class="cmdrow">
      <label class="fld">API key <input type="text" id="pv-key" placeholder="sk-..." style="min-width:340px"></label>
    </div>
    <label class="fld" style="display:block;margin-bottom:6px">Danh sách model (JSON mảng):</label>
    <textarea class="cfg" id="pv-models-json" spellcheck="false" style="height:26vh">[]</textarea>
    <div class="cmdrow" style="margin-top:10px;margin-bottom:0">
      <button class="btn primary" id="pv-save-btn" onclick="pvSave(this)">💾 Lưu provider</button>
      <button class="btn ghost" onclick="document.getElementById('pv-form').style.display='none'">Đóng</button>
      <button class="btn danger" id="pv-del-btn" style="display:none" onclick="pvDel(this)">🗑 Xóa provider</button>
    </div>
    <p class="hint">Lưu sẽ validate schema + tự hoàn tác nếu lỗi. Cấu hình provider phải khớp model đang dùng: <code>omniroute/new-cb</code> → provider <code>omniroute</code>.</p>
  </div>

  <div id="pv-list" class="list"></div>

  <div class="hd" style="margin:22px 0 10px"><div><h2 style="font-size:15px">Auth profiles</h2><div class="sub">API key đã lưu — logout sẽ xóa key khỏi OpenClaw.</div></div></div>
  <div id="pv-auth" class="list"></div>
</section>

<!-- CONFIG -->
<section class="section" id="s-config">
  <div class="hd">
    <div><h2>Cấu hình</h2><div class="sub">Soạn thảo <b>~/.openclaw/openclaw.json</b> trực tiếp — kiểm tra hợp lệ trước khi lưu, tự hoàn tác nếu lỗi schema.</div></div>
  </div>
  <div class="msg" id="cfg-msg"></div>
  <div class="row">
    <button class="btn" onclick="cfgLoad()">⟲ Tải lại từ đĩa</button>
    <button class="btn" onclick="cfgValidate()">✓ Kiểm tra (validate)</button>
    <button class="btn primary" onclick="cfgSave()">💾 Lưu cấu hình</button>
    <button class="btn ghost" onclick="T.open_path('config')">Mở trong Notepad</button>
    <button class="btn ghost" onclick="T.open_path('openclaw')">Thư mục cấu hình</button>
  </div>
  <textarea class="cfg" id="cfg-text" spellcheck="false" placeholder="Nhấn 'Tải lại từ đĩa'…"></textarea>
  <p class="hint">Sau khi lưu cấu hình có thay đổi, nên Restart Gateway để áp dụng. Lỗi schema sẽ tự khôi phục file cũ.</p>
</section>

<!-- DOCTOR -->
<section class="section" id="s-doctor">
  <div class="hd">
    <div><h2>Doctor</h2><div class="sub">Chẩn đoán &amp; sửa chữa tự động — chạy nền, không mở console.</div></div>
  </div>
  <div class="row">
    <button class="btn primary" onclick="runOp(T.op_doctor(false), this)">🩺 Kiểm tra (doctor)</button>
    <button class="btn" onclick="if(confirm('Áp dụng các sửa chữa khuyến nghị?'))runOp(T.op_doctor(true), this)">🛠 Sửa tự động (doctor --fix)</button>
    <button class="btn" onclick="runOp(T.op_validate_config, this)">✓ Kiểm tra cấu hình</button>
  </div>
  <pre class="out" id="doctor-out"></pre>
</section>

<!-- BACKUP & UPDATE -->
<section class="section" id="s-backup">
  <div class="hd">
    <div><h2>Backup &amp; Update</h2><div class="sub">Sao lưu cấu hình + phiên làm việc, kiểm tra/cập nhật phiên bản OpenClaw.</div></div>
  </div>
  <div class="row">
    <button class="btn primary" onclick="runOp(T.op_backup, this)">💾 Tạo backup (có verify)</button>
    <button class="btn" onclick="T.open_path('backups')">Mở thư mục backup</button>
  </div>
  <div class="row">
    <button class="btn" onclick="runOp(T.op_update_status, this)">Kiểm tra cập nhật</button>
    <button class="btn" onclick="if(confirm('Cập nhật OpenClaw lên phiên bản mới nhất?'))runOp(T.op_update_apply, this)">⬆ Cập nhật ngay</button>
  </div>
  <pre class="out" id="bk-out"></pre>
</section>

<!-- LOGS -->
<section class="section" id="s-logs">
  <div class="hd">
    <div><h2>Nhật ký</h2><div class="sub">Đọc tail file log của Gateway — chọn file, chỉnh số dòng, làm mới.</div></div>
  </div>
  <div class="cmdrow">
    <select id="log-file" onchange="logRead()" style="min-width:320px"></select>
    <input type="number" id="log-lines" value="300" min="10" max="5000" step="10" style="width:90px" onchange="logRead()">
    <button class="btn" onclick="logList();logRead()">⟳ Làm mới</button>
    <button class="btn ghost" onclick="T.open_path('logs')">Mở thư mục log</button>
  </div>
  <pre class="out" id="log-out" style="max-height:65vh"></pre>
</section>

<!-- CLI -->
<section class="section" id="s-cli">
  <div class="hd">
    <div><h2>Lệnh CLI</h2><div class="sub">Chạy bất kỳ lệnh <b>openclaw …</b> nào từ trong app. Đầu ra stream theo thời gian thực.</div></div>
  </div>
  <div class="quick">
    <button onclick="quick('status')">status</button>
    <button onclick="quick('health')">health</button>
    <button onclick="quick('sessions list')">sessions list</button>
    <button onclick="quick('agents list')">agents list</button>
    <button onclick="quick('skills list')">skills list</button>
    <button onclick="quick('plugins list')">plugins list</button>
    <button onclick="quick('channels status')">channels status</button>
    <button onclick="quick('update status --json')">update status</button>
    <button onclick="quick('config get gateway')">config get gateway</button>
  </div>
  <div class="cmdrow">
    <span class="prefix">$ openclaw</span>
    <input type="text" id="cli-input" placeholder="vd: sessions list" onkeydown="if(event.key==='Enter')cliRun()">
    <button class="btn primary" id="cli-run" onclick="cliRun()">Chạy</button>
    <button class="btn ghost" onclick="cliClear()">Xóa output</button>
  </div>
  <pre class="out" id="cli-out" style="max-height:60vh"></pre>
</section>

</main>
<script>
let T = window.pywebview ? window.pywebview.api : null;
if (!T) {
  window.addEventListener('pywebviewready', () => { T = window.pywebview.api; refreshStatus(); });
}

/* ---------- nav ---------- */
document.getElementById('nav').addEventListener('click', e => {
  const b = e.target.closest('button'); if (!b) return;
  document.querySelectorAll('#nav button').forEach(x => x.classList.toggle('active', x === b));
  document.querySelectorAll('.section').forEach(s => s.classList.toggle('active', s.id === 's-' + b.dataset.s));
  if (b.dataset.s === 'overview' || b.dataset.s === 'gateway') refreshStatus();
  if (b.dataset.s === 'agents') agLoad();
  if (b.dataset.s === 'orch') orLoad();
  if (b.dataset.s === 'providers') pvLoad();
  if (b.dataset.s === 'config' && !document.getElementById('cfg-text').value) cfgLoad();
  if (b.dataset.s === 'logs' && !document.getElementById('log-file').options.length) { logList(); logRead(); }
});

function openMain(){ if (window.open_main_ui) window.open_main_ui(); }

/* ---------- status ---------- */
function renderOv(s){
  document.getElementById('ov-gw').innerHTML = s.alive
    ? '<span class="badge ok">● live</span>' : '<span class="badge err">● down</span>';
  document.getElementById('ov-omni').innerHTML = s.omniroute
    ? '<span class="badge ok">● live</span>' : '<span class="badge err">● down</span>';
  document.getElementById('ov-ver').textContent = s.version || '—';
  document.getElementById('ov-svc').textContent = s.service || (s.alive ? 'running' : 'stopped');
  document.getElementById('gdot').classList.toggle('on', !!s.alive);
  document.getElementById('ver').textContent = (s.version || '?').replace(/^OpenClaw\s*/,'');
  const fs = document.getElementById('foot-st');
  if (fs) fs.innerHTML = s.alive
    ? 'Gateway <b style="color:var(--ok)">● live</b>' : 'Gateway <b style="color:var(--err)">● down</b>';
}
function renderGw(s){
  document.getElementById('gw-state').innerHTML = s.alive
    ? '<span class="badge ok">● live</span>' : '<span class="badge err">● down</span>';
  document.getElementById('gw-svc').textContent = s.service || '—';
  document.getElementById('gw-omni').innerHTML = s.omniroute
    ? '<span class="badge ok">● live</span>' : '<span class="badge err">● down</span>';
}
function refreshStatus(){ T.gateway_status().then(s => { renderOv(s); renderGw(s); }); }

function gwAct(action, btn){
  const msg = document.getElementById('gw-msg');
  msg.className = 'msg';
  const ids = ['btn-start','btn-restart','btn-stop'];
  ids.forEach(i => document.getElementById(i).disabled = true);
  msg.className = 'msg ok'; msg.textContent = 'Đang ' + action + ' gateway…';
  const fn = action === 'start' ? T.gw_start : action === 'stop' ? T.gw_stop : T.gw_restart;
  fn().then(s => {
    ids.forEach(i => document.getElementById(i).disabled = false);
    renderGw(s); renderOv(s);
    msg.className = s.alive ? 'msg ok' : 'msg err';
    msg.textContent = s.alive ? 'Gateway đang chạy (live).' : 'Gateway KHÔNG phản hồi — thử Start hoặc kiểm tra log.';
  });
}

/* ---------- job streaming ---------- */
function streamJob(jid, outEl, onDone){
  let cursor = 0;
  const tick = () => T.job_poll(jid, cursor).then(r => {
    if (!r.exists) { clearInterval(iv); return; }
    if (r.lines && r.lines.length) {
      outEl.textContent += r.lines.join('\n') + '\n';
      cursor = r.total;
      outEl.scrollTop = outEl.scrollHeight;
    }
    if (r.done) { clearInterval(iv); onDone && onDone(r); }
  });
  const iv = setInterval(tick, 500);
  tick();
}
function runOp(opFn, btn){
  const section = btn.closest('.section');
  const out = section.querySelector('pre.out');
  const label = btn.textContent;
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> ' + label;
  const p = (typeof opFn === 'function') ? opFn() : opFn;
  p.then(r => {
    btn.disabled = false; btn.textContent = label;
    if (r && r.job) streamJob(r.job, out);
    else out.textContent += '[error] ' + ((r && r.error) || 'failed') + '\n';
  }).catch(e => {
    btn.disabled = false; btn.textContent = label;
    out.textContent += '[error] ' + e + '\n';
  });
}

/* ---------- config ---------- */
function cfgMsg(ok, text){
  const el = document.getElementById('cfg-msg');
  el.className = 'msg ' + (ok ? 'ok' : 'err');
  el.textContent = text;
  setTimeout(() => { el.className = 'msg'; }, 8000);
}
function cfgLoad(){
  T.config_read().then(r => {
    if (r.ok) { document.getElementById('cfg-text').value = r.text; cfgMsg(true, 'Đã tải ' + r.path); }
    else cfgMsg(false, r.error);
  });
}
function cfgSave(){
  const text = document.getElementById('cfg-text').value;
  if (!confirm('Lưu ~/.openclaw/openclaw.json? (tự hoàn tác nếu lỗi schema)')) return;
  T.config_write(text).then(r => {
    if (r.ok) cfgMsg(true, '✓ Đã lưu & validate OK' + (r.warnings && r.warnings.length ? ' (warnings: ' + r.warnings.join('; ') + ')' : ''));
    else cfgMsg(false, '✗ ' + r.error);
  });
}
function cfgValidate(){
  const text = document.getElementById('cfg-text').value;
  try { JSON.parse(text); cfgMsg(true, '✓ JSON hợp lệ — bấm "Lưu cấu hình" để validate theo schema (tự hoàn tác nếu lỗi).'); }
  catch(e){ cfgMsg(false, '✗ Lỗi cú pháp JSON: ' + e.message); }
}

/* ---------- doctor / backup output ---------- */

/* ---------- logs ---------- */
function logList(){
  T.logs_list().then(files => {
    const sel = document.getElementById('log-file');
    const cur = sel.value;
    sel.innerHTML = '';
    files.forEach(f => {
      const o = document.createElement('option');
      o.value = f.name;
      o.textContent = f.name + '  (' + Math.round(f.size/1024) + ' KB)';
      sel.appendChild(o);
    });
    if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
  });
}
function logRead(){
  const name = document.getElementById('log-file').value;
  if (!name) return;
  const n = parseInt(document.getElementById('log-lines').value) || 300;
  T.logs_read(name, n).then(r => { document.getElementById('log-out').textContent = r.text || ''; });
}

/* ---------- CLI ---------- */
function quick(args){ document.getElementById('cli-input').value = args; cliRun(); }
function cliClear(){ document.getElementById('cli-out').textContent = ''; }
function cliRun(){
  const input = document.getElementById('cli-input');
  const args = input.value.trim();
  if (!args) return;
  const btn = document.getElementById('cli-run');
  const out = document.getElementById('cli-out');
  out.textContent += '$ openclaw ' + args + '\n';
  btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Chạy';
  T.job_start(args.split(/\s+/)).then(r => {
    if (!r.job) { out.textContent += '[refused]\n'; btn.disabled = false; btn.textContent = 'Chạy'; return; }
    streamJob(r.job, out, s => {
      btn.disabled = false; btn.textContent = 'Chạy';
      if (s.code) out.textContent += '[exit ' + s.code + ']\n';
      out.scrollTop = out.scrollHeight;
    });
  });
}
document.getElementById('cli-input').addEventListener('keydown', e => { if (e.key === 'Enter') cliRun(); });

/* ---------- helpers ---------- */
function esc(s){ return String(s==null?'':s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function pvMsg(ok, text){
  const el = document.getElementById('pv-msg');
  el.className = 'msg ' + (ok ? 'ok' : 'err');
  el.textContent = text;
  setTimeout(() => { el.className = 'msg'; }, 9000);
}
function agMsg(ok, text){
  const el = document.getElementById('ag-msg');
  el.className = 'msg ' + (ok ? 'ok' : 'err');
  el.textContent = text;
  setTimeout(() => { el.className = 'msg'; }, 9000);
}
function orMsg(ok, text){
  const el = document.getElementById('or-msg');
  el.className = 'msg ' + (ok ? 'ok' : 'err');
  el.textContent = text;
  setTimeout(() => { el.className = 'msg'; }, 9000);
}
function busy(btn, on, label){
  if (on) { btn.dataset.lb = btn.textContent; btn.disabled = true; btn.innerHTML = '<span class="spin"></span> ' + (label||'…'); }
  else { btn.disabled = false; btn.textContent = btn.dataset.lb || label || '…'; }
}

/* ---------- AGENTS ---------- */
let AG_CURRENT = null;
function agToggleAdd(){
  const b = document.getElementById('ag-add');
  b.style.display = b.style.display === 'none' ? 'block' : 'none';
  document.getElementById('ag-edit').style.display = 'none';
}
function agLoad(){
  T.agents_list().then(r => {
    if (!r.ok) { agMsg(false, '✗ ' + r.error); return; }
    agRender(r.agents || []);
    T.models_configured().then(m => {
      if (m.ok) {
        const dl = document.getElementById('ag-models');
        dl.innerHTML = (m.models || []).map(x => '<option value="' + esc(x) + '"></option>').join('');
      }
    });
  });
}
function agRender(list){
  const el = document.getElementById('ag-list');
  if (!list.length) { el.innerHTML = '<div class="item">Chưa có agent nào.</div>'; return; }
  el.innerHTML = list.map(a => {
    const def = a.isDefault ? ' <span class="badge ok">default</span>' : '';
    const em = esc(a.identityEmoji || '🤖');
    return '<div class="item">'
      + '<span class="ava">' + em + '</span>'
      + '<span class="id">' + esc(a.id) + def + '</span>'
      + '<span class="meta">' + esc(a.identityName || '') + '</span>'
      + '<span class="chip">' + esc(a.model || '—') + '</span>'
      + '<span class="sp"></span>'
      + '<button class="btn mini" data-id="' + esc(a.id) + '" data-model="' + esc(a.model || '') + '" onclick="agEdit(this)">✎ Sửa</button>'
      + '<button class="btn mini danger" data-id="' + esc(a.id) + '" data-nm="' + esc(a.identityName || a.id) + '" onclick="agDel(this)">🗑 Xóa</button>'
      + '</div>';
  }).join('');
}
function agAdd(btn){
  const id = document.getElementById('ag-new-id').value.trim();
  const model = document.getElementById('ag-new-model').value.trim();
  const ws = document.getElementById('ag-new-ws').value.trim();
  const role = document.getElementById('ag-new-role').value;
  if (!id) { agMsg(false, 'Nhập ID agent.'); return; }
  busy(btn, true, 'Đang tạo…');
  T.agent_add(id, ws, model, role).then(r => {
    busy(btn, false, '✓ Tạo agent');
    if (r.ok) {
      agMsg(true, '✓ Đã tạo agent "' + id + '"');
      document.getElementById('ag-add').style.display = 'none';
      agLoad();
    } else agMsg(false, '✗ ' + r.error);
  }).catch(e => { busy(btn, false, '✓ Tạo agent'); agMsg(false, '✗ ' + e); });
}
function agEdit(btn){
  const id = btn.dataset.id;
  AG_CURRENT = id;
  document.getElementById('ag-add').style.display = 'none';
  const box = document.getElementById('ag-edit');
  box.style.display = 'block';
  document.getElementById('ag-edit-id').textContent = id;
  document.getElementById('ag-edit-name').value = '';
  document.getElementById('ag-edit-emoji').value = '';
  document.getElementById('ag-edit-theme').value = '';
  const m = document.getElementById('ag-edit-model');
  m.value = btn.dataset.model || '';
  m.dataset.orig = btn.dataset.model || '';
  box.scrollIntoView({behavior:'smooth', block:'nearest'});
}
function agSaveEdit(btn){
  const id = AG_CURRENT;
  if (!id) return;
  const name = document.getElementById('ag-edit-name').value.trim();
  const emoji = document.getElementById('ag-edit-emoji').value.trim();
  const theme = document.getElementById('ag-edit-theme').value.trim();
  const mEl = document.getElementById('ag-edit-model');
  const model = mEl.value.trim();
  busy(btn, true, 'Đang lưu…');
  const done = (ok, text) => { busy(btn, false, '💾 Lưu'); ok ? agMsg(true, text) : agMsg(false, text); if (ok) agLoad(); };
  const step1 = (name || emoji || theme)
    ? T.agent_identity(id, name, emoji, theme)
    : Promise.resolve({ok: true});
  step1.then(r1 => {
    if (!r1.ok) { done(false, '✗ Identity: ' + r1.error); return; }
    if (model && model !== (mEl.dataset.orig || '')) {
      T.agent_set_model(id, model).then(r2 => {
        r2.ok ? done(true, '✓ Đã lưu identity + model cho "' + id + '"')
              : done(false, '✗ Model: ' + r2.error);
      });
    } else done(true, '✓ Đã lưu identity cho "' + id + '"');
  }).catch(e => { busy(btn, false, '💾 Lưu'); agMsg(false, '✗ ' + e); });
}
function agDel(btn){
  const id = btn.dataset.id, nm = btn.dataset.nm || id;
  if (!confirm('Xóa agent "' + nm + '" (' + id + ')?\nWorkspace có thể được giữ lại nếu dùng chung.')) return;
  busy(btn, true, 'Đang xóa…');
  T.agent_delete(id).then(r => {
    busy(btn, false, '🗑 Xóa');
    if (r.ok) { agMsg(true, '✓ Đã xóa agent "' + id + '"'); agLoad(); }
    else agMsg(false, '✗ ' + r.error);
  }).catch(e => { busy(btn, false, '🗑 Xóa'); agMsg(false, '✗ ' + e); });
}

/* ---------- ORCHESTRATION ---------- */
let OR_DATA = null, OR_CUR = null;
function orAllowText(arr){
  if (!arr || !arr.length) return 'chỉ tự mình';
  if (arr.indexOf('*') >= 0) return 'mọi agent';
  return arr.join(', ');
}
function orLoad(){
  T.orch_get().then(r => {
    if (!r.ok) { orMsg(false, '✗ ' + r.error); return; }
    OR_DATA = r;
    orRender(r);
  }).catch(e => orMsg(false, '✗ ' + e));
}
function orRender(r){
  document.getElementById('or-def-sum').textContent = orAllowText(r.defaults.allow)
    + (r.defaults.mode ? ' · ' + r.defaults.mode : '');
  document.getElementById('or-sys-sum').textContent = r.systemAgent || '—';
  document.getElementById('or-talk-sum').textContent = r.talkAgent || '—';
  document.getElementById('or-count').textContent = r.ids.length;
  const ids = r.ids;
  if (!ids.length) { document.getElementById('or-matrix').innerHTML = '<p class="hint">Chưa có agent nào.</p>'; return; }
  let h = '<table class="matrix"><thead><tr><th class="rh"></th>';
  ids.forEach(id => { h += '<th title="' + esc(id) + '">' + esc(id) + '</th>'; });
  h += '</tr></thead><tbody>';
  r.agents.forEach(a => {
    h += '<tr><th class="rh" title="Chỉnh sửa phân công của ' + esc(a.id) + '" onclick="orEditAgent(\'' + esc(a.id) + '\')">'
       + esc(a.emoji || '🤖') + ' ' + esc(a.id)
       + (a.allowRaw === null ? '' : ' <span class="badge info">riêng</span>')
       + (a.modeRaw ? ' <span class="chip">' + esc(a.modeRaw) + '</span>' : '')
       + '</th>';
    ids.forEach(c => {
      let st, mark;
      if (a.id === c) { st = 'self'; mark = '◐'; }
      else if (a.allow.indexOf('*') >= 0 || a.allow.indexOf(c) >= 0) { st = 'ok'; mark = '●'; }
      else { st = 'no'; mark = '○'; }
      const title = a.id + ' → ' + c + ' : ' + (st === 'self' ? 'tự mình' : st === 'ok' ? 'được giao' : 'không được giao');
      h += '<td class="cell ' + st + '" title="' + esc(title) + '" onclick="orEditAgent(\'' + esc(a.id) + '\')">' + mark + '</td>';
    });
    h += '</tr>';
  });
  h += '</tbody></table>';
  document.getElementById('or-matrix').innerHTML = h;
}
function orChipsHTML(selfId, allow, starId){
  const ids = (OR_DATA.ids || []).filter(x => x !== selfId);
  const star = allow.indexOf('*') >= 0;
  let h = ids.map(x => {
    const on = star || allow.indexOf(x) >= 0;
    return '<label><input type="checkbox" value="' + esc(x) + '"' + (on ? ' checked' : '') + '>' + esc(x) + '</label>';
  }).join('');
  h += '<label class="star"><input type="checkbox" id="' + starId + '"' + (star ? ' checked' : '')
     + ' onchange="orStarToggle(this)">Tất cả agent (*)</label>';
  if (!ids.length) h = '<span class="hint" style="margin:0">Không có agent khác trong hệ thống.</span>'
    + '<label class="star"><input type="checkbox" id="' + starId + '"' + (star ? ' checked' : '') + '>Tất cả agent (*)</label>';
  return h;
}
function orStarToggle(el){
  document.querySelectorAll('#or-chips input[type=checkbox]:not(#or-star), #or-def-chips input[type=checkbox]:not(#or-def-star)')
    .forEach(i => { i.disabled = el.checked; });
}
function orEditAgent(id){
  if (!OR_DATA) return;
  const a = (OR_DATA.agents || []).find(x => x.id === id);
  if (!a) return;
  OR_CUR = id;
  document.getElementById('or-def').style.display = 'none';
  const box = document.getElementById('or-edit');
  box.style.display = 'block';
  document.getElementById('or-edit-agent').textContent = (a.emoji || '🤖') + ' ' + a.id + (a.name ? ' — ' + a.name : '');
  document.getElementById('or-own-hint').innerHTML = a.allowRaw === null
    ? 'Đang <b>kế thừa</b> quyền mặc định: <b>' + esc(orAllowText(a.allow)) + '</b>'
    : 'Agent này <b>tự khai báo</b> quyền riêng: <b>' + esc(orAllowText(a.allowRaw)) + '</b>';
  document.getElementById('or-chips').innerHTML = orChipsHTML(id, a.allowRaw === null ? a.allow : a.allowRaw, 'or-star');
  document.getElementById('or-mode').value = a.modeRaw;
  const reqSel = document.getElementById('or-req');
  reqSel.value = a.req === true ? 'true' : a.req === false ? 'false' : '';
  orStarToggle({checked: false});
  box.scrollIntoView({behavior:'smooth', block:'nearest'});
}
function orGather(chipsId, starId){
  const star = document.getElementById(starId);
  if (star && star.checked) return ['*'];
  return [...document.querySelectorAll('#' + chipsId + ' input[type=checkbox]:not(#' + starId + ')')]
    .filter(c => c.checked).map(c => c.value);
}
function orSave(btn){
  if (!OR_CUR) return;
  const allow = orGather('or-chips', 'or-star');
  const mode = document.getElementById('or-mode').value;
  const reqV = document.getElementById('or-req').value;
  const req = reqV === '' ? null : reqV === 'true';
  busy(btn, true, 'Đang lưu…');
  T.orch_save('agent', OR_CUR, JSON.stringify(allow), mode, req).then(r => {
    busy(btn, false, '💾 Lưu phân công');
    if (r.ok) {
      orMsg(true, '✓ Đã lưu phân công cho "' + OR_CUR + '": ' + orAllowText(allow) + ' — restart Gateway để áp dụng.');
      orLoad();
    } else orMsg(false, '✗ ' + r.error);
  }).catch(e => { busy(btn, false, '💾 Lưu phân công'); orMsg(false, '✗ ' + e); });
}
function orEditDefaults(){
  if (!OR_DATA) return;
  document.getElementById('or-edit').style.display = 'none';
  const box = document.getElementById('or-def');
  box.style.display = 'block';
  document.getElementById('or-def-chips').innerHTML = orChipsHTML(null, OR_DATA.defaults.allow, 'or-def-star');
  document.getElementById('or-def-mode').value = OR_DATA.defaults.mode;
  const reqSel = document.getElementById('or-def-req');
  reqSel.value = OR_DATA.defaults.req === true ? 'true' : OR_DATA.defaults.req === false ? 'false' : '';
  orStarToggle({checked: false});
  box.scrollIntoView({behavior:'smooth', block:'nearest'});
}
function orSaveDef(btn){
  const allow = orGather('or-def-chips', 'or-def-star');
  const mode = document.getElementById('or-def-mode').value;
  const reqV = document.getElementById('or-def-req').value;
  const req = reqV === '' ? null : reqV === 'true';
  busy(btn, true, 'Đang lưu…');
  T.orch_save('defaults', '', JSON.stringify(allow), mode, req).then(r => {
    busy(btn, false, '💾 Lưu mặc định');
    if (r.ok) {
      orMsg(true, '✓ Đã lưu quyền mặc định: ' + orAllowText(allow) + ' — restart Gateway để áp dụng.');
      orLoad();
    } else orMsg(false, '✗ ' + r.error);
  }).catch(e => { busy(btn, false, '💾 Lưu mặc định'); orMsg(false, '✗ ' + e); });
}

/* ---------- PROVIDERS ---------- */
let PV_CURRENT = null;
function pvToggleAdd(){
  PV_CURRENT = null;
  const box = document.getElementById('pv-form');
  box.style.display = 'block';
  document.getElementById('pv-form-title').textContent = 'Thêm provider mới';
  document.getElementById('pv-id').disabled = false;
  document.getElementById('pv-id').value = '';
  document.getElementById('pv-api').value = 'openai-completions';
  document.getElementById('pv-base').value = '';
  document.getElementById('pv-key').value = '';
  document.getElementById('pv-models-json').value = '[]';
  document.getElementById('pv-del-btn').style.display = 'none';
}
function pvLoad(){
  T.providers_get().then(r => {
    if (!r.ok) { pvMsg(false, '✗ ' + r.error); return; }
    const provs = r.providers || {};
    const ids = Object.keys(provs);
    document.getElementById('pv-def').textContent = r.defaultModel || '—';
    document.getElementById('pv-np').textContent = ids.length;
    const dl = document.getElementById('pv-models');
    const models = [];
    ids.forEach(pid => (provs[pid].models || []).forEach(m => models.push(pid + '/' + m.id)));
    dl.innerHTML = models.map(x => '<option value="' + esc(x) + '"></option>').join('');
    document.getElementById('pv-list').innerHTML = ids.length ? ids.map(pid => {
      const p = provs[pid] || {};
      const n = (p.models || []).length;
      const key = p.apiKey ? (String(p.apiKey).slice(0, 7) + '…' + String(p.apiKey).slice(-4)) : '(none)';
      return '<div class="item">'
        + '<span class="ava">🧩</span>'
        + '<span class="id">' + esc(pid) + '</span>'
        + '<span class="meta">' + esc(p.baseUrl || '') + ' · ' + esc(p.api || '') + ' · ' + n + ' model · key ' + esc(key) + '</span>'
        + '<span class="sp"></span>'
        + '<button class="btn mini" data-pid="' + esc(pid) + '" onclick="pvEdit(this)">✎ Sửa</button>'
        + '<button class="btn mini danger" data-pid="' + esc(pid) + '" onclick="pvDel(this)">🗑</button>'
        + '</div>';
    }).join('') : '<div class="item">Chưa có provider nào trong models.providers.</div>';
  });
  T.auth_list().then(r => {
    if (!r.ok) { document.getElementById('pv-auth').innerHTML = '<div class="item">' + esc(r.error) + '</div>'; return; }
    const ps = r.profiles || [];
    document.getElementById('pv-na').textContent = ps.length;
    document.getElementById('pv-auth').innerHTML = ps.length ? ps.map(p => {
      const cd = p.cooldownUntil ? ' <span class="badge warn">cooldown</span>' : '';
      return '<div class="item">'
        + '<span class="ava">🔑</span>'
        + '<span class="id">' + esc(p.id) + '</span>'
        + '<span class="meta">' + esc(p.provider) + ' · ' + esc(p.type) + cd + '</span>'
        + '<span class="sp"></span>'
        + '<button class="btn mini danger" data-pid="' + esc(p.id) + '" onclick="authDel(this)">🗑 Logout</button>'
        + '</div>';
    }).join('') : '<div class="item">Không có auth profile nào.</div>';
  }).catch(() => {});
}
function pvEdit(btn){
  const pid = btn.dataset.pid;
  T.providers_get().then(r => {
    if (!r.ok) { pvMsg(false, '✗ ' + r.error); return; }
    const p = (r.providers || {})[pid];
    if (!p) { pvMsg(false, 'Không thấy provider ' + pid); return; }
    PV_CURRENT = pid;
    const box = document.getElementById('pv-form');
    box.style.display = 'block';
    document.getElementById('pv-form-title').textContent = 'Sửa provider: ' + pid;
    document.getElementById('pv-id').disabled = true;
    document.getElementById('pv-id').value = pid;
    document.getElementById('pv-api').value = p.api || 'openai-completions';
    document.getElementById('pv-base').value = p.baseUrl || '';
    document.getElementById('pv-key').value = p.apiKey || '';
    document.getElementById('pv-models-json').value = JSON.stringify(p.models || [], null, 2);
    document.getElementById('pv-del-btn').style.display = '';
  });
}
function pvSave(btn){
  const pid = PV_CURRENT || document.getElementById('pv-id').value.trim();
  if (!pid) { pvMsg(false, 'Nhập ID provider.'); return; }
  let models;
  try { models = JSON.parse(document.getElementById('pv-models-json').value || '[]'); }
  catch (e) { pvMsg(false, '✗ Danh sách model không phải JSON hợp lệ: ' + e.message); return; }
  const obj = {
    baseUrl: document.getElementById('pv-base').value.trim(),
    apiKey: document.getElementById('pv-key').value.trim(),
    api: document.getElementById('pv-api').value,
    models: models
  };
  busy(btn, true, 'Đang lưu…');
  T.provider_save(pid, JSON.stringify(obj)).then(r => {
    busy(btn, false, '💾 Lưu provider');
    if (r.ok) { pvMsg(true, '✓ Đã lưu provider "' + pid + '" — restart Gateway để áp dụng.'); pvLoad(); }
    else pvMsg(false, '✗ ' + r.error);
  }).catch(e => { busy(btn, false, '💾 Lưu provider'); pvMsg(false, '✗ ' + e); });
}
function pvDel(btn){
  const pid = btn.dataset.pid || (PV_CURRENT || document.getElementById('pv-id').value.trim());
  if (!pid) return;
  if (!confirm('Xóa provider "' + pid + '"?\nModel trỏ tới provider này sẽ ngừng hoạt động.')) return;
  busy(btn, true, '…');
  T.provider_delete(pid).then(r => {
    busy(btn, false, '🗑 Xóa provider');
    if (r.ok) {
      pvMsg(true, '✓ Đã xóa provider "' + pid + '"');
      document.getElementById('pv-form').style.display = 'none';
      PV_CURRENT = null;
      pvLoad();
    } else pvMsg(false, '✗ ' + r.error);
  }).catch(e => { busy(btn, false, '🗑 Xóa provider'); pvMsg(false, '✗ ' + e); });
}
function pvSetDefault(btn){
  const m = document.getElementById('pv-def-in').value.trim();
  if (!m) { pvMsg(false, 'Nhập model id.'); return; }
  busy(btn, true, 'Đang đặt…');
  T.model_set_default(m).then(r => {
    busy(btn, false, '✓ Đặt làm mặc định');
    if (r.ok) { pvMsg(true, '✓ Model mặc định: ' + m + ' — restart Gateway để áp dụng.'); pvLoad(); }
    else pvMsg(false, '✗ ' + r.error);
  }).catch(e => { busy(btn, false, '✓ Đặt làm mặc định'); pvMsg(false, '✗ ' + e); });
}
function authDel(btn){
  const pid = btn.dataset.pid;
  if (!confirm('Logout auth profile "' + pid + '"?\nAPI key của profile này sẽ bị xóa.')) return;
  busy(btn, true, '…');
  T.auth_logout(pid).then(r => {
    busy(btn, false, '🗑 Logout');
    if (r.ok) { pvMsg(true, '✓ Đã logout "' + pid + '"'); pvLoad(); }
    else pvMsg(false, '✗ ' + r.error);
  }).catch(e => { busy(btn, false, '🗑 Logout'); pvMsg(false, '✗ ' + e); });
}

/* ---------- init ---------- */
if (T) refreshStatus();
setInterval(() => {
  if (!T) return;
  const active = document.querySelector('#nav button.active');
  if (active && (active.dataset.s === 'overview' || active.dataset.s === 'gateway')) refreshStatus();
}, 15000);
</script>
</body>
</html>
"""


def create_toolkit(api: ToolkitApi, open_main_cb):
    """Create the (hidden) Toolkit window. Returns the window."""
    import webview

    window = webview.create_window(
        title=TOOLKIT_TITLE,
        html=TOOLKIT_HTML,
        width=1100,
        height=760,
        min_size=(860, 560),
        background_color="#0a0d13",
        text_select=True,
        js_api=api,
        hidden=True,
    )
    # expose a bridge for "Mở Control UI"
    def _on_loaded():
        try:
            window.evaluate_js(
                "window.open_main_ui = function(){ window.pywebview.api.open_main(); }; true;"
            )
        except Exception:
            pass

    window.events.loaded += _on_loaded

    api._window = window
    api._open_main = open_main_cb
    return window


_orig_loaded_handler = None
