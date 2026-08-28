# Estrutura do Portal Financeiro Da Terrinha

> Mapa de arquitetura do código: onde cada coisa mora e o que cada módulo faz. Complementa
> `docs/historico_desenvolvimento.md` (decisões e linha do tempo) e `docs/mapeamento_ui.md`
> (seletores/URLs do Bluesoft). Gerado em 2026-08-25 lendo o código real do projeto - se algo aqui
> não bater com o que você vê no arquivo, o arquivo é que vale.

## 0. Visão geral

App único **Streamlit** (`app.py`, ~1550 linhas), multi-página via `st.navigation`, com toda a
lógica de negócio/automação em módulos separados dentro de `src/notas_devolucao/`. Não tem
backend/API própria - o Streamlit é o servidor, e os dados vêm de duas fontes:

1. **Bluesoft** (ERP do cliente), via automação Playwright (screen-scraping, sem API oficial) -
   ver `auth.py`/`navigation.py`/`extraction.py`/`factoring.py`.
2. **SQLite local** (`dados/notas_devolucao.db`), que funciona como cache - o app nunca lê o
   Bluesoft ao vivo pra montar a tela; sempre lê do banco, e um botão explícito ("Atualizar dados
   do Bluesoft" ou "Verificar boletos de factoring") dispara a sincronização que atualiza o banco.

Uma terceira fonte, só-leitura, é a planilha externa de Antecipação (`antecipacao.py`).

## 1. Como rodar

- `1 - ABRIR APP.bat` na raiz - ativa o `.venv` e roda `streamlit run app.py`.
- **F5 na página** já pega mudanças em `app.py`. Mudanças em qualquer arquivo de
  `src/notas_devolucao/` exigem **reiniciar o processo inteiro** (matar e rodar o `.bat` de novo) -
  Python cacheia os módulos importados.
- Configuração de instância em `config/settings.toml` (não versionado - copiar de
  `config/settings.example.toml`). Campos: `[busca] data_inicial/data_final`, `[intercompany]
  nomes`, `[auth] usuario/senha_hash`, `[antecipacao] caminho_planilha` (opcional).
- Sessão do Bluesoft fica cacheada em `storage_state.json` (raiz, fora do git) - login manual
  assistido na primeira vez que expirar.

## 2. Árvore do projeto

```
app.py                              — todas as 5 páginas Streamlit + funções de UI/tabela/Excel
.streamlit/config.toml              — tema visual (cores da marca, fonte Montserrat)
config/
  settings.toml                     — config real da instância (fora do git)
  settings.example.toml             — template
docs/
  historico_desenvolvimento.md      — decisões e linha do tempo
  mapeamento_ui.md                  — seletores/URLs do Bluesoft
  estrutura_portal.md               — este arquivo
src/notas_devolucao/
  config.py                         — Configuracao (dataclass) + carregar_configuracao()
  models.py                         — dataclasses: NotaDevolucao, ContaPagar, BoletoFactoring, Vinculo
  auth.py                           — login manual assistido no Bluesoft (Playwright)
  navigation.py                     — navegar/filtrar/paginar as telas do Bluesoft
  selectors.py                      — locators Playwright reaproveitados por navigation.py
  extraction.py                     — ler o model Angular da tela e virar NotaDevolucao/ContaPagar
  relatorio_abatimento.py           — extrair notas quitadas + detalhe de abatimento (aba Abatimentos)
  factoring.py                      — todo o pipeline de detecção de boleto pago via factoring
  ocr.py                            — fallback OCR (pytesseract) quando o PDF não tem texto nativo
  filtros.py                        — eh_intercompany() (exclui empresas do próprio grupo)
  sincronizacao.py                  — orquestra auth+navigation+extraction, grava no storage
  storage.py                        — camada SQLite (schema + CRUD de tudo)
  antecipacao.py                    — lê a planilha externa de antecipação (só leitura)
  email_cobranca.py                 — monta e-mail de cobrança e abre como rascunho no Outlook (COM)
  login.py                          — login/senha de acesso ao próprio portal (SHA-256)
dados/notas_devolucao.db            — SQLite, gerado em runtime (fora do git)
anexos_baixados/                    — PDFs de boleto baixados durante a checagem de factoring
assets/logo_daterrinha.png          — logo usado no header e no e-mail de cobrança
storage_state.json                  — sessão Playwright do Bluesoft (fora do git)
```

## 3. `app.py` — páginas e fluxo

Registro fixo de páginas via `st.navigation` (sempre as 5, mesmo pra quem não está logado - só a
visibilidade no menu muda; a proteção de verdade é `exigir_login()` no topo do arquivo, chamada
pra qualquer página exceto `/antecipacao`):

| Página | Função | `url_path` | Precisa login | Botão de sync próprio |
|---|---|---|---|---|
| Home | `pagina_home()` | (raiz, default) | sim | não (só lê o banco) |
| Fornecedores | `pagina_fornecedores()` | — | sim | "Atualizar dados do Bluesoft" |
| Dashboard | `pagina_dashboard()` | — | sim | "Atualizar dados do Bluesoft" |
| Pagamentos via Boleto / Factoring | `pagina_factoring()` | — | sim | "Verificar boletos de factoring" |
| Antecipação de Fornecedores | `pagina_antecipacao()` | `antecipacao` | **não** | não (só lê a planilha) |

Funções auxiliares principais em `app.py` (todas privadas, prefixo `_`):

- **Preparação de dados:** `_preparar_dados_devolucao()` (Fornecedores/Dashboard: filtro de loja +
  período + separa pendentes reais vs. já vinculadas) e `_resumo_devolucao()` (versão sem widgets,
  só pra Home, período/loja fixos da config).
- **Tabelas (`pd.DataFrame`/`Styler`):** `_tabela_notas`, `_tabela_contas`,
  `_tabela_notas_vinculadas`, `_tabela_notas_quitadas`, `_ranking_fornecedores`, `_tabela_boletos`,
  `_tabela_ranking_fornecedores_factoring`, `_tabela_tendencia_factoring`,
  `_tabela_mensal_antecipacao`, `_tabela_ranking_antecipacao`.
- **Exportação Excel (`io.BytesIO` + `openpyxl`):** `_gerar_excel` (Fornecedores, 2 abas),
  `_gerar_excel_factoring`, `_gerar_excel_antecipacao`, `_gerar_excel_resumo_executivo` (Home, 1
  linha por indicador de cada área).
- **Agrupamento:** `_agrupar_por_fornecedor()` (chave = CNPJ, junta notas + contas + quitadas num
  dict por fornecedor - base de quase toda a página Fornecedores e do ranking do Dashboard).
- **Estilo condicional:** `_cor_atraso()` (linha de nota por dias de atraso), `_cor_status_factoring()`.

Gráficos são todos Altair (`alt.Chart`), com o mesmo padrão de interatividade tipo Power BI:
`alt.selection_point` + `on_select="rerun"` no `st.altair_chart`, lendo o clique de volta em
`resultado["selection"]` pra filtrar as tabelas abaixo do gráfico (cross-filtering). Padrão visual
de rótulo em cima da barra: `_ROTULO_VALOR_BARRA` (dict reaproveitado em todo gráfico de barra).

Tabelas do Dashboard/Antecipação usam `st_aggrid.AgGrid` (filtro Excel-style, tema `"streamlit"`)
em vez de `st.dataframe` simples, com `JsCode` para formatar moeda/percentual em pt-BR.

## 4. `src/notas_devolucao/` — módulos de automação e domínio

### `models.py` — as 4 entidades do domínio
- `NotaDevolucao` — uma nota de devolução (Contas a Receber). Campo-chave:
  `duplicata_pagamento_vinculada` (`str | None`) — quando preenchido, o Bluesoft já considera essa
  nota abatida contra aquela duplicata de pagamento, e ela sai da lista de "pendentes reais".
- `ContaPagar` — uma conta a pagar do mesmo fornecedor (Contas a Pagar).
- `BoletoFactoring` — resultado da checagem de uma duplicata paga: `status`
  (`factoring`/`normal`/`nao_verificado`/`ignorado`), CNPJs encontrados no boleto,
  `trecho_beneficiario` (motivo da falha, quando não confirmado), `pago_via_edi`.
- `Vinculo` — abatimento manual criado no app (não vem do Bluesoft), `status`
  `pendente`/`abatido`.

### `config.py` — carregamento de configuração
`Configuracao` (dataclass) lida de `config/settings.toml`; centraliza também os paths fixos do
projeto (`DB_PATH`, `STORAGE_STATE_PATH`, `ANEXOS_DIR`) e a URL base do Bluesoft
(`BLUESOFT_BASE_URL = "https://erp.bluesoft.com.br/daterrinha"`).

### `auth.py` — login no Bluesoft
Login sempre **manual assistido** (nunca guarda usuário/senha do Bluesoft): `abrir_navegador()` →
`criar_contexto()` (reaproveita `storage_state.json` se existir) → `garantir_login()` checa
`sessao_esta_ativa()` (ausência de `input[type=password]`) e, se não, `aguardar_login_manual()`
espera até 3 min a pessoa logar na janela do Chromium. `_desligar_pausas_debugger()` existe
especificamente por causa do `debugger;` do Bluesoft que já travou a automação (ver histórico).

### `navigation.py` — navegar e paginar as telas do Bluesoft
Abre Contas a Pagar/Contas a Receber (dentro de `<iframe>`), aplica filtros (loja, tipo de
duplicata via widget select2, período), roda a busca Sintética, e **pagina até carregar tudo**
(`carregar_todas_paginas()` — usa `scope.vm || scope` por causa do bug de paginação documentado no
histórico; Contas a Receber não expõe `vm`). `obter_total_informado()` lê o rodapé "Total X
duplicatas" pra auto-checagem contra o que foi extraído. `verificar_vinculo_pagamento()` visita a
tela de detalhe de cada nota pra achar `duplicata_pagamento_vinculada`.

### `selectors.py` — locators Playwright reaproveitados
Só funções `Frame → Locator`, sem lógica; usadas por `navigation.py`. Prefixo `cap_` = Contas a
Pagar, `car_` = Contas a Receber.

### `extraction.py` — DOM/Angular → dataclass
Lê `angular.element(tr).scope()` linha por linha da grid renderizada e monta `NotaDevolucao`
(`extrair_notas_devolucao`) / `ContaPagar` (`extrair_contas_a_pagar`).

### `relatorio_abatimento.py` — notas já quitadas e valor exato do abatimento
`extrair_notas_quitadas()` (Contas a Receber filtrando por `DATA_QUITACAO`) e
`obter_abatimentos()` (visita a duplicata de PAGAMENTO, aba Abatimentos, grid virtualizada
`ui-grid` — pega o valor exato abatido por nota, que pode ser parcial).

### `factoring.py` — pipeline de detecção de boleto pago via factoring
O módulo mais denso (442 linhas). Fluxo por duplicata (`verificar_duplicata`):
`_localizar_linhas_candidatas` (todos os anexos, "boleto" no nome primeiro) →
`visualizar_e_capturar_anexo` (baixa o PDF/imagem) → `_texto_e_classificacao_do_anexo` (texto
nativo via `pdfplumber`, cai pra OCR se os sinais não aparecerem) →
`extrair_cnpjs_beneficiario` + `detectar_indicio_factoring` (regex de CNPJ + nomes de
FIDC/Fomento Mercantil/Securitizadora) → compara raiz de 8 dígitos do CNPJ (`_raiz_cnpj`) com o
cadastrado. `verificar_pagamento_via_edi()` lê a tela de Ocorrências como sinal extra (não decide
status sozinho). `dividir_em_janelas()` quebra o período de busca em blocos de ~31 dias.

### `ocr.py` — fallback de leitura de imagem
`_configurar_tesseract()`, `extrair_texto_via_ocr_pdf()` (renderiza página a 300dpi e roda OCR),
`extrair_texto_via_ocr_imagem()`. Acionado por `factoring.py` quando o texto nativo do PDF não tem
os sinais esperados (mesmo não estando vazio - PDF corrompido de certos bancos).

### `filtros.py`
`eh_intercompany(nome, termos)` — compara por palavra inteira (não substring), usado pra excluir
empresas do próprio grupo Daterrinha cadastradas como "fornecedor".

### `sincronizacao.py` — orquestração
`atualizar_dados()` — login + extrai notas de devolução + contas a pagar + quitadas + verifica
vínculo de pagamento, grava tudo no storage, devolve avisos. `verificar_boletos_factoring()` —
mesma ideia, mas escopo/período próprios (todas as lojas pagadoras) e incremental (pula duplicata
já verificada, via `storage.chaves_ja_verificadas_factoring()`). `_checar_total_informado()` — a
auto-checagem permanente contra `navigation.obter_total_informado()`.

### `storage.py` — camada SQLite
`DB_PATH = dados/notas_devolucao.db`. Tabelas: `notas_devolucao`, `contas_pagar`,
`notas_devolucao_quitadas`, `vinculos`, `sincronizacao_info` (timestamp da última sync),
`contatos_fornecedor` (e-mail salvo por CNPJ), `boletos_factoring`. `inicializar()` roda todo
`CREATE TABLE IF NOT EXISTS` + `ALTER TABLE` incrementais pra colunas novas (ex.: `pago_via_edi`).
Funções `carregar_*`/`salvar_*`/`remover_*` por entidade; `marcar_status_manual_factoring()` e
`remover_verificacao_factoring()` sustentam a correção manual e o "forçar reverificar" da página
Factoring.

### `antecipacao.py` — planilha externa (só leitura)
`Antecipacao` (dataclass) + `carregar_antecipacoes(caminho)` — lê a aba "report" da planilha do
financeiro **por posição de coluna**, não pelo nome do cabeçalho (os cabeçalhos vêm com acento
corrompido na origem). Nunca escreve no arquivo.

### `email_cobranca.py` — e-mail de cobrança
Monta corpo HTML + tabela (`_corpo_html`/`_tabela_html`) e `abrir_no_outlook()` abre como
**rascunho** via COM (`win32com.client`, `mail.Display(False)` — nunca `True`, trava o processo
inteiro). Não envia sozinho; anexa o logo (precisa de caminho absoluto).

### `login.py` — acesso ao próprio portal
`exigir_login(config)` — usuário/senha fixos vindos de `settings.toml` (`auth_usuario`,
`auth_senha_hash` em SHA-256 via `_hash()`). Guarda `st.session_state["autenticado"]`.

## 5. Onde cada bug/decisão documentada no histórico vive no código

Só apontando o arquivo, os detalhes completos (motivo, como foi validado) ficam em
`docs/historico_desenvolvimento.md`:

- Paginação incompleta silenciosa → `navigation.carregar_todas_paginas()` + auto-checagem em
  `sincronizacao._checar_total_informado()`.
- Duplicata sem anexo / motivo específico de falha → `factoring._texto_e_classificacao_do_anexo()`
  + status `"ignorado"` em `models.BoletoFactoring.status`.
- OCR não disparava com PDF corrompido não-vazio → `factoring._texto_e_classificacao_do_anexo()`.
- Nem todo anexo tem "boleto" no nome → `factoring._localizar_linhas_candidatas()`.
- Sinal "Pago via EDI" → `factoring.verificar_pagamento_via_edi()` +
  `models.BoletoFactoring.pago_via_edi`.
- `debugger;` trava a automação → `auth._desligar_pausas_debugger()`.
- Campo nativo "Factoring" do Bluesoft (pendência, não investigado) → não tem código ainda.

Contexto de automação Bluesoft cross-projeto (login, cookies, gotchas gerais de UI AngularJS) está
em `CONHECIMENTO_BLUESOFT.md` na Área de Trabalho do usuário, não duplicado aqui.
