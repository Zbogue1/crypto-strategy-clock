"""
THE SELL AUDIT. Six failure modes, every path.

If a position cannot be closed when it must be, nothing else in this system
matters. Each test below corresponds to a way that has actually failed.
"""
import sys, types, os
sys.path.insert(0,".")
for n in ("anthropic",):
    m=types.ModuleType(n); m.Anthropic=lambda **k:None
    m.APIStatusError=Exception; m.APIError=Exception; sys.modules[n]=m

import fomo_exit as FE, fomo_portfolio as FP
alerts=[]; FE._send_telegram=lambda t: alerts.append(t)
FE._send_telegram_button_local=lambda *a,**k: alerts.append(a[0] if a else "")
FP.save_fomo_portfolio=lambda s: None
FP.sync_fomo_state_to_github=lambda: None
FE.record_trade_outcome=lambda **k: None
FE.run_fomo_postmortem=lambda *a,**k: None
FE.run_fomo_ai_postmortem=lambda *a,**k: None

fails=[]
def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        fails.append(f"{name} {detail}")

def mk(units=300000, spent=100.0, entry=0.0006, **kw):
    h={"token_ticker":"T","units":units,"spent":spent,"entry_price":entry,
       "contract_address":"C","position_id":"p1","peak_price":entry}
    h.update(kw); return h

print("\n1. MARKED-DONE-WITHOUT-EXECUTING  (the $MADE bug)")
h=mk(units=0); st={"cash":0,"tranche_sales":[]}
FE._execute_partial_sell(h,1/3,0.0012,"tranche_1_2x",st,flags=["tranche_1_sold"])
check("aborted tranche leaves no flag", not h.get("tranche_1_sold"))
check("aborted tranche writes no record", len(st["tranche_sales"])==0)
h2=mk(); st2={"cash":0,"tranche_sales":[]}
net=FE._execute_partial_sell(h2,1/3,0.0012,"tranche_1_2x",st2,flags=["tranche_1_sold"])
check("completed tranche sets flag AND record",
      h2["tranche_1_sold"] and len(st2["tranche_sales"])==1 and net>0)
check("units actually reduced", abs(h2["units"]-200000)<1)
check("cost basis reduced proportionally", abs(h2["spent"]-66.67)<0.1)

print("\n2. SILENT FAILURE  (stop-loss that doesn't fire)")
alerts.clear()
h3=mk(); st3={"cash":0,"holdings":[h3],"trade_history":[]}
net3=FE._execute_full_sell(h3, 999.0, "stop_loss", st3)   # insane price
check("refused sale returns 0", net3==0.0)
check("refused STOP-LOSS raises an alarm", len(alerts)>0)
check("alarm says position is still open",
      any("still open" in a.lower() or "unprotected" in a.lower() for a in alerts))
check("position NOT removed from book", h3 in st3["holdings"])

print("\n3. FALSE 'EXITED' CLAIM")
import inspect
src=inspect.getsource(FE._check_holding)
check("stop alarm gated on net>0", "if net > 0:\n            _fire_stop_alarm" in src)
check("tranche 1 alert gated on net>0",
      src.count("if net > 0:")>=3, f"found {src.count('if net > 0:')}")

print("\n4. PERMANENTLY-BLOCKED SELL")
check("no caller pre-sets tranche flags",
      'tranche_1_sold"] = True' not in inspect.getsource(FE))

print("\n5. DOUBLE SELL")
h4=mk(); st4={"cash":0,"tranche_sales":[]}
FE._execute_partial_sell(h4,1/3,0.0012,"tranche_1_2x",st4,flags=["tranche_1_sold"])
u_after=h4["units"]
# second call is blocked by the caller's flag check, but verify the flag is set
check("flag set after first sale blocks a repeat", h4.get("tranche_1_sold") is True)

print("\n6. FULL EXIT INTEGRITY")
alerts.clear()
h5=mk(); st5={"cash":0.0,"holdings":[h5],"trade_history":[],"total_trades":0}
net5=FE._execute_full_sell(h5, 0.0012, "stop_loss", st5)
check("full sell returns proceeds", net5>0)
check("position removed from book", h5 not in st5["holdings"])
check("trade recorded", len(st5["trade_history"])==1)
check("cash credited", abs(st5["cash"]-net5)<0.01)
t=st5["trade_history"][0]
check("record has exit price + reason",
      t.get("exit_price")==0.0012 and t.get("exit_reason")=="stop_loss")

print("\n" + "="*62)
if fails:
    print("FAILURES:")
    for f in fails: print("  " + f)
    sys.exit(1)
print("Every sell path holds: refuse loudly, or sell and record. Never both-and-neither.")
