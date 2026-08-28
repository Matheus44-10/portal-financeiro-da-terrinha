# Mapeamento de UI - Bluesoft (Contas a Receber / Notas de Devolução)

Validado ao vivo em sessão supervisionada (login manual do usuário + navegação/filtros aplicados
por ele mesmo enquanto o estado da tela era inspecionado via Playwright), em
`https://erp.bluesoft.com.br/daterrinha/...`. Mesmo método usado no projeto irmão "Automação
agrupamento" para mapear Contas a Pagar.

## Tela: Consultar Contas a Receber

- Acessada pelo menu central antigo (`erp-app/areas/core/menu-central/menu-central.index.jsp`),
  clicando no link "Consultar Contas a Receber" (aparece em "Mais Acessados").
- Assim como Contas a Pagar, o conteúdo real vive dentro de um **`<iframe>`**:
  `https://erp.bluesoft.com.br/daterrinha/financeiro/cobranca/consultaContasAReceber/index.action`.
  Toda interação precisa ser no frame, não na page de nível superior.

### Campos de filtro confirmados (ids reais, mesma classe select2-offscreen por trás de `<select>` reais)

- Sacado (quem deve o valor - é o **fornecedor** no caso de devolução): `#sacadoNome` (texto, busca
  parcial) / `#sacadoKeys` (hidden, ids reais quando busca múltipla via lupa).
- Quitado: `select#pagos` - opção usada: **"Não Pagos"**.
- Tipo de duplicata: `select#tipoDuplicataCobrancaKeys` (**select multiple**) - as notas de
  devolução usam duas opções: **"Devolução de Compra"** e **"Devolução de Trocas"**. Esse campo vive
  dentro do painel **"Exibir/Esconder Filtros Avançados"**, recolhido por padrão - **é preciso clicar
  nesse link antes de tudo**. `select_option()` direto no `<select>` escondido (mesmo com
  `force=True`) faz o valor aparecer selecionado no DOM mas **não afeta a busca de verdade**
  (confirmado ao vivo: retornava 0 resultados mesmo com dados reais existentes). A automação precisa
  interagir com o **widget select2 visível**: clicar no container (`#s2id_tipoDuplicataCobrancaKeys`),
  digitar o termo no campo de busca (`.select2-input`), e clicar no primeiro resultado filtrado
  (`#select2-drop li.select2-result-selectable`) - uma vez para cada opção.
- Tipo de data do período: `select#tipoDataDuplicata` - por padrão `DATA_VENCIMENTO`. Existe uma
  opção `NAO_CONSIDERAR_DATA` ("Não considerar data") que teoricamente ignora o filtro de período,
  mas **não funcionou de forma confiável nos testes ao vivo** (a busca voltou 0 resultados mesmo com
  devoluções pendentes reais existindo). **Usar sempre um período explícito** (`dataInicial`/
  `dataFinal`, mesmo formato `dd/mm/aaaa` com datepicker jQuery, igual Contas a Pagar) em vez de
  tentar ignorar a data.
- Período: `#dataInicial` / `#dataFinal`.

### Botões

- `#btnAnalitica` ("ANALÍTICA") e `#btnSintetica` ("SINTÉTICA") - equivalentes aos de Contas a
  Pagar. **Nos testes ao vivo, `Analítica` retornou 0 linhas mesmo com filtros corretos e dados reais
  existentes; `Sintética` retornou os dados corretamente** (motivo exato não identificado - pode ser
  um detalhe de timing/AJAX específico dessa tela). **A extração desta automação usa o botão
  Sintética.**
- `#btnManutencao` ("MANUTENÇÃO DE DUPLICATAS") e `#btn-agruparDuplicatas` ("Agrupar duplicatas")
  também existem nesta tela (mesmo padrão de Contas a Pagar) mas não são usados - esta automação é
  **somente leitura**.

### Extração de dados

Mesma técnica de Contas a Pagar: ler direto do model do AngularJS via
`angular.element(tr).scope()`, não parsear texto/ícones do DOM.

- Ao clicar Sintética: `ng-repeat="cobranca in cobrancasSintetica"` (cada `<tr>` expõe o objeto
  completo em `scope.cobranca`).
- Campos confirmados no objeto (mesmo formato de Contas a Pagar, com nomes próprios desta tela):
  `duplicataKey`, `sacadoNome` (fornecedor), `sacadoCpfCnpj` (**CNPJ do fornecedor - chave natural
  para cruzar com `favorecidoCpfCnpj` de Contas a Pagar**), `lojaKey`, `lojaNome`,
  `lojaRecebedoraKey`, `valorNominal`, `valorLiquido`, `valorPago`, `dataEmissao`, `dataVencimento`,
  `descritivo` (geralmente cita o nº da NF), `numeroDocumento`, `tipoDuplicataDescricao` ("Devolução
  de Compra" / "Devolução de Trocas"), `tipoTransacao`, `contaContabilDescricao`,
  `contaContabilDestinoDescricao`, `liquidada` (bool - **false = ainda pendente**),
  `liquidadaDescricao`, `comprovanteRecebidoDescricao` ("Cobrado"/"Não Cobrado" - status de
  cobrança), `quantidadeDiasDeAtraso`, `competenciaKey`, `borderoKey`, `contaBancariaDescricao`,
  `grupoEconomico` (quando aplicável).

### Exemplo real validado

Fornecedor "TEREOS AMIDO E ADOCANTES BRASIL S.A." (CNPJ 65.882.680/0001-60), várias duplicatas de
"Devolução de Trocas" com `liquidada: false` e `quantidadeDiasDeAtraso: 73`, vinculadas a notas
fiscais específicas via campo `descritivo` (ex: "FORN.: TEREOS AMIDO - NF: 12.617-1").

## Fluxo completo validado ponta a ponta

Rodando `sincronizacao.atualizar_dados()`: login (sessão reaproveitada) → filtros de devolução
(Filtros Avançados expandido, tipo de duplicata via widget select2, Não Pagos, período) → Sintética →
75 notas de devolução pendentes reais extraídas → para cada um dos fornecedores envolvidos, consulta
de Contas a Pagar (Não Pagos, favorecido, período) → 289 contas a pagar em aberto extraídas ao todo
para 33 fornecedores. Volumes por fornecedor variaram de 0 a 70 duplicatas, confirmando que a
paginação genérica (`navigation.carregar_todas_paginas`) funciona tanto para Contas a Pagar quanto
para a Sintética de devolução.

## Itens ainda não validados

- Se `#sacadoNome`/`#sacadoKeys` funcionam com `select_option`/`fill` direto como em Contas a Pagar
  (não testado isoladamente - o teste ao vivo não filtrou por um fornecedor específico; a automação
  final também não precisa disso, já que busca todos os fornecedores de uma vez).
