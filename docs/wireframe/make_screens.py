"""Render every screen state of the UrbanView mockup with headless Chrome.

The mockup (``wireframe-decoded.html``) is a single-file vanilla-JS app whose fonts and brand
SVGs are bundled resources that do not resolve once the file is unpacked. This script builds a
*harness* copy that

* loads JetBrains Mono / Schibsted Grotesk from Google Fonts (the same faces and weights the
  bundle ships),
* points the logo / brand-mark ``<img>`` tags at ``../brand/*.svg``,
* seeds ``Math.random`` so the generated cadastre is identical on every run,
* drives the app into one named state per ``location.hash`` (see ``STATES`` in the driver),

then screenshots each state into ``screens/<state>.png`` and dumps the computed styles of the
key selectors into ``computed-styles.json`` (the effective values after the stylesheet's
override passes, which are what the frontend must reproduce).

Usage (from the repo root, Chrome or Edge installed):

    python docs/wireframe/make_screens.py            # all states
    python docs/wireframe/make_screens.py urban ai   # a subset
    python docs/wireframe/make_screens.py --dump     # computed styles only

Requires no Python packages beyond the standard library.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "wireframe-decoded.html"
BRAND = HERE.parent / "brand"
SCREENS = HERE / "screens"
COMPUTED = HERE / "computed-styles.json"

CHROME_CANDIDATES = [
    os.environ.get("CHROME_BIN", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

FONTS_LINK = (
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2'
    "?family=JetBrains+Mono:wght@400;500;700"
    '&family=Schibsted+Grotesk:wght@400;500;600;700;800&display=swap">'
)

# (state, window WxH). The driver below maps each state to the calls that produce it.
STATES: list[tuple[str, str]] = [
    ("empty", "1440,900"),
    ("toast", "1440,900"),
    ("pin", "1440,900"),
    ("search", "1440,900"),
    ("cadastral", "1440,900"),
    ("cadastral-none", "1440,900"),
    ("urban", "1440,900"),
    ("urban-scrolled", "1440,900"),
    ("urban-paid", "1440,900"),
    ("urban-paid-scrolled", "1440,900"),
    ("zone", "1440,900"),
    ("doc", "1440,900"),
    ("order", "1440,900"),
    ("order-legal", "1440,900"),
    ("pay", "1440,900"),
    ("success", "1440,900"),
    ("upgrade", "1440,900"),
    ("upgrade-ai", "1440,900"),
    ("engine", "1440,900"),
    ("method", "1440,900"),
    ("method-last", "1440,900"),
    ("ai", "1440,900"),
    ("ai-quota", "1440,900"),
    ("rail-collapsed", "1440,900"),
    ("layers-all", "1440,900"),
    ("dep", "1440,900"),
    ("legend-min", "1440,900"),
    ("uncovered", "1440,900"),
    ("admin", "1440,900"),
    ("admin-review", "1440,900"),
    ("admin-rules", "1440,900"),
    ("admin-fin", "1440,900"),
    ("admin-engine", "1440,900"),
    ("admin-orders", "1440,900"),
    ("admin-data", "1440,900"),
    ("narrow-1100", "1000,800"),
    ("narrow-860", "820,800"),
    ("narrow-760", "740,800"),
]

DRIVER = r"""
<script>
(function(){
  const state=(location.hash||'#empty').slice(1);
  const $q=s=>document.querySelector(s);
  const withUp=PARCELS.find(p=>p.up&&p.zone==='B'&&p.planArea<=500);
  const bigUp=PARCELS.find(p=>p.up&&p.planArea>500);
  const noUp=PARCELS.find(p=>!p.up);
  const tab=id=>$q(`.admintabs button[data-av="${id}"]`).click();
  const scrollPanel=()=>{const s=$q('.pscroll'); if(s) s.scrollTop=s.scrollHeight;};
  const RUN={
    empty(){},
    toast(){ setTimeout(()=>{ toast('Click any parcel to see what can be built'); clearTimeout(toastT); },1000); },
    pin(){ STATE.freePin={x:80,y:500}; drawPinAt(80,500); $q('#coords').textContent=coordStr(80,500); renderPanelEmpty({x:80,y:500}); },
    search(){ const i=$q('#search'); i.value='Par'; i.dispatchEvent(new Event('input')); i.focus(); },
    cadastral(){ selectParcel(withUp.id,'cad'); },
    'cadastral-none'(){ selectParcel(noUp.id,'cad'); },
    urban(){ selectParcel(withUp.id,'urban'); },
    'urban-scrolled'(){ selectParcel(withUp.id,'urban'); scrollPanel(); },
    'urban-paid'(){ subscribe('market'); selectParcel(withUp.id,'urban'); },
    'urban-paid-scrolled'(){ subscribe('market'); selectParcel(withUp.id,'urban'); scrollPanel(); },
    zone(){ selectZone('B'); },
    doc(){ selectDoc('A'); },
    order(){ selectParcel(withUp.id,'urban'); openOrder(); },
    'order-legal'(){ selectParcel(bigUp.id,'urban'); STATE.orderType='legal'; openOrder(); },
    pay(){ selectParcel(withUp.id,'urban'); openPay(); },
    success(){ selectParcel(withUp.id,'urban'); orderSuccess(); },
    upgrade(){ selectParcel(withUp.id,'urban'); openUpgrade('market'); },
    'upgrade-ai'(){ selectParcel(withUp.id,'urban'); openUpgrade('ai'); },
    engine(){ selectParcel(withUp.id,'urban'); openEngine(); },
    method(){ selectParcel(withUp.id,'urban'); openMethodology(1); },
    'method-last'(){ selectParcel(withUp.id,'urban'); openMethodology(5); },
    ai(){ selectParcel(withUp.id,'urban'); toggleAI(true); aiAsk('What can I build here?'); },
    'ai-quota'(){ selectParcel(withUp.id,'urban'); toggleAI(true); aiAsk('What can I build here?');
      setTimeout(()=>aiAsk('Explain FAR and coverage'),700); setTimeout(()=>aiAsk('Is this parcel worth developing?'),1400); },
    'rail-collapsed'(){ selectParcel(withUp.id,'urban'); STATE.railOpen=false; renderRail(); },
    'layers-all'(){ STATE.paidMarket=true; ['owner','restit','landuse','heatFAR','traffic','heatMkt'].forEach(k=>STATE.layers[k]=true); renderRail(); renderMap(); renderLegend(); },
    dep(){ STATE.layers.cadastre=false; STATE.layers.owner=true; renderRail(); renderMap(); renderLegend(); },
    'legend-min'(){ $q('#legmin').click(); },
    uncovered(){ clearSelection(); $q('#panel').classList.add('hidden'); $q('#coverwarn').classList.add('on'); const u=$q('#uncovered'); if(u) u.setAttribute('fill','#d0c9b8'); },
    admin(){ toggleAdmin(true); },
    'admin-review'(){ toggleAdmin(true); tab('review'); },
    'admin-rules'(){ toggleAdmin(true); tab('rules'); },
    'admin-fin'(){ toggleAdmin(true); tab('fin'); },
    'admin-engine'(){ toggleAdmin(true); tab('engine'); },
    'admin-orders'(){ toggleAdmin(true); tab('orders'); },
    'admin-data'(){ toggleAdmin(true); tab('data'); },
    'narrow-1100'(){ selectParcel(withUp.id,'urban'); },
    'narrow-860'(){ selectParcel(withUp.id,'urban'); },
    'narrow-760'(){ selectParcel(withUp.id,'urban'); },
    dump(){ dumpComputed(); }
  };
  const PROPS=['font-family','font-size','font-weight','font-style','letter-spacing','line-height','text-transform','color',
    'background-color','background-image','border','border-radius','box-shadow','padding','margin','height','width','max-width',
    'min-height','gap','opacity','filter','right','bottom','left','top','position'];
  const SEL=['body','.topbar','.brand .logo','.searchwrap','.searchwrap input','.searchwrap .kbd','.searchsug','.searchsug button','.searchsug .ico',
    '.topnav button','.topnav button.active','.pill','.rail','.railhead','.railhead b','.rail .rlabel','.lyr','.lyr .swatch','.lyr .nm','.lyr .subnm',
    '.lyr .chk','.lyr .paidlock','.lyr .subnm.lockedsub','.depnote','.railopen','.mapwrap','.legend','.legend h4','.legh','.legrow','.legrow .sw',
    '.coverwarn','.scalebar .t','.coords','.maptools','.mbtn','.zlabel','.panel','.pscroll','.pempty','.pempty .ill','.pempty h3','.pempty p',
    '.pempty .pinnote','.chiphint','.phead','.peyebrow','.peyebrow .tag','.ptitle','.psub','.pclose','.backlink','.idgrid','.idcell','.idcell .k',
    '.idcell .v','.sect','.secthead','.secthead .lbl','.badge','.badge.free','.badge.paid','.prow','.prow .pk','.prow .pv','.prow .pv .u','.srcref',
    '.vscard','.vscard .txt','.vsdelta','.upcard','.upcard .upn','.upcard .upd','.upcard .upgo','.upcard.none','.lockedlist','.lrow','.lk2','.lk2 .ld',
    '.lockval','.lu','.lockcta','.lockcta .lki','.lockcta .lct b','.lockcta .lct span','.lockcta .b','.roihero','.roihero .rlab','.roihero .rval',
    '.roihero .rrange','.rangebar','.rangebar .fill','.rangebar .mark','.assum','.assum .ah','.arow label','.arow .av','.ctastack','.cta','.cta.gold',
    '.cta.ghost','.cta.line','.cta.primary','.cta small','.doclist','.docitem','.docitem .di','.docitem .dn','.docitem .dn .dm','.dstat','.dstat.adopted',
    '.dstat.progress','.aifab','.aifab .badge2','.aipanel','.aihead','.aihead .av','.aihead .t h4','.aihead .t p','.aiquota','.aiquota .qd','.aibody',
    '.msg .b','.msg.bot .b','.msg.user .b','.msg .b .cite','.aichips','.aichip','.aifoot','.aifoot input','.aifoot .send','.overlay','.modal','.modal.wide',
    '.mhead','.mhead .mi','.mhead .meyebrow','.mhead h2','.mhead p','.mhead .x','.mbody','.mfoot','.mfoot .fnote','.mfoot .cta','.field','.field label',
    '.field label .opt','.field input','.frow','.ctx','.ctx .ci','.ctx .cd','.ordersum','.osrow','.osrow em','.osrow .mono','.osrow.deliv','.pricenote',
    '.fieldlab','.seg','.segb','.segb.on','.methlink','.methlink .mlt b','.methlink .mlt span','.methlink .mla','.paysummary','.payline','.payline.total',
    '.paycard','.paycard .brandmark','.securenote','.success','.success .ok','.success h2','.success p','.orderref','.plans','.plan','.plan.feat',
    '.plan .ptag','.plan h3','.plan .pr','.plan .pr small','.plan .pd','.plan li','.ftable','.frow2','.frow2 .fn','.frow2 .ff','.frow2 .fs','.irow',
    '.irow .in2','.irow .id2','.growbox','.growbox b','.method','.mrail','.mrail-h','.mstep','.mstep.on','.mstep .mn','.mstep.on .mn','.mstep.done .mn',
    '.mstep .ml b','.mstep .ml span','.mpane','.mpane .peyebrow2','.mpane h2','.mpane .phase','.mdiagram','.mbodytext','.mbodytext .sub','.mnav',
    '.mnav .mcount','.mprog','.mprog i','.admin','.adminbar','.adminbar .at','.admintabs button','.admintabs button.active','.adminbody','.astat-grid',
    '.astat','.astat .sl','.astat .sv','.astat .sv small','.astat .sd','.card','.cardhd','.cardhd h3','.cardhd .sub','.tbl','.tbl th','.tbl td',
    '.tbl .mono','.st','.st.ok','.st.pend','.st.rev','.abtn','.abtn.sm','.abtn.ghost','.review-item','.review-item .rq','.review-item .rsrc',
    '.review-item .rextract','.toast','.toast .ti','.zone','.zoneline','.blockline','.docarea','.uparcel','.parcel','.road.major','.road.minor','.river'];
  const out={};
  function probe(scope){
    SEL.forEach(s=>{ if(out[s]) return; const e=document.querySelector(s); if(!e) return;
      const cs=getComputedStyle(e); const r={}; PROPS.forEach(p=>{ r[p]=cs.getPropertyValue(p); });
      const b=e.getBoundingClientRect(); r._rect={w:Math.round(b.width*10)/10,h:Math.round(b.height*10)/10}; r._scope=scope; out[s]=r; });
  }
  function dumpComputed(){
    probe('initial');
    RUN.search(); probe('search'); $q('#searchsug').classList.remove('on');
    RUN.pin(); probe('pin');
    selectParcel(withUp.id,'cad'); probe('cadastral');
    selectParcel(noUp.id,'cad'); probe('cadastral-none');
    selectParcel(withUp.id,'urban'); probe('urban');
    toggleAI(true); aiAsk('What can I build here?'); probe('ai');
    STATE.layers.cadastre=false; STATE.layers.owner=true; renderRail(); probe('dep'); STATE.layers.cadastre=true; renderRail();
    STATE.railOpen=false; renderRail(); probe('rail-collapsed'); STATE.railOpen=true; renderRail();
    selectZone('B'); probe('zone');
    selectDoc('A'); probe('doc');
    subscribe('market'); selectParcel(withUp.id,'urban'); probe('urban-paid');
    openOrder(); probe('order'); openPay(); probe('pay'); orderSuccess(); probe('success');
    openUpgrade('market'); probe('upgrade'); openEngine(); probe('engine'); openMethodology(1); probe('method'); closeModal();
    toggleAdmin(true); probe('admin'); tab('review'); probe('admin-review'); toggleAdmin(false);
    toast('x'); probe('toast');
    const root=getComputedStyle(document.documentElement);
    const tokens={}; ['--ink','--ink-2','--ink-3','--paper','--paper-2','--line','--rule','--brand','--brand-dark','--brand-tint','--z-res','--z-com',
      '--z-mix','--z-pub','--z-grn','--paid','--paid-dark','--paid-tint','--danger','--white','--shadow','--shadow-lg','--r','--r-lg','--mono','--disp','--body']
      .forEach(t=>tokens[t]=root.getPropertyValue(t).trim());
    const pre=document.createElement('pre'); pre.id='computed'; pre.textContent=JSON.stringify({tokens,selectors:out},null,1);
    document.body.appendChild(pre);
  }
  if(state!=='toast') $q('#toast').style.display='none';
  setTimeout(()=>{ (RUN[state]||RUN.empty)(); document.documentElement.setAttribute('data-state',state); },60);
})();
</script>
"""


def find_chrome() -> str:
    for c in CHROME_CANDIDATES:
        if c and Path(c).exists():
            return c
    sys.exit("Chrome / Edge not found; set CHROME_BIN")


def build_harness(workdir: Path) -> Path:
    html = SOURCE.read_text(encoding="utf-8")
    html, n = re.subn(r"<style>/\* cyrillic-ext \*/.*?</style>", FONTS_LINK, html, count=1, flags=re.S)
    assert n == 1, "font-face block not found"
    pre = (
        "<script>window.__resources={logo:'UrbanView_logo.svg',mark:'UrbanView_mark.svg'};"
        "(function(){let s=20260924;Math.random=function(){s=(s*1664525+1013904223)>>>0;return s/4294967296;};})();"
        "</script>\n<script>"
    )
    html, n = re.subn(r'(<script id="appdata"[^>]*>\{\}</script>\s*)<script>', lambda m: m.group(1) + pre, html, count=1)
    assert n == 1, "app script not found"
    html = html.replace("</body>", DRIVER + "</body>", 1)
    out = workdir / "harness.html"
    out.write_text(html, encoding="utf-8")
    for svg in ("UrbanView_logo.svg", "UrbanView_mark.svg"):
        shutil.copy(BRAND / svg, workdir / svg)
    return out


def chrome_args(chrome: str, workdir: Path, size: str) -> list[str]:
    return [
        chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={workdir / 'profile'}", f"--window-size={size}", "--virtual-time-budget=4000",
        "--force-device-scale-factor=1",
    ]


def main(argv: list[str]) -> None:
    dump_only = "--dump" in argv
    wanted = [a for a in argv if not a.startswith("--")]
    chrome = find_chrome()
    workdir = Path(tempfile.mkdtemp(prefix="urbanview-screens-"))
    harness = build_harness(workdir)
    url = harness.resolve().as_uri()
    if not dump_only:
        SCREENS.mkdir(exist_ok=True)
        for state, size in STATES:
            if wanted and state not in wanted:
                continue
            target = SCREENS / f"{state}.png"
            subprocess.run(chrome_args(chrome, workdir, size) + [f"--screenshot={target}", f"{url}#{state}"],
                           check=True, capture_output=True, timeout=120)
            print(f"{state:22s} {size:>9s}  {target.stat().st_size // 1024:5d} KB")
    if dump_only or not wanted:
        res = subprocess.run(chrome_args(chrome, workdir, "1440,900") + ["--dump-dom", f"{url}#dump"],
                             check=True, capture_output=True, timeout=120, encoding="utf-8", errors="replace")
        m = re.search(r'<pre id="computed">(.*?)</pre>', res.stdout, flags=re.S)
        if not m:
            sys.exit("computed-style dump not found in DOM")
        import html as html_mod
        data = json.loads(html_mod.unescape(m.group(1)))
        COMPUTED.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"computed styles: {len(data['selectors'])} selectors -> {COMPUTED}")
    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1:])
