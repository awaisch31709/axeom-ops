"""Merging uploaded files into the current data."""
import hashlib

import pandas as pd

ID_COL = {"leads": "lead_id", "bookings": "booking_id", "invoices": "invoice_no", "payments": "payment_id",
          "production_board": "video_id", "team": "name", "costs": "item"}


def data_hash(data):
    h = hashlib.md5()
    for k in sorted(data):
        v = data[k]
        h.update(k.encode())
        h.update(v.to_csv(index=False).encode() if isinstance(v, pd.DataFrame) else str(v).encode())
    return h.hexdigest()


def merge_upload(data, kind, content, mode="auto"):
    """Return (new data, one-line summary). mode: auto | add | replace."""
    data = dict(data)
    if kind == "messages":
        old = [l for l in str(data.get("messages", "")).splitlines() if l.strip()]
        new = [l for l in str(content).splitlines() if l.strip()]
        overlap = len(set(old) & set(new)) / max(len(old), 1)
        if mode == "replace" or (mode == "auto" and overlap >= 0.5):
            data["messages"] = "\n".join(new)
            return data, f"messages: replaced ({len(new)} lines)"
        added = [l for l in new if l not in set(old)]
        merged = sorted(old + added, key=lambda l: l[:18])
        data["messages"] = "\n".join(merged)
        return data, f"messages: {len(added)} new added"

    df = content.copy()
    df.columns = [c.strip().lower() for c in df.columns]
    idc = ID_COL[kind]
    old = data.get(kind)
    if old is None or not len(old) or idc not in df.columns or mode == "replace":
        data[kind] = df
        return data, f"{kind}: replaced ({len(df)} rows)"
    old_ids, new_ids = set(old[idc]), set(df[idc])
    overlap = len(old_ids & new_ids) / max(len(old_ids), 1)
    if mode == "auto" and overlap >= 0.5:
        data[kind] = df
        return data, f"{kind}: new version replaced the old one ({len(df)} rows; {len(new_ids - old_ids)} new)"
    keep = old[~old[idc].isin(new_ids)]
    merged = pd.concat([keep, df.reindex(columns=old.columns.union(df.columns, sort=False))], ignore_index=True)
    data[kind] = merged.fillna("")
    return data, f"{kind}: {len(new_ids - old_ids)} new rows added, {len(new_ids & old_ids)} updated"
