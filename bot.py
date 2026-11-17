#!/usr/bin/env python3
"""Radar Congressos: avisa no Telegram sobre congressos/workshops com submissão aberta
e acompanha os artigos que você submeteu (revisão, resposta, camera-ready).

Modos:
  python bot.py                 # varredura completa (Groq + busca na web) + lembretes + comandos
  python bot.py --reminders     # só lembretes de prazos e datas dos seus artigos + comandos
  python bot.py --poll          # só lê e responde seus comandos do Telegram
  python bot.py --dry-run       # imprime em vez de enviar; não grava estado
  python bot.py --mock f.json   # usa itens de um arquivo em vez do Groq (teste)
  python bot.py --say "/ajuda"  # simula um comando seu (teste local)
  python bot.py --get-chat-id   # descobre seu chat_id depois de mandar /start pro bot

Somente biblioteca padrão do Python 3.9+.
"""
import argparse
import datetime as dt
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
STATE_FILE = Path(os.environ.get("RADAR_STATE", ROOT / "state.json"))
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
TODAY = dt.date.today()
MS_DAYS = CFG.get("milestone_reminder_days", [14, 7, 2, 0])
QRANK = {"A1": 0, "A2": 1, "A3": 2, "A4": 3, "B1": 4, "B2": 5, "B3": 6, "B4": 7, "B5": 8, "C": 9}
INACTIVE = ("rejeitado", "retirado")
STATUS_ALIAS = {
    "submetido": "submetido",
    "revisao": "em revisão", "revisão": "em revisão", "revisado": "em revisão", "emrevisao": "em revisão",
    "aceito": "aceito", "aceita": "aceito", "aprovado": "aceito",
    "rejeitado": "rejeitado", "rejeitada": "rejeitado", "negado": "rejeitado", "recusado": "rejeitado",
    "retirado": "retirado", "retirei": "retirado",
}

SCHEMA = """Retorne SOMENTE um array JSON (sem texto antes ou depois), com objetos neste formato:
{
  "name": "nome completo",
  "acronym": "sigla com ano, ex: NeurIPS 2027",
  "kind": "main_track" (conferência principal de artigos) | "workshop" (de artigos) | "event" (evento, summit, hackathon, meetup, encontro) | "grant" (bolsa de viagem, voluntariado, fellowship),
  "parent_conference": "evento que hospeda o workshop, ou null",
  "deadline": "YYYY-MM-DD (prazo de submissão de artigo, o mais próximo ainda não vencido)",
  "deadline_type": "full_paper" | "abstract" | "extended_abstract" | "poster" | "registration" | "speaker" | "grant",
  "archival": true | false | null (true se os anais são publicados/indexados),
  "qualis": "A1|A2|B1|B2|B3|B4|B5|C|null (Qualis CAPES, Ciência da Computação, da conferência principal ou da que hospeda o workshop; null se não tiver certeza)",
  "indexing": "IEEE, ACM, Springer, PMLR, OpenReview... ou null",
  "location": "Cidade, País ou online (SEMPRE informe)",
  "event_date": "YYYY-MM-DD ou null",
  "topics": ["tags curtas"],
  "url": "URL oficial do call for papers"
}"""


def pick(*opts):
    return random.choice(opts)


def esc(s):
    return html.escape(str(s))


# ---------------------------------------------------------------- HTTP helpers
def http_json(url, payload=None, headers=None, timeout=240, retries=4):
    data = json.dumps(payload).encode() if payload is not None else None
    hdrs = {"Content-Type": "application/json", "User-Agent": "radar-congressos/1.0 (+https://github.com)", "Accept": "application/json", **(headers or {})}
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


_BAD_MODELS = set()
_ANNOUNCED = set()


def groq_search(prompt):
    """Tenta cada modelo de CFG['models'] em ordem; pula os que a conta não tem acesso."""
    key = os.environ["GROQ_API_KEY"]
    last = None
    for model in CFG.get("models", [CFG["model"]]):
        if model in _BAD_MODELS:
            continue
        payload = {
            "model": model,
            "temperature": 0.1,
            "messages": [
                {
                    "role": "system",
                    "content": "Você é um assistente de pesquisa acadêmica. Use a busca na web e "
                    "confie apenas em páginas oficiais de call for papers. Nunca invente datas ou URLs.",
                },
                {"role": "user", "content": prompt},
            ],
        }
        if model.startswith("openai/gpt-oss"):
            payload["tools"] = [{"type": "browser_search"}]
        try:
            res = http_json(GROQ_URL, payload, headers={"Authorization": f"Bearer {key}"})
        except RuntimeError as e:
            if "model_not_found" in str(e) or "does not exist" in str(e):
                print(f"modelo {model} indisponível nesta conta, tentando o próximo", file=sys.stderr)
                _BAD_MODELS.add(model)
                last = e
                continue
            raise
        if model not in _ANNOUNCED:
            print(f"usando o modelo {model}", file=sys.stderr)
            _ANNOUNCED.add(model)
        return extract_json_array(res["choices"][0]["message"]["content"] or "")
    raise last or RuntimeError("nenhum modelo do Groq disponível")


def theme_prompt(theme):
    return (
        f"Hoje é {TODAY.isoformat()}. Pesquise congressos, conferências e workshops acadêmicos sobre: {theme}. Liste o MÁXIMO possível (pelo menos 10), não só os mais famosos.\n"
        f"Quero os que têm prazo de submissão de artigos ABERTO agora ou que vence nos próximos "
        f"{CFG['horizon_days']} dias. Inclua tanto a conferência principal (main_track) quanto workshops "
        f"associados. Só inclua itens com data de prazo confirmada em página oficial e URL oficial.\n\n{SCHEMA}"
    )


def event_prompt(theme):
    return (
        f"Hoje é {TODAY.isoformat()}. Pesquise eventos de tecnologia sobre: {theme}.\n"
        f"Quero os que têm inscrição, chamada de palestrantes/propostas, bolsa ou vaga de voluntariado com prazo ABERTO "
        f"agora ou nos próximos {CFG['horizon_days']} dias, ou que acontecem nesse período. Informe SEMPRE a cidade e o país "
        f"(ou online) e a data do evento. Use kind 'event' ou 'grant'. Em 'deadline' coloque o prazo de inscrição, chamada ou bolsa "
        f"(se não houver prazo, a data de início do evento). Liste o MÁXIMO possível (pelo menos 10). Só com URL oficial.\n\n{SCHEMA}"
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
    prompts += [event_prompt(t) for t in CFG.get("event_themes", [])]
    wl = CFG["watchlist"]
    for k in range(0, len(wl), 7):
        prompts.append(watch_prompt(wl[k : k + 7]))
    for area in CFG.get("ieee_areas", []):
        prompts.append(theme_prompt(f"conferências e workshops IEEE (e co-patrocinados pela IEEE Computer Society) de {area}"))
    # varredura por mês de prazo: pega eventos que os temas não cobrem
    first = TODAY.replace(day=1)
    for k in range(8):
        m = first.month - 1 + k
        d = dt.date(first.year + m // 12, m % 12 + 1, 1)
        prompts.append(
            f"Hoje é {TODAY.isoformat()}. Liste o MÁXIMO possível (pelo menos 15) de conferências e workshops acadêmicos de "
            f"computação, IA, visão, saúde digital, segurança e engenharia (IEEE, ACM, Springer, Elsevier e outros) cujo prazo de "
            f"submissão de artigos cai em {d.strftime('%m/%Y')}. Só com data confirmada em página oficial e URL oficial.\n\n{SCHEMA}"
        )
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


def lookup_milestones(name, url):
    """Datas pós-submissão (revisão, resposta, aceite, camera-ready, evento) do evento."""
    if not os.environ.get("GROQ_API_KEY"):
        return []
    prompt = (
        f"Hoje é {TODAY.isoformat()}. Um artigo foi submetido para: {name}" + (f" ({url})" if url else "") + ".\n"
        "Pesquise no site oficial as próximas datas importantes DEPOIS da submissão: divulgação das revisões, "
        "período de rebuttal/discussão, notificação de aceite, camera-ready e data do evento. "
        "Só inclua datas confirmadas no site oficial e ainda não passadas.\n"
        'Inclua também um item com label "Evento" (data de início do evento) e "location" (Cidade, País). '
        'Retorne SOMENTE um array JSON: [{"label": "Notificação de aceite", "date": "YYYY-MM-DD"}, '
        '{"label": "Evento", "date": "YYYY-MM-DD", "location": "Cidade, País"}]'
    )
    try:
        got = groq_search(prompt)
    except Exception as e:
        print(f"lookup falhou: {e}", file=sys.stderr)
        return []
    out = []
    for g in got:
        try:
            d = dt.date.fromisoformat(str(g.get("date"))[:10])
        except ValueError:
            continue
        if d >= TODAY and g.get("label"):
            item = {"label": str(g["label"])[:60], "date": d.isoformat()}
            if g.get("location"):
                item["location"] = str(g["location"])[:80]
            out.append(item)
    return out


# ---------------------------------------------------------------- normalização
def norm_key(it):
    base = (it.get("acronym") or it.get("name") or "").lower()
    base = re.sub(r"[^a-z0-9]+", "", base)
    return f"{base}|{it.get('kind', 'main_track')}|{(it.get('parent_conference') or '').lower()[:12]}"


def clean(items):
    out = {}
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
        it["kind"] = it.get("kind") if it.get("kind") in ("workshop", "event", "grant") else "main_track"
        q = str(it.get("qualis") or "").upper().strip()
        it["qualis"] = q if re.fullmatch(r"A[1-4]|B[1-5]|C", q) else None
        if it["kind"] in ("event", "grant"):
            it["qualis"] = None
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


def days_left(d):
    return (dt.date.fromisoformat(d) - TODAY).days


def is_priority(it):
    s = f"{it.get('name', '')} {it.get('acronym', '')} {it.get('parent_conference', '')}".lower()
    return any(k in s for k in CFG["priority_keywords"])


def fmt_item(it):
    left = days_left(it["deadline"])
    star = "⭐ " if is_priority(it) and it["kind"] == "main_track" else ""
    icon = "📄 PUBLICAR (main track)" if it["kind"] == "main_track" else "🧩 Workshop"
    title = esc(it.get("acronym") or it.get("name"))
    num = f" · #{it['id']}" if it.get("id") else ""
    lines = [f"{star}<b>{title}</b>{num}"]
    if it.get("name") and it.get("acronym") and it["name"] != it["acronym"]:
        lines.append(esc(it["name"]))
    lines.append(icon + (f" · em {esc(it['parent_conference'])}" if it.get("parent_conference") else ""))
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
        lines.append("ℹ️ " + esc(" · ".join(meta)))
    if it.get("topics"):
        lines.append("🏷 " + esc(", ".join(map(str, it["topics"][:5]))))
    lines.append(f'🔗 <a href="{html.escape(it["url"], quote=True)}">Call for papers</a>')
    return "\n".join(lines)


KIND_ICON = {"main_track": "📄", "workshop": "🧩", "event": "🎟", "grant": "🎁"}
KIND_RANK = {"main_track": 0, "workshop": 1, "grant": 2, "event": 3}


def fmt_compact(it):
    star = "⭐ " if is_priority(it) and it["kind"] == "main_track" else ""
    kind = KIND_ICON.get(it["kind"], "📄")
    title = esc(it.get("acronym") or it.get("name"))
    num = f"#{it['id']} " if it.get("id") else ""
    host = f" (em {esc(it['parent_conference'])})" if it.get("parent_conference") else ""
    q = f" · 🏅{esc(it['qualis'] or 'Qualis ?')}" if it["kind"] in ("main_track", "workshop") else ""
    try:
        when = f"🗓 {br(str(it['event_date'])[:10])}"
    except (ValueError, TypeError, KeyError):
        when = "🗓 data a confirmar"
    where = f"📍 {esc(it['location'])}" if it.get("location") else "📍 local a confirmar"
    return (
        f"{star}{kind} {num}<b>{title}</b>{host}{q}\n"
        f"{where}  {when}\n"
        f"⏳ prazo {br(it['deadline'])} · faltam {days_left(it['deadline'])} dias · "
        f'<a href="{html.escape(it["url"], quote=True)}">link</a>'
    )


def order(items):
    return sorted(items, key=lambda i: (KIND_RANK.get(i["kind"], 0), not is_priority(i), QRANK.get(i.get("qualis"), 5), i["deadline"]))


def build_messages(sections):
    """sections: [(titulo, [blocos de texto])] -> lista de textos <= 4000 chars."""
    msgs, cur = [], ""
    for title, blocks in sections:
        head = f"<b>{title}</b>\n\n"
        started = False
        for b in blocks:
            body = b + "\n\n"
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
    st = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    st.setdefault("events", {})
    st.setdefault("subs", [])
    st.setdefault("offset", 0)
    st.setdefault("next_id", 1)
    st.setdefault("next_sub", 1)
    st.setdefault("ignored", [])
    return st


def save_state(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def due_thresholds(left, reminded, days):
    """True (e marca como avisado) se cruzou um limiar de lembrete ainda não avisado."""
    if any(left <= r and r not in reminded for r in days):
        reminded.extend(x for x in days if x >= left and x not in reminded)
        return True
    return False


# ---------------------------------------------------------------- seus artigos
HELP = (
    "<b>Comandos</b>\n"
    "/eventos: lista os prazos abertos com número (#)\n"
    "/submeti 12: registra que você submeteu o evento #12\n"
    "/submeti Nome do evento: registra um evento que não está na lista\n"
    "/minhas: seus artigos e as próximas datas\n"
    "/status S1 revisao | aceito | rejeitado | retirado\n"
    "/data S1 Notificação 2027-08-20: adiciona ou corrige uma data\n"
    "/remover S1: apaga o registro\n"
    "/ignorar 12: esconde o evento #12 e não avisa mais dele\n"
    "/vou 12: marca que você vai a um evento (entra na /agenda)\n"
    "/agenda: sua turnê: congressos aceitos e eventos que você vai, com cidade e data ✈️\n"
    "/local S1 Paris, França: guarda a cidade do evento\n"
    "/evento S1 2027-12-07 [2027-12-11]: guarda as datas do evento\n\n"
    "Respondo em até ~3h e lembro 14, 7 e 2 dias antes de cada data, e no dia. 💅"
)


def sub_by_id(st, tok):
    try:
        n = int(re.sub(r"^[sS]", "", tok))
    except ValueError:
        return None
    return next((s for s in st["subs"] if s["id"] == n), None)


def fmt_sub(s):
    lines = [f"<b>S{s['id']} · {esc(s['name'])}</b> — {esc({'vou': 'vai participar'}.get(s['status'], s['status']))}"]
    info = []
    if s.get("location"):
        info.append(f"📍 {esc(s['location'])}")
    if s.get("event_date"):
        end = f" a {br(s['event_end'])}" if s.get("event_end") else ""
        info.append(f"🗓 {br(s['event_date'])}{end}")
    if info:
        lines.append("  " + "  ".join(info))
    ms = sorted((m for m in s["milestones"] if days_left(m["date"]) >= 0), key=lambda m: m["date"])
    for m in ms:
        left = days_left(m["date"])
        lines.append(f"  • {esc(m['label'])}: {br(m['date'])} ({'hoje' if left == 0 else f'em {left} dias'})")
    if not ms:
        lines.append("  (sem datas futuras; use /data para adicionar)")
    if s.get("url"):
        lines.append(f'  🔗 <a href="{html.escape(s["url"], quote=True)}">site</a>')
    return "\n".join(lines)


def apply_event_info(sub, found):
    for f in found:
        if f.get("location") and not sub.get("location"):
            sub["location"] = f["location"]
        if f["label"].lower().startswith("event") and not sub.get("event_date"):
            sub["event_date"] = f["date"]


def merge_milestones(sub, found):
    """Atualiza datas vindas da busca; não sobrescreve as manuais. Retorna textos do que mudou."""
    changes = []
    by_label = {m["label"].lower(): m for m in sub["milestones"]}
    for f in found:
        cur = by_label.get(f["label"].lower())
        if cur is None:
            sub["milestones"].append({"label": f["label"], "date": f["date"], "reminded": []})
            changes.append(f"nova: {f['label']} em {br(f['date'])}")
        elif not cur.get("manual") and cur["date"] != f["date"]:
            changes.append(f"{cur['label']}: {br(cur['date'])} → {br(f['date'])}")
            cur["date"], cur["reminded"] = f["date"], []
    return changes


def handle(text, st):
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lstrip("/").split("@")[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in ("start", "ajuda", "help"):
        return pick(
            "💋 <b>Radar Congressos</b> · SISSS, oi! Sou o radar de tudo da pesquisadora de tecnologia: artigos, workshops, eventos e bolsas. Vamos lacrar, babilônica? ✨\n\n",
            "👠 <b>Radar Congressos</b> · Oi, diva! Chegou a madrinha dos prazos. Artigos, eventos, bolsas: eu cuido do chá, você cuida do brilho 💅\n\n",
        ) + HELP

    if cmd == "eventos":
        evs = sorted(st["events"].values(), key=lambda i: i["deadline"])[:25]
        if not evs:
            return "Ainda não tenho eventos na lista. A busca completa roda toda sexta às 12h."
        rows = [
            f"#{e.get('id', '?')} · {KIND_ICON.get(e['kind'], '📄')} {esc(e.get('acronym') or e['name'])} · {br(e['deadline'])} · 📍{esc(e.get('location') or 'local a confirmar')}"
            for e in evs
        ]
        return "<b>Na passarela agora</b>\n" + "\n".join(rows) + "\n\nArtigo submetido: /submeti número · Vai participar: /vou número"

    if cmd in ("submeti", "submetido", "submetida"):
        if not arg:
            return "Use /submeti 12 (número do evento) ou /submeti Nome do evento."
        ev = None
        if arg.lstrip("#").isdigit():
            ev = next((e for e in st["events"].values() if str(e.get("id")) == arg.lstrip("#")), None)
            if not ev:
                return "Não achei esse número. Veja /eventos."
        name = (ev.get("acronym") or ev["name"]) if ev else arg
        url = ev.get("url") if ev else None
        sub = {"id": st["next_sub"], "name": name, "url": url, "status": "submetido", "milestones": [], "created": TODAY.isoformat()}
        st["next_sub"] += 1
        if ev:
            sub["location"] = ev.get("location") or None
            try:
                sub["event_date"] = dt.date.fromisoformat(str(ev.get("event_date"))[:10]).isoformat()
            except ValueError:
                pass
        found = lookup_milestones(name, url)
        merge_milestones(sub, found)
        apply_event_info(sub, found)
        st["subs"].append(sub)
        extra = "" if found else (
            "\n\nNão consegui achar as datas pós-submissão. Confira no site e adicione com "
            f"/data S{sub['id']} Notificação 2027-08-20"
        )
        cheer = pick(
            "SUBMETEU!!! 👑 Arrasou, BABILÔNICA! Tá no close, tá na lacração. Guardei tudo, agora é hidratar e esperar o veredito, sis 💅✨",
            "Condragulations, diva! 🎉 Artigo submetido, passou na passarela. Eu cuido das datas, você cuida do brilho ✨",
            "ENTREGOU, MÃE! 🔥 Submissão registrada. Tá montada e pronta pro veredito 👠💋",
        )
        return f"{cheer}\n\n{fmt_sub(sub)}{extra}\n\nConfira as datas no site oficial."

    if cmd in ("vou", "vouir"):
        if not arg:
            return "Use /vou 12 (número do evento) ou /vou Nome do evento."
        ev = None
        if arg.lstrip("#").isdigit():
            ev = next((e for e in st["events"].values() if str(e.get("id")) == arg.lstrip("#")), None)
            if not ev:
                return "Não achei esse número. Veja /eventos."
        name = (ev.get("acronym") or ev["name"]) if ev else arg
        sub = {
            "id": st["next_sub"], "name": name, "url": ev.get("url") if ev else None, "status": "vou",
            "milestones": [], "created": TODAY.isoformat(), "location": (ev.get("location") if ev else None) or None,
        }
        st["next_sub"] += 1
        try:
            d = dt.date.fromisoformat(str(ev.get("event_date"))[:10]) if ev else None
        except ValueError:
            d = None
        if d and d >= TODAY:
            sub["event_date"] = d.isoformat()
            sub["milestones"].append({"label": "Evento", "date": d.isoformat(), "reminded": []})
        st["subs"].append(sub)
        missing = []
        if not sub.get("location"):
            missing.append(f"/local S{sub['id']} Cidade, País")
        if not sub.get("event_date"):
            missing.append(f"/evento S{sub['id']} AAAA-MM-DD")
        tail = ("\n\nMe conta pra eu guardar:\n" + "\n".join(missing)) if missing else ""
        return pick(
            f"VAI BRILHAR!!! ✈️👠 Tá confirmada no {esc(name)}, sis! Guardei tudo, e eu aviso quando a data chegar perto 💋",
            f"SISSS, você VAI! 🎟✨ {esc(name)} que se prepare, a diva tá chegando! Anotado na turnê 💅",
        ) + "\n\n" + fmt_sub(sub) + tail

    if cmd == "minhas":
        if not st["subs"]:
            return "Você ainda não registrou nenhum artigo. Use /submeti."
        return "<b>Seus artigos</b>\n\n" + "\n\n".join(fmt_sub(s) for s in st["subs"])

    if cmd == "status":
        tok, _, val = arg.partition(" ")
        s = sub_by_id(st, tok) if tok else None
        new = STATUS_ALIAS.get(re.sub(r"[\s_-]", "", val.lower()))
        if not s or not new:
            return "Use /status S1 revisao, aceito, rejeitado ou retirado."
        s["status"] = new
        sid = f"S{s['id']}"
        if new == "aceito":
            msg = pick(
                f"ACEITAAAA!!! 🏆👑 SERVIU CUNT, MONSTRA! LACROU TUDO! Condragulations, {esc(s['name'])} é SEU! Mamou todo mundo 💅🔥🎉",
                f"TOMBOU GERAL!!! 💥 ACEITARAM {esc(s['name'])}! SISSS, você é uma estrela, uma ícone, uma lenda! The time has come de brilhar no palco ✨👠",
            )
            missing = []
            if not s.get("location"):
                missing.append(f"/local {sid} Cidade, País")
            if not s.get("event_date"):
                missing.append(f"/evento {sid} AAAA-MM-DD")
            if missing:
                msg += "\n\nMe conta pra eu guardar a turnê:\n" + "\n".join(missing)
            return msg + "\n\nSua turnê: /agenda ✈️"
        if new == "rejeitado":
            return pick(
                f"Sashay away?? NUNCA, amiga! 💔👠 Rejeição é só o reviewer 2 sem enxergar a arte. {esc(s['name'])} só não achou o palco certo ainda. Hidrata, chora no banho e volta mais montada: /eventos",
                f"Ai, sis, o bafo foi ruim 🥲 mas ninguém lacra sem passar perrengue. {esc(s['name'])} merece um palco melhor. Bora pro próximo? 💅",
            ) + " Paro de lembrar das datas dele."
        if new == "retirado":
            return f"{esc(s['name'])} saiu de cena com dignidade, tipo diva que sabe a hora de descer do palco 🎭 Paro de lembrar das datas dele."
        if new == "em revisão":
            return pick(
                f"Os jurados estão deliberando {esc(s['name'])}... respira, hidrata e mantém a pose 💅✨ ({sid} anotado)",
                f"Em revisão, sis. Reviewer 2 que se prepare, porque a obra é icônica 👑 ({sid} anotado)",
            )
        return f"Atualizado: {sid} está {esc(new)}."

    if cmd == "data":
        m = re.match(r"(\S+)\s+(.+?)\s+(\d{4}-\d{2}-\d{2})$", arg)
        s = sub_by_id(st, m.group(1)) if m else None
        if not m or not s:
            return "Use /data S1 Notificação 2027-08-20 (data no formato AAAA-MM-DD)."
        try:
            dt.date.fromisoformat(m.group(3))
        except ValueError:
            return "Data inválida. Use AAAA-MM-DD."
        label, date = m.group(2), m.group(3)
        cur = next((x for x in s["milestones"] if x["label"].lower() == label.lower()), None)
        if cur:
            cur.update(date=date, reminded=[], manual=True)
        else:
            s["milestones"].append({"label": label, "date": date, "reminded": [], "manual": True})
        return f"Anotado em S{s['id']}: {esc(label)} em {br(date)}."

    if cmd == "agenda":
        ac = sorted((x for x in st["subs"] if x["status"] in ("aceito", "vou")), key=lambda x: x.get("event_date") or "9999")
        if not ac:
            return "A turnê ainda tá vazia, sis, mas é questão de tempo 💅 Quando aceitarem: /status S1 aceito. Ou marque um evento: /vou número"
        rows = []
        for x in ac:
            when = br(x["event_date"]) if x.get("event_date") else "data a confirmar"
            if x.get("event_end"):
                when += f" a {br(x['event_end'])}"
            left = f" (faltam {days_left(x['event_date'])} dias)" if x.get("event_date") and days_left(x["event_date"]) >= 0 else ""
            rows.append(f"<b>{esc(x['name'])}</b> (S{x['id']})\n📍 {esc(x.get('location') or 'local a confirmar')}\n🗓 {when}{left}")
        return "<b>✈️ A TURNÊ DA DIVA</b>\n\n" + "\n\n".join(rows)

    if cmd == "local":
        tok, _, place = arg.partition(" ")
        x = sub_by_id(st, tok) if tok else None
        if not x or not place.strip():
            return "Use /local S1 Paris, França."
        x["location"] = place.strip()[:80]
        return f"Anotado, diva! 📍 {esc(x['name'])} vai ser em {esc(x['location'])}."

    if cmd == "evento":
        m = re.match(r"(\S+)\s+(\d{4}-\d{2}-\d{2})(?:\s+(?:a\s+)?(\d{4}-\d{2}-\d{2}))?$", arg)
        x = sub_by_id(st, m.group(1)) if m else None
        if not m or not x:
            return "Use /evento S1 2027-12-07 ou /evento S1 2027-12-07 2027-12-11."
        try:
            d1 = dt.date.fromisoformat(m.group(2))
            d2 = dt.date.fromisoformat(m.group(3)) if m.group(3) else None
        except ValueError:
            return "Data inválida. Use AAAA-MM-DD."
        x["event_date"] = d1.isoformat()
        x["event_end"] = d2.isoformat() if d2 else None
        cur = next((k for k in x["milestones"] if k["label"].lower() == "evento"), None)
        if cur:
            cur.update(date=d1.isoformat(), reminded=[], manual=True)
        else:
            x["milestones"].append({"label": "Evento", "date": d1.isoformat(), "reminded": [], "manual": True})
        return f"Guardado! 🗓 {esc(x['name'])}: {br(d1.isoformat())}" + (f" a {br(d2.isoformat())}" if d2 else "") + ". Eu aviso quando chegar perto ✈️"

    if cmd in ("ignorar", "ignora"):
        k = next((k for k, e in st["events"].items() if str(e.get("id")) == arg.lstrip("#")), None)
        if not k:
            return "Use /ignorar 12 (número do evento, veja /eventos)."
        name = st["events"].pop(k)
        st["ignored"].append(k)
        return pick("Próxima, sis! 💅", "Sashay away, evento! 👋", "Não é o meu babado. Tchau! ✨") + f" Não aviso mais de {esc(name.get('acronym') or name['name'])}."

    if cmd in ("remover", "apagar"):
        s = sub_by_id(st, arg)
        if not s:
            return "Use /remover S1."
        st["subs"].remove(s)
        return f"Removi S{s['id']} ({esc(s['name'])})."

    return "Sis, não entendi 🥲 Manda /ajuda que a madrinha explica de novo 💋"


def run_poll(st):
    for u in tg("getUpdates", {"offset": st["offset"], "timeout": 0}).get("result", []):
        st["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str((m.get("chat") or {}).get("id")) != str(os.environ["TELEGRAM_CHAT_ID"]):
            continue  # ignora quem não é você
        text = (m.get("text") or "").strip()
        if text:
            send(handle(text, st))


# ---------------------------------------------------------------- principal
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reminders", action="store_true")
    ap.add_argument("--poll", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--mock")
    ap.add_argument("--say")
    ap.add_argument("--get-chat-id", action="store_true")
    a = ap.parse_args()

    if a.get_chat_id:
        return get_chat_id()

    st = load_state()

    if a.say:
        print(handle(a.say, st))
        if not a.dry_run:
            save_state(st)
        return

    if not a.dry_run:
        run_poll(st)  # responde seus comandos antes de tudo
        if a.poll:
            save_state(st)
            return

    # eventos vencidos saem da lista
    st["events"] = {k: v for k, v in st["events"].items() if days_left(v["deadline"]) >= 0}

    new_items, changed = [], []
    if not a.reminders and not a.poll:
        found = clean(collect_items(a.mock))
        for k, it in found.items():
            if k in st["ignored"]:
                continue
            old = st["events"].get(k)
            if old is None:
                it["id"] = st["next_id"]
                st["next_id"] += 1
                it["reminded"] = []
                st["events"][k] = it
                new_items.append(it)
            else:
                it["id"] = old["id"]
                if old["deadline"] != it["deadline"]:
                    it["reminded"] = []
                    changed.append(it)
                else:
                    it["reminded"] = old.get("reminded", [])
                st["events"][k] = it

    # lembretes de prazos de submissão
    fresh = {id(i) for i in new_items + changed}
    due = []
    for it in st["events"].values():
        if id(it) not in fresh and due_thresholds(days_left(it["deadline"]), it.setdefault("reminded", []), CFG["reminder_days"]):
            due.append(it)

    # datas dos seus artigos: reconfere na busca completa e lembra
    ms_changes = []
    if not a.reminders and not a.poll and not a.mock and os.environ.get("GROQ_API_KEY"):
        for s in st["subs"]:
            if s["status"] not in INACTIVE:
                got = lookup_milestones(s["name"], s.get("url"))
                apply_event_info(s, got)
                for c in merge_milestones(s, got):
                    ms_changes.append(f"<b>S{s['id']} · {esc(s['name'])}</b>: {esc(c)}")
    ms_due = []
    for s in st["subs"]:
        if s["status"] in INACTIVE:
            continue
        for m in s["milestones"]:
            left = days_left(m["date"])
            if left >= 0 and due_thresholds(left, m.setdefault("reminded", []), MS_DAYS):
                when = "hoje" if left == 0 else f"em {left} dias ({br(m['date'])})"
                loc = f"\n📍 {esc(s['location'])}" if s.get("location") else ""
                ms_due.append(f"<b>S{s['id']} · {esc(s['name'])}</b>\n{esc(m['label'])}: {when}{loc}")

    papers = [i for i in new_items if i["kind"] in ("main_track", "workshop")]
    others = [i for i in new_items if i["kind"] not in ("main_track", "workshop")]
    sections = [
        ("📬 ATENÇÃO, SIS: o relógio do runway tá correndo", ms_due),
        ("🔁 Ué, mudaram as datas dos seus artigos! Que babado", ms_changes),
        ("🆕 Novos prazos de artigos: o chá fresquinho ☕", [fmt_compact(i) for i in order(papers)]),
        ("🎟 Novos eventos e bolsas: seu próximo palco 🎤", [fmt_compact(i) for i in order(others)]),
        ("🔄 Prazo mudou", [fmt_compact(i) for i in order(changed)]),
        ("⏰ CORRE, MONSTRA: o prazo fecha logo! 👠", [fmt_compact(i) for i in order(due)]),
    ]
    msgs = build_messages(sections)
    full = not a.reminders and not a.poll
    if full and not msgs:
        msgs = [pick("Sis, semana de calmaria: nenhum prazo novo. Aproveita pra escrever e hidratar 💧✨", "Nada de babado novo essa semana, diva. A madrinha volta sexta 💋")]
    if full and msgs:
        msgs[0] = (
            f"📡 <b>Radar Congressos</b> · {TODAY.strftime('%d/%m/%Y')} · {len(new_items)} novo(s)\n"
            + pick(
                "💋 BAFO! Chegou o Radar da madrinha com o chá quente dos congressos, sis! ☕✨",
                "SENHORAS E SENHORES, o runway de prazos abriu! 👠✨ Vamos publicar artigos, babilônica?",
                "SISSS, o babado é certo: tem congresso novo no pedaço! 💅🔥",
            )
            + "\nSubmeteu: /submeti número · Vai participar: /vou número · Sem interesse: /ignorar número · /ajuda\n\n" + msgs[0]
        )

    for m in msgs:
        if a.dry_run:
            print(m, "\n" + "=" * 40)
        else:
            send(m)
            time.sleep(1)
    if not a.dry_run:
        save_state(st)
    print(
        f"novos={len(new_items)} mudaram={len(changed)} lembretes={len(due)} datas={len(ms_due)} mensagens={len(msgs)}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
