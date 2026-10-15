"""Markdown reports (in Portuguese, like the rest of the project's output)."""
from __future__ import annotations

import os
from collections import Counter

import numpy as np
import pandas as pd

from .charts import copy_curves
from .evaluate import Settings, sample_problem
from .pipeline import load_daily, run_dir
from .store import load_gz, save_json

EXPLORER = "https://app.hyperliquid.xyz/explorer/address/"
ORDER = ["aprovada", "aprovada (inativa)", "promissora", "reprovada", "dados insuficientes", "erro"]


PLURAL = {"aprovada": "aprovadas", "aprovada (inativa)": "aprovadas (inativas)", "promissora": "promissoras",
          "reprovada": "reprovadas", "dados insuficientes": "com dados insuficientes", "erro": "erros"}


def conclusion(counts: Counter, table: pd.DataFrame, cfg: Settings) -> str:
    n_ok = counts.get("aprovada", 0) + counts.get("aprovada (inativa)", 0)
    n_pr = counts.get("promissora", 0)
    if n_ok:
        return (f"{n_ok} carteira(s) passaram em todos os critérios. Ainda assim, acompanhe-as com `track` antes "
                "de copiar: o teste cobre só alguns meses.")
    fam = int(table["in_family"].fillna(False).astype(bool).sum())
    best = table[table["in_family"].fillna(False).astype(bool)]["p_main"].min()
    txt = (f"nenhuma carteira passou. Com {fam} testes na família, a correção de Benjamini-Hochberg pede um "
           f"p perto de {num(cfg.q_max / max(fam, 1), 4)} para a melhor; a melhor teve p = {num(best, 4)}. ")
    if n_pr:
        txt += (f"As {n_pr} promissoras continuaram lucrando com atraso e custos depois do corte, mas um resultado "
                "assim aparece por acaso entre tantas carteiras. O próximo passo é acompanhá-las daqui para frente "
                "(`track`) e só considerar copiar as que mantiverem o resultado.")
    return txt


def short(a: str) -> str:
    return f"{a[:6]}…{a[-4:]}"


def pct(x, nd=0) -> str:
    if x is None or not np.isfinite(x):
        return "–"
    return f"{x * 100:+.{nd}f}%".replace(".", ",")


def share(x) -> str:
    if x is None or not np.isfinite(x):
        return "–"
    return f"{x * 100:.0f}%"


def num(x, nd=2) -> str:
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "–"
    return f"{x:.{nd}f}".replace(".", ",")


def usd(x) -> str:
    if x is None or not np.isfinite(x):
        return "–"
    if abs(x) < 0.5:
        return "US$ 0"
    s = "−" if x < 0 else "+"
    v = abs(x)
    if v >= 1e6:
        return f"{s}US$ {v / 1e6:.2f}M".replace(".", ",")
    if v >= 1e3:
        return f"{s}US$ {v / 1e3:.0f} mil"
    return f"{s}US$ {v:.0f}"


def day(ms) -> str:
    return pd.Timestamp(ms, unit="ms").strftime("%d/%m/%Y")


def delay_label(d) -> str:
    if d == "ref":
        return "imediata, sem custos"
    if d < 60 or d % 60:
        return f"{d} min"
    return f"{d // 60} h"


def delays(cfg: Settings) -> list:
    return ["ref"] + sorted(set(cfg.delays_min) | {cfg.main_delay_min})


def criteria_text(cfg: Settings) -> str:
    """Every rule `classify` applies, written from the settings."""
    main = delay_label(cfg.main_delay_min)
    rob = ", ".join(delay_label(d) for d in cfg.robust_delays_min) or main
    first_rob = delay_label(cfg.robust_delays_min[0]) if cfg.robust_delays_min else main
    return (
        "Critérios para **aprovada** (todos fora da amostra, com custos): (1) dados suficientes: fills completos "
        f"no teste, pelo menos {cfg.min_oos_days} dias de teste, {cfg.min_active_days} dias posicionada e "
        f"{cfg.min_opens} entradas, posição sem fill visível ≤ {cfg.max_jump_share * 100:.0f}% do volume e posições "
        f"reconstruídas batendo com a conta no fim e com o histórico de funding no início (diferença ≤ "
        f"{cfg.max_recon_share * 100:.0f}% do nocional); (2) a cópia com {main} deixa o copiador mais rico: "
        "retorno médio diário, retorno composto e PnL em US$ positivos, sem nenhum dia que leve todo o capital; "
        f"(3) o mesmo com {rob} de atraso; (4) q de Benjamini-Hochberg ≤ {num(cfg.q_max)} (sobre a família do "
        f"teste final) para o retorno médio diário com {main}; (5) habilidade além da direção do mercado: teste "
        f"de deslocamento p ≤ {num(cfg.skill_p_max)} ou alfa sobre o BTC com t ≥ {num(cfg.skill_alpha_t, 1)}; "
        "(6) lucro positivo sem os 5 melhores dias. **Promissora:** dados suficientes, p < 0,05 sem correção e "
        f"o copiador mais rico com {main} e com {first_rob}, mas falha algum outro "
        "critério.")


def wallet_card(s: dict, cfg: Settings, img_rel: str | None) -> list[str]:
    a = s["address"]
    st = s.get("style") or {}
    L = [f"### [{short(a)}]({EXPLORER}{a}) — {s['verdict']}", "", f"`{a}`", ""]
    if not s.get(f"oos_{cfg.main_delay_min}"):
        return L + [f"- **Sem teste:** {'; '.join(s.get('why') or [s.get('reason', '')])}.", ""]
    if img_rel:
        L += [f"![cópia com atraso]({img_rel})", ""]
    L += ["| atraso | dias | retorno a.a. | Sharpe a.a. | t (HAC) | queda máx. | PnL (escala da carteira) | taxas+spread | funding |",
          "|---|---|---|---|---|---|---|---|---|"]
    for d in delays(cfg):
        x = s.get(f"oos_{d}")
        if not x or x.get("days", 0) < 2:
            continue
        L.append(f"| {delay_label(d)} | {x['days']} | {pct(x['ret_ann'])} | {num(x['sharpe_ann'], 1)} | {num(x['t'])} | "
                 f"{pct(-x['maxdd'])} | {usd(x['pnl'])} | {usd(x['fees'])} | {usd(x['funding'])} |")
    L.append("")
    sh = s.get("shift") or {}
    al = s.get("alpha") or {}
    m = s.get(f"oos_{cfg.main_delay_min}") or {}
    ci = s.get("ret_ann_ci90")
    L += [
        f"- **Teste fora da amostra** ({day(s['oos_start'])} → hoje, cópia com {cfg.main_delay_min} min, atraso efetivo médio de "
        f"{num(s.get('eff_delay_min'), 0)} min; posicionada em {m.get('active_days', '–')} dias): p = {num(s.get('p_used'), 4)}, q (BH) = {num(s.get('q'), 3)}"
        + (f"; intervalo de 90% do retorno a.a. (bootstrap em blocos): {pct(ci[0])} a {pct(ci[1])}" if ci else "")
        + (f"; retorno composto no período: {pct(m.get('ret_comp'))}" if m.get("ret_comp") is not None else "")
        + ("; **a cópia perderia todo o capital num dia**" if m.get("ruined") else "") + ".",
        f"- **É habilidade ou mercado?** Teste de deslocamento (mesmas posições em outras datas): p = {num(sh.get('p'), 3)}. "
        f"Regressão no BTC: beta {num(al.get('beta'))}, t do alfa {num(al.get('t_alpha'))}.",
        f"- **Estilo:** {st.get('opens', 0)} entradas em {st.get('coins_traded', 0)} moedas, posição média de "
        f"{num(st.get('hold_days'), 1)} dias, {share(st.get('long_share'))} da exposição comprada, "
        f"posicionado {share(st.get('time_in_market'))} do tempo; alavancagem média {num(m.get('lev_avg'), 1)}x "
        f"(máx. {num(m.get('lev_max'), 1)}x).",
        (f"- **Concentração:** os 5 melhores dias somam {share(m.get('top5_share'))} do PnL; sem eles, "
         f"{usd(m.get('pnl_ex_top5'))}." if (m.get("pnl") or 0) > 0 else
         f"- **Concentração:** PnL total negativo; sem os 5 melhores dias, {usd(m.get('pnl_ex_top5'))}."),
    ]
    bc = s.get("by_coin") or []
    if bc:
        top = sorted(bc, key=lambda x: -abs(x["pnl"]))[:6]
        L.append(f"- **Por moeda (cópia com {delay_label(cfg.main_delay_min)}, período de teste):** " +
                 ", ".join(f"{x['coin']} {usd(x['pnl'])}" for x in top) + ".")
    pos = s.get("open_positions") or {}
    L.append(f"- **Agora:** patrimônio em perps {usd(s.get('account_value_now'))}; último fill em "
             f"{day(s['last_fill']) if s.get('last_fill') else '–'}; posições abertas: "
             + (", ".join(f"{c} {'+' if v > 0 else ''}{v:g}" for c, v in list(pos.items())[:8]) or "nenhuma") + ".")
    L += data_quality(s)
    if s.get("role") == "vault":
        L.append("- **É um vault:** dá para depositar nele direto, sem o atraso da cópia.")
    elif s.get("role") == "subAccount" and s.get("master"):
        L.append(f"- **Subconta** de [`{short(s['master'])}`]({EXPLORER}{s['master']}).")
    if s.get("why"):
        L.append("- **Pendências:** " + "; ".join(s["why"]) + ".")
    L.append("")
    return L


def data_quality(s: dict) -> list[str]:
    """How much of the wallet's book the fills explain."""
    if not s.get("complete_to"):
        return []
    rc = s.get("recon") or {}
    rs = s.get("recon_start") or {}
    count = lambda n: f"{n or 0:,}".replace(",", ".")
    small = lambda x: "–" if x is None or not np.isfinite(x) else f"{x * 100:.1f}%".replace(".", ",")
    txt = (f"- **Dados:** fills completos de {day(s['coverage_start'])} a {day(s['complete_to'])} "
           f"({count(s.get('n_fills'))} fills, {count(s.get('n_twap'))} de TWAP); posição mudando sem fill visível: "
           f"{small(s.get('jump_share'))} do volume; posições reconstruídas contra a conta: diferença de "
           f"{usd(abs(rc.get('gap_usd') or 0.0)).lstrip('+')} ({small(rc.get('share'))} do nocional) no fim e "
           + (f"{usd(abs(rs.get('gap_usd') or 0.0)).lstrip('+')} ({small(rs.get('share'))}) contra o funding no início"
              if rs.get("checked") else "sem conferência no início"))
    pc = s.get("pnl_check_oos")
    if pc:
        txt += (f"; PnL da cópia imediata no teste {usd(pc['sim'])}, PnL de perps da conta no mesmo período "
                f"{usd(pc['real'])}")
    return [txt + "."]


def btc_change(data_dir: str, start_ms: int, end_ms: int) -> float | None:
    rows = load_gz(os.path.join(data_dir, "market", "candles", "BTC_1d.json.gz")) or []
    c = pd.Series({r[0]: r[2] for r in rows}).sort_index()
    # from the close of the day before the start (the candle of day d closes at the end of d)
    a, b = c[c.index <= start_ms - 86_400_000], c[c.index <= end_ms]
    if a.empty or b.empty:
        return None
    return float(b.iloc[-1] / a.iloc[-1] - 1)


def persistence_section(table: pd.DataFrame, results: list[dict], cfg: Settings, data_dir: str = "data") -> list[str]:
    """Is the failure the trader's (no edge after the split) or the copy's
    (delay and costs)? And how many hits would chance alone produce?"""
    by = {s["address"]: s for s in results}
    fam = table[table["in_family"].fillna(False).astype(bool)]
    testable = fam[[sample_problem(by[a], cfg) is None for a in fam["address"]]]
    n = len(testable)
    if n == 0:
        return []
    def count(key, thr):
        c = 0
        for a in testable["address"]:
            x = by[a].get(key) or {}
            if x.get("p", 1.0) < thr and x.get("ret_ann", 0) > 0:
                c += 1
        return c
    k = cfg.main_delay_min
    L = ["## O que sobra do passado", "",
         f"Das {len(fam)} carteiras da família, {n} tinham dados para o teste. Se nenhuma tivesse vantagem real, "
         f"o acaso daria cerca de {num(n * 0.05, 1)} com p < 0,05 e {num(n * 0.01, 1)} com p < 0,01, e metade "
         "teria retorno médio positivo.", "",
         "| cópia | p < 0,05 | p < 0,01 | retorno médio > 0 |", "|---|---|---|---|"]
    for key, lab in ((("oos_ref", "imediata, sem custos (o próprio trader)"),
                      (f"oos_{k}", f"{delay_label(k)}, com custos"))
                     + tuple((f"oos_{d}", f"{delay_label(d)}, com custos") for d in cfg.robust_delays_min)):
        pos = sum(1 for a in testable["address"] if (by[a].get(key) or {}).get("ret_ann", 0) > 0)
        L.append(f"| {lab} | {count(key, 0.05)} | {count(key, 0.01)} | {pos} de {n} |")
    ch = btc_change(data_dir, cfg.split_ms, cfg.end_ms)
    if ch is not None:
        L += ["", f"No mesmo período o BTC variou {pct(ch)}: retorno positivo sozinho pode ser só direção do mercado, "
              "por isso o teste de deslocamento e o alfa."]
    L += ["", "A linha de cima mede se o trader manteve a vantagem depois do corte; as de baixo, quanto dela "
          "sobrevive ao atraso e aos custos. Quando a linha de cima já fica perto do acaso, o problema não é o "
          "atraso: o desempenho passado das carteiras não se repetiu.", ""]
    return L


def write_scan_report(out_root: str, data_dir: str, tag: str, cfg: Settings, funnel: dict, b: pd.DataFrame,
                      table: pd.DataFrame, results: list[dict]) -> str:
    date = pd.Timestamp(cfg.end_ms, unit="ms").strftime("%Y-%m-%d")
    out = os.path.join(out_root, f"varredura_{date}")
    img = os.path.join(out, "img")
    os.makedirs(img, exist_ok=True)
    rd = run_dir(data_dir, tag)
    table = table.copy()
    table["_o"] = table["verdict"].map({v: i for i, v in enumerate(ORDER)}).fillna(9)
    table = table.sort_values(["_o", "q", "p_main"]).drop(columns="_o")
    table.to_csv(os.path.join(out, "resultados.csv"), index=False, float_format="%.6g")
    cols_b = [c for c in ["address", "ok", "reason", "n_weeks", "n_active", "t_w", "mean_w", "sd_w", "pos_frac",
                          "pnl_in", "maxdd_w", "av_split", "turnover"] if c in b.columns]
    b[cols_b].to_csv(os.path.join(out, "etapa_b.csv.gz"), index=False, float_format="%.6g", compression="gzip")
    by_addr = {s["address"]: s for s in results}
    counts = Counter(table["verdict"])
    split_ts = pd.Timestamp(cfg.split_ms, unit="ms")
    main = delay_label(cfg.main_delay_min)
    rob = list(cfg.robust_delays_min)

    L = [f"# Varredura de {pd.Timestamp(cfg.end_ms, unit='ms'):%d/%m/%Y}: carteiras copiáveis com atraso", ""]
    L += [
        "Carteiras da Hyperliquid cuja estratégia em perps **continua lucrativa quando copiada com pelo menos "
        f"{cfg.main_delay_min} minutos de atraso**, depois de taxas, spread e funding, num período que **não** foi usado "
        "para escolhê-las. O método está no [README](../../README.md#método).",
        "",
        "## Resumo",
        "",
        f"- **Corte:** a seleção usou só dados até **{split_ts:%d/%m/%Y}**; o teste da cópia vai de {split_ts:%d/%m/%Y} "
        f"a {pd.Timestamp(cfg.end_ms, unit='ms'):%d/%m/%Y} ({(cfg.end_ms - cfg.split_ms) / 86_400_000:.0f} dias).",
        f"- **Funil:** {funnel['leaderboard']:,} carteiras no leaderboard → {funnel['stage_a']:,} com volume "
        f"≥ US$ {funnel['min_volume'] / 1e6:,.0f}M → {funnel['stage_b_universe']:,} com histórico baixado → "
        f"{funnel['stage_b_scored']:,} pontuáveis antes do corte → {funnel['stage_b_positive']:,} com retorno semanal "
        f"positivo → **{funnel['stage_c']} simuladas** (as de maior t semanal antes do corte) → "
        f"**{funnel.get('family', funnel['stage_c'])} na família do teste final** (as que não perdiam dinheiro "
        f"copiando com {main} nos {cfg.insample_days} dias antes do corte).".replace(",", "."),
        "- **Resultado:** " + ", ".join(f"{counts.get(v, 0)} {PLURAL[v] if counts.get(v, 0) > 1 else v}"
                                        for v in ORDER if counts.get(v, 0)) + ".",
        "- **Conclusão:** " + conclusion(counts, table, cfg),
        f"- **Custos do copiador:** taxa *taker* de {num(cfg.taker_bps, 1)} bp (HIP-3: {num(cfg.hip3_fee_mult, 0)}x) "
        "mais meio spread/impacto por faixa de liquidez (1–25 bp), funding horário real nas moedas relevantes; "
        f"alavancagem do copiador limitada a {cfg.max_leverage:.0f}x.",
        "",
        criteria_text(cfg),
        "",
    ]
    show = table[table["verdict"].isin(["aprovada", "aprovada (inativa)", "promissora"])]
    if len(show):
        L += ["## Carteiras aprovadas e promissoras", "",
              f"| carteira | veredito | retorno a.a. {main} | " + " | ".join(delay_label(d) for d in rob)
              + " | composto no teste | Sharpe | t | q | deslocamento p | alfa t | queda máx. | alav. | entradas | dias/posição | moedas principais |",
              "|---|---|---|" + "---|" * len(rob) + "---|---|---|---|---|---|---|---|---|---|---|"]
        for _, r in show.iterrows():
            L.append(f"| [{short(r['address'])}](#{r['address']}) | {r['verdict']} | {pct(r['ret_ann_main'])} | "
                     + " | ".join(pct(r.get(f"ret_ann_{d}")) for d in rob)
                     + f" | {pct(r['ret_comp_main'])} | {num(r['sharpe_main'], 1)} | {num(r['t_main'])} | "
                     f"{num(r['q'], 3)} | {num(r['shift_p'], 3)} | {num(r['alpha_t'])} | "
                     f"{pct(-r['maxdd_main'] if r['maxdd_main'] is not None else None)} | "
                     f"{num(r['lev_avg'], 1)}x | {int(r['opens'] or 0)} | {num(r['hold_days'], 1)} | {r['top_coins']} |")
        L.append("")
        L += ["Retorno a.a. = média simples dos retornos diários × 365 (sem capitalização), sobre o patrimônio da "
              "carteira, como quem copia na mesma proporção do patrimônio dela. \"Composto no teste\" é quanto o "
              "copiador teria ganhado de fato no período. Em janelas de 4 meses os intervalos são largos: veja o "
              "intervalo de bootstrap em cada ficha.", ""]
        L += ["## Fichas", ""]
        for _, r in show.iterrows():
            s = by_addr[r["address"]]
            daily = load_daily(os.path.join(rd, f"{r['address']}.csv.gz"))
            rel = None
            if len(daily):
                fn = f"{r['address']}.png"
                copy_curves(daily, split_ts, os.path.join(img, fn), f"{short(r['address'])}: cópia por atraso, período de teste",
                            cfg.main_delay_min)
                rel = f"img/{fn}"
            L.append(f'<a id="{r["address"]}"></a>')
            L += wallet_card(s, cfg, rel)
    else:
        L += ["## Nenhuma carteira passou", "",
              "Nenhuma carteira testada atendeu aos critérios. Isso é um resultado: o teste fora da amostra com "
              "correção para múltiplos testes é exigente de propósito.", ""]
    L += persistence_section(table, results, cfg, data_dir)
    # why the rest failed
    rest = table[~table["verdict"].isin(["aprovada", "aprovada (inativa)", "promissora"])]
    reasons = Counter()
    for w in rest["why"].fillna(""):
        for part in w.replace("; fills só desde", ", fills só desde").split("; "):
            if part:
                key = part.split(" (")[0].replace("dias, fills", "dias")
                reasons[key] += 1
    if reasons:
        L += ["## Por que as outras não passaram", "",
              "Uma carteira pode falhar em mais de um critério.", "",
              "| motivo | carteiras |", "|---|---|"]
        for k, v in reasons.most_common():
            L.append(f"| {k} | {v} |")
        L.append("")
    tested = table[table["verdict"].isin(["reprovada"])].sort_values("t_main", ascending=False).head(15)
    if len(tested):
        L += ["### As 15 melhores entre as reprovadas", "",
              f"| carteira | retorno a.a. {main} | t | q | deslocamento p | alfa t | beta BTC | lucro sem 5 melhores dias | motivo |",
              "|---|---|---|---|---|---|---|---|---|"]
        for _, r in tested.iterrows():
            L.append(f"| [{short(r['address'])}]({EXPLORER}{r['address']}) | {pct(r['ret_ann_main'])} | {num(r['t_main'])} | "
                     f"{num(r['q'], 2)} | {num(r['shift_p'], 3)} | {num(r['alpha_t'])} | {num(r['beta_btc'])} | "
                     f"{usd(r['pnl_ex_top5'])} | {r['why']} |")
        L.append("")
    L += ["## Arquivos", "",
          "- `resultados.csv`: todas as carteiras testadas na etapa C, com as métricas e o motivo do veredito;",
          "- `etapa_b.csv.gz`: a pontuação semanal (antes do corte) de todas as carteiras da etapa B;",
          "- `selecao.json`: as aprovadas e promissoras, com a data, para acompanhar daqui para frente "
          "(`python -m hlwf track reports/varredura_…/selecao.json`).", "",
          "## Limitações", "",
          "- A cópia é simulada com velas de 5 min (Binance até ontem, validada contra o fechamento diário da "
          "Hyperliquid, e Hyperliquid nas últimas horas). Moedas sem Binance (HIP-3 e as exclusivas da Hyperliquid) "
          "usam velas de 15 min nos últimos 52 dias e de 1 h antes disso: o atraso efetivo fica maior (mais conservador).",
          "- Spread e impacto são estimados pela liquidez de hoje, não a da época. Copiadores grandes pagam mais.",
          "- A seleção ainda vê um pouco do período de teste: o volume que define o universo (etapa A) e o giro que "
          "exclui robôs de alta frequência (etapa B) são o volume total de hoje menos o do último mês, porque a API não "
          "dá o histórico de volume. Carteiras que só passaram do limite operando entre o corte e o último mês entram; "
          "o efeito é de sobrevivência (favorece quem continuou operando). A triagem antes do corte também depende de "
          "quantos fills a API ainda guarda e usa o custo de liquidez de hoje.",
          "- A API serve só a parte mais recente dos fills (de horas a anos, conforme a carteira) e as fatias de TWAP "
          "das últimas semanas. Uma carteira sai como dados insuficientes se os fills não cobrem o teste até o fim, se "
          "a posição muda sem fill visível em mais de 2% do volume, ou se as posições reconstruídas não batem com as "
          "da conta (todas as dex, inclusive HIP-3).",
          "- Fora da amostra não quer dizer futuro: um trader pode mudar de estilo ou parar. Acompanhe com `track`.",
          "- Nada aqui é recomendação de investimento.", ""]
    path = os.path.join(out, "RELATORIO.md")
    with open(path, "w") as f:
        f.write("\n".join(L))
    sel = [{"address": r["address"], "verdict": r["verdict"], "ret_ann_main": r["ret_ann_main"], "q": r["q"]}
           for _, r in show.iterrows()]
    save_json(os.path.join(out, "selecao.json"), {"selected_at": int(cfg.end_ms), "split_ms": int(cfg.split_ms),
                                                  "wallets": sel})
    return path


def write_wallet_report(out_root: str, data_dir: str, tag: str, cfg: Settings, table: pd.DataFrame,
                        results: list[dict], title: str) -> str:
    out = os.path.join(out_root, "carteiras")
    img = os.path.join(out, "img")
    os.makedirs(img, exist_ok=True)
    rd = run_dir(data_dir, tag)
    split_ts = pd.Timestamp(cfg.split_ms, unit="ms")
    L = [f"# {title}", "",
         f"Cópia simulada de {split_ts:%d/%m/%Y} a {pd.Timestamp(cfg.end_ms, unit='ms'):%d/%m/%Y}, com custos. "
         "Cada carteira é um teste isolado (sem seleção), então o q aqui é só sobre as carteiras desta lista.", ""]
    for s in results:
        if s.get("status") == "erro":
            L += [f"### {s['address']}: erro", "", f"`{s.get('reason')}`", ""]
            continue
        daily = load_daily(os.path.join(rd, f"{s['address']}.csv.gz"))
        rel = None
        if len(daily):
            fn = f"{s['address']}_{tag}.png"
            copy_curves(daily, split_ts, os.path.join(img, fn), f"{short(s['address'])}: cópia por atraso, período de teste",
                        cfg.main_delay_min)
            rel = f"img/{fn}"
        L += wallet_card(s, cfg, rel)
    path = os.path.join(out, f"{tag}.md")
    with open(path, "w") as f:
        f.write("\n".join(L))
    table.to_csv(os.path.join(out, f"{tag}.csv"), index=False, float_format="%.6g")
    return path
