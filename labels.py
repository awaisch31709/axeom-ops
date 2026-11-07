"""Coarse message categories used for the Jev job and its accuracy check."""
import re
import edits

CATEGORIES = {
    "edit_request": "Asks for a change to a specific video (cut, blur, captions, music, name spelling, colours, add a line, use another shot)",
    "complaint_not_fixed": "Says an earlier change request was not done or is still wrong",
    "wants_to_leave": "Wants to stop, pause, or talk about ending or continuing the contract",
    "wants_more_work": "Asks about buying more services, a package, or a price for more work",
    "referral": "Recommends or introduces another business that may want videos",
    "billing_payment": "About paying, invoices, or money owed",
    "scheduling": "About a shoot time, being late, moving or cancelling a shoot, what to bring, parking",
    "praise_or_approval": "Thanks, praise, a review, or approving a video",
    "contact_update": "New email, new phone number, or new business name",
    "spam_or_scam": "Unwanted sales pitch, scam, phishing, or not from a real client",
}


def rule_label(text):
    t = text.lower()
    k = edits.classify(text)
    if k == "spam":
        return "spam_or_scam"
    if re.search(r"whether we continue|talk about (?:our|the) contract|we'll pause", t):
        return "wants_to_leave"
    if k == "follow_up":
        return "complaint_not_fixed"
    if k not in ("other",):
        return "edit_request"
    if re.search(r"how much|what would that cost|we'd pay|package", t):
        return "wants_more_work"
    if re.search(r"friend|cousin|reach out|gave her your number", t):
        return "referral"
    if re.search(r"new email|new office number|is now ", t):
        return "contact_update"
    if re.search(r"pay|invoice|cash|sent half|accounting", t):
        return "billing_payment"
    if re.search(r"late|move|2pm|bring anything|parking|see you|cancel", t):
        return "scheduling"
    if re.search(r"thanks|great|love|best|review|approved|sales went up", t):
        return "praise_or_approval"
    return "?"
