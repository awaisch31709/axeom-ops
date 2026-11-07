"""Client edit requests: find them in messages, link them to a video, check they reached the board."""
import re

import pandas as pd

# request type -> (words in the message, words in a board note)
KINDS = [
    ("cut_intro", r"cut the first 3 seconds", r"cut first 3"),
    ("blur", r"blur the face", r"blur"),
    ("phone_end", r"phone number at the end", r"phone number"),
    ("walk_ins", r"walk-ins welcome", r"walk-ins"),
    ("captions", r"captions bigger", r"captions"),
    ("prices", r"talk about prices|hablo de precios", r"prices"),
    ("name_spelling", r"name is spelled wrong", r"name spelling"),
    ("stumble", r"stumble on my words", r"stumble"),
    ("calmer_music", r"calmer song", r"calmer music"),
    ("music_lower", r"music on .* too loud", r"music lower"),
    ("outside_shot", r"other shot, the one outside", r"outside shot"),
    ("brand_color", r"brand colou?r", r"brand colou?r"),
    ("new_song_intro", r"different song and a new intro", r"new intro|different song"),
    ("full_name", r"full name", r"full name|company name"),
]
FOLLOW_UP = r"i asked for|still not fixed|still has the same problem|second time i ask|did anyone see my message|still wrong|sigue igual|ya les hab"
SPAM = r"pay immediately|invoice\.zip|final notice|domain expires|real followers|crypto|rank #1|free audit|10x guaranteed"


def classify(text):
    t = text.lower()
    if re.search(SPAM, t):
        return "spam"
    if re.search(FOLLOW_UP, t):
        return "follow_up"
    for k, rx, _ in KINDS:
        if re.search(rx, t):
            return k
    return "other"


def find_title(text, titles):
    t = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    t = " " + re.sub(r"\s+", " ", t) + " "
    best = None
    for title in titles:
        k = " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", title.lower())).strip() + " "
        if k.strip() and k in t and (best is None or len(k) > len(best[1])):
            best = (title, k)
    return best[0] if best else None


def video_dates(PB, B):
    """Board rows have no date. Use the shoot's date; rows without a shoot borrow the date of the
    nearest row above them (video ids are in the order the work was added)."""
    d = PB["shoot_booking"].map(dict(zip(B["booking_id"], B["date_dt"])))
    order = PB["video_id"].str.extract(r"(\d+)")[0].astype(int)
    s = pd.Series(d.values, index=order.values).sort_index().ffill().bfill()
    return pd.Series(s.loc[order.values].values, index=PB.index)


def link_requests(r):
    M, B = r["messages"].copy(), r["bookings"]
    PB = r["board"].copy()
    PB["est_date"] = video_dates(PB, B)
    M = M[M["direction"] == "from"].copy()
    M["kind"] = M["text"].map(classify)
    rows = []
    for _, m in M.iterrows():
        if m["kind"] in ("spam", "other"):
            continue
        c = m["client_id"]
        vids = PB[PB["client_id"] == c] if c else PB.iloc[0:0]
        title = find_title(m["text"], vids["title"].unique()) if len(vids) else None
        vague = bool(re.search(r"the one we shot last week", m["text"], re.I))
        match = vids[vids["title"] == title] if title else vids.iloc[0:0]
        note_rx = next((n for k, _, n in KINDS if k == m["kind"]), None)
        if len(match) > 1:
            # the same title is made again every month: keep the versions that existed in the
            # 60 days before the message; if one of them carries the note, that's the one
            win = match[(match["est_date"] <= m["ts"] + pd.Timedelta(days=3)) &
                        (match["est_date"] >= m["ts"] - pd.Timedelta(days=60))]
            if not len(win):
                win = match.sort_values("est_date").iloc[[0]]
            noted = win[win["notes_on_board"].fillna("").str.contains(note_rx, case=False)] if note_rx else win.iloc[0:0]
            match = noted.iloc[[-1]] if len(noted) else win.sort_values("est_date").iloc[[-1]]
        if vague and c and not title:
            # the most recent shoot for this client before the message
            shoots = B[(B["client_id"] == c) & (B["date_dt"] <= m["ts"]) & (B["status"] == "completed")]
            if len(shoots):
                last = shoots.sort_values("date_dt")["booking_id"].iloc[-1]
                match = vids[vids["shoot_booking"] == last]
        on_board = False
        if note_rx and len(match):
            on_board = match["notes_on_board"].fillna("").str.contains(note_rx, case=False, regex=True).any()
        # several videos can share a title; prefer the one that was made before the message
        vid = match.iloc[-1] if len(match) else None
        rows.append(dict(
            msg_id=m["msg_id"], date=m["ts"], channel=m["channel"], client_id=c, kind=m["kind"],
            title=title or ("(vague: 'the one we shot last week')" if vague else None),
            video_ids=",".join(match["video_id"]) if len(match) else "",
            editor=",".join(sorted(set(match["editor"]))) if len(match) else "",
            stage=",".join(sorted(set(match["stage"]))) if len(match) else "",
            on_board=on_board, vague=vague,
            unknown_sender=bool(m["phone"]) and m["phone"] not in set(r["leads"].get("phone_n", [])),
            spanish=bool(re.search(r"\b(quiten|por favor|sigue|igual|parte|hablo)\b", m["text"], re.I)),
            text=m["text"],
        ))
    R = pd.DataFrame(rows)
    return M, R


def why_lost(row, nico_like=()):
    """Plain reason a request never reached the board."""
    reasons = []
    if row["editor"] and set(row["editor"].split(",")) <= set(nico_like):
        reasons.append(f"Video is edited by {row['editor']}; client notes never get onto {row['editor']}'s cards")
    if row["vague"]:
        reasons.append("Client wrote 'the one we shot last week' - no video named, nobody worked out which one")
    if row["channel"] in ("Instagram DM", "Voice Note"):
        reasons.append(f"Came by {row['channel']}, which nobody copies to the board")
    if row.get("unknown_sender"):
        reasons.append("Sent from a number/handle not on file")
    if row["spanish"]:
        reasons.append("Written in Spanish")
    if row["kind"] in ("new_song_intro",):
        reasons.append("Client changed their mind after approving the video")
    if row["kind"] == "full_name":
        reasons.append("Brand rule for all videos, but the board only has per-video notes")
    if not row["video_ids"] and not reasons:
        reasons.append("Could not match it to any video on the board")
    return "; ".join(reasons) or "Reason unclear from the data"
