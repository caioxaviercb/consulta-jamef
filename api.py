from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright
from pydantic import BaseModel
from typing import Optional
import asyncio

app = FastAPI(title="Jamef Rastreamento API", version="1.0.0")

# Libera CORS para o Lovable (ou qualquer frontend) acessar
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

CNPJ_PADRAO = "48775191000190"


# ── Modelos de resposta ───────────────────────────────────────────────────────

class EventoHistorico(BaseModel):
    data: Optional[str]
    status: Optional[str]
    estado_origem: Optional[str]
    municipio_origem: Optional[str]
    estado_destino: Optional[str]
    municipio_destino: Optional[str]

class ResultadoRastreamento(BaseModel):
    nf: str
    origem: Optional[str]
    destino: Optional[str]
    previsao_entrega: Optional[str]
    status_atual: Optional[str]
    historico: list[EventoHistorico]


# ── Lógica de scraping ────────────────────────────────────────────────────────

async def scrape_jamef(numero_nf: str, cnpj: str) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"]
        )
        page = await browser.new_page()

        try:
            # 1. Acessar o site
            await page.goto("https://www.jamef.com.br/", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # 2. Preencher NF e pesquisar
            await page.wait_for_selector('input[placeholder*="nota"]', timeout=10000)
            await page.fill('input[placeholder*="nota"]', numero_nf)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(3000)

            # 3. Preencher CNPJ e pesquisar
            await page.wait_for_selector('input[placeholder*="CPF"]', timeout=10000)
            await page.fill('input[placeholder*="CPF"]', cnpj)
            await page.click('button[type="submit"]')
            await page.wait_for_url("**/rastrear/**", timeout=15000)
            await page.wait_for_timeout(2000)

            # 4. Capturar dados da página de resultado
            dados_pagina = await page.evaluate("""
                () => {
                    const getText = (selector) => {
                        const el = document.querySelector(selector);
                        return el ? el.textContent.trim() : null;
                    };

                    // Previsão de entrega: busca o <span> dentro do elemento
                    let previsao = null;
                    for (const el of document.querySelectorAll('*')) {
                        if (el.childElementCount === 1 &&
                            el.textContent.includes('Previsão de Entrega:')) {
                            const span = el.querySelector('span');
                            if (span) { previsao = span.textContent.trim(); break; }
                        }
                    }

                    // Origem e Destino
                    const headings = [...document.querySelectorAll('h3, h4, strong, b')];
                    let origem = null, destino = null;
                    for (const h of headings) {
                        if (h.textContent.trim() === 'Origem')
                            origem = h.nextElementSibling?.textContent.trim() ?? null;
                        if (h.textContent.trim() === 'Destino')
                            destino = h.nextElementSibling?.textContent.trim() ?? null;
                    }

                    return { previsao, origem, destino };
                }
            """)

            # 5. Abrir pop-up Histórico
            await page.click('button.button.bg-red')
            await page.wait_for_selector('.popup-content .content', timeout=8000)
            await page.wait_for_timeout(1000)

            # 6. Capturar histórico do pop-up
            historico = await page.evaluate("""
                () => {
                    const content = document.querySelector('.popup-content .content');
                    if (!content) return [];

                    const keyMap = {
                        'Data':              'data',
                        'Status':            'status',
                        'Estado origem':     'estado_origem',
                        'Município origem':  'municipio_origem',
                        'Estado destino':    'estado_destino',
                        'Município destino': 'municipio_destino'
                    };

                    const entries = [];
                    let current = {};

                    for (const p of content.querySelectorAll('p')) {
                        const bold = p.querySelector('b');
                        if (!bold) continue;
                        const rawKey = bold.textContent.replace(':', '').trim();
                        const field  = keyMap[rawKey];
                        const value  = p.textContent.replace(bold.textContent, '').trim();
                        if (rawKey === 'Data') {
                            if (Object.keys(current).length > 0) entries.push(current);
                            current = {};
                        }
                        if (field) current[field] = value;
                    }
                    if (Object.keys(current).length > 0) entries.push(current);
                    return entries;
                }
            """)

            # Status atual = primeiro item do histórico
            status_atual = historico[0].get("status") if historico else None

            return {
                "nf": numero_nf,
                "origem": dados_pagina.get("origem"),
                "destino": dados_pagina.get("destino"),
                "previsao_entrega": dados_pagina.get("previsao"),
                "status_atual": status_atual,
                "historico": historico
            }

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Erro no scraping: {str(e)}")

        finally:
            await browser.close()


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "message": "Jamef Rastreamento API rodando"}


@app.get("/rastrear/{numero_nf}", response_model=ResultadoRastreamento)
async def rastrear(numero_nf: str, cnpj: str = CNPJ_PADRAO):
    """
    Rastreia uma NF na Jamef.
    - **numero_nf**: número da nota fiscal
    - **cnpj**: CNPJ do remetente (opcional, usa o padrão se omitido)
    """
    resultado = await scrape_jamef(numero_nf, cnpj)
    return resultado
