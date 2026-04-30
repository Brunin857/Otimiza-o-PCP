# FlexOtimiza — Planejamento de Corte de Bobinas

Ferramenta de otimização de sequenciamento e agrupamento de lotes para indústria flexográfica.

## Como rodar localmente

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Como publicar no Streamlit Cloud (acesso via link para o cliente)

1. Crie uma conta gratuita em https://streamlit.io
2. Suba os arquivos para um repositório GitHub:
   - app.py
   - models.py
   - optimizer.py
   - pattern_generator.py
   - requirements.txt
3. No Streamlit Cloud, clique em "New app" e aponte para o repositório
4. O cliente acessa pelo link gerado — sem instalar nada

## Estrutura dos arquivos

| Arquivo | Função |
|---|---|
| `app.py` | Interface Streamlit (tela do supervisor) |
| `models.py` | Estruturas de dados (pedidos, máquinas, bobinas) |
| `pattern_generator.py` | Gerador de padrões de corte (Cutting Stock) |
| `optimizer.py` | Motor de otimização OR-Tools CP-SAT |
| `requirements.txt` | Dependências Python |

## Parâmetros a atualizar após a cronoanálise

Em **Configurações → Parâmetros de Setup**:
- Tempo fixo de setup (min) — medir em campo
- Tempo por faca alterada (min) — medir em campo

Em **Configurações → Parâmetros das Máquinas**:
- Comprimento padrão da bobina-mãe (m) — confirmar com o cliente
- Largura bobina grande (mm) — confirmar com o cliente
- Largura bobina pequena (mm) — confirmar com o cliente
