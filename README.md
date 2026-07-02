# Relatório semanal do Jira no Discord

Todo **segunda-feira às 09h (horário de Brasília)**, um workflow do GitHub Actions
consulta a API do Jira Cloud, calcula os principais indicadores da **semana anterior
(segunda a domingo)** e posta um resumo em um canal do Discord via webhook.

## Indicadores no relatório

- ✅ **Issues concluídas** na semana
- 🐛 **Bugs criados vs. resolvidos** na semana (com saldo e tendência)
- 📋 **Backlog atual de bugs em aberto**, quebrado por prioridade
- 🏆 **Vazão (throughput) por pessoa** (top 5)
- ⏱️ **Tempo médio de resolução** das issues resolvidas na semana

Por padrão cobre **todos os projetos**. Dá para restringir via `JIRA_PROJECTS`
ou aplicar um filtro extra com `JIRA_EXTRA_JQL`.

---

## Configuração (passo a passo)

### 1. Gerar o API Token do Jira
1. Acesse https://id.atlassian.com/manage-profile/security/api-tokens
2. **Create API token**, dê um nome (ex: `discord-report`) e copie o valor.
3. Guarde também o **email** da conta e a **URL** da instância
   (ex: `https://suaempresa.atlassian.net`).

### 2. Criar o webhook do Discord
1. No Discord: **Configurações do canal → Integrações → Webhooks → Novo webhook**.
2. Escolha o canal e **copie a URL do webhook**.

### 3. Subir para um repositório GitHub
```bash
cd jira-discord-report
git init
git add .
git commit -m "Relatorio semanal do Jira no Discord"
git branch -M main
git remote add origin https://github.com/SUA_ORG/jira-discord-report.git
git push -u origin main
```

### 4. Cadastrar os secrets no GitHub
No repositório: **Settings → Secrets and variables → Actions → New repository secret**.
Crie os seguintes secrets:

| Secret | Obrigatório | Exemplo |
|---|---|---|
| `JIRA_BASE_URL` | ✅ | `https://suaempresa.atlassian.net` |
| `JIRA_EMAIL` | ✅ | `voce@suaempresa.com.br` |
| `JIRA_API_TOKEN` | ✅ | *(o token gerado no passo 1)* |
| `DISCORD_WEBHOOK_URL` | ✅ | `https://discord.com/api/webhooks/...` |
| `JIRA_PROJECTS` | opcional | `ABC,DEF` (vazio = todos) |
| `JIRA_EXTRA_JQL` | opcional | `labels != interno` |

### 5. Testar sem esperar segunda-feira
Na aba **Actions** do repositório → workflow **"Relatorio semanal do Jira no Discord"**
→ **Run workflow**. Isso dispara manualmente (`workflow_dispatch`) e posta no Discord.

---

## Testar localmente (opcional)

```powershell
cd jira-discord-report
python -m pip install -r requirements.txt

# Defina as variáveis de ambiente na sessão:
$env:JIRA_BASE_URL      = "https://suaempresa.atlassian.net"
$env:JIRA_EMAIL         = "voce@suaempresa.com.br"
$env:JIRA_API_TOKEN     = "seu_token"
$env:DISCORD_WEBHOOK_URL= "https://discord.com/api/webhooks/..."

# Só mostra o JSON que seria enviado, sem postar no Discord:
python jira_discord_report.py --dry-run

# Envia de verdade:
python jira_discord_report.py
```

---

## Como funciona / ajustes

- **Período:** o script sempre calcula a semana anterior completa (segunda 00:00 até
  a segunda seguinte 00:00, exclusiva). Rodando na segunda, reporta a semana que fechou.
- **Horário do disparo:** definido no cron do arquivo
  `.github/workflows/weekly-jira-report.yml` (`0 12 * * 1` = 12:00 UTC = 09:00 BRT).
  Ajuste se quiser outro horário.
- **"Concluída"** é medido por `resolutiondate` (data de resolução). Se seu fluxo
  marca conclusão sem setar resolução, me avise para ajustar a JQL.
- **API do Jira:** usa os endpoints atuais do Jira Cloud
  (`/rest/api/3/search/jql` e `/rest/api/3/search/approximate-count`), com fallback
  para o endpoint legado quando disponível.
```
