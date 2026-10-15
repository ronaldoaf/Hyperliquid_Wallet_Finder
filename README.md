# Hyperliquid Wallet Finder

Localiza carteiras da Hyperliquid cuja estratégia em perps é **lucrativa com evidência estatística** e **continua lucrativa quando copiada com pelo menos 30 minutos de atraso**, já descontando taxa, spread e funding do copiador.

Usa só dados públicos: o leaderboard (`stats-data.hyperliquid.xyz`), a Info API (`POST https://api.hyperliquid.xyz/info`, sem chave) e, para preços intradiários antigos, o arquivo público de velas da Binance. Parte da coleta e da análise vem do projeto [Hyperliquid_RE](https://github.com/ronaldoaf/Hyperliquid_RE) (cliente da API, cadeia de posição pelos fills, cópia com atraso), aqui reescrita para rodar em milhares de carteiras.

## Resultado da varredura de 14/10/2026

Relatório completo: [`reports/varredura_2026-10-14/RELATORIO.md`](reports/varredura_2026-10-14/RELATORIO.md).

- **Universo:** as 7.147 carteiras com volume total ≥ US$ 100M (de 47.188 no leaderboard). Corte em 16/06/2026; teste da cópia de 16/06 a 14/10/2026 (121 dias).
- **Funil:** 1.966 pontuáveis antes do corte → 150 simuladas → 118 na família do teste final → 52 com dados suficientes no teste (as outras 66 da etapa C não têm fills que cubram o período, ou entraram poucas vezes).
- **Nenhuma carteira passou em todos os critérios.** Com 118 testes, a melhor precisaria de p ≈ 0,0008; a melhor teve p = 0,013.
- **5 promissoras** (lucro com 30 min, 1 h e 2 h de atraso depois do corte, p < 0,05 sem correção, e a maioria com teste de deslocamento p < 0,1): [`0xf97a…ddd1`](reports/varredura_2026-10-14/RELATORIO.md#0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1), [`0xe868…7879`](reports/varredura_2026-10-14/RELATORIO.md#0xe8681168f59af16c60c805dab7842eb75f127879), [`0x2312…9e92`](reports/varredura_2026-10-14/RELATORIO.md#0x2312171890250347dbf1b082bdf7950504909e92), [`0xb69e…033a`](reports/varredura_2026-10-14/RELATORIO.md#0xb69eec8fea0081343c3b09e652ba1fa7dcf7033a) (alavancada, queda de 76% no teste, sem ordens novas desde 05/09/2026 mas ainda posicionada) e [`0x352d…425a`](reports/varredura_2026-10-14/RELATORIO.md#0x352deb23bebae8b4c57d0ae341d9c1951fd8425a). O acaso sozinho daria ~2,6 carteiras com p < 0,05 entre 52: são candidatas para acompanhar com `track`, não para copiar já.
- **O atraso não é o problema; a persistência é.** Entre as carteiras que lucraram copiando na hora, a cópia com 30 min e custos manteve a mediana de 78% do retorno. O que falha é o desempenho passado se repetir: depois do corte, o próprio trader (cópia imediata, sem custos) teve 5 resultados com p < 0,05 entre 52, perto dos 2,6 do acaso, num período em que o BTC subiu 26%.
- **Validação nas 10 carteiras do [Hyperliquid_RE](https://github.com/ronaldoaf/Hyperliquid_RE)** ([`reports/carteiras/avulsas_2026-06-16.md`](reports/carteiras/avulsas_2026-06-16.md)): o resultado bate com as análises de lá. A `0x8bae` (lucro de fills fora do preço de mercado) perde dinheiro quando copiada a mercado; o bot HFT `0x78dc` não tem fills que cubram o teste (a API guarda só os últimos dias dele); o carry `0x833b` perde (é uma perna de um *hedge*); as de posição longa (`0xcac1`, `0xfce0`, `0x16bf`) têm poucas entradas para teste; a `0x0812` sai como promissora, e a análise de lá já dizia que a amostra era curta.

## Como usar

```bash
pip install -r requirements.txt

# 1. baixa o leaderboard e o histórico semanal de cada carteira com volume >= US$ 50M
#    (~55 carteiras por minuto pelo limite da API; o cache evita baixar de novo)
python -m hlwf portfolios --min-volume 5e7

# 2. varredura completa: pontua antes do corte, testa a cópia depois do corte, gera o relatório
#    (~1,5 h para 150 carteiras na primeira vez; preços e fills ficam em cache)
python -m hlwf scan --min-volume 5e7 --split-days 120 --top 150

# testar carteiras específicas (cada uma é um teste isolado)
python -m hlwf wallet 0xENDERECO1 0xENDERECO2

# acompanhar uma seleção antiga daqui para frente (o teste fora da amostra de verdade)
python -m hlwf track reports/varredura_2026-10-14/selecao.json

# o que as carteiras selecionadas têm aberto agora e o que fizeram nas últimas 24 h
python -m hlwf positions reports/varredura_2026-10-14/selecao.json --hours 24
```

Opções úteis: `--main-delay-min 60` (atraso principal), `--taker-bps 3.0` (se você tem desconto de taxa), `--max-leverage 10` (teto de alavancagem do copiador), `--budget 1100` (peso de API por minuto; o limite por IP é 1200).

Saída de `scan` em `reports/varredura_<data>/`: `RELATORIO.md` (funil, aprovadas, fichas com gráfico, motivos de reprovação), `resultados.csv`, `etapa_b.csv.gz` e `selecao.json`. Os dados brutos ficam em `data/` (fora do git).

## Método

A pergunta tem duas partes, e cada uma tem uma armadilha.

1. **"Lucrativa estatisticamente comprovada."** Entre dezenas de milhares de carteiras, centenas vão parecer excelentes por sorte. Escolher as melhores e testar nos mesmos dados confirma a sorte. Por isso a escolha e o teste usam **períodos diferentes**: as carteiras são escolhidas com dados até a data de corte (padrão: 120 dias atrás) e testadas só depois dela, com correção para o número de carteiras testadas.
2. **"Ainda lucrativa copiando com atraso."** O que interessa não é o PnL da carteira, e sim o do copiador: entrando e saindo 30 minutos depois, a preço de mercado, pagando taxa *taker* e spread. O teste é feito sobre essa cópia simulada, não sobre o PnL da carteira.

### Etapa A: leaderboard (uma chamada)

Mantém as carteiras com volume ≥ US$ 50M (padrão; a varredura de 15/10/2026 usou ≥ US$ 100M para caber no tempo). **Nenhum filtro de PnL, ROI ou patrimônio atual**: esses números incluem o período de teste. Filtrar por eles excluiria, antes do teste, quem quebrou depois do corte, e o teste ficaria otimista (viés de sobrevivência). O volume também inclui o período de teste, e a API não dá o histórico dele; por isso o filtro usa o volume total menos o do último mês. Toda carteira que já passava do limite no corte continua dentro, e as que só passaram dele operando no último mês ficam fora. As que passaram entre o corte e o último mês ainda entram: um resíduo de sobrevivência, declarado no relatório. A opção `--leaky` liga os filtros de PnL e patrimônio para uma varredura rápida, sabendo do viés.

### Etapa B: histórico semanal até o corte (uma chamada por carteira)

O endpoint `portfolio` traz o PnL de perps e o patrimônio desde mai/2024, em pontos semanais. Só a parte **anterior ao corte** é usada:

- retorno semanal = ΔPnL de perps / capital em risco (o maior entre o patrimônio do início e o do fim menos o PnL, para um depósito não virar retorno). O patrimônio é o maior entre o total e o de perps, o mesmo da etapa C: com margem unificada, o de perps sozinho pode ser quase zero;
- descarta: menos de 8 semanas com PnL, parada nas 8 semanas antes do corte, patrimônio < US$ 10 mil no corte, giro > 20.000x o patrimônio por ano (market makers e HFT, que não se copiam com atraso). O giro usa patrimônio e anos de antes do corte e o volume total menos o do último mês (mesmo resíduo da etapa A);
- pontua pelo t do retorno semanal médio e manda as `--top` maiores para a etapa C.

### Etapa C: cópia com atraso, testada fora da amostra

Para cada carteira escolhida, atualiza o histórico de patrimônio, baixa os fills de perps (inclusive HIP-3) desde 30 dias antes do corte e simula um copiador:

- **Fills:** baixados página por página até o momento da coleta (depois de ler o estado da conta), inclusive as fatias de ordens TWAP, que vêm de outro endpoint. Uma ordem a mercado que bate em mais de 2.000 *makers* no mesmo milissegundo não cabe numa página; esse milissegundo é lido agregado por ordem. A API serve só a parte mais recente do histórico, e quanto ela guarda varia de horas a anos conforme a carteira; por isso o cache registra o intervalo em que os fills estão completos (`coverage` a `complete_to`). O início é o pedido quando a API ainda serve fills mais antigos que ele; senão, é o fill mais antigo servido. Ao estender o cache, a coleta recomeça do último fill até onde ele está completo, e a primeira página nova precisa conter esse fill; se não contém, houve um buraco e a cobertura recomeça depois dele. Uma carteira só é testada se os fills estão completos até o fim do teste.
- **Posição:** reconstruída pela cadeia `startPosition` dos fills. Fills do mesmo milissegundo são somados sem depender da ordem da API. Um salto sem fill visível (fatia de TWAP que a API já não serve, fill perdido) entra no fill seguinte; se esses saltos passam de 2% do volume negociado na janela, a carteira sai como dados insuficientes. Posições abertas o período todo, sem fill, vêm do estado atual da conta em todas as dex (a principal e cada dex HIP-3). No fim, a posição reconstruída de cada moeda é comparada com a da conta; no início da janela, com os tamanhos sobre os quais a carteira pagou funding (o histórico de funding é um registro independente do que ela tinha, e pega posições montadas por fatias de TWAP que a API já não serve). Se alguma das duas diferenças passa de 10% do nocional, a carteira sai como dados insuficientes. Também sai se uma moeda relevante não tem preço. A ficha mostra ainda o PnL da cópia imediata contra o PnL de perps que a própria bolsa informa, só como diagnóstico.
- **Execução:** cada mudança de posição vira uma ordem a mercado do copiador, enviada `atraso` depois do fill e executada na abertura da primeira vela que começa depois disso (com velas de 5 min, o atraso efetivo fica entre 30 e 35 min). Fills na mesma vela se compensam, como num copiador que consulta a conta periodicamente. Ao começar, o copiador entra nas posições que a carteira já tinha.
- **Preços:** velas de 5 min da Binance até o último arquivo publicado e da Hyperliquid depois disso; as da Binance só são aceitas se o fechamento diário bater com o da Hyperliquid (com conversão de unidades `kPEPE` → `1000PEPEUSDT`; nome igual de token diferente é rejeitado). A Binance publica os arquivos com atraso, então um arquivo recente que falta não marca a moeda como não listada, e um buraco no meio da série é coberto pelas velas da Hyperliquid; para moedas sem Binance (HIP-3 e as exclusivas da Hyperliquid), velas de 15 min da Hyperliquid nos últimos 52 dias e de 1 h antes disso (atraso efetivo de até 45 min ou 90 min: mais conservador). **O preço do fill da carteira nunca é usado**: quem opera a preços que ninguém mais consegue (livros ilíquidos, prints fora do mercado, como a `0x8bae` do Hyperliquid_RE) não ganha crédito por isso.
- **Custos:** taxa *taker* de 4,5 bp (nível 0, sem desconto; HIP-3 com 2x) mais meio spread e impacto por faixa de liquidez de hoje (1 bp acima de US$ 1B/dia até 25 bp abaixo de US$ 2M/dia, ou o *impact spread* da bolsa, o que for maior). Funding horário real, no período de teste, para as moedas com ≥ 5% da exposição; para as demais, antes do corte e em qualquer hora que o download não trouxe, a taxa base de 0,00125%/h (os comprados pagam).
- **Retorno:** PnL diário (marcado no fechamento UTC; o último dia, no preço do fim do teste) sobre o patrimônio da carteira no início do dia, como quem copia na mesma proporção do patrimônio. O patrimônio é a última leitura conhecida naquele momento (sem interpolar para uma leitura futura), o maior entre o total e o de perps (com margem unificada, o de perps sozinho pode ser quase zero) e nunca menor que a maior exposição bruta do dia / 20 (teto de alavancagem do copiador; trades abertos e fechados no mesmo dia contam). Um dia nunca perde mais que 100%; se perde tudo, a cópia acaba ali (os dias seguintes valem 0), e o retorno composto também é medido.

Atrasos simulados: imediato sem custos (referência), 30 min, 1 h, 2 h e 4 h.

### A família do teste final

Antes de olhar o período de teste, uma última triagem com dados de antes do corte: a cópia com 30 min, com custos e a taxa base de funding, nos 30 dias anteriores ao corte (`--insample-days`) não pode ter dado prejuízo, se a carteira estava posicionada nesse período. Quem já perdia copiando antes do corte não entra na família de testes. Isso não usa os retornos do período de teste, então os valores-p continuam válidos, e a correção para múltiplos testes não gasta poder com carteiras que já não se copiavam bem (market makers, *hedges* de uma perna só, quem opera a preços fora do mercado). Carteiras da família sem dados suficientes no teste, ou cujo download falhou, contam como p = 1. Dois resíduos ficam declarados: quantos fills a API ainda guarda (que define se há triagem) depende da atividade recente, e o custo de liquidez usado é o de hoje.

### Os testes

Todos sobre os retornos diários **depois do corte**, com custos:

| critério | teste | por quê |
|---|---|---|
| lucro com 30 min de atraso | t do retorno médio diário com erro padrão de Newey-West (HAC); p unilateral | retornos de quem carrega posição são autocorrelacionados; o t comum superestima a confiança |
| não é sorte entre muitas | q de **Benjamini-Hochberg** ≤ 0,10 sobre a família do teste final (as sem dados contam como p = 1) | controla a fração de falsas descobertas entre as aprovadas |
| sobrevive a atrasos maiores | com 1 h e 2 h, o copiador também termina mais rico (mesmo critério da linha abaixo) | a vantagem não pode depender de copiar no minuto exato |
| é habilidade, não só direção do mercado | **teste de deslocamento**: as mesmas exposições horárias, deslocadas no tempo (≥ 7 dias, circular), formam a distribuição nula; p ≤ 0,10. Alternativa: alfa sobre o BTC com t ≥ 2 | quem ficou comprado numa alta ganha, mas o lucro vem do mercado. O deslocamento mantém o viés comprado/vendido, o tamanho e o tempo de cada posição; só muda *quando* |
| o copiador termina com mais dinheiro | retorno composto > 0, PnL em US$ > 0 e nenhum dia de perda total | a média dos retornos diários pode ser positiva enquanto o copiador perde |
| não depende de poucos dias | lucro positivo sem os 5 melhores dias | um único trade de sorte não é estratégia |
| amostra mínima | ≥ 60 dias de teste, ≥ 40 dias com posição e ≥ 10 entradas depois do corte (posição herdada do corte não conta) | abaixo disso o teste não tem poder |

**Aprovada** passa em tudo; **promissora** tem dados suficientes, p < 0,05 sem correção e deixa o copiador mais rico com 30 min e com 1 h, mas falha em algum outro critério; **dados insuficientes** quando os fills não cobrem o período de teste inteiro, a posição muda sem fills visíveis, as posições reconstruídas não batem com as da conta no fim ou com o funding no início, falta preço de uma moeda relevante, ou há poucas entradas.

Cada ficha também mostra: retorno anualizado com intervalo de 90% por bootstrap estacionário em blocos, Sharpe, queda máxima, alavancagem média e máxima, beta ao BTC, concentração, estilo (entradas, dias por posição, % comprado, tempo posicionado), PnL por moeda e as posições abertas agora.

### Acompanhamento

O teste fora da amostra é honesto, mas é passado. `python -m hlwf track …/selecao.json` reavalia as carteiras escolhidas a partir da primeira meia-noite depois da seleção (nenhuma hora usada para escolhê-las conta como evidência): é o teste para frente, que deve ser rodado periodicamente antes de colocar dinheiro. Nas primeiras semanas os mínimos de amostra são proporcionais ao tempo decorrido, e o veredito só fica firme depois de uns dois meses.

## Estrutura

```
hlwf/
  api.py        cliente da Info API com controle de peso por minuto; download do leaderboard
  screen.py     etapas A e B: filtro de atividade e pontuação semanal antes do corte
  wallet.py     fills (cache incremental), estado da conta, série de patrimônio
  market.py     velas Hyperliquid + Binance 5m validadas, funding horário, custo por moeda
  copysim.py    simulação do copiador com atraso (execução, custos, funding, retornos)
  stats.py      HAC, bootstrap estacionário, PSR/DSR, Benjamini-Hochberg, deslocamento, alfa/beta
  evaluate.py   etapa C de uma carteira e o veredito
  pipeline.py   varredura completa, testes avulsos, acompanhamento
  report.py     relatórios em markdown;  charts.py  gráficos
  cli.py        python -m hlwf …
tests/          testes do simulador e das estatísticas (pytest)
reports/        relatórios gerados
```

## Limitações

- **Fills:** a API só serve a parte mais recente dos fills (a documentação fala em 10 mil; na prática varia de horas a anos) e as fatias de TWAP das últimas semanas. Carteiras muito ativas não têm fills que cubram o período de teste e saem como "dados insuficientes". Rodar a coleta periodicamente faz o histórico local crescer.
- **Custos de hoje:** spread e impacto vêm da liquidez atual. Um copiador grande paga mais impacto do que o modelo supõe.
- **Proporção ao patrimônio:** se a carteira guarda muito em spot, o retorno da cópia fica subestimado (conservador). Se usa hedge fora da Hyperliquid, a cópia é de uma perna só; o teste de deslocamento e o alfa costumam pegar isso.
- **Resultados guardados:** cada resultado da etapa C grava o fim do teste, um hash das configurações e um hash do código; uma varredura só reaproveita um resultado se os três batem. Uma varredura interrompida, retomada no mesmo dia, mantém a data de fim da primeira (`--no-resume` recomeça do zero).
- **O passado não garante o futuro:** um trader pode mudar de estilo, de tamanho ou parar. Use `track`.
- Nada aqui é recomendação de investimento.
