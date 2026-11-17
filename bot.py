#!/usr/bin/env python3
"""Radar Congressos: avisa no Telegram sobre congressos/workshops com submissão aberta.

Modos:
  python bot.py                 # varredura completa (Groq + busca na web) + envio
  python bot.py --reminders     # só lembretes de prazos já conhecidos (sem API de IA)
  python bot.py --dry-run       # imprime em vez de enviar; não grava estado
  python bot.py --mock f.json   # usa itens de um arquivo em vez do Groq (teste)
  python bot.py --get-chat-id   # descobre seu chat_id depois de mandar /start pro bot

Somente biblioteca padrão do Python 3.9+.
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "state.json"
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
TODAY = dt.date.today()

SCHEMA = """Retorne SOMENTE um array JSON (sem texto antes ou depois), com objetos neste formato:
{
  "name": "nome completo",
  "acronym": "sigla com ano, ex: NeurIPS 2027",
  "kind": "main_track" ou "workshop",
  "parent_conference": "evento que hospeda o workshop, ou null",
  "deadline": "YYYY-MM-DD (prazo de submissão de artigo, o mais próximo ainda não vencido)",
  "deadline_type": "full_paper" | "abstract" | "extended_abstract" | "poster",
  "archival": true | false | null (true se os anais são publicados/indexados),
  "qualis": "A1|A2|B1|B2|B3|B4|B5|C|null (Qualis CAPES, Ciência da Computação, da conferência principal ou da que hospeda o workshop; null se não tiver certeza)",
  "indexing": "IEEE, ACM, Springer, PMLR, OpenReview... ou null",
  "location": "cidade/país ou online",
  "event_date": "YYYY-MM-DD ou null",
  "topics": ["tags curtas"],
  "url": "URL oficial do call for papers"
}"""


# ---------------------------------------------------------------- HTTP helpers
def http_json(url, payload=None, headers=None, timeout=240, retries=3):
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            last = f"HTTP {e.code}: {body}"
            if e.code not in (429, 500, 502, 503, 504):
                break
        except Exception as e:  # rede, timeout
            last = repr(e)
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(last)


# ---------------------------------------------------------------- busca (Groq)
def extract_json_array(text):
    text = re.sub(r"```(?:json)?", "", text)
    i, j = text.find("["), text.rfind("]")
    if i == -1 or j <= i:
        return []
    try:
        return json.loads(text[i : j + 1])
    except json.JSONDecodeError:
        return []


def groq_search(prompt):
    key = os.environ["GROQ_API_KEY"]
    res = http_json(
        GROQ_URL,
        {
            "model": CFG["model"],
            "temperature": 0.1,
            "messages": [
                {
                    "role": "system",
                    "content": "Você é um assistente de pesquisa acadêmica. Use a busca na web e "
                    "confie apenas em páginas oficiais de call for papers. Nunca invente datas ou URLs.",
                },
                {"role": "user", "content": prompt},
            ],
        },
        headers={"Authorization": f"Bearer {key}"},
    )
    return extract_json_array(res["choices"][0]["message"]["content"] or "")


def theme_prompt(theme):
    return (
        f"Hoje é {TODAY.isoformat()}. Pesquise congressos, conferências e workshops acadêmicos sobre: {theme}.\n"
        f"Quero os que têm prazo de submissão de artigos ABERTO agora ou que vence nos próximos "
        f"{CFG['horizon_days']} dias. Inclua tanto a conferência principal (main_track) quanto workshops "
        f"associados. Só inclua itens com data de prazo confirmada em página oficial e URL oficial.\n\n{SCHEMA}"
    )


def watch_prompt(names):
    return (
        f"Hoje é {TODAY.isoformat()}. Verifique na web o prazo de submissão atual/próximo (ainda não vencido, "
        f"dentro de {CFG['horizon_days']} dias) de cada um destes eventos, tanto da conferência principal quanto dos "
        f"workshops, quando já houver call for papers: {', '.join(names)}.\n"
        f"Omita os que ainda não divulgaram datas.\n\n{SCHEMA}"
    )


def collect_items(mock=None):
    if mock:
        return json.loads(Path(mock).read_text(encoding="utf-8"))
    prompts = [theme_prompt(t) for t in CFG["themes"]]
    wl = CFG["watchlist"]
    for k in range(0, len(wl), 7):
        prompts.append(watch_prompt(wl[k : k + 7]))
    items = []
    for n, p in enumerate(prompts, 1):
        try:
            got = groq_search(p)
            print(f"[{n}/{len(prompts)}] {len(got)} itens", file=sys.stderr)
            items.extend(got)
        except Exception as e:
            print(f"[{n}/{len(prompts)}] falhou: {e}", file=sys.stderr)
        time.sleep(2)
    return items


# ---------------------------------------------------------------- normalização
def norm_key(it):
    base = (it.get("acronym") or it.get("name") or "").lower()
    base = re.sub(r"[^a-z0-9]+", "", base)
    return f"{base}|{it.get('kind', 'main_track')}|{(it.get('parent_conference') or '').lower()[:12]}"


QRANK = {"A1": 0, "A2": 1, "A3": 2, "A4": 3, "B1": 4, "B2": 5, "B3": 6, "B4": 7, "B5": 8, "C": 9}


def clean(items):
    out, seen = {}, set()
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            d = dt.date.fromisoformat(str(it.get("deadline"))[:10])
        except ValueError:
            continue
        if d < TODAY or (d - TODAY).days > CFG["horizon_days"] + 60:
            continue
        url = str(it.get("url") or "")
        if not url.startswith("http") or not (it.get("name") or it.get("acronym")):
            continue
        it["deadline"] = d.isoformat()
        it["kind"] = "workshop" if it.get("kind") == "workshop" else "main_track"
        q = str(it.get("qualis") or "").upper().strip()
        it["qualis"] = q if re.fullmatch(r"A[1-4]|B[1-5]|C", q) else None
        if it["qualis"] and QRANK[it["qualis"]] > QRANK[CFG.get("min_qualis", "C")]:
            continue
        k = norm_key(it)
        if k in out and out[k]["deadline"] <= it["deadline"]:
            continue
        out[k] = it
    return out


# ---------------------------------------------------------------- formatação
def br(d):
    return dt.date.fromisoformat(d).strftime("%d/%m/%Y")


def is_priority(it):
    s = f"{it.get('name', '')} {it.get('acronym', '')} {it.get('parent_conference', '')}".lower()
    return any(k in s for k in CFG["priority_keywords"])


def fmt_item(it):
    left = (dt.date.fromisoformat(it["deadline"]) - TODAY).days
    star = "⭐ " if is_priority(it) and it["kind"] == "main_track" else ""
    icon = "📄 PUBLICAR (main track)" if it["kind"] == "main_track" else "🧩 Workshop"
    title = html.escape(it.get("acronym") or it.get("name"))
    lines = [f"{star}<b>{title}</b>"]
    if it.get("name") and it.get("acronym") and it["name"] != it["acronym"]:
        lines.append(html.escape(it["name"]))
    lines.append(icon + (f" · em {html.escape(it['parent_conference'])}" if it.get("parent_conference") else ""))
    who = "Qualis do evento principal" if it["kind"] == "workshop" else "Qualis"
    lines.append(f"🏅 {who}: {it['qualis']}" if it.get("qualis") else f"🏅 {who}: não confirmado")
    dtype = {"full_paper": "artigo completo", "abstract": "abstract", "extended_abstract": "resumo estendido", "poster": "poster"}.get(
        it.get("deadline_type"), "submissão"
    )
    lines.append(f"📅 {br(it['deadline'])} ({dtype}) · faltam {left} dias")
    meta = []
    if it.get("archival") is True:
        meta.append("com anais")
    elif it.get("archival") is False:
        meta.append("não-arquival")
    if it.get("indexing"):
        meta.append(str(it["indexing"]))
    if it.get("location"):
        meta.append(str(it["location"]))
    if meta:
        lines.append("ℹ️ " + html.escape(" · ".join(meta)))
    if it.get("topics"):
        lines.append("🏷 " + html.escape(", ".join(map(str, it["topics"][:5]))))
    lines.append(f'🔗 <a href="{html.escape(it["url"], quote=True)}">Call for papers</a>')
    return "\n".join(lines)


def order(items):
    return sorted(items, key=lambda i: (i["kind"] != "main_track", not is_priority(i), QRANK.get(i.get("qualis"), 5), i["deadline"]))


def build_messages(sections):
    """sections: [(titulo, [itens])] -> lista de textos <= 4000 chars."""
    msgs, cur = [], ""
    for title, items in sections:
        head = f"<b>{title}</b>\n\n"
        started = False
        for it in order(items):
            body = fmt_item(it) + "\n\n"
            if cur and len(cur) + len(body) + len(head) > 3900:
                msgs.append(cur)
                cur, started = "", False  # nova mensagem repete o título da seção
            if not started:
                cur += head
                started = True
            cur += body
    if cur.strip():
        msgs.append(cur)
    return msgs


# ---------------------------------------------------------------- Telegram
def tg(method, payload):
    tok = os.environ["TELEGRAM_BOT_TOKEN"]
    return http_json(f"https://api.telegram.org/bot{tok}/{method}", payload, timeout=60)


def send(text):
    tg(
        "sendMessage",
        {
            "chat_id": os.environ["TELEGRAM_CHAT_ID"],
            "text": text.strip(),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )


def get_chat_id():
    for u in tg("getUpdates", {}).get("result", []):
        m = u.get("message") or u.get("channel_post") or {}
        c = m.get("chat")
        if c:
            print(f"chat_id={c['id']}  ({c.get('first_name') or c.get('title')})")


# ---------------------------------------------------------------- estado
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reminders", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mock")
    ap.add_argument("--get-chat-id", action="store_true")
    a = ap.parse_args()

    if a.get_chat_id:
        return get_chat_id()

    state = load_state()
    # descarta o que já venceu
    state = {k: v for k, v in state.items() if dt.date.fromisoformat(v["item"]["deadline"]) >= TODAY}

    new_items, changed = [], []
    if not a.reminders:
        found = clean(collect_items(a.mock))
        for k, it in found.items():
            if k not in state:
                new_items.append(it)
                state[k] = {"item": it, "reminded": []}
            elif state[k]["item"]["deadline"] != it["deadline"]:
                changed.append(it)
                state[k] = {"item": it, "reminded": []}
            else:
                state[k]["item"] = it  # atualiza metadados

    due = []
    for k, v in state.items():
        left = (dt.date.fromisoformat(v["item"]["deadline"]) - TODAY).days
        if v["item"] in new_items or v["item"] in changed:
            continue
        for r in sorted(CFG["reminder_days"]):
            if left <= r and r not in v["reminded"]:
                due.append(v["item"])
                v["reminded"] = sorted(set(v["reminded"] + [x for x in CFG["reminder_days"] if x >= left]))
                break

    sections = [
        ("🆕 Novos prazos abertos", new_items),
        ("🔄 Prazo mudou", changed),
        ("⏰ Fechando em breve", due),
    ]
    msgs = build_messages(sections)
    if not a.reminders and not msgs:
        msgs = ["Radar Congressos: varredura feita, nada novo esta semana."]
    if not a.reminders and msgs:
        n = len(new_items)
        msgs[0] = f"📡 <b>Radar Congressos</b> · {TODAY.strftime('%d/%m/%Y')} · {n} novo(s)\n" + msgs[0]

    for m in msgs:
        if a.dry_run:
            print(m, "\n" + "=" * 40)
        else:
            send(m)
            time.sleep(1)
    if not a.dry_run:
        save_state(state)
    print(f"novos={len(new_items)} mudaram={len(changed)} lembretes={len(due)} mensagens={len(msgs)}", file=sys.stderr)


if __name__ == "__main__":
    main()
