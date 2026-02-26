from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import asyncio
import uuid
import time
import os
import httpx

app = FastAPI(title="Jamef Rastreamento API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

CNPJ_PADRAO = "48775191000190"

# Configurações via variáveis de ambiente (defina no Render: Settings > Environment)
JAMEF_USERNAME = os.getenv("JAMEF_USERNAME", "")
JAMEF_PASSWORD = os.getenv("JAMEF_PASSWORD", "")
JAMEF_AUTH_URL  = os.getenv("JAMEF_AUTH_URL",  "https://api.jamef.com.br/auth/v1/login")
JAMEF_RASTR_URL = os.getenv("JAMEF_RASTR_URL", "https://api.jamef.com.br/consulta/v1/rastreamento")

# Cache do token JWT em memória
_token: dict = {"value": None, "expires_at": 0.0}

# Storage in-memory de jobs { job_id: { status, result, error, created_at } }
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
    status: str          # "processing" | "done" | "error"
    result: Optional[ResultadoRastreamento] = None
    error: Optional[str] = None


# ── Autenticação com cache ────────────────────────────────────────────────────

async def obter_token() -> str:
    """Retorna token JWT válido, renovando automaticamente se necessário."""
    agora = time.time()
    # Reutiliza token se ainda válido por mais de 5 minutos
    if _token["value"] and agora < _token["expires_at"] - 300:
        return _token["value"]

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            JAMEF_AUTH_URL,
            json={"username": JAMEF_USERNAME, "password": JAMEF_PASSWORD},
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        data = resp.json()
        token      = data["dado"][0]["accessToken"]
        expires_in = data["dado"][0].get("expiresIn", 3600)
        _token["value"]      = token
        _token["expires_at"] = agora + expires_in

    return _token["value"]


# ── Consulta à API oficial Jamef ──────────────────────────────────────────────

async def consultar_jamef(numero_nf: str, cnpj: str) -> dict:
    token = await obter_token()

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            JAMEF_RASTR_URL,
            params={
                "documentoRemetente": cnpj,
                "numeroNotaFiscal":   numero_nf,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()

    data = resp.json()
    rastreamentos = data.get("dado", [{}])[0].get("rastreamento", [])

    if not rastreamentos:
        raise ValueError(f"Nenhum rastreamento encontrado para NF {numero_nf}")

    r      = rastreamentos[0]
    rem    = r.get("remetente",   {})
    dest   = r.get("destinatario", {})
    frete  = r.get("frete",        {})
    eventos = r.get("eventosRastreio", [])

    origem  = f"{rem.get('cidade','')}-{rem.get('uf','')}"   if rem.get("cidade")  else None
    destino = f"{dest.get('cidade','')}-{dest.get('uf','')}" if dest.get("cidade") else None

    historico = [
        {
            "data":             ev.get("data"),
            "status":           ev.get("status"),
            "estado_origem":    ev.get("localOrigem",  {}).get("uf"),
            "municipio_origem": ev.get("localOrigem",  {}).get("cidade"),
            "estado_destino":   ev.get("localDestino", {}).get("uf"),
            "municipio_destino":ev.get("localDestino", {}).get("cidade"),
        }
        for ev in eventos
    ]

    return {
        "nf":               numero_nf,
        "origem":           origem,
        "destino":          destino,
        "previsao_entrega": frete.get("previsaoEntrega"),
        "status_atual":     historico[0]["status"] if historico else None,
        "historico":        historico,
    }


async def executar_job(job_id: str, numero_nf: str, cnpj: str):
    """Roda a consulta em background e salva o resultado no dicionário de jobs."""
    try:
        resultado = await consultar_jamef(numero_nf, cnpj)
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
    cnpj: str = CNPJ_PADRAO,
):
    """
    Inicia a consulta de uma NF em background.
    Retorna job_id — use GET /status/{job_id} para obter o resultado.
    """
    limpar_jobs_antigos()

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        "status":     "processing",
        "result":     None,
        "error":      None,
        "created_at": time.time(),
    }

    background_tasks.add_task(executar_job, job_id, numero_nf, cnpj)

    return {
        "job_id":  job_id,
        "status":  "processing",
        "message": f"Consulta da NF {numero_nf} iniciada. Verifique o resultado em /status/{job_id}",
    }


@app.get("/status/{job_id}", response_model=JobStatus)
def status(job_id: str):
    """
    Retorna o status de um job de rastreamento.
    - processing → ainda executando (faça polling a cada 3s)
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
