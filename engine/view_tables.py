"""
Dead-simple DB viewer: dump every table in the DB into a single
self-contained HTML with one tab per table. Just to eyeball the data.

Writes the site's ops view (`docs/tables.html`, footer-linked only — it is NOT
the product). `--out` sends it anywhere else, which is what you want while the
site is being built.

  .venv/bin/python view_tables.py                    # writes ../docs/tables.html
  .venv/bin/python view_tables.py --out /tmp/t.html  # anywhere else
  .venv/bin/python view_tables.py --open             # ...and open it
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from pathlib import Path

from models import DB_PATH as DB, SITE_NAME
# The site is served from the repo's docs/ (GitHub Pages, main/docs).
OUT = Path(__file__).parent.parent / "docs" / "tables.html"
TABLES = ["leads_inbox", "pipeline_sources", "source_coverage", "venues",
          "venue_aliases", "groups", "events", "event_scores"]
MAXLEN = 300  # truncate long cells to keep the file light


def clip(v):
    if isinstance(v, str) and len(v) > MAXLEN:
        return v[:MAXLEN] + "…"
    return v


def main(out: Path = OUT, open_after: bool = False):
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    data = {}
    for t in TABLES:
        rows = db.execute(f"select * from {t}").fetchall()
        cols = [d[0] for d in db.execute(f"select * from {t} limit 1").description]
        data[t] = {"columns": cols, "rows": [[clip(r[c]) for c in cols] for r in rows]}

    # Derived (NOT stored): full source→venue/group coverage implied by events.
    # This is the complete picture; source_coverage only stores curated/judged links.
    der = []
    for kind, col in (("venue", "venue_id"), ("group", "group_id")):
        for r in db.execute(f"""
            select e.source_id, '{kind}' tk, e.{col} tid, s.is_aggregator agg, count(*) n
            from events e join pipeline_sources s on s.id=e.source_id
            where e.{col} is not null
            group by e.source_id, e.{col} order by n desc"""):
            der.append([r["source_id"], r["tk"], r["tid"], r["agg"], r["n"]])
    db.close()

    # place the derived view right after the stored coverage table
    ordered = {}
    for k, v in data.items():
        ordered[k] = v
        if k == "source_coverage":
            ordered["coverage ⟵ derived"] = {
                "columns": ["source_id", "target_kind", "target_id", "is_aggregator", "events"],
                "rows": der}
    data = ordered

    html = (TEMPLATE
            .replace("/*DATA*/", json.dumps(data, ensure_ascii=False, default=str))
            .replace("/*SITE_NAME*/", SITE_NAME)
            .replace("/*DBNAME*/", DB.name))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f"Wrote {out}  ({out.stat().st_size // 1024} KB)")
    if open_after:
        subprocess.run(["open", str(out)])


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>/*SITE_NAME*/ · tables</title>
<style>
  :root{--bg:#0e1014;--panel:#171a21;--ink:#e7eaf0;--dim:#8893a3;--line:#262b35;--accent:#5aa9ff}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font:13px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif}
  header{padding:16px 20px 0}
  h1{font-size:16px;margin:0 0 12px;letter-spacing:-.01em}
  .tabs{display:flex;gap:4px;flex-wrap:wrap}
  .tab{padding:7px 13px;border:1px solid var(--line);border-bottom:none;border-radius:8px 8px 0 0;
       background:var(--panel);color:var(--dim);cursor:pointer;font-weight:600}
  .tab .c{color:var(--accent);font-variant-numeric:tabular-nums}
  .tab.on{color:var(--ink);background:#1f2530;border-color:var(--accent)}
  .bar{padding:10px 20px;border-top:1px solid var(--line);background:#12151b}
  .bar input{background:#0c0e12;border:1px solid var(--line);color:var(--ink);padding:6px 10px;
       border-radius:7px;width:320px;font-size:13px}
  .bar .meta{color:var(--dim);margin-left:12px}
  .wrap{padding:0 12px 60px;overflow:auto}
  table{border-collapse:collapse;width:100%;font-family:'SF Mono',ui-monospace,Menlo,monospace;font-size:11.5px}
  th,td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top;max-width:340px;
        overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  th{position:sticky;top:0;background:#1f2530;color:var(--accent);cursor:pointer;z-index:1;white-space:nowrap}
  tr:nth-child(even) td{background:#13161c}
  td.null{color:#4d5663;font-style:italic}
  td.bool-1{color:#56d18c}td.bool-0{color:#ff7aa2}
</style></head><body>
<header><h1><a href="./" style="color:var(--accent);text-decoration:none">← /*SITE_NAME*/</a> · <span id="dbname">/*DBNAME*/</span></h1><div class="tabs" id="tabs"></div></header>
<div class="bar"><input id="filter" placeholder="filter rows (substring, any column)…"><span class="meta" id="meta"></span></div>
<div class="wrap"><table id="tbl"><thead></thead><tbody></tbody></table></div>
<script>
const DATA = /*DATA*/;
const names = Object.keys(DATA);
let cur = names[0], sortCol = -1, sortDir = 1;
const tabs = document.getElementById('tabs');
names.forEach(n => {
  const el = document.createElement('div');
  el.className = 'tab' + (n===cur?' on':'');
  el.innerHTML = n + ' <span class="c">' + DATA[n].rows.length + '</span>';
  el.onclick = () => { cur = n; sortCol = -1; document.getElementById('filter').value=''; draw(); };
  tabs.appendChild(el);
});
function draw(){
  [...tabs.children].forEach((t,i)=>t.classList.toggle('on', names[i]===cur));
  const {columns, rows} = DATA[cur];
  const q = document.getElementById('filter').value.toLowerCase();
  let r = q ? rows.filter(row => row.some(c => (c==null?'':String(c)).toLowerCase().includes(q))) : rows.slice();
  if (sortCol >= 0) r.sort((a,b)=>{ const x=a[sortCol],y=b[sortCol];
    if(x==null)return 1; if(y==null)return -1;
    return (isFinite(x)&&isFinite(y) ? x-y : String(x).localeCompare(String(y)))*sortDir; });
  document.querySelector('thead').innerHTML = '<tr>' + columns.map((c,i)=>
    '<th data-i="'+i+'">'+c+(sortCol===i?(sortDir>0?' ▲':' ▼'):'')+'</th>').join('') + '</tr>';
  document.querySelector('tbody').innerHTML = r.map(row=>'<tr>'+row.map(c=>{
    if(c==null) return '<td class="null">·</td>';
    if(c===0||c===1) return '<td class="bool-'+c+'">'+c+'</td>';
    const s=String(c); return '<td title="'+s.replace(/"/g,'&quot;')+'">'+s.replace(/</g,'&lt;')+'</td>';
  }).join('')+'</tr>').join('');
  document.getElementById('meta').textContent = r.length + ' / ' + rows.length + ' rows · ' + columns.length + ' cols';
  document.querySelectorAll('th').forEach(th=>th.onclick=()=>{
    const i=+th.dataset.i; sortDir = (sortCol===i? -sortDir : 1); sortCol=i; draw(); });
}
document.getElementById('filter').oninput = draw;
draw();
</script></body></html>"""


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=OUT,
                    help=f"where to write the HTML (default: {OUT})")
    ap.add_argument("--open", action="store_true", help="open it after writing")
    args = ap.parse_args()
    main(out=args.out, open_after=args.open)
