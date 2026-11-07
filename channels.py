"""Profit per client and per marketing channel."""
import re
import pandas as pd

STUDIO_HOURS = {"content day": 6, "reels pack": 3, "retainer shoot": 4}


def studio_hours(service, costs=None):
    s = service.lower()
    if s in STUDIO_HOURS:
        return STUDIO_HOURS[s]
    m = re.search(r"(\d+)\s*hr", s)
    return float(m.group(1)) if m else 0


def pay_rates(team):
    rates = {}
    for _, t in team.iterrows():
        m = re.search(r"[\d.]+", str(t.get("pay", "")))
        if m:
            rates[t["name"]] = float(m.group(0))
    return rates


def client_profit(r, data, studio_rate=40.0):
    B, I, PB, L = r["bookings"], r["invoices"], r["board"], r["leads"]
    rates = pay_rates(data.get("team", pd.DataFrame(columns=["name", "pay"])))
    done = B[B["status"] == "completed"].copy()
    done["studio_cost"] = done["service"].map(studio_hours) * studio_rate
    pb = PB.copy()
    pb["editor_cost"] = pb["editor_hours"].astype(float) * pb["editor"].map(rates).fillna(0)
    inv = I[I["status"].str.lower() != "void"]
    rows = []
    for _, c in r["clients"].iterrows():
        cid = c["client_id"]
        leads = L[L["client_id"] == cid].sort_values("created_dt")
        won = leads[leads["status"] == "Won"]
        src = (won if len(won) else leads)["source"].iloc[0] if len(leads) else "Unknown"
        first = leads["created_dt"].min() if len(leads) else pd.NaT
        rev = inv.loc[inv["client_id"] == cid, "amount_num"].sum()
        sc = done.loc[done["client_id"] == cid, "studio_cost"].sum()
        ec = pb.loc[pb["client_id"] == cid, "editor_cost"].sum()
        rows.append(dict(client_id=cid, client=c["client"], source=src, first_contact=first,
                         billed=rev, studio_cost=sc, editor_cost=round(ec, 2), profit=round(rev - sc - ec, 2),
                         videos=int((pb["client_id"] == cid).sum())))
    return pd.DataFrame(rows)


def channel_table(r, data, today):
    cp = client_profit(r, data)
    cp["months"] = ((today - cp["first_contact"]).dt.days / 30.4).clip(lower=1)
    cp["profit_per_month"] = cp["profit"] / cp["months"]
    L = r["leads"]
    junk = L["business"].fillna("").eq("") & L["name"].str.contains(r"test|asdf|crypto|seo", case=False)
    leads = L[~junk & L["client_id"].isna() | (L["status"] == "Won")]
    t = cp.groupby("source").agg(clients=("client", "count"), billed=("billed", "sum"), profit=("profit", "sum"),
                                 profit_per_client=("profit", "mean"), profit_per_client_month=("profit_per_month", "mean"))
    t["leads"] = L[~junk].groupby("source")["lead_id"].count()
    t["win_rate"] = (t["clients"] / t["leads"]).round(2)
    return cp, t.sort_values("profit", ascending=False)
