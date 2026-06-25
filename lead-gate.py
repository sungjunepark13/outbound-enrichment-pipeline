#!/usr/bin/env python3
"""Round 3 gate — WIDENED ICP + domain-dedup (F9 fix). Hard geo, OPERATIONAL,
domain-dedup, and a SHRUNK deterministic DQ (only clear non-fab: distributors/
supply/gas, leather/non-fab, equipment dealers). Auto/ornamental/structural/welding
are now ALLOWED through to the judgment node (widened ICP)."""
import json, math, os, re
RAW=os.path.expanduser("./data/raw_candidates.json")
OUT=os.path.expanduser("./data/widened_gated.json")
CENTER=(33.92,-83.40); RADIUS_KM=24.0
def hav(a,b):
    R=6371.0;(la1,lo1),(la2,lo2)=a,b
    p1,p2=math.radians(la1),math.radians(la2);dp=math.radians(la2-la1);dl=math.radians(lo2-lo1)
    h=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*R*math.asin(math.sqrt(h))
# Widened DQ: only clear NON-fabrication businesses
DQ=[("welding supply","distributor"),("welding & steel","distributor"),("gases","distributor"),
    ("airgas","distributor"),("holston","distributor"),("steelmart","distributor"),
    ("steel depot","distributor"),("equipment hub","equipment dealer"),("leather","non-fab")]
def dq(n,types):
    nl=n.lower()
    for kw,r in DQ:
        if kw in nl: return r
    if any(t in("store","hardware_store","gas_station") for t in types): return "retail/store"
    return None
def root(u):
    if not u: return None
    return re.sub(r"^https?://","",u).split("/")[0].lower().replace("www.","")
def main():
    raw=json.load(open(RAW)); cands=[]
    for p in raw:
        n=p.get("displayName",{}).get("text","?"); loc=p.get("location",{})
        ll=(loc.get("latitude"),loc.get("longitude")); types=p.get("types",[])
        if p.get("businessStatus")!="OPERATIONAL": continue
        if None in ll: continue
        d=hav(CENTER,ll)
        if d>RADIUS_KM: continue
        if dq(n,types): continue
        cands.append({"name":n,"id":p.get("id"),"addr":p.get("formattedAddress",""),
            "web":p.get("websiteUri"),"phone":p.get("nationalPhoneNumber"),
            "reviews":p.get("userRatingCount") or 0,"rating":p.get("rating"),
            "hours":bool(p.get("regularOpeningHours")),"dist_km":round(d,1),"ll":ll})
    # domain-dedup (F9): keep highest-review per root domain; name+addr dedup for no-web
    seen={}; deduped=[]; dropped_dup=[]
    for c in sorted(cands,key=lambda x:-x["reviews"]):
        key=root(c["web"]) or re.sub(r"\W+","",c["name"].lower())[:18]+"|"+re.sub(r"\W+","",c["addr"].lower())[:18]
        if key in seen: dropped_dup.append((c["name"],seen[key])); continue
        seen[key]=c["name"]; deduped.append(c)
    deduped.sort(key=lambda x:x["dist_km"])
    json.dump(deduped,open(OUT,"w"),indent=2)
    print(f"raw {len(raw)} -> in-cluster+operational+DQ {len(cands)} -> domain-deduped {len(deduped)} (dropped {len(dropped_dup)} dups)")
    if dropped_dup: print("  dedup dropped:", [f"{a} (dup of {b})" for a,b in dropped_dup])
    print(f"\n{'NAME':34} {'KM':>4} {'WEB':3} {'REV':>3}  CITY")
    for c in deduped:
        city=[t.strip() for t in c['addr'].split(',')]; city=city[1] if len(city)>2 else ''
        print(f"{c['name'][:33]:34} {c['dist_km']:>4} {'Y' if c['web'] else '-':3} {str(c['reviews']):>3}  {city}")
if __name__=="__main__": main()
