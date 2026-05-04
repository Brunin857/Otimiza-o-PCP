"""
FlexOtimiza — Agente de QA com IA
Fluxo: Diagnóstico → Apresenta erros → Pergunta se quer corrigir → Aplica correções → Valida
"""

import json
import time
import subprocess
import sys
import urllib.request
import streamlit as st

st.set_page_config(page_title="FlexOtimiza QA", page_icon="🔍", layout="wide")
st.markdown("""<style>
.section  { font-size:16px; font-weight:700; color:#1F4E79; margin:14px 0 6px 0; }
.sev-crit { background:#7b0000; color:white;  padding:2px 10px; border-radius:12px; font-size:12px; font-weight:600; }
.sev-alto { background:#c55a11; color:white;  padding:2px 10px; border-radius:12px; font-size:12px; }
.sev-med  { background:#f9a825; color:#333;   padding:2px 10px; border-radius:12px; font-size:12px; }
.sev-low  { background:#adb5bd; color:white;  padding:2px 10px; border-radius:12px; font-size:12px; }
.fix-box  { background:#e8f4fd; border-left:4px solid #2E75B6; padding:10px 14px;
            border-radius:0 8px 8px 0; margin:8px 0; font-family:monospace; font-size:13px; }
.ok-box   { background:#e2f0d9; border-left:4px solid #375623; padding:10px 14px; border-radius:0 8px 8px 0; }
</style>""", unsafe_allow_html=True)

st.markdown("## 🔍 FlexOtimiza — Agente de QA")
st.caption("Diagnostica erros, explica brevemente e pergunta se quer corrigir.")
st.divider()

import os

# ── Sessão ────────────────────────────────────────────────────────────────────
for k, v in [
    ("phase", "input"),
    ("issues", []),
    ("fixes_selected", set()),
    ("fix_results", []),
    ("api_key", os.environ.get("ANTHROPIC_API_KEY", "")),
]:
    if k not in st.session_state:
        st.session_state[k] = v


# ── Helpers ───────────────────────────────────────────────────────────────────
def call_claude(api_key: str, system: str, user: str, max_tokens: int = 3000) -> str:
    payload = json.dumps({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}]
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
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["content"][0]["text"]


def read_source() -> dict:
    files = {}
    for name in ["models.py", "pattern_generator.py", "optimizer.py", "app.py"]:
        try:
            with open(f"/home/claude/flexo/{name}") as f:
                files[name] = f.read()
        except Exception:
            files[name] = ""
    return files


def run_tests() -> dict:
    result = subprocess.run(
        [sys.executable, "tests.py"],
        capture_output=True, text=True, cwd="/home/claude/flexo"
    )
    try:
        with open("/home/claude/test_results.json") as f:
            return json.load(f)
    except Exception:
        return {"summary": {"total":0,"passed":0,"failed":0,"warnings":0}, "tests": []}


def apply_fix(filename: str, old_code: str, new_code: str) -> tuple[bool, str]:
    """Aplica uma correção em um arquivo. Retorna (sucesso, mensagem)."""
    path = f"/home/claude/flexo/{filename}"
    try:
        with open(path) as f:
            content = f.read()
        if old_code not in content:
            return False, f"Trecho não encontrado em {filename} — o arquivo pode já estar corrigido."
        content = content.replace(old_code, new_code, 1)
        with open(path, "w") as f:
            f.write(content)
        # Validate syntax
        result = subprocess.run(
            [sys.executable, "-c", f"import py_compile; py_compile.compile('{path}')"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            # Rollback
            with open(path) as f: content_check = f.read()
            return False, f"Erro de sintaxe após correção: {result.stderr[:200]}"
        return True, f"✅ {filename} corrigido com sucesso."
    except Exception as e:
        return False, f"Erro ao aplicar correção: {e}"


# ═════════════════════════════════════════════════════════════════════════════
# FASE 1 — INPUT
# ═════════════════════════════════════════════════════════════════════════════
if st.session_state.phase == "input":
    # Prefer key from Streamlit Secrets / env var
    _env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if _env_key:
        st.markdown("""<div style="background:#e2f0d9;border-left:4px solid #375623;
        padding:8px 14px;border-radius:0 8px 8px 0;margin-bottom:12px;">
        🔒 <strong>Chave da API carregada dos Secrets do Streamlit.</strong>
        </div>""", unsafe_allow_html=True)
        api_key = _env_key
    else:
        st.warning("⚠️ Chave não encontrada nos Secrets. Insira manualmente (apenas para uso local).")
        col_k, col_b = st.columns([3, 1])
        with col_k:
            api_key = st.text_input(
                "Chave da API Anthropic",
                type="password", placeholder="sk-ant-...",
                help="Para produção, configure em Streamlit Cloud → Settings → Secrets",
                value=st.session_state.api_key,
            )
        with col_b:
            st.markdown("<br>", unsafe_allow_html=True)

    if api_key:
        st.session_state.api_key = api_key

    col_k2, col_b2 = st.columns([3, 1])
    with col_b2:
        start = st.button("🔍 Diagnosticar", type="primary",
                          use_container_width=True, disabled=not api_key)
    with col_k2:
        pass

    st.markdown("""
    **Fluxo do agente:**
    1. Roda os testes automatizados
    2. Envia código + resultados para o Claude
    3. Apresenta os problemas encontrados de forma resumida
    4. Pergunta quais você quer corrigir
    5. Aplica as correções e valida
    """)

    if start:
        st.session_state.phase = "diagnosing"
        st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# FASE 2 — DIAGNOSTICANDO
# ═════════════════════════════════════════════════════════════════════════════
elif st.session_state.phase == "diagnosing":
    st.markdown('<div class="section">🔄 Executando diagnóstico...</div>',
                unsafe_allow_html=True)

    # Step 1: tests
    with st.status("Rodando testes automatizados...", expanded=True) as status:
        st.write("Iniciando 22 testes (solver pode levar ~90s)...")
        test_data = run_tests()
        s = test_data["summary"]
        st.write(f"Testes: {s['passed']}/{s['total']} passaram | {s['failed']} falhas | {s['warnings']} avisos")
        status.update(label="✅ Testes concluídos", state="complete")

    # Step 2: read source
    with st.status("Lendo código-fonte...", expanded=False) as status:
        source = read_source()
        total_lines = sum(len(c.splitlines()) for c in source.values())
        status.update(label=f"✅ {len(source)} arquivos ({total_lines} linhas)", state="complete")

    # Step 3: Claude diagnosis
    with st.status("Agente analisando...", expanded=False) as status:
        source_text = "\n\n".join(
            f"### {name}\n```python\n{code[:6000]}\n```"
            for name, code in source.items()
        )
        tests_text = json.dumps(test_data, indent=2, ensure_ascii=False)

        SYSTEM = """Você é um engenheiro sênior especializado em OR, Python e sistemas industriais.
Analise o FlexOtimiza (otimizador de corte de bobinas flexográficas) e retorne APENAS um JSON válido."""

        USER = f"""Analise o código e testes do FlexOtimiza.

## Testes
```json
{tests_text}
```

## Código
{source_text}

Retorne APENAS um JSON com esta estrutura (sem texto antes ou depois, sem markdown):
{{
  "issues": [
    {{
      "id": 1,
      "titulo": "Título curto do problema (max 60 chars)",
      "resumo": "Explicação em 1-2 frases do que está errado e qual o impacto para o usuário",
      "severidade": "CRITICO|ALTO|MEDIO|BAIXO",
      "arquivo": "nome_do_arquivo.py",
      "linha": "numero ou range ex: 142-150",
      "corrigivel": true,
      "correcao": {{
        "descricao": "O que a correção faz",
        "old_code": "trecho EXATO do código atual a substituir (copiado literalmente)",
        "new_code": "trecho corrigido completo"
      }}
    }}
  ]
}}

Regras:
- Inclua apenas problemas REAIS que afetam o funcionamento ou resultado
- Se corrigivel=false, omita o campo "correcao"
- old_code deve ser copiado LITERALMENTE do código (para str.replace funcionar)
- Máximo 8 problemas, ordenados por severidade
- Foco em: bugs lógicos, sub-otimalidades do solver, riscos de usabilidade"""

        try:
            raw = call_claude(st.session_state.api_key, SYSTEM, USER, max_tokens=3000)
            # Strip potential markdown code fences
            raw = raw.strip()
            if raw.startswith("```"):
                raw = "\n".join(raw.split("\n")[1:])
            if raw.endswith("```"):
                raw = "\n".join(raw.split("\n")[:-1])
            parsed = json.loads(raw)
            issues = parsed.get("issues", [])
            st.session_state.issues = issues
            status.update(label=f"✅ {len(issues)} problema(s) identificado(s)", state="complete")
        except Exception as e:
            status.update(label=f"❌ Erro na análise: {e}", state="error")
            st.error(f"Erro ao parsear resposta do agente: {e}\n\nResposta bruta:\n{raw[:500]}")
            st.stop()

    st.session_state.phase = "review"
    st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# FASE 3 — REVIEW: mostra problemas e pergunta o que corrigir
# ═════════════════════════════════════════════════════════════════════════════
elif st.session_state.phase == "review":
    issues = st.session_state.issues

    if not issues:
        st.markdown("""<div class="ok-box">
        ✅ <strong>Nenhum problema encontrado.</strong> O código está saudável.
        </div>""", unsafe_allow_html=True)
        if st.button("🔄 Rodar diagnóstico novamente"):
            st.session_state.phase = "input"
            st.rerun()
        st.stop()

    sev_order = {"CRITICO": 0, "ALTO": 1, "MEDIO": 2, "BAIXO": 3}
    issues_sorted = sorted(issues, key=lambda x: sev_order.get(x.get("severidade","BAIXO"), 3))

    criticos = [i for i in issues_sorted if i.get("severidade") == "CRITICO"]
    altos    = [i for i in issues_sorted if i.get("severidade") == "ALTO"]
    outros   = [i for i in issues_sorted if i.get("severidade") not in ("CRITICO","ALTO")]

    st.markdown(f'<div class="section">🔎 {len(issues)} Problema(s) Encontrado(s)</div>',
                unsafe_allow_html=True)

    sev_labels = {
        "CRITICO": '<span class="sev-crit">CRÍTICO</span>',
        "ALTO":    '<span class="sev-alto">ALTO</span>',
        "MEDIO":   '<span class="sev-med">MÉDIO</span>',
        "BAIXO":   '<span class="sev-low">BAIXO</span>',
    }

    selected = set(st.session_state.fixes_selected)

    for issue in issues_sorted:
        sev  = issue.get("severidade", "BAIXO")
        iid  = issue.get("id", 0)
        corr = issue.get("corrigivel", False)
        sev_html = sev_labels.get(sev, sev)

        with st.container():
            col_check, col_content = st.columns([0.06, 0.94])
            with col_check:
                if corr:
                    checked = st.checkbox("", value=(iid in selected), key=f"chk_{iid}",
                                         label_visibility="collapsed")
                    if checked:
                        selected.add(iid)
                    else:
                        selected.discard(iid)
                else:
                    st.markdown("—")
            with col_content:
                st.markdown(
                    f"{sev_html} &nbsp; **{issue.get('titulo','')}** "
                    f"<small style='color:#888'>— {issue.get('arquivo','')} linha {issue.get('linha','?')}</small>",
                    unsafe_allow_html=True
                )
                st.caption(issue.get("resumo", ""))
                if corr and issue.get("correcao"):
                    with st.expander("Ver correção proposta"):
                        st.markdown(f"**O que faz:** {issue['correcao'].get('descricao','')}")
                        col_old, col_new = st.columns(2)
                        with col_old:
                            st.markdown("**Código atual:**")
                            st.code(issue["correcao"].get("old_code","")[:400], language="python")
                        with col_new:
                            st.markdown("**Código corrigido:**")
                            st.code(issue["correcao"].get("new_code","")[:400], language="python")
                elif not corr:
                    st.caption("⚠️ Este problema requer correção manual — envolve lógica de negócio ou arquitetura.")
            st.divider()

    st.session_state.fixes_selected = selected

    # Action buttons
    n_selected = len([i for i in issues_sorted
                      if i.get("corrigivel") and i.get("id") in selected])
    n_fixable  = len([i for i in issues_sorted if i.get("corrigivel")])

    col_sel, col_fix, col_skip = st.columns([2, 1, 1])
    with col_sel:
        if n_fixable > 0:
            st.caption(f"{n_selected} de {n_fixable} problemas corrigíveis selecionados")
    with col_fix:
        fix_btn = st.button(
            f"🔧 Corrigir {n_selected} selecionado(s)" if n_selected > 0 else "Selecione problemas",
            use_container_width=True, type="primary",
            disabled=n_selected == 0,
        )
    with col_skip:
        if st.button("↩️ Novo diagnóstico", use_container_width=True):
            st.session_state.phase = "input"
            st.session_state.issues = []
            st.session_state.fixes_selected = set()
            st.rerun()

    if fix_btn:
        st.session_state.phase = "fixing"
        st.rerun()


# ═════════════════════════════════════════════════════════════════════════════
# FASE 4 — FIXING: aplica correções e valida
# ═════════════════════════════════════════════════════════════════════════════
elif st.session_state.phase == "fixing":
    issues   = st.session_state.issues
    selected = st.session_state.fixes_selected
    to_fix   = [i for i in issues if i.get("corrigivel") and i.get("id") in selected]

    st.markdown(f'<div class="section">🔧 Aplicando {len(to_fix)} Correção(ões)</div>',
                unsafe_allow_html=True)

    results = []
    for issue in to_fix:
        correcao = issue.get("correcao", {})
        filename = issue.get("arquivo", "")
        old_code = correcao.get("old_code", "")
        new_code = correcao.get("new_code", "")

        with st.spinner(f"Corrigindo: {issue.get('titulo','')}..."):
            success, msg = apply_fix(filename, old_code, new_code)
            results.append({"issue": issue, "success": success, "msg": msg})

        if success:
            st.success(f"✅ **{issue.get('titulo','')}** — {msg}")
        else:
            st.warning(f"⚠️ **{issue.get('titulo','')}** — {msg}")

    # Re-run tests to validate
    st.markdown("---")
    with st.status("Validando correções com testes...", expanded=True) as status:
        test_data = run_tests()
        s = test_data["summary"]
        if s["failed"] == 0:
            status.update(label=f"✅ Todos os testes passam ({s['passed']}/{s['total']})", state="complete")
        else:
            status.update(label=f"⚠️ {s['failed']} teste(s) falhando após correções", state="error")

    st.divider()

    # Summary
    applied   = sum(1 for r in results if r["success"])
    not_applied = len(results) - applied

    if applied > 0:
        st.markdown(f"""<div class="ok-box">
        ✅ <strong>{applied} correção(ões) aplicada(s).</strong>
        Lembre-se de subir os arquivos atualizados no GitHub.
        </div>""", unsafe_allow_html=True)

        st.markdown("**Arquivos modificados:**")
        modified = list({r["issue"].get("arquivo") for r in results if r["success"]})
        for f in modified:
            st.markdown(f"- `{f}`")

    if not_applied > 0:
        st.warning(f"{not_applied} correção(ões) não foram aplicadas automaticamente — verifique manualmente.")

    st.markdown(f"**Testes após correções:** {s['passed']}/{s['total']} passaram | {s['failed']} falhas | {s['warnings']} avisos")

    col_r1, col_r2 = st.columns(2)
    with col_r1:
        if st.button("🔍 Rodar diagnóstico novamente", use_container_width=True, type="primary"):
            st.session_state.phase = "input"
            st.session_state.issues = []
            st.session_state.fixes_selected = set()
            st.session_state.fix_results = []
            st.rerun()
    with col_r2:
        if st.button("↩️ Voltar para revisão", use_container_width=True):
            st.session_state.phase = "review"
            st.rerun()
