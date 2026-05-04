"""
FlexOtimiza — Agente de QA com IA
Página Streamlit separada: roda testes + análise Claude API
"""

import io
import json
import time
import subprocess
import sys
import streamlit as st

st.set_page_config(page_title="FlexOtimiza QA", page_icon="🔍", layout="wide")

st.markdown("""<style>
.section { font-size:17px; font-weight:700; color:#1F4E79; margin:16px 0 6px 0; }
.pass  { background:#e2f0d9; color:#375623; padding:2px 10px; border-radius:12px; font-size:13px; }
.fail  { background:#fce4d6; color:#c55a11; padding:2px 10px; border-radius:12px; font-size:13px; }
.warn  { background:#fff8e1; color:#7d5a00; padding:2px 10px; border-radius:12px; font-size:13px; }
.report-box { background:#f8f9fa; border:1px solid #dee2e6; border-radius:8px;
              padding:20px 24px; font-family: 'Courier New', monospace; font-size:13px;
              white-space:pre-wrap; line-height:1.6; }
</style>""", unsafe_allow_html=True)

st.markdown("## 🔍 FlexOtimiza — Agente de QA com IA")
st.caption("Executa testes automatizados + análise crítica do código via Claude API")
st.divider()

# ── API Key ───────────────────────────────────────────────────────────────────
col_key, col_btn = st.columns([3, 1])
with col_key:
    api_key = st.text_input(
        "Chave da API Anthropic",
        type="password",
        placeholder="sk-ant-...",
        help="Obtenha em console.anthropic.com. A chave não é armazenada."
    )
with col_btn:
    st.markdown("<br>", unsafe_allow_html=True)
    run_btn = st.button("🚀 Executar QA Completo", use_container_width=True,
                        type="primary", disabled=not api_key)

st.divider()

if not run_btn:
    st.info("Insira sua chave da API Anthropic e clique em **Executar QA Completo** para iniciar.")
    st.markdown("""
    **O que o agente faz:**
    1. **Roda 22 testes automatizados** — correção, edge cases, performance, lógica de negócio
    2. **Envia código-fonte + resultados** para o Claude Sonnet
    3. **Recebe análise crítica** com bugs, riscos e melhorias priorizadas
    """)
    st.stop()

# ── Step 1: Run tests ─────────────────────────────────────────────────────────
st.markdown('<div class="section">Passo 1 — Executando Testes Automatizados</div>',
            unsafe_allow_html=True)

with st.spinner("Rodando 22 testes... (pode levar ~90 segundos por conta do solver)"):
    t0 = time.time()
    result = subprocess.run(
        [sys.executable, "tests.py"],
        capture_output=True, text=True, cwd="/home/claude/flexo"
    )
    elapsed = time.time() - t0

if result.returncode != 0 and "test_results.json" not in result.stdout:
    st.error(f"Erro ao rodar testes:\n{result.stderr}")
    st.stop()

# Parse results
try:
    with open("/home/claude/test_results.json") as f:
        test_data = json.load(f)
except Exception as e:
    st.error(f"Não foi possível ler resultados: {e}")
    st.stop()

s = test_data["summary"]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Total", s["total"])
c2.metric("✅ Passou", s["passed"])
c3.metric("❌ Falhou", s["failed"])
c4.metric("⚠️ Avisos", s["warnings"])

# Show test table
rows = []
for t in test_data["tests"]:
    status = "✅ PASS" if t["passed"] else "❌ FAIL"
    rows.append({
        "Status":   status,
        "Teste":    t["name"],
        "Tempo(s)": t["duration_s"],
        "Detalhe":  t["detail"] or t["error"][:80] if t["error"] else "",
        "Avisos":   " | ".join(t["warnings"]) if t["warnings"] else "",
    })

import pandas as pd
st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

if s["failed"] > 0:
    with st.expander("Ver erros completos"):
        for t in test_data["tests"]:
            if not t["passed"]:
                st.markdown(f"**{t['name']}**")
                st.code(t["error"])

st.divider()

# ── Step 2: Read source files ─────────────────────────────────────────────────
st.markdown('<div class="section">Passo 2 — Lendo Código-Fonte</div>',
            unsafe_allow_html=True)

def read_file(path):
    try:
        with open(path) as f: return f.read()
    except Exception: return f"[não encontrado: {path}]"

files = {
    "models.py":            read_file("/home/claude/flexo/models.py"),
    "pattern_generator.py": read_file("/home/claude/flexo/pattern_generator.py"),
    "optimizer.py":         read_file("/home/claude/flexo/optimizer.py"),
    "app.py":               read_file("/home/claude/flexo/app.py"),
}

total_lines = sum(len(c.splitlines()) for c in files.values())
st.success(f"✅ {len(files)} arquivos lidos — {total_lines} linhas de código")

st.divider()

# ── Step 3: Claude analysis ───────────────────────────────────────────────────
st.markdown('<div class="section">Passo 3 — Análise do Agente de IA</div>',
            unsafe_allow_html=True)

source_summary = "\n\n".join(
    f"### {name} ({len(code.splitlines())} linhas)\n```python\n{code[:8000]}\n```"
    for name, code in files.items()
)

tests_summary = json.dumps(test_data, indent=2, ensure_ascii=False)

SYSTEM_PROMPT = """Você é um engenheiro sênior de software especializado em:
- Pesquisa Operacional e otimização combinatória (CP-SAT, MIP)
- Python, Streamlit e arquitetura de aplicações industriais
- Engenharia de Produção e sistemas de PCP

Analise o FlexOtimiza — sistema de otimização de corte de bobinas flexográficas.
Seja CRÍTICO e HONESTO. Foque em problemas reais que afetam o objetivo principal:
gerar sequenciamento ótimo que minimize desperdício, setup e atraso de entrega."""

USER_PROMPT = f"""Analise o FlexOtimiza com base no código e testes abaixo.

## Resultados dos Testes
```json
{tests_summary}
```

## Código-Fonte
{source_summary}

## Relatório esperado

### 1. BUGS IDENTIFICADOS
Para cada bug: localização (arquivo:linha), descrição, impacto, severidade (CRÍTICO/ALTO/MÉDIO/BAIXO), correção sugerida.

### 2. PROBLEMAS NO MODELO DE OTIMIZAÇÃO
- A formulação matemática está correta?
- O greedy pós-solver introduz sub-otimalidade significativa?
- O makespan como proxy é uma aproximação válida?
- Há cenários em que o modelo produz resultados incorretos?

### 3. RISCOS DE USABILIDADE
Situações em que o sistema pode confundir o supervisor:
- Métricas ambíguas
- Fluxos sem feedback adequado
- Ações sem confirmação

### 4. PERFORMANCE E ESCALABILIDADE
Com base nos avisos de tempo dos testes:
- Limite prático de pedidos no Streamlit Cloud?
- Gargalos identificados no código?
- O limite de 2000 padrões no pattern_generator é adequado para cenários reais?

### 5. TOP 5 MELHORIAS (por prioridade)
| Prioridade | Melhoria | Justificativa | Esforço |
Para cada melhoria, inclua esforço estimado em dias.

### 6. MELHORIAS DE LONGO PRAZO
Sugestões arquiteturais para escalabilidade futura.

Seja específico e técnico. Não elogie o que funciona — foque no que precisa mudar."""

with st.spinner("🤖 Claude analisando código e testes..."):
    import urllib.request

    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4000,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": USER_PROMPT}]
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    )

    error_msg = None
    report = None
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        report = data["content"][0]["text"]
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        error_msg = f"Erro HTTP {e.code}: {body}"
    except Exception as e:
        error_msg = str(e)

if error_msg:
    st.error(f"Erro na API: {error_msg}")
    st.stop()

# ── Step 4: Display report ────────────────────────────────────────────────────
st.divider()
st.markdown('<div class="section">📋 Relatório do Agente de QA</div>', unsafe_allow_html=True)
st.markdown(report)

# Save and offer download
report_full = f"# FlexOtimiza — Relatório QA\n\n**Testes:** {s['passed']}/{s['total']} | Avisos: {s['warnings']}\n\n---\n\n{report}"
with open("/home/claude/qa_report.md", "w") as f:
    f.write(report_full)

st.divider()
st.download_button(
    "📥 Baixar relatório (.md)",
    data=report_full.encode(),
    file_name="flexotimiza_qa_report.md",
    mime="text/markdown",
)
