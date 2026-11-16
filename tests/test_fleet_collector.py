"""Testes da junção AD + Tactical + GLPI + Kaspersky, sem acessar nenhum servidor.

Uso: python tests/test_fleet_collector.py      (precisa de: pip install requests)
"""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src'))
import fleet_collector as fc  # noqa: E402

AGORA = dt.datetime(2026, 11, 16, 12, 0, tzinfo=dt.timezone.utc)


def iso(dias_atras):
    return (AGORA - dt.timedelta(days=dias_atras)).isoformat()


def filetime(dias_atras):
    """lastLogonTimestamp do AD: intervalos de 100 ns desde 01/01/1601."""
    d = AGORA - dt.timedelta(days=dias_atras)
    return int((d - dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)).total_seconds() * 10_000_000)


fc.aplicar_config_unidades({'SITES': 'HQ,BR01,BR02', 'SITE_ALIASES': 'matriz=HQ;filial norte=BR02', 'HOST_PREFIX': 'CORP-'})

# ---- unidade: pelo nome, pelo nome com número (BR012 -> BR01), pela entidade do GLPI, ou OUTROS
assert fc.unidade('CORP-HQ-101') == 'HQ'
assert fc.unidade('CORP-BR01-007') == 'BR01'
assert fc.unidade('NOTEBOOK-JOAO', 'Empresa > Filial Norte') == 'BR02'
assert fc.unidade('DESKTOP-ABC123') == 'OUTROS'
assert fc.curto('CORP-HQ-101') == 'HQ-101' and fc.curto('DESKTOP-X') == 'DESKTOP-X'

# ---- datas: FILETIME do AD, ISO do Tactical/KSC e data local sem fuso do GLPI
assert abs(fc.dias(AGORA, filetime(3)) - 3) < 0.01
assert abs(fc.dias(AGORA, iso(1.5)) - 1.5) < 0.01
assert fc.dias(AGORA, None) is None and fc.dias(AGORA, '0') is None

ad = {
    'CORP-HQ-101': {'so': 'Windows 11 Pro', 'logon': filetime(1), 'ou': 'Estacoes', 'habilitado': True, 'criado': None},
    'CORP-HQ-102': {'so': 'Windows 11 Pro', 'logon': filetime(2), 'ou': 'Estacoes', 'habilitado': True, 'criado': None},
    'CORP-BR01-007': {'so': 'Windows 10 Pro', 'logon': filetime(120), 'ou': 'Estacoes', 'habilitado': True, 'criado': None},
    'CORP-HQ-SRV1': {'so': 'Windows Server 2022', 'logon': filetime(0.1), 'ou': 'Servidores', 'habilitado': True, 'criado': None},
    'CORP-HQ-103': {'so': 'Windows 11 Pro', 'logon': filetime(1), 'ou': 'Estacoes', 'habilitado': False, 'criado': None},
}
tac = {
    'CORP-HQ-101': {'status': 'online', 'visto': iso(0.01), 'usuario': 'CORP\\ana', 'ip': '192.0.2.10', 'serial': 'S1',
                    'modelo': 'Laptop X', 'so': 'Windows 11', 'tipo': 'workstation', 'site': 'HQ'},
    'CORP-HQ-102': {'status': 'offline', 'visto': iso(20), 'usuario': '', 'ip': '', 'serial': '', 'modelo': '',
                    'so': 'Windows 11', 'tipo': 'workstation', 'site': 'HQ'},
    'OLD-PC-01': {'status': 'overdue', 'visto': iso(2), 'usuario': '', 'ip': '', 'serial': '', 'modelo': '',
                  'so': 'Windows 10', 'tipo': 'workstation', 'site': 'HQ'},
}
glpi = {
    'CORP-HQ-101': {'inventario': iso(0.5), 'estado': 'Em uso', 'usuario': 'ana', 'entidade': 'Empresa > Matriz',
                    'serial': 'S1', 'id': 1, 'cadastros': 1},
    'CORP-HQ-102': {'inventario': iso(1), 'estado': 'Em uso', 'usuario': '', 'entidade': 'Empresa > Matriz',
                    'serial': '', 'id': 2, 'cadastros': 2},
}
ksc = {
    'CORP-HQ-101': {'contato': iso(0.1), 'kes': '12.10', 'protegida': True, 'ip': '192.0.2.10'},
    'CORP-HQ-102': {'contato': iso(0.2), 'kes': '12.10', 'protegida': True, 'ip': ''},
}

maquinas = fc.juntar(AGORA, ad, tac, glpi, ksc, 30, 7)
m = {x['maquina']: x for x in maquinas}

assert m['CORP-HQ-101']['completo'] and m['CORP-HQ-101']['situacao'] == 'Completa'
assert m['CORP-HQ-101']['usuario'] == 'ana' and m['CORP-HQ-101']['unidade'] == 'HQ'
assert m['CORP-HQ-102']['situacao'] == 'Falta Tactical', m['CORP-HQ-102']['situacao']    # agente há 20 dias sem falar
assert m['CORP-BR01-007']['situacao'] == 'Inativa no AD'                                # 120 dias sem logon
assert m['CORP-HQ-103']['situacao'] == 'Inativa no AD'                                  # conta desabilitada
assert m['OLD-PC-01']['situacao'] == 'Fora do AD' and not m['OLD-PC-01']['em_ad']       # lixo nas ferramentas
assert m['CORP-HQ-SRV1']['tipo'] == 'servidor'

g, unid, pend = fc.resumo(maquinas)
assert g['ativas'] == 2 and g['completas'] == 1 and g['pct_completas'] == 50.0, g
assert g['fora_ad_tactical'] == 1 and g['glpi_duplicados'] == 1
assert unid['HQ']['ativas'] == 2 and unid['HQ']['pct_completas'] == 50.0
motivos = {k: [x['maquina'] for x in v] for k, v in pend}
assert motivos['Sem Tactical'] == ['CORP-HQ-102'] and motivos['Sem Kaspersky'] == []
assert motivos['Sem logon no AD > 90 dias'] == ['CORP-BR01-007']

# ---- sem antivírus configurado: a cobertura só exige Tactical + GLPI e não lista "Sem Kaspersky"
sem_av = {x['maquina']: x for x in fc.juntar(AGORA, ad, tac, glpi, {}, 30, 7, usa_av=False)}
assert sem_av['CORP-HQ-101']['completo'], sem_av['CORP-HQ-101']['faltando']
_, _, pend2 = fc.resumo(list(sem_av.values()), usa_av=False)
assert 'Sem Kaspersky' not in dict(pend2)

# ---- line protocol do InfluxDB: escapa espaço/vírgula/igual nas tags, int com sufixo i, texto entre aspas
ln = fc.linha('parque_maquina', {'maquina': 'CORP HQ,1', 'unidade': 'HQ'}, {'ok': True, 'n': 3, 'd': 1.5, 's': 'a"b', 'x': None}, 100)
assert ln == 'parque_maquina,maquina=CORP\\ HQ\\,1,unidade=HQ ok=true,n=3i,d=1.5,s="a\'b" 100', ln

print('TODOS OS TESTES PASSARAM')
