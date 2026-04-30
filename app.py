import streamlit as st
import pandas as pd
from models import (
    OrderItem, BobinaStock, Machine, SetupParams, GlobalParams,
    MaintenanceWindow, MaterialType, BobinaSize, MachineSize,
)
from optimizer import optimize

# ── Configuração da página ────────────────────────────────────────────────────
st.set_page_config(
    page_title="FlexOtimiza — Planejamento de Corte",
    page_icon="🎯",
    layout="wide",
)

st.markdown("""
<style>
    .metric-box {
        background: #f0f4ff;
        border-radius: 10px;
        padding: 16px 20px;
        text-align: center;
        border-left: 5px solid #2E75B6;
    }
    .metric-label { font-size: 13px; color: #555; margin-bottom: 4px; }
    .metric-value { font-size: 26px; font-weight: bold; color: #1F4E79; }
    .metric-sub   { font-size: 12px; color: #888; margin-top: 2px; }
    .ok-badge  { background:#e2f0d9; color:#375623; padding:2px 10px; border-radius:20px; font-size:13px; }
    .err-badge { background:#fce4d6; color:#c55a11; padding:2px 10px; border-radius:20px; font-size:13px; }
    .section-title { font-size:18px; font-weight:700; color:#1F4E79; margin-bottom:8px; }
</style>
""", unsafe_allow_html=True)

MATERIALS = [m.value for m in MaterialType]
MAT_MAP   = {m.value: m for m in MaterialType}

# ── Estado da sessão ──────────────────────────────────────────────────────────
if "items_df" not in st.session_state:
    st.session_state.items_df = pd.DataFrame(columns=[
        "Pedido", "Item", "Largura (mm)", "Quantidade", "Material", "Prazo (h)", "Produzido"
    ])
if "stock_df" not in st.session_state:
    st.session_state.stock_df = pd.DataFrame(columns=[
        "Bobina", "Tamanho", "Material", "Quantidade"
    ])
if "maint_df" not in st.session_state:
    st.session_state.maint_df = pd.DataFrame(columns=[
        "Máquina", "Início (h)", "Duração (h)"
    ])
if "result" not in st.session_state:
    st.session_state.result = None

# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("## 🎯 FlexOtimiza — Planejamento de Corte de Bobinas")
st.caption("Otimizador de sequenciamento e agrupamento para indústria flexográfica")
st.divider()

# ── Abas principais ───────────────────────────────────────────────────────────
tab_pedidos, tab_estoque, tab_config, tab_resultado = st.tabs([
    "📋 Pedidos", "📦 Estoque de Bobinas-Mãe", "⚙️ Configurações", "📊 Resultado"
])

# ════════════════════════════════════════════════════════════════════════════════
# TAB 1 — PEDIDOS
# ════════════════════════════════════════════════════════════════════════════════
with tab_pedidos:
    st.markdown('<div class="section-title">Fila de Pedidos</div>', unsafe_allow_html=True)
    st.caption("Cada linha representa um item de um pedido. Um pedido pode ter múltiplos itens com larguras e materiais diferentes.")

    col_form, col_table = st.columns([1, 2])

    with col_form:
        st.markdown("**Adicionar item**")
        with st.form("form_pedido", clear_on_submit=True):
            pedido_id  = st.text_input("Nº do Pedido", placeholder="P001")
            item_id    = st.text_input("ID do Item", placeholder="P001-A")
            largura    = st.number_input("Largura (mm)", min_value=1, max_value=2000, value=100, step=1)
            quantidade = st.number_input("Quantidade", min_value=1, max_value=200, value=1)
            material   = st.selectbox("Material", MATERIALS)
            prazo      = st.number_input("Prazo (horas)", min_value=1, max_value=240, value=72)
            produzido  = st.number_input("Já produzido", min_value=0, value=0,
                                          help="Preencha ao rerrodar o otimizador no decorrer do dia")
            submitted = st.form_submit_button("➕ Adicionar Item", use_container_width=True)

        if submitted:
            if not pedido_id or not item_id:
                st.warning("Preencha o número do pedido e o ID do item.")
            elif item_id in st.session_state.items_df["Item"].values:
                st.warning(f"Item '{item_id}' já existe. Use um ID diferente.")
            else:
                nova_linha = pd.DataFrame([{
                    "Pedido": pedido_id,
                    "Item": item_id,
                    "Largura (mm)": largura,
                    "Quantidade": quantidade,
                    "Material": material,
                    "Prazo (h)": prazo,
                    "Produzido": produzido,
                }])
                st.session_state.items_df = pd.concat(
                    [st.session_state.items_df, nova_linha], ignore_index=True
                )
                st.success(f"Item {item_id} adicionado.")

        # Import Excel
        st.markdown("---")
        st.markdown("**Importar do Excel**")
        uploaded = st.file_uploader(
            "Planilha com colunas: Pedido, Item, Largura (mm), Quantidade, Material, Prazo (h), Produzido",
            type=["xlsx", "csv"]
        )
        if uploaded:
            try:
                if uploaded.name.endswith(".csv"):
                    df_import = pd.read_csv(uploaded)
                else:
                    df_import = pd.read_excel(uploaded)
                required = {"Pedido","Item","Largura (mm)","Quantidade","Material","Prazo (h)"}
                if required.issubset(set(df_import.columns)):
                    if "Produzido" not in df_import.columns:
                        df_import["Produzido"] = 0
                    st.session_state.items_df = df_import[list(required | {"Produzido"})].copy()
                    st.success(f"{len(df_import)} itens importados.")
                else:
                    st.error(f"Colunas esperadas: {required}")
            except Exception as e:
                st.error(f"Erro ao importar: {e}")

    with col_table:
        st.markdown("**Itens na fila**")
        if st.session_state.items_df.empty:
            st.info("Nenhum item adicionado ainda.")
        else:
            edited = st.data_editor(
                st.session_state.items_df,
                use_container_width=True,
                num_rows="dynamic",
                key="items_editor",
            )
            st.session_state.items_df = edited

            # resumo por pedido
            st.markdown("**Resumo por pedido**")
            summary = (
                st.session_state.items_df
                .groupby("Pedido")
                .agg(Itens=("Item","count"), Prazo_min=("Prazo (h)","min"))
                .reset_index()
                .rename(columns={"Prazo_min": "Prazo (h)"})
            )
            st.dataframe(summary, use_container_width=True, hide_index=True)

# ════════════════════════════════════════════════════════════════════════════════
# TAB 2 — ESTOQUE
# ════════════════════════════════════════════════════════════════════════════════
with tab_estoque:
    st.markdown('<div class="section-title">Estoque de Bobinas-Mãe</div>', unsafe_allow_html=True)
    st.caption("Informe as bobinas-mãe disponíveis antes de rodar o otimizador.")

    col_s1, col_s2 = st.columns([1, 2])
    with col_s1:
        st.markdown("**Adicionar bobina-mãe**")
        with st.form("form_stock", clear_on_submit=True):
            bid      = st.text_input("ID da Bobina", placeholder="BM-001")
            tamanho  = st.selectbox("Tamanho", ["Grande (~2000mm)", "Pequena (~1000mm)"])
            mat_bob  = st.selectbox("Material", MATERIALS)
            qtd_bob  = st.number_input("Quantidade", min_value=1, max_value=100, value=1)
            sub_s    = st.form_submit_button("➕ Adicionar", use_container_width=True)
        if sub_s:
            if not bid:
                st.warning("Informe o ID da bobina.")
            elif bid in st.session_state.stock_df["Bobina"].values:
                st.warning("ID já existe.")
            else:
                nova = pd.DataFrame([{"Bobina": bid, "Tamanho": tamanho, "Material": mat_bob, "Quantidade": qtd_bob}])
                st.session_state.stock_df = pd.concat([st.session_state.stock_df, nova], ignore_index=True)
                st.success("Bobina adicionada.")

    with col_s2:
        st.markdown("**Estoque atual**")
        if st.session_state.stock_df.empty:
            st.info("Nenhuma bobina registrada.")
        else:
            edited_s = st.data_editor(st.session_state.stock_df, use_container_width=True,
                                       num_rows="dynamic", key="stock_editor")
            st.session_state.stock_df = edited_s

# ════════════════════════════════════════════════════════════════════════════════
# TAB 3 — CONFIGURAÇÕES
# ════════════════════════════════════════════════════════════════════════════════
with tab_config:
    st.markdown('<div class="section-title">Configurações</div>', unsafe_allow_html=True)

    col_c1, col_c2, col_c3 = st.columns(3)

    with col_c1:
        st.markdown("**⏱️ Parâmetros de Setup**")
        st.caption("Valores a preencher com dados da cronoanálise em campo.")
        fixed_time    = st.number_input("Tempo fixo de setup (min)", min_value=1.0, value=15.0, step=0.5)
        knife_time    = st.number_input("Tempo por faca alterada (min)", min_value=0.1, value=2.5, step=0.1)

        st.markdown("**🏭 Parâmetros das Máquinas**")
        bobina_len    = st.number_input("Comprimento padrão da bobina-mãe (m)", min_value=100.0, value=2000.0, step=100.0)
        speed         = st.number_input("Velocidade de corte (m/min)", min_value=100.0, value=225.0, step=5.0)
        large_width   = st.number_input("Largura bobina grande (mm)", min_value=500, value=2000, step=50)
        small_width   = st.number_input("Largura bobina pequena (mm)", min_value=200, value=1000, step=50)

    with col_c2:
        st.markdown("**⚖️ Pesos da Função Objetivo**")
        st.caption("Ajuste os pesos para definir o que o otimizador deve priorizar.")

        alpha = st.slider("α — Peso do Desperdício de Material", 0.0, 5.0, 1.0, 0.5,
                          help="Quanto maior, mais o otimizador evita sobras de largura")
        beta  = st.slider("β — Peso do Tempo de Setup", 0.0, 5.0, 1.0, 0.5,
                          help="Quanto maior, mais o otimizador minimiza trocas de faca")
        gamma = st.slider("γ — Peso do Atraso", 0.0, 5.0, 3.0, 0.5,
                          help="Quanto maior, mais o otimizador prioriza cumprimento do prazo de 72h")
        delta = st.slider("δ — Peso da Superprodução", 0.0, 5.0, 0.5, 0.5,
                          help="Quanto maior, mais o otimizador evita produzir além do pedido")

        st.markdown("**⏰ Tempo máximo do solver**")
        time_limit = st.number_input("Limite de tempo (segundos)", min_value=10, max_value=300, value=60)

    with col_c3:
        st.markdown("**🔧 Janelas de Manutenção**")
        st.caption("Informe as máquinas que estarão paradas e por quanto tempo.")
        with st.form("form_maint", clear_on_submit=True):
            mach_id   = st.selectbox("Máquina", ["G1", "G2", "P1", "P2"])
            start_h   = st.number_input("Início (h a partir de agora)", min_value=0.0, value=2.0, step=0.5)
            dur_h     = st.number_input("Duração (h)", min_value=0.5, value=1.0, step=0.5)
            sub_m     = st.form_submit_button("➕ Adicionar Janela", use_container_width=True)
        if sub_m:
            nova_m = pd.DataFrame([{"Máquina": mach_id, "Início (h)": start_h, "Duração (h)": dur_h}])
            st.session_state.maint_df = pd.concat([st.session_state.maint_df, nova_m], ignore_index=True)

        if not st.session_state.maint_df.empty:
            edited_m = st.data_editor(st.session_state.maint_df, use_container_width=True,
                                       num_rows="dynamic", key="maint_editor")
            st.session_state.maint_df = edited_m
        else:
            st.info("Sem janelas de manutenção.")

    # Salva configurações no estado
    st.session_state["cfg"] = dict(
        fixed_time=fixed_time, knife_time=knife_time,
        bobina_len=bobina_len, speed=speed,
        large_width=large_width, small_width=small_width,
        alpha=alpha, beta=beta, gamma=gamma, delta=delta,
        time_limit=time_limit,
    )

# ════════════════════════════════════════════════════════════════════════════════
# BOTÃO OTIMIZAR (fora das abas, sempre visível)
# ════════════════════════════════════════════════════════════════════════════════
st.divider()
col_btn1, col_btn2, col_btn3 = st.columns([2, 1, 1])
with col_btn2:
    run_btn = st.button("🚀 Otimizar Agora", use_container_width=True, type="primary")
with col_btn3:
    if st.button("🗑️ Limpar Tudo", use_container_width=True):
        for key in ["items_df", "stock_df", "maint_df", "result"]:
            del st.session_state[key]
        st.rerun()

if run_btn:
    errors = []
    if st.session_state.items_df.empty:
        errors.append("Nenhum item de pedido informado.")
    if st.session_state.stock_df.empty:
        errors.append("Nenhuma bobina-mãe no estoque.")
    if errors:
        for e in errors:
            st.error(e)
    else:
        cfg = st.session_state.get("cfg", {})

        # Monta objetos do modelo
        params = GlobalParams(
            bobina_length_m=cfg.get("bobina_len", 2000),
            speed_mpm=cfg.get("speed", 225),
            large_width_mm=cfg.get("large_width", 2000),
            small_width_mm=cfg.get("small_width", 1000),
        )
        setup = SetupParams(
            fixed_time_min=cfg.get("fixed_time", 15),
            time_per_knife_min=cfg.get("knife_time", 2.5),
        )
        machines = [
            Machine("G1", MachineSize.GRANDE,  params),
            Machine("G2", MachineSize.GRANDE,  params),
            Machine("P1", MachineSize.PEQUENA, params),
            Machine("P2", MachineSize.PEQUENA, params),
        ]

        items = []
        for _, row in st.session_state.items_df.iterrows():
            mat = MAT_MAP.get(row["Material"])
            if mat is None:
                continue
            items.append(OrderItem(
                item_id=str(row["Item"]),
                order_id=str(row["Pedido"]),
                width_mm=int(row["Largura (mm)"]),
                quantity=int(row["Quantidade"]),
                material=mat,
                deadline_h=float(row["Prazo (h)"]),
                produced=int(row.get("Produzido", 0)),
            ))

        stock = []
        size_map = {"Grande (~2000mm)": BobinaSize.GRANDE, "Pequena (~1000mm)": BobinaSize.PEQUENA}
        for _, row in st.session_state.stock_df.iterrows():
            mat = MAT_MAP.get(row["Material"])
            sz  = size_map.get(row["Tamanho"], BobinaSize.GRANDE)
            if mat is None:
                continue
            stock.append(BobinaStock(
                bobina_id=str(row["Bobina"]),
                size=sz,
                material=mat,
                quantity=int(row["Quantidade"]),
            ))

        maintenance = []
        for _, row in st.session_state.maint_df.iterrows():
            maintenance.append(MaintenanceWindow(
                machine_id=str(row["Máquina"]),
                start_h=float(row["Início (h)"]),
                duration_h=float(row["Duração (h)"]),
            ))

        with st.spinner("Calculando sequenciamento ótimo..."):
            try:
                result = optimize(
                    items=items, stock=stock, machines=machines,
                    setup=setup, params=params, maintenance=maintenance,
                    alpha=cfg.get("alpha", 1.0), beta=cfg.get("beta", 1.0),
                    gamma=cfg.get("gamma", 3.0), delta=cfg.get("delta", 0.5),
                    time_limit_s=cfg.get("time_limit", 60),
                )
                st.session_state.result = result
                st.session_state.result_items = items
                st.success("✅ Otimização concluída! Veja os resultados na aba **Resultado**.")
            except Exception as e:
                st.error(f"Erro na otimização: {e}")

# ════════════════════════════════════════════════════════════════════════════════
# TAB 4 — RESULTADO
# ════════════════════════════════════════════════════════════════════════════════
with tab_resultado:
    result = st.session_state.get("result")
    items_res = st.session_state.get("result_items", [])

    if result is None:
        st.info("Rode a otimização para ver os resultados aqui.")
    else:
        order_deadlines = {i.order_id: i.deadline_h for i in items_res}

        # ── Métricas principais ───────────────────────────────────────────────
        m1, m2, m3, m4, m5 = st.columns(5)
        with m1:
            st.markdown(f"""
            <div class="metric-box">
                <div class="metric-label">Puxadas planejadas</div>
                <div class="metric-value">{len(result.pulls)}</div>
                <div class="metric-sub">sequências de corte</div>
            </div>""", unsafe_allow_html=True)
        with m2:
            st.markdown(f"""
            <div class="metric-box">
                <div class="metric-label">Desperdício de largura</div>
                <div class="metric-value">{result.total_waste_mm:,.0f}</div>
                <div class="metric-sub">mm total</div>
            </div>""", unsafe_allow_html=True)
        with m3:
            st.markdown(f"""
            <div class="metric-box">
                <div class="metric-label">Tempo total de setup</div>
                <div class="metric-value">{result.total_setup_min:.0f}</div>
                <div class="metric-sub">minutos</div>
            </div>""", unsafe_allow_html=True)
        with m4:
            delay_color = "#c55a11" if result.total_delay_h > 0 else "#375623"
            st.markdown(f"""
            <div class="metric-box">
                <div class="metric-label">Atraso acumulado</div>
                <div class="metric-value" style="color:{delay_color}">{result.total_delay_h:.1f}h</div>
                <div class="metric-sub">{"⚠️ há atrasos" if result.total_delay_h > 0 else "✅ sem atrasos"}</div>
            </div>""", unsafe_allow_html=True)
        with m5:
            st.markdown(f"""
            <div class="metric-box">
                <div class="metric-label">Superprodução</div>
                <div class="metric-value">{result.total_overproduction}</div>
                <div class="metric-sub">bobinas excedentes</div>
            </div>""", unsafe_allow_html=True)

        st.markdown(f"<br>**Status do solver:** `{result.solver_status}`", unsafe_allow_html=True)
        st.divider()

        # ── Sequência por máquina ─────────────────────────────────────────────
        st.markdown('<div class="section-title">Sequência de Puxadas por Máquina</div>', unsafe_allow_html=True)

        machine_ids = ["G1", "G2", "P1", "P2"]
        for mid in machine_ids:
            machine_pulls = sorted(
                [p for p in result.pulls if p.machine.machine_id == mid],
                key=lambda p: p.position,
            )
            if not machine_pulls:
                continue

            with st.expander(f"🔧 Máquina {mid} — {len(machine_pulls)} puxada(s)", expanded=True):
                rows = []
                for pull in machine_pulls:
                    parts = " + ".join(
                        f"{q}×{pull.pattern.widths[iid]}mm"
                        for iid, q in pull.pattern.items.items()
                    )
                    rows.append({
                        "Puxada": pull.pull_id,
                        "Padrão de Corte": parts,
                        "Material": pull.pattern.material.value,
                        "Total Bobinas": pull.pattern.total_rolls,
                        "Sobra Largura (mm)": pull.pattern.waste_mm,
                        "Bobina-Mãe": pull.bobina.bobina_id,
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        st.divider()

        # ── Status dos pedidos ────────────────────────────────────────────────
        st.markdown('<div class="section-title">Status de Conclusão dos Pedidos</div>', unsafe_allow_html=True)

        pedido_rows = []
        for oid in sorted(set(i.order_id for i in items_res)):
            completion = result.order_completion.get(oid)
            deadline   = order_deadlines.get(oid, 72)
            if completion is None:
                status = "⏳ Pendente"
                atraso = "—"
            elif completion <= deadline:
                status = "✅ No prazo"
                atraso = "—"
            else:
                status = "❌ Atrasado"
                atraso = f"+{completion - deadline:.1f}h"
            pedido_rows.append({
                "Pedido": oid,
                "Conclusão prevista (h)": f"{completion:.1f}" if completion else "—",
                "Prazo (h)": deadline,
                "Status": status,
                "Atraso": atraso,
            })

        st.dataframe(pd.DataFrame(pedido_rows), use_container_width=True, hide_index=True)
        st.divider()

        # ── Export ────────────────────────────────────────────────────────────
        st.markdown("**📥 Exportar resultado**")
        all_rows = []
        for pull in result.pulls:
            for iid, qty in pull.pattern.items.items():
                order_id = next((i.order_id for i in items_res if i.item_id == iid), "?")
                all_rows.append({
                    "Puxada": pull.pull_id,
                    "Máquina": pull.machine.machine_id,
                    "Posição": pull.position + 1,
                    "Pedido": order_id,
                    "Item": iid,
                    "Largura (mm)": pull.pattern.widths.get(iid, "?"),
                    "Qtd Produzida": qty,
                    "Material": pull.pattern.material.value,
                    "Sobra (mm)": pull.pattern.waste_mm,
                    "Bobina-Mãe": pull.bobina.bobina_id,
                })
        df_export = pd.DataFrame(all_rows)

        import io
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            df_export.to_excel(writer, index=False, sheet_name="Sequenciamento")
            pd.DataFrame(pedido_rows).to_excel(writer, index=False, sheet_name="Status Pedidos")
        buffer.seek(0)

        st.download_button(
            label="📥 Baixar Excel com sequenciamento",
            data=buffer,
            file_name="flexotimiza_resultado.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
