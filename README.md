# Axeom – what to do today

A phone-first app for the owner of Axeom. It reads the studio's 8 files, links every record to the real client (even when the name is spelled differently), and shows the few things to do first. The owner taps Yes or No. Nothing is ever sent automatically.

## Files
- `app.py` – the Streamlit app (4 tabs: Do first, Clients, Money, Add new files)
- `pipeline.py` – reads and cleans the files and links records to clients (email → phone → name)
- `money.py` – matches bank payments to invoices and finds overdue, unbilled and wrong invoices
- `edits.py` – finds client edit requests in messages and checks whether they reached the production board
- `actions.py` – turns the findings into ranked suggestions
- `channels.py` – profit per client and per marketing channel
- `store.py` – merges uploaded files into the data
- `build_answers.py` – writes answers.json from the same code (`python build_answers.py data out/answers.json`)
- `data/` – the original pack 92 files

## Run locally
```
pip install -r requirements.txt
streamlit run app.py
```
