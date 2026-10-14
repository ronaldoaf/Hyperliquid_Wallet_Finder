"""Markdown reports (in Portuguese, like the rest of the project's output)."""
from __future__ import annotations

import os
from collections import Counter

import numpy as np
import pandas as pd

from .charts import copy_curves
from .evaluate import Settings
from .pipeline import load_daily, run_dir
from .store import save_json

EXPLORER = "https://app.hyperliquid.xyz/explorer/address/"
ORDER = ["aprovada", "aprovada (inativa)", "promissora", "reprovada", "dados insuficientes", "erro"]


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
    return f"{d} min" if d < 60 else f"{d // 60} h"


def wallet_card(s: dict, cfg: Settings, img_rel: str | None) -> list[str]:
    a = s["address"]
    st = s.get("style") or {}
    L = [f"### [{short(a)}]({EXPLORER}{a}) — {s['verdict']}", "", f"`{a}`", ""]
    if img_rel:
        L += [f"![cópia com atraso]({img_rel})", ""]
    L += ["| atraso | dias | retorno a.a. | Sharpe a.a. | t (HAC) | queda máx. | PnL (escala da carteira) | taxas+spread | funding |",
          "|---|---|---|---|---|---|---|---|---|"]
    for d in ["ref", 30, 60, 120, 240]:
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
        + (f"; intervalo de 90% do retorno a.a. (bootstrap em blocos): {pct(ci[0])} a {pct(ci[1])}" if ci else "") + ".",
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
        L.append("- **Por moeda (cópia de 30 min, janela inteira):** " +
                 ", ".join(f"{x['coin']} {usd(x['pnl'])}" for x in top) + ".")
    pos = s.get("open_positions") or {}
    L.append(f"- **Agora:** patrimônio em perps {usd(s.get('account_value_now'))}; último fill em "
             f"{day(s['last_fill']) if s.get('last_fill') else '–'}; posições abertas: "
             + (", ".join(f"{c} {'+' if v > 0 else ''}{v:g}" for c, v in list(pos.items())[:8]) or "nenhuma") + ".")
    if s.get("role") == "vault":
        L.append("- **É um vault:** dá para depositar nele direto, sem o atraso da cópia.")
    elif s.get("role") == "subAccount" and s.get("master"):
        L.append(f"- **Subconta** de [`{short(s['master'])}`]({EXPLORER}{s['master']}).")
    if s.get("why"):
        L.append("- **Pendências:** " + "; ".join(s["why"]) + ".")
    L.append("")
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
    table = table.sort_values(["_o", "q", "p_30"]).drop(columns="_o")
    table.to_csv(os.path.join(out, "resultados.csv"), index=False, float_format="%.6g")
    cols_b = [c for c in ["address", "ok", "reason", "n_weeks", "n_active", "t_w", "mean_w", "sd_w", "pos_frac",
                          "pnl_in", "maxdd_w", "av_split", "turnover"] if c in b.columns]
    b[cols_b].to_csv(os.path.join(out, "etapa_b.csv.gz"), index=False, float_format="%.6g", compression="gzip")
    by_addr = {s["address"]: s for s in results}
    counts = Counter(table["verdict"])
    split_ts = pd.Timestamp(cfg.split_ms, unit="ms")

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
        f"copiando com {cfg.main_delay_min} min nos {cfg.insample_days} dias antes do corte).".replace(",", "."),
        "- **Resultado:** " + ", ".join(f"{counts.get(v, 0)} {v}" for v in ORDER if counts.get(v, 0)) + ".",
        f"- **Custos do copiador:** taxa *taker* de {num(cfg.taker_bps, 1)} bp (HIP-3: {num(cfg.hip3_fee_mult, 0)}x) "
        "mais meio spread/impacto por faixa de liquidez (1–25 bp), funding horário real nas moedas relevantes; "
        f"alavancagem do copiador limitada a {cfg.max_leverage:.0f}x.",
        "",
        "Critérios para **aprovada** (todos fora da amostra, com custos): q de Benjamini-Hochberg ≤ "
        f"{num(cfg.q_max)} (sobre a família do teste final) para o retorno médio diário com {cfg.main_delay_min} min de atraso; retorno positivo também "
        f"com {', '.join(delay_label(d) for d in cfg.robust_delays_min)}; habilidade além da direção do mercado "
        f"(teste de deslocamento p ≤ {num(cfg.skill_p_max)} ou alfa sobre o BTC com t ≥ {num(cfg.skill_alpha_t, 1)}); "
        f"lucro positivo sem os 5 melhores dias; pelo menos {cfg.min_oos_days} dias e {cfg.min_opens} entradas no teste. "
        "**Promissora:** p < 0,05 sem correção e lucro com 30 e 60 min, mas falha algum outro critério.",
        "",
    ]
    show = table[table["verdict"].isin(["aprovada", "aprovada (inativa)", "promissora"])]
    if len(show):
        L += ["## Carteiras aprovadas e promissoras", "",
              "| carteira | veredito | retorno a.a. 30 min | 1 h | 2 h | Sharpe | t | q | deslocamento p | alfa t | queda máx. | alav. | entradas | dias/posição | moedas principais |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for _, r in show.iterrows():
            L.append(f"| [{short(r['address'])}](#{r['address']}) | {r['verdict']} | {pct(r['ret_ann_30'])} | "
                     f"{pct(r['ret_ann_60'])} | {pct(r['ret_ann_120'])} | {num(r['sharpe_30'], 1)} | {num(r['t_30'])} | "
                     f"{num(r['q'], 3)} | {num(r['shift_p'], 3)} | {num(r['alpha_t'])} | {pct(-r['maxdd_30'] if r['maxdd_30'] is not None else None)} | "
                     f"{num(r['lev_avg'], 1)}x | {int(r['opens'] or 0)} | {num(r['hold_days'], 1)} | {r['top_coins']} |")
        L.append("")
        L += ["Retornos anualizados sobre o patrimônio da carteira (copiar na mesma proporção do patrimônio dela), "
              "soma simples dos retornos diários × 365. Valores altos em janelas de 4 meses têm intervalos largos: "
              "veja o intervalo de bootstrap em cada ficha.", ""]
        L += ["## Fichas", ""]
        for _, r in show.iterrows():
            s = by_addr[r["address"]]
            daily = load_daily(os.path.join(rd, f"{r['address']}.csv.gz"))
            rel = None
            if len(daily):
                fn = f"{r['address']}.png"
                copy_curves(daily, split_ts, os.path.join(img, fn), f"{short(r['address'])}: cópia por atraso")
                rel = f"img/{fn}"
            L.append(f'<a id="{r["address"]}"></a>')
            L += wallet_card(s, cfg, rel)
    else:
        L += ["## Nenhuma carteira passou", "",
              "Nenhuma carteira testada atendeu aos critérios. Isso é um resultado: o teste fora da amostra com "
              "correção para múltiplos testes é exigente de propósito.", ""]
    # why the rest failed
    rest = table[~table["verdict"].isin(["aprovada", "aprovada (inativa)", "promissora"])]
    reasons = Counter()
    for w in rest["why"].fillna(""):
        for part in w.split("; "):
            if part:
                key = part.split(" (")[0]
                reasons[key] += 1
    if reasons:
        L += ["## Por que as outras não passaram", "",
              "Uma carteira pode falhar em mais de um critério.", "",
              "| motivo | carteiras |", "|---|---|"]
        for k, v in reasons.most_common():
            L.append(f"| {k} | {v} |")
        L.append("")
    tested = table[table["verdict"].isin(["reprovada"])].sort_values("t_30", ascending=False).head(15)
    if len(tested):
        L += ["### As 15 melhores entre as reprovadas", "",
              "| carteira | retorno a.a. 30 min | t | q | deslocamento p | alfa t | beta BTC | lucro sem 5 melhores dias | motivo |",
              "|---|---|---|---|---|---|---|---|---|"]
        for _, r in tested.iterrows():
            L.append(f"| [{short(r['address'])}]({EXPLORER}{r['address']}) | {pct(r['ret_ann_30'])} | {num(r['t_30'])} | "
                     f"{num(r['q'], 2)} | {num(r['shift_p'], 3)} | {num(r['alpha_t'])} | {num(r['beta_btc'])} | "
                     f"{usd(r['pnl_ex_top5'])} | {r['why']} |")
        L.append("")
    L += ["## Arquivos", "",
          "- `resultados.csv`: todas as carteiras testadas na etapa C, com as métricas e o motivo do veredito;",
          "- `etapa_b.csv.gz`: a pontuação semanal (antes do corte) de todas as carteiras da etapa B;",
          "- `selecao.json`: as aprovadas e promissoras, com a data, para acompanhar daqui para frente "
          "(`python -m hlwf track reports/varredura_…/selecao.json`).", "",
          "## Limitações", "",
          "- A cópia é simulada com velas de 5 min (Hyperliquid nos últimos 17 dias, Binance antes, validada contra o "
          "fechamento diário da Hyperliquid); moedas sem Binance usam velas de 15 min a 4 h, e o atraso efetivo fica maior.",
          "- Spread e impacto são estimados pela liquidez de hoje, não a da época. Copiadores grandes pagam mais.",
          "- Posições em HIP-3 abertas antes do período de fills e nunca mexidas não aparecem (o estado aberto só é lido "
          "da bolsa principal). Ordens TWAP antigas podem faltar; o salto de posição é aplicado no fill seguinte.",
          "- Fora da amostra não quer dizer futuro: um trader pode mudar de estilo ou parar. Acompanhe com `track`.",
          "- Nada aqui é recomendação de investimento.", ""]
    path = os.path.join(out, "RELATORIO.md")
    with open(path, "w") as f:
        f.write("\n".join(L))
    sel = [{"address": r["address"], "verdict": r["verdict"], "ret_ann_30": r["ret_ann_30"], "q": r["q"]}
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
            copy_curves(daily, split_ts, os.path.join(img, fn), f"{short(s['address'])}: cópia por atraso")
            rel = f"img/{fn}"
        L += wallet_card(s, cfg, rel)
    path = os.path.join(out, f"{tag}.md")
    with open(path, "w") as f:
        f.write("\n".join(L))
    table.to_csv(os.path.join(out, f"{tag}.csv"), index=False, float_format="%.6g")
    return path
