"""Axeom - what to do today. A phone-first Streamlit app.

Reads the 8 files, links every record to its real client, and turns problems into
suggestions. The owner says Yes or No. Nothing is ever sent from here.
"""
import io
from pathlib import Path

import pandas as pd
import streamlit as st

import actions as act
import pipeline as pl
from store import merge_upload, data_hash

st.set_page_config(page_title="Axeom - Today", page_icon=":clapper:", layout="centered")

st.markdown("""
<style>
.block-container {padding-top: 2.6rem; padding-bottom: 3rem; max-width: 760px;}
div[data-testid="stVerticalBlockBorderWrapper"] p {margin-bottom: .35rem;}
.kind {font-size: .75rem; font-weight: 600; text-transform: uppercase; letter-spacing: .04em; color: #b45309;}
.kind.money {color: #047857;} .kind.leave {color: #b91c1c;} .kind.lead {color: #1d4ed8;}
.big {font-size: 1.05rem; font-weight: 600; line-height: 1.35;}
.muted {color: #6b7280; font-size: .85rem;}
</style>""", unsafe_allow_html=True)

PACK = Path(__file__).parent / "data"

# ------------------------------------------------------------------ data in this session
if "data" not in st.session_state:
    st.session_state.data = pl.read_folder(PACK)
    st.session_state.decisions = {}
    st.session_state.upload_log = []


def jev_key():
    try:
        return st.secrets.get("TYPESAFE_API_KEY", "")
    except Exception:
        return ""


@st.cache_resource
def jev_cache():
    return {}  # message text -> Jev answer, shared by all visitors so each message is asked once


def jev_label(text, key):
    import jev
    c = jev_cache()
    if text not in c or not c[text].get("jev_category"):
        c[text] = jev.ask(text, key)
    return c[text]


@st.cache_data(show_spinner="Linking everything...")
def compute(h, _data, key):
    r = pl.build(_data)
    labels = {}
    if key:
        msgs = r["messages"][r["messages"]["direction"] == "from"]
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(6) as ex:
            res = list(ex.map(lambda t: jev_label(t, key), msgs["text"]))
        labels = {m: x.get("jev_category") for m, x in zip(msgs["msg_id"], res) if x.get("jev_category")}
    else:
        # no Jev key: use the labels the open Jev-style model (run in Colab) gave each message, matched by text
        lab = PACK / "model_labels.csv"
        if lab.exists():
            ml = pd.read_csv(lab, dtype=str, keep_default_na=False)
            by_text = dict(zip(ml["text"].str.strip(), ml["model_label"]))
            msgs = r["messages"][r["messages"]["direction"] == "from"]
            labels = {m: by_text[t.strip()] for m, t in zip(msgs["msg_id"], msgs["text"]) if t.strip() in by_text}
    r["jev_labels"] = labels
    # "today" = the data's own today, or later if new files carry newer dates
    dates = [pl.TODAY]
    for k, c in (("payments", "date_dt"), ("invoices", "issued_dt")):
        if len(r[k]):
            dates.append(r[k][c].max())
    if len(r["messages"]) and r["messages"]["ts"].notna().any():
        dates.append(r["messages"]["ts"].max().normalize())
    today = max(d for d in dates if pd.notna(d))
    acts, f, R = act.build_actions(r, today, labels)
    return r, acts, f, R, today


data = st.session_state.data
r, acts, f, R, today = compute(data_hash(data), data, jev_key())
names = dict(zip(r["clients"]["client_id"], r["clients"]["client"]))
dec = st.session_state.decisions

KIND_CLASS = {"Client may leave": "leave", "Money owed to us": "money", "Never billed": "money",
              "We owe them": "money", "Fix billing tool": "money", "No reply yet": "lead",
              "Wants to buy more": "lead", "Referral waiting": "lead"}


def card(a, i):
    with st.container(border=True):
        st.markdown(f"<div class='kind {KIND_CLASS.get(a['kind'], '')}'>{a['kind']}</div>"
                    f"<div class='big'>{a['title']}</div>", unsafe_allow_html=True)
        state = dec.get(a["key"])
        if state == "yes":
            st.success("You said yes. Nothing was sent. Copy this and send it yourself:" if a["draft"]
                       else "You said yes. It's on your list.")
            if a["draft"]:
                st.code(a["draft"], language=None, wrap_lines=True)
            c1, c2 = st.columns(2)
            if c1.button("Done, show next", key=f"k{i}{a['key']}", type="primary", width="stretch"):
                dec[a["key"]] = "done"
                st.rerun()
            if c2.button("Undo", key=f"u{i}{a['key']}", width="stretch"):
                dec.pop(a["key"], None)
                st.rerun()
            return
        if a["detail"]:
            with st.expander("Why"):
                st.text(a["detail"])
        st.markdown(f"**{a['ask']}**")
        c1, c2 = st.columns(2)
        if c1.button("Yes", key=f"y{i}{a['key']}", type="primary", width="stretch"):
            dec[a["key"]] = "yes"
            st.rerun()
        if c2.button("No", key=f"n{i}{a['key']}", width="stretch"):
            dec[a["key"]] = "no"
            st.rerun()


todo = [a for a in acts if dec.get(a["key"]) in (None, "yes")]

# ------------------------------------------------------------------ header
st.markdown(f"### Today, {today:%a %b %d}")
owed = sum(x["amount_owed"] for x in f["overdue"])
unbilled = sum(x["amount"] for x in f["never"])
lost = int(((R["kind"] != "follow_up") & ~R["on_board"]).sum()) if len(R) else 0
st.markdown(f"<span class='muted'>Owed to us <b>${owed:,.0f}</b> · Never billed <b>${unbilled:,.0f}</b> · "
            f"Edit requests missed <b>{lost}</b></span>", unsafe_allow_html=True)

tab_today, tab_clients, tab_money, tab_upload = st.tabs(["Do first", "Clients", "Money", "Add new files"])

# ------------------------------------------------------------------ today
with tab_today:
    if not todo:
        st.success("Nothing left to decide. Good work.")
    # top 3: the most urgent item of three different kinds (and three different clients),
    # so the first screen shows the three different problems, not one problem three times
    top, kinds_seen, clients_seen = [], set(), set()
    for a in todo:
        group = "money" if KIND_CLASS.get(a["kind"]) == "money" else a["kind"]
        if group in kinds_seen or (a["client_id"] and a["client_id"] in clients_seen):
            continue
        top.append(a)
        kinds_seen.add(group)
        clients_seen.add(a["client_id"])
        if len(top) == 3:
            break
    for i, a in enumerate(top):
        card(a, f"t{i}")

    rest = [a for a in todo if a not in top]
    if rest:
        st.markdown("")
        with st.expander(f"Everything else ({len(rest)})"):
            kinds = pd.Series([a["kind"] for a in rest]).value_counts()
            pick = st.radio("Show", ["All"] + [f"{k} ({v})" for k, v in kinds.items()], horizontal=True,
                            label_visibility="collapsed")
            for i, a in enumerate(rest):
                if pick == "All" or pick.startswith(a["kind"]):
                    card(a, f"r{i}")
    done = [a for a in acts if dec.get(a["key"]) in ("no", "done")]
    if done:
        with st.expander(f"Decided ({len(done)})"):
            for i, a in enumerate(done):
                st.markdown(f"{'Yes' if dec[a['key']] == 'done' else 'No'}: {a['title']}")
                if st.button("Undo", key=f"d{i}{a['key']}"):
                    dec.pop(a["key"], None)
                    st.rerun()

# ------------------------------------------------------------------ clients
with tab_clients:
    aliases = {}
    for df, nc in ((r["bookings"], "client_name"), (r["invoices"], "bill_to"), (r["payments"], "payer"), (r["board"], "client")):
        if len(df):
            for c, nm in zip(df["client_id"], df[nc]):
                if isinstance(c, str):
                    aliases.setdefault(c, set()).add(str(nm).strip())
    opts = sorted(names, key=lambda c: names[c].lower())
    label = {c: f"{names[c]}  ·  {', '.join(sorted(aliases.get(c, set()) - {names[c]}, key=str.lower)[:4])}" for c in opts}
    cid = st.selectbox(f"{len(opts)} clients. Type any name, even a misspelled one you've seen in the files.",
                       opts, format_func=lambda c: label[c], index=None, placeholder="Search a client")
    if cid:
        I, P = f["invoices"], f["payments"]
        ci = I[I["client_id"] == cid]
        st.markdown(f"#### {names[cid]}")
        L = r["leads"]
        lead = L[L["client_id"] == cid]
        emails = sorted(set(r["bookings"].loc[r["bookings"]["client_id"] == cid, "email_n"]) | set(lead["email_n"]))
        phones = sorted(set(lead["phone"]) | {f"{p[:3]}-{p[3:6]}-{p[6:]}" for p in r["messages"].loc[r["messages"]["client_id"] == cid, "phone"] if p})
        st.markdown(f"<span class='muted'>Also called: {', '.join(sorted(aliases.get(cid, set()) - {names[cid]}, key=str.lower))}<br>"
                    f"{' · '.join(emails)}<br>{' · '.join(p for p in phones if p)}<br>"
                    f"Came from: {', '.join(lead['source'].unique()) or 'unknown'}</span>", unsafe_allow_html=True)
        c1, c2, c3 = st.columns(3)
        c1.metric("Billed", f"${ci.loc[ci['status'].str.lower() != 'void', 'amount_num'].sum():,.0f}")
        c2.metric("Owes us", f"${ci.loc[ci['days_late'] > 0, 'balance'].sum():,.0f}")
        c3.metric("Missed edits", int(((R["client_id"] == cid) & (R["kind"] != "follow_up") & ~R["on_board"]).sum()))
        mine = [a for a in todo if a["client_id"] == cid]
        if mine:
            st.markdown("**Suggestions**")
            for i, a in enumerate(mine):
                card(a, f"c{i}")
        with st.expander("Invoices (billing tool vs bank)"):
            st.dataframe(ci[["invoice_no", "issued", "due", "amount_num", "status", "truth", "balance", "days_late", "memo"]]
                         .rename(columns={"status": "tool says", "truth": "bank says", "amount_num": "amount"}),
                         hide_index=True, width="stretch")
        with st.expander("Payments"):
            cp = P[P["client_id"] == cid]
            st.dataframe(cp[["payment_id", "date_dt", "payer", "amount_num", "method", "reference", "applied_to"]]
                         .rename(columns={"date_dt": "date", "amount_num": "amount"}), hide_index=True, width="stretch")
        with st.expander("Bookings"):
            st.dataframe(r["bookings"].loc[r["bookings"]["client_id"] == cid, ["booking_id", "date_dt", "client_name", "service", "status", "price_num"]]
                         .rename(columns={"date_dt": "date", "price_num": "price"}), hide_index=True, width="stretch")
        with st.expander("Videos"):
            st.dataframe(r["board"].loc[r["board"]["client_id"] == cid, ["video_id", "title", "editor", "stage", "notes_on_board", "revisions", "client_approved_on"]],
                         hide_index=True, width="stretch")
        with st.expander("Messages and edit requests"):
            cr = R[R["client_id"] == cid]
            for x in r["messages"][r["messages"]["client_id"] == cid].sort_values("ts", ascending=False).itertuples():
                tag = ""
                hit = cr[cr["msg_id"] == x.msg_id]
                if len(hit) and hit["kind"].iloc[0] != "follow_up":
                    tag = " · on board" if hit["on_board"].iloc[0] else " · **NOT on board**"
                elif len(hit):
                    tag = " · complaint"
                jl = r["jev_labels"].get(x.msg_id)
                jl = f" · AI label: {jl.replace('_', ' ')}" if jl else ""
                st.markdown(f"<span class='muted'>{x.ts:%b %d} · {x.channel}{jl}</span>{tag}<br>{x.text}", unsafe_allow_html=True)

# ------------------------------------------------------------------ money
with tab_money:
    st.markdown(f"**Owed to us and late: ${owed:,.0f}**")
    st.dataframe(pd.DataFrame(f["overdue"]).drop(columns=["client_id"], errors="ignore")
                 .rename(columns={"tool_says": "billing tool says"}), hide_index=True, width="stretch")
    st.markdown(f"**Work we did but never billed: ${unbilled:,.0f}**")
    st.dataframe(pd.DataFrame(f["never"]).drop(columns=["client_id"], errors="ignore"), hide_index=True, width="stretch")
    st.markdown(f"**Money we owe clients: ${sum(x['amount'] for x in f['credits']):,.0f}**")
    st.dataframe(pd.DataFrame(f["credits"]).drop(columns=["client_id"], errors="ignore"), hide_index=True, width="stretch")
    st.markdown(f"**Billing tool is wrong ({len(f['wrong'])})**")
    st.dataframe(pd.DataFrame(f["wrong"]).drop(columns=["client_id"], errors="ignore"), hide_index=True, width="stretch")
    st.caption("Bank payments are matched to invoices by the invoice number in the reference; if there is none, by payer, "
               "amount and date. Agency payments (e.g. Bluecat) are matched to the invoices billed to the agency.")

# ------------------------------------------------------------------ upload
with tab_upload:
    st.markdown("Add new **leads, bookings, invoices, payments, production_board, team, costs** (.csv) or **messages** (.txt). "
                "Everything above updates at once.")
    mode = st.radio("How to use them", ["Auto", "Add to what's here", "Replace what's here"], horizontal=True,
                    help="Auto: if the new file contains most of the old rows it replaces the old file, otherwise its rows are added (same ID = updated).")
    files = st.file_uploader("Add new files", type=["csv", "txt"], accept_multiple_files=True)
    if files and st.button("Update everything", type="primary"):
        new = dict(st.session_state.data)
        for up in files:
            raw = up.getvalue()
            kind = pl.guess_kind(up.name, raw)
            if not kind:
                st.session_state.upload_log.append(f"{up.name}: could not tell which file this is (header not recognised). Skipped.")
                continue
            content = raw.decode("utf-8", errors="replace") if kind == "messages" else pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
            new, msg = merge_upload(new, kind, content, {"Auto": "auto", "Add to what's here": "add", "Replace what's here": "replace"}[mode])
            st.session_state.upload_log.append(f"{up.name} → {msg}")
        st.session_state.data = new
        st.rerun()
    for line in st.session_state.upload_log[-12:]:
        st.markdown(f"- {line}")
    if st.button("Start over with the original pack"):
        st.session_state.data = pl.read_folder(PACK)
        st.session_state.upload_log = []
        st.session_state.decisions = {}
        st.rerun()
    st.caption((f"{len(r['jev_labels'])} messages sorted by the AI model; others by keyword rules. " if r["jev_labels"]
                else "Messages sorted by keyword rules. ") +
               f"Data as of {today:%b %d, %Y}. {len(names)} clients, {len(r['bookings'])} bookings, {len(r['invoices'])} invoices, "
               f"{len(r['payments'])} payments, {len(r['board'])} videos, {len(r['messages'])} messages.")
