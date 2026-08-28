from __future__ import annotations

import logging

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from .config import BLUESOFT_BASE_URL, STORAGE_STATE_PATH

logger = logging.getLogger("notas_devolucao.auth")

URL_LOGIN = f"{BLUESOFT_BASE_URL}/login"


def abrir_navegador(headless: bool = False):
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=headless)
    return playwright, browser


def _desligar_pausas_debugger(page: Page) -> None:
    """O Bluesoft parece ter um `debugger;` no JS que, como o Playwright controla o Chromium via
    CDP, pausa a execução de verdade ("Debugger paused in another tab") - travando a automação
    silenciosamente (sem erro nenhum) até alguém destravar manualmente. Relevante principalmente
    no fluxo de checagem de factoring (muitas páginas de duplicata abertas em sequência)."""
    try:
        cdp = page.context.new_cdp_session(page)
        cdp.send("Debugger.enable")
        cdp.send("Debugger.setSkipAllPauses", {"skip": True})
    except Exception as e:
        logger.warning("Não consegui desligar pausas de depuração para a página %s: %r", page.url, e)


def criar_contexto(browser: Browser) -> BrowserContext:
    if STORAGE_STATE_PATH.exists():
        contexto = browser.new_context(storage_state=str(STORAGE_STATE_PATH))
    else:
        contexto = browser.new_context()
    contexto.on("page", _desligar_pausas_debugger)
    return contexto


def sessao_esta_ativa(page: Page) -> bool:
    page.goto(f"{BLUESOFT_BASE_URL}/erp-app/areas/core/menu-central/menu-central.index.jsp")
    page.wait_for_load_state("networkidle")
    # goto sempre "sucede" mesmo se o Bluesoft redirecionar para a tela de login (SPA) -
    # o único jeito confiável de saber se logou é checar se NÃO há campo de senha na tela.
    return page.locator('input[type="password"]').count() == 0


def aguardar_login_manual(page: Page, contexto: BrowserContext, timeout_ms: int = 180_000) -> None:
    """Abre a tela de login e espera o usuário logar manualmente (sem ver/pedir senha)."""
    page.goto(URL_LOGIN)
    logger.info("Aguardando login manual no navegador (até %s segundos)...", timeout_ms // 1000)

    intervalo_ms = 2000
    tentativas = timeout_ms // intervalo_ms
    for _ in range(tentativas):
        if page.locator('input[type="password"]').count() == 0:
            contexto.storage_state(path=str(STORAGE_STATE_PATH))
            logger.info("Login confirmado, sessão salva para reuso.")
            return
        page.wait_for_timeout(intervalo_ms)

    raise TimeoutError("Login manual não foi concluído dentro do tempo limite.")


def garantir_login(contexto: BrowserContext, page: Page) -> None:
    if sessao_esta_ativa(page):
        logger.info("Sessão salva ainda válida, login não é necessário.")
        return
    aguardar_login_manual(page, contexto)
