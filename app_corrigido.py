"""
FlexOtimiza — Planejamento de Corte de Bobinas
Fixes applied vs previous version:
  1. Added 'from typing import Dict' (NameError crash in confirm_op)
  2. st.rerun() moved outside try/except (RerunException was silently swallowed)
  3. Fixed deadline comparison (now_h absolute vs d relative units)
  4. export_session() now lazy (not called on every render)
  5. Default optimizer timeout reduced to 25s (safe for Streamlit Cloud free tier)
  6. cfg always read from widget values directly, no race condition
"""

import io
from typing import Dict
from datetime import datetime
from zoneinfo import ZoneInfo
import streamlit as st
import pandas as pd

from models import (
    OrderItem, BobinaStock, Machine, SetupParams, GlobalParams,
    MaintenanceWindow, MaterialType, BobinaSize, MachineSize,
)
from optimizer import optimize

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(page_title="FlexOtimiza", page_icon="🎯", layout="wide")
st.markdown("""<style>
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
MACHINES  = ["G1", "G2", "P1", "P2"]

# ── Session state defaults ────────────────────────────────────────────────────
_EMPTY_ITEMS = pd.DataFrame(columns=["Pedido","Item","Largura (mm)","Quantidade","Material","Prazo (h)","Produzido"])
_EMPTY_STOCK = pd.DataFrame(columns=["Bobina","Tamanho","Material","Quantidade"])
_EMPTY_MAINT = pd.DataFrame(columns=["Máquina","Início (h)","Duração (h)"])

for k, v in [
    ("items_df",   _EMPTY_ITEMS),
    ("stock_df",   _EMPTY_STOCK),
    ("maint_df",   _EMPTY_MAINT),
    ("machine_busy", {"G1":0.0,"G2":0.0,"P1":0.0,"P2":0.0}),
    ("op_history", []),
    ("op_counter", 1),
    ("result",     None),
    ("result_items", []),
    ("items_sem_estoque", []),
    ("pedidos_incompletos", []),
    ("recomendacoes", []),
    ("blocked_orders", []),
    ("stock_decision_pending", False),
    ("optimizer_running", False),
]:
    if k not in st.session_state:
        st.session_state[k] = v


# ── Utility functions ─────────────────────────────────────────────────────────
def now_real_h() -> float:
    """Hora atual de Brasília como decimal (ex: 14:30 → 14.5). NUNCA ajustada."""
    t = datetime.now(ZoneInfo("America/Sao_Paulo")).time()
    return t.hour + t.minute / 60.0 + t.second / 3600.0


def planning_h(shift_start: float, shift_end: float) -> float:
    """
    Hora de planejamento: hora atual se dentro do turno,
    senão shift_start (planeja para o próximo turno).
    """
    h = now_real_h()
    return h if shift_start <= h < shift_end else shift_start


def fmt_h(h: float) -> str:
    hh = int(h)
    mm = int(round((h - hh) * 60))
    if mm == 60:
        hh += 1; mm = 0
    return f"{hh:02d}:{mm:02d}"


def build_objects(items_df, stock_df, maint_df, cfg, exclude_orders=None):
    exclude_orders = exclude_orders or []
    params = GlobalParams(
        bobina_length_m = cfg.get("bobina_len",  2000),
        speed_mpm       = cfg.get("speed",        225),
        large_width_mm  = cfg.get("large_width",  2000),
        small_width_mm  = cfg.get("small_width",  1000),
        shift_start_h   = cfg.get("shift_start",  8.0),
        shift_end_h     = cfg.get("shift_end",   17.0),
    )
    setup = SetupParams(
        fixed_time_min     = cfg.get("fixed_time",  15.0),
        time_per_knife_min = cfg.get("knife_time",   2.5),
    )
    busy = st.session_state.machine_busy
    machines = [
        Machine("G1", MachineSize.GRANDE,  params, busy_until_h=busy.get("G1", 0.0)),
        Machine("G2", MachineSize.GRANDE,  params, busy_until_h=busy.get("G2", 0.0)),
        Machine("P1", MachineSize.PEQUENA, params, busy_until_h=busy.get("P1", 0.0)),
        Machine("P2", MachineSize.PEQUENA, params, busy_until_h=busy.get("P2", 0.0)),
    ]
    items = []
    for _, row in items_df.iterrows():
        if str(row["Pedido"]) in exclude_orders:
            continue
        mat = MAT_MAP.get(str(row["Material"]))
        if mat is None:
            continue
        items.append(OrderItem(
            item_id    = str(row["Item"]),
            order_id   = str(row["Pedido"]),
            width_mm   = int(row["Largura (mm)"]),
            quantity   = int(row["Quantidade"]),
            material   = mat,
            deadline_h = float(row["Prazo (h)"]),
            produced   = int(row.get("Produzido", 0)),
        ))
    stock = []
    for _, row in stock_df.iterrows():
        mat = MAT_MAP.get(str(row["Material"]))
        sz  = SIZE_MAP.get(str(row["Tamanho"]), BobinaSize.GRANDE)
        if mat is None:
            continue
        stock.append(BobinaStock(
            bobina_id = str(row["Bobina"]),
            size      = sz,
            material  = mat,
            quantity  = int(row["Quantidade"]),
        ))
    maintenance = [
        MaintenanceWindow(str(r["Máquina"]), float(r["Início (h)"]), float(r["Duração (h)"]))
        for _, r in maint_df.iterrows()
    ]
    return items, stock, machines, setup, params, maintenance


def check_stock_coverage(items_df, stock_df):
    stock_qty: Dict[tuple, int] = {}
    for _, row in stock_df.iterrows():
        key = (str(row["Material"]), str(row["Tamanho"]))
        stock_qty[key] = stock_qty.get(key, 0) + int(row["Quantidade"])
    itens_sem, pedidos_inc, recomendacoes = [], set(), {}
    for _, row in items_df.iterrows():
        mat = str(row["Material"])
        tem = any(stock_qty.get((mat, sz), 0) > 0
                  for sz in ["Grande (~2000mm)", "Pequena (~1000mm)"])
        if not tem:
            itens_sem.append({
                "Pedido": row["Pedido"], "Item": row["Item"],
                "Largura (mm)": row["Largura (mm)"], "Quantidade": row["Quantidade"],
                "Material": mat, "Motivo": "Sem bobina-mãe deste material",
            })
            pedidos_inc.add(str(row["Pedido"]))
            recomendacoes.setdefault(mat, {"Material": mat, "Qtd sugerida": 0})
            recomendacoes[mat]["Qtd sugerida"] += 1
    return itens_sem, list(pedidos_inc), list(recomendacoes.values())


def run_optimizer(cfg: dict, exclude_orders=None) -> bool:
    """
    Runs the optimizer. Returns True on success, False on failure.
    IMPORTANT: st.rerun() must be called by the CALLER after this returns True.
    st.rerun() raises RerunException internally — calling it inside a
    try/except would silently swallow the exception and prevent the rerun.
    """
    items, stock, machines, setup, params, maintenance = build_objects(
        st.session_state.items_df, st.session_state.stock_df,
        st.session_state.maint_df, cfg, exclude_orders=exclude_orders,
    )
    if not items:
        st.error("Nenhum pedido pode ser produzido com o estoque atual.")
        return False

    current_h = planning_h(cfg.get("shift_start", 8.0), cfg.get("shift_end", 17.0))
    tl        = int(cfg.get("time_limit", 25))  # default 25s — safe for Streamlit Cloud

    error_msg = None
    error_tb  = None
    result    = None

    # Run optimizer inside spinner — ALL exceptions are caught here except RerunException
    with st.spinner(f"⚙️ Calculando sequenciamento ótimo (limite: {tl}s)..."):
        try:
            result = optimize(
                items        = items,
                stock        = stock,
                machines     = machines,
                setup        = setup,
                params       = params,
                now_h        = current_h,
                maintenance  = maintenance,
                alpha        = cfg.get("alpha", 1.0),
                beta         = cfg.get("beta",  1.0),
                gamma        = cfg.get("gamma", 3.0),
                delta        = cfg.get("delta", 0.5),
                time_limit_s = tl,
            )
        except Exception as e:
            import traceback
            error_msg = str(e)
            error_tb  = traceback.format_exc()

    # Handle error OUTSIDE spinner and try/except
    if error_msg:
        st.error(f"Erro na otimização: {error_msg}")
        with st.expander("Detalhes do erro"):
            st.code(error_tb)
        return False

    if result is None:
        st.error("Otimizador não retornou resultado. Verifique os dados de entrada.")
        return False

    # Persist result to session state
    st.session_state.result                  = result
    st.session_state.result_items            = items
    st.session_state.blocked_orders          = list(exclude_orders or [])
    st.session_state.stock_decision_pending  = False
    return True


def confirm_op(machine_id: str, result, items_res, cfg: dict):
    """Confirms an OP: updates stock, order queue, machine availability, and history."""
    pulls = sorted(
        [p for p in result.pulls if p.machine.machine_id == machine_id],
        key=lambda p: p.position,
    )
    if not pulls:
        return

    # 1. Update stock
    bobinas_usadas: Dict[str, int] = {}
    for pull in pulls:
        bid = pull.bobina.bobina_id
        bobinas_usadas[bid] = bobinas_usadas.get(bid, 0) + 1

    stock_df = st.session_state.stock_df.copy()
    for bid, used in bobinas_usadas.items():
        mask = stock_df["Bobina"] == bid
        if mask.any():
            old_qty = int(stock_df.loc[mask, "Quantidade"].values[0])
            stock_df.loc[mask, "Quantidade"] = max(0, old_qty - used)
    stock_df = stock_df[stock_df["Quantidade"] > 0].reset_index(drop=True)
    st.session_state.stock_df = stock_df

    # 2. Update order queue
    items_produced: Dict[str, int] = {}
    for pull in pulls:
        for iid, qty in pull.pattern.items.items():
            items_produced[iid] = items_produced.get(iid, 0) + qty

    items_df = st.session_state.items_df.copy()
    for iid, qty_prod in items_produced.items():
        mask = items_df["Item"] == iid
        if mask.any():
            old_prod  = int(items_df.loc[mask, "Produzido"].values[0])
            total_qty = int(items_df.loc[mask, "Quantidade"].values[0])
            items_df.loc[mask, "Produzido"] = min(old_prod + qty_prod, total_qty)
    items_df = items_df[
        items_df.apply(lambda r: int(r["Produzido"]) < int(r["Quantidade"]), axis=1)
    ].reset_index(drop=True)
    st.session_state.items_df = items_df

    # 3. Update machine availability
    end_h = max(p.end_time_h for p in pulls)
    st.session_state.machine_busy[machine_id] = end_h

    # 4. Record in history
    op_id     = f"OP{st.session_state.op_counter:04d}"
    now_str   = datetime.now(ZoneInfo("America/Sao_Paulo")).strftime("%Y-%m-%d %H:%M:%S")
    shift_end = cfg.get("shift_end", 17.0)

    for pull in pulls:
        for iid, qty in pull.pattern.items.items():
            order_id = next((i.order_id for i in items_res if i.item_id == iid), "?")
            st.session_state.op_history.append({
                "OP":               op_id,
                "Máquina":          machine_id,
                "Confirmada em":    now_str,
                "Puxada":           pull.pull_id,
                "Pedido":           order_id,
                "Item":             iid,
                "Largura (mm)":     pull.pattern.widths.get(iid, "?"),
                "Qtd Produzida":    qty,
                "Material":         pull.pattern.material.value,
                "Sobra (mm)":       pull.pattern.waste_mm,
                "Bobina-Mãe":       pull.bobina.bobina_id,
                "Início":           fmt_h(pull.start_time_h),
                "Fim":              fmt_h(pull.end_time_h),
                "Passa do Turno":   "⚠️ Sim" if pull.end_time_h > shift_end else "✅ Não",
            })
    st.session_state.op_counter += 1
    st.session_state.result = None  # force re-optimization


def make_session_excel() -> bytes:
    """Generates session Excel. Called LAZILY only when user requests download."""
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        st.session_state.items_df.to_excel(w, index=False, sheet_name="Pedidos")
        st.session_state.stock_df.to_excel(w, index=False, sheet_name="Estoque")
        st.session_state.maint_df.to_excel(w, index=False, sheet_name="Manutencao")
        pd.DataFrame([
            {"Máquina": k, "Livre às (h)": v}
            for k, v in st.session_state.machine_busy.items()
        ]).to_excel(w, index=False, sheet_name="Maquinas")
        if st.session_state.op_history:
            pd.DataFrame(st.session_state.op_history).to_excel(w, index=False, sheet_name="Historico")
        pd.DataFrame([{"op_counter": st.session_state.op_counter}]).to_excel(
            w, index=False, sheet_name="Meta")
    return buf.getvalue()


def load_session_excel(uploaded) -> bool:
    try:
        xls = pd.ExcelFile(uploaded)
        if "Pedidos"    in xls.sheet_names: st.session_state.items_df   = pd.read_excel(xls, "Pedidos")
        if "Estoque"    in xls.sheet_names: st.session_state.stock_df   = pd.read_excel(xls, "Estoque")
        if "Manutencao" in xls.sheet_names: st.session_state.maint_df   = pd.read_excel(xls, "Manutencao")
        if "Maquinas"   in xls.sheet_names:
            df_m = pd.read_excel(xls, "Maquinas")
            st.session_state.machine_busy = dict(zip(df_m["Máquina"], df_m["Livre às (h)"]))
        if "Historico"  in xls.sheet_names:
            st.session_state.op_history   = pd.read_excel(xls, "Historico").to_dict("records")
        if "Meta"       in xls.sheet_names:
            st.session_state.op_counter   = int(pd.read_excel(xls, "Meta")["op_counter"].iloc[0])
        return True
    except Exception as e:
        st.error(f"Erro ao carregar sessão: {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
# LAYOUT
# ═════════════════════════════════════════════════════════════════════════════
# Version tag — visible in UI to confirm which code is deployed
APP_VERSION = "v3.1"
st.markdown("## 🎯 FlexOtimiza — Planejamento de Corte de Bobinas")
st.caption(f"Otimizador de sequenciamento e agrupamento para indústria flexográfica  |  {APP_VERSION}")

# ── Shift bar (reads cfg from session_state — already set by tab_c on prev run) ──
_cfg        = st.session_state.get("cfg", {})
_ss         = _cfg.get("shift_start", 8.0)
_se         = _cfg.get("shift_end",  17.0)
_now        = now_real_h()
_plan_h     = planning_h(_ss, _se)
_outside    = (_now >= _se or _now < _ss)
_remaining  = max(0.0, (_se - _now) * 60)
_busy       = [k for k, v in st.session_state.machine_busy.items() if v > _plan_h]

sb1, sb2, sb3, sb4 = st.columns(4)
with sb1:
    st.markdown(f"""<div class="shift-bar">
    🕐 <strong>Agora: {fmt_h(_now)}</strong><br>
    <small>Turno: {fmt_h(_ss)} – {fmt_h(_se)}</small>
    </div>""", unsafe_allow_html=True)
with sb2:
    if _outside:
        st.markdown(f"""<div class="shift-bar">
        📋 <strong style="color:#375623">Modo planejamento</strong><br>
        <small>Fora do turno — planejando a partir das {fmt_h(_ss)}</small>
        </div>""", unsafe_allow_html=True)
    else:
        _col = "#c55a11" if _remaining < 60 else "#1F4E79"
        st.markdown(f"""<div class="shift-bar">
        ⏳ <strong style="color:{_col}">{_remaining:.0f} min restantes</strong><br>
        <small>até fim do turno ({fmt_h(_se)})</small>
        </div>""", unsafe_allow_html=True)
with sb3:
    if _busy:
        _bs = " | ".join(f"{m}: {fmt_h(st.session_state.machine_busy[m])}" for m in _busy)
        st.markdown(f"""<div class="warn-box">🔧 <strong>Ocupadas:</strong> {_bs}</div>""",
                    unsafe_allow_html=True)
    else:
        st.markdown("""<div class="ok-box">✅ <strong>Todas as máquinas livres</strong></div>""",
                    unsafe_allow_html=True)
with sb4:
    if st.button("🔄 Atualizar horário", use_container_width=True):
        st.rerun()

# ── Save / Load session ───────────────────────────────────────────────────────
with st.expander("💾 Salvar / Carregar sessão", expanded=False):
    _c1, _c2 = st.columns(2)
    with _c1:
        st.markdown("**Salvar sessão completa**")
        # BUG FIX 4: session Excel only generated when button is clicked, not on every render
        if st.button("📥 Gerar e baixar sessão (.xlsx)", use_container_width=True, key="btn_gen_session"):
            st.session_state["_session_bytes"] = make_session_excel()
        if st.session_state.get("_session_bytes"):
            st.download_button(
                "⬇️ Clique para baixar",
                data=st.session_state["_session_bytes"],
                file_name="flexotimiza_sessao.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key="dl_session",
            )
    with _c2:
        st.markdown("**Carregar sessão salva**")
        _up = st.file_uploader("Arquivo de sessão (.xlsx)", type=["xlsx"], key="sess_up")
        if _up and load_session_excel(_up):
            st.success("Sessão carregada!")
            st.rerun()

st.divider()

# ═════════════════════════════════════════════════════════════════════════════
# TABS
# ═════════════════════════════════════════════════════════════════════════════
tab_p, tab_s, tab_c, tab_r, tab_h = st.tabs([
    "📋 Pedidos", "📦 Estoque", "⚙️ Configurações", "📊 Resultado", "📜 Histórico de OPs"
])

# ── TAB: PEDIDOS ──────────────────────────────────────────────────────────────
with tab_p:
    st.markdown('<div class="section-title">Fila de Pedidos</div>', unsafe_allow_html=True)
    st.caption("Cada linha é um item de um pedido. Um pedido pode ter múltiplos itens com larguras e materiais diferentes.")
    _pf, _pt = st.columns([1, 2])
    with _pf:
        with st.form("fp", clear_on_submit=True):
            _pid  = st.text_input("Nº do Pedido", placeholder="P001")
            _iid  = st.text_input("ID do Item",   placeholder="P001-A", help="Deve ser único. Ex: P001-A, P001-B")
            _larg = st.number_input("Largura (mm)", 1, 5000, 100)
            _qtd  = st.number_input("Quantidade",   1, 500,  1)
            _mat  = st.selectbox("Material", MATERIALS)
            _prz  = st.number_input("Prazo (h)",    1, 720,  72)
            _prod = st.number_input("Já produzido", 0, 500,  0)
            if st.form_submit_button("➕ Adicionar", use_container_width=True):
                if not _pid or not _iid:
                    st.warning("Preencha pedido e ID do item.")
                elif _iid in st.session_state.items_df["Item"].values:
                    st.warning(f"ID '{_iid}' já existe.")
                else:
                    st.session_state.items_df = pd.concat([
                        st.session_state.items_df,
                        pd.DataFrame([{"Pedido":_pid,"Item":_iid,"Largura (mm)":_larg,
                                       "Quantidade":_qtd,"Material":_mat,"Prazo (h)":_prz,"Produzido":_prod}])
                    ], ignore_index=True)
                    st.success(f"Item {_iid} adicionado.")
        st.markdown("---")
        st.markdown("**Importar do Excel**")
        st.caption("Colunas: Pedido, Item, Largura (mm), Quantidade, Material, Prazo (h)")
        _up_i = st.file_uploader("Planilha de pedidos", type=["xlsx","csv"], key="items_up")
        if _up_i:
            try:
                df_i = pd.read_csv(_up_i) if _up_i.name.endswith(".csv") else pd.read_excel(_up_i)
                req  = {"Pedido","Item","Largura (mm)","Quantidade","Material","Prazo (h)"}
                if req.issubset(df_i.columns):
                    if "Produzido" not in df_i.columns: df_i["Produzido"] = 0
                    st.session_state.items_df = df_i[list(req | {"Produzido"})].copy()
                    st.success(f"{len(df_i)} itens importados.")
                else:
                    st.error(f"Colunas necessárias: {req}")
            except Exception as e:
                st.error(f"Erro ao importar: {e}")
    with _pt:
        if st.session_state.items_df.empty:
            st.info("Nenhum item adicionado ainda.")
        else:
            st.session_state.items_df = st.data_editor(
                st.session_state.items_df, use_container_width=True,
                num_rows="dynamic", key="items_ed")
            st.markdown("**Resumo por pedido**")
            try:
                _summary = (
                    st.session_state.items_df.copy()
                    .assign(Pendente=lambda df: df.apply(
                        lambda r: max(0, int(r["Quantidade"]) - int(r["Produzido"])), axis=1))
                    .groupby("Pedido")
                    .agg(Itens=("Item","count"), Prazo=("Prazo (h)","min"), Pendente=("Pendente","sum"))
                    .reset_index()
                )
                st.dataframe(_summary, use_container_width=True, hide_index=True)
            except Exception:
                pass

# ── TAB: ESTOQUE ──────────────────────────────────────────────────────────────
with tab_s:
    st.markdown('<div class="section-title">Estoque de Bobinas-Mãe</div>', unsafe_allow_html=True)
    _ss1, _ss2 = st.columns([1, 2])
    with _ss1:
        with st.form("fs", clear_on_submit=True):
            _bid  = st.text_input("ID da Bobina", placeholder="BM-001")
            _tam  = st.selectbox("Tamanho", ["Grande (~2000mm)", "Pequena (~1000mm)"])
            _matb = st.selectbox("Material", MATERIALS)
            _qtdb = st.number_input("Quantidade", 1, 500, 1)
            if st.form_submit_button("➕ Adicionar", use_container_width=True):
                if not _bid:
                    st.warning("Informe o ID da bobina.")
                elif _bid in st.session_state.stock_df["Bobina"].values:
                    st.warning("ID já existe.")
                else:
                    st.session_state.stock_df = pd.concat([
                        st.session_state.stock_df,
                        pd.DataFrame([{"Bobina":_bid,"Tamanho":_tam,"Material":_matb,"Quantidade":_qtdb}])
                    ], ignore_index=True)
                    st.success("Bobina adicionada.")
    with _ss2:
        if st.session_state.stock_df.empty:
            st.info("Nenhuma bobina registrada.")
        else:
            st.session_state.stock_df = st.data_editor(
                st.session_state.stock_df, use_container_width=True,
                num_rows="dynamic", key="stock_ed")

# ── TAB: CONFIGURAÇÕES ────────────────────────────────────────────────────────
with tab_c:
    st.markdown('<div class="section-title">Configurações</div>', unsafe_allow_html=True)
    _cc1, _cc2, _cc3 = st.columns(3)
    with _cc1:
        st.markdown("**🕐 Turno**")
        _shift_start_h = st.number_input("Início (h)", 0.0, 23.0, 8.0,  0.5, key="cfg_ss")
        _shift_end_h   = st.number_input("Fim (h)",    0.0, 23.0, 17.0, 0.5, key="cfg_se")
        st.markdown("**⏱️ Setup** *(preencher após cronoanálise)*")
        _fixed_time  = st.number_input("Tempo fixo (min)",           1.0, 120.0, 15.0, 0.5,  key="cfg_ft")
        _knife_time  = st.number_input("Tempo/faca alterada (min)",  0.1,  30.0,  2.5, 0.1,  key="cfg_kt")
        st.markdown("**🏭 Máquinas**")
        _bobina_len  = st.number_input("Comprimento bobina-mãe (m)", 100.0, 10000.0, 2000.0, 100.0, key="cfg_bl")
        _speed       = st.number_input("Velocidade (m/min)",         100.0,   500.0,  225.0,   5.0, key="cfg_sp")
        _large_width = st.number_input("Largura bobina grande (mm)", 500,  5000, 2000, 50, key="cfg_lw")
        _small_width = st.number_input("Largura bobina pequena (mm)",200,  2000, 1000, 50, key="cfg_sw")
    with _cc2:
        st.markdown("**⚖️ Pesos da Função Objetivo**")
        st.caption("Valores maiores = maior prioridade nesse objetivo")
        _alpha      = st.slider("α — Desperdício",    0.0, 5.0, 1.0, 0.5, key="cfg_a")
        _beta       = st.slider("β — Setup",          0.0, 5.0, 1.0, 0.5, key="cfg_b")
        _gamma      = st.slider("γ — Atraso",         0.0, 5.0, 3.0, 0.5, key="cfg_g")
        _delta      = st.slider("δ — Superprodução",  0.0, 5.0, 0.5, 0.5, key="cfg_d")
        st.markdown("**⏰ Solver**")
        _time_limit = st.number_input(
            "Tempo máx. (s)", 5, 120, 25, 5, key="cfg_tl",
            help="25s é seguro para Streamlit Cloud. Aumente se rodar localmente.")
        st.markdown("**🔧 Disponibilidade das Máquinas**")
        st.caption("Ajuste se uma máquina já tiver trabalho em andamento.")
        for _mid in MACHINES:
            _val = float(st.session_state.machine_busy.get(_mid, 0.0))
            _nv  = st.number_input(f"{_mid} — livre às (h)", 0.0, 24.0, _val, 0.25, key=f"busy_{_mid}")
            st.session_state.machine_busy[_mid] = _nv
    with _cc3:
        st.markdown("**🔧 Manutenção Preventiva**")
        with st.form("fm", clear_on_submit=True):
            _mid_m = st.selectbox("Máquina", MACHINES)
            _sh    = st.number_input("Início (h)",   0.0, 24.0, 2.0, 0.5)
            _dh    = st.number_input("Duração (h)",  0.5,  8.0, 1.0, 0.5)
            if st.form_submit_button("➕ Adicionar", use_container_width=True):
                st.session_state.maint_df = pd.concat([
                    st.session_state.maint_df,
                    pd.DataFrame([{"Máquina":_mid_m,"Início (h)":_sh,"Duração (h)":_dh}])
                ], ignore_index=True)
        if not st.session_state.maint_df.empty:
            st.session_state.maint_df = st.data_editor(
                st.session_state.maint_df, use_container_width=True,
                num_rows="dynamic", key="maint_ed")
        else:
            st.info("Sem janelas de manutenção.")

    # Always update cfg from current widget values (no race condition)
    st.session_state["cfg"] = {
        "shift_start": _shift_start_h, "shift_end": _shift_end_h,
        "fixed_time":  _fixed_time,    "knife_time": _knife_time,
        "bobina_len":  _bobina_len,    "speed":      _speed,
        "large_width": _large_width,   "small_width": _small_width,
        "alpha":       _alpha,         "beta":        _beta,
        "gamma":       _gamma,         "delta":       _delta,
        "time_limit":  _time_limit,
    }

# ── OPTIMIZE BUTTON ───────────────────────────────────────────────────────────
st.divider()
_, _ob1, _ob2 = st.columns([2, 1, 1])
with _ob1:
    run_btn = st.button("🚀 Otimizar Agora", use_container_width=True, type="primary")
with _ob2:
    if st.button("🗑️ Limpar Tudo", use_container_width=True):
        for k in ["items_df","stock_df","maint_df","machine_busy","op_history","op_counter",
                  "result","result_items","items_sem_estoque","pedidos_incompletos",
                  "recomendacoes","blocked_orders","stock_decision_pending","cfg","_session_bytes"]:
            if k in st.session_state:
                del st.session_state[k]
        st.rerun()

# ── Run optimization when button clicked ─────────────────────────────────────
# NOTE: cfg is now guaranteed correct because tab_c always runs before this point
_cfg = st.session_state.get("cfg", {})

# Show result summary banner if result exists (visible regardless of active tab)
if st.session_state.get("result") is not None:
    _r = st.session_state.result
    st.markdown(f"""<div class="ok-box">
    ✅ <strong>Resultado disponível</strong> — {len(_r.pulls)} puxadas |
    Desperdício: {_r.total_waste_mm:,.0f}mm |
    Setup: {_r.total_setup_min:.0f}min |
    Atraso: {_r.total_delay_h:.1f}h |
    Solver: {_r.solver_status}
    — <em>Veja detalhes na aba 📊 Resultado</em>
    </div>""", unsafe_allow_html=True)

if run_btn:
    _n_items = len(st.session_state.items_df)
    _n_stock = len(st.session_state.stock_df)
    st.info(f"🔄 Iniciando otimização... ({_n_items} itens, {_n_stock} bobinas no estoque)")

    if st.session_state.items_df.empty or st.session_state.stock_df.empty:
        st.error("Preencha pedidos e estoque antes de otimizar.")
    else:
        _itens_sem, _pedidos_inc, _recomendacoes = check_stock_coverage(
            st.session_state.items_df, st.session_state.stock_df)
        if _itens_sem:
            st.session_state.items_sem_estoque   = _itens_sem
            st.session_state.pedidos_incompletos = _pedidos_inc
            st.session_state.recomendacoes       = _recomendacoes
            st.session_state.stock_decision_pending = True
            st.rerun()
        else:
            st.session_state.items_sem_estoque = []
            st.session_state.recomendacoes     = []
            if run_optimizer(_cfg):
                st.rerun()

# Debug panel (toggle in sidebar)
with st.sidebar:
    st.markdown("### 🔍 Diagnóstico")
    _show_debug = st.checkbox("Mostrar painel de debug", value=False)
    if _show_debug:
        st.markdown(f"**Versão:** {APP_VERSION}")
        st.markdown(f"**Itens:** {len(st.session_state.items_df)}")
        st.markdown(f"**Estoque:** {len(st.session_state.stock_df)}")
        st.markdown(f"**result:** {'✅ existe' if st.session_state.get('result') else '❌ None'}")
        st.markdown(f"**stock_decision_pending:** {st.session_state.get('stock_decision_pending')}")
        st.markdown(f"**cfg.time_limit:** {st.session_state.get('cfg',{}).get('time_limit','?')}")
        st.markdown(f"**cfg.alpha:** {st.session_state.get('cfg',{}).get('alpha','?')}")
        st.markdown(f"**cfg.gamma:** {st.session_state.get('cfg',{}).get('gamma','?')}")
        if not st.session_state.items_df.empty:
            st.markdown("**Items (primeiros 3):**")
            st.dataframe(st.session_state.items_df.head(3), use_container_width=True)
        if not st.session_state.stock_df.empty:
            st.markdown("**Estoque (primeiros 3):**")
            st.dataframe(st.session_state.stock_df.head(3), use_container_width=True)

# ── Stock shortage popup ──────────────────────────────────────────────────────
if st.session_state.get("stock_decision_pending"):
    _itens_sem   = st.session_state.items_sem_estoque
    _pedidos_inc = st.session_state.pedidos_incompletos
    _recomend    = st.session_state.recomendacoes
    st.markdown("---")
    st.markdown("""<div class="err-box">
    ⚠️ <strong>Estoque insuficiente</strong> — itens abaixo sem bobina-mãe disponível.
    </div>""", unsafe_allow_html=True)
    st.dataframe(pd.DataFrame(_itens_sem), use_container_width=True, hide_index=True)
    st.markdown(f"**Pedidos afetados:** `{', '.join(_pedidos_inc)}`")
    st.markdown("**Como prosseguir?**")
    _d1, _d2 = st.columns(2)
    with _d1:
        if st.button("🚫 Bloquear pedidos incompletos", use_container_width=True, key="btn_block"):
            if run_optimizer(_cfg, exclude_orders=_pedidos_inc):
                st.rerun()
    with _d2:
        if st.button("⚡ Otimizar tudo (listar sem estoque)", use_container_width=True, key="btn_all"):
            if run_optimizer(_cfg):
                st.rerun()
    if _recomend:
        st.markdown("---")
        st.markdown("**🛒 Recomendação de compra:**")
        st.dataframe(pd.DataFrame(_recomend), use_container_width=True, hide_index=True)

# ═════════════════════════════════════════════════════════════════════════════
# TAB: RESULTADO
# ═════════════════════════════════════════════════════════════════════════════
with tab_r:
    result    = st.session_state.get("result")
    items_res = st.session_state.get("result_items", [])
    blocked   = st.session_state.get("blocked_orders", [])
    itens_sem = st.session_state.get("items_sem_estoque", [])
    recomend  = st.session_state.get("recomendacoes", [])
    cfg_r     = st.session_state.get("cfg", {})
    shift_end_r = cfg_r.get("shift_end", 17.0)
    plan_h_r    = planning_h(cfg_r.get("shift_start", 8.0), shift_end_r)

    if result is None:
        st.info("Rode a otimização para ver os resultados aqui.")
    else:
        # Alerts
        if blocked:
            st.markdown(f"""<div class="warn-box">
            🚫 <strong>Bloqueados por falta de estoque:</strong> {', '.join(blocked)}
            </div>""", unsafe_allow_html=True)
        if itens_sem and not blocked:
            st.markdown("""<div class="warn-box">⚠️ <strong>Itens sem estoque</strong> — listados abaixo.</div>""",
                        unsafe_allow_html=True)
            st.dataframe(pd.DataFrame(itens_sem), use_container_width=True, hide_index=True)
        if recomend:
            st.markdown("**🛒 Recomendação de compra:**")
            st.dataframe(pd.DataFrame(recomend), use_container_width=True, hide_index=True)
            st.divider()

        # Metrics
        _m1, _m2, _m3, _m4, _m5 = st.columns(5)
        for _col, _lbl, _val, _sub in [
            (_m1, "Puxadas",     f"{len(result.pulls)}",                 "sequências de corte"),
            (_m2, "Desperdício", f"{result.total_waste_mm:,.0f}mm",      "largura não usada"),
            (_m3, "Setup total", f"{result.total_setup_min:.0f}min",     "troca de facas"),
            (_m4, "Atraso",      f"{result.total_delay_h:.1f}h",         "⚠️ atrasos" if result.total_delay_h > 0 else "✅ ok"),
            (_m5, "Solver",      result.solver_status,                   "status"),
        ]:
            with _col:
                st.markdown(f"""<div class="metric-box">
                <div class="metric-label">{_lbl}</div>
                <div class="metric-value">{_val}</div>
                <div class="metric-sub">{_sub}</div></div>""", unsafe_allow_html=True)
        st.divider()

        # Machine sequences
        st.markdown('<div class="section-title">Sequência por Máquina</div>', unsafe_allow_html=True)
        for _mid in MACHINES:
            _pulls = sorted([p for p in result.pulls if p.machine.machine_id == _mid],
                            key=lambda p: p.position)
            if not _pulls:
                continue
            _overflow = any(p.end_time_h > shift_end_r for p in _pulls)
            _label = f"🔧 Máquina {_mid} — {len(_pulls)} puxada(s)"
            if _overflow:
                _label += " ⚠️ ultrapassa turno"

            with st.expander(_label, expanded=True):
                _rows = []
                for pull in _pulls:
                    _parts = " + ".join(f"{q}×{pull.pattern.widths[i]}mm"
                                        for i, q in pull.pattern.items.items())
                    _rows.append({
                        "Puxada":    pull.pull_id,
                        "Padrão":    _parts,
                        "Material":  pull.pattern.material.value,
                        "Bobinas":   pull.pattern.total_rolls,
                        "Sobra(mm)": pull.pattern.waste_mm,
                        "Início":    fmt_h(pull.start_time_h),
                        "Fim":       fmt_h(pull.end_time_h),
                        "Turno":     "⚠️ Passa" if pull.end_time_h > shift_end_r else "✅ Ok",
                    })
                st.dataframe(pd.DataFrame(_rows), use_container_width=True, hide_index=True)

                _over_pulls = [p for p in _pulls if p.end_time_h > shift_end_r]
                if _over_pulls:
                    st.markdown(f"""<div class="warn-box">
                    ⚠️ Puxada <strong>{_over_pulls[0].pull_id}</strong> termina às
                    <strong>{fmt_h(_over_pulls[0].end_time_h)}</strong> — após {fmt_h(shift_end_r)}.
                    O supervisor pode confirmar mesmo assim.
                    </div>""", unsafe_allow_html=True)

                st.markdown(f"**Confirmar produção da Máquina {_mid}:**")
                if st.button(f"✅ Enviar OP para Produção — {_mid}",
                             key=f"confirm_{_mid}", use_container_width=True, type="primary"):
                    confirm_op(_mid, result, items_res, cfg_r)
                    st.success(f"OP da máquina {_mid} confirmada! Estoque e fila atualizados.")
                    st.rerun()

        st.divider()

        # Order status
        st.markdown('<div class="section-title">Status dos Pedidos</div>', unsafe_allow_html=True)
        # BUG FIX 3: deadline is absolute clock time (plan_h + d), not now_h + d
        order_deadlines = {i.order_id: i.deadline_h for i in items_res}
        _ped_rows = []
        for _oid in sorted(set(i.order_id for i in items_res) | set(blocked)):
            if _oid in blocked:
                _ped_rows.append({"Pedido":_oid,"Conclusão":"—","Prazo":"—","Status":"🚫 Bloqueado","Atraso":"—"})
            else:
                _c = result.order_completion.get(_oid)
                _d = order_deadlines.get(_oid, 72)
                # BUG FIX 3: compare absolute completion time vs absolute deadline
                _deadline_abs = plan_h_r + _d  # e.g. 8.0 + 72 = 80.0 (next day 8AM)
                _ped_rows.append({
                    "Pedido":    _oid,
                    "Conclusão": fmt_h(_c) if _c else "—",
                    "Prazo":     f"{_d}h a partir de {fmt_h(plan_h_r)}",
                    "Status":    "✅ No prazo" if _c and _c <= _deadline_abs
                                 else ("❌ Atrasado" if _c else "⏳ Pendente"),
                    "Atraso":    f"+{_c - _deadline_abs:.1f}h" if _c and _c > _deadline_abs else "—",
                })
        st.dataframe(pd.DataFrame(_ped_rows), use_container_width=True, hide_index=True)
        st.divider()

        # Export result
        _all_rows = []
        for pull in result.pulls:
            for iid, qty in pull.pattern.items.items():
                _oid = next((i.order_id for i in items_res if i.item_id == iid), "?")
                _all_rows.append({
                    "Puxada": pull.pull_id, "Máquina": pull.machine.machine_id,
                    "Posição": pull.position + 1, "Pedido": _oid, "Item": iid,
                    "Largura(mm)": pull.pattern.widths.get(iid, "?"),
                    "Qtd": qty, "Material": pull.pattern.material.value,
                    "Sobra(mm)": pull.pattern.waste_mm,
                    "Início": fmt_h(pull.start_time_h), "Fim": fmt_h(pull.end_time_h),
                })
        _buf_r = io.BytesIO()
        with pd.ExcelWriter(_buf_r, engine="openpyxl") as _w:
            pd.DataFrame(_all_rows).to_excel(_w, index=False, sheet_name="Sequenciamento")
            pd.DataFrame(_ped_rows).to_excel(_w, index=False, sheet_name="Status Pedidos")
            if itens_sem:  pd.DataFrame(itens_sem).to_excel(_w, index=False, sheet_name="Sem Estoque")
            if recomend:   pd.DataFrame(recomend).to_excel(_w, index=False, sheet_name="Compras")
        _buf_r.seek(0)
        st.download_button("📥 Baixar resultado (.xlsx)", data=_buf_r,
                           file_name="flexotimiza_resultado.xlsx",
                           mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ═════════════════════════════════════════════════════════════════════════════
# TAB: HISTÓRICO
# ═════════════════════════════════════════════════════════════════════════════
with tab_h:
    st.markdown('<div class="section-title">📜 Histórico de Ordens de Produção</div>',
                unsafe_allow_html=True)
    if not st.session_state.op_history:
        st.info("Nenhuma OP confirmada ainda.")
    else:
        _df_hist = pd.DataFrame(st.session_state.op_history)
        _fh1, _fh2, _fh3 = st.columns(3)
        with _fh1:
            _op_f  = st.selectbox("Filtrar por OP",     ["Todas"]  + sorted(_df_hist["OP"].unique().tolist(), reverse=True))
        with _fh2:
            _mf    = st.selectbox("Filtrar por Máquina", ["Todas"] + sorted(_df_hist["Máquina"].unique().tolist()))
        with _fh3:
            _pf    = st.selectbox("Filtrar por Pedido",  ["Todos"] + sorted(_df_hist["Pedido"].unique().tolist()))
        _dv = _df_hist.copy()
        if _op_f != "Todas": _dv = _dv[_dv["OP"]      == _op_f]
        if _mf   != "Todas": _dv = _dv[_dv["Máquina"] == _mf]
        if _pf   != "Todos": _dv = _dv[_dv["Pedido"]  == _pf]
        st.dataframe(_dv, use_container_width=True, hide_index=True)
        st.markdown(f"**{len(_dv)} registros** de {len(_df_hist)} no total.")
        st.divider()
        _buf_csv = io.StringIO()
        _df_hist.to_csv(_buf_csv, index=False, encoding="utf-8-sig")
        st.download_button(
            "📥 Exportar histórico completo (.csv)",
            data=_buf_csv.getvalue().encode("utf-8-sig"),
            file_name=f"flexotimiza_historico_{datetime.now(ZoneInfo('America/Sao_Paulo')).strftime('%Y%m%d')}.csv",
            mime="text/csv",
        )
        if st.button("🗑️ Limpar histórico", key="clear_hist"):
            st.session_state.op_history = []
            st.session_state.op_counter = 1
            st.rerun()
