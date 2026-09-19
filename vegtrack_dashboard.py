"""
VegTrack Dashboard v4 (Light Theme)
- Modern glassmorphism + gradient-icon UI (inspired by premium admin dashboards)
- Multi-lot comparison panel (รองรับไมโครบิตหลายตัว)
- Data quality indicator (นับแถวที่กรองออก)
- กรองขยะทั้งใน CSV และ Google Sheets
- Auto-save + atexit + watchdog ครบ
- Backend logic เหมือน v3 ทุกประการ — เปลี่ยนแค่หน้าตา (DASHBOARD_HTML)
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


def is_format_ok(d):
    """เช็คว่าบรรทัดข้อมูลพาร์สได้ถูกต้อง (ไม่ใช่การเช็คช่วงค่าเซนเซอร์)
    ถ้าไม่ผ่านข้อนี้แปลว่าบรรทัดเพี้ยนทั้งบรรทัด ไม่มีอะไรใช้ได้เลย"""
    for v in d.values():
        if "=" in str(v):
            return False
    grade = d.get("Grade", "-")
    if grade not in ("A", "B", "C", "-", ""):
        return False
    return True


def is_dht_value_ok(t, h):
    """เช็คเฉพาะว่าค่าที่ DHT อ่านได้อยู่ในช่วงที่เป็นไปได้จริงหรือไม่
    แยกจาก is_format_ok เพื่อให้ Event/Impact จาก Edge AI ยังใช้ได้
    แม้ DHT จะอ่านค่าขยะ (-999, 0, ฯลฯ) ก็ตาม"""
    if t is None or h is None:
        return False
    if not (TEMP_VALID[0] <= t <= TEMP_VALID[1]):
        return False
    if not (HUM_VALID[0] <= h <= HUM_VALID[1]):
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
        if not is_format_ok(d):
            # บรรทัดเพี้ยนทั้งบรรทัด (ไม่ใช่แค่ DHT) ไม่มีอะไรใช้ได้เลย ข้ามทิ้ง
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

        dht_ok    = is_dht_value_ok(t, h)
        weight_ok = (w is not None)

        if not dht_ok:
            set_alert(f"{lot_name}_dht", "warn", f"[{lot_name}] DHT sensor ไม่ส่งค่า T/H")
        else:
            clear_alert(f"{lot_name}_dht")
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

        infra["last_row_ts"] = time.time()
        clear_alert(f"{lot_name}_timeout")

        if not dht_ok:
            # DHT อ่านค่าขยะ (-999, 0, ฯลฯ) — ไม่บันทึกลง CSV/Google Sheets เด็ดขาด
            # แต่ Event/Impact จาก Edge AI ยังทำงานอิสระจาก DHT จึงยังอัปเดตให้เห็นสด ๆ ได้
            # (ไม่แตะ temp/humidity/pct_weight/days/history — ค่าจริงล่าสุดที่ยังถูกต้องจะยังค้างแสดงอยู่)
            with _lock:
                s = _lots_state[lot_name]
                s["impact"]        = impact
                s["event"]         = event
                s["time"]          = stamp
                s["connected"]     = True
                s["sensor_dht"]    = False
                s["sensor_weight"] = weight_ok
                s["rows_skipped"] += 1
            continue

        if not weight_ok:
            set_alert(f"{lot_name}_weight", "warn", f"[{lot_name}] Load cell ไม่ส่งค่าน้ำหนัก")
        else:
            clear_alert(f"{lot_name}_weight")

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


# ── dashboard HTML (v4 — modernized UI) ───────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VegTrack Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Noto+Sans+Thai:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#eef1f7;
  --bg-grad-a:#eef2ff;
  --bg-grad-b:#f6f8fc;
  --sidebar:#ffffff;
  --card:#ffffff;
  --card-2:#f8fafc;
  --border:rgba(15,23,42,.08);
  --border-h:rgba(15,23,42,.16);
  --text:#0f172a;
  --muted:#64748b;
  --muted-2:#94a3b8;

  --green:#16a34a;   --green-2:#15803d;  --lgreen:rgba(22,163,74,.10);
  --orange:#d97706;  --orange-2:#b45309; --lorange:rgba(217,119,6,.10);
  --red:#e11d48;     --red-2:#be123c;    --lred:rgba(225,29,72,.10);
  --blue:#2563eb;    --blue-2:#1d4ed8;   --lblue:rgba(37,99,235,.10);
  --purple:#7c3aed;  --purple-2:#6d28d9; --lpurple:rgba(124,58,237,.10);
  --teal:#0d9488;    --teal-2:#0f766e;   --lteal:rgba(13,148,136,.10);
  --pink:#db2777;    --pink-2:#be185d;   --lpink:rgba(219,39,119,.10);

  --r:18px;
  --r-sm:12px;
  --shadow:0 4px 18px rgba(15,23,42,.07);
  --shadow-lg:0 16px 40px rgba(15,23,42,.10);
}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-font-smoothing:antialiased;scroll-behavior:smooth}
body{
  font-family:'Inter','Noto Sans Thai','Segoe UI',sans-serif;
  color:var(--text);
  min-height:100vh;display:flex;flex-direction:column;font-size:16px;
  background:
    radial-gradient(900px 500px at 85% -10%, rgba(124,58,237,.07), transparent 60%),
    radial-gradient(700px 500px at -5% 10%, rgba(22,163,74,.06), transparent 55%),
    linear-gradient(180deg, var(--bg-grad-a), var(--bg-grad-b) 40%);
  background-attachment:fixed;
}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:rgba(100,116,139,.25);border-radius:8px}
::-webkit-scrollbar-thumb:hover{background:rgba(100,116,139,.4)}

/* ── Shell ───────────────────────────────── */
.app{display:flex;flex:1;min-height:0}

/* ── Sidebar ──────────────────────────────── */
.sidebar{
  width:296px;flex-shrink:0;background:rgba(255,255,255,.85);backdrop-filter:blur(24px);
  border-right:1px solid var(--border);
  display:flex;flex-direction:column;
  position:sticky;top:0;height:100vh;overflow-y:auto;
}
.sb-header{padding:26px 20px 20px;border-bottom:1px solid var(--border);flex-shrink:0}
.sb-logo{display:flex;align-items:center;gap:12px}
.logo-icon{
  width:48px;height:48px;border-radius:14px;flex-shrink:0;
  background:linear-gradient(135deg,#22c55e 0%,#0d9488 100%);
  display:flex;align-items:center;justify-content:center;
  font-size:21px;font-weight:800;color:#fff;
  box-shadow:0 6px 18px rgba(34,197,94,.35);
}
.logo-name{font-size:18px;font-weight:700;color:var(--text);letter-spacing:.2px}
.logo-sub{font-size:11px;color:var(--muted-2);margin-top:3px;letter-spacing:1.2px;text-transform:uppercase;font-weight:600}
.sb-section{
  font-size:11px;font-weight:700;letter-spacing:2px;text-transform:uppercase;
  color:var(--muted-2);padding:20px 20px 10px;flex-shrink:0;
}

/* Lot nav in sidebar */
.compare-grid{display:flex;flex-direction:column;gap:10px;padding:2px 14px 12px;flex-shrink:0}
.lot-card{
  border-radius:var(--r-sm);padding:17px 18px;cursor:pointer;
  transition:all .22s cubic-bezier(.4,0,.2,1);border:1px solid var(--border);
  background:rgba(15,23,42,.02);position:relative;overflow:hidden;
}
.lot-card:hover{background:rgba(15,23,42,.045);border-color:var(--border-h);transform:translateX(2px)}
.lot-card.selected{background:linear-gradient(135deg,rgba(22,163,74,.12),rgba(13,148,136,.05));border-color:rgba(22,163,74,.4);box-shadow:0 4px 20px rgba(22,163,74,.1)}
.lot-card.selected::before{content:'';position:absolute;left:0;top:10px;bottom:10px;width:3px;background:linear-gradient(180deg,var(--green),var(--teal));border-radius:3px}
.lot-card.urgent-c{border-color:rgba(225,29,72,.4)!important;background:linear-gradient(135deg,rgba(225,29,72,.08),rgba(225,29,72,.02))!important}
.lot-card.urgent-b{border-color:rgba(217,119,6,.35)!important;background:linear-gradient(135deg,rgba(217,119,6,.08),rgba(217,119,6,.02))!important}
.lot-top{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.lot-name{font-size:14.5px;font-weight:600;color:var(--text)}
.lot-priority{font-size:10.5px;font-weight:700;padding:4px 10px;border-radius:20px;border:1px solid;display:inline-flex;align-items:center;gap:4px}
.lp-c{background:rgba(225,29,72,.12);color:#be123c;border-color:rgba(225,29,72,.3);animation:pulse2 1.6s ease infinite}
.lp-b{background:rgba(217,119,6,.12);color:#b45309;border-color:rgba(217,119,6,.3)}
.lp-a{background:rgba(22,163,74,.12);color:#15803d;border-color:rgba(22,163,74,.3)}
@keyframes pulse2{0%,100%{opacity:1}50%{opacity:.45}}
.lot-grade{font-family:'JetBrains Mono',monospace;font-size:48px;font-weight:800;line-height:1;margin-bottom:4px;letter-spacing:-1px}
.gA{color:#16a34a}
.gB{color:#b45309}
.gC{color:#e11d48}
.lot-days{font-size:13px;color:var(--muted);margin-bottom:9px;font-weight:500}
.lot-sensors{display:flex;gap:5px;flex-wrap:wrap}
.sbadge{font-size:10px;font-weight:600;padding:4px 9px;border-radius:20px;border:1px solid}
.sok{background:rgba(22,163,74,.1);color:#15803d;border-color:rgba(22,163,74,.25)}
.serr{background:rgba(225,29,72,.1);color:#be123c;border-color:rgba(225,29,72,.25)}
.dq-bar-wrap{margin-top:10px;padding-top:9px;border-top:1px solid rgba(15,23,42,.06)}
.dq-label{font-size:9px;font-weight:600;color:var(--muted-2);margin-bottom:5px;display:flex;justify-content:space-between}
.dq-bar{height:4px;border-radius:3px;background:rgba(15,23,42,.07);overflow:hidden}
.dq-fill{height:100%;border-radius:3px;transition:width .7s cubic-bezier(.4,0,.2,1)}
.dq-good{background:linear-gradient(90deg,#22c55e,#4ade80)}
.dq-fair{background:linear-gradient(90deg,#f59e0b,#fbbf24)}
.dq-poor{background:linear-gradient(90deg,#f43f5e,#fb7185)}

/* Sidebar alerts */
.sb-alerts{padding:0 12px 18px;overflow-y:auto}
.alist{display:flex;flex-direction:column;gap:5px}
.aitem{display:flex;gap:8px;padding:10px 12px;border-radius:10px;font-size:12.5px;font-weight:500;align-items:flex-start;line-height:1.5;border:1px solid transparent}
.aitem.err{background:rgba(225,29,72,.08);color:#be123c;border-color:rgba(225,29,72,.16)}
.aitem.warn{background:rgba(217,119,6,.08);color:#b45309;border-color:rgba(217,119,6,.16)}
.aitem.info{background:rgba(22,163,74,.08);color:#15803d;border-color:rgba(22,163,74,.16)}
.atime{opacity:.65;font-size:9px;white-space:nowrap;padding-top:2px;font-family:'JetBrains Mono',monospace;flex-shrink:0}

/* ── Main area ────────────────────────────── */
.main-area{flex:1;overflow-x:hidden;min-width:0;display:flex;flex-direction:column}
.topbar{
  background:rgba(255,255,255,.75);backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);
  padding:0 32px;height:70px;
  display:flex;align-items:center;gap:14px;
  position:sticky;top:0;z-index:200;flex-shrink:0;
}
.topbar-lots{display:flex;gap:8px}
.tlot{display:flex;align-items:center;gap:7px;font-size:13px;font-weight:500;color:var(--muted);
  padding:6px 14px;border-radius:20px;border:1px solid var(--border);background:rgba(15,23,42,.02)}
.sound-btn{
  display:inline-flex;align-items:center;gap:6px;
  background:rgba(15,23,42,.03);border:1px solid var(--border);border-radius:20px;
  padding:7px 15px;font-size:12.5px;font-weight:600;color:var(--muted);
  cursor:pointer;transition:all .2s;font-family:inherit;
}
.sound-btn:hover{background:rgba(15,23,42,.06)}
.sound-btn.on{color:#15803d;border-color:rgba(22,163,74,.3);background:rgba(22,163,74,.08)}
.log-header{display:flex;align-items:center;justify-content:space-between;margin:26px 0 10px}
.log-header .section-lbl{margin:0}
.save-btn{
  display:inline-flex;align-items:center;gap:6px;
  background:linear-gradient(135deg,#16a34a,#0d9488);color:#fff;
  border:none;border-radius:20px;padding:8px 16px;font-size:12.5px;font-weight:600;
  cursor:pointer;box-shadow:0 4px 12px rgba(22,163,74,.25);
  transition:all .2s;font-family:inherit;
}
.save-btn:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(22,163,74,.35)}
.save-btn:active{transform:translateY(0)}
.save-btn svg{width:14px;height:14px}
.toast{
  position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);
  background:#0f172a;color:#fff;padding:11px 20px;border-radius:12px;font-size:13px;font-weight:600;
  box-shadow:0 10px 30px rgba(0,0,0,.25);opacity:0;pointer-events:none;transition:all .3s cubic-bezier(.4,0,.2,1);
  z-index:999;display:flex;align-items:center;gap:8px;
}
.toast.show{opacity:1;transform:translateX(-50%) translateY(0)}
.degraded-banner{
  display:none;align-items:center;gap:10px;
  background:linear-gradient(135deg,#fef3c7,#fde68a);
  border:1px solid #f59e0b;color:#78350f;
  padding:12px 18px;border-radius:14px;
  font-size:13px;font-weight:700;margin-bottom:16px;
}
.degraded-banner.show{display:flex}
.live-pulse{
  width:9px;height:9px;border-radius:50%;background:#16a34a;
  display:inline-block;margin-left:7px;flex-shrink:0;
  animation:livepulse 1.3s ease infinite;
}
@keyframes livepulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(1.5)}}
.card.stale{opacity:.4;filter:grayscale(.6)}
.gh-days-block.stale{opacity:.4;filter:grayscale(.6)}

.tdot{width:6px;height:6px;border-radius:50%;background:rgba(15,23,42,.15);transition:.4s}
.tdot.on{background:#16a34a;box-shadow:0 0 8px rgba(22,163,74,.5)}
.topbar-right{margin-left:auto;font-family:'JetBrains Mono',monospace;font-size:12.5px;color:var(--muted-2)}
.astrip{display:none;align-items:center;gap:11px;padding:13px 32px;font-size:14px;font-weight:600;flex-shrink:0}
.astrip.err{background:rgba(225,29,72,.08);color:#be123c;border-bottom:1px solid rgba(225,29,72,.16)}
.astrip.warn{background:rgba(217,119,6,.08);color:#b45309;border-bottom:1px solid rgba(217,119,6,.16)}
.astripx{cursor:pointer;font-size:15px;margin-left:auto;opacity:.6}
.astripx:hover{opacity:1}
.wrap{padding:32px;flex:1;max-width:1520px;margin:0 auto;width:100%}
.tabs{display:none}
.section-lbl{font-size:12px;font-weight:700;color:var(--muted-2);text-transform:uppercase;letter-spacing:1.5px;margin:26px 0 15px}
.detail{display:none;animation:fadein .35s ease}
.detail.visible{display:block}
@keyframes fadein{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}

/* ── Grade hero ───────────────────────────── */
.grade-hero{
  border-radius:var(--r);padding:36px 42px;margin-bottom:22px;
  display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:20px;
  position:relative;overflow:hidden;background:var(--card);
  box-shadow:var(--shadow-lg);border:1px solid var(--border);
}
.grade-hero::after{
  content:'';position:absolute;top:-70px;right:-70px;
  width:260px;height:260px;border-radius:50%;background:rgba(255,255,255,.05);
  pointer-events:none;
}
.grade-hero::before{
  content:'';position:absolute;bottom:-90px;left:20%;
  width:220px;height:220px;border-radius:50%;background:rgba(255,255,255,.03);
  pointer-events:none;
}
.gA-bg{background:linear-gradient(120deg,#0f3d27 0%,#14532d 45%,#16a34a 130%)}
.gB-bg{background:linear-gradient(120deg,#4a2c06 0%,#78350f 45%,#d97706 130%)}
.gC-bg{background:linear-gradient(120deg,#4c0519 0%,#881337 45%,#e11d48 130%)}
.gnull-bg{background:linear-gradient(120deg,#1e2540 0%,#242c4d 100%)}
.gh-left{display:flex;align-items:center;gap:20px;position:relative;z-index:1}
.gh-lbl{font-size:12px;font-weight:700;letter-spacing:2px;text-transform:uppercase;color:rgba(255,255,255,.55);margin-bottom:8px}
.gh-val{font-family:'JetBrains Mono',monospace;font-size:106px;font-weight:800;line-height:.9;color:#fff;letter-spacing:-3px}
.gh-msg{font-size:15.5px;color:rgba(255,255,255,.7);margin-top:10px;font-weight:500}
.gh-right{display:flex;align-items:center;gap:18px;position:relative;z-index:1}
.ring-wrap{position:relative;width:124px;height:124px;flex-shrink:0}
.ring-wrap svg{transform:rotate(-90deg)}
.ring-track{fill:none;stroke:rgba(255,255,255,.15);stroke-width:9}
.ring-fill{fill:none;stroke:#fff;stroke-width:9;stroke-linecap:round;transition:stroke-dashoffset .8s cubic-bezier(.4,0,.2,1)}
.ring-center{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.ring-num{font-family:'JetBrains Mono',monospace;font-size:26px;font-weight:800;color:#fff;line-height:1}
.ring-unit{font-size:10px;color:rgba(255,255,255,.65);font-weight:600;text-transform:uppercase;letter-spacing:.5px;margin-top:3px}
.gh-days-block{text-align:right}
.gh-days-lbl{font-size:11.5px;color:rgba(255,255,255,.55);margin-bottom:7px;letter-spacing:.5px;text-transform:uppercase;font-weight:600}
.gh-days-num{font-family:'JetBrains Mono',monospace;font-size:46px;font-weight:800;color:#fff;line-height:1}
.gh-days-unit{font-size:16px;color:rgba(255,255,255,.6);margin-left:5px;font-weight:500}

/* ── Metric cards ─────────────────────────── */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px;margin-bottom:10px}
.card{
  background:linear-gradient(160deg, var(--ctint,rgba(255,255,255,.03)), var(--card) 55%);
  border:1px solid var(--border);border-radius:var(--r);
  padding:23px 24px;transition:all .22s cubic-bezier(.4,0,.2,1);
  box-shadow:var(--shadow);position:relative;overflow:hidden;
}
.card:hover{transform:translateY(-3px);box-shadow:0 14px 32px rgba(15,23,42,.12);border-color:var(--border-h)}
.cicon{
  width:50px;height:50px;border-radius:14px;display:flex;align-items:center;justify-content:center;
  background:var(--cg);box-shadow:0 6px 16px var(--cs);margin-bottom:14px;
}
.cicon svg{width:25px;height:25px;color:#fff}
.clbl{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px}
.cval{font-family:'JetBrains Mono',monospace;font-size:31px;font-weight:700;color:var(--text);line-height:1}
.cunit{font-size:12.5px;color:var(--muted);font-weight:400}
.card.card-wide{grid-column:span 1}
.dq-mini-wrap{margin-top:12px}
.dq-mini-row{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-bottom:4px}

/* ── Charts ───────────────────────────────── */
.charts{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:8px}
@media(max-width:760px){.charts{grid-template-columns:1fr}}
.chartcard{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:24px;box-shadow:var(--shadow)}
.chartlbl{font-size:12.5px;font-weight:700;color:var(--muted);margin-bottom:16px;display:flex;gap:11px;align-items:center;flex-wrap:wrap}
.ldot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
svg.chart{width:100%;height:170px;display:block}

/* ── Log ──────────────────────────────────── */
.logbox{
  background:#f8fafc;color:#334155;
  font-family:'JetBrains Mono','Consolas',monospace;
  font-size:12.5px;line-height:1.9;padding:20px;border-radius:var(--r);
  max-height:320px;overflow-y:auto;white-space:pre;
  border:1px solid var(--border);
  box-shadow:inset 0 1px 4px rgba(15,23,42,.05), var(--shadow);
}
.rA{color:#15803d;font-weight:600}
.rB{color:#b45309;font-weight:600}
.rC{color:#be123c;font-weight:600}

@media(max-width:640px){
  .sidebar{width:100%;height:auto;position:relative}
  .app{flex-direction:column}
  .compare-grid{flex-direction:row;overflow-x:auto;flex-wrap:nowrap;padding:8px}
  .lot-card{min-width:180px}
  .gh-val{font-size:64px}
  .wrap{padding:16px}
}
footer{text-align:center;font-size:12.5px;color:var(--muted-2);padding:16px 0 24px;border-top:1px solid var(--border);flex-shrink:0}
</style>
</head>
<body>
<div class="app">

  <aside class="sidebar">
    <div class="sb-header">
      <div class="sb-logo">
        <div class="logo-icon">V</div>
        <div>
          <div class="logo-name">VegTrack</div>
          <div class="logo-sub">Monitoring v4</div>
        </div>
      </div>
    </div>
    <div class="sb-section">ล็อตผัก</div>
    <div class="compare-grid" id="compareGrid"></div>
    <div class="sb-section">การแจ้งเตือน</div>
    <div class="sb-alerts">
      <div class="alist" id="alist">
        <div class="aitem info"><span class="atime">-</span><span>รอข้อมูล...</span></div>
      </div>
    </div>
  </aside>

  <div class="main-area">
    <header class="topbar">
      <div class="topbar-lots" id="tlots"></div>
      <button id="soundToggle" class="sound-btn on" onclick="toggleSound()">🔔 เสียงแจ้งเตือน: เปิด</button>
      <span class="topbar-right" id="updlbl"></span>
    </header>
    <div class="astrip" id="astrip">
      <span>&#9888;</span>
      <span id="astripmsgs" style="flex:1"></span>
      <span class="astripx" onclick="this.parentElement.style.display='none'">&#10005;</span>
    </div>
    <div class="wrap">
      <div class="tabs" id="tabBar"></div>
      <div id="detailPanes"></div>
    </div>
  </div>

</div>
<footer>VegTrack v4 &mdash; ทีม BUZZA11DAY &mdash; ทำงานในเครื่อง ไม่ต้องใช้อินเทอร์เน็ต (ยกเว้นฟอนต์)</footer>
<div class="toast" id="toast"></div>

<script>
const GMSG={A:'ขายตามลำดับปกติ',B:'ควรเร่งขายก่อน',C:'ต้องขายวันนี้!'};
const REF_DAYS=3;   // ใช้ปรับสเกลวงแหวนวันที่เหลือ (baseline อายุคะน้าที่อุณหภูมิห้อง)
let selectedLot=null;
let lotNames=[];
let lastLotsData={};     // name -> lot object ล่าสุด (ไว้ให้ปุ่มบันทึก CSV ใช้)
let prevGrades={};       // name -> เกรดล่าสุดที่เคยเห็น (ไว้ตรวจจับตอนเปลี่ยนเป็น C)
let soundOn=true;
let audioCtx=null;

function toggleSound(){
  soundOn=!soundOn;
  const btn=document.getElementById('soundToggle');
  if(soundOn){
    btn.textContent='🔔 เสียงแจ้งเตือน: เปิด';
    btn.classList.add('on');
    try{
      if(!audioCtx)audioCtx=new (window.AudioContext||window.webkitAudioContext)();
      if(audioCtx.state==='suspended')audioCtx.resume();
    }catch(e){}
  }else{
    btn.textContent='🔕 เสียงแจ้งเตือน: ปิด';
    btn.classList.remove('on');
  }
}

function beep(){
  if(!soundOn)return;
  try{
    if(!audioCtx)audioCtx=new (window.AudioContext||window.webkitAudioContext)();
    const now=audioCtx.currentTime;
    [0,0.18].forEach(delay=>{
      const o=audioCtx.createOscillator();
      const g=audioCtx.createGain();
      o.type='sine';o.frequency.value=880;
      g.gain.setValueAtTime(0.0001,now+delay);
      g.gain.exponentialRampToValueAtTime(0.22,now+delay+0.02);
      g.gain.exponentialRampToValueAtTime(0.0001,now+delay+0.28);
      o.connect(g);g.connect(audioCtx.destination);
      o.start(now+delay);o.stop(now+delay+0.3);
    });
  }catch(e){}
}

function showToast(msg){
  const t=document.getElementById('toast');
  if(!t)return;
  t.textContent=msg;
  t.classList.add('show');
  clearTimeout(t._timer);
  t._timer=setTimeout(()=>t.classList.remove('show'),2600);
}

function csvEscape(v){
  v=(v==null?'':String(v));
  if(/[",\\n]/.test(v))return '"'+v.replace(/"/g,'""')+'"';
  return v;
}

function downloadCSV(lotName){
  const lot=lastLotsData[lotName];
  if(!lot||!lot.history||!lot.history.length){
    showToast('ยังไม่มีข้อมูลให้บันทึก');
    return;
  }
  const header=['time','temp_c','humidity_pct','pct_weight','days_left','grade','impact_total','event'];
  const rows=[header];
  lot.history.forEach(r=>rows.push([r.time,r.temp,r.humidity,r.pct_weight,r.days,r.grade,r.impact,r.event]));
  const csv=rows.map(r=>r.map(csvEscape).join(',')).join('\\r\\n');
  const blob=new Blob(['\ufeff'+csv],{type:'text/csv;charset=utf-8;'});
  const url=URL.createObjectURL(blob);
  const stamp=new Date().toISOString().slice(0,19).replace(/[:T]/g,'-');
  const a=document.createElement('a');
  a.href=url;
  a.download='vegtrack_'+lotName.replace(/\s+/g,'_')+'_'+stamp+'.csv';
  document.body.appendChild(a);a.click();document.body.removeChild(a);
  setTimeout(()=>URL.revokeObjectURL(url),1000);
  showToast('บันทึก CSV แล้ว: '+lot.history.length+' แถว');
}

const ICONS={
  temp:'<path d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z" stroke="currentColor" stroke-width="2"/><path d="M12 12V4a2 2 0 1 0-4 0v8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>',
  hum:'<path d="M12 3c4 5 7 8.5 7 12a7 7 0 1 1-14 0c0-3.5 3-7 7-12Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/>',
  weight:'<rect x="4" y="10" width="4" height="10" rx="1" stroke="currentColor" stroke-width="2"/><rect x="10" y="6" width="4" height="14" rx="1" stroke="currentColor" stroke-width="2"/><rect x="16" y="3" width="4" height="17" rx="1" stroke="currentColor" stroke-width="2"/>',
  impact:'<path d="M13 2 4 14h6l-1 8 9-12h-6l1-8Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>',
  event:'<path d="M3 12h4l2-7 4 14 2-7h6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
  quality:'<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6l-8-3Z" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="m9 12 2 2 4-4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>',
};

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
      <div class="lot-sensors">
        <span class="sbadge ${lot.connected?'sok':'serr'}">Serial ${lot.connected?'✓':'✗'}</span>
        <span class="sbadge ${lot.sensor_dht?'sok':'serr'}">DHT ${lot.sensor_dht?'✓':'✗'}</span>
        <span class="sbadge ${lot.sensor_weight?'sok':'serr'}">Scale ${lot.sensor_weight?'✓':'✗'}</span>
      </div>
      ${dqHtml}
    </div>`;
  }).join('');
}

function renderTabs(lots){}

function ensureDetailPanes(lots){
  const panes=document.getElementById('detailPanes');
  lots.forEach(lot=>{
    const id='pane_'+lot.name.replace(/\\s/g,'_');
    if(!document.getElementById(id)){
      const div=document.createElement('div');
      div.id=id;div.className='detail';
      div.innerHTML=`
        <div class="degraded-banner" id="${id}_degraded">
          <span>⚠</span>
          <span>DHT ขัดข้องชั่วคราว — กำลังแสดงเฉพาะผลจาก Edge AI สด (อุณหภูมิ/ความชื้นค้างค่าเก่าล่าสุด)</span>
        </div>
        <div class="grade-hero gnull-bg" id="${id}_hero">
          <div class="gh-left">
            <div>
              <div class="gh-lbl">เกรดผัก &middot; ${esc(lot.name)}</div>
              <div class="gh-val" id="${id}_gv">-</div>
              <div class="gh-msg" id="${id}_gmsg">รอข้อมูล...</div>
            </div>
          </div>
          <div class="gh-right">
            <div class="ring-wrap">
              <svg viewBox="0 0 124 124" width="124" height="124">
                <circle class="ring-track" cx="62" cy="62" r="52"/>
                <circle class="ring-fill" id="${id}_ring" cx="62" cy="62" r="52"
                        stroke-dasharray="326.7" stroke-dashoffset="326.7"/>
              </svg>
              <div class="ring-center">
                <div class="ring-num" id="${id}_ringnum">-</div>
                <div class="ring-unit">น้ำหนัก%</div>
              </div>
            </div>
            <div class="gh-days-block">
              <div class="gh-days-lbl">วันที่เหลือ</div>
              <div><span class="gh-days-num" id="${id}_dv">-</span><span class="gh-days-unit">วัน</span></div>
            </div>
          </div>
        </div>

        <div class="cards">
          <div class="card" style="--cg:linear-gradient(135deg,#f43f5e,#fb923c);--cs:rgba(244,63,94,.35)">
            <div class="cicon" style="background:var(--cg);box-shadow:0 6px 16px var(--cs)"><svg viewBox="0 0 24 24" fill="none">${ICONS.temp}</svg></div>
            <div class="clbl">อุณหภูมิ</div>
            <div class="cval"><span id="${id}_tv">-</span><span class="cunit"> °C</span></div>
          </div>
          <div class="card" style="--cg:linear-gradient(135deg,#3b82f6,#22d3ee);--cs:rgba(59,130,246,.35)">
            <div class="cicon" style="background:var(--cg);box-shadow:0 6px 16px var(--cs)"><svg viewBox="0 0 24 24" fill="none">${ICONS.hum}</svg></div>
            <div class="clbl">ความชื้น</div>
            <div class="cval"><span id="${id}_hv">-</span><span class="cunit"> %</span></div>
          </div>
          <div class="card" style="--cg:linear-gradient(135deg,#14b8a6,#4ade80);--cs:rgba(20,184,166,.35)">
            <div class="cicon" style="background:var(--cg);box-shadow:0 6px 16px var(--cs)"><svg viewBox="0 0 24 24" fill="none">${ICONS.weight}</svg></div>
            <div class="clbl">น้ำหนักคงเหลือ</div>
            <div class="cval"><span id="${id}_wv">-</span><span class="cunit"> %</span></div>
          </div>
          <div class="card" style="--cg:linear-gradient(135deg,#8b5cf6,#ec4899);--cs:rgba(139,92,246,.35)">
            <div class="cicon" style="background:var(--cg);box-shadow:0 6px 16px var(--cs)"><svg viewBox="0 0 24 24" fill="none">${ICONS.impact}</svg></div>
            <div class="clbl">กระแทกสะสม</div>
            <div class="cval" id="${id}_iv">-</div>
          </div>
          <div class="card" style="--cg:linear-gradient(135deg,#f59e0b,#fbbf24);--cs:rgba(245,158,11,.35)">
            <div class="cicon" style="background:var(--cg);box-shadow:0 6px 16px var(--cs)"><svg viewBox="0 0 24 24" fill="none">${ICONS.event}</svg></div>
            <div class="clbl">เหตุการณ์ล่าสุด<span class="live-pulse" title="Edge AI ทำงานสด"></span></div>
            <div class="cval" style="font-size:16px;padding-top:2px" id="${id}_ev">-</div>
          </div>
          <div class="card" style="--cg:linear-gradient(135deg,#22c55e,#0d9488);--cs:rgba(34,197,94,.35)">
            <div class="cicon" style="background:var(--cg);box-shadow:0 6px 16px var(--cs)"><svg viewBox="0 0 24 24" fill="none">${ICONS.quality}</svg></div>
            <div class="clbl">คุณภาพข้อมูล</div>
            <div class="dq-mini-row"><span>บันทึก: <b style="color:#4ade80" id="${id}_saved">0</b></span><span>กรองออก: <b style="color:#f59e0b" id="${id}_skip">0</b></span></div>
            <div class="dq-bar" style="margin-top:6px"><div class="dq-fill dq-good" id="${id}_dqfill" style="width:0%"></div></div>
          </div>
        </div>

        <div class="charts">
          <div class="chartcard">
            <div class="chartlbl">
              <span class="ldot" style="background:#e11d48"></span>อุณหภูมิ °C
              &nbsp;<span class="ldot" style="background:#0891b2"></span>ความชื้น %
            </div>
            <svg class="chart" id="${id}_svgTH" viewBox="0 0 600 140" preserveAspectRatio="none"></svg>
          </div>
          <div class="chartcard">
            <div class="chartlbl"><span class="ldot" style="background:#16a34a"></span>วันที่เหลือ (Days)</div>
            <svg class="chart" id="${id}_svgDays" viewBox="0 0 600 140" preserveAspectRatio="none"></svg>
          </div>
        </div>

        <div class="log-header">
          <div class="section-lbl">Raw Log</div>
          <button class="save-btn" data-lot="${esc(lot.name)}">
            <svg viewBox="0 0 24 24" fill="none"><path d="M12 3v12m0 0 4-4m-4 4-4-4M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
            บันทึก CSV
          </button>
        </div>
        <div class="logbox" id="${id}_log">รอข้อมูล...</div>`;
      panes.appendChild(div);
      const saveBtn = div.querySelector('.save-btn');
      if(saveBtn) saveBtn.addEventListener('click', ()=>downloadCSV(lot.name));
    }
  });
}

function selectLot(name){
  selectedLot=name;
  document.querySelectorAll('.detail').forEach(d=>d.classList.remove('visible'));
  const id='pane_'+name.replace(/\\s/g,'_');
  const p=document.getElementById(id);
  if(p)p.classList.add('visible');
  document.querySelectorAll('.lot-card').forEach(c=>{
    c.classList.toggle('selected',c.querySelector('.lot-name')?.textContent===name);
  });
}

function updateDetail(lot){
  const id='pane_'+lot.name.replace(/\\s/g,'_');
  const s=(sid,v)=>{const el=document.getElementById(id+sid);if(el)el.textContent=v??'-';};
  const grade=lot.grade||'-';
  const hero=document.getElementById(id+'_hero');
  const gEl=document.getElementById(id+'_gv');
  const gMsg=document.getElementById(id+'_gmsg');
  if(hero)hero.className='grade-hero g'+(grade==='-'?'null':grade)+'-bg';
  if(gEl)gEl.textContent=grade;
  if(gMsg)gMsg.textContent=GMSG[grade]||'รอข้อมูล...';
  s('_tv',lot.temp);s('_hv',lot.humidity);s('_wv',lot.pct_weight);
  s('_dv',lot.days);s('_iv',lot.impact);s('_ev',lot.event);
  s('_skip',lot.rows_skipped);s('_saved',lot.rows_saved);

  // DHT ขัดข้องชั่วคราว -> โชว์แบนเนอร์ + ทำการ์ดอุณหภูมิ/ความชื้น/น้ำหนัก/วันที่ให้จาง (ค้างค่าล่าสุด)
  const dhtOk = lot.sensor_dht !== false;
  const banner = document.getElementById(id+'_degraded');
  if(banner) banner.classList.toggle('show', !dhtOk);
  const tCard = document.getElementById(id+'_tv') && document.getElementById(id+'_tv').closest('.card');
  const hCard = document.getElementById(id+'_hv') && document.getElementById(id+'_hv').closest('.card');
  const wCard = document.getElementById(id+'_wv') && document.getElementById(id+'_wv').closest('.card');
  const daysBlock = document.getElementById(id+'_dv') && document.getElementById(id+'_dv').closest('.gh-days-block');
  [tCard,hCard,wCard,daysBlock].forEach(function(el){ if(el) el.classList.toggle('stale', !dhtOk); });

  // ring = % น้ำหนักคงเหลือ (ข้อมูลจริงจากบอร์ด ไม่ใช่ค่าประดิษฐ์)
  const ring=document.getElementById(id+'_ring');
  const ringnum=document.getElementById(id+'_ringnum');
  const CIRC=326.7;
  if(ring){
    const pct=(lot.pct_weight!=null)?Math.max(0,Math.min(100,lot.pct_weight)):0;
    ring.style.strokeDashoffset=String(CIRC-(pct/100)*CIRC);
  }
  if(ringnum)ringnum.textContent=(lot.pct_weight!=null)?lot.pct_weight+'%':'-';

  const dq=dataQuality(lot);
  const dqfill=document.getElementById(id+'_dqfill');
  if(dqfill&&dq){dqfill.style.width=dq.pct+'%';dqfill.className='dq-fill '+dq.cls;}

  const th=document.getElementById(id+'_svgTH');
  const dsvg=document.getElementById(id+'_svgDays');
  if(th)drawLines(th,[lot.history.map(r=>r.temp),lot.history.map(r=>r.humidity)],['#e11d48','#0891b2'],false);
  if(dsvg)drawLines(dsvg,[lot.history.map(r=>r.days)],['#16a34a'],true);

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

function drawLines(svg,series,colors,marker,W=600,H=140,P=20){
  svg.innerHTML='';
  const defs=document.createElementNS('http://www.w3.org/2000/svg','defs');
  svg.appendChild(defs);
  for(let i=0;i<=3;i++){
    const y=P+i*(H-2*P)/3;
    const el=document.createElementNS('http://www.w3.org/2000/svg','line');
    el.setAttribute('x1',P);el.setAttribute('x2',W-P);
    el.setAttribute('y1',y);el.setAttribute('y2',y);
    el.setAttribute('stroke','rgba(15,23,42,0.07)');el.setAttribute('stroke-width','1');
    svg.appendChild(el);
  }
  const n=series[0]?.length||0;if(n<2)return;
  series.forEach((pts,si)=>{
    const vals=pts.filter(v=>v!=null);
    if(vals.length<2)return;
    const mx=Math.max(...vals),mn=Math.min(...vals),rng=(mx-mn)||1;
    const x=i=>P+(i/(n-1))*(W-2*P);
    const y=v=>H-P-((v-mn)/rng)*(H-2*P);

    const gid='grad'+si+Math.random().toString(36).slice(2,8);
    const grad=document.createElementNS('http://www.w3.org/2000/svg','linearGradient');
    grad.setAttribute('id',gid);grad.setAttribute('x1','0');grad.setAttribute('y1','0');grad.setAttribute('x2','0');grad.setAttribute('y2','1');
    const st1=document.createElementNS('http://www.w3.org/2000/svg','stop');
    st1.setAttribute('offset','0%');st1.setAttribute('stop-color',colors[si]);st1.setAttribute('stop-opacity','.28');
    const st2=document.createElementNS('http://www.w3.org/2000/svg','stop');
    st2.setAttribute('offset','100%');st2.setAttribute('stop-color',colors[si]);st2.setAttribute('stop-opacity','0');
    grad.appendChild(st1);grad.appendChild(st2);defs.appendChild(grad);

    let d='',area='',prev=null,firstX=null,lastX=null;
    pts.forEach((v,i)=>{
      if(v==null){prev=null;return;}
      const px=x(i),py=y(v);
      if(prev===null){d+='M '+px+' '+py;area+='M '+px+' '+H+' L '+px+' '+py;firstX=firstX??px;}
      else{d+=' L '+px+' '+py;area+=' L '+px+' '+py;}
      lastX=px;prev=i;
    });
    if(lastX!=null)area+=' L '+lastX+' '+H+' Z';

    const areaPath=document.createElementNS('http://www.w3.org/2000/svg','path');
    areaPath.setAttribute('d',area);areaPath.setAttribute('fill','url(#'+gid+')');areaPath.setAttribute('stroke','none');
    svg.appendChild(areaPath);

    const path=document.createElementNS('http://www.w3.org/2000/svg','path');
    path.setAttribute('d',d);path.setAttribute('fill','none');
    path.setAttribute('stroke',colors[si]);path.setAttribute('stroke-width','2.4');
    path.setAttribute('stroke-linecap','round');path.setAttribute('stroke-linejoin','round');
    svg.appendChild(path);
    if(marker){
      const li=pts.reduce((a,v,i)=>v!=null?i:a,-1);
      if(li>=0){
        const glow=document.createElementNS('http://www.w3.org/2000/svg','circle');
        glow.setAttribute('cx',x(li));glow.setAttribute('cy',y(pts[li]));
        glow.setAttribute('r','8');glow.setAttribute('fill',colors[si]);glow.setAttribute('opacity','.25');
        svg.appendChild(glow);
        const c=document.createElementNS('http://www.w3.org/2000/svg','circle');
        c.setAttribute('cx',x(li));c.setAttribute('cy',y(pts[li]));
        c.setAttribute('r','4');c.setAttribute('fill',colors[si]);
        c.setAttribute('stroke','#ffffff');c.setAttribute('stroke-width','1.5');
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

  document.getElementById('tlots').innerHTML=lots.map(l=>
    `<div class="tlot"><span class="tdot ${l.connected?'on':''}"></span>${esc(l.name)}</div>`
  ).join('');
  const anyTime=lots.find(l=>l.time);
  if(anyTime)document.getElementById('updlbl').textContent='อัปเดต '+anyTime.time;

  const alerts=j.alerts||[];
  const strip=document.getElementById('astrip');
  if(alerts.length){
    strip.style.display='flex';
    strip.className='astrip '+(alerts.some(a=>a.level==='err')?'err':'warn');
    document.getElementById('astripmsgs').textContent=alerts.map(a=>a.msg).join('  |  ');
  }

  const sorted=[...lots].sort((a,b)=>urgency(b)-urgency(a));
  if(!selectedLot||!lotNames.includes(selectedLot))selectedLot=sorted[0].name;

  // ตรวจจับการเปลี่ยนเป็นเกรด C ครั้งแรก -> ส่งเสียงเตือน (ไม่ร้องซ้ำถ้ายังเป็น C ต่อเนื่อง)
  lots.forEach(l=>{
    lastLotsData[l.name]=l;
    if(l.connected&&l.grade==='C'&&prevGrades[l.name]!=='C'){
      beep();
      showToast('⚠ '+l.name+' ถึงเกรด C แล้ว — ต้องขายวันนี้!');
    }
    prevGrades[l.name]=l.grade;
  });

  renderCompare(lots);
  renderTabs(lots);
  ensureDetailPanes(lots);

  document.querySelectorAll('.detail').forEach(d=>d.classList.remove('visible'));
  const selPane=document.getElementById('pane_'+selectedLot.replace(/\\s/g,'_'));
  if(selPane)selPane.classList.add('visible');

  lots.forEach(lot=>updateDetail(lot));

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
    print("VegTrack Dashboard v4 พร้อมแล้ว")
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