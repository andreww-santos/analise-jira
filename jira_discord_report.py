#!/usr/bin/env python3
"""
Relatorio semanal do Jira -> Discord.

Roda toda segunda-feira e posta no Discord (via webhook) um resumo dos
principais indicadores da semana anterior (segunda a domingo):

  - Issues concluidas na semana
  - Bugs criados vs. bugs resolvidos/entregues na semana
  - Backlog atual de bugs em aberto (quebrado por prioridade)
  - Vazao (throughput) por pessoa e tempo medio de resolucao

Configuracao via variaveis de ambiente (ver .env.example / README.md).
"""

import os
import sys
import base64
from datetime import date, datetime, timedelta, timezone

import requests

# Garante saida em UTF-8 (evita UnicodeEncodeError no console do Windows/cp1252)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# ---------------------------------------------------------------------------
# Configuracao (via variaveis de ambiente / GitHub Secrets)
# ---------------------------------------------------------------------------
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

# Opcionais: restringir escopo. Vazio = todos os projetos.
JIRA_PROJECTS = os.environ.get("JIRA_PROJECTS", "").strip()   # ex: "ABC,DEF"
JIRA_EXTRA_JQL = os.environ.get("JIRA_EXTRA_JQL", "").strip()  # ex: "labels != interno"
TIMEZONE_LABEL = os.environ.get("TIMEZONE_LABEL", "America/Sao_Paulo")

# Tipo(s) de issue que representam "erro/bug" no seu Jira (separados por virgula).
# Padrao "Erro" (nomenclatura usada nos projetos da Bruning).
JIRA_BUG_TYPES = os.environ.get("JIRA_BUG_TYPES", "").strip() or "Erro"

# Status para os quais o QA move a demanda apos testar ("passar pra frente").
# Padrao: "release" (web) e "AG. VERSÃO" (Desktop). Separados por virgula.
JIRA_QA_STATUSES = os.environ.get("JIRA_QA_STATUSES", "").strip() or "release,AG. VERSÃO"

TIMEOUT = 30


def die(msg: str) -> None:
    print(f"ERRO: {msg}", file=sys.stderr)
    sys.exit(1)


def check_config() -> None:
    missing = [
        name
        for name, val in [
            ("JIRA_BASE_URL", JIRA_BASE_URL),
            ("JIRA_EMAIL", JIRA_EMAIL),
            ("JIRA_API_TOKEN", JIRA_API_TOKEN),
            ("DISCORD_WEBHOOK_URL", DISCORD_WEBHOOK_URL),
        ]
        if not val
    ]
    if missing:
        die("Variaveis de ambiente faltando: " + ", ".join(missing))


# ---------------------------------------------------------------------------
# Jira client
# ---------------------------------------------------------------------------
def jira_headers() -> dict:
    token = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def scope_clause() -> str:
    """Clausula JQL para restringir o escopo (projetos / extra)."""
    parts = []
    if JIRA_PROJECTS:
        keys = ",".join(k.strip() for k in JIRA_PROJECTS.split(",") if k.strip())
        parts.append(f"project in ({keys})")
    if JIRA_EXTRA_JQL:
        parts.append(f"({JIRA_EXTRA_JQL})")
    return " AND ".join(parts)


def with_scope(jql: str) -> str:
    scope = scope_clause()
    return f"({jql}) AND {scope}" if scope else jql


def bug_type_clause() -> str:
    """Clausula JQL para o(s) tipo(s) de erro/bug. Ex: issuetype in ("Erro")."""
    tipos = ",".join(
        f'"{t.strip()}"' for t in JIRA_BUG_TYPES.split(",") if t.strip()
    )
    return f"issuetype in ({tipos})"


def count_issues(jql: str) -> int:
    """Conta issues de um JQL. Usa /search/approximate-count (rapido) com
    fallbacks para compatibilidade entre instancias do Jira Cloud."""
    jql = with_scope(jql)

    # 1) Endpoint moderno de contagem aproximada
    try:
        r = requests.post(
            f"{JIRA_BASE_URL}/rest/api/3/search/approximate-count",
            headers=jira_headers(),
            json={"jql": jql},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            return int(r.json().get("count", 0))
    except requests.RequestException:
        pass

    # 2) Fallback legado (retorna "total")
    try:
        r = requests.get(
            f"{JIRA_BASE_URL}/rest/api/3/search",
            headers=jira_headers(),
            params={"jql": jql, "maxResults": 0},
            timeout=TIMEOUT,
        )
        if r.status_code == 200:
            return int(r.json().get("total", 0))
    except requests.RequestException:
        pass

    # 3) Ultimo recurso: pagina e conta
    return len(search_issues(jql, fields=["key"]))


def search_issues(jql: str, fields: list, apply_scope: bool = True) -> list:
    """Retorna todas as issues de um JQL, paginando via nextPageToken."""
    if apply_scope:
        jql = with_scope(jql)
    issues = []
    next_token = None
    while True:
        body = {"jql": jql, "fields": fields, "maxResults": 100}
        if next_token:
            body["nextPageToken"] = next_token
        r = requests.post(
            f"{JIRA_BASE_URL}/rest/api/3/search/jql",
            headers=jira_headers(),
            json=body,
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            die(f"Falha na busca Jira ({r.status_code}): {r.text[:500]}")
        data = r.json()
        issues.extend(data.get("issues", []))
        next_token = data.get("nextPageToken")
        if not next_token or data.get("isLast"):
            break
    return issues


def get_changelog(key: str) -> list:
    """Retorna o historico (changelog) completo de uma issue, paginado."""
    histories = []
    start_at = 0
    while True:
        r = requests.get(
            f"{JIRA_BASE_URL}/rest/api/3/issue/{key}/changelog",
            headers=jira_headers(),
            params={"startAt": start_at, "maxResults": 100},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            break
        data = r.json()
        vals = data.get("values", [])
        histories.extend(vals)
        total = data.get("total")
        start_at += len(vals)
        if data.get("isLast") or not vals or (total is not None and start_at >= total):
            break
    return histories


# ---------------------------------------------------------------------------
# Periodo: semana anterior (segunda a domingo)
# ---------------------------------------------------------------------------
def week_window(today: date):
    """Retorna (inicio, fim_exclusivo, rotulo). Inicio = segunda passada,
    fim_exclusivo = esta segunda (00:00)."""
    # today.weekday(): segunda=0 ... domingo=6
    this_monday = today - timedelta(days=today.weekday())
    start = this_monday - timedelta(days=7)   # segunda da semana passada
    end_excl = this_monday                     # esta segunda (exclusivo)
    last_sunday = this_monday - timedelta(days=1)
    label = f"{start.strftime('%d/%m/%Y')} a {last_sunday.strftime('%d/%m/%Y')}"
    return start, end_excl, label


def d(dt: date) -> str:
    return dt.strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Coleta de metricas
# ---------------------------------------------------------------------------
def parse_jira_dt(value: str):
    if not value:
        return None
    # ex: 2024-01-08T15:04:05.000-0300
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None


def collect(start: date, end_excl: date) -> dict:
    s, e = d(start), d(end_excl)
    m = {}

    # Issues concluidas na semana (qualquer tipo)
    m["done"] = count_issues(
        f'resolutiondate >= "{s}" AND resolutiondate < "{e}"'
    )

    bug = bug_type_clause()

    # Erros criados na semana
    m["bugs_created"] = count_issues(
        f'{bug} AND created >= "{s}" AND created < "{e}"'
    )

    # Erros resolvidos/entregues na semana
    m["bugs_resolved"] = count_issues(
        f'{bug} AND resolutiondate >= "{s}" AND resolutiondate < "{e}"'
    )

    # Backlog atual de erros em aberto, por prioridade
    open_bugs = search_issues(
        f"{bug} AND statusCategory != Done",
        fields=["priority"],
    )
    by_priority = {}
    for it in open_bugs:
        pr = (it["fields"].get("priority") or {}).get("name") or "Sem prioridade"
        by_priority[pr] = by_priority.get(pr, 0) + 1
    m["bug_backlog_total"] = len(open_bugs)
    m["bug_backlog_by_priority"] = by_priority

    # Vazao (throughput) + tempo medio de resolucao, sobre o resolvido na semana
    resolved = search_issues(
        f'resolutiondate >= "{s}" AND resolutiondate < "{e}"',
        fields=["assignee", "created", "resolutiondate", "issuetype"],
    )
    by_assignee = {}
    cycle_secs = []
    for it in resolved:
        f = it["fields"]
        who = (f.get("assignee") or {}).get("displayName") or "Sem responsavel"
        by_assignee[who] = by_assignee.get(who, 0) + 1
        c = parse_jira_dt(f.get("created"))
        rdt = parse_jira_dt(f.get("resolutiondate"))
        if c and rdt:
            cycle_secs.append((rdt - c).total_seconds())
    m["throughput_by_assignee"] = by_assignee
    m["avg_cycle_days"] = (
        round(sum(cycle_secs) / len(cycle_secs) / 86400, 1) if cycle_secs else None
    )
    m["resolved_total"] = len(resolved)

    # --- Metricas de QA ---
    # Subtasks abertas (qualquer tipo de subtarefa, ainda nao concluidas)
    m["open_subtasks"] = count_issues(
        "issuetype in subTaskIssueTypes() AND statusCategory != Done"
    )

    # QAs que "passaram demandas pra frente": quem moveu o status para um dos
    # status de QA (release / AG. VERSÃO) dentro da semana. Atribuido pelo autor
    # da transicao no changelog.
    qa_status = [st.strip() for st in JIRA_QA_STATUSES.split(",") if st.strip()]
    qa_targets = set(qa_status)
    status_list = ",".join(f'"{st}"' for st in qa_status)
    candidates = search_issues(
        f'status changed to ({status_list}) AFTER "{s}" BEFORE "{e}"',
        fields=["key"],
    )
    qa_forwarded = {}
    for it in candidates:
        for hist in get_changelog(it["key"]):
            created = hist.get("created", "")
            if not (s <= created[:10] < e):
                continue
            for item in hist.get("items", []):
                if item.get("field") == "status" and item.get("toString") in qa_targets:
                    who = (hist.get("author") or {}).get("displayName") or "Desconhecido"
                    qa_forwarded[who] = qa_forwarded.get(who, 0) + 1
    m["qa_forwarded"] = qa_forwarded
    m["qa_forwarded_total"] = sum(qa_forwarded.values())
    return m


# ---------------------------------------------------------------------------
# Montagem e envio do Discord
# ---------------------------------------------------------------------------
def fmt_priority(by_priority: dict) -> str:
    if not by_priority:
        return "Nenhum erro em aberto \U0001F389"
    order = [
        "Urgente", "Highest", "Blocker", "Critical",
        "Alta", "High",
        "Média", "Media", "Medium",
        "Baixa", "Low", "Lowest",
    ]
    def rank(name):
        return order.index(name) if name in order else len(order)
    linhas = [
        f"- {name}: **{qtd}**"
        for name, qtd in sorted(by_priority.items(), key=lambda x: (rank(x[0]), -x[1]))
    ]
    return "\n".join(linhas)


def fmt_top(by_assignee: dict, top: int = 5) -> str:
    if not by_assignee:
        return "Sem dados"
    ordenado = sorted(by_assignee.items(), key=lambda x: -x[1])[:top]
    return "\n".join(f"- {nome}: **{qtd}**" for nome, qtd in ordenado)


def build_payload(m: dict, label: str) -> dict:
    saldo = m["bugs_resolved"] - m["bugs_created"]
    saldo_txt = f"+{saldo}" if saldo > 0 else str(saldo)
    tendencia = "\U0001F7E2" if saldo >= 0 else "\U0001F534"  # verde / vermelho

    avg = m["avg_cycle_days"]
    avg_txt = f"{avg} dias" if avg is not None else "n/d"

    fields = [
        {
            "name": "✅ Issues concluidas",
            "value": f"**{m['done']}** na semana",
            "inline": True,
        },
        {
            "name": "\U0001F41B Erros (criados vs. resolvidos)",
            "value": (
                f"Criados: **{m['bugs_created']}**\n"
                f"Entregues: **{m['bugs_resolved']}**\n"
                f"Saldo: **{saldo_txt}** {tendencia}"
            ),
            "inline": True,
        },
        {
            "name": "⏱️ Tempo medio de resolucao",
            "value": f"**{avg_txt}**\n({m['resolved_total']} issues resolvidas)",
            "inline": True,
        },
        {
            "name": "\U0001F9EA Subtasks abertas",
            "value": f"**{m['open_subtasks']}** em aberto",
            "inline": True,
        },
        {
            "name": f"\U0001F4CB Backlog de erros em aberto ({m['bug_backlog_total']})",
            "value": fmt_priority(m["bug_backlog_by_priority"]),
            "inline": False,
        },
        {
            "name": "\U0001F3C6 Vazao por pessoa (top 5)",
            "value": fmt_top(m["throughput_by_assignee"]),
            "inline": False,
        },
        {
            "name": (
                "\U0001F9D1‍\U0001F52C QAs — demandas liberadas "
                f"({m['qa_forwarded_total']}) [release / AG. VERSÃO]"
            ),
            "value": fmt_top(m["qa_forwarded"]),
            "inline": False,
        },
    ]

    embed = {
        "title": "\U0001F4CA Indicadores semanais do Jira",
        "description": f"Periodo: **{label}** ({TIMEZONE_LABEL})",
        "color": 0x2684FF,  # azul Jira
        "fields": fields,
        "footer": {"text": "Gerado automaticamente toda segunda-feira"},
    }
    return {"embeds": [embed]}


def send_discord(payload: dict) -> None:
    r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=TIMEOUT)
    if r.status_code not in (200, 204):
        die(f"Falha ao postar no Discord ({r.status_code}): {r.text[:500]}")
    print("Relatorio postado no Discord com sucesso.")


# ---------------------------------------------------------------------------
def main() -> None:
    check_config()
    today = datetime.now(timezone.utc).date()
    start, end_excl, label = week_window(today)
    print(f"Coletando indicadores do periodo: {label}")
    metrics = collect(start, end_excl)
    print("Metricas:", metrics)
    payload = build_payload(metrics, label)
    if "--dry-run" in sys.argv:
        import json
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    send_discord(payload)


if __name__ == "__main__":
    main()
