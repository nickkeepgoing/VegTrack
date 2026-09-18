"""
VegTrack Dashboard v3
- Dark glassmorphism UI
- Multi-lot comparison panel (รองรับไมโครบิตหลายตัว)
- Data quality indicator (นับแถวที่กรองออก)
- กรองขยะทั้งใน CSV และ Google Sheets
- Auto-save + atexit + watchdog ครบ
"""

import atexit
import csv
import http.server
import json
import os
import re
import signal
import socket
import socketserver
import sys
import threading
import time
from datetime import datetime

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("ยังไม่ได้ติดตั้ง pyserial  →  pip install pyserial")
    sys.exit(1)

# ── config ────────────────────────────────────────────────────────────────────
BAUD           = 115200
WEB_PORT       = 8000
MAX_HISTORY    = 500
SENSOR_TIMEOUT = 20        # วิ ไม่รับข้อมูล → แจ้งเตือน

TEMP_VALID   = (5.0,  65.0)
HUM_VALID    = (5.0, 100.0)
WEIGHT_VALID = (0.0, 100.0)

# เพิ่ม lot ได้เรื่อยๆ — ปล่อย port="" ให้ auto-detect
LOTS = [
    {"name": "ล็อต 1", "port": "", "csv": "vegtrack_log.csv"},
    # {"name": "ล็อต 2", "port": "", "csv": "vegtrack_log_2.csv"},
]

GSHEET_CREDS_FILE = "credentials.json"
GSHEET_SHEET_ID   = "1j3kn5q1e5cyaHSsP6JMWHxgy4e9suB2TgVvDJL2FNjM"

# ── shared state ──────────────────────────────────────────────────────────────
_lock          = threading.Lock()
_lots_state    = {}   # lot_name → state dict (ส่งไป frontend)
_lot_infra     = {}   # lot_name → {csv_handle, csv_writer, last_row_ts, gsheet_ws}
_active_alerts = {}
_alerts_log    = []


# ── helpers ───────────────────────────────────────────────────────────────────
def find_microbit_ports():
    """คืน list ของ COM port ทุกตัวที่เป็น micro:bit"""
    found = []
    for p in serial.tools.list_ports.comports():
        d = (p.description or "").lower()
        h = (p.hwid or "").lower()
        if any(k in d for k in ("mbed", "micro:bit", "microbit")) or "0d28" in h:
            found.append(p.device)
    return found


def parse_line(line):
    d = {}
    for part in line.split():
        if "=" in part:
            k, _, v = part.partition("=")
            d[k] = v
    return d


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def is_clean(d):
    for v in d.values():
        if "=" in str(v):
            return False
    grade = d.get("Grade", "-")
    if grade not in ("A", "B", "C", "-", ""):
        return False
    t = to_float(d.get("T")) if d.get("T", "") not in ("", "-") else None
    h = to_float(d.get("H")) if d.get("H", "") not in ("", "-") else None
    if t is not None and not (TEMP_VALID[0] <= t <= TEMP_VALID[1]):
        return False
    if h is not None and not (HUM_VALID[0] <= h <= HUM_VALID[1]):
        return False
    if t == 0.0 and h == 0.0:
        return False
    return True


def is_clean_csv(row):
    for v in row.values():
        if "=" in str(v):
            return False
    grade = row.get("grade", "-")
    if grade not in ("A", "B", "C", "-", ""):
        return False
    t = to_float(row.get("temp_c"))
    h = to_float(row.get("humidity_pct"))
    if t is not None and not (TEMP_VALID[0] <= t <= TEMP_VALID[1]):
        return False
    if h is not None and not (HUM_VALID[0] <= h <= HUM_VALID[1]):
        return False
    if t == 0.0 and h == 0.0:
        return False
    return True


# ── alerts ────────────────────────────────────────────────────────────────────
def set_alert(key, level, msg):
    with _lock:
        is_new = key not in _active_alerts
        _active_alerts[key] = {"level": level, "msg": msg}
        if is_new:
            _alerts_log.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "level": level, "msg": msg,
            })
            if len(_alerts_log) > 100:
                _alerts_log.pop(0)


def clear_alert(key):
    with _lock:
        _active_alerts.pop(key, None)


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ── lot state init ────────────────────────────────────────────────────────────
def _init_lot(name, port):
    _lots_state[name] = {
        "name": name, "port": port,
        "temp": None, "humidity": None, "pct_weight": None,
        "days": None, "grade": None, "impact": None,
        "event": None, "time": None,
        "connected": False, "sensor_dht": True, "sensor_weight": True,
        "history": [],
        "rows_saved": 0, "rows_skipped": 0,
    }
    _lot_infra[name] = {
        "csv_handle": None, "csv_writer": None,
        "last_row_ts": 0.0, "gsheet_ws": None,
    }


# ── CSV ───────────────────────────────────────────────────────────────────────
def open_lot_csv(lot_name, csv_file):
    infra = _lot_infra[lot_name]
    new_file = not os.path.exists(csv_file)
    infra["csv_handle"] = open(csv_file, "a", newline="", encoding="utf-8-sig")
    infra["csv_writer"] = csv.writer(infra["csv_handle"])
    if new_file:
        infra["csv_writer"].writerow(["timestamp", "temp_c", "humidity_pct",
                                      "pct_weight", "days_left", "grade",
                                      "impact_total", "event"])
        infra["csv_handle"].flush()


def close_all_csv():
    for infra in _lot_infra.values():
        h = infra.get("csv_handle")
        if h:
            try:
                h.flush()
                h.close()
            except Exception:
                pass
            infra["csv_handle"] = None


atexit.register(close_all_csv)
try:
    signal.signal(signal.SIGTERM, lambda *_: (close_all_csv(), sys.exit(0)))
except (OSError, AttributeError):
    pass


def load_lot_history(lot_name, csv_file):
    if not os.path.exists(csv_file):
        return 0
    rows = []
    try:
        with open(csv_file, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if not is_clean_csv(row):
                    continue
                ts = row.get("timestamp", "")
                rows.append({
                    "temp":       to_float(row.get("temp_c")),
                    "humidity":   to_float(row.get("humidity_pct")),
                    "pct_weight": to_float(row.get("pct_weight")),
                    "days":       to_float(row.get("days_left")),
                    "grade":      row.get("grade", "-"),
                    "impact":     row.get("impact_total", "-"),
                    "event":      row.get("event", "-"),
                    "time":       ts[11:19] if len(ts) > 10 else ts,
                    "connected":  True,
                })
    except Exception as e:
        print(f"โหลด CSV ({csv_file}) ล้มเหลว:", e)
        return 0
    with _lock:
        _lots_state[lot_name]["history"].extend(rows[-MAX_HISTORY:])
    return len(rows)


# ── Google Sheets ─────────────────────────────────────────────────────────────
def init_gsheet():
    if not GSHEET_SHEET_ID or not os.path.exists(GSHEET_CREDS_FILE):
        return
    try:
        import gspread
        gc = gspread.oauth(credentials_filename=GSHEET_CREDS_FILE)
        sh = gc.open_by_key(GSHEET_SHEET_ID)
        print("เชื่อม Google Sheets:", sh.title)
        for lot in LOTS:
            name = lot["name"]
            # หา worksheet ชื่อ lot หรือสร้างใหม่
            try:
                ws = sh.worksheet(name)
            except Exception:
                ws = sh.add_worksheet(title=name, rows=10000, cols=10)
                ws.append_row(["timestamp", "temp_c", "humidity_pct",
                               "pct_weight", "days_left", "grade",
                               "impact_total", "event"])
            _lot_infra[name]["gsheet_ws"] = ws
            print(f"  Sheet '{name}' พร้อม")
    except Exception as e:
        print("Google Sheets เชื่อมไม่ได้:", e)


def gsheet_append(lot_name, values):
    ws = _lot_infra[lot_name].get("gsheet_ws")
    if ws is None:
        return
    try:
        ws.append_row(values, value_input_option="USER_ENTERED")
    except Exception:
        pass


# ── serial reader ─────────────────────────────────────────────────────────────
def serial_reader_loop(lot_name, csv_file):
    open_lot_csv(lot_name, csv_file)
    infra = _lot_infra[lot_name]
    port = _lots_state[lot_name]["port"]
    ser = None

    while True:
        if ser is None:
            try:
                ser = serial.Serial(port, BAUD, timeout=2)
                print(f"[{lot_name}] เชื่อมต่อพอร์ต {port}")
                with _lock:
                    _lots_state[lot_name]["connected"] = True
                clear_alert(f"{lot_name}_disconnected")
            except Exception as e:
                print(f"[{lot_name}] เปิดพอร์ตไม่สำเร็จ: {e} — ลองใหม่ใน 3 วิ")
                with _lock:
                    _lots_state[lot_name]["connected"] = False
                set_alert(f"{lot_name}_disconnected", "err",
                          f"[{lot_name}] บอร์ดขาดการเชื่อมต่อ: {port}")
                time.sleep(3)
                continue

        try:
            raw = ser.readline()
        except Exception as e:
            print(f"[{lot_name}] อ่านพอร์ตล้มเหลว: {e}")
            try:
                ser.close()
            except Exception:
                pass
            ser = None
            with _lock:
                _lots_state[lot_name]["connected"] = False
            set_alert(f"{lot_name}_disconnected", "err",
                      f"[{lot_name}] บอร์ดหลุดจากการเชื่อมต่อ")
            time.sleep(2)
            continue

        if not raw:
            continue
        try:
            line = raw.decode("utf-8", errors="ignore").strip()
        except Exception:
            continue
        if not line or "=" not in line:
            continue

        d = parse_line(line)
        if not is_clean(d):
            with _lock:
                _lots_state[lot_name]["rows_skipped"] += 1
            continue

        now   = datetime.now()
        stamp = now.strftime("%H:%M:%S")
        t      = to_float(d.get("T"))
        h      = to_float(d.get("H"))
        w      = to_float(d.get("W"))
        days   = to_float(d.get("Days"))
        grade  = d.get("Grade", "-")
        impact = d.get("Impact", "-")
        event  = d.get("Event", "-")

        dht_ok    = (t is not None and h is not None)
        weight_ok = (w is not None)

        if not dht_ok:
            set_alert(f"{lot_name}_dht", "warn", f"[{lot_name}] DHT sensor ไม่ส่งค่า T/H")
        else:
            clear_alert(f"{lot_name}_dht")
        if not weight_ok:
            set_alert(f"{lot_name}_weight", "warn", f"[{lot_name}] Load cell ไม่ส่งค่าน้ำหนัก")
        else:
            clear_alert(f"{lot_name}_weight")
        if grade == "C":
            set_alert(f"{lot_name}_grade_c", "err",
                      f"[{lot_name}] เกรด C! ต้องขายหรือคัดแยกทันที")
        else:
            clear_alert(f"{lot_name}_grade_c")
        try:
            if int(impact) > 20:
                set_alert(f"{lot_name}_impact", "warn",
                          f"[{lot_name}] กระแทกสะสมสูง: {impact} ครั้ง")
            else:
                clear_alert(f"{lot_name}_impact")
        except (ValueError, TypeError):
            pass

        row = {
            "temp": t, "humidity": h, "pct_weight": w, "days": days,
            "grade": grade, "impact": impact, "event": event,
            "time": stamp, "connected": True,
        }

        infra["csv_writer"].writerow([now.strftime("%Y-%m-%d %H:%M:%S"),
                                      d.get("T", ""), d.get("H", ""),
                                      d.get("W", ""), d.get("Days", ""),
                                      grade, impact, event])
        infra["csv_handle"].flush()
        infra["last_row_ts"] = time.time()
        clear_alert(f"{lot_name}_timeout")

        threading.Thread(
            target=gsheet_append,
            args=(lot_name, [now.strftime("%Y-%m-%d %H:%M:%S"),
                             d.get("T",""), d.get("H",""), d.get("W",""),
                             d.get("Days",""), grade, impact, event]),
            daemon=True,
        ).start()

        with _lock:
            s = _lots_state[lot_name]
            s.update(row)
            s["sensor_dht"]    = dht_ok
            s["sensor_weight"] = weight_ok
            s["rows_saved"]   += 1
            s["history"].append(row)
            if len(s["history"]) > MAX_HISTORY:
                s["history"].pop(0)


def watchdog_loop():
    while True:
        time.sleep(10)
        now = time.time()
        with _lock:
            snapshot = {k: (v["connected"], _lot_infra[k]["last_row_ts"])
                        for k, v in _lots_state.items()}
        for lot_name, (connected, last_ts) in snapshot.items():
            if connected and last_ts > 0 and (now - last_ts) > SENSOR_TIMEOUT:
                gap = now - last_ts
                set_alert(f"{lot_name}_timeout", "warn",
                          f"[{lot_name}] ไม่ได้รับข้อมูลนาน {gap:.0f} วิ")


# ── dashboard HTML ────────────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VegTrack Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Thai:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#f6f8f6;
  --bg2:#eef2ee;
  --card:rgba(255,255,255,0.92);
  --card2:rgba(255,255,255,0.98);
  --blur:blur(14px);
  --border:rgba(15,23,42,0.08);
  --text:#1c2620;
  --muted:#6b7c74;
  --green:#16a34a;--lgreen:rgba(22,163,74,.10);
  --orange:#ea580c;--lorange:rgba(234,88,12,.10);
  --red:#dc2626;--lred:rgba(220,38,38,.10);
  --blue:#0284c7;
  --shadow:0 10px 30px rgba(15,23,42,.06);
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-font-smoothing:antialiased;-moz-osx-font-smoothing:grayscale}
body{
  font-family:'Noto Sans Thai','Segoe UI',Tahoma,sans-serif;
  font-weight:400;
  background:var(--bg);
  color:var(--text);
  min-height:100vh;
  background-image:
    radial-gradient(ellipse at 15% 0%,rgba(22,163,74,.05) 0%,transparent 55%),
    radial-gradient(ellipse at 85% 15%,rgba(2,132,199,.04) 0%,transparent 55%);
  background-attachment:fixed;
}

/* topbar */
.topbar{
  background:rgba(255,255,255,.82);
  backdrop-filter:var(--blur);
  border-bottom:1px solid var(--border);
  padding:0 20px;height:56px;
  display:flex;align-items:center;gap:12px;
  position:sticky;top:0;z-index:300;
  box-shadow:0 1px 0 rgba(15,23,42,.03);
}
.logo{font-size:18px;font-weight:800;letter-spacing:.2px;color:var(--green);display:flex;align-items:center;gap:6px}
.topbar-lots{display:flex;gap:8px;margin-left:8px}
.tlot{display:flex;align-items:center;gap:5px;font-size:11px;font-weight:600;color:var(--muted);
  padding:4px 10px;border-radius:20px;border:1px solid var(--border);
  background:rgba(15,23,42,.02)}
.tdot{width:7px;height:7px;border-radius:50%;background:rgba(15,23,42,.15);transition:.4s}
.tdot.on{background:var(--green);box-shadow:0 0 6px rgba(22,163,74,.5)}
.topbar-right{margin-left:auto;font-size:11px;font-weight:600;color:var(--muted)}

/* alert strip */
.astrip{display:none;align-items:center;gap:10px;padding:10px 20px;font-size:13px;font-weight:700}
.astrip.err{background:#dc2626;color:#fff}
.astrip.warn{background:#ea580c;color:#fff}
.astripx{cursor:pointer;font-size:16px;margin-left:auto;opacity:.8}

.wrap{padding:20px 20px 8px;max-width:1140px;margin:0 auto}

/* comparison panel */
.section-lbl{font-size:11px;font-weight:700;color:var(--muted);
  text-transform:uppercase;letter-spacing:1px;margin-bottom:12px}

.compare-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));
  gap:14px;margin-bottom:20px}

.lot-card{
  background:var(--card);
  backdrop-filter:var(--blur);
  border:1px solid var(--border);
  border-radius:18px;
  padding:20px 20px 16px;
  box-shadow:var(--shadow);
  cursor:pointer;
  transition:transform .2s,border-color .25s,box-shadow .25s;
}
.lot-card:hover{border-color:rgba(22,163,74,.35);transform:translateY(-2px);box-shadow:0 14px 34px rgba(15,23,42,.09)}
.lot-card.selected{border-color:var(--green);box-shadow:0 0 0 3px rgba(22,163,74,.12),var(--shadow)}
.lot-card.urgent-c{border-color:var(--red)!important;box-shadow:0 0 0 3px rgba(220,38,38,.12),var(--shadow)!important}
.lot-card.urgent-b{border-color:var(--orange)!important;box-shadow:0 0 0 3px rgba(234,88,12,.12),var(--shadow)!important}

.lot-top{display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:10px}
.lot-name{font-size:13px;font-weight:700;color:var(--muted)}
.lot-priority{font-size:10px;font-weight:700;padding:3px 9px;border-radius:10px;
  border:1px solid;animation:blink2 1.4s ease infinite}
.lp-c{background:var(--lred);color:var(--red);border-color:rgba(220,38,38,.3)}
.lp-b{background:var(--lorange);color:var(--orange);border-color:rgba(234,88,12,.3)}
.lp-a{background:var(--lgreen);color:var(--green);border-color:rgba(22,163,74,.3)}
@keyframes blink2{0%,100%{opacity:1}50%{opacity:.55}}

.lot-grade{font-size:52px;font-weight:800;line-height:1;margin:6px 0}
.gA{color:var(--green)}
.gB{color:var(--orange)}
.gC{color:var(--red);animation:blink2 1.2s ease infinite}
.lot-days{font-size:13px;font-weight:600;color:var(--muted);margin-bottom:12px}

.lot-meta{display:flex;gap:12px;font-size:12px;margin-bottom:10px;flex-wrap:wrap}
.lmeta-item{color:var(--muted);font-weight:500}
.lmeta-item span{color:var(--text);font-weight:700}

.lot-sensors{display:flex;gap:6px;flex-wrap:wrap}
.sbadge{font-size:10px;font-weight:700;padding:3px 8px;border-radius:8px;border:1px solid}
.sok{background:var(--lgreen);color:var(--green);border-color:rgba(22,163,74,.25)}
.serr{background:var(--lred);color:var(--red);border-color:rgba(220,38,38,.25)}

/* data quality bar */
.dq-bar-wrap{margin-top:12px;border-top:1px solid var(--border);padding-top:10px}
.dq-label{font-size:10px;font-weight:600;color:var(--muted);margin-bottom:5px;display:flex;justify-content:space-between}
.dq-bar{height:5px;border-radius:3px;background:rgba(15,23,42,.06);overflow:hidden}
.dq-fill{height:100%;border-radius:3px;transition:width .5s}
.dq-good{background:var(--green)}
.dq-fair{background:var(--orange)}
.dq-poor{background:var(--red)}

/* tab bar */
.tabs{display:flex;gap:4px;margin-bottom:16px;border-bottom:1px solid var(--border);padding-bottom:0}
.tab{padding:9px 18px;font-size:12px;font-weight:700;color:var(--muted);
  cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px;
  transition:color .2s,border-color .2s,background .2s;border-radius:8px 8px 0 0}
.tab:hover{color:var(--text);background:rgba(15,23,42,.03)}
.tab.active{color:var(--green);border-bottom-color:var(--green)}

/* detail section */
.detail{display:none}
.detail.visible{display:block}

/* value cards */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(128px,1fr));gap:10px;margin-bottom:14px}
.card{background:var(--card);backdrop-filter:var(--blur);border:1px solid var(--border);
  border-radius:14px;padding:14px 16px;box-shadow:0 4px 16px rgba(15,23,42,.05)}
.clbl{font-size:9px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.7px;margin-bottom:6px}
.cval{font-size:26px;font-weight:800;color:var(--green);line-height:1.1}
.cunit{font-size:10px;color:var(--muted);font-weight:500}

/* charts */
.charts{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}
@media(max-width:560px){.charts{grid-template-columns:1fr}.compare-grid{grid-template-columns:1fr}}
.chartcard{background:var(--card);backdrop-filter:var(--blur);border:1px solid var(--border);
  border-radius:14px;padding:14px;box-shadow:0 4px 16px rgba(15,23,42,.05)}
.chartlbl{font-size:10px;font-weight:700;color:var(--muted);margin-bottom:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.ldot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
svg.chart{width:100%;height:130px;display:block}

/* alerts */
.alist{display:flex;flex-direction:column;gap:4px;max-height:150px;overflow-y:auto;margin-bottom:14px}
.aitem{display:flex;gap:8px;padding:6px 12px;border-radius:8px;font-size:12px;font-weight:500}
.aitem.err{background:var(--lred);color:var(--red);border:1px solid rgba(220,38,38,.18)}
.aitem.warn{background:var(--lorange);color:var(--orange);border:1px solid rgba(234,88,12,.18)}
.aitem.info{background:var(--lgreen);color:var(--green);border:1px solid rgba(22,163,74,.18)}
.atime{opacity:.6;font-size:10px;white-space:nowrap;padding-top:1px}

/* log */
.logbox{background:#0f172a;color:#7ee2a8;
  font-family:'JetBrains Mono','Consolas','Courier New',monospace;
  font-size:11px;line-height:1.7;padding:12px 16px;border-radius:14px;
  max-height:260px;overflow-y:auto;white-space:pre;
  border:1px solid rgba(15,23,42,.1);margin-bottom:16px;
  box-shadow:0 4px 16px rgba(15,23,42,.08)}
.rA{color:#4ade80}.rB{color:#fb923c}.rC{color:#f87171}

footer{text-align:center;font-size:11px;font-weight:500;color:var(--muted);padding:10px 0 24px}
</style>
</head>
<body>

<div class="topbar">
  <span class="logo">&#9652; VegTrack</span>
  <div class="topbar-lots" id="tlots"></div>
  <span class="topbar-right" id="updlbl"></span>
</div>
<div class="astrip" id="astrip">
  <span>&#9888;</span>
  <span id="astripmsgs" style="flex:1"></span>
  <span class="astripx" onclick="this.parentElement.style.display='none'">&#10005;</span>
</div>

<div class="wrap">

  <!-- comparison panel -->
  <div class="section-lbl">ภาพรวมทุกล็อต — คลิกเพื่อดูรายละเอียด</div>
  <div class="compare-grid" id="compareGrid"></div>

  <!-- tab bar -->
  <div class="tabs" id="tabBar"></div>

  <!-- per-lot detail panes -->
  <div id="detailPanes"></div>

  <!-- global alerts log -->
  <div class="section-lbl" style="margin-top:4px">ประวัติการแจ้งเตือน</div>
  <div class="alist" id="alist">
    <div class="aitem info"><span class="atime">-</span><span>รอข้อมูล...</span></div>
  </div>

</div>
<footer>VegTrack v3 &mdash; ทีม BUZZA11DAY &mdash; ทำงานในเครื่อง ไม่ต้องใช้อินเทอร์เน็ต</footer>

<script>
const GMSG={A:'ขายตามลำดับปกติ',B:'ควรเร่งขายก่อน',C:'ต้องขายวันนี้!'};
let selectedLot=null;
let lotNames=[];

function esc(s){return String(s==null?'-':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function pad(s,n){s=(s==null?'-':String(s));while(s.length<n)s+=' ';return s.slice(0,n);}

function urgency(lot){
  if(!lot.connected)return 0;
  return lot.grade==='C'?3:lot.grade==='B'?2:lot.grade==='A'?1:0;
}

function dataQuality(lot){
  const total=lot.rows_saved+lot.rows_skipped;
  if(!total)return null;
  const pct=Math.round(lot.rows_saved/total*100);
  const cls=pct>=90?'dq-good':pct>=70?'dq-fair':'dq-poor';
  const lbl=pct>=90?'คุณภาพดี':pct>=70?'ปานกลาง':'ต่ำ — มีขยะมาก';
  return{pct,cls,lbl};
}

function renderCompare(lots){
  const sorted=[...lots].sort((a,b)=>urgency(b)-urgency(a));
  const mostUrgent=sorted[0]?.name;

  document.getElementById('compareGrid').innerHTML=sorted.map(lot=>{
    const u=urgency(lot);
    const urg=u===3?'urgent-c':u===2?'urgent-b':'';
    const sel=lot.name===selectedLot?'selected':'';
    const priCls=u===3?'lp-c':u===2?'lp-b':'lp-a';
    const priLbl=u===3?'&#9888; ขายก่อน':u===2?'&#8679; เร่งขาย':'&#10003; ปกติ';
    const dq=dataQuality(lot);
    const dqHtml=dq?`<div class="dq-bar-wrap">
      <div class="dq-label"><span>คุณภาพข้อมูล: ${dq.lbl}</span><span>${dq.pct}%</span></div>
      <div class="dq-bar"><div class="dq-fill ${dq.cls}" style="width:${dq.pct}%"></div></div>
    </div>`:'';
    return `<div class="lot-card ${urg} ${sel}" onclick="selectLot('${esc(lot.name)}')">
      <div class="lot-top">
        <span class="lot-name">${esc(lot.name)}</span>
        <span class="lot-priority ${priCls}">${priLbl}</span>
      </div>
      <div class="lot-grade g${lot.grade||'-'}">${lot.grade||'-'}</div>
      <div class="lot-days">${lot.days!=null?lot.days+' วัน':'รอข้อมูล...'}</div>
      <div class="lot-meta">
        <span class="lmeta-item">T <span>${lot.temp!=null?lot.temp:'—'}°</span></span>
        <span class="lmeta-item">H <span>${lot.humidity!=null?lot.humidity:'—'}%</span></span>
        <span class="lmeta-item">&#9679; <span>${lot.event||'—'}</span></span>
      </div>
      <div class="lot-sensors">
        <span class="sbadge ${lot.connected?'sok':'serr'}">Serial ${lot.connected?'✓':'✗'}</span>
        <span class="sbadge ${lot.sensor_dht?'sok':'serr'}">DHT ${lot.sensor_dht?'✓':'✗'}</span>
        <span class="sbadge ${lot.sensor_weight?'sok':'serr'}">Scale ${lot.sensor_weight?'✓':'✗'}</span>
      </div>
      ${dqHtml}
    </div>`;
  }).join('');
}

function renderTabs(lots){
  const sorted=[...lots].sort((a,b)=>urgency(b)-urgency(a));
  document.getElementById('tabBar').innerHTML=sorted.map(l=>
    `<div class="tab ${l.name===selectedLot?'active':''}" onclick="selectLot('${esc(l.name)}')">${esc(l.name)}</div>`
  ).join('');
}

function ensureDetailPanes(lots){
  const panes=document.getElementById('detailPanes');
  lots.forEach(lot=>{
    const id='pane_'+lot.name.replace(/\s/g,'_');
    if(!document.getElementById(id)){
      const div=document.createElement('div');
      div.id=id;div.className='detail';
      div.innerHTML=`
        <div class="cards">
          <div class="card"><div class="clbl">อุณหภูมิ</div><span class="cval" id="${id}_tv">-</span><span class="cunit"> °C</span></div>
          <div class="card"><div class="clbl">ความชื้น</div><span class="cval" id="${id}_hv">-</span><span class="cunit"> %</span></div>
          <div class="card"><div class="clbl">น้ำหนัก</div><span class="cval" id="${id}_wv">-</span><span class="cunit"> %</span></div>
          <div class="card"><div class="clbl">วันที่เหลือ</div><span class="cval" id="${id}_dv">-</span><span class="cunit"> วัน</span></div>
          <div class="card"><div class="clbl">กระแทก</div><div class="cval" id="${id}_iv">-</div></div>
          <div class="card"><div class="clbl">เหตุการณ์</div><div class="cval" style="font-size:14px;padding-top:4px" id="${id}_ev">-</div></div>
          <div class="card"><div class="clbl">แถวที่กรองออก</div><div class="cval" style="font-size:20px;color:var(--orange)" id="${id}_skip">0</div><span class="cunit"> แถว</span></div>
          <div class="card"><div class="clbl">แถวที่บันทึก</div><div class="cval" style="font-size:20px" id="${id}_saved">0</div><span class="cunit"> แถว</span></div>
        </div>
        <div class="charts">
          <div class="chartcard">
            <div class="chartlbl">
              <span class="ldot" style="background:#e53935"></span>T°C
              &nbsp;<span class="ldot" style="background:#38bdf8"></span>H%
            </div>
            <svg class="chart" id="${id}_svgTH" viewBox="0 0 600 120" preserveAspectRatio="none"></svg>
          </div>
          <div class="chartcard">
            <div class="chartlbl"><span class="ldot" style="background:#22c55e"></span>Days remaining</div>
            <svg class="chart" id="${id}_svgDays" viewBox="0 0 600 120" preserveAspectRatio="none"></svg>
          </div>
        </div>
        <div class="section-lbl">Log ดิบ</div>
        <div class="logbox" id="${id}_log">รอข้อมูล...</div>`;
      panes.appendChild(div);
    }
  });
}

function selectLot(name){
  selectedLot=name;
  document.querySelectorAll('.detail').forEach(d=>d.classList.remove('visible'));
  const id='pane_'+name.replace(/\s/g,'_');
  const p=document.getElementById(id);
  if(p)p.classList.add('visible');
  document.querySelectorAll('.tab').forEach(t=>{
    t.classList.toggle('active',t.textContent===name);
  });
  document.querySelectorAll('.lot-card').forEach(c=>{
    c.classList.toggle('selected',c.querySelector('.lot-name')?.textContent===name);
  });
}

function updateDetail(lot){
  const id='pane_'+lot.name.replace(/\s/g,'_');
  const s=(sid,v)=>{const el=document.getElementById(id+sid);if(el)el.textContent=v??'-';};
  s('_tv',lot.temp);s('_hv',lot.humidity);s('_wv',lot.pct_weight);
  s('_dv',lot.days);s('_iv',lot.impact);s('_ev',lot.event);
  s('_skip',lot.rows_skipped);s('_saved',lot.rows_saved);

  const th=document.getElementById(id+'_svgTH');
  const dsvg=document.getElementById(id+'_svgDays');
  if(th)drawLines(th,[lot.history.map(r=>r.temp),lot.history.map(r=>r.humidity)],['#e53935','#38bdf8'],false);
  if(dsvg)drawLines(dsvg,[lot.history.map(r=>r.days)],['#22c55e'],true);

  const log=document.getElementById(id+'_log');
  if(log&&lot.history.length){
    const hdr=pad('เวลา',10)+pad('T°C',7)+pad('H%',7)+pad('W%',8)+pad('Days',7)+pad('Gr',5)+pad('Impact',8)+'  Event';
    const rows=lot.history.slice().reverse().map(r=>{
      const ln=pad(r.time,10)+pad(r.temp,7)+pad(r.humidity,7)+pad(r.pct_weight,8)+pad(r.days,7)+pad(r.grade,5)+pad(r.impact,8)+'  '+(r.event||'-');
      const cls=r.grade==='A'?'rA':r.grade==='B'?'rB':r.grade==='C'?'rC':'';
      const e=ln.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      return cls?'<span class="'+cls+'">'+e+'</span>':e;
    });
    log.innerHTML=[hdr,'─'.repeat(62),...rows].join('\\n');
  }
}

function drawLines(svg,series,colors,marker,W=600,H=120,P=18){
  svg.innerHTML='';
  for(let i=0;i<=3;i++){
    const y=P+i*(H-2*P)/3;
    const el=document.createElementNS('http://www.w3.org/2000/svg','line');
    el.setAttribute('x1',P);el.setAttribute('x2',W-P);
    el.setAttribute('y1',y);el.setAttribute('y2',y);
    el.setAttribute('stroke','rgba(15,23,42,.08)');el.setAttribute('stroke-width','1');
    svg.appendChild(el);
  }
  const n=series[0]?.length||0;if(n<2)return;
  series.forEach((pts,si)=>{
    const vals=pts.filter(v=>v!=null);
    if(vals.length<2)return;
    const mx=Math.max(...vals),mn=Math.min(...vals),rng=(mx-mn)||1;
    const x=i=>P+(i/(n-1))*(W-2*P);
    const y=v=>H-P-((v-mn)/rng)*(H-2*P);
    let d='',prev=null;
    pts.forEach((v,i)=>{if(v==null){prev=null;return;}d+=(prev===null?'M ':'L ')+x(i)+' '+y(v);prev=i;});
    const path=document.createElementNS('http://www.w3.org/2000/svg','path');
    path.setAttribute('d',d);path.setAttribute('fill','none');
    path.setAttribute('stroke',colors[si]);path.setAttribute('stroke-width','2');
    svg.appendChild(path);
    if(marker){
      const li=pts.reduce((a,v,i)=>v!=null?i:a,-1);
      if(li>=0){
        const c=document.createElementNS('http://www.w3.org/2000/svg','circle');
        c.setAttribute('cx',x(li));c.setAttribute('cy',y(pts[li]));
        c.setAttribute('r','4');c.setAttribute('fill',colors[si]);
        svg.appendChild(c);
      }
    }
  });
}

async function refresh(){
  let j;
  try{j=await fetch('/data').then(r=>r.json());}
  catch(e){document.getElementById('tlots').innerHTML='<span style="color:var(--red);font-size:11px">เซิร์ฟเวอร์ไม่ตอบ</span>';return;}

  const lots=Object.values(j.lots);
  if(!lots.length)return;
  lotNames=lots.map(l=>l.name);

  // topbar dots
  document.getElementById('tlots').innerHTML=lots.map(l=>
    `<div class="tlot"><span class="tdot ${l.connected?'on':''}"></span>${esc(l.name)}</div>`
  ).join('');
  const anyTime=lots.find(l=>l.time);
  if(anyTime)document.getElementById('updlbl').textContent='อัปเดต '+anyTime.time;

  // alert strip
  const alerts=j.alerts||[];
  const strip=document.getElementById('astrip');
  if(alerts.length){
    strip.style.display='flex';
    strip.className='astrip '+(alerts.some(a=>a.level==='err')?'err':'warn');
    document.getElementById('astripmsgs').textContent=alerts.map(a=>a.msg).join('  |  ');
  }

  // auto select most urgent lot
  const sorted=[...lots].sort((a,b)=>urgency(b)-urgency(a));
  if(!selectedLot||!lotNames.includes(selectedLot))selectedLot=sorted[0].name;

  renderCompare(lots);
  renderTabs(lots);
  ensureDetailPanes(lots);

  // show selected pane
  document.querySelectorAll('.detail').forEach(d=>d.classList.remove('visible'));
  const selPane=document.getElementById('pane_'+selectedLot.replace(/\s/g,'_'));
  if(selPane)selPane.classList.add('visible');

  // update all lots' details
  lots.forEach(lot=>updateDetail(lot));

  // alerts log
  const alog=j.alerts_log||[];
  if(alog.length){
    document.getElementById('alist').innerHTML=alog.slice().reverse().slice(0,30).map(a=>
      `<div class="aitem ${a.level}"><span class="atime">${esc(a.time)}</span><span>${esc(a.msg)}</span></div>`
    ).join('');
  }
}

refresh();
setInterval(refresh,2000);
</script>
</body>
</html>
"""


# ── HTTP handler ──────────────────────────────────────────────────────────────
class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = DASHBOARD_HTML.encode("utf-8")
            self._send(200, "text/html; charset=utf-8", body)
        elif self.path == "/data":
            with _lock:
                payload = {
                    "lots": {
                        name: {
                            **{k: v for k, v in state.items() if k != "history"},
                            "history": list(state["history"])[-300:],
                        }
                        for name, state in _lots_state.items()
                    },
                    "alerts":     list(_active_alerts.values()),
                    "alerts_log": list(_alerts_log),
                }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(200, "application/json; charset=utf-8", body)
        else:
            self.send_response(404)
            self.end_headers()

    def _send(self, code, ct, body):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    # auto-detect micro:bit ports
    mb_ports = find_microbit_ports()
    if mb_ports:
        print(f"พบ micro:bit: {', '.join(mb_ports)}")

    # assign ports to lots
    for i, lot in enumerate(LOTS):
        if not lot["port"]:
            if i < len(mb_ports):
                lot["port"] = mb_ports[i]
                print(f"  {lot['name']} → {mb_ports[i]} (auto)")
            else:
                p = input(f"  {lot['name']}: พิมพ์พอร์ต (หรือ Enter ข้าม): ").strip()
                lot["port"] = p

    # initialize state & load history
    for lot in LOTS:
        _init_lot(lot["name"], lot["port"])
        n = load_lot_history(lot["name"], lot["csv"])
        if n:
            print(f"[{lot['name']}] โหลดประวัติ {n} แถว")

    init_gsheet()

    # start threads
    for lot in LOTS:
        if lot["port"]:
            threading.Thread(
                target=serial_reader_loop,
                args=(lot["name"], lot["csv"]),
                daemon=True,
            ).start()
        else:
            print(f"[{lot['name']}] ไม่มีพอร์ต — ข้ามการเชื่อมต่อ")

    threading.Thread(target=watchdog_loop, daemon=True).start()

    ip = get_local_ip()
    print("=" * 68)
    print("VegTrack Dashboard v3 พร้อมแล้ว")
    print(f"  http://localhost:{WEB_PORT}")
    print(f"  http://{ip}:{WEB_PORT}  (มือถือ/เครื่องอื่น)")
    print("=" * 68)

    server = ThreadingHTTPServer(("0.0.0.0", WEB_PORT), DashboardHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nปิดเซิร์ฟเวอร์แล้ว")
        close_all_csv()
        server.shutdown()


if __name__ == "__main__":
    main()
