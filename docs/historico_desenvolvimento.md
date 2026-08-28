# Histórico de desenvolvimento — Portal Financeiro Da Terrinha

> Arquivo de referência com o histórico de decisões, funcionalidades e correções feitas no portal,
> pra não se perder ao longo do tempo. Complementa o `docs/mapeamento_ui.md` (que tem os detalhes
> técnicos de seletores/URLs do Bluesoft).

## Visão geral

O que começou como um app simples de acompanhamento de devolução/abatimento de fornecedores (IT FCP
014) virou o **Portal Financeiro Da Terrinha**, com várias áreas:

- **Home** — resumo rápido das 4 áreas abaixo, com botão de exportar um resumo executivo em Excel.
- **Fornecedores** — tela original: vínculo entre notas de devolução pendentes e contas a pagar do
  mesmo fornecedor, com envio de e-mail de cobrança e vínculos manuais de abatimento.
- **Dashboard** — ranking de fornecedores por devolução (valor total ou quantidade de notas), com
  filtro Excel-style (AgGrid) e gráfico Top 10 clicável.
- **Pagamentos via Boleto / Factoring** — detecta quais boletos pagos foram na verdade pagos a uma
  factoring/financeira em vez do fornecedor cadastrado.
- **Antecipação de Fornecedores** — mostra o desconto financeiro ganho por antecipar pagamento a
  fornecedores, lido de uma planilha externa mantida pelo financeiro.

Todas as páginas rodam dentro do mesmo app Streamlit (`app.py`), com sincronização de dados feita
via automação Playwright do Bluesoft (login manual assistido, sem API oficial).

## Linha do tempo por área

### Fornecedores / Devolução (base do projeto)

- Extrai notas de devolução pendentes (Contas a Receber) e cruza com contas a pagar do mesmo
  fornecedor (Contas a Pagar), restrito às lojas Matriz e Filial SP.
- Detecta automaticamente quando o Bluesoft já marca uma nota como abatida contra uma duplicata de
  pagamento (`duplicata_pagamento_vinculada`), pra não contar como pendência de novo.
- Envio de e-mail de cobrança direto como rascunho no Outlook (via COM, `pywin32`) - nunca envia
  sozinho, sempre abre pra revisão manual antes.
- Exclusão de empresas do próprio grupo (2JM, Okker, Terrafec, Terrinha, FFAMM, Master, Save)
  cadastradas como "fornecedor" mas que são transferência interna.

**Produto de cada nota, lido direto do Bluesoft (2026-08-28):** cada nota de devolução agora mostra
o produto do item devolvido (ex.: "BOBINA OREGANO DA TERRINHA 200 G"), sem precisar cadastro manual.

- **Como funciona:** no mesmo laço do sync que já visita a tela de detalhe da duplicata de cobrança
  (`navigation.verificar_vinculo_pagamento`, pra checar vínculo de abatimento), lê-se também o
  número da **Fatura** vinculada (já vem na própria página, sem navegação extra —
  `navigation.obter_fatura_key`). A fatura lista a(s) **Nota(s) Fiscal(is)**, e cada nota fiscal tem
  uma tabela HTML estática `table#itens` ("Itens da nota fiscal") com a descrição do produto —
  `navigation.obter_produtos_da_fatura` faz essa navegação (fatura → nota fiscal) e extrai a
  descrição de cada item. Uma fatura pode ter mais de uma NF, e uma NF mais de um item (nesse caso o
  campo `NotaDevolucao.produtos` guarda todos separados por `"; "`).
- **Cache por fatura durante o sync:** várias notas de devolução costumam compartilhar a mesma
  fatura (o Bluesoft desmembra uma cobrança original em várias duplicatas conforme abate
  parcialmente — ver desmembramento abaixo), então o resultado é cacheado por `fatura_key` pra não
  repetir a navegação fatura→NF pra cada duplicata da mesma fatura.
- Depois disso, a tentativa inicial foi um campo "Produto" **editável manualmente** por fornecedor
  (tabela `produtos_fornecedor`, com input + botão "Salvar produto" na tela de Fornecedores) — o
  usuário pediu a automação acima no lugar e removeu esse campo manual em seguida (tabela antiga no
  banco não foi apagada, só a lógica no app ficou órfã).

**Desmembramento de nota em parcelas (2026-08-28):** o usuário percebeu que o Bluesoft desmembra a
duplicata de cobrança original em várias parcelas conforme vai tendo abatimento parcial (ex.:
fatura 1176150 → 5 duplicatas diferentes, todas da mesma NF 143.165-1). Isso fazia a mesma devolução
aparecer várias vezes na tabela do app, dando a falsa impressão de mais notas em aberto do que
realmente há. Corrigido consolidando por **NF** (`numero_documento`) em dois pontos:
- `_tabela_notas` (tabela "Notas de devolução pendentes" de cada fornecedor): uma linha por NF,
  somando o valor líquido das parcelas pendentes, mostrando o vencimento mais próximo entre elas e a
  quantidade de parcelas agrupadas. O formulário de "Registrar vínculo" continua operando na parcela
  individual (`notas_pendentes_grupo`), sem mudança - o desmembramento é real no Bluesoft, só a
  exibição resumo é que agrupa.
- Os contadores "Qtde. de notas de devolução" (Fornecedores) e "Notas pendentes" (Home) agora contam
  por `(fornecedor_cnpj, numero_documento)` único, não mais por `duplicata_key`.
- O "Resumo por produto" (ver abaixo) também soma "Qtde. de notas" por NF único, não por parcela.

**"Resumo por produto" + alerta de recorrência (2026-08-28):** na tela de Fornecedores, logo abaixo
dos cartões de total:
- Tabela clicável (`st.dataframe(..., on_select="rerun", selection_mode="single-row")`) agrupando
  todas as notas pendentes por produto, com quantidade de notas (por NF, ver acima) e valor
  pendente somado. Clicar numa linha mostra o(s) fornecedor(es) daquele produto
  (`_fornecedores_do_produto`).
- **Alerta de recorrência:** quando o mesmo produto aparece em devoluções de **2+ meses distintos**
  pra um fornecedor (`_produtos_recorrentes`, olha `data_emissao` de `grupo["notas"]`), o expander
  daquele fornecedor ganha um ⚠️ no título e um aviso detalhado dentro (ex.: "Bobina (3x em 2
  meses)") — sinal de possível problema recorrente de qualidade, não só atraso de cobrança pontual.
- **Formatação de moeda brasileira:** `st.column_config.NumberColumn` não suporta separador de
  milhar customizado (ficava "R$ 39680.00", sem separador nenhum) - criada `_formatar_moeda_br` que
  troca o formato padrão do Python (`"39,680.00"`) pro formato brasileiro (`"39.680,00"`) via troca
  de separadores em string. Como a tabela precisa ser um DataFrame puro (não Styler) pra suportar
  `on_select`, a coluna "Valor pendente" já sai formatada como texto do próprio
  `_resumo_por_produto`, não via `.style.format`.
- A tabela "Notas de devolução quitadas no período" (dentro de cada fornecedor) foi removida a
  pedido do usuário - considerada redundante com a visão de quitação que já existe no Dashboard.
- Coluna "Dias de atraso" removida da tabela de notas pendentes (junto com a função `_cor_atraso`,
  que só existia pra colorir essa coluna).

### Dashboard (ranking)

- Ranking por fornecedor com filtro Excel-style (AgGrid) e gráfico Top 10.
- Interatividade tipo Power BI: clicar numa barra do gráfico mostra um painel de detalhe do
  fornecedor selecionado (implementado com `alt.selection_point` do Altair).

### Pagamentos via Boleto / Factoring

Feature mais trabalhada do portal. Ideia: saber quais boletos pagos pela empresa foram na verdade
redirecionados pra uma factoring/financeira em vez do fornecedor cadastrado no Bluesoft.

**Como funciona:** pra cada duplicata já quitada (paga via boleto), abre a tela de detalhe, baixa o
anexo do boleto, extrai o texto (nativo via `pdfplumber`, com fallback OCR via `pytesseract`),
procura o CNPJ do beneficiário perto de um rótulo ("beneficiário"/"cedente"/"favorecido") ou um
nome característico de fundo de recebíveis (FIDC, Fomento Mercantil, Securitizadora), e compara com
o CNPJ cadastrado (só a raiz de 8 dígitos, pra não confundir filiais da mesma empresa).

**Bugs reais encontrados e corrigidos ao longo do projeto:**
1. **Paginação incompleta e silenciosa** — a tela de Contas a Receber usa um controller Angular
   diferente da tela de Contas a Pagar (métodos de paginação ficam direto no `scope`, não em
   `scope.vm`); a extração parava nas primeiras ~100-124 linhas sem erro nenhum. Corrigido, e
   virou uma **auto-checagem permanente**: o portal agora compara o total extraído com o total que
   o próprio Bluesoft informa na tela, avisando automaticamente se não bater.
2. **Duplicatas sem anexo nenhum** (não foram pagas via boleto de verdade) eram tratadas como
   "não confirmado" igual a um boleto ilegível de verdade. Corrigido: o motivo específico da falha
   agora aparece na coluna "Trecho do boleto", e existe um status manual **"Ignorado"** pra excluir
   de vez esses casos.
3. **Fonte de PDF corrompendo a extração nativa** (confirmado com boletos do banco SICOOB) — o
   texto sai cheio de caracteres, mas a palavra "Beneficiário" e a barra do CNPJ ficam ilegíveis.
   Corrigido: tenta OCR de novo quando a busca não acha nada no texto nativo, mesmo que ele não
   esteja vazio. Também relaxada a regex do CNPJ pra aceitar qualquer separador entre os grupos.
4. **Nem todo anexo de boleto tem "boleto" no nome do arquivo** — antes desistia se tivesse vários
   anexos e nenhum batesse esse filtro. Agora tenta todos os anexos, um por um, até achar um que
   pareça boleto de verdade.
5. **Sinal novo: "Pago via EDI"** (ideia do usuário) — pagamentos processados via EDI bancário
   tendem a ter o beneficiário validado pelo próprio banco. Lido da tela de "Ocorrências" de cada
   duplicata, mostrado como coluna informativa (não decide o status sozinho).
6. **Pendência em aberto:** foi avistado um campo nativo do Bluesoft chamado literalmente
   **"Factoring"** no formulário de edição de duplicata - não foi investigado se ele já guarda essa
   informação de forma mais direta. Vale checar numa sessão futura.

**Funcionalidades de correção manual:**
- Formulário individual e "em lote" (colar vários números de duplicata de uma vez) pra corrigir
  qualquer boleto - inclusive os já classificados como Factoring/Normal, não só os não confirmados.
- Botão de "Forçar reverificar" uma duplicata específica, ou todas as "não confirmadas" listadas de
  uma vez (remove o registro, reprocessa na próxima verificação daquele período).
- Ranking de "Fornecedores identificados em factoring" (agrupado, não boleto por boleto).
- Cross-filtering nos gráficos (clicar numa barra filtra as tabelas abaixo).

### Antecipação de Fornecedores

- Lê uma planilha externa mantida manualmente pelo financeiro
  (`DT - FINANCEIRO CP - CONFIRMAÇÂO DE ENTREGA\Report de antecipação dos fornecedores.xlsx`) - o
  portal só lê, nunca escreve nela. Caminho configurável em `config/settings.toml [antecipacao]`.
- Mostra valor líquido antecipado, desconto financeiro ganho e % médio, com gráfico mensal e
  ranking por fornecedor.
- Indicador de "planilha desatualizada" (mais de 35 dias sem modificação) e botão de recarregar.
- **Acesso sem login, só pra essa página** (pedido do usuário, pra compartilhar com alguém sem dar
  acesso ao resto do portal): a URL `/antecipacao` pula a tela de login, e o menu lateral fica
  escondido pra quem não está logado - as demais páginas continuam exigindo login normalmente
  mesmo por essa via. Link de acesso: `http://<ip-local-da-máquina>:8501/antecipacao`.
  - **Atenção:** esse link só funciona enquanto o portal estiver rodando nesta máquina e ela
    estiver ligada/na rede. O IP local pode mudar (já mudou de `10.4.1.67` pra `10.4.1.68` entre um
    dia e outro) - se o link parar de funcionar do nada, o motivo mais provável é esse.

## Ajustes visuais

- Tema de marca (`Área de Trabalho\Notas de devolução\.streamlit\config.toml`): laranja `#EE6931`,
  fonte Montserrat, cantos arredondados.
- Cabeçalho com gradiente sutil + sombra, cards de métrica com sombra e barra fina no topo (em vez
  de borda lateral grossa), divisores mais finos na cor da marca.
- Tema do AgGrid trocado de `"alpine"` (genérico) pra `"streamlit"` (herda as cores da marca).
- Rótulo de valor padronizado em cima de toda coluna de gráfico de barras.

## Controle de versão e modo "somente visualização"

**GitHub (2026-08-28):** o projeto virou repositório git (`git init` + primeiro commit), hospedado
em `https://github.com/Matheus44-10/portal-financeiro-da-terrinha` (privado). O `.gitignore` já
existia e cobria certinho tudo que não pode ser versionado (`storage_state.json`, `config/settings.toml`,
`dados/`, `anexos_baixados/`, `.venv/`) - conferido item a item antes do primeiro commit, mais uma
varredura por padrão de segredo hardcoded, nada encontrado. Autor dos commits configurado localmente
pelo usuário (`git config user.name/user.email`, nunca pelo Claude - é uma configuração de máquina).

**Modo "somente visualização" tipo Power BI (2026-08-28):** o usuário queria um link pra outras
pessoas só verem o portal (todas as páginas), sem poder mexer em nada, mantendo esta máquina como
"central" (só ela roda sync com Bluesoft e grava dado). Implementado como uma extensão do padrão que
já existia pra página de Antecipação (bypass de login por URL):
- `config/settings.toml [visualizacao] token = "..."` - um token secreto (gerado com
  `secrets.token_urlsafe(24)`). Link de acesso: `http://<ip-desta-máquina>:8501/<pagina>?chave=<token>`.
  Token vazio/ausente desativa o recurso inteiro.
- Ao carregar qualquer página com `?chave=` batendo o token (`hmac.compare_digest`), pula o login e
  liga `st.session_state["modo_leitura"]`. Toda ação que grava algo (`_modo_leitura()` checado antes)
  fica escondida: botão "Atualizar dados do Bluesoft", formulário de vínculo/e-mail do responsável,
  botão "Gerar e-mail de cobrança" (importante: esse botão dispara automação COM do Outlook **nesta
  máquina**, nunca no dispositivo de quem clica - teria que ficar escondido de visitante mesmo sem
  pedido explícito), edição de status de vínculo, e toda a seção de correção manual/reverificação de
  factoring. Dados, gráficos, downloads e filtros continuam liberados.
- **Armadilha real encontrada:** os links do menu lateral do `st.navigation` trocam de página sem
  manter `?chave=` na URL (viram link limpo tipo `/pagina_fornecedores`) - se `modo_leitura` fosse
  recalculado do zero em toda execução a partir da URL, o visitante caía na tela de login ao clicar
  em qualquer página do menu. Corrigido guardando o resultado em `session_state` com OR (`estado
  anterior or chave válida agora`), nunca rebaixando de True pra False - mesmo padrão que
  `autenticado` já usava.
- **Outra armadilha:** a visibilidade do menu lateral (`visibility="hidden"`/`"visible"` nos
  `st.Page`) checava só `session_state["autenticado"]`, então quem entrava em modo leitura via
  `?chave=` via o menu inteiro sumir (ficava preso numa página só, sem conseguir navegar). Corrigido
  pra checar `autenticado OU modo_leitura`.
- **Armadilha de ambiente (não é do código, é do Windows):** rodar `streamlit run app.py` sobe um
  processo pai + um processo filho (o filho é quem realmente escuta a porta 8501); matar só o
  processo que está "ouvindo" a porta (`Get-NetTCPConnection ... OwningProcess`) deixa o pai órfão
  rodando em segundo plano com o código antigo. Depois de várias reinicializações isso acumula
  processos zumbi e pode fazer parecer que uma mudança não "pegou" (ex.: `AttributeError` num campo
  que já existe no arquivo fonte). Solução: sempre matar TODOS os `python.exe` cujo `CommandLine`
  contenha `streamlit run app.py` antes de subir de novo, não só o dono da porta.

**Publicação na nuvem tipo Power BI (2026-08-28, mesmo dia):** o link `?chave=` acima só funciona
com o PC ligado. O usuário queria o equivalente real de um relatório *publicado* do Power BI -
acessível de qualquer lugar, mesmo com a máquina desligada. Implementado com Streamlit Community
Cloud (gratuito), reaproveitando o GitHub que a gente já tinha:

- **App na nuvem:** `https://financeiro-daterrinha.streamlit.app`, deployado a partir do mesmo
  repositório/branch `main`, arquivo principal `app.py`. Como o repositório é privado, o app nasce
  privado (Streamlit Cloud herda a visibilidade do repo) - restrito por e-mail em
  Settings → Sharing → "Only specific people can view this app" (hoje: `mara.financeiro@` e
  `andre.magalhaes@daterrinhaalimentos.com.br`). Confirmado ao vivo: sem estar na lista, a URL
  mostra "You do not have access" mesmo pra quem tem o link.
- **`config.MODO_NUVEM`** (`src/notas_devolucao/config.py`): liga via variável de ambiente
  `PORTAL_MODO_NUVEM=1`, configurada nas "Secrets" do app no Streamlit Cloud (confirmado na
  documentação oficial que secrets de nível raiz também viram variável de ambiente de verdade, não
  só `st.secrets`). Com isso ligado: `CONFIG_PATH` aponta pra `config/settings.cloud.toml` (versionado,
  sem segredo nenhum - login/senha ali nunca são usados de verdade) em vez de `settings.toml`, e
  `DB_PATH` aponta pra `dados_publicados/notas_devolucao.sqlite` (também versionado) em vez do banco
  real local. O app inteiro fica travado em `modo_leitura=True` e pula o login por completo - não
  existe automação de Bluesoft/Outlook possível rodando na nuvem mesmo, então não faz sentido nem
  mostrar tela de login lá.
- **Publicar dados** (`src/notas_devolucao/publicacao.py`, botão "☁️ Publicar para nuvem" na
  sidebar do app local, ao lado de "Atualizar dados do Bluesoft"): copia `dados/notas_devolucao.db`
  pra `dados_publicados/notas_devolucao.sqlite` e roda `git add` + `git commit` + `git push` (nome de
  arquivo diferente de propósito - `.sqlite` em vez de `.db` - pra não cair nas regras `dados/` e
  `*.db` do `.gitignore`, que são pro banco real, não pro retrato publicado). O push dispara redeploy
  automático do Streamlit Community Cloud em ~1 minuto. É uma ação deliberada e separada do sync
  normal (equivalente ao botão "Publicar" do Power BI Desktop) - sincronizar com o Bluesoft não
  publica nada sozinho.
- **`requirements.txt`:** `pywin32` (usado só por `email_cobranca.abrir_no_outlook`, importado de
  forma lazy dentro da função) precisou virar `pywin32; sys_platform == "win32"` - sem isso o
  `pip install` inteiro falha no Linux do Streamlit Cloud, já que esse pacote não existe pra lá.
  Os outros pacotes (`playwright`, `pdfplumber`, `pytesseract`, `Pillow`) são cross-platform e
  instalam normalmente mesmo sem nunca serem chamados de verdade em modo nuvem.
- **Armadilha ao criar o app pela primeira vez:** colar só `usuario/repositorio` no campo
  "Repository" não bastou (dava "This repository does not exist"/"This branch does not exist" mesmo
  preenchendo Branch e Main file path certos) - funcionou usando **"Paste GitHub URL"** com a URL
  completa apontando pro arquivo: `https://github.com/<usuario>/<repo>/blob/main/app.py`.
- Trechos de código que só rodam em ambiente Windows local (`abrir_no_outlook`) já importavam
  `win32com.client`/`pythoncom` **dentro da função**, não no topo do módulo - por isso importar
  `email_cobranca` (feito no topo do `app.py`) não quebra no Linux, só chamar a função quebraria (e
  ela nunca é chamada em modo nuvem, o botão fica escondido).
- **Validado ponta a ponta com usuário real:** a Mara (`mara.financeiro@daterrinhaalimentos.com.br`)
  tentou acessar antes de fazer login no Streamlit e caiu na tela "You do not have access" (mesma
  mensagem que aparece pra quem não está autorizado) - resolvido orientando ela a clicar em "sign in"
  e entrar com o e-mail exatamente igual ao cadastrado na lista de Sharing. Depois disso o acesso
  funcionou normalmente. Vale lembrar isso é o comportamento normal (a tela de "sem acesso" aparece
  igual tanto pra quem não está na lista quanto pra quem está mas ainda não logou).

## Notas operacionais importantes

- **F5 vs. reiniciar o processo:** editar `app.py` só precisa de F5 na página. Editar qualquer
  arquivo dentro de `src/notas_devolucao/` (navigation.py, factoring.py, storage.py etc.) exige
  **reiniciar o processo do Streamlit por completo** (matar e rodar `1 - ABRIR APP.bat` de novo) -
  o Python cacheia os módulos importados, então só um refresh de página não pega a mudança.
- **Se a automação travar no meio de uma verificação** (o Bluesoft tem um `debugger;` que já
  travou a automação silenciosamente duas vezes neste projeto): nunca mate só a janela do
  Chromium - se o loop estiver salvando resultado incrementalmente no banco, matar só o navegador
  faz o restante da fila ser processado sem navegador de verdade e gravar tudo errado. Encerre o
  processo do Streamlit inteiro e reinicie; o que já foi salvo antes do travamento continua válido.
- **Se um arquivo Excel externo der erro de permissão** (ex.: planilha de Antecipação) - geralmente
  é porque o arquivo está aberto no Excel em algum computador; feche e tente de novo.

## Contexto técnico mais aprofundado

Detalhes de seletores, URLs e armadilhas específicas de automação do Bluesoft (não só deste
projeto, mas dos outros 3 projetos irmãos de automação da empresa) estão consolidados em
`C:\Users\Daterrinha68\daterrinhaalimentos.com.br\...\Área de Trabalho\CONHECIMENTO_BLUESOFT.md` -
vale consultar antes de começar uma automação nova ou investigar um bug de extração parecido com os
já descritos aqui.
