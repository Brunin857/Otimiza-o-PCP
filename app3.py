import io
import csv
import json
from datetime import datetime, time
import streamlit as st
import pandas as pd
from models import (
    OrderItem, BobinaStock, Machine, SetupParams, GlobalParams,
    MaintenanceWindow, MaterialType, BobinaSize, MachineSize, OPRecord,
)
from optimizer import optimize

st.set_page_config(page_title="FlexOtimiza", page_icon="🎯", layout="wide")
st.markdown("""
<style>
    .metric-box{background:#f0f4ff;border-radius:10px;padding:14px 18px;
                text-align:center;border-left:5px solid #2E75B6;}
    .metric-label{font-size:12px;color:#555;margin-bottom:4px;}
    .metric-value{font-size:24px;font-weight:bold;color:#1F4E79;}
    .metric-sub{font-size:11px;color:#888;margin-top:2px;}
    .section-title{font-size:17px;font-weight:700;color:#1F4E79;margin-bottom:6px;}
    .warn-box{background:#fff8e1;border-left:5px solid #f9a825;
              padding:10px 14px;border-radius:8px;margin:6px 0;}
    .err-box{background:#fce4d6;border-left:5px solid #c55a11;
             padding:10px 14px;border-radius:8px;margin:6px 0;}
    .ok-box{background:#e2f0d9;border-left:5px solid #375623;
            padding:10px 14px;border-radius:8px;margin:6px 0;}
    .shift-bar{background:#e8f4fd;border-radius:8px;padding:10px 16px;
               border:1px solid #2E75B6;margin-bottom:12px;}
</style>""", unsafe_allow_html=True)

MATERIALS = [m.value for m in MaterialType]
MAT_MAP   = {m.value: m for m in MaterialType}
SIZE_MAP  = {"Grande (~2000mm)": BobinaSize.GRANDE, "Pequena (~1000mm)": BobinaSize.PEQUENA}
MACHINES  = ["G1","G2","P1","P2"]

# ── Estado inicial ────────────────────────────────────────────────────────────
DEFAULTS = {
    "items_df":   pd.DataFrame(columns=["Pedido","Item","Largura (mm)","Quantidade","Material","Prazo (h)","Produzido"]),
    "stock_df":   pd.DataFrame(columns=["Bobina","Tamanho","Material","Quantidade"]),
    "maint_df":   pd.DataFrame(columns=["Máquina","Início (h)","Duração (h)"]),
    "machine_busy": {"G1":0.0,"G2":0.0,"P1":0.0,"P2":0.0},  # hora em que cada máquina fica livre
    "op_history": [],          # lista de dicts para CSV
    "op_counter": 1,
    "result": None,
    "result_items": [],
    "items_sem_estoque": [],
    "pedidos_incompletos": [],
    "recomendacoes": [],
    "blocked_orders": [],
    "stock_decision_pending": False,
    "cfg": {},
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


# ── Helpers ───────────────────────────────────────────────────────────────────
def now_decimal() -> float:
    """Hora atual como decimal (ex: 14h30 = 14.5)."""
    t = datetime.now().time()
    return t.hour + t.minute / 60.0 + t.second / 3600.0


def fmt_h(h: float) -> str:
    """Converte hora decimal para string HH:MM."""
    hh = int(h)
    mm = int(round((h - hh) * 60))
    if mm == 60:
        hh += 1; mm = 0
    return f"{hh:02d}:{mm:02d}"


def build_objects(items_df, stock_df, maint_df, cfg, exclude_orders=None):
    exclude_orders = exclude_orders or []
    params = GlobalParams(
        bobina_length_m=cfg.get("bobina_len", 2000),
        speed_mpm=cfg.get("speed", 225),
        large_width_mm=cfg.get("large_width", 2000),
        small_width_mm=cfg.get("small_width", 1000),
        shift_start_h=cfg.get("shift_start", 8.0),
        shift_end_h=cfg.get("shift_end", 17.0),
    )
    setup = SetupParams(
        fixed_time_min=cfg.get("fixed_time", 15),
        time_per_knife_min=cfg.get("knife_time", 2.5),
    )
    busy = st.session_state.machine_busy
    machines = [
        Machine("G1", MachineSize.GRANDE,  params, busy_until_h=busy.get("G1",0.0)),
        Machine("G2", MachineSize.GRANDE,  params, busy_until_h=busy.get("G2",0.0)),
        Machine("P1", MachineSize.PEQUENA, params, busy_until_h=busy.get("P1",0.0)),
        Machine("P2", MachineSize.PEQUENA, params, busy_until_h=busy.get("P2",0.0)),
    ]
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
        sz  = SIZE_MAP.get(str(row["Tamanho"]), BobinaSize.GRANDE)
        if mat:
            stock.append(BobinaStock(
                bobina_id=str(row["Bobina"]), size=sz,
                material=mat, quantity=int(row["Quantidade"]),
            ))
    maintenance = [
        MaintenanceWindow(str(r["Máquina"]), float(r["Início (h)"]), float(r["Duração (h)"]))
        for _, r in maint_df.iterrows()
    ]
    return items, stock, machines, setup, params, maintenance


def check_stock_coverage(items_df, stock_df):
    stock_qty = {}
    for _, row in stock_df.iterrows():
        key = (row["Material"], str(row["Tamanho"]))
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
                               "Motivo": "Sem bobina-mãe deste material"})
            pedidos_inc.add(str(row["Pedido"]))
            recomendacoes.setdefault(mat, {"Material": mat, "Qtd sugerida": 0})
            recomendacoes[mat]["Qtd sugerida"] += 1
    return itens_sem, list(pedidos_inc), list(recomendacoes.values())


def confirm_op(machine_id: str, result, items_res, cfg):
    """Confirma OP de uma máquina: atualiza estoque, fila e histórico."""
    pulls = sorted(
        [p for p in result.pulls if p.machine.machine_id == machine_id],
        key=lambda p: p.position,
    )
    if not pulls:
        return

    # ── 1. Atualiza estoque ───────────────────────────────────────────────────
    bobinas_usadas = {}
    for pull in pulls:
        bid = pull.bobina.bobina_id
        bobinas_usadas[bid] = bobinas_usadas.get(bid, 0) + 1

    stock_df = st.session_state.stock_df.copy()
    for _, row in stock_df.iterrows():
        bid = str(row["Bobina"])
        if bid in bobinas_usadas:
            used = bobinas_usadas[bid]
            stock_df.loc[stock_df["Bobina"] == bid, "Quantidade"] = max(0, int(row["Quantidade"]) - used)
    # remove bobinas com quantidade zero
    stock_df = stock_df[stock_df["Quantidade"] > 0].reset_index(drop=True)
    st.session_state.stock_df = stock_df

    # ── 2. Atualiza fila de pedidos (baixa itens produzidos) ──────────────────
    items_produced: Dict[str, int] = {}
    for pull in pulls:
        for iid, qty in pull.pattern.items.items():
            items_produced[iid] = items_produced.get(iid, 0) + qty

    items_df = st.session_state.items_df.copy()
    for iid, qty_prod in items_produced.items():
        mask = items_df["Item"] == iid
        if mask.any():
            current_prod = int(items_df.loc[mask, "Produzido"].values[0])
            new_prod     = current_prod + qty_prod
            total_qty    = int(items_df.loc[mask, "Quantidade"].values[0])
            items_df.loc[mask, "Produzido"] = min(new_prod, total_qty)
    # remove itens completamente produzidos
    items_df = items_df[
        items_df.apply(lambda r: int(r["Produzido"]) < int(r["Quantidade"]), axis=1)
    ].reset_index(drop=True)
    st.session_state.items_df = items_df

    # ── 3. Atualiza disponibilidade da máquina ────────────────────────────────
    end_h = max(p.end_time_h for p in pulls)
    st.session_state.machine_busy[machine_id] = end_h

    # ── 4. Registra no histórico ──────────────────────────────────────────────
    op_id    = f"OP{st.session_state.op_counter:04d}"
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    shift_end = cfg.get("shift_end", 17.0)

    for pull in pulls:
        for iid, qty in pull.pattern.items.items():
            order_id = next((i.order_id for i in items_res if i.item_id == iid), "?")
            st.session_state.op_history.append({
                "OP": op_id,
                "Máquina": machine_id,
                "Confirmada em": now_str,
                "Puxada": pull.pull_id,
                "Pedido": order_id,
                "Item": iid,
                "Largura (mm)": pull.pattern.widths.get(iid, "?"),
                "Qtd Produzida": qty,
                "Material": pull.pattern.material.value,
                "Sobra Largura (mm)": pull.pattern.waste_mm,
                "Bobina-Mãe": pull.bobina.bobina_id,
                "Início": fmt_h(pull.start_time_h),
                "Fim": fmt_h(pull.end_time_h),
                "Passa do Turno": "⚠️ Sim" if pull.end_time_h > shift_end else "✅ Não",
            })

    st.session_state.op_counter += 1
    st.session_state.result = None   # limpa resultado para forçar nova otimização


def export_session():
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        st.session_state.items_df.to_excel(w, index=False, sheet_name="Pedidos")
        st.session_state.stock_df.to_excel(w, index=False, sheet_name="Estoque")
        st.session_state.maint_df.to_excel(w, index=False, sheet_name="Manutencao")
        pd.DataFrame([{"Máquina": k, "Livre às (h)": v}
                      for k, v in st.session_state.machine_busy.items()
                      ]).to_excel(w, index=False, sheet_name="Maquinas")
        pd.DataFrame(st.session_state.op_history).to_excel(
            w, index=False, sheet_name="Historico") if st.session_state.op_history else None
        pd.DataFrame([{"op_counter": st.session_state.op_counter}]
                     ).to_excel(w, index=False, sheet_name="Meta")
    buf.seek(0)
    return buf


def import_session(uploaded):
    try:
        xls = pd.ExcelFile(uploaded)
        if "Pedidos"    in xls.sheet_names: st.session_state.items_df = pd.read_excel(xls,"Pedidos")
        if "Estoque"    in xls.sheet_names: st.session_state.stock_df = pd.read_excel(xls,"Estoque")
        if "Manutencao" in xls.sheet_names: st.session_state.maint_df = pd.read_excel(xls,"Manutencao")
        if "Maquinas"   in xls.sheet_names:
            df_m = pd.read_excel(xls,"Maquinas")
            st.session_state.machine_busy = dict(zip(df_m["Máquina"], df_m["Livre às (h)"]))
        if "Historico"  in xls.sheet_names:
            st.session_state.op_history = pd.read_excel(xls,"Historico").to_dict("records")
        if "Meta"       in xls.sheet_names:
            st.session_state.op_counter = int(pd.read_excel(xls,"Meta")["op_counter"].iloc[0])
        return True
    except Exception as e:
        st.error(f"Erro ao importar: {e}")
        return False


def run_optimizer(cfg, exclude_orders=None):
    items, stock, machines, setup, params, maintenance = build_objects(
        st.session_state.items_df, st.session_state.stock_df,
        st.session_state.maint_df, cfg, exclude_orders=exclude_orders)
    if not items:
        st.error("Nenhum pedido pode ser produzido com o estoque atual.")
        return
    current_h = now_decimal()
    with st.spinner("Calculando sequenciamento ótimo..."):
        try:
            result = optimize(
                items=items, stock=stock, machines=machines,
                setup=setup, params=params, now_h=current_h,
                maintenance=maintenance,
                alpha=cfg.get("alpha",1.0), beta=cfg.get("beta",1.0),
                gamma=cfg.get("gamma",3.0), delta=cfg.get("delta",0.5),
                time_limit_s=cfg.get("time_limit",60),
            )
            st.session_state.result       = result
            st.session_state.result_items = items
            st.session_state.blocked_orders   = exclude_orders or []
            st.session_state.stock_decision_pending = False
            st.success("✅ Otimização concluída! Veja a aba **Resultado**.")
        except Exception as e:
            st.error(f"Erro: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# HEADER
# ═════════════════════════════════════════════════════════════════════════════
st.markdown("## 🎯 FlexOtimiza — Planejamento de Corte de Bobinas")
st.caption("Otimizador de sequenciamento e agrupamento para indústria flexográfica")

# ── Barra de turno ────────────────────────────────────────────────────────────
cfg_cur = st.session_state.get("cfg", {})
shift_start = cfg_cur.get("shift_start", 8.0)
shift_end   = cfg_cur.get("shift_end",  17.0)
now_h       = now_decimal()
remaining_min = max(0.0, (shift_end - now_h) * 60)
busy_machines = [k for k, v in st.session_state.machine_busy.items() if v > now_h]

col_sh1, col_sh2, col_sh3, col_sh4 = st.columns(4)
with col_sh1:
    st.markdown(f"""<div class="shift-bar">
    🕐 <strong>Agora:</strong> {fmt_h(now_h)}<br>
    <small>Turno: {fmt_h(shift_start)} – {fmt_h(shift_end)}</small>
    </div>""", unsafe_allow_html=True)
with col_sh2:
    color = "#c55a11" if remaining_min < 60 else "#1F4E79"
    st.markdown(f"""<div class="shift-bar">
    ⏳ <strong style="color:{color}">{remaining_min:.0f} min restantes</strong><br>
    <small>até fim do turno ({fmt_h(shift_end)})</small>
    </div>""", unsafe_allow_html=True)
with col_sh3:
    if busy_machines:
        busy_str = " | ".join(f"{m}: livre às {fmt_h(st.session_state.machine_busy[m])}"
                               for m in busy_machines)
        st.markdown(f"""<div class="warn-box">
        🔧 <strong>Máquinas ocupadas:</strong><br><small>{busy_str}</small>
        </div>""", unsafe_allow_html=True)
    else:
        st.markdown("""<div class="ok-box">✅ <strong>Todas as máquinas livres</strong></div>""",
                    unsafe_allow_html=True)
with col_sh4:
    if st.button("🔄 Atualizar horário", use_container_width=True):
        st.rerun()

# ── Salvar / Carregar ─────────────────────────────────────────────────────────
with st.expander("💾 Salvar / Carregar sessão", expanded=False):
    cs, cl = st.columns(2)
    with cs:
        st.markdown("**Salvar sessão completa**")
        st.caption("Inclui pedidos, estoque, máquinas ocupadas e histórico de OPs.")
        st.download_button("📥 Baixar sessão (.xlsx)", data=export_session(),
                           file_name="flexotimiza_sessao.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                           use_container_width=True)
    with cl:
        st.markdown("**Carregar sessão salva**")
        up = st.file_uploader("Arquivo de sessão", type=["xlsx"], key="sess_up")
        if up and import_session(up):
            st.success("Sessão carregada!")
            st.rerun()

st.divider()

# ═════════════════════════════════════════════════════════════════════════════
# ABAS
# ═════════════════════════════════════════════════════════════════════════════
tab_p, tab_s, tab_c, tab_r, tab_h = st.tabs([
    "📋 Pedidos", "📦 Estoque", "⚙️ Configurações", "📊 Resultado", "📜 Histórico de OPs"
])

# ── TAB PEDIDOS ───────────────────────────────────────────────────────────────
with tab_p:
    st.markdown('<div class="section-title">Fila de Pedidos</div>', unsafe_allow_html=True)
    st.caption("Cada linha é um item de um pedido. Um pedido pode ter múltiplos itens.")
    cf, ct = st.columns([1, 2])
    with cf:
        with st.form("fp", clear_on_submit=True):
            pid  = st.text_input("Nº do Pedido", placeholder="P001")
            iid  = st.text_input("ID do Item", placeholder="P001-A")
            larg = st.number_input("Largura (mm)", 1, 2000, 100)
            qtd  = st.number_input("Quantidade", 1, 200, 1)
            mat  = st.selectbox("Material", MATERIALS)
            prz  = st.number_input("Prazo (h)", 1, 240, 72)
            prod = st.number_input("Já produzido", 0, value=0)
            if st.form_submit_button("➕ Adicionar", use_container_width=True):
                if not pid or not iid:
                    st.warning("Preencha pedido e ID.")
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
        up_i = st.file_uploader("Planilha de pedidos", type=["xlsx","csv"], key="items_up")
        if up_i:
            try:
                df_i = pd.read_csv(up_i) if up_i.name.endswith(".csv") else pd.read_excel(up_i)
                req = {"Pedido","Item","Largura (mm)","Quantidade","Material","Prazo (h)"}
                if req.issubset(df_i.columns):
                    if "Produzido" not in df_i.columns: df_i["Produzido"] = 0
                    st.session_state.items_df = df_i[list(req|{"Produzido"})].copy()
                    st.success(f"{len(df_i)} itens importados.")
                else:
                    st.error(f"Colunas necessárias: {req}")
            except Exception as e:
                st.error(f"Erro: {e}")
    with ct:
        if st.session_state.items_df.empty:
            st.info("Nenhum item adicionado ainda.")
        else:
            st.session_state.items_df = st.data_editor(
                st.session_state.items_df, use_container_width=True,
                num_rows="dynamic", key="items_ed")
            st.markdown("**Resumo por pedido**")
            st.dataframe(
                st.session_state.items_df.groupby("Pedido")
                .agg(Itens=("Item","count"), Prazo=("Prazo (h)","min"),
                     Pendente=("Produzido", lambda x: sum(
                         max(0, int(st.session_state.items_df.loc[x.index[i],"Quantidade"]) - int(v))
                         for i, v in enumerate(x)
                     ))).reset_index(),
                use_container_width=True, hide_index=True)

# ── TAB ESTOQUE ───────────────────────────────────────────────────────────────
with tab_s:
    st.markdown('<div class="section-title">Estoque de Bobinas-Mãe</div>', unsafe_allow_html=True)
    cs1, cs2 = st.columns([1, 2])
    with cs1:
        with st.form("fs", clear_on_submit=True):
            bid  = st.text_input("ID da Bobina", placeholder="BM-001")
            tam  = st.selectbox("Tamanho", ["Grande (~2000mm)","Pequena (~1000mm)"])
            matb = st.selectbox("Material", MATERIALS)
            qtdb = st.number_input("Quantidade", 1, 100, 1)
            if st.form_submit_button("➕ Adicionar", use_container_width=True):
                if not bid: st.warning("Informe o ID.")
                elif bid in st.session_state.stock_df["Bobina"].values: st.warning("ID já existe.")
                else:
                    st.session_state.stock_df = pd.concat([st.session_state.stock_df,
                        pd.DataFrame([{"Bobina":bid,"Tamanho":tam,"Material":matb,"Quantidade":qtdb}])
                    ], ignore_index=True)
                    st.success("Bobina adicionada.")
    with cs2:
        if st.session_state.stock_df.empty:
            st.info("Nenhuma bobina registrada.")
        else:
            st.session_state.stock_df = st.data_editor(
                st.session_state.stock_df, use_container_width=True,
                num_rows="dynamic", key="stock_ed")

# ── TAB CONFIGURAÇÕES ─────────────────────────────────────────────────────────
with tab_c:
    st.markdown('<div class="section-title">Configurações</div>', unsafe_allow_html=True)
    cc1, cc2, cc3 = st.columns(3)
    with cc1:
        st.markdown("**🕐 Turno**")
        shift_start_h = st.number_input("Início do turno (h)", 0.0, 23.0, 8.0, 0.5)
        shift_end_h   = st.number_input("Fim do turno (h)", 0.0, 23.0, 17.0, 0.5)
        st.markdown("**⏱️ Setup**")
        fixed_time  = st.number_input("Tempo fixo (min)", 1.0, 120.0, 15.0, 0.5)
        knife_time  = st.number_input("Tempo/faca alterada (min)", 0.1, 30.0, 2.5, 0.1)
        st.markdown("**🏭 Máquinas**")
        bobina_len  = st.number_input("Comprimento bobina-mãe (m)", 100.0, 10000.0, 2000.0, 100.0)
        speed       = st.number_input("Velocidade (m/min)", 100.0, 500.0, 225.0, 5.0)
        large_width = st.number_input("Largura bobina grande (mm)", 500, 5000, 2000, 50)
        small_width = st.number_input("Largura bobina pequena (mm)", 200, 2000, 1000, 50)
    with cc2:
        st.markdown("**⚖️ Pesos da Função Objetivo**")
        alpha = st.slider("α — Desperdício", 0.0, 5.0, 1.0, 0.5)
        beta  = st.slider("β — Setup", 0.0, 5.0, 1.0, 0.5)
        gamma = st.slider("γ — Atraso", 0.0, 5.0, 3.0, 0.5)
        delta = st.slider("δ — Superprodução", 0.0, 5.0, 0.5, 0.5)
        time_limit = st.number_input("Tempo máx. solver (s)", 10, 300, 60)

        st.markdown("**🔧 Disponibilidade das Máquinas**")
        st.caption("Ajuste manualmente se necessário.")
        for mid in MACHINES:
            val = st.session_state.machine_busy.get(mid, 0.0)
            new_val = st.number_input(f"{mid} — livre às (h)", 0.0, 24.0, float(val), 0.25, key=f"busy_{mid}")
            st.session_state.machine_busy[mid] = new_val

    with cc3:
        st.markdown("**🔧 Manutenção Preventiva**")
        with st.form("fm", clear_on_submit=True):
            mid_m = st.selectbox("Máquina", MACHINES)
            sh    = st.number_input("Início (h)", 0.0, 24.0, 2.0, 0.5)
            dh    = st.number_input("Duração (h)", 0.5, 8.0, 1.0, 0.5)
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
        shift_start=shift_start_h, shift_end=shift_end_h,
        fixed_time=fixed_time, knife_time=knife_time,
        bobina_len=bobina_len, speed=speed,
        large_width=large_width, small_width=small_width,
        alpha=alpha, beta=beta, gamma=gamma, delta=delta,
        time_limit=time_limit,
    )

# ── BOTÃO OTIMIZAR ────────────────────────────────────────────────────────────
st.divider()
_, cb1, cb2 = st.columns([2,1,1])
with cb1:
    run_btn = st.button("🚀 Otimizar Agora", use_container_width=True, type="primary")
with cb2:
    if st.button("🗑️ Limpar Tudo", use_container_width=True):
        for k in list(DEFAULTS.keys()):
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

# ── POPUP ESTOQUE INSUFICIENTE ────────────────────────────────────────────────
if st.session_state.get("stock_decision_pending"):
    itens_sem    = st.session_state.items_sem_estoque
    pedidos_inc  = st.session_state.pedidos_incompletos
    recomendacoes= st.session_state.recomendacoes
    cfg          = st.session_state.get("cfg", {})
    st.markdown("---")
    st.markdown("""<div class="err-box">
    ⚠️ <strong>Estoque insuficiente detectado</strong> — itens abaixo sem bobina-mãe disponível.
    </div>""", unsafe_allow_html=True)
    st.dataframe(pd.DataFrame(itens_sem), use_container_width=True, hide_index=True)
    st.markdown(f"**Pedidos afetados:** `{', '.join(pedidos_inc)}`")
    cd1, cd2 = st.columns(2)
    with cd1:
        if st.button("🚫 Bloquear pedidos sem estoque completo", use_container_width=True, key="btn_block"):
            run_optimizer(cfg, exclude_orders=pedidos_inc)
    with cd2:
        if st.button("⚡ Otimizar tudo (listar sem estoque no resultado)", use_container_width=True, key="btn_all"):
            run_optimizer(cfg)
    if recomendacoes:
        st.markdown("---")
        st.markdown("**🛒 Recomendação de compra:**")
        st.dataframe(pd.DataFrame(recomendacoes), use_container_width=True, hide_index=True)

# ═════════════════════════════════════════════════════════════════════════════
# TAB RESULTADO
# ═════════════════════════════════════════════════════════════════════════════
with tab_r:
    result    = st.session_state.get("result")
    items_res = st.session_state.get("result_items", [])
    itens_sem = st.session_state.get("items_sem_estoque", [])
    blocked   = st.session_state.get("blocked_orders", [])
    recomendacoes = st.session_state.get("recomendacoes", [])
    cfg       = st.session_state.get("cfg", {})
    shift_end_cfg = cfg.get("shift_end", 17.0)

    if result is None:
        st.info("Rode a otimização para ver os resultados aqui.")
    else:
        order_deadlines = {i.order_id: i.deadline_h for i in items_res}

        if blocked:
            st.markdown(f"""<div class="warn-box">
            🚫 <strong>Bloqueados por falta de estoque:</strong> {', '.join(blocked)}
            </div>""", unsafe_allow_html=True)
        if itens_sem and not blocked:
            st.markdown("""<div class="warn-box">⚠️ <strong>Itens sem estoque</strong> listados abaixo.</div>""",
                        unsafe_allow_html=True)
            st.dataframe(pd.DataFrame(itens_sem), use_container_width=True, hide_index=True)
        if recomendacoes:
            st.markdown("**🛒 Recomendação de compra:**")
            st.dataframe(pd.DataFrame(recomendacoes), use_container_width=True, hide_index=True)
            st.divider()

        # métricas
        m1,m2,m3,m4,m5 = st.columns(5)
        for col, lbl, val, sub in [
            (m1,"Puxadas",f"{len(result.pulls)}","sequências"),
            (m2,"Desperdício",f"{result.total_waste_mm:,.0f}mm","largura não usada"),
            (m3,"Setup total",f"{result.total_setup_min:.0f}min","troca de facas"),
            (m4,"Atraso",f"{result.total_delay_h:.1f}h","⚠️ atrasos" if result.total_delay_h>0 else "✅ ok"),
            (m5,"Solver",result.solver_status,"status"),
        ]:
            with col:
                st.markdown(f"""<div class="metric-box">
                <div class="metric-label">{lbl}</div>
                <div class="metric-value">{val}</div>
                <div class="metric-sub">{sub}</div></div>""", unsafe_allow_html=True)

        st.divider()
        st.markdown('<div class="section-title">Sequência por Máquina</div>', unsafe_allow_html=True)

        for mid in MACHINES:
            pulls = sorted([p for p in result.pulls if p.machine.machine_id==mid],
                           key=lambda p: p.position)
            if not pulls:
                continue

            has_overflow = any(p.end_time_h > shift_end_cfg for p in pulls)
            label = f"🔧 Máquina {mid} — {len(pulls)} puxada(s)"
            if has_overflow:
                label += " ⚠️ ultrapassa turno"

            with st.expander(label, expanded=True):
                rows = []
                for pull in pulls:
                    parts = " + ".join(f"{q}×{pull.pattern.widths[i]}mm"
                                       for i,q in pull.pattern.items.items())
                    over  = "⚠️ Passa do turno" if pull.end_time_h > shift_end_cfg else "✅ Ok"
                    rows.append({
                        "Puxada": pull.pull_id,
                        "Padrão": parts,
                        "Material": pull.pattern.material.value,
                        "Bobinas": pull.pattern.total_rolls,
                        "Sobra (mm)": pull.pattern.waste_mm,
                        "Início": fmt_h(pull.start_time_h),
                        "Fim": fmt_h(pull.end_time_h),
                        "Turno": over,
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

                # Alerta de overflow
                overflow_pulls = [p for p in pulls if p.end_time_h > shift_end_cfg]
                if overflow_pulls:
                    first_over = overflow_pulls[0]
                    st.markdown(f"""<div class="warn-box">
                    ⚠️ A puxada <strong>{first_over.pull_id}</strong> termina às
                    <strong>{fmt_h(first_over.end_time_h)}</strong>, após o fim do turno
                    ({fmt_h(shift_end_cfg)}). O supervisor pode confirmar mesmo assim.
                    </div>""", unsafe_allow_html=True)

                # Botão confirmar OP
                st.markdown(f"**Confirmar produção da Máquina {mid}:**")
                if st.button(f"✅ Enviar OP para Produção — {mid}",
                             key=f"confirm_{mid}", use_container_width=True, type="primary"):
                    confirm_op(mid, result, items_res, cfg)
                    st.success(f"OP da máquina {mid} confirmada! Estoque e fila atualizados.")
                    st.rerun()

        st.divider()

        # Status dos pedidos
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
                    "Conclusão": fmt_h(c) if c else "—",
                    "Prazo": f"{d}h",
                    "Status": "✅ No prazo" if c and c<=now_h+d else ("❌ Atrasado" if c else "⏳"),
                    "Atraso": f"+{c-(now_h+d):.1f}h" if c and c>now_h+d else "—",
                })
        st.dataframe(pd.DataFrame(pedido_rows), use_container_width=True, hide_index=True)
        st.divider()

        # Exportar resultado
        all_rows = []
        for pull in result.pulls:
            for iid, qty in pull.pattern.items.items():
                oid = next((i.order_id for i in items_res if i.item_id==iid), "?")
                all_rows.append({
                    "Puxada":pull.pull_id,"Máquina":pull.machine.machine_id,
                    "Posição":pull.position+1,"Pedido":oid,"Item":iid,
                    "Largura (mm)":pull.pattern.widths.get(iid,"?"),
                    "Qtd":qty,"Material":pull.pattern.material.value,
                    "Sobra (mm)":pull.pattern.waste_mm,
                    "Início":fmt_h(pull.start_time_h),"Fim":fmt_h(pull.end_time_h),
                })
        buf_r = io.BytesIO()
        with pd.ExcelWriter(buf_r, engine="openpyxl") as w:
            pd.DataFrame(all_rows).to_excel(w, index=False, sheet_name="Sequenciamento")
            pd.DataFrame(pedido_rows).to_excel(w, index=False, sheet_name="Status Pedidos")
            if itens_sem: pd.DataFrame(itens_sem).to_excel(w, index=False, sheet_name="Sem Estoque")
            if recomendacoes: pd.DataFrame(recomendacoes).to_excel(w, index=False, sheet_name="Compras Sugeridas")
        buf_r.seek(0)
        st.download_button("📥 Baixar resultado (.xlsx)", data=buf_r,
                           file_name="flexotimiza_resultado.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ═════════════════════════════════════════════════════════════════════════════
# TAB HISTÓRICO
# ═════════════════════════════════════════════════════════════════════════════
with tab_h:
    st.markdown('<div class="section-title">📜 Histórico de Ordens de Produção</div>',
                unsafe_allow_html=True)

    if not st.session_state.op_history:
        st.info("Nenhuma OP confirmada ainda.")
    else:
        df_hist = pd.DataFrame(st.session_state.op_history)

        # Filtros
        fh1, fh2, fh3 = st.columns(3)
        with fh1:
            ops_disp = ["Todas"] + sorted(df_hist["OP"].unique().tolist(), reverse=True)
            op_filter = st.selectbox("Filtrar por OP", ops_disp)
        with fh2:
            mach_disp = ["Todas"] + sorted(df_hist["Máquina"].unique().tolist())
            mach_filter = st.selectbox("Filtrar por Máquina", mach_disp)
        with fh3:
            pedido_disp = ["Todos"] + sorted(df_hist["Pedido"].unique().tolist())
            ped_filter = st.selectbox("Filtrar por Pedido", pedido_disp)

        df_view = df_hist.copy()
        if op_filter    != "Todas":  df_view = df_view[df_view["OP"]      == op_filter]
        if mach_filter  != "Todas":  df_view = df_view[df_view["Máquina"] == mach_filter]
        if ped_filter   != "Todos":  df_view = df_view[df_view["Pedido"]  == ped_filter]

        st.dataframe(df_view, use_container_width=True, hide_index=True)
        st.markdown(f"**{len(df_view)} registros exibidos** de {len(df_hist)} no total.")

        st.divider()
        st.markdown("**📥 Exportar histórico completo**")
        st.caption("Formato CSV — leve, compatível com qualquer banco de dados ou Excel. "
                   "Recomendado para armazenamento de longo prazo.")

        buf_csv = io.StringIO()
        df_hist.to_csv(buf_csv, index=False, encoding="utf-8-sig")
        st.download_button(
            "📥 Baixar histórico completo (.csv)",
            data=buf_csv.getvalue().encode("utf-8-sig"),
            file_name=f"flexotimiza_historico_{datetime.now().strftime('%Y%m%d')}.csv",
            mime="text/csv",
        )

        if st.button("🗑️ Limpar histórico", key="clear_hist"):
            st.session_state.op_history = []
            st.session_state.op_counter = 1
            st.rerun()
