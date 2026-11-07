"""Axeom data pipeline: read the 8 files, clean them, link every record to one client.

Used by both the analysis scripts and the Streamlit app, so the numbers in
answers.json and the numbers on screen come from the same code.
"""
import io
import re
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

TODAY = pd.Timestamp("2026-10-06")

# ---------------------------------------------------------------- reading

FILES = ["leads", "bookings", "invoices", "payments", "production_board", "team", "costs"]


def read_folder(folder):
    """Read a pack folder into {name: DataFrame or str}."""
    folder = Path(folder)
    out = {}
    for f in FILES:
        p = folder / f"{f}.csv"
        if p.exists():
            out[f] = pd.read_csv(p, dtype=str, keep_default_na=False)
    m = folder / "messages.txt"
    if m.exists():
        out["messages"] = m.read_text(encoding="utf-8", errors="replace")
    return out


def guess_kind(filename, content_bytes):
    """Work out which file an upload is, from its name or its header row."""
    name = filename.lower()
    if name.endswith(".txt"):
        return "messages"
    head = content_bytes[:400].decode("utf-8", errors="replace").lower().splitlines()[0] if content_bytes else ""
    signatures = {
        "leads": "lead_id", "bookings": "booking_id", "invoices": "invoice_no",
        "payments": "payment_id", "production_board": "video_id", "team": "role",
        "costs": "item,cost",
    }
    for kind, sig in signatures.items():
        if sig in head:
            return kind
    for kind in signatures:
        if kind.replace("_", "") in name.replace("_", "").replace(" ", ""):
            return kind
    return None


# ---------------------------------------------------------------- cleaning

def money(x):
    s = str(x).replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_dates(series, default="mdy"):
    """Parse a column of dates that may be ISO, MM/DD/YYYY or DD/MM/YYYY.

    The order is decided for the whole column: if any value has a first part
    > 12 it must be DD/MM; if any has a second part > 12 it must be MM/DD.
    """
    s = series.astype(str).str.strip()
    slash = s[s.str.match(r"^\d{1,2}/\d{1,2}/\d{4}$")]
    order = default
    if len(slash):
        a = slash.str.split("/").str[0].astype(int)
        b = slash.str.split("/").str[1].astype(int)
        if (a > 12).any():
            order = "dmy"
        elif (b > 12).any():
            order = "mdy"
    def one(v):
        if re.match(r"^\d{4}-\d{2}-\d{2}", v):
            return pd.to_datetime(v[:10], errors="coerce")
        if re.match(r"^\d{1,2}/\d{1,2}/\d{4}$", v):
            return pd.to_datetime(v, format="%d/%m/%Y" if order == "dmy" else "%m/%d/%Y", errors="coerce")
        return pd.to_datetime(v, errors="coerce")
    return s.map(one), order


STOP = {"llc", "pllc", "inc", "co", "the", "dds", "dr", "tx", "dfw", "group", "and", "company"}


def norm_name(s):
    s = re.sub(r"\s+-\s+.*$", "", str(s))  # "Clearwater Med Spa - Frisco" -> location dropped
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    words = [w for w in s.split() if w not in STOP]
    return " ".join(words)


def norm_phone(s):
    d = re.sub(r"\D", "", str(s))
    return d[-10:] if len(d) >= 10 else ""


def norm_email(s):
    s = str(s).strip().lower()
    return s if "@" in s else ""


# ---------------------------------------------------------------- messages

MSG_RE = re.compile(
    r"^\[(?P<ts>[\d\- :]+)\]\s+(?P<channel>EMAIL|WHATSAPP|VOICE NOTE|INSTAGRAM DM|WEBSITE FORM|SMS)\s+"
    r"(?P<dir>from|to)\s+(?P<who>.+?)(?::\s|,\s*\d+:\d+\.\s*Transcript:\s*)(?P<text>.*)$"
)


def parse_messages(text):
    rows = []
    for i, line in enumerate(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        m = MSG_RE.match(line)
        if not m:
            rows.append(dict(msg_id=f"M{i+1:03d}", ts=None, channel="UNKNOWN", direction="from",
                             who=line, text=line, email="", phone="", handle="", person=""))
            continue
        who = m.group("who")
        email = norm_email(re.search(r"\S+@\S+", who).group(0)) if re.search(r"\S+@\S+", who) else ""
        phone_m = re.search(r"\+?1?\s?(\d{3}-\d{3}-\d{4})", who)
        handle_m = re.search(r"@([a-z0-9_.]+)", who) if not email else None
        person_m = re.search(r"\(([^)]+)\)", who)
        text_ = m.group("text").strip().strip('"')
        rows.append(dict(
            msg_id=f"M{i+1:03d}",
            ts=pd.to_datetime(m.group("ts"), errors="coerce"),
            channel=m.group("channel").title().replace("Dm", "DM"),
            direction=m.group("dir"),
            who=who, text=text_,
            email=email,
            phone=norm_phone(phone_m.group(1)) if phone_m else "",
            handle=handle_m.group(1) if handle_m else "",
            person=person_m.group(1).split(",")[0].strip() if person_m else "",
        ))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- linking

class UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


AGENCY_WORDS = ("agency", "mktg", "marketing")


def build(data, manual_links=None):
    """Clean everything and link all records to clients. Returns a dict of DataFrames."""
    d = {k: (v.copy() if isinstance(v, pd.DataFrame) else v) for k, v in data.items()}
    for k in ["leads", "bookings", "invoices", "payments", "production_board"]:
        d.setdefault(k, pd.DataFrame())
    d.setdefault("messages", "")

    L, B, I, P, PB = d["leads"], d["bookings"], d["invoices"], d["payments"], d["production_board"]
    M = parse_messages(d["messages"])

    # --- clean columns
    if len(B):
        B["date_dt"], _ = parse_dates(B["date"], "mdy")
        B["price_num"] = B["price"].map(money)
        B["email_n"] = B["email"].map(norm_email)
    if len(I):
        I["issued_dt"], _ = parse_dates(I["issued"])
        I["due_dt"], _ = parse_dates(I["due"])
        I["amount_num"] = I["amount"].map(money)
        I["email_n"] = I["email"].map(norm_email)
        I["memo"] = I["memo"].fillna("")
    if len(P):
        P["date_dt"], P.attrs["date_order"] = parse_dates(P["date"], "dmy")
        P["amount_num"] = P["amount"].map(money)
        P["email_n"] = P["payer_email"].map(norm_email)
    if len(L):
        L["created_dt"] = pd.to_datetime(L["created"], errors="coerce")
        L["first_reply_dt"] = pd.to_datetime(L["first_reply"].replace("", None), errors="coerce")
        L["email_n"] = L["email"].map(norm_email)
        L["phone_n"] = L["phone"].map(norm_phone)

    # --- union-find over identities: email, phone, booking id, normalized names
    uf = UF()
    name_votes = defaultdict(Counter)  # node -> names seen

    def node_e(e): return f"e:{e}"
    def node_p(p): return f"p:{p}"
    def node_n(n): return f"n:{n}"

    agency_emails = set()
    for e in pd.concat([B.get("email_n", pd.Series(dtype=str)), I.get("email_n", pd.Series(dtype=str)),
                        P.get("email_n", pd.Series(dtype=str)), L.get("email_n", pd.Series(dtype=str))]):
        if any(w in str(e) for w in AGENCY_WORDS):
            agency_emails.add(e)
    if len(L):
        for _, r in L.iterrows():
            if any(w in str(r.get("business", "")).lower() for w in ("agency",)):
                agency_emails.add(r["email_n"])

    # Same email = same client (except agency emails, which pay for several clients).
    # Phone from a WON lead = same client.
    if len(L):
        for _, r in L.iterrows():
            if not r["email_n"]:
                continue
            uf.find(node_e(r["email_n"]))
            if r["phone_n"] and r["email_n"] not in agency_emails:
                uf.union(node_e(r["email_n"]), node_p(r["phone_n"]))
            if r.get("business") and r["email_n"] not in agency_emails:
                uf.union(node_e(r["email_n"]), node_n(norm_name(r["business"])))
                w = 5 if r.get("status") == "Won" else 1
                name_votes[node_e(r["email_n"])][re.sub(r"\s+-\s+.*$", "", r["business"])] += w
        # a person's full name is an alias only if that name belongs to exactly one email
        person_emails = defaultdict(set)
        for _, r in L.iterrows():
            if r["email_n"] and r["email_n"] not in agency_emails:
                person_emails[norm_name(r["name"])].add(r["email_n"])
        for pn, es in person_emails.items():
            if len(es) == 1 and len(pn.split()) >= 2:
                uf.union(node_e(next(iter(es))), node_n(pn))

    # Names used with a (non-agency) email in bookings / invoices / payments become aliases of that email.
    # words that identify an agency (e.g. "bluecat"), so "Bluecat - Elevate" is not an alias of Bluecat
    agency_tokens = {e.split("@")[1].split(".")[0].replace("mktg", "").replace("agency", "") for e in agency_emails}
    agency_tokens.discard("")

    def add_names(df, name_col, email_col="email_n"):
        if not len(df):
            return
        for _, r in df.iterrows():
            e, nm = r[email_col], r[name_col]
            if not e or e in agency_emails or not str(nm).strip():
                continue
            if any(t in str(nm).lower() for t in agency_tokens):
                continue
            uf.union(node_e(e), node_n(norm_name(nm)))
            name_votes[node_e(e)][nm] += 1

    add_names(B, "client_name")
    add_names(I, "bill_to")
    add_names(P, "payer")

    # Typo emails: same mailbox, TLD typo (.con vs .com) -> link
    emails = {k[2:] for k in uf.p if k.startswith("e:")}
    for e in emails:
        fixed = re.sub(r"\.con$", ".com", e)
        if fixed != e and fixed in emails:
            uf.union(node_e(fixed), node_e(e))

    # Same company web domain = same client (never for free-mail domains or agencies)
    FREE = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "aol.com"}
    by_dom = defaultdict(list)
    for e in emails:
        dom = e.split("@")[-1]
        if dom not in FREE and e not in agency_emails:
            by_dom[dom].append(e)
    for dom, es in by_dom.items():
        for e in es[1:]:
            uf.union(node_e(es[0]), node_e(e))

    # Messages that announce a new email / phone
    for _, r in M.iterrows():
        t = r["text"]
        me = re.search(r"new email is (\S+@\S+?)[.!]?(?:\s|$)", t, re.I)
        if me and r["email"]:
            uf.union(node_e(r["email"]), node_e(norm_email(me.group(1))))
        mp = re.search(r"new (?:office |phone )?number is ([\d\- ]{10,})", t, re.I)
        if mp and r["email"]:
            uf.union(node_e(r["email"]), node_p(norm_phone(mp.group(1))))

    # Leads are kept as people who asked; an existing client's own email in leads links automatically.
    for a, b in (manual_links or []):
        uf.union(a, b)

    # --- clients = clusters that have at least one booking or invoice
    def cid(node):
        return uf.find(node)

    client_nodes = set()
    for df in (B, I):
        if len(df):
            for e in df["email_n"]:
                if e and e not in agency_emails:
                    client_nodes.add(cid(node_e(e)))

    # Records without a usable email (agency bookings, the production board, payments with no email)
    # are matched by name: exact normalized name first, then "all words of the name appear in
    # exactly one client's names", then a person's name from that client's lead.
    def name_candidates(nm, restrict=None):
        key = norm_name(nm)
        toks = set(key.split()) - agency_tokens
        if not toks:
            return set()
        n = node_n(" ".join(w for w in key.split() if w not in agency_tokens))
        if n in uf.p and cid(n) in client_nodes:
            return {cid(n)}
        cands = set()
        for node in list(uf.p):
            if node.startswith("n:") and toks <= set(node[2:].split()):
                root = cid(node)
                if root in client_nodes:
                    cands.add(root)
        if not cands:
            # fall back to a distinctive word: one that appears in only one client's names
            word_clients = defaultdict(set)
            for node in list(uf.p):
                if node.startswith("n:"):
                    root = cid(node)
                    if root in client_nodes:
                        for w in node[2:].split():
                            word_clients[w].add(root)
            for w in toks:
                if len(w) >= 4 and len(word_clients.get(w, ())) == 1:
                    cands |= word_clients[w]
        if restrict and cands & restrict:
            cands &= restrict
        return cands

    def resolve_name(nm, restrict=None):
        c = name_candidates(nm, restrict)
        return next(iter(c)) if len(c) == 1 else None

    person_index = defaultdict(set)
    if len(L):
        for _, r in L.iterrows():
            if r["email_n"] and r["email_n"] not in agency_emails:
                root = cid(node_e(r["email_n"]))
                if root in client_nodes:
                    for w in norm_name(r["name"]).split():
                        person_index[w].add(root)

    # clients known to be handled by an agency: a booking named "<agency> - X" under the client's
    # own email, or the client's name in a message sent from the agency
    agency_clients_pre = set()
    if len(B):
        for _, r in B.iterrows():
            if r["email_n"] not in agency_emails and any(t in str(r["client_name"]).lower() for t in agency_tokens):
                c = cid(node_e(r["email_n"]))
                if c in client_nodes:
                    agency_clients_pre.add(c)

    for _, r in M.iterrows():
        if r["email"] in agency_emails:
            txt = " " + norm_name(r["text"]) + " "
            for node in list(uf.p):
                if node.startswith("n:") and len(node[2:].split()) >= 2 and f" {node[2:]} " in txt:
                    c = cid(node)
                    if c in client_nodes:
                        agency_clients_pre.add(c)

    def resolve_agency_row(nm, restrict=None):
        restrict = restrict or agency_clients_pre
        c = resolve_name(nm, restrict)
        if c:
            return c
        toks = [w for w in norm_name(nm).split() if w not in agency_tokens]
        cands = set()
        for w in toks:
            cands |= person_index.get(w, set())
        cands &= client_nodes
        if restrict and cands & restrict:
            cands &= restrict
        return next(iter(cands)) if len(cands) == 1 else None

    # Canonical client names
    person_names = set()
    if len(L):
        person_names = {norm_name(x) for x in L["name"]}
    latest_name = {}
    for df, nc, dc in ((B, "client_name", "date_dt"), (I, "bill_to", "issued_dt"), (P, "payer", "date_dt")):
        if len(df):
            for e, nm, dt in zip(df["email_n"], df[nc], df[dc]):
                if e and e not in agency_emails and pd.notna(dt):
                    root = cid(node_e(e))
                    if root not in latest_name or dt >= latest_name[root][0]:
                        latest_name[root] = (dt, nm)

    def best_name(root):
        """Most used business name (not a person's name); the newest name wins ties (renames)."""
        votes = Counter()
        for node in uf.p:
            if uf.find(node) == root and node in name_votes:
                votes.update(name_votes[node])
        by_key = defaultdict(lambda: [0, ""])
        for nm, v in votes.items():
            k = norm_name(nm)
            if not k or k in person_names or any(t in nm.lower() for t in agency_tokens):
                continue
            by_key[k][0] += v
            pretty = re.sub(r"\s+-\s+.*$", "", nm)
            if pretty != pretty.upper() and (not by_key[k][1] or v > votes.get(by_key[k][1], 0)):
                by_key[k][1] = pretty
        if root in latest_name:
            k = norm_name(latest_name[root][1])
            if k in by_key:
                by_key[k][0] += 4
        if not by_key:
            return votes.most_common(1)[0][0] if votes else root.split(":", 1)[1]
        # a short name ("Bayou") also counts as a vote for the longer names that contain it
        score = {k: sum(v[0] for k2, v in by_key.items() if set(k2.split()) <= set(k.split())) for k in by_key}
        k = max(by_key, key=lambda k: (score[k], len(k.split())))
        return by_key[k][1] or k.title()

    clients = {root: best_name(root) for root in client_nodes}

    def client_of_email(e):
        if not e:
            return None
        n = node_e(e)
        if n not in uf.p:
            return None
        r = cid(n)
        return r if r in client_nodes else None

    def client_of_phone(p):
        if not p:
            return None
        n = node_p(p)
        if n not in uf.p:
            return None
        r = cid(n)
        return r if r in client_nodes else None

    # --- attach client id to each table
    def tag(df, name_col):
        if not len(df):
            return
        out, how = [], []
        for _, r in df.iterrows():
            e = r.get("email_n", "")
            c = None if e in agency_emails else client_of_email(e)
            h = "email"
            if c is None:
                c = resolve_agency_row(r[name_col])
                h = "name"
            out.append(c)
            how.append(h if c else "unmatched")
        df["client_id"] = out
        df["match_how"] = how
        df["paid_by_agency"] = df["email_n"].isin(agency_emails)

    tag(B, "client_name")
    tag(I, "bill_to")
    tag(P, "payer")
    if len(L):
        L["client_id"] = [client_of_email(e) for e in L["email_n"]]

    # invoice -> booking, from the memo ("B005 Content Day" or "Content Day 4/9")
    if len(I) and len(B):
        agency_clients = set(B.loc[B["paid_by_agency"], "client_id"].dropna()) | set(
            B.loc[B["client_name"].str.lower().map(lambda n: any(t in n for t in agency_tokens)), "client_id"].dropna())
        B["agency_client"] = B["client_id"].isin(agency_clients)

        def candidates(r):
            if r["email_n"] in agency_emails:
                return B["agency_client"]
            return (B["email_n"] == r["email_n"]) | ((B["client_id"] == r["client_id"]) & B["client_id"].notna())
        refs = []
        for _, r in I.iterrows():
            memo = r["memo"]
            ref = None
            mb = re.search(r"\bB\d{3,}\b", memo)
            if mb and mb.group(0) in set(B["booking_id"]):
                ref = mb.group(0)
            else:
                md = re.search(r"(\d{1,2})/(\d{1,2})\b", memo)
                if md:
                    yr = r["issued_dt"].year if pd.notna(r["issued_dt"]) else TODAY.year
                    try:
                        day = pd.Timestamp(yr, int(md.group(1)), int(md.group(2)))
                    except ValueError:
                        day = None
                    if day is not None:
                        same = B[(B["date_dt"] == day) & candidates(r)]
                        svc = [s for s in same["service"] if s.lower() in memo.lower()]
                        if svc:
                            same = same[same["service"].isin(svc)]
                        if len(same) == 1:
                            ref = same["booking_id"].iloc[0]
            refs.append(ref)
        # invoices with no usable memo: the same client's nearest earlier booking with the same price,
        # not already billed (greedy, oldest invoice first)
        used = {x for x in refs if x}
        for idx in I.sort_values("issued_dt").index:
            pos = I.index.get_loc(idx)
            if refs[pos]:
                continue
            r = I.loc[idx]
            c = r["client_id"]
            pool = B[candidates(r) & (B["booking_id"].map(lambda x: x not in used)) & (B["status"] != "cancelled") &
                     (B["date_dt"] <= r["issued_dt"] + pd.Timedelta(days=7)) &
                     (B["date_dt"] >= r["issued_dt"] - pd.Timedelta(days=45)) &
                     ((B["price_num"] - r["amount_num"]).abs() < 0.01)]
            if len(pool):
                best = pool.loc[(r["issued_dt"] - pool["date_dt"]).abs().idxmin(), "booking_id"]
                refs[pos] = best
                used.add(best)
        I["booking_ref"] = refs
        I["ref_how"] = ["memo" if (isinstance(m, str) and (re.search(r"\bB\d{3,}\b", m) or re.search(r"\d/\d", m))) and x else ("guessed" if x else "") for m, x in zip(I["memo"], refs)]
        b2c = dict(zip(B["booking_id"], B["client_id"]))
        I["client_id"] = [c if isinstance(c, str) else b2c.get(ref) for c, ref in zip(I["client_id"], I["booking_ref"])]

    # production board: via booking id first, else name
    if len(PB):
        bid2c = dict(zip(B["booking_id"], B["client_id"])) if len(B) else {}
        cs = []
        for _, r in PB.iterrows():
            c = bid2c.get(r.get("shoot_booking", ""))
            if c is None:
                c = resolve_name(r["client"]) or resolve_agency_row(r["client"])
            cs.append(c)
        PB["client_id"] = cs

    # messages: email, phone, instagram handle
    handle_cands = {root: set(norm_name(nm).replace(" ", "") for nm in [clients[root]]) for root in clients}
    all_names = defaultdict(set)
    for node in uf.p:
        if node.startswith("n:"):
            r = cid(node)
            if r in client_nodes:
                all_names[r].add(node[2:].replace(" ", ""))
    mc = []
    for _, r in M.iterrows():
        c = client_of_email(r["email"]) or client_of_phone(r["phone"])
        if c is None and r["handle"]:
            h = re.sub(r"[^a-z]", "", r["handle"].lower())
            hits = [root for root, names in all_names.items() if any(n and n in h for n in names)]
            c = hits[0] if len(hits) == 1 else None
        mc.append(c)
    M["client_id"] = mc

    clients_df = pd.DataFrame([{"client_id": k, "client": v} for k, v in clients.items()]).sort_values("client")
    return dict(leads=L, bookings=B, invoices=I, payments=P, board=PB, messages=M,
                clients=clients_df, agency_emails=sorted(agency_emails), uf=uf)
