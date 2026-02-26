import asyncio
import json
from playwright.async_api import async_playwright

# ─── CONFIGURAÇÃO ────────────────────────────────────────────────────────────
CNPJ = "48775191000190"
# ─────────────────────────────────────────────────────────────────────────────


async def consulta_jamef(numero_nf: str):
    print(f"\n{'='*60}")
    print(f"  Consultando NF: {numero_nf}")
    print(f"{'='*60}\n")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        page = await browser.new_page()

        try:
            # 1. Acessar o site
            print("[1/7] Acessando https://www.jamef.com.br/ ...")
            await page.goto("https://www.jamef.com.br/", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # 2. Preencher número da NF
            print(f"[2/7] Preenchendo NF: {numero_nf} ...")
            await page.wait_for_selector('input[placeholder*="nota"]', timeout=10000)
            await page.fill('input[placeholder*="nota"]', numero_nf)

            # 3. Clicar em Pesquisar
            print("[3/7] Clicando em Pesquisar ...")
            await page.click('button[type="submit"]')
            await page.wait_for_timeout(3000)

            # 4. Preencher CPF/CNPJ
            print(f"[4/7] Preenchendo CPF/CNPJ: {CNPJ} ...")
            await page.wait_for_selector('input[placeholder*="CPF"]', timeout=10000)
            await page.fill('input[placeholder*="CPF"]', CNPJ)

            # 5. Clicar em Pesquisar novamente
            print("[5/7] Clicando em Pesquisar novamente ...")
            await page.click('button[type="submit"]')
            await page.wait_for_url("**/rastrear/**", timeout=15000)
            await page.wait_for_timeout(2000)

            # 6. Capturar Previsão de Entrega
            print("[6/7] Capturando Previsão de Entrega ...")
            previsao = await page.evaluate("""
                () => {
                    const all = document.querySelectorAll('*');
                    for (const el of all) {
                        if (el.childElementCount === 1 &&
                            el.textContent.includes('Previsão de Entrega:')) {
                            const span = el.querySelector('span');
                            if (span) return span.textContent.trim();
                        }
                    }
                    return null;
                }
            """)

            # 7. Clicar no botão Histórico e capturar pop-up
            print("[7/7] Clicando em Histórico e capturando dados ...")
            await page.click('button.button.bg-red')
            await page.wait_for_selector('.popup-content .content', timeout=8000)
            await page.wait_for_timeout(1000)

            historico = await page.evaluate("""
                () => {
                    const content = document.querySelector('.popup-content .content');
                    if (!content) return [];

                    const paragraphs = content.querySelectorAll('p');
                    const entries = [];
                    let current = {};

                    const keyMap = {
                        'Data':             'data',
                        'Status':           'status',
                        'Estado origem':    'estado_origem',
                        'Município origem': 'municipio_origem',
                        'Estado destino':   'estado_destino',
                        'Município destino':'municipio_destino'
                    };

                    for (const p of paragraphs) {
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

            # ── Exibir resultado ──────────────────────────────────────────
            print(f"\n{'='*60}")
            print(f"  RESULTADO - NF {numero_nf}")
            print(f"{'='*60}")
            print(f"  Previsão de Entrega: {previsao}\n")
            print(f"  Histórico completo ({len(historico)} registros):")
            print(json.dumps(historico, ensure_ascii=False, indent=2))
            print(f"{'='*60}\n")

            return {
                "nf": numero_nf,
                "previsao_entrega": previsao,
                "historico": historico
            }

        except Exception as e:
            print(f"\n[ERRO] {e}")
            raise

        finally:
            input("\nPressione ENTER para fechar o navegador...")
            await browser.close()


if __name__ == "__main__":
    numero_nf = input("Digite o número da Nota Fiscal: ").strip()
    asyncio.run(consulta_jamef(numero_nf))
