"""
Airtasker AI Coordinator — GitHub Actions version
===================================================
Runs headless in the cloud, scrapes Airtasker, filters Mar 18–25,
scores jobs with Claude, and writes dashboard.html to /docs
so GitHub Pages serves it as a live URL.

Credentials come from GitHub Secrets (never stored in code).
"""

import asyncio
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

# ─────────────────────────────────────────────
# CONFIG — all sensitive values come from GitHub Secrets
# ─────────────────────────────────────────────
EMAIL          = os.environ["AT_EMAIL"]
PASSWORD       = os.environ["AT_PASSWORD"]
ANTHROPIC_KEY  = os.environ["ANTHROPIC_API_KEY"]

DATE_FROM      = date(2026, 3, 18)
DATE_TO        = date(2026, 3, 25)
MAX_RADIUS_KM  = 50
BASE_SUBURB    = "Rose Bay, Sydney"
SERVICES       = ["IKEA/flatpack assembly (min $50)", "removals up to king bed (min $100)", "gardening with lawnmower or whipper snipper ($100-$400)"]
EXCLUDED       = ["cleaning", "end of lease clean", "house clean", "office clean", "carpet clean", "window clean"]
SCROLL_PASSES  = 8
OUTPUT_PATH    = Path("docs/dashboard.html")


# ─────────────────────────────────────────────
# STEP 1 — login + scrape
# ─────────────────────────────────────────────
async def scrape_jobs() -> list[dict]:
    print("[1/4] Launching headless browser and logging in...")

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

        # Mask automation fingerprint
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] });
        """)

        page = await ctx.new_page()

        # Login
        try:
            await page.goto("https://www.airtasker.com/login/", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)
            await page.fill('input[type="email"], input[name="email"]', EMAIL)
            await page.wait_for_timeout(800)
            await page.fill('input[type="password"], input[name="password"]', PASSWORD)
            await page.wait_for_timeout(600)
            await page.click('button[type="submit"], button:has-text("Log in"), button:has-text("Sign in")')
            await page.wait_for_timeout(4000)
            print("[1/4] Login submitted.")
        except Exception as e:
            print(f"  ⚠ Login step error: {e} — continuing anyway")

        # Browse tasks
        await page.goto(
            "https://www.airtasker.com/tasks/?sort=new",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_timeout(3000)

        # Scroll to load more
        for i in range(SCROLL_PASSES):
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            print(f"       Scroll pass {i+1}/{SCROLL_PASSES}")

        # Extract job cards
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

    print(f"[1/4] Scraped {len(raw_jobs)} raw entries.")
    return raw_jobs


# ─────────────────────────────────────────────
# STEP 2 — date filter
# ─────────────────────────────────────────────
def is_in_date_window(text: str) -> bool:
    months = {
        "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
        "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
        "january":1,"february":2,"march":3,"april":4,
        "june":6,"july":7,"august":8,"september":9,
        "october":10,"november":11,"december":12,
    }
    t = text.lower()
    for month_str, month_num in months.items():
        for pat in [rf'(\d{{1,2}})(?:st|nd|rd|th)?\s+{month_str}', rf'{month_str}\s+(\d{{1,2}})(?:st|nd|rd|th)?']:
            for m in re.finditer(pat, t):
                try:
                    d = date(DATE_FROM.year, month_num, int(m.group(1)))
                    return DATE_FROM <= d <= DATE_TO
                except ValueError:
                    pass
    for m in re.finditer(r'(\d{1,2})[/\-](\d{1,2})', t):
        try:
            d = date(DATE_FROM.year, int(m.group(2)), int(m.group(1)))
            return DATE_FROM <= d <= DATE_TO
        except ValueError:
            pass
    return False  # no date mentioned — exclude it


# ─────────────────────────────────────────────
# STEP 3 — score with Claude
# ─────────────────────────────────────────────
async def score_batch(jobs: list[dict]) -> list[dict]:
    jobs_text = "\n\n---\n\n".join(f"JOB {i+1}:\n{j['text'][:600]}" for i, j in enumerate(jobs))

    prompt = f"""You are an AI coordinator for a small Airtasker operation based in {BASE_SUBURB}, Sydney, Australia.
Working dates: {DATE_FROM.strftime('%d %B')} to {DATE_TO.strftime('%d %B %Y')} ONLY.
Max radius: {MAX_RADIUS_KM}km from Rose Bay, Sydney.
Two teams of 2 available simultaneously (Team A and Team B).

We accept three categories of jobs. Be GENEROUS with matching — if a job could reasonably fit a category, include it. Only skip jobs that clearly do not fit any category, are cleaning, or have a budget explicitly stated and too low.

REMOVALS — accept any job involving moving, transporting, lifting or disposing of physical items. This includes:
- Moving furniture, boxes, appliances, mattresses, beds, couches, fridges, washing machines
- Single item moves or full house/unit/office moves
- Rubbish removal, junk removal, tip runs, taking things to the dump, skip bin loading
- Helping someone move out or move in
- Transporting items bought from Facebook Marketplace, Gumtree, or stores
- Loading or unloading a truck, van or trailer
- Disposing of old furniture, e-waste, garden waste, building materials
- Budget must be $100 or above — skip if explicitly under $100. If budget is open/not stated, include it.

ASSEMBLY — accept any job involving building, putting together, installing or mounting things. This includes:
- IKEA and all flatpack furniture (PAX, KALLAX, BILLY, HEMNES, MALM, BESTA, EKET, STUVA, FRIHETEN etc.)
- Any flatpack or self-assembly furniture from Kmart, Big W, Bunnings, Temple & Webster, Fantastic Furniture, JYSK, Freedom
- Beds, bed frames, wardrobes, shelving, bookcases, TV units, dining tables, desks, office chairs, drawers
- Trampolines, cubby houses, outdoor furniture, BBQs, gym equipment, bike assembly
- TV wall mounting, shelf installation, picture hanging, curtain rod installation
- Flat-pack kitchens, laundry units, storage systems
- Budget must be $50 or above — skip if explicitly under $50. If budget is open/not stated, include it.

GARDENING — accept any outdoor garden or yard job. This includes:
- Lawn mowing, grass cutting, ride-on mowing, whipper snipping, line trimming, edging
- Garden cleanup, yard cleanup, clearing overgrown areas, removing weeds, leaf blowing
- Hedge trimming, bush trimming, pruning, cutting back trees or shrubs
- Mulching, garden bed preparation, laying turf
- Pressure washing driveways, paths, decks
- General outdoor tidying and maintenance
- Budget must be $200 or below — skip if explicitly over $200. If budget is open/not stated, include it.

LOCATION:
- Accept jobs within {MAX_RADIUS_KM}km of Rose Bay — Eastern Suburbs, Inner West, Inner East, Lower North Shore, Northern Beaches, City, South Sydney, Sutherland Shire all fine
- Skip only if the job is clearly in outer west (Penrith, Blacktown, Parramatta area), Central Coast, Wollongong, or Blue Mountains
- If no location is stated, include the job — do not skip for missing location

CLEANING — always Skip. This means end of lease cleans, bond cleans, house cleaning, office cleaning, carpet cleaning, window cleaning, oven cleaning, bathroom scrubbing. Do NOT accept cleaning jobs under any circumstances.

EVERYTHING ELSE that cannot reasonably fit removals, assembly or gardening — Skip.

Return ONLY a valid JSON array — no markdown, no explanation.
One object per job:
{{
  "index": 1,
  "title": "concise title max 8 words",
  "budget": "$XX or open",
  "location": "suburb or not specified",
  "category": "assembly|removals|gardening|skip",
  "dateNote": "exact date mentioned in listing or none",
  "inDateWindow": true,
  "score": 0-100,
  "scoreLevel": "high|med|low",
  "assignTo": "Team A|Team B|Either|Skip",
  "reason": "one sentence explaining score",
  "bidMessage": "50-70 word personalised bid, natural tone, reference their specific job details, say we are a professional team of 2, mention we are available and keen to help, do not use Hey, no exclamation marks — leave empty string if assignTo is Skip"
}}

Scoring guide:
- Removals $100-199 = score 50-65 | $200+ = score 70-90 | open budget = score 50
- Assembly IKEA/flatpack named brand $50-99 = score 60-75 | $100+ = score 75-90 | open budget = score 55
- Assembly generic flatpack $50+ = score 45-60 | open budget = score 45
- Gardening any outdoor work up to $200 = score 45-75 | open budget = score 45
- Location within 10km of Rose Bay = +15 points
- Location 10-30km = +5 points | 30-50km = 0 bonus
- assignTo Skip ONLY if: it is a cleaning job, budget is explicitly stated and too low, or location is clearly beyond {MAX_RADIUS_KM}km

Jobs to analyse:
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

    text = "".join(b.get("text","") for b in resp.json()["content"])
    text = text.replace("```json","").replace("```","").strip()
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
    print(f"[3/4] Scoring {len(jobs)} jobs with Claude AI...")
    all_scored = []
    batch_size = 15
    for i in range(0, len(jobs), batch_size):
        batch = jobs[i:i+batch_size]
        print(f"       Batch {i//batch_size+1} ({len(batch)} jobs)...")
        scored = await score_batch(batch)
        all_scored.extend(scored)

    all_scored = [j for j in all_scored if j.get("assignTo") != "Skip"
    all_scored.sort(key=lambda x: x.get("score", 0), reverse=True)
    print(f"[3/4] {len(all_scored)} viable jobs after filtering.")
    return all_scored


# ─────────────────────────────────────────────
# STEP 4 — generate dashboard HTML
# ─────────────────────────────────────────────
def generate_dashboard(jobs: list[dict]) -> None:
    print("[4/4] Writing dashboard.html...")
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
  :root{{--bg:#0e0f11;--surface:#16181c;--surface2:#1e2026;--border:rgba(255,255,255,0.07);--border2:rgba(255,255,255,0.13);--text:#e8eaf0;--muted:#6b7280;--accent:#22d3a0;--amber:#f59e0b;--red:#f87171;--blue:#60a5fa;--purple:#a78bfa;--font:'Sora',sans-serif;--mono:'DM Mono',monospace}}
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
  .te{{background:rgba(255,255,255,.06);color:var(--muted)}}
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
  <div class="meta">Updated {generated_at} &nbsp;·&nbsp; {DATE_FROM.strftime('%d %b')}–{DATE_TO.strftime('%d %b')} &nbsp;·&nbsp; <span id="vc"></span></div>
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
  <div class="main"><div id="detail"><div class="empty"><div style="font-size:28px;opacity:.3">←</div><div>Select a job to view the bid</div></div></div></div>
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
  jobs.forEach((j,i)=>{{
    const d=document.createElement('div');
    d.className='job-card '+sl(j.score);
    d.onclick=()=>sel(j,d);
    d.innerHTML=`<div class="jt"><div class="jname">${{j.title}}</div><div class="jprice">${{j.budget}}</div></div>
    <div class="tags"><span class="tag tc">${{j.category}}</span>${{j.location!=='not specified'?'<span class="tag tl">'+j.location+'</span>':''}}<span class="tag ${{tc(j.assignTo)}}">${{j.assignTo}}</span>${{j.dateNote&&j.dateNote!=='no date specified'?'<span class="tag te">'+j.dateNote+'</span>':''}}</div>
    <div class="jreason">${{j.reason}}</div>
    <div class="brow"><div class="btrack"><div class="bfill" style="width:${{j.score}}%;background:${{sc(j.score)}}"></div></div><div class="bnum">${{j.score}}/100</div></div>`;
    el.appendChild(d);
  }});
  if(jobs.length>0)sel(jobs[0],el.firstChild);
}}
function sel(j,el){{
  document.querySelectorAll('.job-card').forEach(c=>c.classList.remove('active'));
  el.classList.add('active');
  const link=j.url?`<a class="obtn" href="${{j.url}}" target="_blank">Open on Airtasker ↗</a>`:'';
  document.getElementById('detail').innerHTML=`<div class="panel">
    <div class="ptitle">${{j.title}}</div>
    <div class="psub">${{j.location}} · ${{j.budget}} · ${{j.dateNote}} · ${{j.assignTo}}</div>
    <div class="plabel"><span>Bid message — copy and paste into Airtasker</span><button class="cbtn" id="cb" onclick="copy()">Copy</button></div>
    <div class="bidbox" id="bt">${{j.bidMessage}}</div>
    <div style="display:flex;gap:8px;align-items:center">${{link}}<span style="font-size:11px;color:var(--muted)">Score ${{j.score}}/100</span></div>
  </div>`;
}}
function copy(){{
  navigator.clipboard.writeText(document.getElementById('bt').textContent).then(()=>{{
    const b=document.getElementById('cb');b.textContent='Copied ✓';b.classList.add('ok');
    setTimeout(()=>{{b.textContent='Copy';b.classList.remove('ok')}},2000);
  }});
}}
function filt(type,btn){{
  document.querySelectorAll('.f-btn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');
  let f=JOBS;
  if(type==='A')f=JOBS.filter(j=>j.assignTo==='Team A'||j.assignTo==='Either');
  else if(type==='B')f=JOBS.filter(j=>j.assignTo==='Team B'||j.assignTo==='Either');
  else if(type==='high')f=JOBS.filter(j=>j.score>=65);
  else if(type==='assembly')f=JOBS.filter(j=>j.category==='assembly');
  else if(type==='removals')f=JOBS.filter(j=>j.category==='removals');
  else if(type==='gardening')f=JOBS.filter(j=>j.category==='gardening');
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

    print(f"[4/4] Dashboard written to {OUTPUT_PATH}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    print("=" * 52)
    print("  Airtasker Coordinator AI — GitHub Actions")
    print(f"  Window: {DATE_FROM.strftime('%d %b')} – {DATE_TO.strftime('%d %b %Y')}")
    print("=" * 52)

    raw = await scrape_jobs()
    if not raw:
        print("✗ No jobs scraped. Possible IP block or login failure.")
        sys.exit(1)

    print(f"[2/4] Date filtering {len(raw)} jobs...")
    filtered = [j for j in raw if is_in_date_window(j["text"])]
    print(f"[2/4] {len(filtered)} jobs passed date filter.")

    if not filtered:
        print("  No jobs in date window. Writing empty dashboard.")
        generate_dashboard([])
        return

    scored = await score_all(filtered)
    generate_dashboard(scored)
    print(f"\n✓ Done — {len(scored)} jobs in dashboard.\n")


if __name__ == "__main__":
    asyncio.run(main())
