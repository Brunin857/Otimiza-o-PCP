import io
import streamlit as st
import pandas as pd
from models import (
    OrderItem, BobinaStock, Machine, SetupParams, GlobalParams,
    MaintenanceWindow, MaterialType, BobinaSize, MachineSize,
)
from optimizer import optimize

st.set_page_config(page_title="FlexOtimiza — Planejamento de Corte", page_icon="🎯", layout="wide")

st.markdown("""
<style>
    .metric-box { background:#f0f4ff; border-radius:10px; padding:16px 20px;
                  text-align:center; border-left:5px solid #2E75B6; }
    .metric-label { font-size:13px; color:#555; margin-bottom:4px; }
    .metric-value { font-size:26px; font-weight:bold; color:#1F4E79; }
    .metric-sub   { font-size:12px; color:#888; margin-top:2px; }
    .section-title { font-size:18px; font-weight:700; color:#1F4E79; margin-bottom:8px; }
    .warn-box { background:#fff8e1; border-left:5px solid #f9a825; padding:12px 16px; border-radius:8px; margin:8px 0; }
    .err-box  { background:#fce4d6; border-left:5px solid #c55a11; padding:12px 16px; border-radius:8px; margin:8px 0; }
</style>
""", unsafe_allow_html=True)

MATERIALS = [m.value for m in MaterialType]
MAT_MAP   = {m.value: m for m in MaterialType}
SIZE_MAP  = {"Grande (~2000mm)": BobinaSize.GRANDE, "Pequena (~1000mm)": BobinaSize.PEQUENA}

# ── Estado da sessão ──────────────────────────────────────────────────────────
for key, default in [
    ("items_df", pd.DataFrame(columns=["Pedido","Item","Largura (mm)","Quantidade","Material","Prazo (h)","Produzido"])),
    ("stock_df", pd.DataFrame(columns=["Bobina","Tamanho","Material","Quantidade"])),
    ("maint_df", pd.DataFrame(columns=["Máquina","Início (h)","Duração (h)"])),
    ("result", None), ("stock_decision_pending", False),
    ("items_sem_estoque", []), ("pedidos_incompletos", []),
    ("recomendacoes", []), ("blocked_orders", []), ("result_items", []),
]:
    if key not in st.session_state:
        st.session_state[key] = default


# ── Helpers ───────────────────────────────────────────────────────────────────
def build_objects(items_df, stock_df, maint_df, cfg, exclude_orders=None):
    exclude_orders = exclude_orders or []
    params = GlobalParams(
        bobina_length_m=cfg.get("bobina_len", 2000), speed_mpm=cfg.get("speed", 225),
        large_width_mm=cfg.get("large_width", 2000), small_width_mm=cfg.get("small_width", 1000),
    )
    setup = SetupParams(fixed_time_min=cfg.get("fixed_time", 15),
                        time_per_knife_min=cfg.get("knife_time", 2.5))
    machines = [Machine("G1", MachineSize.GRANDE, params), Machine("G2", MachineSize.GRANDE, params),
                Machine("P1", MachineSize.PEQUENA, params), Machine("P2", MachineSize.PEQUENA, params)]
    items = []
    for _, row in items_df.iterrows():
        if str(row["Pedido"]) in exclude_orders:
            continue
        mat = MAT_MAP.get(row["Material"])
        if mat:
            items.append(OrderItem(
                item_id=str(row["Item"]), order_id=str(row["Pedido"]),
                width_mm=int(row["Largura (mm)"]), quantity=int(row["Quantidade"]),
                material=mat, deadline_h=float(row["Prazo (h)"]),
                produced=int(row.get("Produzido", 0)),
            ))
    stock = []
    for _, row in stock_df.iterrows():
        mat = MAT_MAP.get(row["Material"])
        sz  = SIZE_MAP.get(row["Tamanho"], BobinaSize.GRANDE)
        if mat:
            stock.append(BobinaStock(bobina_id=str(row["Bobina"]), size=sz,
                                      material=mat, quantity=int(row["Quantidade"])))
    maintenance = [MaintenanceWindow(machine_id=str(r["Máquina"]),
                                      start_h=float(r["Início (h)"]),
                                      duration_h=float(r["Duração (h)"]))
                   for _, r in maint_df.iterrows()]
    return items, stock, machines, setup, params, maintenance


def check_stock_coverage(items_df, stock_df):
    stock_qty = {}
    for _, row in stock_df.iterrows():
        for sz in ["Grande (~2000mm)", "Pequena (~1000mm)"]:
            if row["Tamanho"] == sz:
                key = (row["Material"], sz)
                stock_qty[key] = stock_qty.get(key, 0) + int(row["Quantidade"])
    itens_sem, pedidos_inc, recomendacoes = [], set(), {}
    for _, row in items_df.iterrows():
        mat = row["Material"]
        tem = any(stock_qty.get((mat, sz), 0) > 0
                  for sz in ["Grande (~2000mm)", "Pequena (~1000mm)"])
        if not tem:
            itens_sem.append({"Pedido": row["Pedido"], "Item": row["Item"],
                               "Largura (mm)": row["Largura (mm)"],
                               "Quantidade": row["Quantidade"], "Material": mat,
                               "Motivo": "Sem bobina-mãe deste material no estoque"})
            pedidos_inc.add(str(row["Pedido"]))
            recomendacoes.setdefault(mat, {"Material": mat, "Qtd sugerida": 0})
            recomendacoes[mat]["Qtd sugerida"] += 1
    return itens_sem, list(pedidos_inc), list(recomendacoes.values())


def export_session():
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        st.session_state.items_df.to_excel(w, index=False, sheet_name="Pedidos")
        st.session_state.stock_df.to_excel(w, index=False, sheet_name="Estoque")
        st.session_state.maint_df.to_excel(w, index=False, sheet_name="Manutencao")
    buf.seek(0)
    return buf


def run_optimizer(cfg, exclude_orders=None):
    items, stock, machines, setup, params, maintenance = build_objects(
        st.session_state.items_df, st.session_state.stock_df,
        st.session_state.maint_df, cfg, exclude_orders=exclude_orders)
    if not items:
        st.error("Nenhum pedido pode ser produzido com o estoque atual.")
        return
    with st.spinner("Calculando sequenciamento ótimo..."):
        try:
            result = optimize(
                items=items, stock=stock, machines=machines,
                setup=setup, params=params, maintenance=maintenance,
                alpha=cfg.get("alpha",1.0), beta=cfg.get("beta",1.0),
                gamma=cfg.get("gamma",3.0), delta=cfg.get("delta",0.5),
                time_limit_s=cfg.get("time_limit",60),
            )
            st.session_state.result = result
            st.session_state.result_items = items
            st.session_state.blocked_orders = exclude_orders or []
            st.session_state.stock_decision_pending = False
            st.success("✅ Otimização concluída! Veja a aba **Resultado**.")
        except Exception as e:
            st.error(f"Erro: {e}")


# ── Header ────────────────────────────────────────────────────────────────────
st.markdown("## 🎯 FlexOtimiza — Planejamento de Corte de Bobinas")
st.caption("Otimizador de sequenciamento e agrupamento para indústria flexográfica")

# ── Salvar / Carregar sessão ──────────────────────────────────────────────────
with st.expander("💾 Salvar / Carregar sessão", expanded=False):
    col_sv, col_ld = st.columns(2)
    with col_sv:
        st.markdown("**Salvar sessão**")
        st.caption("Baixe para não perder dados ao fechar o navegador.")
        st.download_button("📥 Baixar sessão (.xlsx)", data=export_session(),
                           file_name="flexotimiza_sessao.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)
    with col_ld:
        st.markdown("**Carregar sessão salva**")
        up_sess = st.file_uploader("Arquivo de sessão (.xlsx)", type=["xlsx"], key="sess_up")
        if up_sess:
            try:
                xls = pd.ExcelFile(up_sess)
                if "Pedidos" in xls.sheet_names:
                    st.session_state.items_df = pd.read_excel(xls, "Pedidos")
                if "Estoque" in xls.sheet_names:
                    st.session_state.stock_df = pd.read_excel(xls, "Estoque")
                if "Manutencao" in xls.sheet_names:
                    st.session_state.maint_df = pd.read_excel(xls, "Manutencao")
                st.success("Sessão carregada!")
                st.rerun()
            except Exception as e:
                st.error(f"Erro: {e}")

st.divider()

# ── Abas ──────────────────────────────────────────────────────────────────────
tab_p, tab_s, tab_c, tab_r = st.tabs(["📋 Pedidos","📦 Estoque de Bobinas-Mãe","⚙️ Configurações","📊 Resultado"])

# ── TAB PEDIDOS ───────────────────────────────────────────────────────────────
with tab_p:
    st.markdown('<div class="section-title">Fila de Pedidos</div>', unsafe_allow_html=True)
    st.caption("Cada linha é um item de um pedido. Um pedido pode ter múltiplos itens com larguras e materiais diferentes.")
    col_f, col_t = st.columns([1, 2])
    with col_f:
        with st.form("fp", clear_on_submit=True):
            pid  = st.text_input("Nº do Pedido", placeholder="P001")
            iid  = st.text_input("ID do Item", placeholder="P001-A", help="Deve ser único. Ex: P001-A, P001-B")
            larg = st.number_input("Largura (mm)", min_value=1, max_value=2000, value=100)
            qtd  = st.number_input("Quantidade", min_value=1, max_value=200, value=1)
            mat  = st.selectbox("Material", MATERIALS)
            prz  = st.number_input("Prazo (h)", min_value=1, max_value=240, value=72)
            prod = st.number_input("Já produzido", min_value=0, value=0)
            if st.form_submit_button("➕ Adicionar", use_container_width=True):
                if not pid or not iid:
                    st.warning("Preencha pedido e ID do item.")
                elif iid in st.session_state.items_df["Item"].values:
                    st.warning("ID já existe.")
                else:
                    st.session_state.items_df = pd.concat([st.session_state.items_df,
                        pd.DataFrame([{"Pedido":pid,"Item":iid,"Largura (mm)":larg,
                                       "Quantidade":qtd,"Material":mat,"Prazo (h)":prz,"Produzido":prod}])
                    ], ignore_index=True)
                    st.success(f"Item {iid} adicionado.")
        st.markdown("---")
        st.markdown("**Importar do Excel**")
        up_items = st.file_uploader("Planilha de pedidos", type=["xlsx","csv"], key="items_up")
        if up_items:
            try:
                df_i = pd.read_csv(up_items) if up_items.name.endswith(".csv") else pd.read_excel(up_items)
                req = {"Pedido","Item","Largura (mm)","Quantidade","Material","Prazo (h)"}
                if req.issubset(df_i.columns):
                    if "Produzido" not in df_i.columns: df_i["Produzido"] = 0
                    st.session_state.items_df = df_i[list(req|{"Produzido"})].copy()
                    st.success(f"{len(df_i)} itens importados.")
                else:
                    st.error(f"Colunas necessárias: {req}")
            except Exception as e:
                st.error(f"Erro: {e}")
    with col_t:
        if st.session_state.items_df.empty:
            st.info("Nenhum item adicionado ainda.")
        else:
            st.session_state.items_df = st.data_editor(
                st.session_state.items_df, use_container_width=True,
                num_rows="dynamic", key="items_ed")
            st.markdown("**Resumo por pedido**")
            st.dataframe(
                st.session_state.items_df.groupby("Pedido")
                .agg(Itens=("Item","count"), Prazo=("Prazo (h)","min"))
                .reset_index(), use_container_width=True, hide_index=True)

# ── TAB ESTOQUE ───────────────────────────────────────────────────────────────
with tab_s:
    st.markdown('<div class="section-title">Estoque de Bobinas-Mãe</div>', unsafe_allow_html=True)
    col_s1, col_s2 = st.columns([1, 2])
    with col_s1:
        with st.form("fs", clear_on_submit=True):
            bid  = st.text_input("ID da Bobina", placeholder="BM-001")
            tam  = st.selectbox("Tamanho", ["Grande (~2000mm)","Pequena (~1000mm)"])
            matb = st.selectbox("Material", MATERIALS)
            qtdb = st.number_input("Quantidade", min_value=1, max_value=100, value=1)
            if st.form_submit_button("➕ Adicionar", use_container_width=True):
                if not bid: st.warning("Informe o ID.")
                elif bid in st.session_state.stock_df["Bobina"].values: st.warning("ID já existe.")
                else:
                    st.session_state.stock_df = pd.concat([st.session_state.stock_df,
                        pd.DataFrame([{"Bobina":bid,"Tamanho":tam,"Material":matb,"Quantidade":qtdb}])
                    ], ignore_index=True)
                    st.success("Bobina adicionada.")
    with col_s2:
        if st.session_state.stock_df.empty: st.info("Nenhuma bobina registrada.")
        else:
            st.session_state.stock_df = st.data_editor(
                st.session_state.stock_df, use_container_width=True,
                num_rows="dynamic", key="stock_ed")

# ── TAB CONFIGURAÇÕES ─────────────────────────────────────────────────────────
with tab_c:
    st.markdown('<div class="section-title">Configurações</div>', unsafe_allow_html=True)
    cc1, cc2, cc3 = st.columns(3)
    with cc1:
        st.markdown("**⏱️ Setup**")
        fixed_time  = st.number_input("Tempo fixo (min)", min_value=1.0, value=15.0, step=0.5)
        knife_time  = st.number_input("Tempo/faca alterada (min)", min_value=0.1, value=2.5, step=0.1)
        st.markdown("**🏭 Máquinas**")
        bobina_len  = st.number_input("Comprimento bobina-mãe (m)", min_value=100.0, value=2000.0, step=100.0)
        speed       = st.number_input("Velocidade (m/min)", min_value=100.0, value=225.0, step=5.0)
        large_width = st.number_input("Largura bobina grande (mm)", min_value=500, value=2000, step=50)
        small_width = st.number_input("Largura bobina pequena (mm)", min_value=200, value=1000, step=50)
    with cc2:
        st.markdown("**⚖️ Pesos da Função Objetivo**")
        alpha = st.slider("α — Desperdício", 0.0, 5.0, 1.0, 0.5)
        beta  = st.slider("β — Setup", 0.0, 5.0, 1.0, 0.5)
        gamma = st.slider("γ — Atraso", 0.0, 5.0, 3.0, 0.5)
        delta = st.slider("δ — Superprodução", 0.0, 5.0, 0.5, 0.5)
        time_limit = st.number_input("Tempo máx. solver (s)", min_value=10, max_value=300, value=60)
    with cc3:
        st.markdown("**🔧 Manutenção**")
        with st.form("fm", clear_on_submit=True):
            mid_m = st.selectbox("Máquina", ["G1","G2","P1","P2"])
            sh    = st.number_input("Início (h)", min_value=0.0, value=2.0, step=0.5)
            dh    = st.number_input("Duração (h)", min_value=0.5, value=1.0, step=0.5)
            if st.form_submit_button("➕ Adicionar", use_container_width=True):
                st.session_state.maint_df = pd.concat([st.session_state.maint_df,
                    pd.DataFrame([{"Máquina":mid_m,"Início (h)":sh,"Duração (h)":dh}])
                ], ignore_index=True)
        if not st.session_state.maint_df.empty:
            st.session_state.maint_df = st.data_editor(
                st.session_state.maint_df, use_container_width=True,
                num_rows="dynamic", key="maint_ed")
        else:
            st.info("Sem janelas de manutenção.")

    st.session_state["cfg"] = dict(
        fixed_time=fixed_time, knife_time=knife_time, bobina_len=bobina_len,
        speed=speed, large_width=large_width, small_width=small_width,
        alpha=alpha, beta=beta, gamma=gamma, delta=delta, time_limit=time_limit,
    )

# ── BOTÃO OTIMIZAR ────────────────────────────────────────────────────────────
st.divider()
_, cb1, cb2 = st.columns([2, 1, 1])
with cb1:
    run_btn = st.button("🚀 Otimizar Agora", use_container_width=True, type="primary")
with cb2:
    if st.button("🗑️ Limpar Tudo", use_container_width=True):
        for k in ["items_df","stock_df","maint_df","result","stock_decision_pending",
                  "items_sem_estoque","pedidos_incompletos","recomendacoes","blocked_orders","result_items"]:
            if k in st.session_state: del st.session_state[k]
        st.rerun()

if run_btn:
    if st.session_state.items_df.empty or st.session_state.stock_df.empty:
        st.error("Preencha pedidos e estoque antes de otimizar.")
    else:
        itens_sem, pedidos_inc, recomendacoes = check_stock_coverage(
            st.session_state.items_df, st.session_state.stock_df)
        if itens_sem:
            st.session_state.items_sem_estoque   = itens_sem
            st.session_state.pedidos_incompletos = pedidos_inc
            st.session_state.recomendacoes       = recomendacoes
            st.session_state.stock_decision_pending = True
            st.rerun()
        else:
            st.session_state.items_sem_estoque = []
            st.session_state.recomendacoes = []
            run_optimizer(st.session_state.get("cfg", {}))

# ── POPUP DE DECISÃO DE ESTOQUE ───────────────────────────────────────────────
if st.session_state.get("stock_decision_pending"):
    itens_sem    = st.session_state.items_sem_estoque
    pedidos_inc  = st.session_state.pedidos_incompletos
    recomendacoes = st.session_state.recomendacoes
    cfg = st.session_state.get("cfg", {})

    st.markdown("---")
    st.markdown("""<div class="err-box">
    ⚠️ <strong>Estoque insuficiente detectado</strong><br>
    Os itens abaixo não têm bobina-mãe disponível no material correspondente.
    </div>""", unsafe_allow_html=True)
    st.dataframe(pd.DataFrame(itens_sem), use_container_width=True, hide_index=True)
    st.markdown(f"**Pedidos afetados:** `{', '.join(pedidos_inc)}`")
    st.markdown("**Como deseja prosseguir?**")

    cd1, cd2 = st.columns(2)
    with cd1:
        if st.button("🚫 Bloquear pedidos incompletos\n(otimizar só os com estoque completo)",
                     use_container_width=True, key="btn_block"):
            run_optimizer(cfg, exclude_orders=pedidos_inc)
    with cd2:
        if st.button("⚡ Otimizar tudo\n(listar itens sem estoque no resultado)",
                     use_container_width=True, key="btn_all"):
            run_optimizer(cfg)

    if recomendacoes:
        st.markdown("---")
        st.markdown("**🛒 Recomendação de compra para atender todos os pedidos:**")
        st.dataframe(pd.DataFrame(recomendacoes), use_container_width=True, hide_index=True)
        st.caption("Qtd sugerida = número mínimo de bobinas-mãe necessárias para cobrir os itens sem estoque.")

# ── TAB RESULTADO ─────────────────────────────────────────────────────────────
with tab_r:
    result    = st.session_state.get("result")
    items_res = st.session_state.get("result_items", [])
    itens_sem = st.session_state.get("items_sem_estoque", [])
    blocked   = st.session_state.get("blocked_orders", [])
    recomendacoes = st.session_state.get("recomendacoes", [])

    if result is None:
        st.info("Rode a otimização para ver os resultados aqui.")
    else:
        order_deadlines = {i.order_id: i.deadline_h for i in items_res}

        if blocked:
            st.markdown(f"""<div class="warn-box">
            🚫 <strong>Pedidos bloqueados por falta de estoque:</strong> {', '.join(blocked)}
            </div>""", unsafe_allow_html=True)
        if itens_sem and not blocked:
            st.markdown("""<div class="warn-box">
            ⚠️ <strong>Itens sem estoque</strong> — foram otimizados os demais.
            </div>""", unsafe_allow_html=True)
            st.dataframe(pd.DataFrame(itens_sem), use_container_width=True, hide_index=True)
        if recomendacoes:
            st.markdown("**🛒 Recomendação de compra:**")
            st.dataframe(pd.DataFrame(recomendacoes), use_container_width=True, hide_index=True)
            st.divider()

        m1,m2,m3,m4,m5 = st.columns(5)
        for col, lbl, val, sub in [
            (m1,"Puxadas",f"{len(result.pulls)}","sequências"),
            (m2,"Desperdício",f"{result.total_waste_mm:,.0f} mm","largura não usada"),
            (m3,"Setup total",f"{result.total_setup_min:.0f} min","troca de facas"),
            (m4,"Atraso",f"{result.total_delay_h:.1f}h","⚠️ há atrasos" if result.total_delay_h>0 else "✅ sem atrasos"),
            (m5,"Superprodução",f"{result.total_overproduction}","bobinas excedentes"),
        ]:
            with col:
                st.markdown(f"""<div class="metric-box">
                <div class="metric-label">{lbl}</div>
                <div class="metric-value">{val}</div>
                <div class="metric-sub">{sub}</div></div>""", unsafe_allow_html=True)

        st.markdown(f"<br>**Solver:** `{result.solver_status}`", unsafe_allow_html=True)
        st.divider()

        st.markdown('<div class="section-title">Sequência por Máquina</div>', unsafe_allow_html=True)
        for mid in ["G1","G2","P1","P2"]:
            pulls = sorted([p for p in result.pulls if p.machine.machine_id==mid], key=lambda p:p.position)
            if not pulls: continue
            with st.expander(f"🔧 Máquina {mid} — {len(pulls)} puxada(s)", expanded=True):
                st.dataframe(pd.DataFrame([{
                    "Puxada": p.pull_id,
                    "Padrão": " + ".join(f"{q}×{p.pattern.widths[i]}mm" for i,q in p.pattern.items.items()),
                    "Material": p.pattern.material.value,
                    "Bobinas": p.pattern.total_rolls,
                    "Sobra (mm)": p.pattern.waste_mm,
                    "Bobina-Mãe": p.bobina.bobina_id,
                } for p in pulls]), use_container_width=True, hide_index=True)

        st.divider()
        st.markdown('<div class="section-title">Status dos Pedidos</div>', unsafe_allow_html=True)
        pedido_rows = []
        for oid in sorted(set(i.order_id for i in items_res)|set(blocked)):
            if oid in blocked:
                pedido_rows.append({"Pedido":oid,"Conclusão":"—","Prazo":"—","Status":"🚫 Bloqueado","Atraso":"—"})
            else:
                c = result.order_completion.get(oid)
                d = order_deadlines.get(oid, 72)
                pedido_rows.append({
                    "Pedido": oid,
                    "Conclusão": f"{c:.1f}h" if c else "—",
                    "Prazo": f"{d}h",
                    "Status": "✅ No prazo" if c and c<=d else ("❌ Atrasado" if c else "⏳ Pendente"),
                    "Atraso": f"+{c-d:.1f}h" if c and c>d else "—",
                })
        st.dataframe(pd.DataFrame(pedido_rows), use_container_width=True, hide_index=True)
        st.divider()

        # Exportar resultado
        all_rows = []
        for pull in result.pulls:
            for iid, qty in pull.pattern.items.items():
                oid = next((i.order_id for i in items_res if i.item_id==iid), "?")
                all_rows.append({
                    "Puxada":pull.pull_id, "Máquina":pull.machine.machine_id,
                    "Posição":pull.position+1, "Pedido":oid, "Item":iid,
                    "Largura (mm)":pull.pattern.widths.get(iid,"?"),
                    "Qtd":qty, "Material":pull.pattern.material.value,
                    "Sobra (mm)":pull.pattern.waste_mm, "Bobina-Mãe":pull.bobina.bobina_id,
                })
        buf_r = io.BytesIO()
        with pd.ExcelWriter(buf_r, engine="openpyxl") as w:
            pd.DataFrame(all_rows).to_excel(w, index=False, sheet_name="Sequenciamento")
            pd.DataFrame(pedido_rows).to_excel(w, index=False, sheet_name="Status Pedidos")
            if itens_sem: pd.DataFrame(itens_sem).to_excel(w, index=False, sheet_name="Sem Estoque")
            if recomendacoes: pd.DataFrame(recomendacoes).to_excel(w, index=False, sheet_name="Compras Sugeridas")
        buf_r.seek(0)
        st.download_button("📥 Baixar resultado completo (.xlsx)", data=buf_r,
                           file_name="flexotimiza_resultado.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
