"""
Airtasker AI Coordinator — GitHub Actions version
===================================================
No date filtering. Scrapes all jobs, scores with Claude,
filters by category/budget/location only.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
EMAIL         = os.environ["AT_EMAIL"]
PASSWORD      = os.environ["AT_PASSWORD"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]

MAX_RADIUS_KM = 50
BASE_SUBURB   = "Rose Bay, Sydney"
SCROLL_PASSES = 50
OUTPUT_PATH   = Path("docs/dashboard.html")


# ─────────────────────────────────────────────
# STEP 1 — login + scrape
# ─────────────────────────────────────────────
async def scrape_jobs() -> list[dict]:
    print("[1/3] Launching headless browser and logging in...")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-AU",
        )

        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] });
        """)

        page = await ctx.new_page()

        try:
            await page.goto("https://www.airtasker.com/login/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)
            await page.fill('input[type="email"], input[name="email"]', EMAIL)
            await page.wait_for_timeout(800)
            await page.fill('input[type="password"], input[name="password"]', PASSWORD)
            await page.wait_for_timeout(600)
            await page.click('button[type="submit"], button:has-text("Log in"), button:has-text("Sign in")')
            await page.wait_for_timeout(4000)
            print("[1/3] Login submitted.")
        except Exception as e:
            print(f"  ⚠ Login step error: {e} — continuing anyway")

        await page.goto(
            "https://www.airtasker.com/tasks/?sort=new",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        await page.wait_for_timeout(6000)

        for i in range(SCROLL_PASSES):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(4000)
            print(f"       Scroll pass {i+1}/{SCROLL_PASSES}")

        raw_jobs = await page.evaluate("""
            () => {
                const selectors = [
                    '[data-testid="task-item"]',
                    '[class*="TaskItem"]',
                    '[class*="task-item"]',
                    'article[class*="task"]',
                    'a[href*="/tasks/"]'
                ];
                let cards = [];
                for (const sel of selectors) {
                    const found = document.querySelectorAll(sel);
                    if (found.length > cards.length) cards = Array.from(found);
                }
                return cards.map(c => ({
                    text: (c.innerText || '').trim(),
                    url: (c.querySelector('a[href*="/tasks/"]')?.href)
                         || (c.tagName === 'A' ? c.href : '')
                })).filter(j => j.text.length > 10);
            }
        """)

        await browser.close()

    print(f"[1/3] Scraped {len(raw_jobs)} jobs.")
    return raw_jobs


# ─────────────────────────────────────────────
# STEP 2 — score with Claude
# ─────────────────────────────────────────────
async def score_batch(jobs: list[dict]) -> list[dict]:
    jobs_text = "\n\n---\n\n".join(
        f"JOB {i+1}:\n{j['text'][:600]}" for i, j in enumerate(jobs)
    )

    prompt = f"""You are an AI coordinator for a small Airtasker operation based in {BASE_SUBURB}, Sydney, Australia.
Max radius: {MAX_RADIUS_KM}km from Rose Bay.
Two teams of 2 people available simultaneously (Team A and Team B).

We accept three categories of work. Be GENEROUS with matching — if a job could reasonably fit, include it. Only skip jobs that are clearly cleaning, or have a budget explicitly stated that is too low.

REMOVALS — accept any job involving moving, transporting, lifting or disposing of items:
- Moving furniture, boxes, appliances, mattresses, beds, fridges, washing machines, sofas
- Full house/unit/office moves or single item moves
- Rubbish removal, junk removal, tip runs, dump runs, skip bin loading, hard rubbish
- Helping someone move in or move out
- Transporting marketplace or store purchases (Facebook, Gumtree, IKEA, etc.)
- Loading/unloading trucks, vans, trailers
- Disposing of old furniture, e-waste, garden waste, building materials
- Budget: skip only if explicitly stated under $100. Open/unstated budget = include it.

ASSEMBLY — accept any job involving building, putting together or mounting things:
- All IKEA furniture: PAX, KALLAX, BILLY, HEMNES, MALM, BESTA, EKET, STUVA, FRIHETEN, LACK, ALEX, etc.
- Any flatpack or self-assembly furniture from Kmart, Big W, Bunnings, Temple & Webster, Fantastic Furniture, JYSK, Freedom, Harvey Norman, Nick Scali
- Beds, bed frames, wardrobes, shelving, bookcases, TV units, dining tables, desks, chairs, drawers, cabinets
- Trampolines, cubby houses, play equipment, outdoor furniture, BBQs, fire pits
- Gym equipment, treadmills, exercise bikes, weight benches
- TV wall mounting, shelf installation, picture hanging, curtain rod fitting, blind installation
- Flat-pack kitchens, laundry units, bathroom vanities, storage systems
- Budget: skip only if explicitly stated under $50. Open/unstated budget = include it.

GARDENING — accept any outdoor yard or garden work:
- Lawn mowing, grass cutting, ride-on mowing, whipper snipping, line trimming, edging
- Garden cleanup, yard cleanup, clearing overgrown areas, removing weeds, leaf blowing, raking
- Hedge trimming, bush trimming, pruning trees or shrubs, cutting back plants
- Mulching, garden bed prep, laying turf, soil spreading
- Pressure washing driveways, paths, decks, fences
- General outdoor tidying, maintenance and odd jobs
- Budget: skip only if explicitly stated over $200. Open/unstated budget = include it.

LOCATION:
- Accept jobs in Eastern Suburbs, Inner West, Inner East, Lower North Shore, Northern Beaches, City, South Sydney, Sutherland Shire
- Skip only if clearly in outer west (Penrith, Blacktown, Mt Druitt), Central Coast, Wollongong, or Blue Mountains
- If no location stated, include the job — do not skip for missing location

CLEANING — always Skip. End of lease, bond clean, house clean, office clean, carpet clean, window clean, oven clean, bathroom scrub. No exceptions.

EVERYTHING ELSE that cannot fit removals, assembly or gardening — Skip.

Return ONLY a valid JSON array. No markdown, no explanation, nothing else before or after the array.
One object per job:
{{
  "index": 1,
  "title": "concise job title max 8 words",
  "budget": "$XX or open",
  "location": "suburb name or not specified",
  "category": "assembly|removals|gardening|skip",
  "score": 0-100,
  "scoreLevel": "high|med|low",
  "assignTo": "Team A|Team B|Either|Skip",
  "reason": "one sentence explaining the score",
  "bidMessage": "50-70 word personalised bid, natural tone, mention their specific task details, say we are a professional team of 2 who are available and ready to help, do not start with Hey, no exclamation marks, empty string if assignTo is Skip"
}}

Scoring:
- Removals open budget = 55 | $100-199 = 55-65 | $200+ = 70-90
- Assembly IKEA/named brand open budget = 55 | $50-99 = 60-75 | $100+ = 75-90
- Assembly generic open budget = 45 | $50+ = 45-60
- Gardening open budget = 45 | up to $200 = 45-75
- Within 10km of Rose Bay = +15 | 10-30km = +5 | 30-50km = 0
- Skip only if: cleaning job, budget explicitly too low, or location clearly beyond {MAX_RADIUS_KM}km

Jobs:
{jobs_text}"""

    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4000,
                "messages": [{"role": "user", "content": prompt}],
            },
        )

    if resp.status_code != 200:
        print(f"  ✗ Claude API error {resp.status_code}: {resp.text[:200]}")
        return []

    text = "".join(b.get("text", "") for b in resp.json()["content"])
    text = text.replace("```json", "").replace("```", "").strip()
    s, e = text.find("["), text.rfind("]") + 1
    if s == -1:
        print("  ✗ No JSON found in Claude response")
        return []

    scored = json.loads(text[s:e])
    for item in scored:
        idx = item.get("index", 1) - 1
        if 0 <= idx < len(jobs):
            item["url"] = jobs[idx].get("url", "")
    return scored


async def score_all(jobs: list[dict]) -> list[dict]:
    print(f"[2/3] Scoring {len(jobs)} jobs with Claude...")
    all_scored = []
    batch_size = 15
    for i in range(0, len(jobs), batch_size):
        batch = jobs[i:i + batch_size]
        print(f"       Batch {i // batch_size + 1} ({len(batch)} jobs)...")
        scored = await score_batch(batch)
        all_scored.extend(scored)

    for j in all_scored:
        print(f"  → {j.get('category','?')} | {j.get('assignTo','?')} | {j.get('budget','?')} | {j.get('title','?')[:40]}")
    all_scored = [j for j in all_scored if j.get("assignTo") != "Skip"]
    all_scored.sort(key=lambda x: x.get("score", 0), reverse=True)
    print(f"[2/3] {len(all_scored)} viable jobs after filtering.")
    return all_scored


# ─────────────────────────────────────────────
# STEP 3 — generate dashboard
# ─────────────────────────────────────────────
def generate_dashboard(jobs: list[dict]) -> None:
    print("[3/3] Writing dashboard...")
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.now().strftime("%d %b %Y, %I:%M %p")
    jobs_json = json.dumps(jobs, indent=2)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Jobs — {generated_at}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Sora:wght@400;500;600&display=swap');
  :root{{--bg:#0e0f11;--surface:#16181c;--surface2:#1e2026;--border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.13);--text:#e8eaf0;--muted:#6b7280;--accent:#22d3a0;--amber:#f59e0b;--blue:#60a5fa;--purple:#a78bfa;--font:'Sora',sans-serif;--mono:'DM Mono',monospace}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:var(--text);font-family:var(--font);min-height:100vh}}
  .header{{padding:18px 24px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;background:var(--bg);z-index:10}}
  .logo{{display:flex;align-items:center;gap:10px;font-size:15px;font-weight:600;letter-spacing:-.02em}}
  .dot{{width:7px;height:7px;background:var(--accent);border-radius:50%;box-shadow:0 0 8px var(--accent);animation:pulse 2s ease-in-out infinite}}
  @keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:.5}}}}
  .meta{{font-size:11px;color:var(--muted);font-family:var(--mono)}}
  .layout{{display:grid;grid-template-columns:340px 1fr;min-height:calc(100vh - 58px)}}
  .sidebar{{border-right:1px solid var(--border);overflow-y:auto}}
  .main{{padding:20px 24px;overflow-y:auto}}
  .filter-row{{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;gap:6px;flex-wrap:wrap}}
  .f-btn{{background:transparent;border:1px solid var(--border);border-radius:7px;color:var(--muted);font-family:var(--font);font-size:11px;padding:5px 11px;cursor:pointer;transition:all .15s}}
  .f-btn:hover,.f-btn.active{{border-color:var(--border2);color:var(--text);background:var(--surface)}}
  .job-card{{padding:14px 16px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .12s;border-left:3px solid transparent}}
  .job-card:hover{{background:var(--surface)}}
  .job-card.active{{background:var(--surface2);border-left-color:var(--blue)!important}}
  .job-card.sh{{border-left-color:var(--accent)}}
  .job-card.sm{{border-left-color:var(--amber)}}
  .job-card.sl{{border-left-color:var(--muted)}}
  .jt{{display:flex;justify-content:space-between;align-items:flex-start;gap:8px;margin-bottom:5px}}
  .jname{{font-size:13px;font-weight:500;line-height:1.4}}
  .jprice{{font-family:var(--mono);font-size:13px;color:var(--accent);white-space:nowrap}}
  .tags{{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:5px}}
  .tag{{font-size:10px;padding:2px 6px;border-radius:5px;font-weight:500;font-family:var(--mono)}}
  .tc{{background:rgba(96,165,250,.12);color:var(--blue)}}
  .tl{{background:rgba(167,139,250,.12);color:var(--purple)}}
  .ta{{background:rgba(34,211,160,.12);color:var(--accent)}}
  .tb{{background:rgba(245,158,11,.12);color:var(--amber)}}
  .jreason{{font-size:11px;color:var(--muted);margin-bottom:7px;line-height:1.5}}
  .brow{{display:flex;align-items:center;gap:8px}}
  .btrack{{flex:1;height:3px;background:var(--border);border-radius:3px;overflow:hidden}}
  .bfill{{height:100%;border-radius:3px}}
  .bnum{{font-family:var(--mono);font-size:11px;color:var(--muted);min-width:32px;text-align:right}}
  .panel{{background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:20px;margin-bottom:14px}}
  .ptitle{{font-size:18px;font-weight:600;letter-spacing:-.02em;margin-bottom:4px}}
  .psub{{font-size:13px;color:var(--muted);margin-bottom:16px}}
  .plabel{{font-size:10px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin-bottom:8px;display:flex;justify-content:space-between;align-items:center}}
  .bidbox{{background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:14px;font-size:13px;line-height:1.75;white-space:pre-wrap;word-wrap:break-word;margin-bottom:12px}}
  .cbtn{{background:var(--surface2);border:1px solid var(--border);border-radius:7px;color:var(--text);font-family:var(--font);font-size:11px;padding:5px 12px;cursor:pointer;transition:all .15s;font-weight:500}}
  .cbtn:hover{{border-color:var(--accent);color:var(--accent)}}
  .cbtn.ok{{color:var(--accent);border-color:var(--accent)}}
  .obtn{{display:inline-flex;align-items:center;gap:5px;background:transparent;border:1px solid var(--border);border-radius:7px;color:var(--muted);font-family:var(--font);font-size:11px;padding:5px 12px;cursor:pointer;text-decoration:none;transition:all .15s}}
  .obtn:hover{{border-color:var(--border2);color:var(--text)}}
  .empty{{display:flex;flex-direction:column;align-items:center;justify-content:center;height:350px;color:var(--muted);font-size:13px;gap:8px;text-align:center}}
  @media(max-width:680px){{.layout{{grid-template-columns:1fr}}.sidebar{{border-right:none;border-bottom:1px solid var(--border);max-height:45vh}}}}
</style>
</head>
<body>
<div class="header">
  <div class="logo"><div class="dot"></div>Coordinator Dashboard</div>
  <div class="meta">Updated {generated_at} &nbsp;·&nbsp; Rose Bay 50km &nbsp;·&nbsp; <span id="vc"></span></div>
</div>
<div class="layout">
  <div class="sidebar">
    <div class="filter-row">
      <button class="f-btn active" onclick="filt('all',this)">All</button>
      <button class="f-btn" onclick="filt('A',this)">Team A</button>
      <button class="f-btn" onclick="filt('B',this)">Team B</button>
      <button class="f-btn" onclick="filt('high',this)">High score</button>
      <button class="f-btn" onclick="filt('assembly',this)">Assembly</button>
      <button class="f-btn" onclick="filt('removals',this)">Removals</button>
      <button class="f-btn" onclick="filt('gardening',this)">Gardening</button>
    </div>
    <div id="jlist"></div>
  </div>
  <div class="main">
    <div id="detail">
      <div class="empty"><div style="font-size:28px;opacity:.3">←</div><div>Select a job to view the bid</div></div>
    </div>
  </div>
</div>
<script>
const JOBS={jobs_json};
function sc(s){{return s>=65?'var(--accent)':s>=35?'var(--amber)':'var(--muted)'}}
function sl(s){{return s>=65?'sh':s>=35?'sm':'sl'}}
function tc(t){{return t==='Team A'?'ta':t==='Team B'?'tb':'te'}}
function renderList(jobs){{
  const el=document.getElementById('jlist');
  el.innerHTML='';
  document.getElementById('vc').textContent=jobs.length+' job'+(jobs.length!==1?'s':'');
  jobs.forEach((j)=>{{
    const d=document.createElement('div');
    d.className='job-card '+sl(j.score);
    d.onclick=()=>sel(j,d);
    d.innerHTML=`
      <div class="jt"><div class="jname">${{j.title}}</div><div class="jprice">${{j.budget}}</div></div>
      <div class="tags">
        <span class="tag tc">${{j.category}}</span>
        ${{j.location&&j.location!=='not specified'?'<span class="tag tl">'+j.location+'</span>':''}}
        <span class="tag ${{tc(j.assignTo)}}">${{j.assignTo}}</span>
      </div>
      <div class="jreason">${{j.reason}}</div>
      <div class="brow">
        <div class="btrack"><div class="bfill" style="width:${{j.score}}%;background:${{sc(j.score)}}"></div></div>
        <div class="bnum">${{j.score}}/100</div>
      </div>`;
    el.appendChild(d);
  }});
  if(jobs.length>0)sel(jobs[0],el.firstChild);
}}
function sel(j,el){{
  document.querySelectorAll('.job-card').forEach(c=>c.classList.remove('active'));
  el.classList.add('active');
  const link=j.url?`<a class="obtn" href="${{j.url}}" target="_blank">Open on Airtasker ↗</a>`:'';
  document.getElementById('detail').innerHTML=`
    <div class="panel">
      <div class="ptitle">${{j.title}}</div>
      <div class="psub">${{j.location||'Location not stated'}} · ${{j.budget}} · ${{j.assignTo}}</div>
      <div class="plabel">
        <span>Bid message — copy and paste into Airtasker</span>
        <button class="cbtn" id="cb" onclick="copy()">Copy</button>
      </div>
      <div class="bidbox" id="bt">${{j.bidMessage}}</div>
      <div style="display:flex;gap:8px;align-items:center">
        ${{link}}<span style="font-size:11px;color:var(--muted)">Score ${{j.score}}/100</span>
      </div>
    </div>`;
}}
function copy(){{
  navigator.clipboard.writeText(document.getElementById('bt').textContent).then(()=>{{
    const b=document.getElementById('cb');
    b.textContent='Copied ✓';b.classList.add('ok');
    setTimeout(()=>{{b.textContent='Copy';b.classList.remove('ok')}},2000);
  }});
}}
function filt(type,btn){{
  document.querySelectorAll('.f-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  let f=JOBS;
  if(type==='A') f=JOBS.filter(j=>j.assignTo==='Team A'||j.assignTo==='Either');
  else if(type==='B') f=JOBS.filter(j=>j.assignTo==='Team B'||j.assignTo==='Either');
  else if(type==='high') f=JOBS.filter(j=>j.score>=65);
  else if(type==='assembly') f=JOBS.filter(j=>j.category==='assembly');
  else if(type==='removals') f=JOBS.filter(j=>j.category==='removals');
  else if(type==='gardening') f=JOBS.filter(j=>j.category==='gardening');
  renderList(f);
}}
renderList(JOBS);
</script>
</body>
</html>"""

    OUTPUT_PATH.write_text(html, encoding="utf-8")
    index = OUTPUT_PATH.parent / "index.html"
    index.write_text(
        '<!DOCTYPE html><html><head><meta http-equiv="refresh" content="0;url=dashboard.html"></head></html>',
        encoding="utf-8",
    )
    print(f"[3/3] Dashboard written → {OUTPUT_PATH}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    print("=" * 52)
    print("  Airtasker Coordinator AI — No Date Filter")
    print(f"  Base: {BASE_SUBURB} | Radius: {MAX_RADIUS_KM}km")
    print("=" * 52)

    raw = await scrape_jobs()
    if not raw:
        print("✗ No jobs scraped. Possible IP block or login failure.")
        sys.exit(1)

    scored = await score_all(raw)
    generate_dashboard(scored)
    print(f"\n✓ Done — {len(scored)} jobs in dashboard.\n")


if __name__ == "__main__":
    asyncio.run(main())
