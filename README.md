# SergipePrevidência - Wayback Machine & Snapshot Diário

Sistema automatizado de preservação web e auditoria diária para o portal da **SergipePrevidência** (`https://sergipeprevidencia.se.gov.br/`), com foco especial no **Portal da Transparência** e **Pró-Gestão**.

##  Como Funciona

1. **Descoberta Automática:** O script [`snapshot_archiver.py`](snapshot_archiver.py) consulta o `page-sitemap.xml` oficial do site e varre os menus do **Portal da Transparência** e **Pró-Gestão** (~180 páginas).
2. **Clone 100% Navegável:** Todas as páginas HTML são baixadas e têm seus links internos reescritos para que você possa clicar nos menus e navegar entre as páginas exatamente como estavam naquele dia.
3. **Deduplicação Inteligente de Assets (`_assets/`):** Arquivos de estilo (`.css`), scripts (`.js`), fontes e imagens são salvos de forma deduplicada em `archive/_assets/`. Assim, cada novo snapshot diário adiciona apenas poucos megabytes ao repositório.
4. **Comprovação de Integridade (SHA-256):** Cada snapshot gera um arquivo `audit_hashes.json` registrando o código hash criptográfico `SHA-256` de cada página e a contagem de PDFs/planilhas verificados naquela data.
5. **Painel Estilo Wayback Machine (`archive/index.html`):** Interface visual interativa com seletor de datas no topo, busca rápida de páginas, filtro exclusivo para a aba **Transparência** e visualização lado a lado com o site ao vivo.

## 💻 Como Rodar ou Testar Localmente (Windows / Linux / Mac)

Não precisa instalar nenhuma biblioteca externa (usa apenas a biblioteca padrão do Python 3):

```powershell
# Capturar o snapshot completo de hoje (~180 páginas)
python snapshot_archiver.py --output archive

# Testar rapidamente capturando apenas as 25 primeiras páginas (Início + Transparência)
python snapshot_archiver.py --output archive --limit 25
```

Depois de rodar, basta abrir o arquivo `archive/index.html` no seu navegador!
