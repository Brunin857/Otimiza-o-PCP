"""
FlexOtimiza — Agente de QA (sem API externa)
Roda os testes automatizados e exibe diagnóstico completo com sugestões.
"""

import json
import time
import subprocess
import sys
from pathlib import Path
import streamlit as st
import pandas as pd

BASE_DIR = Path(__file__).parent.resolve()

st.set_page_config(page_title="FlexOtimiza QA", page_icon="🔍", layout="wide")
st.markdown("""<style>
.section  { font-size:16px; font-weight:700; color:#1F4E79; margin:14px 0 6px 0; }
.crit { background:#7b0000; color:white;  padding:2px 10px; border-radius:12px; font-size:12px; }
.alto { background:#c55a11; color:white;  padding:2px 10px; border-radius:12px; font-size:12px; }
.med  { background:#f9a825; color:#333;   padding:2px 10px; border-radius:12px; font-size:12px; }
.low  { background:#adb5bd; color:white;  padding:2px 10px; border-radius:12px; font-size:12px; }
.ok-box  { background:#e2f0d9; border-left:4px solid #375623;
           padding:10px 14px; border-radius:0 8px 8px 0; margin:6px 0; }
.warn-box { background:#fff8e1; border-left:4px solid #f9a825;
            padding:10px 14px; border-radius:0 8px 8px 0; margin:6px 0; }
.err-box  { background:#fce4d6; border-left:4px solid #c55a11;
            padding:10px 14px; border-radius:0 8px 8px 0; margin:6px 0; }
</style>""", unsafe_allow_html=True)

# ── Sugestões pré-definidas para cada aviso/falha ────────────────────────────
# Mapeadas pelo nome do teste → sugestão de correção
SUGGESTIONS = {
    "Distribuição entre máquinas": {
        "severidade": "ALTO",
        "resumo": "O otimizador está concentrando puxadas em poucas máquinas em vez de distribuir entre as 4.",
        "sugestao": "Verifique a função objetivo em optimizer.py. O makespan deve penalizar concentração de carga. Certifique-se que `machine_load` e `makespan` estão corretamente definidos.",
    },
    "Performance: carga típica": {
        "severidade": "MEDIO",
        "resumo": "Tempo de otimização próximo do limite de 25s do Streamlit Cloud.",
        "sugestao": "Reduza max_patterns no pattern_generator.py (atualmente 2000) ou limite o número de itens por rodada. Considere rodar localmente para pedidos grandes.",
    },
    "Performance: carga pesada": {
        "severidade": "MEDIO",
        "resumo": "Com 24+ itens o solver retorna FEASIBLE — pode não ser a solução ótima.",
        "sugestao": "Para cargas pesadas, aumente o time_limit nas Configurações (recomendado rodar localmente) ou divida o lote em rodadas menores.",
    },
    "Janela de manutenção não recebe puxadas": {
        "severidade": "MEDIO",
        "resumo": "Puxadas podem estar sendo agendadas durante janelas de manutenção no sequenciamento greedy.",
        "sugestao": "O sequenciamento greedy em optimizer.py precisa checar janelas de manutenção ao calcular o clock de cada puxada. Revise o loop de machine_pulls.",
    },
    "Demanda sempre satisfeita": {
        "severidade": "CRITICO",
        "resumo": "A demanda dos pedidos não está sendo totalmente atendida pelo otimizador.",
        "sugestao": "Verifique a restrição de demanda em optimizer.py: `model.Add(sum(produced) >= item.remaining)`. Pode haver problema com itens filtrados antes do solver.",
    },
    "Materiais nunca misturados no mesmo padrão": {
        "severidade": "CRITICO",
        "resumo": "Itens de materiais diferentes estão sendo agrupados na mesma puxada.",
        "sugestao": "Revise generate_patterns() em pattern_generator.py — o filtro `i.material == material` deve garantir que apenas itens do mesmo material entrem num padrão.",
    },
    "Bobina grande nunca vai para máquina pequena": {
        "severidade": "CRITICO",
        "resumo": "Bobinas grandes estão sendo alocadas em máquinas pequenas.",
        "sugestao": "Verifique a restrição em optimizer.py: `if pat.bobina_size == BobinaSize.GRANDE and machine.size == MachineSize.PEQUENA: model.Add(use[(p,m)] == 0)`",
    },
    "Padrões respeitam largura útil (com rebarba)": {
        "severidade": "ALTO",
        "resumo": "Padrões de corte excedem a largura útil da bobina-mãe (rebarba não descontada corretamente).",
        "sugestao": "Certifique-se que pattern_generator.py usa `params.large_usable_mm` e `params.small_usable_mm` em vez de `large_width_mm` e `small_width_mm`.",
    },
    "Rebarba não entra no desperdício": {
        "severidade": "ALTO",
        "resumo": "A rebarba está sendo contabilizada no desperdício, distorcendo a métrica.",
        "sugestao": "O cálculo `waste_mm = bobina_width_mm - total_width_mm` deve usar a largura útil, não a largura total. Verifique CuttingPattern em models.py.",
    },
    "Puxadas sequenciais sem sobreposição": {
        "severidade": "ALTO",
        "resumo": "Puxadas na mesma máquina têm sobreposição de horário — impossível na prática.",
        "sugestao": "Verifique o cálculo de start_time_h e end_time_h no loop de machine_pulls em optimizer.py. O clock deve ser atualizado corretamente.",
    },
    "Modo planejamento fora do turno": {
        "severidade": "MEDIO",
        "resumo": "Fora do horário de turno o sistema não planeja para o próximo turno.",
        "sugestao": "Verifique a função planning_h() em app.py. Quando now_h >= shift_end, deve retornar shift_start.",
    },
    "Cálculo de desperdício correto (sem rebarba)": {
        "severidade": "MEDIO",
        "resumo": "O desperdício exibido não corresponde ao cálculo esperado.",
        "sugestao": "waste_mm deve ser: largura_útil - soma_das_larguras_cortadas. Não deve incluir a rebarba.",
    },
}

# Avisos esperados (não são falhas — apenas pontos de atenção)
EXPECTED_WARNINGS = {
    "Performance: carga típica",
    "Performance: carga pesada",
}


def run_tests() -> dict:
    result = subprocess.run(
        [sys.executable, str(BASE_DIR / "tests.py")],
        capture_output=True, text=True, cwd=str(BASE_DIR)
    )
    try:
        with open(BASE_DIR / "test_results.json") as f:
            return json.load(f)
    except Exception:
        return {"summary": {"total":0,"passed":0,"failed":0,"warnings":0}, "tests": [], "raw_output": result.stdout}


def severity_badge(sev: str) -> str:
    cls = {"CRITICO":"crit","ALTO":"alto","MEDIO":"med","BAIXO":"low"}.get(sev, "low")
    return f'<span class="{cls}">{sev}</span>'


# ═════════════════════════════════════════════════════════════════════════════
# LAYOUT
# ═════════════════════════════════════════════════════════════════════════════
st.markdown("## 🔍 FlexOtimiza — Agente de QA")
st.caption("Executa testes automatizados e diagnostica problemas no sistema.")
st.divider()

run_btn = st.button("🚀 Executar Diagnóstico", type="primary", use_container_width=False)

if not run_btn and "qa_results" not in st.session_state:
    st.info("Clique em **Executar Diagnóstico** para rodar os 22 testes e ver o relatório.")
    st.markdown("""
    **O que é testado:**
    - ✅ Correção dos resultados (demanda atendida, materiais, facas, largura)
    - ✅ Controle de turno e horários
    - ✅ Casos de borda (sem estoque, item largo, manutenção)
    - ✅ Performance e escalabilidade
    - ✅ Lógica de negócio (prazo, desperdício, superprodução)
    """)
    st.stop()

if run_btn:
    with st.spinner("Rodando 22 testes... (o solver pode levar ~90 segundos)"):
        t0 = time.time()
        data = run_tests()
        elapsed = time.time() - t0
    st.session_state["qa_results"] = data
    st.session_state["qa_elapsed"] = elapsed

data    = st.session_state.get("qa_results", {"summary":{"total":0,"passed":0,"failed":0,"warnings":0},"tests":[]})
elapsed = st.session_state.get("qa_elapsed", 0)
s       = data["summary"]
tests   = data["tests"]

# ── Métricas ──────────────────────────────────────────────────────────────────
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total",       s["total"])
c2.metric("✅ Passou",   s["passed"])
c3.metric("❌ Falhou",   s["failed"])
c4.metric("⚠️ Avisos",   s["warnings"])
c5.metric("⏱️ Tempo",    f"{elapsed:.0f}s")

if s["failed"] == 0 and s["warnings"] == 0:
    st.markdown("""<div class="ok-box">
    ✅ <strong>Todos os testes passaram sem avisos.</strong> O sistema está saudável.
    </div>""", unsafe_allow_html=True)
elif s["failed"] == 0:
    st.markdown(f"""<div class="warn-box">
    ⚠️ <strong>Testes passaram com {s['warnings']} aviso(s).</strong>
    Veja os detalhes abaixo.
    </div>""", unsafe_allow_html=True)
else:
    st.markdown(f"""<div class="err-box">
    ❌ <strong>{s['failed']} teste(s) falhando.</strong>
    O sistema tem problemas que precisam de correção.
    </div>""", unsafe_allow_html=True)

st.divider()

# ── Problemas encontrados ─────────────────────────────────────────────────────
failed_tests  = [t for t in tests if not t["passed"]]
warning_tests = [t for t in tests if t["passed"] and t["warnings"]]

if failed_tests or warning_tests:
    st.markdown('<div class="section">🔎 Problemas Identificados</div>', unsafe_allow_html=True)

    all_issues = []
    for t in failed_tests:
        info = SUGGESTIONS.get(t["name"], {
            "severidade": "ALTO",
            "resumo": "Teste falhou — verifique o erro abaixo.",
            "sugestao": t["error"][:300] if t["error"] else "Sem detalhes.",
        })
        all_issues.append({"test": t, "info": info, "tipo": "FALHA"})

    for t in warning_tests:
        info = SUGGESTIONS.get(t["name"], {
            "severidade": "MEDIO",
            "resumo": " | ".join(t["warnings"]),
            "sugestao": "Revise o comportamento descrito no aviso.",
        })
        all_issues.append({"test": t, "info": info, "tipo": "AVISO"})

    # Ordena por severidade
    sev_order = {"CRITICO":0,"ALTO":1,"MEDIO":2,"BAIXO":3}
    all_issues.sort(key=lambda x: sev_order.get(x["info"]["severidade"],"BAIXO"))

    for issue in all_issues:
        t    = issue["test"]
        info = issue["info"]
        sev  = info["severidade"]
        badge = severity_badge(sev)
        tipo_icon = "❌" if issue["tipo"] == "FALHA" else "⚠️"

        with st.expander(f"{tipo_icon} {t['name']}", expanded=(sev in ("CRITICO","ALTO"))):
            st.markdown(f"{badge}", unsafe_allow_html=True)
            st.markdown(f"**Problema:** {info['resumo']}")
            st.markdown(f"**Sugestão de correção:** {info['sugestao']}")
            if t.get("error"):
                st.markdown("**Erro do teste:**")
                st.code(t["error"][:600])
            if t.get("warnings"):
                for w in t["warnings"]:
                    st.markdown(f"- ⚠️ {w}")

    st.divider()

# ── Todos os testes — tabela resumo ──────────────────────────────────────────
st.markdown('<div class="section">📋 Todos os Testes</div>', unsafe_allow_html=True)

rows = []
for t in tests:
    if not t["passed"]:
        status = "❌ FALHOU"
    elif t["warnings"]:
        status = "⚠️ AVISO"
    else:
        status = "✅ PASSOU"
    rows.append({
        "Status":   status,
        "Teste":    t["name"],
        "Tempo(s)": t["duration_s"],
        "Detalhe":  (t["detail"] or (t["error"][:80] if t["error"] else "")) ,
    })

st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

# ── Rodapé ────────────────────────────────────────────────────────────────────
st.divider()
if st.button("🔄 Rodar novamente", use_container_width=False):
    if "qa_results" in st.session_state:
        del st.session_state["qa_results"]
    st.rerun()
