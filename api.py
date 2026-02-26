from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from playwright.async_api import async_playwright
from pydantic import BaseModel
from typing import Optional
import asyncio
import uuid
import time

app = FastAPI(title="Jamef Rastreamento API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

CNPJ_PADRAO = "48775191000190"

# Timeouts para ambiente cloud
TIMEOUT_SELECTOR  = 30_000
TIMEOUT_NAVEGACAO = 45_000
TIMEOUT_POPUP     = 20_000
WAIT_CURTO        = 2_000
WAIT_LONGO        = 4_000

# ── Storage in-memory de jobs ─────────────────────────────────────────────────
# { job_id: { status, result, error, created_at } }
jobs: dict = {}

def limpar_jobs_antigos():
    """Remove jobs com mais de 1 hora para evitar vazamento de memória."""
    agora = time.time()
    expirados = [jid for jid, j in jobs.items() if agora - j["created_at"] > 3600]
    for jid in expirados:
        del jobs[jid]


# ── Modelos ───────────────────────────────────────────────────────────────────

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

class JobIniciado(BaseModel):
    job_id: str
    status: str
    message: str

class JobStatus(BaseModel):
    job_id: str
    status: str                          # "processing" | "done" | "error"
    result: Optional[ResultadoRastreamento] = None
    error: Optional[str] = None


# ── Lógica de scraping ────────────────────────────────────────────────────────

async def scrape_jamef(numero_nf: str, cnpj: str) -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
        )
        page = await browser.new_page()

        try:
            await page.goto(
                "https://www.jamef.com.br/",
                wait_until="domcontentloaded",
                timeout=TIMEOUT_NAVEGACAO
            )
            await page.wait_for_timeout(WAIT_CURTO)

            await page.wait_for_selector('input[placeholder*="nota"]', timeout=TIMEOUT_SELECTOR)
            await page.fill('input[placeholder*="nota"]', numero_nf)
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(WAIT_LONGO)

            await page.wait_for_selector('input[placeholder*="CPF"]', timeout=TIMEOUT_SELECTOR)
            await page.fill('input[placeholder*="CPF"]', cnpj)
            await page.click('button[type="submit"]')
            await page.wait_for_url("**/rastrear/**", timeout=TIMEOUT_NAVEGACAO)
            await page.wait_for_timeout(WAIT_LONGO)

            dados_pagina = await page.evaluate("""
                () => {
                    let previsao = null;
                    for (const el of document.querySelectorAll('*')) {
                        if (el.childElementCount === 1 &&
                            el.textContent.includes('Previsão de Entrega:')) {
                            const span = el.querySelector('span');
                            if (span) { previsao = span.textContent.trim(); break; }
                        }
                    }
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

            await page.click('button.button.bg-red')
            await page.wait_for_selector('.popup-content .content', timeout=TIMEOUT_POPUP)
            await page.wait_for_timeout(WAIT_CURTO)

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

            return {
                "nf": numero_nf,
                "origem": dados_pagina.get("origem"),
                "destino": dados_pagina.get("destino"),
                "previsao_entrega": dados_pagina.get("previsao"),
                "status_atual": historico[0].get("status") if historico else None,
                "historico": historico
            }

        finally:
            await browser.close()


async def executar_job(job_id: str, numero_nf: str, cnpj: str):
    """Roda o scraping em background e salva o resultado no dicionário de jobs."""
    try:
        resultado = await scrape_jamef(numero_nf, cnpj)
        jobs[job_id]["status"] = "done"
        jobs[job_id]["result"] = resultado
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"]  = str(e)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "ok", "message": "Jamef Rastreamento API rodando"}


@app.get("/rastrear/{numero_nf}", response_model=JobIniciado)
async def rastrear(
    numero_nf: str,
    background_tasks: BackgroundTasks,
    cnpj: str = CNPJ_PADRAO
):
    """
    Inicia o rastreamento de uma NF em background.
    Retorna um job_id — use GET /status/{job_id} para obter o resultado.
    """
    limpar_jobs_antigos()

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status": "processing",
        "result": None,
        "error": None,
        "created_at": time.time()
    }

    background_tasks.add_task(executar_job, job_id, numero_nf, cnpj)

    return {
        "job_id": job_id,
        "status": "processing",
        "message": f"Consulta da NF {numero_nf} iniciada. Verifique o resultado em /status/{job_id}"
    }


@app.get("/status/{job_id}", response_model=JobStatus)
def status(job_id: str):
    """
    Retorna o status de um job de rastreamento.
    - processing → ainda executando (faça polling a cada 5s)
    - done        → resultado disponível em .result
    - error       → erro disponível em .error
    """
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job não encontrado ou expirado")

    j = jobs[job_id]
    return {
        "job_id": job_id,
        "status": j["status"],
        "result": j["result"],
        "error":  j["error"],
    }
