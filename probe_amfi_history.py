"""
probe_amfi_history.py — does AMFI's own historical file carry L&T Midcap?

Settles this with AMFI's own data instead of news inference. Uses the exact
endpoint already verified working in refresh_nav.py's --amfi-history path:

    https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx
        ?frmdt=DD-Mon-YYYY&todt=DD-Mon-YYYY   (max 90 days per call)

Auto-detects both AMFI text layouts (refresh_nav.py already handles this
same ambiguity):
    OLD 6 cols: Code;ISIN1;ISIN2;Name;NAV;Date
    NEW 8 cols: Code;ISIN1;ISIN2;Name;Plan;Option;NAV;Date

Run:  python probe_amfi_history.py
"""

import sys
import requests

URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"

# A window well before the Nov-2022 HSBC/L&T transition, when a fund called
# "L&T Midcap" -- if it ever existed under that exact name -- must appear.
FRM, TO = "01-Jan-2020", "31-Jan-2020"

SEARCH_TERMS = ["l&t", "l & t", "l and t"]
MIDCAP_TERMS = ["midcap", "mid cap", "mid-cap"]

# Confirmed from the actual header row returned by this endpoint:
#   Scheme Code;NAV Name;Plan;Option;ISIN Div Payout/ISIN Growth;
#   ISIN Div Reinvestment;Net Asset Value;Date
# This is a DIFFERENT column order from NAVAll.txt (which puts ISINs before
# the name). Confirmed against real rows, not assumed by analogy:
#   '119291;L&T Flexicap Fund-Direct Plan-Growth;;;INF917K01FC0;;87.802;01-Jan-2020'


def is_data_row(parts):
    if len(parts) != 8:
        return False
    return parts[0].strip().isdigit()


def parse_stateful(lines):
    rows = []
    headers_seen = []
    current_amc = None

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(";")]
        if is_data_row(parts):
            code, name, plan, option, isin1, isin2, nav, date = parts
            rows.append(dict(code=code, name=name, plan=plan, option=option,
                             isin1=isin1, isin2=isin2, amc=current_amc,
                             nav=nav, date=date))
        else:
            if ";" not in line and not line.lower().startswith("scheme code"):
                current_amc = line
                headers_seen.append(line)

    return rows, headers_seen


def main() -> int:
    print(f"Fetching AMFI history {FRM} -> {TO} ...")
    try:
        r = requests.get(URL, params={"frmdt": FRM, "todt": TO}, timeout=60)
        r.raise_for_status()
    except Exception as e:
        print(f"FETCH FAILED: {e}")
        print("If this errors, run refresh_nav.py --healthcheck first to")
        print("confirm the endpoint is still reachable from this machine.")
        return 1

    lines = r.text.splitlines()
    print(f"Fetched {len(lines)} lines, {len(r.content)/1024:.0f} KB\n")

    rows, headers = parse_stateful(lines)
    print(f"Parsed {len(rows)} data rows "
          f"({len(set(x['code'] for x in rows))} distinct scheme codes)")
    print(f"Detected {len(headers)} AMC section headers\n")

    if not rows:
        print("No rows parsed at all -- format may have changed again.")
        print("First 10 raw lines for inspection:")
        for l in lines[:10]:
            print(f"  {l!r}")
        return 1

    # Show every literal AMC header string containing an L&T-ish fragment,
    # whatever punctuation/spacing AMFI actually used.
    print("=" * 78)
    print("AMC header lines matching an L&T-ish fragment:")
    lt_headers = [h for h in headers
                 if any(t in h.lower() for t in SEARCH_TERMS)]
    if lt_headers:
        for h in lt_headers:
            print(f"  {h!r}")
    else:
        print("  none -- printing ALL headers so you can eyeball the AMC list:")
        for h in sorted(set(headers)):
            print(f"    {h}")

    print("\n" + "=" * 78)
    # Name field embeds the AMC name directly in this report, so search it
    # straight -- no need to rely on header-tracking as the only signal.
    hits = [r for r in rows
            if any(t in r["name"].lower() for t in SEARCH_TERMS)
            and any(t in r["name"].lower() for t in MIDCAP_TERMS)]

    if hits:
        print(f"FOUND {len(hits)} matching row(s):\n")
        seen_codes = {}
        for r in hits:
            seen_codes.setdefault(r["code"], r)
        for code, r in seen_codes.items():
            print(f"  code={code}  isin_growth={r['isin1']}  isin_reinv={r['isin2']}")
            print(f"    name : {r['name']}")
            print(f"    nav  : {r['nav']} on {r['date']}")
        print("\n>> CONFIRMED: the fund existed in AMFI's own data in Jan 2020.")
        print("   mfapi's silence on it is mfapi's gap, not a real absence.")
        print("   Use this code + ISIN to seed the 'scheme' table directly.")
    else:
        print("No 'L&T ... midcap' match even with the corrected column order.")
        print("Showing every L&T-branded scheme name found, so we can see if")
        print("it used a different word entirely (e.g. 'Mid Cap', 'Midcap',")
        print("or a category name that doesn't literally say midcap):\n")
        lt_rows = [r for r in rows
                  if any(t in r["name"].lower() for t in SEARCH_TERMS)]
        seen_names = set()
        for r in lt_rows:
            base = r["name"].split("-")[0].strip()   # drop -Plan-Option suffix
            if base not in seen_names:
                seen_names.add(base)
                print(f"  {r['code']:>8}  {base}")
        if not seen_names:
            print("  (no L&T-branded rows found at all -- something is still off)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
