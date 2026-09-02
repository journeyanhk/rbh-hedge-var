"""Read-only monitor — loopback-bound, mirrors VO's 8011 / rbh's 8010 pattern.

Serves the current snapshot + state.json as JSON and a tiny HTML page. It never
mutates anything and binds to 127.0.0.1 by default.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _read_json(path: str) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


HTML = """<!doctype html><html><head><meta charset=utf-8>
<title>rbh-hedge-var monitor (shadow)</title>
<meta http-equiv=refresh content=15>
<style>body{font:14px system-ui;margin:24px;background:#0b0e14;color:#cdd6f4}
h1{font-size:18px}.tag{background:#f38ba8;color:#11111b;padding:2px 8px;border-radius:4px;font-weight:700}
table{border-collapse:collapse;margin:12px 0}td,th{border:1px solid #313244;padding:6px 12px;text-align:left}
.k{color:#89b4fa}.warn{color:#f9e2af}.ok{color:#a6e3a1}.bad{color:#f38ba8}pre{background:#181825;padding:12px;border-radius:6px}</style>
</head><body>
<h1>rbh-hedge-var <span class=tag>PHASE 1 · SHADOW · NO REAL ORDERS</span></h1>
<div id=body>loading…</div>
<script>
async function load(){
 const r=await fetch('state.json'+'?_='+Date.now());const d=await r.json();
 const s=d.snapshot||{},st=d.state||{};
 const v=st.funding_verified?'<span class=ok>VERIFIED</span>':'<span class=bad>'+(s.funding_unit_status||'unknown')+'</span>';
 let h='<table>';
 const rows=[['mode',st.mode],['round_id',st.round_id],['direction',st.direction||'-'],
  ['candidate_direction',s.candidate_direction||'-'],['funding unit',v],['funding reason',s.funding_unit_reason||''],
  ['var_price',s.var_price],['lighter_price',s.lighter_price],['basis',s.basis],
  ['var_funding_hourly',s.var_funding_hourly],['lighter_funding_hourly',s.lighter_funding_hourly],
  ['spread_hourly',s.spread_hourly],['net_funding_hourly_usdt',s.net_funding_hourly_usdt],
  ['break_even_hours',s.break_even_hours],['realized_pnl(shadow)',st.realized_pnl],
  ['reversal_streak',st.reversal_streak],['last_reason',st.last_reason]];
 for(const[k,val]of rows)h+='<tr><td class=k>'+k+'</td><td>'+(val==null?'-':val)+'</td></tr>';
 h+='</table><h3>round history</h3><pre>'+JSON.stringify(st.round_history||[],null,1)+'</pre>';
 document.getElementById('body').innerHTML=h;
}
load();setInterval(load,15000);
</script></body></html>"""


def make_handler(state_file: str, get_snapshot):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def do_GET(self):
            if self.path.startswith("/state.json"):
                payload = {"state": _read_json(state_file), "snapshot": get_snapshot()}
                body = json.dumps(payload, default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    return Handler


def serve(state_file: str, get_snapshot, host: str = "127.0.0.1", port: int = 8012) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(state_file, get_snapshot))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    print(f"[monitor] read-only dashboard on http://{host}:{port}/", flush=True)
    return httpd
