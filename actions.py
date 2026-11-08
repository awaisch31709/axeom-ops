"""Turn the findings into a short list of suggested actions for the owner. Nothing is sent from here."""
import re

import pandas as pd

import money
import edits as rq

CHURN = r"whether we continue|talk about (?:our|the) contract|we'll pause|we will pause|cancel (?:our|the) (?:contract|retainer)|not happy|too many rounds"
UPSELL = r"what would that cost|how much would that be|we'd pay|every month\?|package where"


def first_name(name):
    return str(name).split()[0] if str(name).strip() else "there"


def contact_for(r, cid):
    L = r["leads"]
    rows = L[(L["client_id"] == cid) & (L["status"] == "Won")]
    rows = rows if len(rows) else L[L["client_id"] == cid]
    return rows["name"].iloc[0] if len(rows) else ""


def build_actions(r, today, labels=None):
    """labels: optional {msg_id: category} from Jev; used together with the keyword rules."""
    labels = labels or {}
    names = dict(zip(r["clients"]["client_id"], r["clients"]["client"]))
    f = money.findings(r, today)
    M, R = rq.link_requests(r)
    acts = []

    def add(key, score, kind, cid, title, detail, ask, draft=""):
        acts.append(dict(key=key, score=score, kind=kind, client_id=cid, client=names.get(cid, cid or ""),
                         title=title, detail=detail, ask=ask, draft=draft))

    msgs = M.copy()
    outgoing = r["messages"][r["messages"]["direction"] == "to"]

    def answered(cid, after):
        o = outgoing[(outgoing["client_id"] == cid) & (outgoing["ts"] > after)]
        return len(o) > 0

    # 1. a client talking about leaving
    by_jev = msgs["msg_id"].map(labels).fillna("")
    churn = msgs[(msgs["text"].str.contains(CHURN, case=False, regex=True) | by_jev.eq("wants_to_leave"))
                 & msgs["client_id"].notna()]
    for cid, g in churn.groupby("client_id"):
        last = g.sort_values("ts").iloc[-1]
        if answered(cid, last["ts"]):
            continue
        active = (r["bookings"]["client_id"] == cid) & (r["bookings"]["date_dt"] >= today - pd.Timedelta(days=45))
        if not active.any():
            continue  # already gone
        days = (today - last["ts"]).days
        people = [p for p in g.sort_values("ts")["person"] if p]
        lead_by_email = r["leads"][r["leads"]["email_n"] == last["email"]]["name"]
        person = (people[-1] if people else "") or (lead_by_email.iloc[0] if len(lead_by_email) else contact_for(r, cid))
        add(f"churn:{cid}", 100 + len(g), "Client may leave", cid,
            f"{names[cid]} may leave. No reply in {days} day{'s' if days != 1 else ''}.",
            " / ".join(f"{x.ts:%b %d}: \"{x.text[:90]}\"" for x in g.sort_values('ts').itertuples()),
            "Call them today?",
            f"Hi {first_name(person)}, this is Zaid. I saw your message about the contract and I'm sorry we were slow. "
            f"Can I call you today? I want to fix what went wrong.")

    # 2. edit requests that never reached the editors
    req = R[(R["kind"] != "follow_up") & (~R["on_board"]) & R["client_id"].notna()]
    B = r["bookings"]
    recent = set(B.loc[B["date_dt"] >= today - pd.Timedelta(days=45), "client_id"].dropna())
    # an editor whose cards never carry client notes
    PB = r["board"]
    for ed, g in PB.groupby("editor"):
        lost_ed = req[req["editor"] == ed]
        if len(g) >= 20 and g["notes_on_board"].fillna("").eq("").all() and len(lost_ed) >= 3:
            add(f"editor:{ed}", 95, "Edits missed", None,
                f"{len(lost_ed)} client edit requests never reached {ed}. None of {ed}'s {len(g)} videos has a client note.",
                f"{ed} edits the videos, but client requests are never written on {ed}'s cards. "
                f"Clients with missed edits: " + ", ".join(sorted({names.get(c, '') for c in lost_ed['client_id']})),
                f"Meet {ed} today and go through the list?")
    fu = R[R["kind"] == "follow_up"].groupby("client_id").size()
    for cid, g in req.groupby("client_id"):
        n_fu = int(fu.get(cid, 0))
        lines = [f"- {x.title or 'video not named'} ({x.video_ids or '?'}): {x.text[:80]}" for x in g.itertuples()]
        add(f"edits:{cid}", 50 + 4 * len(g) + 6 * n_fu - (0 if cid in recent else 35), "Edits missed", cid,
            f"{names[cid]}: {len(g)} edit request{'s' if len(g) > 1 else ''} never reached the editor"
            + (f", {n_fu} complaint{'s' if n_fu > 1 else ''} about it." if n_fu else "."),
            "\n".join(lines), "Put them on the board?",
            "Editor notes:\n" + "\n".join(lines))

    # 3. overdue money
    for x in f["overdue"]:
        person = contact_for(r, x["client_id"])
        tool = " (billing tool wrongly says paid)" if x["tool_says"] == "paid" else ""
        add(f"overdue:{x['invoice']}", 40 + x["days_late"] / 2 + x["amount_owed"] / 200, "Money owed to us", x["client_id"],
            f"{x['client']} owes ${x['amount_owed']:,.0f}. {x['invoice']} is {x['days_late']} days late{tool}.",
            "", "Send a reminder?",
            f"Hi {first_name(person)}, a quick reminder that invoice {x['invoice']} (${x['amount_owed']:,.0f}) is still open. "
            f"Could you take care of it this week? Thank you! - Zaid, Axeom")

    # 4. work never billed
    for x in f["never"]:
        add(f"unbilled:{x['client_id']}:{x['booking_id_or_month']}", 35 + x["amount"] / 200, "Never billed", x["client_id"],
            f"{x['client']}: ${x['amount']:,.0f} never billed ({x['booking_id_or_month']}).", x["why"], "Create the invoice?")

    # 5. money we hold for clients
    for x in f["credits"]:
        add(f"credit:{x['client_id']}:{x['amount']}", 33, "We owe them", x["client_id"],
            f"We owe {x['client']} ${x['amount']:,.0f}.", x["why"], "Refund or credit it?")

    # 6. billing tool says the wrong thing
    for x in f["wrong"]:
        add(f"tool:{x['invoice']}", 15, "Fix billing tool", x["client_id"],
            f"{x['invoice']} ({x['client']}): tool says {x['tool_says']}, really {x['truth']}.", x["how_you_know"],
            f"Mark it {x['truth']} in the billing tool?")

    # 7. people who asked and got no reply
    L = r["leads"]
    junk = L["name"].str.contains(r"^(?:test|asdf)|crypto|seo services", case=False) | \
        L["email"].str.contains(r"test@|a@a\.co|coin-x|rank1seo", case=False)
    for _, x in L[L["first_reply_dt"].isna() & ~junk].iterrows():
        days = (today - x["created_dt"]).days
        if days < 1:
            continue
        existing = isinstance(x["client_id"], str)
        who = f"{x['name']}" + (f" ({x['business']})" if x["business"] else "")
        add(f"lead:{x['lead_id']}", (45 if existing else 22) - min(days, 60) / 10, "No reply yet", x["client_id"] if existing else None,
            f"{who} {'(existing client) ' if existing else ''}asked {days} days ago. Nobody replied.",
            f"{x['lead_id']} - {x['source']} - {x['email']} {x['phone']}", "Reply now?",
            f"Hi {first_name(x['name'])}, thanks for reaching out to Axeom, and sorry for the slow reply. When is a good time for a quick call?")

    # 8. requests to buy more, and referrals mentioned in messages
    for _, x in msgs[msgs["text"].str.contains(UPSELL, case=False, regex=True) | by_jev.eq("wants_more_work")].iterrows():
        if x["client_id"] and not answered(x["client_id"], x["ts"]):
            add(f"upsell:{x['msg_id']}", 30, "Wants to buy more", x["client_id"],
                f"{names.get(x['client_id'], '')} asked to buy more ({x['ts']:%b %d}). No reply.",
                x["text"], "Send them a price?")
    known_phones = set(L["phone_n"])
    for _, x in msgs.iterrows():
        for ph in re.findall(r"\b(\d{3}-\d{3}-\d{4})\b", x["text"]):
            p = re.sub(r"\D", "", ph)
            if p not in known_phones and "number is" in x["text"].lower() and "our new" not in x["text"].lower():
                add(f"referral:{x['msg_id']}", 32, "Referral waiting", x["client_id"],
                    f"Referral from {names.get(x['client_id'], x['person'])}: not in leads yet.", x["text"],
                    "Add as a lead and call?")
    acts.sort(key=lambda a: -a["score"])
    return acts, f, R
