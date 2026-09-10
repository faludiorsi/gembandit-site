# Gembandit

Porcelánékszer Herendi és Zsolnay darabokból, kézzel, Budapesten. Ez a bemutató oldal a Meska-boltra mutat:
https://www.meska.hu/shop/Gembandit

Az oldal: https://faludiorsi.github.io/gembandit-site/

## Hogyan frissül

A `scripts/sync-meska.py` naponta egyszer (GitHub Action) lekéri a Meska-bolt aktív termékeit, és újraírja az
`index.html` két jelölt blokkját (termékkártyák, számsor). A kiemelt hat darab a `featured.json`-ban van.
Ha egy kiemelt darab elkelt, a legújabb kapható kerül a helyére.

Kézzel: `SITE_DIR=. python3 scripts/sync-meska.py`
