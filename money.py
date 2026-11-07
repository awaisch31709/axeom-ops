"""Money truth: match bank payments to invoices, then compare with what the billing tool says."""
import re
from itertools import combinations

import pandas as pd

from pipeline import TODAY

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def account(row):
    """Who pays: the agency for agency-billed rows, else the client."""
    return f"agency:{row['email_n']}" if row.get("paid_by_agency") else row.get("client_id")


def allocate(I, P, today=TODAY):
    I = I.copy()
    P = P.copy()
    I["acct"] = [account(r) for _, r in I.iterrows()]
    P["acct"] = [account(r) for _, r in P.iterrows()]
    live = I["status"].str.lower() != "void"
    I["paid_true"] = 0.0
    I["paid_by"] = [[] for _ in range(len(I))]
    P["left"] = P["amount_num"]
    P["applied_to"] = [[] for _ in range(len(P))]
    P["how"] = ""
    idx_of = {inv: i for i, inv in zip(I.index, I["invoice_no"])}

    def apply(pi, ii, amt, how):
        I.at[ii, "paid_true"] += amt
        I.at[ii, "paid_by"].append(P.at[pi, "payment_id"])
        P.at[pi, "left"] -= amt
        P.at[pi, "applied_to"].append(I.at[ii, "invoice_no"])
        P.at[pi, "how"] = how

    def bal(ii):
        return round(I.at[ii, "amount_num"] - I.at[ii, "paid_true"], 2)

    # 1. payments that name an invoice
    for pi, r in P.iterrows():
        m = re.search(r"INV-\d+", str(r["reference"]))
        if m and m.group(0) in idx_of:
            ii = idx_of[m.group(0)]
            amt = min(r["amount_num"], max(bal(ii), 0))
            apply(pi, ii, amt, "reference")  # anything left over stays as credit (e.g. paid twice)

    # 2. payments that name a month ("Bluecat Jul invoices", "august sessions")
    for pi, r in P.iterrows():
        if P.at[pi, "how"]:
            continue
        ref = str(r["reference"]).lower()
        mon = next((n for k, n in MONTHS.items() if re.search(rf"\b{k}", ref)), None)
        if not mon:
            continue
        cands = I[live & (I["acct"] == r["acct"]) & (I["issued_dt"].dt.month == mon)].sort_values("issued_dt")
        if "session" in ref and len(cands) and "booking_ref" in cands:
            pass
        for ii in cands.index:
            if P.at[pi, "left"] <= 0:
                break
            amt = min(P.at[pi, "left"], max(bal(ii), 0))
            if amt > 0:
                apply(pi, ii, amt, f"month in note ({ref})")

    # 3. payments with no usable reference: same payer, open invoice issued on/before the payment.
    #    Prefer invoices the tool already marks paid but that have no money yet, then the
    #    invoice with the shortest gap (clients usually pay a few days after the bill).
    for pi, r in P.sort_values("date_dt").iterrows():
        if P.at[pi, "how"]:
            continue
        cands = I[live & (I["acct"] == r["acct"]) & (I["issued_dt"] <= r["date_dt"] + pd.Timedelta(days=1))].copy()
        cands["bal"] = [bal(ii) for ii in cands.index]
        cands = cands[cands["bal"] > 0]
        if not len(cands):
            continue
        exact = cands[(cands["bal"] - r["amount_num"]).abs() < 0.01]
        pool = exact if len(exact) else cands
        # invoices the tool marks paid but that have no money yet: the closest in date;
        # all others: oldest first (normal accounting)
        tp = pool["status"].str.lower().eq("paid")
        gap = (r["date_dt"] - pool["issued_dt"]).dt.days.abs()
        pool = pool.assign(rank1=(~tp).astype(int),
                           rank2=[g if t else (d - pd.Timestamp("2000-01-01")).days for g, t, d in zip(gap, tp, pool["issued_dt"])])
        pool = pool.sort_values(["rank1", "rank2"])
        for ii in pool.index:
            if P.at[pi, "left"] <= 0:
                break
            amt = min(P.at[pi, "left"], bal(ii))
            apply(pi, ii, amt, "matched by payer + amount + date")

    I["balance"] = [bal(ii) if live[ii] else 0.0 for ii in I.index]
    I["truth"] = ["void" if not live[ii] else ("paid" if I.at[ii, "balance"] <= 0.005 else
                  ("partial" if I.at[ii, "paid_true"] > 0 else "unpaid")) for ii in I.index]
    I["days_late"] = [(today - d).days if b > 0 and pd.notna(d) and d < today else 0
                      for d, b in zip(I["due_dt"], I["balance"])]
    return I, P


def findings(r, today=TODAY):
    """All money answers from a built dataset."""
    B, I, P = r["bookings"], r["invoices"], r["payments"]
    names = dict(zip(r["clients"]["client_id"], r["clients"]["client"]))
    I, P = allocate(I, P, today)

    def nm(c):
        return names.get(c, c or "?")

    # overdue
    od = I[(I["balance"] > 0) & (I["days_late"] > 0)].sort_values("days_late", ascending=False)
    overdue = [dict(client=nm(c), invoice=inv, amount_owed=round(b, 2), days_late=int(d),
                    tool_says=s, client_id=c)
               for c, inv, b, d, s in zip(od["client_id"], od["invoice_no"], od["balance"], od["days_late"], od["status"])]

    # billing tool wrong
    wrong = []
    for _, x in I.iterrows():
        tool, truth = x["status"].lower(), x["truth"]
        if tool == "void":
            continue
        if tool != truth:
            if truth == "paid":
                how = f"Bank shows payment {', '.join(x['paid_by'])} for it"
            elif truth == "unpaid":
                how = "No payment in the bank matches it (every payment from this client is already matched to another invoice)"
            else:
                how = f"Only ${x['paid_true']:,.0f} received ({', '.join(x['paid_by'])})"
            wrong.append(dict(invoice=x["invoice_no"], client=nm(x["client_id"]), tool_says=tool, truth=truth,
                              how_you_know=how, client_id=x["client_id"]))
    # invoices for cancelled bookings, and duplicate invoices for one booking
    canc = set(B.loc[B["status"] == "cancelled", "booking_id"])
    for _, x in I[I["booking_ref"].isin(canc) & (I["status"].str.lower() != "void")].iterrows():
        wrong.append(dict(invoice=x["invoice_no"], client=nm(x["client_id"]), tool_says=f"bill for {x['booking_ref']}",
                          truth=f"{x['booking_ref']} was cancelled", how_you_know="bookings.csv marks the shoot cancelled",
                          client_id=x["client_id"]))

    # credits: money received that no invoice needs (paid twice, etc.)
    credits = []
    for _, p in P[P["left"] > 0.005].iterrows():
        credits.append(dict(client=nm(p["client_id"]) if p["client_id"] else p["payer"], amount=round(p["left"], 2),
                            why=f"{p['payment_id']} ({p['date_dt']:%b %d}) is extra: {', '.join(p['applied_to']) or 'no invoice'} was already paid",
                            client_id=p["client_id"]))
    # paid invoices for cancelled shoots are money we hold for the client
    for _, x in I[I["booking_ref"].isin(canc) & (I["paid_true"] > 0)].iterrows():
        credits.append(dict(client=nm(x["client_id"]), amount=round(x["paid_true"], 2),
                            why=f"Paid {x['invoice_no']} for shoot {x['booking_ref']}, which was cancelled",
                            client_id=x["client_id"]))

    # never billed: completed paid-for work with no invoice
    billed = set(I.loc[I["status"].str.lower() != "void", "booking_ref"].dropna())
    nb = B[(B["status"] == "completed") & (~B["booking_id"].isin(billed)) & (B["price_num"] > 0)]
    # a shoot that replaced a cancelled, already-paid one is not unbilled
    paid_canc = I[I["booking_ref"].isin(canc) & (I["paid_true"] > 0)]
    covered = set()
    for _, x in paid_canc.iterrows():
        later = nb[(nb["client_id"] == x["client_id"])]
        if len(later):
            covered.add(later.sort_values("date_dt")["booking_id"].iloc[0])
    never = [dict(client=nm(c), booking_id_or_month=b, amount=p, client_id=c,
                  why=f"{s} on {d:%b %d} is completed but has no invoice")
             for c, b, p, s, d in zip(nb["client_id"], nb["booking_id"], nb["price_num"], nb["service"], nb["date_dt"])
             if b not in covered]

    # retainers: months with a retainer shoot but no retainer invoice, or billed below the usual amount
    ret = I[I["memo"].str.contains("retainer", case=False, na=False) & (I["status"].str.lower() != "void")].copy()
    ret["month"] = ret["issued_dt"].dt.to_period("M")
    for c, g in ret.groupby("client_id"):
        usual = g["amount_num"].mode().iloc[0]
        shoots = B[(B["client_id"] == c) & B["service"].str.contains("retainer", case=False) & (B["status"] == "completed")]
        for mon in sorted(set(shoots["date_dt"].dt.to_period("M"))):
            row = g[g["month"] == mon]
            if not len(row):
                never.append(dict(client=nm(c), booking_id_or_month=str(mon), amount=float(usual), client_id=c,
                                  why=f"Retainer shoot in {mon.strftime("%B")} but no {mon.strftime("%B")} retainer invoice"))
            elif row["amount_num"].sum() < usual:
                short = float(usual - row["amount_num"].sum())
                never.append(dict(client=nm(c), booking_id_or_month=str(mon), amount=short, client_id=c,
                                  why=f"{mon.strftime("%B")} retainer billed ${row['amount_num'].sum():,.0f}, usual is ${usual:,.0f}"))
    return dict(invoices=I, payments=P, overdue=overdue, wrong=wrong, credits=credits, never=never)
