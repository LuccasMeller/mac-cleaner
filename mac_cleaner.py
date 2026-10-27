#!/usr/bin/env python3
"""
Mac Cleaner — standalone local web app
Run: python3 mac_cleaner.py
"""

import json
import os
import subprocess
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 7891
HOME = os.path.expanduser("~")

# ── log ───────────────────────────────────────────────────────────────────────

import time as _time
_log = []  # list of {ts, cmd, duration, lines, error}

def _log_add(cmd, duration, output, error=None):
    lines = len(output.splitlines()) if output else 0
    preview = output[:120].replace("\n", " ↵ ") if output else ""
    _log.append({
        "ts": _time.strftime("%H:%M:%S"),
        "cmd": cmd[:120],
        "duration": round(duration, 2),
        "lines": lines,
        "preview": preview,
        "error": error,
    })
    if len(_log) > 200:
        _log.pop(0)

# ── shell helpers ─────────────────────────────────────────────────────────────

def run(cmd, timeout=60):
    t0 = _time.time()
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        _log_add(cmd, _time.time() - t0, r.stdout.strip())
        return r.stdout.strip()
    except subprocess.TimeoutExpired:
        _log_add(cmd, _time.time() - t0, "", error="Timeout")
        return "Timeout — operação demorou demais."
    except Exception as e:
        _log_add(cmd, _time.time() - t0, "", error=str(e))
        return f"Erro: {e}"

def run_lines(cmd, timeout=60):
    out = run(cmd, timeout)
    return [l for l in out.splitlines() if l.strip()]

# ── API handlers ──────────────────────────────────────────────────────────────

def api_disk():
    info = run("diskutil info / 2>/dev/null")
    total_b = free_b = 0
    for line in info.splitlines():
        if "Container Total Space:" in line:
            try: total_b = int(line.split("(")[1].split()[0])
            except Exception: pass
        elif "Container Free Space:" in line:
            try: free_b = int(line.split("(")[1].split()[0])
            except Exception: pass
    if total_b and free_b:
        used_b = total_b - free_b
        def fmt(b): return f"{b/1e9:.1f} GB"
        pct = f"{round(used_b / total_b * 100)}%"
        return {"total": fmt(total_b), "used": fmt(used_b), "free": fmt(free_b), "pct": pct}
    # fallback
    raw = run("df -h / | tail -1").split()
    if len(raw) >= 5:
        return {"total": raw[1], "used": raw[2], "free": raw[3], "pct": raw[4]}
    return {"total": "?", "used": "?", "free": "?", "pct": "?"}

def api_memory():
    page_size = 16384
    vm = run("vm_stat")
    stats = {}
    for line in vm.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            try: stats[k.strip()] = int(v.strip().rstrip("."))
            except Exception: pass

    wired      = stats.get("Pages wired down", 0) * page_size
    active     = stats.get("Pages active", 0) * page_size
    inactive   = stats.get("Pages inactive", 0) * page_size
    free_p     = stats.get("Pages free", 0) * page_size
    speculative= stats.get("Pages speculative", 0) * page_size
    purgeable2 = stats.get("Pages purgeable", 0) * page_size
    compressed = stats.get("Pages occupied by compressor", 0) * page_size

    total_b = int(run("sysctl -n hw.memsize") or 0)
    # Match Activity Monitor: Used = wired + active + compressed (NOT inactive)
    # Inactive = file cache — released automatically when needed, not "real" pressure
    used_b = wired + active + compressed
    free_b = (free_p + speculative + purgeable2)
    # "cached files" shown separately (inactive + file-backed pages)
    cached_b = inactive

    def fmt(b): return f"{b/1073741824:.1f} GB"

    file_cache = stats.get("File-backed pages", 0) * page_size
    purgeable  = purgeable2

    # GPU memory from IOAccelerator
    gpu_alloc = gpu_used = 0
    ioreg_out = run("ioreg -r -d 1 -w 0 -c IOAccelerator 2>/dev/null | grep PerformanceStatistics | head -1")
    import re
    m_alloc = re.search(r'"Alloc system memory"=(\d+)', ioreg_out)
    m_used  = re.search(r'"In use system memory"=(\d+)', ioreg_out)
    if m_alloc: gpu_alloc = int(m_alloc.group(1))
    if m_used:  gpu_used  = int(m_used.group(1))

    # Top processes by RSS+CPU — ps aux: USER PID %CPU %MEM VSZ RSS TT STAT START TIME COMMAND
    ps_out = run("ps aux 2>/dev/null | sort -rn -k6")
    skip = {"ps", "sort", "grep", "awk", "sed", "sh", "bash", "zsh", "launchd", "kernel_task"}
    groups = {}   # name → {rss_total, cpu_total, pids}
    for line in ps_out.splitlines():
        parts = line.strip().split(None, 10)
        if len(parts) < 11: continue
        try:
            pid = int(parts[1]); cpu = float(parts[2].replace(",", ".")); rss = int(parts[5]) * 1024
        except Exception: continue
        raw  = parts[10].split("/")[-1]
        name = raw.split(" Helper")[0].strip()
        if name in skip or rss < 5 * 1024 * 1024: continue
        if name not in groups:
            groups[name] = {"rss_total": 0, "cpu_total": 0.0, "pids": []}
        groups[name]["rss_total"] += rss
        groups[name]["cpu_total"] += cpu
        groups[name]["pids"].append({"pid": pid, "rss": fmt(rss), "rss_b": rss})

    top = sorted(groups.items(), key=lambda x: x[1]["rss_total"], reverse=True)[:20]
    top_list = []
    for name, g in top:
        g["pids"].sort(key=lambda p: p["rss_b"], reverse=True)
        top_list.append({
            "name": name,
            "rss": fmt(g["rss_total"]),
            "rss_b": g["rss_total"],
            "cpu": round(g["cpu_total"], 1),
            "pids": g["pids"][:5],
        })
    procs_total = sum(g["rss_total"] for _, g in top)

    # Reboot estimate: after restart macOS typically settles at ~3 GB for system baseline
    # (kernel wired ~1.5 GB + WindowServer + essential daemons ~1.5 GB)
    # Post-reboot estimate: only wired baseline remains (~1.5 GB kernel)
    POST_BOOT_BASELINE = 1.8 * 1_073_741_824
    reboot_freed = max(0, used_b - POST_BOOT_BASELINE)
    reboot_freed_pct = round(reboot_freed / total_b * 100) if total_b else 0

    return {
        "total": fmt(total_b),
        "used": fmt(used_b),
        "free": fmt(free_b),
        "cached": fmt(cached_b),
        "wired": fmt(wired),
        "active": fmt(active),
        "inactive": fmt(inactive),
        "compressed": fmt(compressed),
        "file_cache": fmt(file_cache),
        "purgeable": fmt(purgeable),
        "gpu_alloc": fmt(gpu_alloc),
        "gpu_used": fmt(gpu_used),
        "procs_total": fmt(procs_total),
        "reboot_freed": fmt(reboot_freed),
        "reboot_freed_pct": reboot_freed_pct,
        "pct": round(used_b / total_b * 100) if total_b else 0,
        "processes": top_list,
    }

def api_top_folders():
    out = run(f"du -sh {HOME}/* 2>/dev/null | sort -rh | head -12")
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            rows.append({"size": parts[0], "path": parts[1].replace(HOME, "~")})
    return rows

def api_large_files(min_size="100M", directory="~"):
    directory = directory.replace("~", HOME)
    out = run(
        f"find {directory} -type f -size +{min_size} 2>/dev/null"
        f" | xargs du -sh 2>/dev/null | sort -rh | head -30",
        timeout=120,
    )
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            rows.append({"size": parts[0], "path": parts[1]})
    return rows

def api_cache_sizes():
    targets = {
        "Sistema (~/ Library/Caches)": f"{HOME}/Library/Caches",
        "Safari": f"{HOME}/Library/Caches/com.apple.Safari",
        "Chrome": f"{HOME}/Library/Caches/Google/Chrome",
        "npm": f"{HOME}/.npm/_cacache",
        "pip": f"{HOME}/.cache/pip",
        "Homebrew": f"{HOME}/Library/Caches/Homebrew",
    }
    results = []
    for label, path in targets.items():
        if os.path.exists(path):
            size = run(f"du -sh '{path}' 2>/dev/null | cut -f1")
        else:
            size = "—"
        results.append({"label": label, "path": path, "size": size, "exists": os.path.exists(path)})
    return results

def api_clean_cache(path):
    path = path.replace("~", HOME)
    if not os.path.exists(path):
        return {"ok": False, "msg": f"Caminho não existe: {path}"}
    size_before = run(f"du -sh '{path}' 2>/dev/null | cut -f1")
    run(f"rm -rf '{path}'/*")
    return {"ok": True, "msg": f"Cache limpo. Tamanho antes: {size_before}"}

def api_duplicates(directory="~/Downloads"):
    directory = directory.replace("~", HOME)
    cmd = (
        f"find '{directory}' -type f 2>/dev/null | xargs md5 2>/dev/null"
        f" | sed 's/MD5 (\\(.*\\)) = \\(.*\\)/\\2 \\1/'"
        f" | sort | awk 'BEGIN{{h=\"\"}} {{if($1==h){{print p; print $0}} h=$1; p=$0}}'"
        f" | head -40"
    )
    out = run(cmd, timeout=120)
    groups = {}
    lines = [l for l in out.splitlines() if l.strip()]
    i = 0
    while i < len(lines):
        parts = lines[i].split(" ", 1)
        if len(parts) == 2:
            h, path = parts[0], parts[1]
            if h not in groups:
                groups[h] = []
            groups[h].append(path)
        i += 1
    result = []
    for h, paths in groups.items():
        if len(paths) > 1:
            size = run(f"du -sh '{paths[0]}' 2>/dev/null | cut -f1")
            result.append({"hash": h[:8], "size": size, "files": paths})
    return result

def api_old_downloads(days=30):
    cmd = (
        f"find {HOME}/Downloads -maxdepth 1 -type f -mtime +{days} 2>/dev/null"
        f" | xargs du -sh 2>/dev/null | sort -rh"
    )
    out = run(cmd)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            rows.append({"size": parts[0], "path": parts[1]})
    total_kb = run(
        f"find {HOME}/Downloads -maxdepth 1 -type f -mtime +{days} 2>/dev/null"
        f" | xargs du -sk 2>/dev/null | awk '{{s+=$1}} END{{print s}}'"
    )
    try:
        total_mb = round(int(total_kb) / 1024, 1)
        total_str = f"{total_mb} MB"
    except Exception:
        total_str = "?"
    return {"files": rows, "total": total_str}

def api_logs_heavy():
    cmd = (
        f"find {HOME}/Library/Logs /Library/Logs /var/log"
        f" -type f -name '*.log' 2>/dev/null"
        f" | xargs du -sh 2>/dev/null | sort -rh | head -20"
    )
    out = run(cmd)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            rows.append({"size": parts[0], "path": parts[1]})
    return rows

def api_apps_list():
    cmd = "du -sh /Applications/* ~/Applications/* 2>/dev/null | sort -rh | head -30"
    out = run(cmd)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            name = os.path.basename(parts[1])
            rows.append({"size": parts[0], "name": name, "path": parts[1]})
    return rows

def api_uninstall_apps(paths):
    removed = []
    errors = []
    residue_dirs = [
        f"{HOME}/Library/Application Support",
        f"{HOME}/Library/Preferences",
        f"{HOME}/Library/Containers",
        f"{HOME}/Library/Caches",
        f"{HOME}/Library/Logs",
        f"{HOME}/Library/Saved Application State",
    ]
    for app_path in paths:
        app_name = os.path.basename(app_path).replace(".app", "")
        bundle_id_prefix = app_name.lower().replace(" ", "")
        result = run(f"rm -rf '{app_path}'")
        removed.append(app_path)
        for rdir in residue_dirs:
            if not os.path.exists(rdir):
                continue
            out = run(f"find '{rdir}' -maxdepth 1 -iname '*{app_name}*' 2>/dev/null")
            for residue in out.splitlines():
                residue = residue.strip()
                if residue:
                    run(f"rm -rf '{residue}'")
                    removed.append(residue)
    return {"removed": removed, "errors": errors}

def api_docker_info():
    docker_bin = run("which docker").strip()
    if not docker_bin:
        return {"available": False}
    df_out = run("docker system df --format json 2>/dev/null || docker system df 2>/dev/null", timeout=15)
    raw = run("docker system df 2>/dev/null", timeout=15)
    lines = [l for l in raw.splitlines() if l.strip()]
    categories = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= 4:
            categories.append({
                "type": parts[0],
                "total": parts[1],
                "active": parts[2],
                "reclaimable": " ".join(parts[3:]),
            })
    return {"available": True, "categories": categories}

def api_docker_prune():
    out = run("docker system prune -a --volumes -f 2>&1", timeout=300)
    return {"ok": True, "output": out}

def api_quick_heavies():
    dirs = [
        f"{HOME}/Downloads", f"{HOME}/Desktop", f"{HOME}/Movies",
        f"{HOME}/Documents", f"{HOME}/Pictures", f"{HOME}/Music",
        f"{HOME}/.docker", f"{HOME}/Library/Developer",
        f"{HOME}/Library/Containers", f"{HOME}/Library/Caches",
    ]
    existing = " ".join(f"'{d}'" for d in dirs if os.path.exists(d))
    if not existing:
        return []
    cmd = (
        f"find {existing} -maxdepth 4 -type f -size +100M 2>/dev/null"
        f" | xargs du -sh 2>/dev/null | sort -rh | head -20"
    )
    out = run(cmd, timeout=60)
    rows = []
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            rows.append({"size": parts[0], "path": parts[1]})
    return rows

def _fmt_bytes(b):
    if b >= 1_073_741_824:
        return f"{b / 1_073_741_824:.1f} GB"
    elif b >= 1_048_576:
        return f"{b / 1_048_576:.1f} MB"
    elif b >= 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b} B"

def api_trash_info():
    names_raw = run("osascript -e 'tell application \"Finder\" to get name of every item in trash'", timeout=15)
    sizes_raw = run("osascript -e 'tell application \"Finder\" to get size of every item in trash'", timeout=15)
    count_str = run("osascript -e 'tell application \"Finder\" to get count of items in trash'", timeout=10)

    try:
        count = int(count_str.strip())
    except Exception:
        count = 0

    sizes = []
    for s in sizes_raw.split(","):
        try:
            sizes.append(int(s.strip()))
        except Exception:
            sizes.append(0)

    raw_parts = names_raw.split(", ")
    if len(raw_parts) == count:
        names = raw_parts
    else:
        parts = list(raw_parts)
        while len(parts) > count and len(parts) > 1:
            parts[-2] = parts[-2] + ", " + parts[-1]
            parts.pop()
        names = parts

    items = []
    total_bytes = 0
    for name, b in zip(names, sizes):
        name = name.strip()
        # Finder returns 0 for .app bundles and folders — fall back to du
        if b == 0:
            path = f"{HOME}/.Trash/{name}"
            kb_str = run(f"du -sk {repr(path)} 2>/dev/null | cut -f1")
            try:
                b = int(kb_str.strip()) * 1024
            except Exception:
                b = 0
        total_bytes += b
        items.append({"name": name, "size": _fmt_bytes(b) if b else "—"})

    return {"size": _fmt_bytes(total_bytes) if total_bytes else "0 B", "count": count, "items": items[:50]}

def api_empty_trash():
    size_before = run("osascript -e 'tell application \"Finder\" to get size of trash'", timeout=10)
    run("osascript -e 'tell application \"Finder\" to empty trash'", timeout=30)
    try:
        b = int(size_before.strip())
        if b >= 1_073_741_824:
            freed = f"{b / 1_073_741_824:.1f} GB"
        elif b >= 1_048_576:
            freed = f"{b / 1_048_576:.1f} MB"
        else:
            freed = f"{b / 1024:.1f} KB"
    except Exception:
        freed = "?"
    return {"ok": True, "msg": f"Lixeira esvaziada. Espaço liberado: {freed}"}

def api_delete_files(paths):
    deleted = []
    errors = []
    for p in paths:
        p = p.replace("~", HOME)
        try:
            run(f"rm -rf '{p}'")
            deleted.append(p)
        except Exception as e:
            errors.append({"path": p, "error": str(e)})
    return {"deleted": deleted, "errors": errors}

# ── HTTP server ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # quiet

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length:
            return json.loads(self.rfile.read(length))
        return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/":
            self.send_html(HTML)
        elif path == "/api/disk":
            self.send_json(api_disk())
        elif path == "/api/top-folders":
            self.send_json(api_top_folders())
        elif path == "/api/large":
            size = qs.get("size", ["100M"])[0]
            directory = qs.get("dir", ["~"])[0]
            self.send_json(api_large_files(size, directory))
        elif path == "/api/cache":
            self.send_json(api_cache_sizes())
        elif path == "/api/duplicates":
            directory = qs.get("dir", ["~/Downloads"])[0]
            self.send_json(api_duplicates(directory))
        elif path == "/api/downloads":
            days = int(qs.get("days", ["30"])[0])
            self.send_json(api_old_downloads(days))
        elif path == "/api/logs":
            self.send_json(api_logs_heavy())
        elif path == "/api/apps":
            self.send_json(api_apps_list())
        elif path == "/api/docker":
            self.send_json(api_docker_info())
        elif path == "/api/trash":
            self.send_json(api_trash_info())
        elif path == "/api/memory":
            self.send_json(api_memory())
        elif path == "/api/quick-heavies":
            self.send_json(api_quick_heavies())
        elif path == "/api/log":
            self.send_json(list(reversed(_log)))
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self.read_body()

        if path == "/api/cache/clean":
            cache_path = body.get("path", "")
            self.send_json(api_clean_cache(cache_path))
        elif path == "/api/delete":
            paths = body.get("paths", [])
            self.send_json(api_delete_files(paths))
        elif path == "/api/apps/uninstall":
            paths = body.get("paths", [])
            self.send_json(api_uninstall_apps(paths))
        elif path == "/api/trash/empty":
            self.send_json(api_empty_trash())
        elif path == "/api/kill":
            pid = body.get("pid")
            try:
                os.kill(int(pid), 15)  # SIGTERM
                self.send_json({"ok": True, "msg": f"Processo {pid} encerrado."})
            except Exception as e:
                self.send_json({"ok": False, "msg": str(e)})
        elif path == "/api/purge":
            out = run("osascript -e 'do shell script \"purge\" with administrator privileges'", timeout=60)
            self.send_json({"ok": True, "msg": "Cache de arquivos liberado." if not out.startswith("Err") else out})
        elif path == "/api/reboot":
            run("osascript -e 'tell application \"Finder\" to restart'")
            self.send_json({"ok": True})
        elif path == "/api/docker/prune":
            self.send_json(api_docker_prune())
        else:
            self.send_json({"error": "not found"}, 404)

# ── embedded HTML ─────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mac Cleaner</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@3.19.0/dist/tabler-icons.min.css">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#fff;--bg2:#f5f5f5;--bg3:#ececec;
  --text:#111;--text2:#555;--text3:#999;
  --border:#e0e0e0;--border2:#ccc;
  --blue:#185FA5;--blue-bg:#e6f1fb;--blue-text:#0c447c;
  --red:#A32D2D;--red-bg:#fcebeb;--red-text:#791f1f;
  --green:#3B6D11;--green-bg:#eaf3de;--green-text:#27500a;
  --amber:#854F0B;--amber-bg:#faeeda;--amber-text:#633806;
  --radius:8px;--radius-lg:12px;
}
@media(prefers-color-scheme:dark){
  :root{
    --bg:#1a1a1a;--bg2:#242424;--bg3:#2e2e2e;
    --text:#f0f0f0;--text2:#aaa;--text3:#666;
    --border:#333;--border2:#444;
    --blue:#378ADD;--blue-bg:#042c53;--blue-text:#85b7eb;
    --red:#E24B4A;--red-bg:#501313;--red-text:#f09595;
    --green:#639922;--green-bg:#173404;--green-text:#c0dd97;
    --amber:#BA7517;--amber-bg:#412402;--amber-text:#fac775;
  }
}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg3);color:var(--text);font-size:14px;min-height:100vh}
.app{display:grid;grid-template-columns:210px 1fr;min-height:100vh}
.sidebar{background:var(--bg2);border-right:1px solid var(--border);padding:20px 10px;display:flex;flex-direction:column;gap:2px;position:sticky;top:0;height:100vh;overflow-y:auto}
.logo{display:flex;align-items:center;gap:10px;padding:4px 10px 20px;font-size:15px;font-weight:600;color:var(--text)}
.logo i{font-size:20px;color:var(--blue)}
.nav-label{font-size:10px;font-weight:600;color:var(--text3);padding:4px 10px 6px;letter-spacing:.08em;text-transform:uppercase;margin-top:8px}
.nav-item{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:var(--radius);cursor:pointer;font-size:13px;color:var(--text2);border:none;background:transparent;width:100%;text-align:left;transition:all .15s}
.nav-item:hover{background:var(--bg3);color:var(--text)}
.nav-item.active{background:var(--blue-bg);color:var(--blue-text);font-weight:500}
.nav-item i{font-size:16px;flex-shrink:0;width:18px;text-align:center}
.content{padding:28px;max-width:860px}
.section{display:none;flex-direction:column;gap:18px}
.section.active{display:flex}
.page-title{font-size:20px;font-weight:600;color:var(--text)}
.page-sub{font-size:13px;color:var(--text2);margin-top:3px}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.btn{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border-radius:var(--radius);border:1px solid var(--border2);background:var(--bg);font-size:13px;font-weight:500;cursor:pointer;color:var(--text);transition:all .15s;white-space:nowrap}
.btn:hover{background:var(--bg2)}
.btn:active{transform:scale(.98)}
.btn-primary{background:var(--blue-bg);border-color:var(--blue);color:var(--blue-text)}
.btn-primary:hover{opacity:.85}
.btn-danger{background:var(--red-bg);border-color:var(--red);color:var(--red-text)}
.btn-danger:hover{opacity:.85}
.btn-sm{padding:5px 10px;font-size:12px}
select,input[type=text]{font-size:13px;padding:6px 10px;border-radius:var(--radius);border:1px solid var(--border2);background:var(--bg2);color:var(--text);outline:none}
select:focus,input[type=text]:focus{border-color:var(--blue)}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}
.metric{background:var(--bg2);border-radius:var(--radius);padding:14px;text-align:center}
.metric-val{font-size:22px;font-weight:600;color:var(--text)}
.metric-lbl{font-size:11px;color:var(--text3);margin-top:3px}
.disk-bar{height:12px;background:var(--bg3);border-radius:6px;overflow:hidden;display:flex;margin:4px 0 2px}
.disk-fill{height:100%;background:var(--blue);transition:width .5s}
.disk-warn{background:var(--red)}
.card{background:var(--bg);border:1px solid var(--border);border-radius:var(--radius-lg);overflow:hidden}
.card-header{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;border-bottom:1px solid var(--border);background:var(--bg2)}
.card-title{font-size:13px;font-weight:500}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:11px;font-weight:600;color:var(--text3);padding:8px 12px;background:var(--bg2);border-bottom:1px solid var(--border);text-transform:uppercase;letter-spacing:.05em}
td{padding:9px 12px;border-bottom:1px solid var(--border);vertical-align:middle}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--bg2)}
.mono{font-family:ui-monospace,monospace;font-size:12px;color:var(--text2)}
.size-badge{font-size:12px;font-weight:600;color:var(--text);min-width:56px;text-align:right}
.empty{text-align:center;padding:36px;color:var(--text3);font-size:13px}
.empty i{font-size:28px;display:block;margin-bottom:8px;opacity:.5}
.loading{text-align:center;padding:36px;color:var(--text3);font-size:13px}
.spinner{display:inline-block;width:18px;height:18px;border:2px solid var(--border2);border-top-color:var(--blue);border-radius:50%;animation:spin .7s linear infinite;vertical-align:-4px;margin-right:8px}
@keyframes spin{to{transform:rotate(360deg)}}
.confirm-bar{display:none;align-items:center;gap:10px;padding:12px 14px;border-radius:var(--radius);border:1px solid var(--red);background:var(--red-bg)}
.confirm-bar.visible{display:flex}
.confirm-msg{flex:1;font-size:13px;color:var(--red-text);font-weight:500}
.tag{display:inline-block;font-size:10px;padding:2px 7px;border-radius:20px;font-weight:600}
.tag-warn{background:var(--amber-bg);color:var(--amber-text)}
.tag-ok{background:var(--green-bg);color:var(--green-text)}
.chk{cursor:pointer;width:14px;height:14px;accent-color:var(--blue)}
.notice{font-size:12px;color:var(--text3);padding:6px 0}
.group-header{background:var(--blue-bg);color:var(--blue-text);padding:8px 12px;font-size:12px;font-weight:600;border-bottom:1px solid var(--border)}
</style>
</head>
<body>
<div class="app">
<nav class="sidebar">
  <div class="logo"><i class="ti ti-brand-apple" aria-hidden="true"></i> Mac Cleaner</div>
  <div class="nav-label">Painel</div>
  <button class="nav-item active" data-sec="overview" onclick="nav(this)"><i class="ti ti-home"></i> Visão geral</button>
  <div class="nav-label">Limpeza</div>
  <button class="nav-item" data-sec="large" onclick="nav(this)"><i class="ti ti-files"></i> Arquivos grandes</button>
  <button class="nav-item" data-sec="cache" onclick="nav(this)"><i class="ti ti-trash"></i> Cache</button>
  <button class="nav-item" data-sec="duplicates" onclick="nav(this)"><i class="ti ti-copy"></i> Duplicatas</button>
  <button class="nav-item" data-sec="logs" onclick="nav(this)"><i class="ti ti-file-text"></i> Logs</button>
  <button class="nav-item" data-sec="downloads" onclick="nav(this)"><i class="ti ti-download"></i> Downloads</button>
  <button class="nav-item" data-sec="trash" onclick="nav(this)"><i class="ti ti-trash"></i> Lixeira</button>
  <div class="nav-label">Sistema</div>
  <button class="nav-item" data-sec="apps" onclick="nav(this)"><i class="ti ti-apps"></i> Apps</button>
  <button class="nav-item" data-sec="docker" onclick="nav(this)"><i class="ti ti-brand-docker"></i> Docker</button>
  <button class="nav-item" data-sec="disk" onclick="nav(this)"><i class="ti ti-chart-pie"></i> Uso de disco</button>
  <div class="nav-label">Dev</div>
  <button class="nav-item" data-sec="log" onclick="nav(this);loadLog()"><i class="ti ti-terminal"></i> Log</button>
</nav>

<main class="content">

<!-- VISÃO GERAL -->
<div class="section active" id="sec-overview">
  <div>
    <div class="page-title">Visão geral</div>
    <div class="page-sub">Resumo do disco, memória e atalhos rápidos.</div>
  </div>

  <div id="health-alerts" style="display:flex;flex-direction:column;gap:8px"></div>

  <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em">Disco</div>
  <div class="metrics">
    <div class="metric"><div class="metric-val" id="ov-used">—</div><div class="metric-lbl">Usado</div></div>
    <div class="metric"><div class="metric-val" id="ov-free">—</div><div class="metric-lbl">Livre</div></div>
    <div class="metric"><div class="metric-val" id="ov-pct">—</div><div class="metric-lbl">% utilizado</div></div>
  </div>
  <div class="disk-bar"><div class="disk-fill" id="ov-bar" style="width:0%"></div></div>

  <div style="font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin-top:4px">Memória RAM</div>
  <div class="metrics">
    <div class="metric"><div class="metric-val" id="mem-total">—</div><div class="metric-lbl">Total</div></div>
    <div class="metric"><div class="metric-val" id="mem-used">—</div><div class="metric-lbl">Em uso real</div></div>
    <div class="metric"><div class="metric-val" id="mem-cached">—</div><div class="metric-lbl">Cache (inativa)</div></div>
    <div class="metric"><div class="metric-val" id="mem-free">—</div><div class="metric-lbl">Livre</div></div>
  </div>
  <div class="disk-bar"><div class="disk-fill" id="mem-bar" style="width:0%"></div></div>
  <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:2px" id="mem-detail"></div>
  <div id="reboot-estimate" style="display:none;border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;display:flex;align-items:center;gap:16px;flex-wrap:wrap">
    <div style="flex:1;min-width:200px">
      <div style="font-size:13px;font-weight:500">Estimativa após reiniciar</div>
      <div style="font-size:12px;color:var(--text2);margin-top:2px">Libera toda memória de apps, cache e comprimida — só o sistema permanece (~3.2 GB).</div>
    </div>
    <div style="text-align:center;min-width:80px">
      <div style="font-size:22px;font-weight:600;color:var(--green-text)" id="reboot-freed-val">—</div>
      <div style="font-size:11px;color:var(--text3)">serão liberados</div>
    </div>
    <button class="btn btn-danger" onclick="confirmReboot()"><i class="ti ti-refresh"></i> Reiniciar agora</button>
  </div>

  <div class="card">
    <div class="card-header">
      <span class="card-title"><i class="ti ti-cpu" aria-hidden="true"></i> Processos por uso de memória</span>
      <button class="btn btn-sm" onclick="loadMemory()"><i class="ti ti-refresh"></i> Atualizar</button>
    </div>
    <div id="mem-procs-body"><div class="loading"><span class="spinner"></span>Carregando...</div></div>
  </div>

  <div class="card">
    <div class="card-header">
      <span class="card-title"><i class="ti ti-folder" aria-hidden="true"></i> Maiores pastas em ~</span>
      <button class="btn btn-sm" onclick="loadTopFolders()"><i class="ti ti-refresh"></i> Atualizar</button>
    </div>
    <div id="top-folders-body"><div class="loading"><span class="spinner"></span>Carregando...</div></div>
  </div>

  <div class="card">
    <div class="card-header">
      <span class="card-title"><i class="ti ti-file-alert" aria-hidden="true"></i> Arquivos pesados detectados (&gt;100 MB)</span>
      <button class="btn btn-sm" onclick="loadQuickHeavies()"><i class="ti ti-refresh"></i> Atualizar</button>
    </div>
    <div id="quick-heavies-body"><div class="loading"><span class="spinner"></span>Escaneando...</div></div>
  </div>
</div>

<!-- ARQUIVOS GRANDES -->
<div class="section" id="sec-large">
  <div>
    <div class="page-title">Arquivos grandes</div>
    <div class="page-sub">Localiza arquivos acima do tamanho mínimo definido.</div>
  </div>
  <div class="row">
    <label style="font-size:13px;color:var(--text2)">Mínimo:</label>
    <select id="large-size">
      <option value="50M">50 MB</option>
      <option value="100M" selected>100 MB</option>
      <option value="500M">500 MB</option>
      <option value="1G">1 GB</option>
    </select>
    <label style="font-size:13px;color:var(--text2)">Em:</label>
    <select id="large-dir">
      <option value="~">Home (~)</option>
      <option value="~/Downloads">Downloads</option>
      <option value="~/Documents">Documents</option>
      <option value="~/Desktop">Desktop</option>
    </select>
    <button class="btn btn-primary" onclick="scanLarge()"><i class="ti ti-search"></i> Escanear</button>
  </div>
  <div class="card" id="large-card" style="display:none">
    <div class="card-header">
      <span class="card-title" id="large-title">Resultados</span>
      <button class="btn btn-sm btn-danger" onclick="showConfirmLarge()"><i class="ti ti-trash"></i> Deletar selecionados</button>
    </div>
    <div id="large-body"></div>
  </div>
  <div class="confirm-bar" id="large-confirm">
    <div class="confirm-msg"><i class="ti ti-alert-triangle"></i> Deletar os arquivos selecionados permanentemente?</div>
    <button class="btn btn-sm" onclick="document.getElementById('large-confirm').classList.remove('visible')">Cancelar</button>
    <button class="btn btn-sm btn-danger" onclick="deleteLarge()"><i class="ti ti-trash"></i> Confirmar</button>
  </div>
  <div class="notice">Os arquivos deletados não vão para a Lixeira.</div>
</div>

<!-- CACHE -->
<div class="section" id="sec-cache">
  <div>
    <div class="page-title">Cache</div>
    <div class="page-sub">Cache de apps, sistema e browsers acumulado.</div>
  </div>
  <div class="row">
    <button class="btn btn-primary" onclick="loadCache()"><i class="ti ti-refresh"></i> Ver tamanhos</button>
  </div>
  <div class="card">
    <div class="card-header"><span class="card-title">Pastas de cache</span></div>
    <div id="cache-body"><div class="empty"><i class="ti ti-search"></i>Clique em "Ver tamanhos" para carregar.</div></div>
  </div>
  <div class="confirm-bar" id="cache-confirm">
    <div class="confirm-msg" id="cache-confirm-msg"><i class="ti ti-alert-triangle"></i></div>
    <button class="btn btn-sm" onclick="document.getElementById('cache-confirm').classList.remove('visible')">Cancelar</button>
    <button class="btn btn-sm btn-danger" onclick="confirmCleanCache()"><i class="ti ti-trash"></i> Limpar</button>
  </div>
</div>

<!-- DUPLICATAS -->
<div class="section" id="sec-duplicates">
  <div>
    <div class="page-title">Duplicatas</div>
    <div class="page-sub">Encontra arquivos com conteúdo idêntico (hash MD5).</div>
  </div>
  <div class="row">
    <label style="font-size:13px;color:var(--text2)">Pasta:</label>
    <select id="dup-dir">
      <option value="~/Downloads">Downloads</option>
      <option value="~/Documents">Documents</option>
      <option value="~/Desktop">Desktop</option>
      <option value="~">Home inteiro (lento)</option>
    </select>
    <button class="btn btn-primary" onclick="scanDups()"><i class="ti ti-search"></i> Escanear</button>
  </div>
  <div class="notice">Pode demorar alguns minutos dependendo do número de arquivos.</div>
  <div class="card" id="dup-card" style="display:none">
    <div class="card-header"><span class="card-title" id="dup-title">Grupos de duplicatas</span></div>
    <div id="dup-body"></div>
  </div>
</div>

<!-- LOGS -->
<div class="section" id="sec-logs">
  <div>
    <div class="page-title">Logs</div>
    <div class="page-sub">Arquivos de log do sistema e apps que podem ser removidos.</div>
  </div>
  <div class="row">
    <button class="btn btn-primary" onclick="scanLogs()"><i class="ti ti-search"></i> Escanear logs pesados</button>
  </div>
  <div class="card" id="logs-card" style="display:none">
    <div class="card-header">
      <span class="card-title" id="logs-title">Logs mais pesados</span>
      <button class="btn btn-sm btn-danger" onclick="showConfirmLogs()"><i class="ti ti-trash"></i> Deletar selecionados</button>
    </div>
    <div id="logs-body"></div>
  </div>
  <div class="confirm-bar" id="logs-confirm">
    <div class="confirm-msg"><i class="ti ti-alert-triangle"></i> Deletar logs selecionados permanentemente?</div>
    <button class="btn btn-sm" onclick="document.getElementById('logs-confirm').classList.remove('visible')">Cancelar</button>
    <button class="btn btn-sm btn-danger" onclick="deleteLogs()"><i class="ti ti-trash"></i> Confirmar</button>
  </div>
</div>

<!-- DOWNLOADS -->
<div class="section" id="sec-downloads">
  <div>
    <div class="page-title">Downloads antigos</div>
    <div class="page-sub">Arquivos esquecidos na pasta Downloads.</div>
  </div>
  <div class="row">
    <label style="font-size:13px;color:var(--text2)">Mais antigos que:</label>
    <select id="dl-days">
      <option value="7">7 dias</option>
      <option value="30" selected>30 dias</option>
      <option value="60">60 dias</option>
      <option value="90">90 dias</option>
    </select>
    <button class="btn btn-primary" onclick="scanDownloads()"><i class="ti ti-search"></i> Escanear</button>
  </div>
  <div class="card" id="dl-card" style="display:none">
    <div class="card-header">
      <span class="card-title" id="dl-title">Downloads encontrados</span>
      <button class="btn btn-sm btn-danger" onclick="showConfirmDl()"><i class="ti ti-trash"></i> Deletar selecionados</button>
    </div>
    <div id="dl-body"></div>
  </div>
  <div class="confirm-bar" id="dl-confirm">
    <div class="confirm-msg"><i class="ti ti-alert-triangle"></i> Deletar downloads selecionados permanentemente?</div>
    <button class="btn btn-sm" onclick="document.getElementById('dl-confirm').classList.remove('visible')">Cancelar</button>
    <button class="btn btn-sm btn-danger" onclick="deleteDownloads()"><i class="ti ti-trash"></i> Confirmar</button>
  </div>
</div>

<!-- LIXEIRA -->
<div class="section" id="sec-trash">
  <div>
    <div class="page-title">Lixeira</div>
    <div class="page-sub">Conteúdo atual de ~/.Trash.</div>
  </div>
  <div class="row">
    <button class="btn btn-primary" onclick="loadTrash()"><i class="ti ti-refresh"></i> Verificar lixeira</button>
    <button class="btn btn-danger" onclick="showConfirmTrash()"><i class="ti ti-trash"></i> Esvaziar lixeira</button>
  </div>
  <div class="metrics" id="trash-metrics" style="display:none">
    <div class="metric"><div class="metric-val" id="trash-size">—</div><div class="metric-lbl">Tamanho total</div></div>
    <div class="metric"><div class="metric-val" id="trash-count">—</div><div class="metric-lbl">Itens</div></div>
  </div>
  <div class="card" id="trash-card" style="display:none">
    <div class="card-header"><span class="card-title">Itens na lixeira</span></div>
    <div id="trash-body"></div>
  </div>
  <div class="confirm-bar" id="trash-confirm">
    <div class="confirm-msg"><i class="ti ti-alert-triangle"></i> Esvaziar a lixeira permanentemente? Os arquivos não poderão ser recuperados.</div>
    <button class="btn btn-sm" onclick="document.getElementById('trash-confirm').classList.remove('visible')">Cancelar</button>
    <button class="btn btn-sm btn-danger" onclick="emptyTrash()"><i class="ti ti-trash"></i> Esvaziar</button>
  </div>
</div>

<!-- APPS -->
<div class="section" id="sec-apps">
  <div>
    <div class="page-title">Apps instalados</div>
    <div class="page-sub">Selecione apps para desinstalar — remove o bundle e resíduos em Library.</div>
  </div>
  <div class="row">
    <button class="btn btn-primary" onclick="loadApps()"><i class="ti ti-search"></i> Listar apps</button>
    <button class="btn btn-danger" onclick="showConfirmApps()"><i class="ti ti-trash"></i> Desinstalar selecionados</button>
  </div>
  <div class="card" id="apps-card" style="display:none">
    <div class="card-header">
      <span class="card-title">Apps em /Applications</span>
      <label style="font-size:12px;color:var(--text3);display:flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" id="apps-check-all" onchange="toggleAllApps(this.checked)"> Selecionar todos
      </label>
    </div>
    <div id="apps-body"></div>
  </div>
  <div class="confirm-bar" id="apps-confirm">
    <div class="confirm-msg"><i class="ti ti-alert-triangle"></i> Desinstalar apps selecionados e remover resíduos? Esta ação não pode ser desfeita.</div>
    <button class="btn btn-sm" onclick="document.getElementById('apps-confirm').classList.remove('visible')">Cancelar</button>
    <button class="btn btn-sm btn-danger" onclick="uninstallApps()"><i class="ti ti-trash"></i> Confirmar desinstalação</button>
  </div>
  <div class="notice">Remove o .app e arquivos residuais em ~/Library (Preferences, Caches, Containers, etc.).</div>
</div>

<!-- DOCKER -->
<div class="section" id="sec-docker">
  <div>
    <div class="page-title">Docker</div>
    <div class="page-sub">Libera espaço removendo imagens, containers parados, volumes e build cache.</div>
  </div>
  <div class="row">
    <button class="btn btn-primary" onclick="loadDocker()"><i class="ti ti-refresh"></i> Ver uso de disco</button>
  </div>
  <div class="card" id="docker-card" style="display:none">
    <div class="card-header"><span class="card-title">Docker — uso atual</span></div>
    <div id="docker-body"></div>
  </div>
  <div class="card" style="border-color:var(--red)">
    <div class="card-header" style="background:var(--red-bg)">
      <span class="card-title" style="color:var(--red-text)"><i class="ti ti-alert-triangle"></i> Limpeza completa</span>
    </div>
    <div style="padding:16px;display:flex;flex-direction:column;gap:10px">
      <div style="font-size:13px;color:var(--text2)">Executa <code style="background:var(--bg3);padding:2px 6px;border-radius:4px;font-size:12px">docker system prune -a --volumes</code> — remove <b>todas</b> as imagens não utilizadas, containers parados, redes e volumes.</div>
      <div>
        <button class="btn btn-danger" onclick="showConfirmDocker()"><i class="ti ti-trash"></i> Limpar tudo do Docker</button>
      </div>
    </div>
  </div>
  <div class="confirm-bar" id="docker-confirm">
    <div class="confirm-msg"><i class="ti ti-alert-triangle"></i> Isso remove TODAS imagens, containers parados, volumes e redes não utilizados. Não pode ser desfeito.</div>
    <button class="btn btn-sm" onclick="document.getElementById('docker-confirm').classList.remove('visible')">Cancelar</button>
    <button class="btn btn-sm btn-danger" onclick="pruneDocker()"><i class="ti ti-trash"></i> Confirmar limpeza</button>
  </div>
</div>

<!-- LOG -->
<div class="section" id="sec-log">
  <div>
    <div class="page-title">Log de comandos</div>
    <div class="page-sub">Todos os comandos shell executados desde o início do servidor, com duração e resultado.</div>
  </div>
  <div class="row">
    <button class="btn btn-primary" onclick="loadLog()"><i class="ti ti-refresh"></i> Atualizar</button>
    <button class="btn" onclick="document.getElementById('log-filter').value='';loadLog()"><i class="ti ti-x"></i> Limpar filtro</button>
    <input type="text" id="log-filter" placeholder="Filtrar por comando..." style="flex:1;max-width:320px" oninput="filterLog()">
  </div>
  <div class="card">
    <div class="card-header"><span class="card-title" id="log-title">Aguardando...</span></div>
    <div id="log-body"><div class="empty"><i class="ti ti-terminal"></i>Clique em Atualizar para carregar.</div></div>
  </div>
</div>

<!-- DISCO -->
<div class="section" id="sec-disk">
  <div>
    <div class="page-title">Uso de disco</div>
    <div class="page-sub">Relatório por pasta do diretório home.</div>
  </div>
  <div class="row">
    <button class="btn btn-primary" onclick="loadDisk()"><i class="ti ti-refresh"></i> Carregar relatório</button>
  </div>
  <div class="card" id="disk-card" style="display:none">
    <div class="card-header"><span class="card-title">Pastas em ~</span></div>
    <div id="disk-body"></div>
  </div>
</div>

</main>
</div>

<script>
const API = 'http://localhost:""" + str(PORT) + """';

function nav(el) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('sec-' + el.dataset.sec).classList.add('active');
}

async function get(path) {
  const r = await fetch(API + path);
  return r.json();
}
async function post(path, body) {
  const r = await fetch(API + path, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  return r.json();
}

function loading(id) {
  document.getElementById(id).innerHTML = '<div class="loading"><span class="spinner"></span>Carregando...</div>';
}

function fileTable(rows, idPrefix, showCheck=true) {
  if (!rows.length) return '<div class="empty"><i class="ti ti-mood-happy"></i>Nenhum arquivo encontrado.</div>';
  let h = '<table><thead><tr>' + (showCheck ? '<th style="width:32px"></th>' : '') + '<th>Caminho</th><th style="text-align:right">Tamanho</th></tr></thead><tbody>';
  rows.forEach((r, i) => {
    const id = idPrefix + i;
    h += `<tr><td><input class="chk" type="checkbox" id="${id}" data-path="${r.path}" checked></td><td class="mono">${r.path.replace('/Users/' + location.hostname, '~')}</td><td class="size-badge">${r.size}</td></tr>`;
  });
  return h + '</tbody></table>';
}

function getChecked(prefix) {
  return [...document.querySelectorAll(`[id^="${prefix}"]`)].filter(c => c.checked).map(c => c.dataset.path);
}

// Visão geral
async function loadOverview() {
  const d = await get('/api/disk');
  document.getElementById('ov-used').textContent = d.used || '?';
  document.getElementById('ov-free').textContent = d.free || '?';
  document.getElementById('ov-pct').textContent = d.pct || '?';
  const pct = parseInt(d.pct) || 0;
  const bar = document.getElementById('ov-bar');
  bar.style.width = pct + '%';
  if (pct > 85) bar.classList.add('disk-warn');
  updateHealthAlerts(pct, d.free);
  loadMemory();
  loadTopFolders();
  loadQuickHeavies();
  checkDockerAlert();
}

function updateHealthAlerts(diskPct, diskFree) {
  const el = document.getElementById('health-alerts');
  let html = '';
  if (diskPct >= 90) {
    html += `<div style="display:flex;align-items:center;gap:14px;padding:14px 16px;background:var(--red-bg);border:2px solid var(--red);border-radius:var(--radius-lg)">
      <i class="ti ti-alert-triangle" style="font-size:32px;color:var(--red-text);flex-shrink:0"></i>
      <div style="flex:1">
        <div style="font-size:15px;font-weight:700;color:var(--red-text)">Disco crítico — ${diskPct}% usado (${diskFree} livres)</div>
        <div style="font-size:12px;color:var(--red-text);opacity:.85;margin-top:4px">O Mac pode travar ou exibir erros com menos de 5 GB livres. Verifique imagens Docker, Downloads e arquivos pesados abaixo.</div>
      </div>
      <button class="btn btn-sm btn-danger" onclick="nav(document.querySelector('[data-sec=large]'))">Ver arquivos</button>
    </div>`;
  } else if (diskPct >= 80) {
    html += `<div style="display:flex;align-items:center;gap:14px;padding:12px 16px;background:var(--amber-bg);border:1px solid var(--amber);border-radius:var(--radius-lg)">
      <i class="ti ti-alert-circle" style="font-size:24px;color:var(--amber-text);flex-shrink:0"></i>
      <div style="flex:1">
        <div style="font-size:13px;font-weight:600;color:var(--amber-text)">Disco quase cheio — ${diskPct}% usado (${diskFree} livres)</div>
        <div style="font-size:12px;color:var(--amber-text);opacity:.85;margin-top:2px">Considere limpar Downloads, Docker ou arquivos pesados em breve.</div>
      </div>
    </div>`;
  }
  el.innerHTML = html;
}

async function checkDockerAlert() {
  try {
    const d = await get('/api/docker');
    if (!d.available || !d.categories || !d.categories.length) return;
    let totalGB = 0;
    d.categories.forEach(c => {
      const m = c.reclaimable.match(/([\\d.]+)\\s*(GB|MB)/i);
      if (m) totalGB += m[2].toLowerCase() === 'gb' ? parseFloat(m[1]) : parseFloat(m[1]) / 1024;
    });
    if (totalGB < 1) return;
    const el = document.getElementById('health-alerts');
    el.innerHTML += `<div style="display:flex;align-items:center;gap:14px;padding:12px 16px;background:var(--blue-bg);border:1px solid var(--blue);border-radius:var(--radius-lg)">
      <i class="ti ti-brand-docker" style="font-size:26px;color:var(--blue-text);flex-shrink:0"></i>
      <div style="flex:1">
        <div style="font-size:13px;font-weight:600;color:var(--blue-text)">Docker: ~${totalGB.toFixed(1)} GB recuperáveis</div>
        <div style="font-size:12px;color:var(--blue-text);opacity:.85;margin-top:2px">Imagens, containers parados e volumes não utilizados podem ser removidos com segurança.</div>
      </div>
      <button class="btn btn-sm btn-primary" onclick="nav(document.querySelector('[data-sec=docker]'))">Limpar Docker</button>
    </div>`;
  } catch(e) {}
}

async function loadQuickHeavies() {
  document.getElementById('quick-heavies-body').innerHTML = '<div class="loading"><span class="spinner"></span>Escaneando Downloads, Desktop, Movies, Documents, Pictures, Library/Developer...</div>';
  const rows = await get('/api/quick-heavies');
  if (!rows.length) {
    document.getElementById('quick-heavies-body').innerHTML = '<div class="empty"><i class="ti ti-mood-happy"></i>Nenhum arquivo acima de 100 MB encontrado.</div>';
    return;
  }
  let h = '<table><thead><tr><th>Arquivo</th><th style="text-align:right">Tamanho</th></tr></thead><tbody>';
  rows.forEach(r => {
    const parts = r.path.split('/');
    const name = parts.pop();
    const dir = parts.join('/');
    h += `<tr><td><div style="font-weight:500;max-width:460px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.path}">${name}</div><div class="mono" style="font-size:11px;color:var(--text3)">${dir}</div></td><td class="size-badge">${r.size}</td></tr>`;
  });
  document.getElementById('quick-heavies-body').innerHTML = h + '</tbody></table>';
}

async function loadMemory() {
  const m = await get('/api/memory');
  document.getElementById('mem-total').textContent = m.total || '?';
  document.getElementById('mem-used').textContent = m.used || '?';
  document.getElementById('mem-cached').textContent = m.cached || '?';
  document.getElementById('mem-free').textContent = m.free || '?';
  const bar = document.getElementById('mem-bar');
  bar.style.width = (m.pct || 0) + '%';
  if (m.pct > 85) bar.classList.add('disk-warn');
  // Reboot estimate card
  const rebootEl = document.getElementById('reboot-estimate');
  rebootEl.style.display = 'flex';
  document.getElementById('reboot-freed-val').textContent = `~${m.reboot_freed}`;
  rebootEl.style.borderColor = m.reboot_freed_pct > 40 ? 'var(--green-text)' : 'var(--border)';
  const canFree = m.file_cache;
  document.getElementById('mem-detail').innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;width:100%">
      <div style="background:var(--bg2);border-radius:var(--radius);padding:10px 12px">
        <div style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px">Breakdown</div>
        <div style="display:flex;flex-direction:column;gap:4px;font-size:12px">
          <div style="display:flex;justify-content:space-between"><span style="color:var(--text2)">Apps (active)</span><b>${m.active}</b></div>
          <div style="display:flex;justify-content:space-between"><span style="color:var(--text2)">Wired / kernel</span><b>${m.wired}</b></div>
          <div style="display:flex;justify-content:space-between"><span style="color:var(--text2)">Comprimida</span><b>${m.compressed}</b></div>
          <div style="border-top:0.5px solid var(--border);margin:4px 0"></div>
          <div style="display:flex;justify-content:space-between"><span style="color:var(--text3)">Cache inativa <span style="font-weight:400">(libera automático)</span></span><span style="color:var(--text3)">${m.inactive}</span></div>
          <div style="display:flex;justify-content:space-between"><span style="color:var(--text3)">File-backed cache</span><span style="color:var(--text3)">${m.file_cache}</span></div>
        </div>
      </div>
      <div style="background:var(--bg2);border-radius:var(--radius);padding:10px 12px">
        <div style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin-bottom:6px">GPU</div>
        <div style="display:flex;flex-direction:column;gap:4px;font-size:12px">
          <div style="display:flex;justify-content:space-between"><span style="color:var(--text2)">Alocada</span><b>${m.gpu_alloc}</b></div>
          <div style="display:flex;justify-content:space-between"><span style="color:var(--text2)">Em uso ativo</span><b>${m.gpu_used}</b></div>
        </div>
        <div style="font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.07em;margin:10px 0 6px">O que pode ser liberado</div>
        <div style="display:flex;flex-direction:column;gap:4px;font-size:12px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="color:var(--text2)">Cache de arquivos</span>
            <button class="btn btn-sm btn-danger" onclick="purgeCache()" style="font-size:11px;padding:3px 8px"><i class="ti ti-bolt"></i> Purge</button>
          </div>
          <div style="font-size:10px;color:var(--text3)">Pede senha de administrador. Wired e comprimida não podem ser liberadas sem reiniciar.</div>
        </div>
      </div>
    </div>`;
  const procs = m.processes || [];
  if (!procs.length) { document.getElementById('mem-procs-body').innerHTML = '<div class="empty">Sem dados.</div>'; return; }
  const maxRss = procs[0].rss_b;
  let h = '<table><thead><tr><th>Processo</th><th style="text-align:right">CPU%</th><th style="text-align:right">RAM</th><th style="width:26%"></th><th style="width:60px"></th></tr></thead><tbody>';
  procs.forEach(p => {
    const w = Math.round(p.rss_b / maxRss * 100);
    const mainPid = p.pids && p.pids[0] ? p.pids[0].pid : null;
    const allPids = p.pids ? p.pids.map(x => x.pid) : [];
    const pidStr = allPids.length > 1 ? `${allPids.length} proc.` : `PID ${mainPid}`;
    const cpuStr = p.cpu > 0 ? `${p.cpu}%` : '—';
    const cpuColor = p.cpu > 50 ? 'var(--red-text)' : p.cpu > 15 ? 'var(--amber-text)' : 'var(--text2)';
    h += `<tr>
      <td>
        <div style="font-weight:500;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${p.name}">${p.name}</div>
        <div style="font-size:10px;color:var(--text3)">${pidStr}</div>
      </td>
      <td style="text-align:right;font-size:12px;font-weight:600;color:${cpuColor}">${cpuStr}</td>
      <td class="size-badge">${p.rss}</td>
      <td><div style="height:6px;background:var(--bg3);border-radius:3px;overflow:hidden"><div style="height:100%;width:${w}%;background:var(--blue)"></div></div></td>
      <td style="text-align:right"><button class="btn btn-sm" style="font-size:11px;padding:3px 8px;color:var(--red-text);border-color:var(--red)" onclick='killProc(${JSON.stringify(allPids)}, "${p.name}")' title="Encerrar processo"><i class="ti ti-x"></i></button></td>
    </tr>`;
  });
  h += `<tr><td colspan="4" style="font-size:11px;color:var(--text3);padding:8px 12px">Soma: <b>${m.procs_total}</b> de <b>${m.used}</b> em uso total</td></tr>`;
  document.getElementById('mem-procs-body').innerHTML = h + '</tbody></table>';
}

async function loadTopFolders() {
  loading('top-folders-body');
  const rows = await get('/api/top-folders');
  if (!rows.length) { document.getElementById('top-folders-body').innerHTML = '<div class="empty">Sem dados.</div>'; return; }
  let h = '<table><thead><tr><th>Pasta</th><th style="text-align:right">Tamanho</th></tr></thead><tbody>';
  rows.forEach(r => { h += `<tr><td class="mono">${r.path}</td><td class="size-badge">${r.size}</td></tr>`; });
  document.getElementById('top-folders-body').innerHTML = h + '</tbody></table>';
}

// Arquivos grandes
async function scanLarge() {
  const size = document.getElementById('large-size').value;
  const dir = document.getElementById('large-dir').value;
  document.getElementById('large-card').style.display = 'block';
  loading('large-body');
  const rows = await get(`/api/large?size=${size}&dir=${encodeURIComponent(dir)}`);
  document.getElementById('large-title').textContent = `${rows.length} arquivo(s) encontrado(s)`;
  document.getElementById('large-body').innerHTML = fileTable(rows, 'lf-');
}
function showConfirmLarge() { document.getElementById('large-confirm').classList.add('visible'); }
async function deleteLarge() {
  const paths = getChecked('lf-');
  if (!paths.length) return;
  const r = await post('/api/delete', {paths});
  alert(`Deletados: ${r.deleted.length} arquivo(s).${r.errors.length ? '\\nErros: ' + r.errors.map(e=>e.path).join(', ') : ''}`);
  document.getElementById('large-confirm').classList.remove('visible');
  scanLarge();
}

// Cache
let _cacheTarget = null;
async function loadCache() {
  loading('cache-body');
  const rows = await get('/api/cache');
  let h = '<table><thead><tr><th>Cache</th><th>Caminho</th><th style="text-align:right">Tamanho</th><th style="width:100px"></th></tr></thead><tbody>';
  rows.forEach(r => {
    const btn = r.exists
      ? `<button class="btn btn-sm btn-danger" onclick="askCleanCache('${r.path}', '${r.label}')"><i class="ti ti-trash"></i> Limpar</button>`
      : '<span style="font-size:12px;color:var(--text3)">Não encontrado</span>';
    h += `<tr><td style="font-weight:500">${r.label}</td><td class="mono">${r.path.replace('/Users/', '~/')}</td><td class="size-badge">${r.size}</td><td>${btn}</td></tr>`;
  });
  document.getElementById('cache-body').innerHTML = h + '</tbody></table>';
}
function askCleanCache(path, label) {
  _cacheTarget = path;
  document.getElementById('cache-confirm-msg').innerHTML = `<i class="ti ti-alert-triangle"></i> Limpar cache de <b>${label}</b>? (${path.replace('/Users/','~/')})`;
  document.getElementById('cache-confirm').classList.add('visible');
}
async function confirmCleanCache() {
  if (!_cacheTarget) return;
  const r = await post('/api/cache/clean', {path: _cacheTarget});
  alert(r.msg);
  document.getElementById('cache-confirm').classList.remove('visible');
  _cacheTarget = null;
  loadCache();
}

// Duplicatas
async function scanDups() {
  const dir = document.getElementById('dup-dir').value;
  document.getElementById('dup-card').style.display = 'block';
  loading('dup-body');
  const groups = await get(`/api/duplicates?dir=${encodeURIComponent(dir)}`);
  document.getElementById('dup-title').textContent = `${groups.length} grupo(s) de duplicatas`;
  if (!groups.length) {
    document.getElementById('dup-body').innerHTML = '<div class="empty"><i class="ti ti-mood-happy"></i>Nenhuma duplicata encontrada.</div>';
    return;
  }
  let h = '';
  groups.forEach((g, gi) => {
    h += `<div class="group-header">Grupo ${gi+1} · ${g.size} cada · hash: ${g.hash}…</div>`;
    g.files.forEach((f, fi) => {
      const isFirst = fi === 0;
      h += `<div style="display:flex;align-items:center;gap:10px;padding:9px 12px;border-bottom:1px solid var(--border)">
        <input class="chk" type="checkbox" id="dup-${gi}-${fi}" data-path="${f}" ${isFirst ? '' : 'checked'}>
        <span class="mono" style="flex:1">${f}</span>
        ${isFirst ? '<span class="tag tag-ok">manter</span>' : ''}
      </div>`;
    });
  });
  document.getElementById('dup-body').innerHTML = h;
}

// Logs
async function scanLogs() {
  document.getElementById('logs-card').style.display = 'block';
  loading('logs-body');
  const rows = await get('/api/logs');
  document.getElementById('logs-title').textContent = `${rows.length} log(s) mais pesados`;
  document.getElementById('logs-body').innerHTML = fileTable(rows, 'log-');
}
function showConfirmLogs() { document.getElementById('logs-confirm').classList.add('visible'); }
async function deleteLogs() {
  const paths = getChecked('log-');
  if (!paths.length) return;
  const r = await post('/api/delete', {paths});
  alert(`Deletados: ${r.deleted.length} arquivo(s).`);
  document.getElementById('logs-confirm').classList.remove('visible');
  scanLogs();
}

// Downloads
async function scanDownloads() {
  const days = document.getElementById('dl-days').value;
  document.getElementById('dl-card').style.display = 'block';
  loading('dl-body');
  const data = await get(`/api/downloads?days=${days}`);
  document.getElementById('dl-title').textContent = `${data.files.length} arquivo(s) · total: ${data.total}`;
  document.getElementById('dl-body').innerHTML = fileTable(data.files, 'dl-');
}
function showConfirmDl() { document.getElementById('dl-confirm').classList.add('visible'); }
async function deleteDownloads() {
  const paths = getChecked('dl-');
  if (!paths.length) return;
  const r = await post('/api/delete', {paths});
  alert(`Deletados: ${r.deleted.length} arquivo(s).`);
  document.getElementById('dl-confirm').classList.remove('visible');
  scanDownloads();
}

// Apps
async function loadApps() {
  document.getElementById('apps-card').style.display = 'block';
  loading('apps-body');
  const rows = await get('/api/apps');
  if (!rows.length) { document.getElementById('apps-body').innerHTML = '<div class="empty">Sem dados.</div>'; return; }
  let h = '<table><thead><tr><th style="width:32px"></th><th>App</th><th>Caminho</th><th style="text-align:right">Tamanho</th></tr></thead><tbody>';
  rows.forEach((r, i) => {
    h += `<tr><td><input class="chk" type="checkbox" id="app-${i}" data-path="${r.path}"></td><td style="font-weight:500">${r.name}</td><td class="mono">${r.path}</td><td class="size-badge">${r.size}</td></tr>`;
  });
  document.getElementById('apps-body').innerHTML = h + '</tbody></table>';
}
function toggleAllApps(checked) {
  document.querySelectorAll('[id^="app-"]').forEach(c => c.checked = checked);
}
function showConfirmApps() {
  const sel = document.querySelectorAll('[id^="app-"]:checked');
  if (!sel.length) { alert('Selecione ao menos um app.'); return; }
  document.getElementById('apps-confirm').classList.add('visible');
}
async function uninstallApps() {
  const paths = [...document.querySelectorAll('[id^="app-"]:checked')].map(c => c.dataset.path);
  if (!paths.length) return;
  document.getElementById('apps-confirm').classList.remove('visible');
  loading('apps-body');
  const r = await post('/api/apps/uninstall', {paths});
  alert(`Desinstalados: ${paths.length} app(s).\\nArquivos removidos: ${r.removed.length}`);
  loadApps();
}

async function confirmReboot() {
  if (!confirm('Reiniciar o Mac agora? O macOS pedirá confirmação antes de reiniciar.')) return;
  await post('/api/reboot', {});
}

async function killProc(pids, name) {
  if (!confirm(`Encerrar "${name}" (${pids.length} processo(s))?`)) return;
  let ok = 0, fail = 0;
  for (const pid of pids) {
    const r = await post('/api/kill', {pid});
    r.ok ? ok++ : fail++;
  }
  alert(`Encerrado: ${ok} processo(s).${fail ? ' Falhas: ' + fail : ''}`);
  loadMemory();
}

async function purgeCache() {
  if (!confirm('Liberar cache de arquivos? O macOS pedirá sua senha de administrador.')) return;
  document.getElementById('mem-detail').innerHTML += '<div style="padding:8px;font-size:12px;color:var(--text3)"><span class="spinner"></span>Aguardando senha e executando purge...</div>';
  const r = await post('/api/purge', {});
  alert(r.msg);
  loadMemory();
}

// Lixeira
async function loadTrash() {
  document.getElementById('trash-metrics').style.display = 'grid';
  document.getElementById('trash-card').style.display = 'block';
  loading('trash-body');
  const d = await get('/api/trash');
  document.getElementById('trash-size').textContent = d.size;
  document.getElementById('trash-count').textContent = d.count;
  if (!d.items.length) {
    document.getElementById('trash-body').innerHTML = '<div class="empty"><i class="ti ti-mood-happy"></i>Lixeira vazia.</div>';
    return;
  }
  let h = '<table><thead><tr><th>Nome</th><th style="text-align:right">Tamanho</th></tr></thead><tbody>';
  d.items.forEach(it => { h += `<tr><td class="mono">${it.name}</td><td class="size-badge">${it.size || '—'}</td></tr>`; });
  if (d.count > d.items.length) h += `<tr><td colspan="2" style="color:var(--text3);font-size:12px;padding:8px 12px">… e mais ${d.count - d.items.length} item(s)</td></tr>`;
  document.getElementById('trash-body').innerHTML = h + '</tbody></table>';
}
function showConfirmTrash() {
  const count = document.getElementById('trash-count').textContent;
  if (count === '—' || count === '0') { alert('A lixeira já está vazia.'); return; }
  document.getElementById('trash-confirm').classList.add('visible');
}
async function emptyTrash() {
  document.getElementById('trash-confirm').classList.remove('visible');
  const r = await post('/api/trash/empty', {});
  alert(r.msg);
  loadTrash();
}

// Docker
async function loadDocker() {
  document.getElementById('docker-card').style.display = 'block';
  loading('docker-body');
  const d = await get('/api/docker');
  if (!d.available) {
    document.getElementById('docker-body').innerHTML = '<div class="empty"><i class="ti ti-brand-docker"></i>Docker não encontrado. Instale o Docker Desktop para usar este módulo.</div>';
    return;
  }
  if (!d.categories || !d.categories.length) {
    document.getElementById('docker-body').innerHTML = '<div class="empty">Nenhum dado retornado pelo Docker.</div>';
    return;
  }
  let h = '<table><thead><tr><th>Tipo</th><th>Total</th><th>Ativo</th><th style="text-align:right">Recuperável</th></tr></thead><tbody>';
  d.categories.forEach(c => {
    h += `<tr><td style="font-weight:500">${c.type}</td><td>${c.total}</td><td>${c.active}</td><td class="size-badge">${c.reclaimable}</td></tr>`;
  });
  document.getElementById('docker-body').innerHTML = h + '</tbody></table>';
}
function showConfirmDocker() { document.getElementById('docker-confirm').classList.add('visible'); }
async function pruneDocker() {
  document.getElementById('docker-confirm').classList.remove('visible');
  document.getElementById('docker-body').innerHTML = '<div class="loading"><span class="spinner"></span>Executando docker system prune... pode demorar alguns minutos.</div>';
  document.getElementById('docker-card').style.display = 'block';
  const r = await post('/api/docker/prune', {});
  document.getElementById('docker-body').innerHTML = `<pre style="padding:14px;font-size:12px;white-space:pre-wrap;color:var(--text2);font-family:ui-monospace,monospace">${r.output}</pre>`;
  loadDocker();
}

// Log
let _logData = [];
async function loadLog() {
  loading('log-body');
  _logData = await get('/api/log');
  renderLog(_logData);
}
function filterLog() {
  const q = document.getElementById('log-filter').value.toLowerCase();
  renderLog(q ? _logData.filter(e => e.cmd.toLowerCase().includes(q)) : _logData);
}
function renderLog(entries) {
  document.getElementById('log-title').textContent = `${entries.length} entrada(s)`;
  if (!entries.length) {
    document.getElementById('log-body').innerHTML = '<div class="empty"><i class="ti ti-mood-happy"></i>Nenhuma entrada.</div>';
    return;
  }
  let h = '<table><thead><tr><th style="width:70px">Hora</th><th style="width:60px">Tempo</th><th>Comando</th><th style="width:60px">Linhas</th><th style="width:70px">Status</th></tr></thead><tbody>';
  entries.forEach(e => {
    const slow = e.duration > 5;
    const err = !!e.error;
    const statusColor = err ? 'var(--red-text)' : slow ? 'var(--amber-text)' : 'var(--green-text)';
    const statusLabel = err ? 'ERRO' : slow ? 'LENTO' : 'OK';
    const rowBg = err ? 'background:var(--red-bg)' : '';
    h += `<tr style="${rowBg}">
      <td class="mono" style="font-size:11px">${e.ts}</td>
      <td style="font-size:12px;color:${slow?'var(--amber-text)':'var(--text2)'}">${e.duration}s</td>
      <td>
        <div class="mono" style="font-size:11px;max-width:480px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${e.cmd}">${e.cmd}</div>
        ${e.error ? `<div style="font-size:10px;color:var(--red-text)">${e.error}</div>` : e.preview ? `<div style="font-size:10px;color:var(--text3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${e.preview}</div>` : ''}
      </td>
      <td style="font-size:12px;color:var(--text2);text-align:center">${e.lines}</td>
      <td style="font-size:11px;font-weight:600;color:${statusColor}">${statusLabel}</td>
    </tr>`;
  });
  document.getElementById('log-body').innerHTML = h + '</tbody></table>';
}

// Disco
async function loadDisk() {
  document.getElementById('disk-card').style.display = 'block';
  loading('disk-body');
  const rows = await get('/api/top-folders');
  if (!rows.length) { document.getElementById('disk-body').innerHTML = '<div class="empty">Sem dados.</div>'; return; }
  let h = '<table><thead><tr><th>Pasta</th><th style="text-align:right">Tamanho</th></tr></thead><tbody>';
  rows.forEach(r => { h += `<tr><td class="mono">${r.path}</td><td class="size-badge">${r.size}</td></tr>`; });
  document.getElementById('disk-body').innerHTML = h + '</tbody></table>';
}

loadOverview();
</script>
</body>
</html>"""

# ── main ──────────────────────────────────────────────────────────────────────

def open_browser():
    import time
    time.sleep(0.8)
    brave_paths = [
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        os.path.expanduser("~/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
    ]
    for path in brave_paths:
        if os.path.exists(path):
            subprocess.Popen([path, f"http://localhost:{PORT}"])
            return
    webbrowser.open(f"http://localhost:{PORT}")

if __name__ == "__main__":
    print(f"Mac Cleaner rodando em http://localhost:{PORT}")
    print("Pressione Ctrl+C para encerrar.\n")
    threading.Thread(target=open_browser, daemon=True).start()
    try:
        HTTPServer(("localhost", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrado.")
