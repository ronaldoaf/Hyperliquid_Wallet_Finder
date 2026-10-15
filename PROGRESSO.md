# Estado do trabalho (15/10/2026)

Este arquivo registra o que já foi feito, onde o trabalho parou e como retomá-lo. O método está no [README](README.md#método).

**Resumo:** o código está pronto e revisado (78 testes passando). A varredura de 15/10/2026, a primeira com o código corrigido, foi **interrompida em 74 de 150 carteiras**. Os resultados parciais estão em [`reports/varredura_2026-10-15_parcial/resultados_parciais.csv`](reports/varredura_2026-10-15_parcial/resultados_parciais.csv). Até ali, **nenhuma carteira tinha evidência estatística suficiente**. Os relatórios de 14/10/2026 são **inválidos** (detalhes abaixo).

## O que já foi feito

### 1. O localizador (pacote `hlwf`)

- **Coleta:** leaderboard, histórico semanal (`portfolio`), fills completos com paginação, fatias de TWAP, estado da conta em todas as dex (inclusive HIP-3), velas da Hyperliquid em 5 camadas (5 min a 1 dia), velas de 5 min da Binance para o passado que a Hyperliquid não serve mais, e funding horário. Tudo fica em cache em `data/`.
- **Seleção sem olhar o teste:**
  - etapa A: volume, sem filtro de PnL;
  - etapa B: t do retorno semanal até o corte;
  - etapa C: cópia simulada, testada só depois do corte;
  - a família do teste final é definida antes do corte.
- **Cópia com atraso:** o copiador segue a posição que a carteira tinha 30 min antes, 1:1, a mercado, na abertura da primeira vela depois do sinal. Paga:
  - taxa *taker* (4,5 bp; 2x nas HIP-3) e meio spread por faixa de liquidez;
  - funding horário (real nas moedas que cobrem 90% da exposição);
  - o custo de entrar no livro herdado no corte e de sair no fim do teste.

  O capital vem do patrimônio da carteira, com piso pela margem necessária. Há liquidação intradiária, e a ruína encerra o caminho.
- **Testes estatísticos:**
  - t com erro HAC (Newey-West);
  - correção de Benjamini-Hochberg na família (q ≤ 0,10);
  - teste de deslocamento circular (timing) ou alfa sobre o BTC;
  - lucro também com 1 h e 2 h de atraso;
  - lucro sem os 5 melhores dias;
  - amostra mínima: 60 dias, 40 dias posicionada e 10 entradas.
- **Comandos:** `portfolios`, `scan`, `wallet`, `track` (teste para frente de uma seleção antiga) e `positions` (o que as carteiras têm aberto agora).

### 2. Por que os relatórios de 14/10/2026 são inválidos

Os relatórios [`reports/varredura_2026-10-14/`](reports/varredura_2026-10-14/RELATORIO.md) e [`reports/carteiras/avulsas_2026-06-16.md`](reports/carteiras/avulsas_2026-06-16.md) saíram de um código com um erro de paginação. A coleta de fills parava cedo e marcava o cache como completo, e as posições ficavam congeladas por semanas na simulação. Faltavam também as fatias de TWAP e as posições das dex HIP-3. Os números daqueles relatórios não valem. Os arquivos ficam no repositório só como registro e estão marcados com um aviso.

### 3. Correções depois de 14/10 (três revisões adversariais)

| commit | o que mudou |
|---|---|
| `9c00554` | paginação de fills corrigida; TWAP e todas as dex; cobertura e `complete_to` no cache; retorno diário do copiador limitado a −100% e ruína absorvente; resultados carimbados com hash das configurações e do código |
| `0b0f7a4` | arquivo da Binance publicado com atraso não marca a moeda como não listada; buracos de velas cobertos por outra camada; patrimônio sem olhar o futuro; último dia marcado no preço do fim; posições do início conferidas contra o histórico de funding; etapa B com o mesmo capital da etapa C |
| `fc00455` | liquidação intradiária; patrimônio = última leitura + PnL desde então; funding real até 90% da exposição; custos de entrada e saída do livro; preços da Binance no nível da Hyperliquid |
| `cb58025`, `bedfc1e` | funding plano por dex HIP-3; piso de capital pela alavancagem máxima de cada moeda, também nos dias em que a posição só é mantida |

### 4. Varredura de 15/10/2026 (interrompida)

- **Parâmetros:** `python -m hlwf --budget 1150 scan --min-volume 1e8 --top 150`. Corte em 17/06/2026; teste de 17/06 a 15/10/2026, 08:41 UTC (120 dias).
- **Funil:** 6.836 carteiras com volume ≥ US$ 100M → 2.318 pontuáveis antes do corte → 1.258 com t semanal positivo → 150 para a etapa C (as de maior t).
- **Andamento:** 74 das 150 avaliadas em ~70 min de etapa C (as 74 primeiras pela ordem da etapa B). Depois a varredura foi parada a pedido.

Resultado parcial das 74:

- **46 com dados insuficientes:** fills que não cobrem o teste, poucas entradas, ou posição que muda sem fills visíveis.
- **28 testáveis.** 6 delas ficam fora da família, porque a cópia perdia dinheiro nos 30 dias antes do corte.
- **2 com p < 0,05** sem correção, e 5 com p < 0,10. O acaso sozinho daria ~1,4 com p < 0,05 entre 28.
- **Correção para múltiplos testes:** 12 das 74 ficaram fora da família, contando as insuficientes. Na mesma proporção, a família teria ~125 testes, e a melhor carteira precisaria de p ≲ 0,0008. A melhor até aqui teve p = 0,017. **Nenhuma aprovada até aqui.**

As cinco de menor p (cópia com 30 min de atraso, depois de todos os custos). O retorno é sobre o patrimônio da carteira, com a cópia 1:1:

| carteira | p | retorno composto no teste | PnL (US$) | PnL sem os 5 melhores dias | veredito provisório |
|---|---|---|---|---|---|
| `0xb69eec8fea0081343c3b09e652ba1fa7dcf7033a` | 0,017 | +1.727% | 1,77M | 2,9 mil | promissora, mas frágil (ver abaixo) |
| `0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1` | 0,033 | +18,8% | 140 mil | 47,5 mil | promissora |
| `0x2312171890250347dbf1b082bdf7950504909e92` | 0,066 | +18,9% | 111 mil | −3,9 mil | reprovada: depende dos 5 melhores dias |
| `0xa5fd942d4badbab4fe84a9e10f565dd40d5f15ff` | 0,070 | +40,6% | 696 mil | 29,8 mil | reprovada: sem timing nem alfa sobre o BTC |
| `0x288ed4efc8fbd1e42a06fe083ea942d20c90b336` | 0,078 | +80,2% | 839 mil | −364 mil | reprovada: depende dos 5 melhores dias |

"Promissora" quer dizer que passou em tudo menos na correção para múltiplos testes, que só pode ser calculada com a família inteira.

**Sobre a `0xb69e…033a`:** 99,8% do lucro vem de 5 dias. Num só dia, 30/07/2026, a cópia ganhou 236%: alavancada (média de 5,5x, máxima de 19,5x) em ações de memória e IA nas dex HIP-3 (SKHX +16,6%, DRAM +19,7%, NBIS +31,9% no dia). Conferi esse dia: o movimento é real e um copiador com 30 min de atraso o pegaria, não é erro de preço nem de capital. Mesmo assim a evidência é frágil. O critério "lucro sem os 5 melhores dias > 0" passa por 2,9 mil de 1,77 milhão.

### 5. Auditoria independente (começada, sem conclusão)

Um agente por carteira recalcularia do zero a cópia de 30 min de `0xb69e…033a`, `0xf97a…ddd1` e `0xa5fd…15ff`, a partir dos dados em cache e de uma especificação do modelo, sem importar o código do `hlwf`. Um segundo agente tentaria refutar cada diferença encontrada. As duas tentativas pararam antes de chegar a um resultado: na primeira acabou o limite de uso, e a segunda foi interrompida junto com a varredura. **Não há conclusão da auditoria.**

## Como retomar

O cache em `data/` não vai para o git. Uma sessão nova começa sem ele e precisa baixar tudo de novo. A varredura só é retomada do ponto em que parou dentro de 24 h do início e com o mesmo código; fora disso, recomeça com um corte novo (120 dias antes do dia em que rodar).

```bash
pip install -r requirements.txt

# 1. leaderboard + histórico semanal das ~6.800 carteiras com volume >= US$ 100M (~2 h)
python -m hlwf --budget 1150 portfolios --min-volume 1e8

# 2. varredura completa (~2 h 15 min para 150 carteiras sem cache)
python -m hlwf --budget 1150 scan --min-volume 1e8 --top 150

# 3. validação nas 10 carteiras do Hyperliquid_RE (depois da varredura: as duas dividem o limite da API)
python -m hlwf wallet 0xcac1f1c3e4599e044dc2a9d38a9b6ef1af1bdae8 0xfce053a5e461683454bf37ad66d20344c0e3f4c0 \
  0x16bf84af3f85f8c8a97597bf2be549dfe0dee637 0x0748e296e5a350ec36c8487a1cea406e1b57d4b2 \
  0x8bae3527e5a33fa0cf184f37bc112d071463ab6d 0xd260b2216b735277da6771564a01c04856e78321 \
  0x833b99b27dac651d02080f5e220e929df891db06 0x78dce96236804d6958bf3c4598cf5efbc371629c \
  0xc7c37b0c7af5af560bf31b0bcbfec04b97361275 0x0812f31ccf62c48e6da3da3fbf98663779e1f45d
```

Rode os comandos longos em segundo plano (`nohup ... > data/logs/scan.log 2>&1 &`). O progresso sai uma linha por carteira.

## Próximos passos

1. **Varredura completa** com o código atual. Gera `reports/varredura_<data>/` com `RELATORIO.md`, `resultados.csv`, `etapa_b.csv.gz` e `selecao.json`.
2. **Auditoria independente da cópia** das carteiras de menor p: recalcular o PnL por moeda e por dia sem o código do `hlwf` e conferir os 5 maiores dias de cada uma.
3. **Auditoria estatística do relatório:** recalcular t HAC, p, q de Benjamini-Hochberg e os vereditos a partir dos CSVs diários.
4. **Validação no Hyperliquid_RE** (comando 3 acima) e novo `reports/carteiras/`.
5. **README:** trocar a seção de resultado pelo da varredura completa e remover os relatórios inválidos de 14/10.
6. **Acompanhamento para frente** (`track`) das promissoras. É o teste fora da amostra de verdade, e o único que pode separar habilidade de sorte quando a correção para múltiplos testes não deixa ninguém passar.

## Pontos em aberto

- **Concentração do lucro.** Hoje o critério só pede PnL > 0 sem os 5 melhores dias, e por isso a `0xb69e…033a` passa com 0,2% do lucro. Um critério mais forte seria, por exemplo, pelo menos 25% do lucro sem os 5 melhores dias. Ele deve ser decidido **antes** da próxima varredura, nunca depois de ver o resultado.
- **Carimbo dos resultados.** O campo `commit` de cada resultado lê o HEAD do git na hora de gravar. Se houver um commit durante a varredura, o carimbo aponta para ele. O `code_hash` continua certo.
