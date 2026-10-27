#!/usr/bin/env python3
"""
SergipePrevidência - Daily Snapshot & Wayback Machine Generator
Varre as páginas institucionais, Portal da Transparência e Pró-Gestão,
baixa um clone 100% navegável (HTML, CSS, JS, fontes e imagens com deduplicação)
e gera uma interface estilo Wayback Machine com linha do tempo e seletor de datas.
"""

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
import ssl
import xml.etree.ElementTree as ET
from pathlib import Path

BASE_DOMAIN = "sergipeprevidencia.se.gov.br"
BASE_URL = f"https://{BASE_DOMAIN}"
SITEMAP_URL = f"{BASE_URL}/page-sitemap.xml"

# Cabeçalho padrão para evitar bloqueios de firewall/WAF
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 (SergipePrevidencia-Wayback-Archiver/1.0)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Extensões de documentos pesados que devem continuar apontando para o servidor oficial (conforme configurado)
DOC_EXTENSIONS = (
    ".pdf", ".xlsx", ".xls", ".csv", ".doc", ".docx", ".zip", ".rar", ".7z",
    ".mp4", ".mp3", ".avi", ".mov", ".ppt", ".pptx", ".odt", ".ods"
)

# Extensões de assets estáticos para baixar e deduplicar em _assets/
ASSET_EXTENSIONS = (
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
    ".woff", ".woff2", ".ttf", ".eot"
)

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE


def fetch_bytes(url: str, timeout: int = 25) -> tuple[bytes | None, str | None, int]:
    """Faz download de uma URL retornando (bytes, content_type, status_code)."""
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
            ctype = resp.headers.get("Content-Type", "")
            return resp.read(), ctype, resp.status
    except urllib.error.HTTPError as e:
        return None, None, e.code
    except Exception:
        return None, None, 0


def normalize_page_url(url: str) -> str | None:
    """Normaliza uma URL interna da SergipePrevidência para formato canônico."""
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/"):
        url = BASE_URL + url

    parsed = urllib.parse.urlsplit(url)
    if parsed.netloc.lower().replace("www.", "") != BASE_DOMAIN:
        return None

    path = parsed.path
    if not path:
        path = "/"

    # Ignorar endpoints técnicos do WordPress, galerias pesadas de fotos de eventos ou arquivos de mídia direta
    lower_path = path.lower()
    if any(lower_path.startswith(p) for p in ("/wp-json", "/wp-admin", "/wp-login", "/xmlrpc.php", "/feed", "/galerias/")):
        return None
    if lower_path.endswith(DOC_EXTENSIONS) or lower_path.endswith(ASSET_EXTENSIONS):
        return None
    if "?p=" in url or "oembed" in url:
        return None

    if not path.endswith("/") and "." not in path.split("/")[-1]:
        path += "/"

    return f"{BASE_URL}{path}"


def url_to_rel_filepath(page_url: str) -> str:
    """Converte https://sergipeprevidencia.se.gov.br/portal-transparencia/ -> portal-transparencia/index.html"""
    parsed = urllib.parse.urlsplit(page_url)
    path = parsed.path.strip("/")
    if not path:
        return "index.html"
    return f"{path}/index.html"


def get_depth_prefix(rel_filepath: str) -> str:
    """Retorna o prefixo relativo (ex: '../../') para voltar à raiz do snapshot."""
    parts = Path(rel_filepath).parts
    depth = len(parts) - 1
    if depth <= 0:
        return "./"
    return "../" * depth


def discover_pages(limit: int | None = None) -> list[str]:
    """Descobre todas as páginas a partir do page-sitemap.xml + links de Transparência e Pró-Gestão."""
    print(f"[1/4] Buscando mapa de páginas em {SITEMAP_URL}...")
    pages_set: set[str] = set()
    pages_set.add(f"{BASE_URL}/")
    pages_set.add(f"{BASE_URL}/portal-transparencia/")

    xml_bytes, _, status = fetch_bytes(SITEMAP_URL)
    if xml_bytes and status == 200:
        xml_text = xml_bytes.decode("utf-8", errors="ignore")
        locs = re.findall(r"<loc>([^<]+)</loc>", xml_text)
        for loc in locs:
            norm = normalize_page_url(loc)
            if norm:
                pages_set.add(norm)

    # Também varrer a página inicial e a página do Portal da Transparência para garantir que nenhum link de menu ficou de fora
    seed_urls = [
        f"{BASE_URL}/",
        f"{BASE_URL}/portal-transparencia/",
        f"{BASE_URL}/portal-transparencia/mapa-do-site/",
        f"{BASE_URL}/progestao/",
    ]
    for seed in seed_urls:
        html_bytes, _, st = fetch_bytes(seed)
        if html_bytes and st == 200:
            html_str = html_bytes.decode("utf-8", errors="ignore")
            hrefs = re.findall(r'href=["\']([^"\'#]+)["\']', html_str, flags=re.IGNORECASE)
            for h in hrefs:
                norm = normalize_page_url(h)
                if norm:
                    # Evitar adicionar milhares de posts de notícias avulsas caso estejam na home
                    pages_set.add(norm)

    # Ordenar priorizando Home e Portal da Transparência
    def sort_key(u: str):
        if u == f"{BASE_URL}/":
            return (0, u)
        if "transparencia" in u:
            return (1, u)
        if "progestao" in u or "pro-gestao" in u:
            return (2, u)
        return (3, u)

    sorted_pages = sorted(pages_set, key=sort_key)
    if limit and limit > 0:
        sorted_pages = sorted_pages[:limit]

    print(f"      -> {len(sorted_pages)} páginas descobertas para o snapshot.")
    return sorted_pages


class AssetManager:
    """Gerencia o download e deduplicação de arquivos estáticos (CSS, JS, Imagens, Fontes) em _assets/."""

    def __init__(self, archive_root: Path, max_workers: int = 12):
        self.archive_root = archive_root
        self.assets_dir = archive_root / "_assets"
        self.assets_dir.mkdir(parents=True, exist_ok=True)
        self.url_to_asset_rel: dict[str, str] = {}
        self.max_workers = max_workers

    def register_asset(self, raw_url: str, base_page_url: str = BASE_URL) -> str | None:
        """Resolve uma URL de asset e retorna o caminho local em _assets/..."""
        if not raw_url:
            return None
        raw_url = raw_url.strip().strip("'\"")
        if raw_url.startswith(("data:", "javascript:", "mailto:", "tel:", "#")):
            return None

        abs_url = urllib.parse.urljoin(base_page_url, raw_url)
        parsed = urllib.parse.urlsplit(abs_url)

        if parsed.scheme not in ("http", "https"):
            return None

        # Não baixar PDFs ou planilhas como assets visuais
        path_lower = parsed.path.lower()
        if path_lower.endswith(DOC_EXTENSIONS):
            return None

        # Só baixar assets do próprio domínio ou CDNs conhecidas de fontes/ícones usadas pelo tema
        netloc = parsed.netloc.lower()
        allowed_hosts = (BASE_DOMAIN, f"www.{BASE_DOMAIN}", "fonts.googleapis.com", "fonts.gstatic.com", "cdn.jsdelivr.net", "cdnjs.cloudflare.com")
        if not any(netloc == h or netloc.endswith("." + h) for h in allowed_hosts):
            return None

        # Criar caminho determinístico baseado no path + hash curto da query string
        clean_path = parsed.path.lstrip("/")
        if not clean_path or clean_path.endswith("/"):
            return None

        # Se tiver query string (ex: ?ver=7.1.3), preservar mas limpar nome de arquivo
        ext = os.path.splitext(clean_path)[1].lower()
        if not ext and "fonts.googleapis.com" in netloc:
            ext = ".css"
            clean_path = "google-fonts/" + hashlib.md5(abs_url.encode()).hexdigest()[:12] + ext

        if netloc.replace("www.", "") != BASE_DOMAIN:
            rel_asset_path = f"_assets/external/{netloc}/{clean_path}"
        else:
            rel_asset_path = f"_assets/{clean_path}"

        # Sanitizar caracteres inválidos no Windows
        rel_asset_path = re.sub(r'[<>:"|?*]', "_", rel_asset_path)
        self.url_to_asset_rel[abs_url] = rel_asset_path
        return rel_asset_path

    def download_all_pending(self):
        """Faz download em paralelo de todos os assets registrados que ainda não existem no disco."""
        to_download = []
        for abs_url, rel_path in list(self.url_to_asset_rel.items()):
            full_path = self.archive_root / rel_path
            if not full_path.exists():
                to_download.append((abs_url, full_path, rel_path))

        if not to_download:
            print("      -> Todos os assets já estão em cache em _assets/.")
            return

        print(f"[3/4] Baixando {len(to_download)} arquivos estáticos novos (CSS, JS, imagens, fontes)...")
        css_files_to_scan = []

        def _dl(item):
            url, dest_path, rel_p = item
            try:
                req = urllib.request.Request(url, headers=HEADERS)
                with urllib.request.urlopen(req, timeout=20, context=ssl_ctx) as resp:
                    clen = resp.headers.get("Content-Length")
                    # Ignorar fotos brutas gigantes (> 3 MB) para não estourar limite do GitHub
                    if clen and int(clen) > 3 * 1024 * 1024:
                        return None
                    data = resp.read()
                    if len(data) > 3 * 1024 * 1024:
                        return None
                    ctype = resp.headers.get("Content-Type", "")
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    dest_path.write_bytes(data)
                    if dest_path.suffix.lower() == ".css" or (ctype and "text/css" in ctype.lower()):
                        return (url, dest_path, rel_p)
            except Exception:
                pass
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as ex:
            for res in ex.map(_dl, to_download):
                if res:
                    css_files_to_scan.append(res)

        # Processar sub-recursos dentro de arquivos CSS (fontes, background-images)
        sub_assets = []
        for css_url, css_dest, css_rel in css_files_to_scan:
            try:
                css_text = css_dest.read_text(encoding="utf-8", errors="ignore")
                urls_in_css = re.findall(r'url\(([^)]+)\)', css_text)
                for u in urls_in_css:
                    u_clean = u.strip().strip("'\"")
                    if u_clean.startswith(("data:", "#")):
                        continue
                    abs_sub = urllib.parse.urljoin(css_url, u_clean)
                    sub_rel = self.register_asset(abs_sub, css_url)
                    if sub_rel:
                        sub_full = self.archive_root / sub_rel
                        if not sub_full.exists():
                            sub_assets.append((abs_sub, sub_full))
                        # Reescrever caminho relativo dentro do CSS se mudou de domínio/pasta
                        css_dir = (self.archive_root / css_rel).parent
                        try:
                            rel_from_css = os.path.relpath(self.archive_root / sub_rel, css_dir).replace("\\", "/")
                            css_text = css_text.replace(u, f'"{rel_from_css}"')
                        except ValueError:
                            pass
                css_dest.write_text(css_text, encoding="utf-8")
            except Exception:
                pass

        if sub_assets:
            unique_subs = { (u, p) for u, p in sub_assets }
            print(f"      -> Baixando {len(unique_subs)} sub-recursos de CSS (fontes/ícones)...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as ex:
                ex.map(lambda it: _dl((it[0], it[1], "")), unique_subs)


def rewrite_html_for_snapshot(
    html: str,
    page_url: str,
    rel_filepath: str,
    known_pages_map: dict[str, str],
    asset_mgr: AssetManager,
    snapshot_id: str,
    timestamp_str: str,
) -> tuple[str, int]:
    """
    Reescreve um documento HTML para funcionar 100% offline/arquivado:
    - Converte assets (CSS, JS, imagens) para caminhos relativos em ../../_assets/...
    - Converte links internos entre páginas arquivadas para navegar dentro do mesmo snapshot
    - Mantém links de PDFs/planilhas apontando para a URL absoluta original (para não quebrar downloads)
    - Injeta uma barra superior discreta estilo Wayback Machine com metadados da captura
    """
    depth_to_snapshot_root = get_depth_prefix(rel_filepath)
    # Os snapshots ficam em archive/snapshots/<YYYY-MM-DD>/, então _assets fica 2 níveis acima da raiz do snapshot
    depth_to_archive_root = depth_to_snapshot_root + "../../"

    # Contar documentos (PDFs, planilhas) presentes na página para relatório de auditoria
    doc_links = set(
        re.findall(
            r'href=["\']([^"\']+\.(?:pdf|xlsx|xls|csv|doc|docx|zip))["\']',
            html,
            flags=re.IGNORECASE,
        )
    )

    # 1. Remover ou neutralizar tag <base> se existir
    html = re.sub(r"<base\b[^>]*>", "", html, flags=re.IGNORECASE)

    # 2. Reescrever atributos src, href de <link>, e srcset
    def replace_asset_attr(match):
        attr_name = match.group(1)
        quote = match.group(2)
        val = match.group(3)

        rel_asset = asset_mgr.register_asset(val, page_url)
        if rel_asset:
            return f'{attr_name}={quote}{depth_to_archive_root}{rel_asset}{quote}'
        return match.group(0)

    # Substituir src="..." em img, script, iframe, source, audio, video
    html = re.sub(
        r'\b(src)=(["\'])([^"\']+)\2',
        replace_asset_attr,
        html,
        flags=re.IGNORECASE,
    )

    # Limpar srcset para forçar o navegador a usar o src local deduplicado
    html = re.sub(r'\bsrcset=(["\'])[^"\']*\1', "", html, flags=re.IGNORECASE)

    # Substituir href="..." em tags <link> (CSS, ícones)
    def replace_link_tag(match):
        full_tag = match.group(0)
        if re.search(r'rel=["\'](?:https://api\.w\.org/|EditURI|wlwmanifest|alternate|shortlink|canonical)["\']', full_tag, re.I):
            return ""
        return re.sub(r'\b(href)=(["\'])([^"\']+)\2', replace_asset_attr, full_tag, flags=re.I)

    html = re.sub(r"<link\b[^>]*>", replace_link_tag, html, flags=re.IGNORECASE)

    # 3. Reescrever URLs de background-image inline (style="...url(...)...")
    def replace_inline_url(match):
        raw_u = match.group(1).strip().strip("'\"")
        rel_asset = asset_mgr.register_asset(raw_u, page_url)
        if rel_asset:
            return f'url("{depth_to_archive_root}{rel_asset}")'
        return match.group(0)

    html = re.sub(r'url\(([^)]+)\)', replace_inline_url, html, flags=re.IGNORECASE)

    # 4. Reescrever links <a href="...">
    def replace_anchor_href(match):
        quote = match.group(1)
        href = match.group(2).strip()

        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            return match.group(0)

        abs_href = urllib.parse.urljoin(page_url, href)
        parsed = urllib.parse.urlsplit(abs_href)

        # Se for um PDF ou planilha do site, garantir que fique com a URL absoluta completa
        if parsed.path.lower().endswith(DOC_EXTENSIONS):
            return f'href={quote}{abs_href}{quote}'

        # Se for uma página que faz parte do nosso snapshot, apontar para a cópia local arquivada
        norm = normalize_page_url(abs_href)
        if norm and norm in known_pages_map:
            target_rel = known_pages_map[norm]
            local_link = f"{depth_to_snapshot_root}{target_rel}"
            if parsed.fragment:
                local_link += f"#{parsed.fragment}"
            return f'href={quote}{local_link}{quote}'

        # Caso contrário (link externo ou notícia antiga fora do escopo), manter URL absoluta
        return f'href={quote}{abs_href}{quote}'

    html = re.sub(
        r'<a\b[^>]*?\bhref=(["\'])([^"\']+)\1',
        lambda m: m.group(0)[: m.start(1) - m.start(0) - 5] + replace_anchor_href(re.search(r'href=(["\'])([^"\']+)\1', m.group(0))),
        html,
        flags=re.IGNORECASE,
    )

    # 5. Injetar banner de auditoria discreto no topo (oculto automaticamente se estiver dentro do iframe do visualizador Wayback)
    wayback_banner = f"""
<!-- SERGIPEPREVIDENCIA WAYBACK SNAPSHOT BANNER -->
<div id="sp-wayback-audit-bar" style="display:none; background:#0f172a; color:#f8fafc; font-family:system-ui,-apple-system,sans-serif; font-size:13px; padding:8px 16px; border-bottom:2px solid #0284c7; align-items:center; justify-content:space-between; z-index:999999; position:relative;">
  <div style="display:flex; align-items:center; gap:10px;">
    <span style="background:#0284c7; color:#fff; font-weight:700; padding:2px 8px; border-radius:4px; font-size:11px; letter-spacing:0.5px;">SNAPSHOT OFICIAL</span>
    <span><strong>Data da Captura:</strong> {timestamp_str} ({snapshot_id})</span>
    <span style="color:#94a3b8;">|</span>
    <span><strong>URL Original:</strong> <a href="{page_url}" target="_blank" style="color:#38bdf8; text-decoration:underline;">{page_url}</a></span>
  </div>
  <div>
    <a href="{depth_to_archive_root}index.html?snapshot={snapshot_id}&page={urllib.parse.quote(rel_filepath)}" style="background:#1e293b; color:#e2e8f0; border:1px solid #334155; padding:4px 10px; border-radius:4px; text-decoration:none; font-size:12px;">Abrir no Painel Wayback</a>
  </div>
</div>
<script>
  if (window.self === window.top) {{
    var bar = document.getElementById('sp-wayback-audit-bar');
    if (bar) bar.style.display = 'flex';
  }}
</script>
"""
    if re.search(r"<body\b[^>]*>", html, flags=re.IGNORECASE):
        html = re.sub(
            r"(<body\b[^>]*>)",
            r"\1" + wayback_banner,
            html,
            count=1,
            flags=re.IGNORECASE,
        )
    else:
        html = wayback_banner + html

    return html, len(doc_links)


def extract_title(html: str, fallback_url: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        title = title.replace(" - Sergipe Previdência", "").replace(" – Sergipe Previdência", "").strip()
        if title:
            return title
    parsed = urllib.parse.urlsplit(fallback_url)
    return parsed.path.strip("/") or "Página Inicial"


def categorize_page(url: str) -> str:
    u = url.lower()
    if u.rstrip("/") == BASE_URL:
        return "Início"
    if "portal-transparencia" in u or "/transparencia" in u or "dados-abertos" in u or "prestacao-de-contas" in u or "demonstracoes-financeiras" in u:
        return "Portal da Transparência"
    if "progestao" in u or "pro-gestao" in u or "indice-de-situacao-previdenciaria" in u:
        return "Pró-Gestão"
    if "institucional" in u or "organograma" in u or "conselhos" in u or "legislacao" in u:
        return "Institucional & Legislação"
    if "programa" in u or "calendario" in u or "galeria" in u or "recital" in u or "censo" in u:
        return "Programas & Ações"
    return "Serviços & Outras Páginas"


def build_wayback_viewer_ui(archive_root: Path, manifest: dict):
    """Gera o arquivo archive/index.html e archive/manifest.json para a interface estilo Wayback Machine."""
    manifest_path = archive_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # Também embutimos o manifesto diretamente no index.html para que funcione mesmo abrindo via file:// (duplo clique local)
    manifest_json_inline = json.dumps(manifest, ensure_ascii=False)

    viewer_html = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Wayback Machine - SergipePrevidência (Snapshots de Auditoria)</title>
  <style>
    :root {{
      --bg-dark: #0f172a;
      --bg-panel: #1e293b;
      --bg-hover: #334155;
      --border: #334155;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #0284c7;
      --accent-light: #38bdf8;
      --success: #10b981;
      --badge-transp: #059669;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
      background: var(--bg-dark);
      color: var(--text-main);
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }}

    /* TOP WAYBACK HEADER */
    .wayback-topbar {{
      background: var(--bg-dark);
      border-bottom: 1px solid var(--border);
      padding: 10px 18px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-shrink: 0;
      box-shadow: 0 4px 12px rgba(0,0,0,0.25);
      z-index: 20;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 12px;
    }}
    .brand-icon {{
      width: 36px;
      height: 36px;
      border-radius: 8px;
      background: linear-gradient(135deg, #0284c7, #0369a1);
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 16px;
      letter-spacing: -0.5px;
      color: #fff;
      box-shadow: 0 2px 6px rgba(2,132,199,0.4);
    }}
    .brand-title {{
      font-size: 15px;
      font-weight: 700;
      line-height: 1.2;
    }}
    .brand-subtitle {{
      font-size: 11px;
      color: var(--text-muted);
    }}

    /* TIMELINE & CONTROLS */
    .controls-bar {{
      display: flex;
      align-items: center;
      gap: 10px;
      flex: 1;
      max-width: 820px;
      justify-content: center;
    }}
    .control-group {{
      display: flex;
      align-items: center;
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 4px 8px;
      gap: 6px;
    }}
    .control-label {{
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
      font-weight: 600;
    }}
    select, input[type="text"] {{
      background: transparent;
      color: var(--text-main);
      border: none;
      font-size: 13px;
      outline: none;
      padding: 4px;
    }}
    select option {{
      background: var(--bg-panel);
      color: var(--text-main);
    }}
    .nav-btn {{
      background: var(--bg-panel);
      color: var(--text-main);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 10px;
      font-size: 12px;
      cursor: pointer;
      transition: all 0.15s;
      display: flex;
      align-items: center;
      gap: 5px;
      text-decoration: none;
    }}
    .nav-btn:hover:not(:disabled) {{
      background: var(--bg-hover);
      border-color: var(--accent-light);
    }}
    .nav-btn:disabled {{
      opacity: 0.4;
      cursor: not-allowed;
    }}
    .nav-btn.primary {{
      background: var(--accent);
      border-color: var(--accent);
      font-weight: 600;
    }}
    .nav-btn.primary:hover {{
      background: #0369a1;
    }}

    /* MAIN WORKSPACE */
    .workspace {{
      display: flex;
      flex: 1;
      overflow: hidden;
    }}

    /* LEFT SIDEBAR: DIRECTORY OF PAGES */
    .sidebar {{
      width: 340px;
      background: var(--bg-panel);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
      transition: margin-left 0.2s ease;
    }}
    .sidebar.collapsed {{
      margin-left: -340px;
    }}
    .sidebar-header {{
      padding: 12px;
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .search-box {{
      display: flex;
      align-items: center;
      background: var(--bg-dark);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 10px;
    }}
    .search-box input {{
      width: 100%;
    }}
    .filter-pills {{
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
    }}
    .pill {{
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--bg-dark);
      color: var(--text-muted);
      border: 1px solid var(--border);
      cursor: pointer;
      user-select: none;
    }}
    .pill.active {{
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
      font-weight: 600;
    }}
    .page-list {{
      flex: 1;
      overflow-y: auto;
      padding: 8px;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .page-item {{
      padding: 8px 10px;
      border-radius: 6px;
      cursor: pointer;
      border: 1px solid transparent;
      display: flex;
      flex-direction: column;
      gap: 3px;
      transition: background 0.12s;
    }}
    .page-item:hover {{
      background: rgba(255,255,255,0.05);
    }}
    .page-item.active {{
      background: rgba(2, 132, 199, 0.18);
      border-color: var(--accent);
    }}
    .page-item-title {{
      font-size: 13px;
      font-weight: 600;
      color: var(--text-main);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
    }}
    .page-item-path {{
      font-size: 11px;
      color: var(--text-muted);
      word-break: break-all;
      font-family: Consolas, monospace;
    }}
    .badge {{
      font-size: 10px;
      padding: 1px 6px;
      border-radius: 4px;
      background: var(--bg-dark);
      color: var(--accent-light);
      white-space: nowrap;
      font-weight: 600;
    }}
    .badge.transp {{
      background: rgba(16, 185, 129, 0.15);
      color: #34d399;
    }}

    /* IFRAME VIEWPORT */
    .viewport-container {{
      flex: 1;
      display: flex;
      flex-direction: column;
      background: #fff;
      position: relative;
    }}
    .address-bar {{
      background: #f1f5f9;
      border-bottom: 1px solid #cbd5e1;
      padding: 6px 14px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      color: #334155;
      font-size: 12px;
    }}
    .address-url {{
      font-family: Consolas, monospace;
      background: #fff;
      border: 1px solid #cbd5e1;
      border-radius: 4px;
      padding: 4px 10px;
      flex: 1;
      color: #0f172a;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .audit-meta {{
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 12px;
      white-space: nowrap;
    }}
    .status-dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--success);
      display: inline-block;
      margin-right: 4px;
    }}
    iframe#snapshot-frame {{
      flex: 1;
      width: 100%;
      border: none;
      background: #fff;
    }}
  </style>
</head>
<body>

  <header class="wayback-topbar">
    <div class="brand">
      <button class="nav-btn" id="toggle-sidebar-btn" title="Mostrar/Ocultar Lista de Páginas">☰ Páginas</button>
      <div class="brand-icon">SP</div>
      <div>
        <div class="brand-title">SergipePrevidência • Wayback Machine</div>
        <div class="brand-subtitle">Arquivo Diário de Conformidade & Portal da Transparência</div>
      </div>
    </div>

    <div class="controls-bar">
      <button class="nav-btn" id="prev-snap-btn" title="Snapshot Anterior">◀ Anterior</button>
      <div class="control-group">
        <span class="control-label">📅 Data do Snapshot:</span>
        <select id="snapshot-select"></select>
      </div>
      <button class="nav-btn" id="next-snap-btn" title="Próximo Snapshot">Próximo ▶</button>
    </div>

    <div style="display:flex; align-items:center; gap:8px;">
      <a id="open-raw-btn" href="#" target="_blank" class="nav-btn" title="Abrir página arquivada em nova guia">↗ Nova Guia</a>
      <a id="open-live-btn" href="https://sergipeprevidencia.se.gov.br/" target="_blank" class="nav-btn primary" title="Abrir URL ao vivo no site oficial">🌐 Site Ao Vivo</a>
    </div>
  </header>

  <div class="workspace">
    <aside class="sidebar" id="sidebar">
      <div class="sidebar-header">
        <div style="display:flex; justify-content:space-between; align-items:center; font-size:12px; color:var(--text-muted);">
          <span>Páginas Arquivadas no Dia</span>
          <strong id="page-count-label" style="color:var(--text-main);">0 páginas</strong>
        </div>
        <div class="search-box">
          <input type="text" id="page-search" placeholder="Filtrar por nome ou URL (ex: licitacoes)..." />
        </div>
        <div class="filter-pills" id="category-pills">
          <span class="pill active" data-cat="ALL">Todas</span>
          <span class="pill" data-cat="Portal da Transparência">Transparência</span>
          <span class="pill" data-cat="Pró-Gestão">Pró-Gestão</span>
          <span class="pill" data-cat="Institucional & Legislação">Institucional</span>
        </div>
      </div>
      <div class="page-list" id="page-list"></div>
    </aside>

    <main class="viewport-container">
      <div class="address-bar">
        <div class="audit-meta">
          <span><span class="status-dot"></span><strong id="meta-timestamp">--</strong></span>
          <span style="color:#94a3b8;">|</span>
          <span id="meta-docs">0 documentos/PDFs verificados na página</span>
        </div>
        <div class="address-url" id="address-url">https://sergipeprevidencia.se.gov.br/</div>
        <div class="audit-meta">
          <span style="font-family:monospace; color:#64748b;" id="meta-sha">SHA256: --</span>
        </div>
      </div>
      <iframe id="snapshot-frame" src="about:blank"></iframe>
    </main>
  </div>

  <script>
    const INLINE_MANIFEST = {manifest_json_inline};

    let manifestData = INLINE_MANIFEST;
    let currentSnapshotId = null;
    let currentPageRel = "index.html";
    let currentCategory = "ALL";
    let searchQuery = "";

    const snapSelect = document.getElementById("snapshot-select");
    const prevBtn = document.getElementById("prev-snap-btn");
    const nextBtn = document.getElementById("next-snap-btn");
    const pageListEl = document.getElementById("page-list");
    const pageSearchEl = document.getElementById("page-search");
    const pageCountLabel = document.getElementById("page-count-label");
    const frameEl = document.getElementById("snapshot-frame");
    const addressUrlEl = document.getElementById("address-url");
    const metaTimestampEl = document.getElementById("meta-timestamp");
    const metaDocsEl = document.getElementById("meta-docs");
    const metaShaEl = document.getElementById("meta-sha");
    const openRawBtn = document.getElementById("open-raw-btn");
    const openLiveBtn = document.getElementById("open-live-btn");
    const sidebarEl = document.getElementById("sidebar");

    document.getElementById("toggle-sidebar-btn").addEventListener("click", () => {{
      sidebarEl.classList.toggle("collapsed");
    }});

    async function init() {{
      try {{
        const resp = await fetch("manifest.json", {{ cache: "no-store" }});
        if (resp.ok) {{
          manifestData = await resp.json();
        }}
      }} catch (e) {{
        // Usa INLINE_MANIFEST quando aberto via file://
      }}

      const snaps = manifestData.snapshots || [];
      if (!snaps.length) {{
        pageListEl.innerHTML = '<div style="padding:16px; color:#94a3b8;">Nenhum snapshot encontrado.</div>';
        return;
      }}

      const params = new URLSearchParams(window.location.search);
      const requestedSnap = params.get("snapshot");
      const requestedPage = params.get("page");

      snapSelect.innerHTML = "";
      snaps.forEach((s) => {{
        const opt = document.createElement("option");
        opt.value = s.id;
        opt.textContent = `${{s.id}} (${{s.page_count}} págs • ${{s.timestamp.split(" ")[1] || ""}})`;
        snapSelect.appendChild(opt);
      }});

      if (requestedSnap && snaps.some(s => s.id === requestedSnap)) {{
        currentSnapshotId = requestedSnap;
      }} else {{
        currentSnapshotId = snaps[0].id; // Mais recente primeiro
      }}

      if (requestedPage) {{
        currentPageRel = requestedPage;
      }}

      snapSelect.value = currentSnapshotId;
      renderSnapshot();
    }}

    function getCurrentSnapshotObj() {{
      return (manifestData.snapshots || []).find(s => s.id === currentSnapshotId);
    }}

    function renderSnapshot() {{
      const snaps = manifestData.snapshots || [];
      const idx = snaps.findIndex(s => s.id === currentSnapshotId);
      // Como snaps está em ordem decrescente (mais recente no índice 0):
      prevBtn.disabled = idx >= snaps.length - 1;
      nextBtn.disabled = idx <= 0;

      const snap = snaps[idx];
      if (!snap) return;

      renderSidebarList(snap);
      loadPageInFrame(snap, currentPageRel);
    }}

    function renderSidebarList(snap) {{
      const pages = snap.pages || [];
      const filtered = pages.filter(p => {{
        if (currentCategory !== "ALL" && p.category !== currentCategory) return false;
        if (searchQuery) {{
          const q = searchQuery.toLowerCase();
          return (p.title || "").toLowerCase().includes(q) || (p.url || "").toLowerCase().includes(q);
        }}
        return true;
      }});

      pageCountLabel.textContent = `${{filtered.length}} de ${{pages.length}} págs`;
      pageListEl.innerHTML = "";

      filtered.forEach(p => {{
        const div = document.createElement("div");
        div.className = "page-item" + (p.rel_path === currentPageRel ? " active" : "");
        const isTransp = p.category === "Portal da Transparência";
        const shortPath = p.url.replace("https://sergipeprevidencia.se.gov.br", "") || "/";
        div.innerHTML = `
          <div class="page-item-title">
            <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${{p.title}}</span>
            <span class="badge ${{isTransp ? 'transp' : ''}}">${{isTransp ? 'Transparência' : p.category.split(' ')[0]}}</span>
          </div>
          <div class="page-item-path">${{shortPath}}</div>
        `;
        div.addEventListener("click", () => {{
          currentPageRel = p.rel_path;
          renderSidebarList(snap);
          loadPageInFrame(snap, currentPageRel);
        }});
        pageListEl.appendChild(div);
      }});
    }}

    function loadPageInFrame(snap, relPath) {{
      let pageObj = (snap.pages || []).find(p => p.rel_path === relPath);
      if (!pageObj && snap.pages && snap.pages.length) {{
        pageObj = snap.pages[0];
        currentPageRel = pageObj.rel_path;
      }}
      if (!pageObj) return;

      const frameSrc = `snapshots/${{snap.id}}/${{pageObj.rel_path}}`;
      frameEl.src = frameSrc;
      addressUrlEl.textContent = pageObj.url;
      metaTimestampEl.textContent = `Capturado em ${{snap.timestamp}}`;
      metaDocsEl.textContent = `${{pageObj.doc_count || 0}} PDFs/documentos na página`;
      metaShaEl.textContent = `SHA256: ${{(pageObj.sha256 || "").slice(0, 12)}}...`;
      openRawBtn.href = frameSrc;
      openLiveBtn.href = pageObj.url;

      const urlParams = new URLSearchParams(window.location.search);
      urlParams.set("snapshot", snap.id);
      urlParams.set("page", pageObj.rel_path);
      window.history.replaceState(null, "", "?" + urlParams.toString());
    }}

    snapSelect.addEventListener("change", (e) => {{
      currentSnapshotId = e.target.value;
      renderSnapshot();
    }});

    prevBtn.addEventListener("click", () => {{
      const snaps = manifestData.snapshots || [];
      const idx = snaps.findIndex(s => s.id === currentSnapshotId);
      if (idx < snaps.length - 1) {{
        currentSnapshotId = snaps[idx + 1].id;
        snapSelect.value = currentSnapshotId;
        renderSnapshot();
      }}
    }});

    nextBtn.addEventListener("click", () => {{
      const snaps = manifestData.snapshots || [];
      const idx = snaps.findIndex(s => s.id === currentSnapshotId);
      if (idx > 0) {{
        currentSnapshotId = snaps[idx - 1].id;
        snapSelect.value = currentSnapshotId;
        renderSnapshot();
      }}
    }});

    pageSearchEl.addEventListener("input", (e) => {{
      searchQuery = e.target.value.trim();
      const snap = getCurrentSnapshotObj();
      if (snap) renderSidebarList(snap);
    }});

    document.querySelectorAll("#category-pills .pill").forEach(pill => {{
      pill.addEventListener("click", () => {{
        document.querySelectorAll("#category-pills .pill").forEach(el => el.classList.remove("active"));
        pill.classList.add("active");
        currentCategory = pill.getAttribute("data-cat");
        const snap = getCurrentSnapshotObj();
        if (snap) renderSidebarList(snap);
      }});
    }});

    // Sincronizar barra de endereço quando o usuário clica em links dentro do iframe
    frameEl.addEventListener("load", () => {{
      try {{
        const loc = frameEl.contentWindow.location.pathname;
        const marker = `/snapshots/${{currentSnapshotId}}/`;
        const pos = loc.indexOf(marker);
        if (pos !== -1) {{
          let rel = loc.substring(pos + marker.length);
          if (!rel || rel.endsWith("/")) rel += "index.html";
          if (rel !== currentPageRel) {{
            currentPageRel = rel;
            const snap = getCurrentSnapshotObj();
            if (snap) {{
              renderSidebarList(snap);
              const pageObj = (snap.pages || []).find(p => p.rel_path === currentPageRel);
              if (pageObj) {{
                addressUrlEl.textContent = pageObj.url;
                metaDocsEl.textContent = `${{pageObj.doc_count || 0}} PDFs/documentos na página`;
                metaShaEl.textContent = `SHA256: ${{(pageObj.sha256 || "").slice(0, 12)}}...`;
                openRawBtn.href = `snapshots/${{snap.id}}/${{pageObj.rel_path}}`;
                openLiveBtn.href = pageObj.url;
              }}
            }}
          }}
        }}
      }} catch (e) {{}}
    }});

    init();
  </script>
</body>
</html>
"""
    (archive_root / "index.html").write_text(viewer_html, encoding="utf-8")


def create_snapshot(output_dir: str, snapshot_date: str | None = None, limit: int | None = None, workers: int = 10):
    archive_root = Path(output_dir).resolve()
    snapshots_dir = archive_root / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.datetime.now()
    snap_id = snapshot_date or now.strftime("%Y-%m-%d")
    timestamp_str = now.strftime("%d/%m/%Y %H:%M:%S")

    current_snap_dir = snapshots_dir / snap_id
    current_snap_dir.mkdir(parents=True, exist_ok=True)

    # 1. Descobrir todas as páginas do escopo (Institucional + Portal da Transparência + Pró-Gestão)
    pages = discover_pages(limit=limit)
    known_pages_map = {u: url_to_rel_filepath(u) for u in pages}

    asset_mgr = AssetManager(archive_root, max_workers=workers)

    # 2. Baixar o HTML de todas as páginas em paralelo
    print(f"[2/4] Capturando HTML de {len(pages)} páginas para o snapshot '{snap_id}'...")
    raw_pages: dict[str, bytes] = {}

    def _fetch_page(u: str):
        data, ctype, st = fetch_bytes(u, timeout=25)
        if data and st == 200:
            return u, data
        return u, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for u, data in ex.map(_fetch_page, pages):
            if data:
                raw_pages[u] = data

    print(f"      -> {len(raw_pages)}/{len(pages)} páginas responderam com sucesso (HTTP 200).")

    # Processar cada página, reescrever links e registrar assets
    page_records = []
    for u in pages:
        data = raw_pages.get(u)
        if not data:
            continue
        html_str = data.decode("utf-8", errors="ignore")
        rel_filepath = known_pages_map[u]
        title = extract_title(html_str, u)
        category = categorize_page(u)
        sha256_hash = hashlib.sha256(data).hexdigest()

        rewritten_html, doc_count = rewrite_html_for_snapshot(
            html=html_str,
            page_url=u,
            rel_filepath=rel_filepath,
            known_pages_map=known_pages_map,
            asset_mgr=asset_mgr,
            snapshot_id=snap_id,
            timestamp_str=timestamp_str,
        )

        dest_file = current_snap_dir / rel_filepath
        dest_file.parent.mkdir(parents=True, exist_ok=True)
        dest_file.write_text(rewritten_html, encoding="utf-8")

        page_records.append({
            "url": u,
            "rel_path": rel_filepath,
            "title": title,
            "category": category,
            "doc_count": doc_count,
            "sha256": sha256_hash,
        })

    # 3. Baixar assets estáticos (CSS, JS, imagens, fontes) deduplicados
    asset_mgr.download_all_pending()

    # 4. Atualizar manifest.json e gerar interface Wayback Machine + Relatório de Auditoria
    print("[4/4] Atualizando índice Wayback Machine e comprovante de integridade (SHA-256)...")
    manifest_path = archive_root / "manifest.json"
    manifest = {"site": BASE_URL, "updated_at": timestamp_str, "snapshots": []}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Remover registro anterior do mesmo dia se estiver re-executando no mesmo dia
    existing_snaps = [s for s in manifest.get("snapshots", []) if s.get("id") != snap_id]
    new_snap_entry = {
        "id": snap_id,
        "timestamp": timestamp_str,
        "page_count": len(page_records),
        "total_docs_verified": sum(p["doc_count"] for p in page_records),
        "pages": page_records,
    }
    existing_snaps.append(new_snap_entry)
    # Ordenar do mais recente para o mais antigo
    existing_snaps.sort(key=lambda s: s["id"], reverse=True)
    manifest["snapshots"] = existing_snaps
    manifest["updated_at"] = timestamp_str

    # Salvar comprovante de auditoria individual na pasta do snapshot
    audit_file = current_snap_dir / "audit_hashes.json"
    audit_file.write_text(json.dumps(new_snap_entry, ensure_ascii=False, indent=2), encoding="utf-8")

    build_wayback_viewer_ui(archive_root, manifest)
    print(f"\n[CONCLUÍDO] Snapshot '{snap_id}' salvo com sucesso em: {current_snap_dir}")
    print(f"            Painel Wayback Machine pronto em: {archive_root / 'index.html'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SergipePrevidência Wayback Machine Snapshot Generator")
    parser.add_argument("--output", default="archive", help="Diretório raiz do arquivo Wayback (padrão: archive)")
    parser.add_argument("--date", default=None, help="Forçar ID da data do snapshot (ex: 2026-10-27)")
    parser.add_argument("--limit", type=int, default=None, help="Limitar número de páginas (útil para teste rápido)")
    parser.add_argument("--workers", type=int, default=12, help="Número de downloads simultâneos")
    args = parser.parse_args()

    create_snapshot(output_dir=args.output, snapshot_date=args.date, limit=args.limit, workers=args.workers)
