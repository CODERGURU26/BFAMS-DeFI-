"""Prototype institution-linking workflow; does not create connections or sync data."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/testing/connect", tags=["Testing connection prototype"])


@router.get("", response_class=HTMLResponse, include_in_schema=False)
def connection_prototype():
    return HTMLResponse(_CONNECTION_HTML)


_CONNECTION_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>BFAMS · Connect account prototype</title>
  <style>
    :root{font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;color:#e8edf8;background:#0b1020;font-synthesis:none;text-rendering:optimizeLegibility}
    *{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 78% -20%,#1b3150 0,transparent 38%),#0b1020}
    .shell{max-width:1180px;margin:auto;padding:28px 36px 60px}.top{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #253047;padding-bottom:20px}
    .brand{display:flex;gap:12px;align-items:center;font-weight:750}.mark{width:36px;height:36px;border-radius:12px;background:linear-gradient(145deg,#78e3c0,#48a7e9);display:grid;place-items:center;color:#08121b;font-weight:900}
    .tag{font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:#92a2bd}.env,.proto{border:1px solid #286b60;background:#102a2b;color:#86e3c4;border-radius:999px;padding:8px 12px;font-size:11px;font-weight:750;letter-spacing:.08em}
    .nav{display:flex;gap:9px}.nav a{color:#adbad0;text-decoration:none;border:1px solid #34425d;background:#151d30;border-radius:9px;padding:8px 12px;font-size:12px}
    .intro{margin:36px 0 24px}.eyebrow{color:#68d9bd;font-size:12px;font-weight:750;letter-spacing:.13em;text-transform:uppercase}h1{font-size:clamp(28px,4vw,38px);letter-spacing:-.045em;margin:8px 0}.sub{color:#99a6bd;margin:0;font-size:14px;max-width:690px;line-height:1.6}
    .layout{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(280px,.65fr);gap:18px;align-items:start}.panel{background:linear-gradient(145deg,#151d30,#111827);border:1px solid #263149;border-radius:16px;padding:22px}
    .steps{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:22px}.step{border-top:2px solid #2a3851;padding-top:10px;font-size:11px;color:#77859d}.step.active{border-color:#65d7ba;color:#b5f0e1}.step.done{border-color:#438c7e;color:#8edbc8}
    h2{font-size:16px;margin:0 0 8px}.copy{font-size:13px;color:#9ba9bf;line-height:1.6;margin:0 0 19px}.field{margin:17px 0}.field label{display:block;color:#aebbd0;font-size:12px;font-weight:650;margin-bottom:8px}
    select{width:100%;padding:12px 13px;border:1px solid #34425d;border-radius:10px;background:#0f1727;color:#e7edf8;font:inherit;font-size:13px;outline:none}select:focus{border-color:#68d9bd}
    .hint{font-size:11px;color:#7f8da4;line-height:1.55;margin-top:7px}.alert{border:1px solid #694c2c;background:#2b2218;color:#f2cb8c;padding:13px 14px;border-radius:11px;font-size:12px;line-height:1.6;margin:16px 0}
    .checks{display:grid;gap:9px;margin:16px 0}.check{display:flex;align-items:flex-start;gap:10px;border:1px solid #29354b;border-radius:10px;padding:12px;background:#111a2a;font-size:12px;color:#bdc9db}.check input{accent-color:#65d7ba;margin-top:2px}
    .actions{display:flex;justify-content:space-between;gap:10px;margin-top:20px}.button{border:1px solid #34425d;color:#e0eaf8;background:#151d30;border-radius:10px;padding:11px 15px;font-weight:680;font-size:12px;cursor:pointer}.button.primary{border-color:#5dceaf;background:#55c7a8;color:#071a17}.button:hover{filter:brightness(1.08)}.button:disabled{opacity:.55;cursor:not-allowed}
    .side-title{font-size:13px;margin:0 0 13px}.info{padding:12px 0;border-bottom:1px solid #29354b}.info:last-child{border:0}.info b{display:block;font-size:12px;margin-bottom:4px}.info span{font-size:11px;color:#95a3b9;line-height:1.5}.pill{display:inline-block;border-radius:99px;padding:5px 9px;font-size:10px;font-weight:750;letter-spacing:.06em;background:#392a18;color:#f4c273}
    .result{display:none;border:1px solid #694c2c;background:#2b2218;color:#f2cb8c;border-radius:12px;padding:15px;margin-top:18px;font-size:12px;line-height:1.65}.result.show{display:block}.result strong{display:block;font-size:13px;margin-bottom:5px}
    .foot{font-size:11px;color:#78869d;line-height:1.6;margin-top:20px}.review-row{display:flex;justify-content:space-between;padding:10px 0;border-bottom:1px solid #29354b;font-size:12px}.review-row span:first-child{color:#91a0b7}.review-row span:last-child{font-weight:650;text-align:right}
    @media(max-width:800px){.shell{padding:20px}.layout{grid-template-columns:1fr}.top{align-items:flex-start}.nav{flex-wrap:wrap;justify-content:flex-end}}
    @media(max-width:520px){.shell{padding:16px}.top{gap:12px}.env{display:none}.steps{gap:5px}.step{font-size:10px}.panel{padding:17px}.actions{flex-direction:column-reverse}.actions button{width:100%}}
  </style>
</head>
<body>
<main class="shell">
  <header class="top">
    <div class="brand"><div class="mark">B</div><div>BFAMS <span class="tag">portfolio lab</span></div></div>
    <nav class="nav"><a href="/testing/dashboard">Portfolio dashboard</a><span class="env">TEST DATABASE</span></nav>
  </header>
  <section class="intro"><div class="eyebrow">Account linking</div><h1>Connect an institution</h1><p class="sub">A future secure connection should let you authorize financial data sharing, discover eligible accounts, and refresh investments with your consent.</p></section>
  <section class="layout">
    <div class="panel">
      <div class="steps"><div class="step active" id="step1">01 &nbsp; Institution</div><div class="step" id="step2">02 &nbsp; Consent</div><div class="step" id="step3">03 &nbsp; Secure connection</div></div>
      <section id="select-view">
        <h2>Choose what you want to link</h2><p class="copy">Select the institution and account category to prepare a consent preview. This prototype does not collect account numbers, passwords, OTPs, or other banking credentials.</p>
        <div class="field"><label for="institution">Institution</label><select id="institution"><option value="">Choose an institution</option><option>State Bank of India</option><option>HDFC Bank</option><option>ICICI Bank</option><option>Axis Bank</option><option>Kotak Mahindra Bank</option><option>Other supported institution</option></select><div class="hint">Availability depends on the live Account Aggregator and the institution's supported data types.</div></div>
        <div class="field"><label for="account-type">Data account type</label><select id="account-type"><option value="securities">Investments / securities</option><option value="deposit">Bank deposits and transactions</option><option value="both">Investments and bank transactions</option></select></div>
        <div class="field"><label for="provider">Account Aggregator</label><select id="provider"><option value="finvu">Finvu (prototype selection)</option><option value="other">Another supported provider (not configured)</option></select><div class="hint">Provider authorization and consent APIs are not connected in this build.</div></div>
        <div class="actions"><span></span><button class="button primary" id="review">Review consent →</button></div>
      </section>
      <section id="consent-view" hidden>
        <h2>Review data-sharing consent</h2><p class="copy">In the live flow, the user would review exact data categories, purpose, duration, and frequency on the authorized provider's consent screen.</p>
        <div id="review-summary"></div>
        <div class="checks"><label class="check"><input type="checkbox" checked disabled> Read investment holdings and securities information</label><label class="check"><input type="checkbox" id="transactions"> Read bank transactions for portfolio context</label></div>
        <div class="field"><label for="duration">Proposed consent duration</label><select id="duration"><option value="90 days">90 days</option><option value="180 days">180 days</option><option value="1 year">1 year</option></select></div>
        <div class="alert">Prototype only: reviewing this screen does not grant consent, contact a bank, or store your selection.</div>
        <div class="actions"><button class="button" id="back">← Back</button><button class="button primary" id="connect">Continue to secure provider</button></div>
        <div class="result" id="result"><strong>Live connection unavailable</strong>This is a non-persistent prototype. No provider redirect, bank access, account discovery, consent, data refresh, or database write occurred. Finvu's live sandbox integration must be configured before this step can connect.</div>
      </section>
    </div>
    <aside class="panel">
      <h2 class="side-title">Connection status</h2><span class="pill">NOT CONNECTED</span>
      <div class="info"><b>Secure authorization</b><span>Live authorization must happen with the supported Account Aggregator. BFAMS should never request bank login credentials.</span></div>
      <div class="info"><b>Data stays consent-led</b><span>Only requested data categories and the consented time period should be fetched.</span></div>
      <div class="info"><b>Automatic refresh</b><span>After a real connection is implemented, sync status and investment updates can appear in the portfolio dashboard.</span></div>
      <div class="info"><b>Current build</b><span>UI prototype only. Selections are held in this page and discarded when you leave.</span></div>
    </aside>
  </section>
  <p class="foot">TEST prototype · No information entered here is transmitted or persisted. Never enter banking credentials, account numbers, or OTPs into this prototype.</p>
</main>
<script>
const institution=document.getElementById('institution'), accountType=document.getElementById('account-type'), provider=document.getElementById('provider');
const selection=document.getElementById('select-view'), consent=document.getElementById('consent-view');
function step(active){['step1','step2','step3'].forEach((id,i)=>{const el=document.getElementById(id);el.className='step'+(i+1<active?' done':i+1===active?' active':'')})}
document.getElementById('review').addEventListener('click',()=>{if(!institution.value){institution.focus();institution.reportValidity();institution.setCustomValidity('Choose an institution to continue.');institution.reportValidity();setTimeout(()=>institution.setCustomValidity(''),1500);return}selection.hidden=true;consent.hidden=false;step(2);const type=accountType.options[accountType.selectedIndex].text;const rows=[['Institution',institution.value],['Requested data',type],['Provider',provider.options[provider.selectedIndex].text],['Purpose','Portfolio overview and investment tracking'],['Refresh frequency','On user request (proposed)']];const box=document.getElementById('review-summary');box.replaceChildren();rows.forEach(([label,value])=>{const row=document.createElement('div');row.className='review-row';const l=document.createElement('span');l.textContent=label;const v=document.createElement('span');v.textContent=value;row.append(l,v);box.append(row)})});
document.getElementById('back').addEventListener('click',()=>{consent.hidden=true;selection.hidden=false;step(1);document.getElementById('result').classList.remove('show')});
document.getElementById('connect').addEventListener('click',()=>{step(3);document.getElementById('result').classList.add('show')});
</script>
</body>
</html>"""
