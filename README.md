# Hyperliquid Wallet Finder

Localiza carteiras da Hyperliquid cuja estratégia em perps é **lucrativa com evidência estatística** e **continua lucrativa quando copiada com pelo menos 30 minutos de atraso**, já descontando taxa, spread e funding do copiador.

Usa só dados públicos: o leaderboard (`stats-data.hyperliquid.xyz`), a Info API (`POST https://api.hyperliquid.xyz/info`, sem chave) e, para preços intradiários antigos, o arquivo público de velas da Binance. Parte da coleta e da análise vem do projeto [Hyperliquid_RE](https://github.com/ronaldoaf/Hyperliquid_RE) (cliente da API, cadeia de posição pelos fills, cópia com atraso), aqui reescrita para rodar em milhares de carteiras.

**Resultado da última varredura:** [`reports/varredura_2026-10-14/RELATORIO.md`](reports/varredura_2026-10-14/RELATORIO.md).

## Como usar

```bash
pip install -r requirements.txt

# 1. baixa o leaderboard e o histórico semanal de cada carteira com volume >= US$ 50M
#    (~55 carteiras por minuto pelo limite da API; o cache evita baixar de novo)
python -m hlwf portfolios --min-volume 5e7

# 2. varredura completa: pontua antes do corte, testa a cópia depois do corte, gera o relatório
python -m hlwf scan --split-days 120 --top 150

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

Mantém as carteiras com volume total ≥ US$ 50M (12,2 mil de 47 mil). **Nenhum filtro de PnL, ROI ou patrimônio atual**: esses números incluem o período de teste. Filtrar por eles excluiria, antes do teste, quem quebrou depois do corte, e o teste ficaria otimista (viés de sobrevivência). A opção `--leaky` liga esses filtros para uma varredura rápida, sabendo do viés.

### Etapa B: histórico semanal até o corte (uma chamada por carteira)

O endpoint `portfolio` traz o PnL e o patrimônio de perps desde mai/2024, em pontos semanais. Só a parte **anterior ao corte** é usada:

- retorno semanal = ΔPnL / capital em risco (o maior entre o patrimônio do início e o do fim menos o PnL, para um depósito não virar retorno);
- descarta: menos de 8 semanas com PnL, parada nas 8 semanas antes do corte, patrimônio < US$ 10 mil no corte, giro > 20.000x o patrimônio por ano (market makers e HFT, que não se copiam com atraso);
- pontua pelo t do retorno semanal médio e manda as `--top` maiores para a etapa C.

### Etapa C: cópia com atraso, testada fora da amostra

Para cada carteira escolhida, baixa os fills de perps (inclusive HIP-3) desde 30 dias antes do corte e simula um copiador:

- **Posição:** reconstruída pela cadeia `startPosition` dos fills. Fills do mesmo milissegundo são somados sem depender da ordem da API; um salto sem fill visível (TWAP antiga, fill que a API não serve mais) entra no fill seguinte, ou seja, o copiador fica sabendo tarde, nunca cedo. Posições abertas o período todo, sem fill, vêm do estado atual da conta.
- **Execução:** cada mudança de posição vira uma ordem a mercado do copiador, enviada `atraso` depois do fill e executada na abertura da primeira vela que começa depois disso (com velas de 5 min, o atraso efetivo fica entre 30 e 35 min). Fills na mesma vela se compensam, como num copiador que consulta a conta periodicamente. Ao começar, o copiador entra nas posições que a carteira já tinha.
- **Preços:** velas de 5 min da Binance até ontem e da Hyperliquid nas últimas horas; as da Binance só são aceitas se o fechamento diário bater com o da Hyperliquid (com conversão de unidades `kPEPE` → `1000PEPEUSDT`; nome igual de token diferente é rejeitado); para moedas sem Binance (HIP-3 e as exclusivas da Hyperliquid), velas de 15 min da Hyperliquid nos últimos 52 dias e de 1 h antes disso (atraso efetivo de até 45 min ou 90 min: mais conservador). **O preço do fill da carteira nunca é usado**: quem opera a preços que ninguém mais consegue (livros ilíquidos, prints fora do mercado, como a `0x8bae` do Hyperliquid_RE) não ganha crédito por isso.
- **Custos:** taxa *taker* de 4,5 bp (nível 0, sem desconto; HIP-3 com 2x) mais meio spread e impacto por faixa de liquidez de hoje (1 bp acima de US$ 1B/dia até 25 bp abaixo de US$ 2M/dia, ou o *impact spread* da bolsa, o que for maior). Funding horário real para as moedas com ≥ 1% da exposição; para as demais, a taxa base de 0,00125%/h.
- **Retorno:** PnL diário (marcado no fechamento UTC) sobre o patrimônio da carteira no início do dia, como quem copia na mesma proporção do patrimônio. O patrimônio é o maior entre o total e o de perps (com margem unificada, o de perps sozinho pode ser quase zero) e nunca menor que a exposição bruta / 20 (teto de alavancagem do copiador).

Atrasos simulados: imediato sem custos (referência), 30 min, 1 h, 2 h e 4 h.

### A família do teste final

Antes de olhar o período de teste, uma última triagem com dados de antes do corte: a cópia com 30 min, com custos, nos 30 dias anteriores ao corte (`--insample-days`) não pode ter dado prejuízo. Quem já perdia copiando antes do corte não entra na família de testes. Isso não usa nada do período de teste, então os valores-p continuam válidos, e a correção para múltiplos testes não gasta poder com carteiras que já não se copiavam bem (market makers, *hedges* de uma perna só, quem opera a preços fora do mercado). Carteiras da família sem dados suficientes no teste contam como p = 1.

### Os testes

Todos sobre os retornos diários **depois do corte**, com custos:

| critério | teste | por quê |
|---|---|---|
| lucro com 30 min de atraso | t do retorno médio diário com erro padrão de Newey-West (HAC); p unilateral | retornos de quem carrega posição são autocorrelacionados; o t comum superestima a confiança |
| não é sorte entre muitas | q de **Benjamini-Hochberg** ≤ 0,10 sobre a família do teste final (as sem dados contam como p = 1) | controla a fração de falsas descobertas entre as aprovadas |
| sobrevive a atrasos maiores | retorno positivo também com 1 h e 2 h | a vantagem não pode depender de copiar no minuto exato |
| é habilidade, não só direção do mercado | **teste de deslocamento**: as mesmas exposições horárias, deslocadas no tempo (≥ 7 dias, circular), formam a distribuição nula; p ≤ 0,10. Alternativa: alfa sobre o BTC com t ≥ 2 | quem ficou comprado numa alta ganha, mas o lucro vem do mercado. O deslocamento mantém o viés comprado/vendido, o tamanho e o tempo de cada posição; só muda *quando* |
| não depende de poucos dias | lucro positivo sem os 5 melhores dias | um único trade de sorte não é estratégia |
| amostra mínima | ≥ 60 dias de teste, ≥ 40 dias com posição e ≥ 10 entradas | abaixo disso o teste não tem poder |

**Aprovada** passa em tudo; **promissora** tem p < 0,05 sem correção e lucra com 30 e 60 min, mas falha em algum outro critério; **dados insuficientes** quando os fills não cobrem o período de teste (a API só guarda os 10 mil fills mais recentes) ou há poucas entradas.

Cada ficha também mostra: retorno anualizado com intervalo de 90% por bootstrap estacionário em blocos, Sharpe, queda máxima, alavancagem média e máxima, beta ao BTC, concentração, estilo (entradas, dias por posição, % comprado, tempo posicionado), PnL por moeda e as posições abertas agora.

### Acompanhamento

O teste fora da amostra é honesto, mas é passado. `python -m hlwf track …/selecao.json` reavalia as carteiras escolhidas a partir da data da seleção: é o teste para frente, que deve ser rodado periodicamente antes de colocar dinheiro.

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

- **Fills:** a API só serve os 10 mil fills mais recentes. Carteiras muito ativas não têm fills que cubram o período de teste e saem como "dados insuficientes". Rodar a coleta periodicamente faz o histórico local crescer.
- **Custos de hoje:** spread e impacto vêm da liquidez atual. Um copiador grande paga mais impacto do que o modelo supõe.
- **Proporção ao patrimônio:** se a carteira guarda muito em spot, o retorno da cópia fica subestimado (conservador). Se usa hedge fora da Hyperliquid, a cópia é de uma perna só; o teste de deslocamento e o alfa costumam pegar isso.
- **HIP-3:** posições abertas antes da janela de fills e nunca mexidas não aparecem (o estado aberto é lido só da bolsa principal).
- **O passado não garante o futuro:** um trader pode mudar de estilo, de tamanho ou parar. Use `track`.
- Nada aqui é recomendação de investimento.
