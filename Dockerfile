# Imagem oficial do Playwright para Python — já tem Chromium + todas as libs de sistema
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

# Instala dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código da aplicação
COPY . .

# Render injeta a variável PORT dinamicamente
CMD uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}
