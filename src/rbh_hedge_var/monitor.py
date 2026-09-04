"""Read-only monitor — loopback-bound, mirrors VO's 8011 / rbh's 8010 pattern.

Serves the current snapshot + state.json as JSON and a minimalist, mobile-first
HTML dashboard (Chinese, flat design, light/dark via prefers-color-scheme). It
never mutates anything and binds to 127.0.0.1 by default.

review16 dashboard (plan C — pure UI, no trade-main-chain changes):
  * 状态灯条  — engine mode / trade mode / HALT / funding-verified /
                attestation validity / data source, red-green dots.
  * 持仓风险  — each risk dimension shows current vs trigger + a proximity bar
                (stop-loss, take-profit, reversal, max-hold, daily loss, basis).
  * 回合统计  — /api/rounds aggregates shadow_rounds.jsonl server-side: totals,
                win-rate, cumulative PnL (price/funding split), avg hold, exit
                reason distribution, last-20 table, cumulative-PnL canvas chart.
  * 心跳过期横幅 — if state.last_update is older than 2x the poll interval the
                whole page shows a red banner; a dead engine can no longer sit
                behind a wall of stale green lights.

Statistics only render fields that actually exist in the ledger today
(direction/time/pnl/reason). Latency/slippage columns are deliberately absent
until the data-layer enrichment lands post-validation (see var-desgin4.md).
"""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def _read_json(path: str) -> dict[str, Any]:
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return {}


def _rounds_path(state_file: str) -> Path:
    return Path(state_file).parent / "shadow_rounds.jsonl"


def _read_rounds(state_file: str) -> list[dict[str, Any]]:
    """Read the append-only shadow ledger, tolerant of partial/legacy rows."""
    rows: list[dict[str, Any]] = []
    p = _rounds_path(state_file)
    if not p.exists():
        return rows
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return rows
    return rows


def _reason_bucket(reason: Any) -> str:
    """Group a verbose exit reason into a short bucket for the distribution."""
    if not reason:
        return "unknown"
    return re.split(r"[ :]", str(reason))[0] or "unknown"


def _num(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def aggregate_rounds(state_file: str) -> dict[str, Any]:
    """Server-side aggregation of the shadow ledger (tolerant of missing fields).

    New/old records mix freely because every access is a .get(); once the
    data-layer enrichment lands, richer fields simply appear here without a
    schema migration."""
    rows = _read_rounds(state_file)
    n = len(rows)
    wins = sum(1 for r in rows if _num(r.get("pnl")) > 0)
    losses = sum(1 for r in rows if _num(r.get("pnl")) < 0)
    cum_pnl = sum(_num(r.get("pnl")) for r in rows)
    cum_price = sum(_num(r.get("price_pnl")) for r in rows)
    cum_funding = sum(_num(r.get("funding_pnl")) for r in rows)

    holds = [
        _num(r.get("closed_at")) - _num(r.get("opened_at"))
        for r in rows
        if r.get("closed_at") and r.get("opened_at")
    ]
    avg_hold_s = (sum(holds) / len(holds)) if holds else None

    reasons: dict[str, int] = {}
    for r in rows:
        b = _reason_bucket(r.get("reason"))
        reasons[b] = reasons.get(b, 0) + 1

    # cumulative-PnL series for the canvas line chart
    series: list[float] = []
    run = 0.0
    for r in rows:
        run += _num(r.get("pnl"))
        series.append(round(run, 4))

    last20 = [
        {
            "round_id": r.get("round_id"),
            "direction": r.get("direction"),
            "opened_at": r.get("opened_at"),
            "closed_at": r.get("closed_at"),
            "hold_s": (_num(r.get("closed_at")) - _num(r.get("opened_at")))
            if (r.get("closed_at") and r.get("opened_at")) else None,
            "reason": r.get("reason"),
            "price_pnl": _num(r.get("price_pnl")),
            "funding_pnl": _num(r.get("funding_pnl")),
            "pnl": _num(r.get("pnl")),
            "shadow": bool(r.get("shadow", True)),
        }
        for r in rows[-20:]
    ]
    last20.reverse()

    return {
        "total": n,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / n) if n else None,
        "cum_pnl": round(cum_pnl, 4),
        "cum_price_pnl": round(cum_price, 4),
        "cum_funding_pnl": round(cum_funding, 4),
        "avg_hold_s": avg_hold_s,
        "reasons": reasons,
        "series": series,
        "last20": last20,
    }


def _thresholds(cfg: dict[str, Any]) -> dict[str, Any]:
    """Trigger lines the risk panel draws proximity bars against."""
    return {
        "max_round_loss_usdt": _num(cfg.get("max_round_loss_usdt")),
        "take_profit_total_pnl_usdt": _num(cfg.get("take_profit_total_pnl_usdt")),
        "max_daily_loss_usdt": _num(cfg.get("max_daily_loss_usdt")),
        "max_hold_hours": _num(cfg.get("max_hold_hours")),
        "spread_reversal_confirm_ticks": int(cfg.get("spread_reversal_confirm_ticks", 3) or 0),
        "force_exit_basis_pct": _num(cfg.get("force_exit_basis_pct")),
        "max_basis_pct": _num(cfg.get("max_basis_pct")),
    }


HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>对冲监控</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--fg:#1c1e26;--muted:#6b7280;--line:#e5e7eb;
 --ok:#16a34a;--warn:#d97706;--bad:#dc2626;--accent:#2563eb;--track:#eef0f3}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b22;--fg:#e6e8ec;
 --muted:#9aa3b2;--line:#262b36;--ok:#22c55e;--warn:#f59e0b;--bad:#f87171;
 --accent:#60a5fa;--track:#232833}}
*{box-sizing:border-box}
body{margin:0;padding:14px;font:15px/1.5 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
 background:var(--bg);color:var(--fg);-webkit-text-size-adjust:100%}
.wrap{max-width:720px;margin:0 auto}
header{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:12px}
h1{font-size:17px;margin:0;font-weight:600}
.badge{font-size:12px;font-weight:700;padding:3px 10px;border-radius:999px}
.badge.live{background:var(--bad);color:#fff}.badge.shadow{background:var(--ok);color:#fff}
#stale{display:none;background:var(--bad);color:#fff;padding:10px 14px;border-radius:10px;
 font-weight:600;margin-bottom:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:12px}
.card h2{font-size:14px;margin:0 0 12px;color:var(--muted);font-weight:600;letter-spacing:.03em}
.lights{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.light{display:flex;align-items:center;gap:8px}
.dot{width:10px;height:10px;border-radius:50%;flex:none}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
.dot.off{background:var(--muted)}
.light .lbl{color:var(--muted);font-size:13px}.light .val{font-weight:600;margin-left:auto;text-align:right}
.risk{margin-bottom:14px}.risk:last-child{margin-bottom:0}
.risk .top{display:flex;justify-content:space-between;font-size:13px;margin-bottom:5px}
.risk .top .lbl{color:var(--fg);font-weight:600}
.risk .top .nums{color:var(--muted)}
.bar{height:8px;border-radius:999px;background:var(--track);overflow:hidden}
.bar>i{display:block;height:100%;border-radius:999px}
.fill-bad{background:var(--bad)}.fill-warn{background:var(--warn)}.fill-ok{background:var(--ok)}.fill-acc{background:var(--accent)}
.note{color:var(--muted);font-size:12px;margin-top:4px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(105px,1fr));gap:10px;margin-bottom:12px}
.stat{background:var(--track);border-radius:10px;padding:9px 11px}
.stat .k{color:var(--muted);font-size:12px}.stat .v{font-size:18px;font-weight:700;margin-top:2px}
.pos{color:var(--ok)}.neg{color:var(--bad)}
canvas{width:100%;height:120px;display:block;margin:6px 0 10px}
.reasons{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px}
.chip{background:var(--track);border-radius:999px;padding:3px 10px;font-size:12px}
.chip b{color:var(--accent)}
.tbl{width:100%;border-collapse:collapse;font-size:12.5px}
.tbl th,.tbl td{padding:6px 6px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}
.tbl th{color:var(--muted);font-weight:600;text-align:right}
.tbl th:first-child,.tbl td:first-child{text-align:left}
.tblwrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.foot{color:var(--muted);font-size:12px;text-align:center;margin-top:6px}
.empty{color:var(--muted);font-size:13px}
</style></head><body><div class=wrap>
<header><h1>对冲监控 · XAU</h1><span id=trade class="badge shadow">影子</span></header>
<div id=stale></div>
<div class=card><h2>系统状态</h2><div class=lights id=lights>加载中…</div></div>
<div class=card><h2>持仓风险</h2><div id=risk class=empty>加载中…</div></div>
<div class=card><h2>回合统计</h2>
 <div class=stats id=stats></div>
 <canvas id=chart></canvas>
 <div class=reasons id=reasons></div>
 <div class=tblwrap><table class=tbl id=tbl></table></div>
</div>
<div class=foot id=foot></div>
</div>
<script>
var POLL=60;
function fx(n,d){if(n==null||n==='')return '-';var v=Number(n);if(isNaN(v))return String(n);
 return v.toFixed(d==null?2:d);}
function sgn(v){return v>0?'pos':(v<0?'neg':'');}
function dur(s){if(s==null)return '-';s=Math.max(0,Math.floor(s));var h=Math.floor(s/3600),m=Math.floor((s%3600)/60);
 if(h>0)return h+'时'+m+'分';var ss=s%60;return m+'分'+ss+'秒';}
function tm(ep){if(!ep)return '-';var d=new Date(ep*1000);
 function p(x){return (x<10?'0':'')+x;}return p(d.getMonth()+1)+'/'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes());}
function clamp(v){return Math.max(0,Math.min(100,v));}
function light(dot,lbl,val){return '<div class=light><span class="dot '+dot+'"></span>'+
 '<span class=lbl>'+lbl+'</span><span class=val>'+val+'</span></div>';}
function bar(pct,cls){return '<div class=bar><i class="'+cls+'" style="width:'+clamp(pct)+'%"></i></div>';}
function riskRow(lbl,nums,pct,cls,note){return '<div class=risk><div class=top><span class=lbl>'+lbl+
 '</span><span class=nums>'+nums+'</span></div>'+bar(pct,cls)+(note?'<div class=note>'+note+'</div>':'')+'</div>';}

function renderStatus(s,st){
 var t=document.getElementById('trade');
 var liveOn=s.live_armed;t.className='badge '+(liveOn?'live':'shadow');t.textContent=liveOn?'实盘':'影子';
 var L=[];
 var mode=st.mode||s.mode||'-';var modeCn={IDLE:'空闲',ENTERING:'开仓中',HOLDING:'持仓中',EXITING:'平仓中',COOLDOWN:'冷却中'}[mode]||mode;
 L.push(light(mode=='HOLDING'?'ok':(mode=='COOLDOWN'?'warn':'off'),'引擎状态',modeCn));
 L.push(light(liveOn?'bad':'ok','交易模式',liveOn?'实盘下单':'影子模拟'));
 var halt=st.halt;L.push(light(halt?'bad':'ok','熔断','<span class="'+(halt?'neg':'')+'">'+(halt?('已熔断'):'正常')+'</span>'));
 var fv=st.funding_verified;L.push(light(fv?'ok':'warn','资金费验证',fv?'已验证':(s.funding_unit_status||'未验证')));
 var att=st.funding_attestation,ax='-',adot='off';
 if(att&&att.expires_at){var rem=att.expires_at-Math.floor(Date.now()/1000);
  if(rem<=0){ax='已过期';adot='bad';}else{ax=(rem/3600).toFixed(1)+'小时';adot=rem<43200?'warn':'ok';}}
 L.push(light(adot,'签名有效期',ax));
 var de=s.data_errors&&Object.keys(s.data_errors).length;
 L.push(light(de?'bad':'ok','数据源',de?'异常':'正常'));
 document.getElementById('lights').innerHTML=L.join('');
 if(halt){document.getElementById('lights').innerHTML+=
  '<div class=light style="grid-column:1/-1"><span class="dot bad"></span><span class=lbl>熔断原因</span>'+
  '<span class=val style="color:var(--bad)">'+(halt.reason||'')+'</span></div>';}
}

function renderRisk(s,st,th){
 var el=document.getElementById('risk');var mode=st.mode||s.mode;var H=[];
 if(mode=='HOLDING'){
  // 止损：round_pnl_vs_entry 相对开仓基线的恶化
  if(th.max_round_loss_usdt>0){var adv=Number(s.round_pnl_vs_entry_usdt||0);
   var p=clamp(-adv/th.max_round_loss_usdt*100);
   H.push(riskRow('止损距离','当前 '+fx(adv)+'U / 触发 -'+fx(th.max_round_loss_usdt)+'U',p,'fill-bad',
    adv<0?('已亏损 '+fx(-adv)+'U，距强平 '+fx(th.max_round_loss_usdt+adv)+'U'):'尚未亏损'));}
  // 止盈：未实现总盈亏 -> 目标
  if(th.take_profit_total_pnl_usdt>0){var tot=Number(s.unrealized_total_pnl_usdt||0);
   var p2=clamp(tot/th.take_profit_total_pnl_usdt*100);
   H.push(riskRow('止盈进度','当前 '+fx(tot)+'U / 目标 '+fx(th.take_profit_total_pnl_usdt)+'U',p2,'fill-ok',
    '达到目标即自动止盈平仓'));}
  // 反转计数
  if(th.spread_reversal_confirm_ticks>0){var rs=Number(st.reversal_streak||0);
   var p3=clamp(rs/th.spread_reversal_confirm_ticks*100);
   H.push(riskRow('资金费反转','已连续 '+rs+' 次 / 触发 '+th.spread_reversal_confirm_ticks+' 次',p3,'fill-warn',
    '连续反转达标即平仓'));}
  // 最大持仓倒计时
  if(th.max_hold_hours>0&&st.opened_at){var held=Math.floor(Date.now()/1000)-Number(st.opened_at);
   var maxs=th.max_hold_hours*3600;var p4=clamp(held/maxs*100);var rem=maxs-held;
   H.push(riskRow('最大持仓','已持 '+dur(held)+' / 上限 '+th.max_hold_hours+'小时',p4,p4>80?'fill-bad':'fill-acc',
    rem>0?('剩余 '+dur(rem)+' 强制平仓'):'已超时，下一tick平仓'));}
 }else{
  H.push('<div class=empty>当前空仓（'+({IDLE:'空闲等待信号',COOLDOWN:'冷却中',ENTERING:'开仓中',EXITING:'平仓中'}[mode]||mode)+
   '），以下为常驻风控。</div>');
 }
 // 常驻：basis 偏离 + 日内亏损（无论是否持仓都相关）
 if(th.force_exit_basis_pct>0){var b=Math.abs(Number(s.basis||0));var pb=clamp(b/th.force_exit_basis_pct*100);
  H.push(riskRow('基差偏离','当前 '+(b*100).toFixed(3)+'% / 强平 '+(th.force_exit_basis_pct*100).toFixed(2)+'%',pb,'fill-warn',
   '两所价差过大触发强制平仓'));}
 if(th.max_daily_loss_usdt>0){var dp=0,dpm=st.daily_pnl||{};for(var k in dpm){}
  // 取今天(UTC)的日内盈亏
  var utc=new Date();var key=utc.getUTCFullYear()+'-'+('0'+(utc.getUTCMonth()+1)).slice(-2)+'-'+('0'+utc.getUTCDate()).slice(-2);
  dp=Number(dpm[key]||0);var pd=clamp(-dp/th.max_daily_loss_usdt*100);
  H.push(riskRow('日内亏损熔断','今日 '+fx(dp)+'U / 熔断 -'+fx(th.max_daily_loss_usdt)+'U',pd,'fill-bad',
   dp<0?('距熔断 '+fx(th.max_daily_loss_usdt+dp)+'U'):'今日未亏损'));}
 // 实盘/账面持仓
 var lp=s.live_positions;
 if(lp){H.push('<div class=note>实盘持仓：Lighter='+lp.lighter+' · Variational='+lp.variational+'</div>');}
 else{H.push('<div class=note>实盘持仓：影子模式（无真实持仓）</div>');}
 el.className='';el.innerHTML=H.join('');
}

function drawChart(series){
 var c=document.getElementById('chart');var dpr=window.devicePixelRatio||1;
 var w=c.clientWidth,h=120;c.width=w*dpr;c.height=h*dpr;var g=c.getContext('2d');g.scale(dpr,dpr);
 g.clearRect(0,0,w,h);
 var cs=getComputedStyle(document.documentElement);
 var acc=cs.getPropertyValue('--accent').trim(),line=cs.getPropertyValue('--line').trim(),
  ok=cs.getPropertyValue('--ok').trim(),bad=cs.getPropertyValue('--bad').trim();
 if(!series||series.length<1){g.fillStyle=cs.getPropertyValue('--muted').trim();g.font='13px sans-serif';
  g.fillText('暂无回合数据',10,h/2);return;}
 var min=Math.min(0,Math.min.apply(null,series)),max=Math.max(0,Math.max.apply(null,series));
 if(max===min){max+=1;min-=1;}
 var pad=6,iw=w-pad*2,ih=h-pad*2;
 function X(i){return pad+(series.length<=1?iw/2:iw*i/(series.length-1));}
 function Y(v){return pad+ih-(v-min)/(max-min)*ih;}
 // zero baseline
 g.strokeStyle=line;g.lineWidth=1;g.beginPath();g.moveTo(pad,Y(0));g.lineTo(w-pad,Y(0));g.stroke();
 // series line
 g.strokeStyle=series[series.length-1]>=0?ok:bad;g.lineWidth=2;g.beginPath();
 for(var i=0;i<series.length;i++){var x=X(i),y=Y(series[i]);if(i===0)g.moveTo(x,y);else g.lineTo(x,y);}
 g.stroke();
 // last point dot
 g.fillStyle=acc;g.beginPath();g.arc(X(series.length-1),Y(series[series.length-1]),3,0,7);g.fill();
}

function renderRounds(a){
 var pnlCls=sgn(a.cum_pnl);
 var S=[['累计盈亏',fx(a.cum_pnl)+'U',pnlCls],['总回合',a.total,''],
  ['胜率',a.win_rate==null?'-':(a.win_rate*100).toFixed(0)+'%',''],
  ['盈/亏',a.wins+' / '+a.losses,''],
  ['价差盈亏',fx(a.cum_price_pnl)+'U',sgn(a.cum_price_pnl)],
  ['资金费盈亏',fx(a.cum_funding_pnl)+'U',sgn(a.cum_funding_pnl)],
  ['平均持仓',dur(a.avg_hold_s),'']];
 document.getElementById('stats').innerHTML=S.map(function(r){
  return '<div class=stat><div class=k>'+r[0]+'</div><div class="v '+r[2]+'">'+r[1]+'</div></div>';}).join('');
 drawChart(a.series);
 var rk=a.reasons||{},rc=[];var rmap={take_profit:'止盈',round_stop_loss:'止损',basis_force_exit:'基差强平',
  funding_spread_reversal:'资金费反转',max_hold_elapsed:'到时平仓',watchdog_naked:'单腿保护',recovered_exit:'恢复平仓'};
 for(var k in rk){rc.push('<span class=chip>'+(rmap[k]||k)+' <b>'+rk[k]+'</b></span>');}
 document.getElementById('reasons').innerHTML=rc.join('')||'<span class=empty>暂无退出记录</span>';
 var rows=a.last20||[];var dmap={short_var_long_lighter:'空V多L',short_lighter_long_var:'空L多V'};
 var html='<tr><th>#</th><th>方向</th><th>开仓</th><th>持仓</th><th>原因</th><th>价差</th><th>资金费</th><th>盈亏</th></tr>';
 if(!rows.length){html+='<tr><td colspan=8 class=empty>暂无已平仓回合</td></tr>';}
 rows.forEach(function(r){html+='<tr><td>'+(r.round_id==null?'-':r.round_id)+'</td>'+
  '<td>'+(dmap[r.direction]||r.direction||'-')+'</td><td>'+tm(r.opened_at)+'</td><td>'+dur(r.hold_s)+'</td>'+
  '<td>'+(rmap[_bucket(r.reason)]||_bucket(r.reason))+'</td>'+
  '<td class="'+sgn(r.price_pnl)+'">'+fx(r.price_pnl)+'</td>'+
  '<td class="'+sgn(r.funding_pnl)+'">'+fx(r.funding_pnl)+'</td>'+
  '<td class="'+sgn(r.pnl)+'">'+fx(r.pnl)+'</td></tr>';});
 document.getElementById('tbl').innerHTML=html;
}
function _bucket(r){if(!r)return 'unknown';return String(r).split(/[ :]/)[0]||'unknown';}

async function load(){
 try{
  var r=await fetch('state.json?_='+Date.now());var d=await r.json();
  var s=d.snapshot||{},st=d.state||{},th=d.thresholds||{};
  POLL=d.poll_interval||60;
  // 心跳过期横幅
  var sb=document.getElementById('stale');var lu=st.last_update;
  var age=lu?(d.now-lu):null;var limit=Math.max(120,POLL*2);
  if(age!=null&&age>limit){sb.style.display='block';
   sb.textContent='⚠️ 数据已过期 '+dur(age)+'（引擎可能已停止），以下为最后一帧';}
  else{sb.style.display='none';}
  renderStatus(s,st);renderRisk(s,st,th);
  document.getElementById('foot').textContent='更新于 '+tm(lu)+' · 每 '+POLL+' 秒刷新';
 }catch(e){}
 try{var r2=await fetch('api/rounds?_='+Date.now());renderRounds(await r2.json());}catch(e){}
}
load();setInterval(load,15000);
</script></body></html>"""


def make_handler(state_file: str, get_snapshot, cfg: dict[str, Any]):
    thresholds = _thresholds(cfg or {})
    poll_interval = int((cfg or {}).get("poll_interval_seconds", 60) or 60)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _send(self, body: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/state.json"):
                payload = {
                    "state": _read_json(state_file),
                    "snapshot": get_snapshot(),
                    "thresholds": thresholds,
                    "poll_interval": poll_interval,
                    "now": int(time.time()),
                }
                self._send(json.dumps(payload, default=str).encode(), "application/json")
                return
            if self.path.startswith("/api/rounds"):
                self._send(json.dumps(aggregate_rounds(state_file), default=str).encode(),
                           "application/json")
                return
            self._send(HTML.encode(), "text/html; charset=utf-8")
    return Handler


def serve(state_file: str, get_snapshot, host: str = "127.0.0.1", port: int = 8012,
          cfg: dict[str, Any] | None = None) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(state_file, get_snapshot, cfg or {}))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    print(f"[monitor] read-only dashboard on http://{host}:{port}/", flush=True)
    return httpd
