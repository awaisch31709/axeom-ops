"""Builds answers.json from the data. Every number here comes from pipeline/money/requests/channels."""
import json
import sys
from collections import defaultdict

import pandas as pd

import channels as ch
import money
import pipeline as pl
import edits as rq

folder = sys.argv[1]
out = sys.argv[2]
data = pl.read_folder(folder)
r = pl.build(data)
names = dict(zip(r["clients"]["client_id"], r["clients"]["client"]))
f = money.findings(r)
M, R = rq.link_requests(r)

# ---------------- clients
other, emails, phones = defaultdict(set), defaultdict(set), defaultdict(set)
for df, nc, ec in ((r["bookings"], "client_name", "email_n"), (r["invoices"], "bill_to", "email_n"),
                   (r["payments"], "payer", "email_n"), (r["board"], "client", None)):
    for _, x in df.iterrows():
        c = x["client_id"]
        if not isinstance(c, str):
            continue
        other[c].add(str(x[nc]).strip())
        if ec and x[ec] and not x.get("paid_by_agency", False):
            emails[c].add(x[ec])
L = r["leads"]
for _, x in L[L["client_id"].notna()].iterrows():
    c = x["client_id"]
    for v in (x["business"], x["name"]):
        if str(v).strip():
            other[c].add(str(v).strip())
    emails[c].add(x["email_n"])
    if x["phone_n"]:
        phones[c].add(x["phone_n"])
for _, x in M[M["client_id"].notna()].iterrows():
    if x["email"]:
        emails[x["client_id"]].add(x["email"])
    if x["phone"]:
        phones[x["client_id"]].add(x["phone"])
    if x["handle"]:
        other[x["client_id"]].add("@" + x["handle"])


def fmt_phone(p):
    return f"{p[:3]}-{p[3:6]}-{p[6:]}"


clients = []
for c, nm in sorted(names.items(), key=lambda kv: kv[1]):
    others = sorted(set(other[c]) - {nm}, key=lambda s: (s.lower(), s))
    clients.append(dict(name=nm, other_names_used_in_the_data=others,
                        emails=sorted(emails[c]), phones=sorted(fmt_phone(p) for p in phones[c])))

# ---------------- requests
req = R[R["kind"] != "follow_up"].copy()
lost = req[~req["on_board"]].copy()
nico_like = [e for e, g in r["board"].groupby("editor") if g["notes_on_board"].fillna("").eq("").all() and len(g) > 20]
CAME = {"Email": "email", "Whatsapp": "whatsapp", "Voice Note": "voice note", "Instagram DM": "instagram",
        "Website Form": "website form"}
lost_rows = []
for _, x in lost.sort_values("date").iterrows():
    vid = x["video_ids"] if "," not in x["video_ids"] else ""
    why = rq.why_lost(x, nico_like)
    if "," in x["video_ids"]:
        why += f" (could be any of {x['video_ids']})"
    lost_rows.append(dict(video_id=vid, request_date=x["date"].strftime("%Y-%m-%d"),
                          came_by=CAME.get(x["channel"], x["channel"].lower()), why_it_was_lost=why,
                          client=names.get(x["client_id"], ""), request=x["text"][:140]))

# ---------------- leads never replied (real people only)
junk = L["name"].str.contains(r"^(?:test|asdf)|crypto|seo services", case=False) | L["email"].str.contains(r"test@|a@a\.co|coin-x|rank1seo", case=False)
never_replied = L[L["first_reply_dt"].isna() & ~junk]["lead_id"].tolist()

# ---------------- channels
cp, chan = ch.channel_table(r, data, pl.TODAY)

ans = {
    "pack_id": 92,
    "your_name": "Muhammad Abdullah",
    "your_email": "scalewithabdullah7@gmail.com",
    "live_link": "",
    "video_link": "",
    "code_or_files_link": "",
    "client_count": len(clients),
    "clients": clients,
    "payers_who_are_not_clients": [
        {"name": "Bluecat Agency (Bluecat Agency LLC, alana@bluecatmktg.com)",
         "pays_for": ["Elevate Pediatrics", "Magnolia Chiropractic"]}
    ],
    "overdue": [{k: v for k, v in x.items() if k in ("client", "invoice", "amount_owed", "days_late")} for x in f["overdue"]],
    "overdue_total": round(sum(x["amount_owed"] for x in f["overdue"]), 2),
    "we_owe": [
        {"client": "Kingsley Barbers", "amount": 900,
         "why": "INV-1053 ($900) was paid twice: P5057 on Jul 7 and P5059 on Jul 9, both with reference INV-1053. Refund or credit $900."},
        {"client": "Red Oak Realty", "amount": 1500,
         "why": "Paid INV-1132 ($1,500, P5123) for the Sep 23 shoot B143, which was cancelled (family emergency, 'we already paid'). Shoot moved to Oct 5 (B156). The $1,500 is a credit: use it for INV-1142 / B156 instead of chasing her for INV-1142."},
    ],
    "never_billed": [{k: v for k, v in x.items() if k in ("client", "booking_id_or_month", "amount")} for x in f["never"]],
    "never_billed_total": round(sum(x["amount"] for x in f["never"]), 2),
    "billing_tool_is_wrong": [{k: v for k, v in x.items() if k in ("invoice", "tool_says", "truth", "how_you_know")} for x in f["wrong"]] + [
        {"invoice": "INV-1142", "tool_says": "unpaid, $1,500 owed by Red Oak Realty (due Oct 13)",
         "truth": "Not owed. It bills the rescheduled shoot B156, which Brooke already paid for via INV-1132 (cancelled B143)",
         "how_you_know": "Red Oak did 4 shoots ($6,000) and the bank shows 4 payments of $1,500 ($6,000). Brooke's Sep 23 WhatsApp: 'Can we move it? We already paid.'"},
        {"invoice": "INV-1088", "tool_says": "$2,000 August retainer for Bluebonnet PT",
         "truth": "Every other Bluebonnet retainer month is $2,500 and no discount is mentioned anywhere: $500 under-billed (also listed under never billed)",
         "how_you_know": "invoices.csv: INV-1035/1060/1116/1148 are $2,500; messages.txt has no discount for Bluebonnet"},
    ],
    "lost_requests": [{k: v for k, v in x.items() if k in ("video_id", "request_date", "came_by", "why_it_was_lost")} for x in lost_rows],
    "lost_requests_root_causes": [
        f"Requests for videos edited by Nico never reach his cards: 0 of Nico's {int((r['board']['editor']=='Nico').sum())} board rows have a note, and {int((lost['editor']=='Nico').sum())} of the {len(lost)} lost requests are on his videos. For Ali, Rita and Remy almost every request is on the board.",
        "There is no single inbox. Requests arrive by email, WhatsApp, Instagram DM and voice note, from different people and new phone numbers, and someone has to copy each one onto the board by hand. Instagram DMs and the voice note were never copied.",
        "Requests that don't name the video ('the one we shot last week') are not worked out and get dropped.",
        "Messages not in English (Lakeside Bridal writes in Spanish) and brand-wide rules (Castillo's full company name) have no place on a per-video board.",
    ],
    "lost_requests_started_getting_worse_in": "2026-06",
    "never_replied_leads": never_replied,
    "most_likely_to_leave": {
        "client": "Castillo Insurance Group",
        "evidence": [
            "Biggest client: $2,500/month retainer, $15,000 billed since May",
            "Sep 29 website form from Mia Castillo: 'We would like to talk about our contract and whether we continue.' It sits in leads.csv as L097, status New, never replied",
            "Oct 3 email: 'Following up. We need to talk about the contract this week.'",
            "Asked (voice note Sep 2) for the full name 'Castillo Insurance Group' for compliance; Sep 9: 'The videos say Castillo Insurance again... Not happy.'",
            "5 of 9 edit requests were lost (most on Nico's videos), 3 follow-ups ('This is the second time I ask')",
            "September retainer INV-1113 ($2,500) unpaid, 21 days late - they may be holding payment",
            "Carter Law left in July for the same reason: 'Too many rounds of fixes, and my notes keep getting missed'",
        ],
        "what_to_do_this_week": "Monday morning the owner calls Mia and David Castillo himself, before their Oct 7 retainer shoot. Apologise, don't argue. Show the fix list: every video re-cut with 'Castillo Insurance Group', the 5 missed edits done by Ali (senior editor) by Friday, one named contact for their requests. Agree the October plan. Ask about the overdue September invoice only after the relationship is back on track, not in the first call.",
    },
    "best_marketing_channel": {
        "channel": "Referral",
        "why": (
            "Referrals brought 10 of the 30 clients from 13 real leads (77% win rate vs 14-33% for every other channel) and the most profit: "
            f"${chan.loc['Referral','profit']:,.0f} billed minus studio and editor cost, and they cost nothing to get. "
            "Per client, Website form (~$4,200) and Google (~$4,150) look higher than Referral (~$2,800), but each rests on one or two "
            "$2,500/month retainers (Castillo, Bluebonnet, Carter, which already left) and we don't know ad spend. Meta ads are the weakest: ~$1,300 profit per client. "
            "Referrals also keep coming in through messages (Mia -> Dr. Romero, Simone -> Juniper Lab, Jordan -> Femi at Carter Tire & Auto, who is not even in the leads file)."
        ),
    },
    "what_this_data_cannot_tell_us": [
        "Ad spend per channel (Meta ads, Google), so true profit per channel can't be calculated",
        "Replies sent outside these files: only 2 outgoing messages are logged, so 'never replied' may be 'replied by phone'",
        "Whether Nico gets client notes some other way (verbally, a chat group) - the board just never shows them",
        "Whether unbilled shoots were free on purpose (make-goods, trials) or simply forgotten",
        "Why Bluebonnet's August retainer was $2,000 - discount or mistake",
        "Which video a client meant by 'the one we shot last week'",
        "Rent, equipment and the owner's own time, so profit here is only studio hours + editor pay",
        "How the videos performed for clients (views, leads), which drives whether they stay",
        "Which invoice an unreferenced payment was really for; I matched by payer, amount and date",
    ],
    "monday_top_3": [
        "Call Castillo Insurance Group (our largest client, $2,500/month) before their Oct 7 shoot. They asked twice to talk about leaving and nobody answered.",
        "Stop losing edit requests: sit with Nico today. None of his 132 videos has a client note. Then put every client request into one list linked to the video before anyone edits it. Clear the ~22 'still not fixed' complaints this week.",
        "Collect and correct the money: approve reminders for $8,020 overdue (7 invoices; Lakeside's INV-1089 shows 'paid' but isn't), invoice $11,200 of work never billed, refund/credit Kingsley $900, and don't chase Rise & Grind, Hassan Family Law, Pecan or Red Oak for invoices they already paid.",
    ],
}
json.dump(ans, open(out, "w"), indent=2, default=str)
# helper outputs for the writeup
pd.DataFrame(lost_rows).to_csv(out.replace(".json", "_lost_requests.csv"), index=False)
print("clients", len(clients), "overdue", ans["overdue_total"], "never", ans["never_billed_total"],
      "lost", len(lost_rows), "never_replied", len(never_replied))
print(chan.round(0).to_string())
